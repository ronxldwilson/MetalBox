package main

import (
	"embed"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"gopkg.in/yaml.v3"
)

//go:embed static/index.html
var staticFS embed.FS

var (
	configPath string
	runDir     string
	logDir     string
	wrapperDir string
)

// --- Config types ---

type ServiceConfig struct {
	Command     string            `yaml:"command"`
	Workdir     string            `yaml:"workdir"`
	Env         map[string]string `yaml:"env"`
	EnvInherit  bool              `yaml:"env_inherit"`
	Ports       []int             `yaml:"ports"`
	Sandbox     *SandboxConfig    `yaml:"sandbox"`
	Resources   ResourceConfig    `yaml:"resources"`
	Restart     string            `yaml:"restart"`
	Healthcheck *HealthConfig     `yaml:"healthcheck"`
	DependsOn   []string          `yaml:"depends_on"`
}

type SandboxConfig struct {
	ReadOnly  []string `yaml:"read_only"`
	ReadWrite []string `yaml:"read_write"`
	AllowNet  bool     `yaml:"allow_net"`
}

type ResourceConfig struct {
	Memory      string `yaml:"memory"`
	MetalMemory string `yaml:"metal_memory"`
	MetalCache  string `yaml:"metal_cache"`
	CPUs        string `yaml:"cpus"`
}

type HealthConfig struct {
	URL         string `yaml:"url"`
	TCP         string `yaml:"tcp"`
	Cmd         string `yaml:"cmd"`
	Interval    int    `yaml:"interval"`
	Timeout     int    `yaml:"timeout"`
	Retries     int    `yaml:"retries"`
	StartPeriod int    `yaml:"start_period"`
}

type Config struct {
	Services map[string]ServiceConfig `yaml:"services"`
}

// --- Runtime state per service ---

type serviceState struct {
	mu              sync.Mutex
	pid             int
	restarts        int
	guardStop       chan struct{}
	healthStop      chan struct{}
	healthy         *bool
	healthFailures  int
	manuallyStopped bool
	events          []serviceEvent
}

type serviceEvent struct {
	Time    time.Time `json:"time"`
	Type    string    `json:"type"`
	Message string    `json:"message"`
}

var (
	states   = map[string]*serviceState{}
	statesMu sync.Mutex
)

func getState(name string) *serviceState {
	statesMu.Lock()
	defer statesMu.Unlock()
	s, ok := states[name]
	if !ok {
		s = &serviceState{}
		states[name] = s
	}
	return s
}

// --- API response type ---

type ServiceStatus struct {
	Name     string         `json:"name"`
	PID      *int           `json:"pid"`
	Status   string         `json:"status"`
	Healthy  *bool          `json:"healthy"`
	RSS      *float64       `json:"rss_mb"`
	CPU      *float64       `json:"cpu_percent"`
	Limit    string         `json:"limit"`
	LimitMB  *float64       `json:"limit_mb"`
	Uptime   string         `json:"uptime"`
	Command  string         `json:"command"`
	Restarts int            `json:"restarts"`
	CPUMode  string         `json:"cpu_mode"`
	Events   []serviceEvent `json:"events"`
}

// --- Size parser ---

var sizeRe = regexp.MustCompile(`(?i)^(\d+(?:\.\d+)?)\s*([kmgt])b?$`)

func parseBytes(s string) int64 {
	if s == "" {
		return 0
	}
	m := sizeRe.FindStringSubmatch(s)
	if m == nil {
		n, _ := strconv.ParseInt(s, 10, 64)
		return n
	}
	val, _ := strconv.ParseFloat(m[1], 64)
	switch strings.ToLower(m[2]) {
	case "k":
		return int64(val * 1024)
	case "m":
		return int64(val * 1024 * 1024)
	case "g":
		return int64(val * 1024 * 1024 * 1024)
	case "t":
		return int64(val * 1024 * 1024 * 1024 * 1024)
	}
	return 0
}

// --- Metal wrapper ---

const metalModuleWrapper = `import os, sys

_metal_memory = %d
_metal_cache = %d

try:
    import mlx.core as mx
    if _metal_memory:
        mx.metal.set_memory_limit(_metal_memory)
    if _metal_cache:
        mx.metal.set_cache_limit(_metal_cache)
except ImportError:
    pass

sys.argv = %s
from runpy import run_module
run_module(%q, run_name="__main__")
`

const metalScriptWrapper = `import os, sys

_metal_memory = %d
_metal_cache = %d

try:
    import mlx.core as mx
    if _metal_memory:
        mx.metal.set_memory_limit(_metal_memory)
    if _metal_cache:
        mx.metal.set_cache_limit(_metal_cache)
except ImportError:
    pass

sys.argv = %s
script = %q
with open(script) as f:
    code = compile(f.read(), script, "exec")
exec(code, {"__name__": "__main__", "__file__": script})
`

func metalWrapCommand(command string, metalMem, metalCache int64, name string) string {
	if metalMem == 0 && metalCache == 0 {
		return command
	}

	parts := strings.Fields(command)
	pyIdx := -1
	for i, p := range parts {
		base := filepath.Base(p)
		if strings.HasPrefix(base, "python") {
			pyIdx = i
			break
		}
	}
	if pyIdx < 0 {
		return command
	}

	prefix := ""
	if pyIdx > 0 {
		prefix = strings.Join(parts[:pyIdx], " ") + " "
	}
	python := parts[pyIdx]
	rest := parts[pyIdx+1:]

	os.MkdirAll(wrapperDir, 0755)
	wrapperPath := filepath.Join(wrapperDir, name+"_metal_wrapper.py")

	var wrapper string
	if len(rest) >= 2 && rest[0] == "-m" {
		module := rest[1]
		argv := append([]string{module}, rest[2:]...)
		argvStr := pythonListRepr(argv)
		wrapper = fmt.Sprintf(metalModuleWrapper, metalMem, metalCache, argvStr, module)
	} else if len(rest) >= 1 && !strings.HasPrefix(rest[0], "-") {
		script := rest[0]
		argvStr := pythonListRepr(rest)
		wrapper = fmt.Sprintf(metalScriptWrapper, metalMem, metalCache, argvStr, script)
	} else {
		return command
	}

	os.WriteFile(wrapperPath, []byte(wrapper), 0644)
	return fmt.Sprintf("%s%s %s", prefix, python, wrapperPath)
}

func pythonListRepr(items []string) string {
	quoted := make([]string, len(items))
	for i, s := range items {
		quoted[i] = fmt.Sprintf("%q", s)
	}
	return "[" + strings.Join(quoted, ", ") + "]"
}

// --- Main ---

func main() {
	home, _ := os.UserHomeDir()
	runDir = filepath.Join(home, ".metalbox", "run")
	logDir = filepath.Join(home, ".metalbox", "logs")
	wrapperDir = filepath.Join(home, ".metalbox", "wrappers")

	configPath = "metalbox.yml"
	if len(os.Args) > 1 {
		configPath = os.Args[1]
	}
	if v := os.Getenv("METALBOX_CONFIG"); v != "" {
		configPath = v
	}

	port := "9090"
	if v := os.Getenv("METALBOX_PORT"); v != "" {
		port = v
	}

	restoreRunningServices()

	mux := http.NewServeMux()
	mux.HandleFunc("/api/services", handleServices)
	mux.HandleFunc("/api/services/", handleServiceAction)
	mux.HandleFunc("/api/logs/", handleLogs)
	mux.HandleFunc("/", handleIndex)

	log.Printf("metalbox dashboard on http://localhost:%s", port)
	log.Fatal(http.ListenAndServe(":"+port, mux))
}

func restoreRunningServices() {
	cfg, err := loadConfig()
	if err != nil {
		return
	}
	for name, svc := range cfg.Services {
		pidFile := filepath.Join(runDir, name+".pid")
		pidData, err := os.ReadFile(pidFile)
		if err != nil {
			continue
		}
		pid, _ := strconv.Atoi(strings.TrimSpace(string(pidData)))
		if pid > 0 && processAlive(pid) {
			st := getState(name)
			st.pid = pid
			st.guardStop = make(chan struct{})
			go rssGuard(name, pid, parseBytes(svc.Resources.Memory), svc, st)
			if svc.Healthcheck != nil {
				st.healthStop = make(chan struct{})
				go healthChecker(name, svc, st)
			}
			log.Printf("[%s] restored for running process (pid %d)", name, pid)
		}
	}
}

func loadConfig() (*Config, error) {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return nil, err
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}

// --- Handlers ---

func handleIndex(w http.ResponseWriter, r *http.Request) {
	data, _ := staticFS.ReadFile("static/index.html")
	w.Header().Set("Content-Type", "text/html")
	w.Write(data)
}

func handleServices(w http.ResponseWriter, r *http.Request) {
	cfg, err := loadConfig()
	if err != nil {
		httpErr(w, "config error: "+err.Error(), 500)
		return
	}

	var services []ServiceStatus
	for name, svc := range cfg.Services {
		st := getState(name)
		st.mu.Lock()

		s := ServiceStatus{
			Name:     name,
			Status:   "stopped",
			Limit:    svc.Resources.Memory,
			Command:  svc.Command,
			Restarts: st.restarts,
			CPUMode:  svc.Resources.CPUs,
			Events:   st.events,
			Healthy:  st.healthy,
		}

		memLimit := parseBytes(svc.Resources.Memory)
		if memLimit > 0 {
			mb := float64(memLimit) / (1024 * 1024)
			s.LimitMB = &mb
		}

		pidFile := filepath.Join(runDir, name+".pid")
		if pidData, err := os.ReadFile(pidFile); err == nil {
			pid, _ := strconv.Atoi(strings.TrimSpace(string(pidData)))
			if pid > 0 && processAlive(pid) {
				s.PID = &pid
				s.Status = "running"
				rss, cpu := getProcessStats(pid)
				if rss > 0 {
					rssMB := float64(rss) / (1024 * 1024)
					s.RSS = &rssMB
				}
				if cpu >= 0 {
					s.CPU = &cpu
				}
				s.Uptime = getUptime(pid)
			} else {
				s.Status = "dead"
			}
		}

		if s.Events == nil {
			s.Events = []serviceEvent{}
		}
		st.mu.Unlock()
		services = append(services, s)
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(services)
}

func handleServiceAction(w http.ResponseWriter, r *http.Request) {
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/services/"), "/")
	if len(parts) < 2 {
		httpErr(w, "usage: /api/services/{name}/{action}", 400)
		return
	}
	name, action := parts[0], parts[1]

	if r.Method != http.MethodPost {
		httpErr(w, "POST only", 405)
		return
	}

	cfg, err := loadConfig()
	if err != nil {
		httpErr(w, "config error: "+err.Error(), 500)
		return
	}
	svc, ok := cfg.Services[name]
	if !ok {
		httpErr(w, "unknown service: "+name, 404)
		return
	}

	switch action {
	case "stop":
		if err := stopService(name); err != nil {
			httpErr(w, err.Error(), 500)
			return
		}
		jsonOK(w, map[string]string{"status": "stopped", "service": name})

	case "start":
		if err := startService(name, svc); err != nil {
			httpErr(w, err.Error(), 500)
			return
		}
		jsonOK(w, map[string]string{"status": "started", "service": name})

	case "restart":
		stopService(name)
		time.Sleep(500 * time.Millisecond)
		if err := startService(name, svc); err != nil {
			httpErr(w, err.Error(), 500)
			return
		}
		jsonOK(w, map[string]string{"status": "restarted", "service": name})

	default:
		httpErr(w, "unknown action: "+action, 400)
	}
}

func handleLogs(w http.ResponseWriter, r *http.Request) {
	name := strings.TrimPrefix(r.URL.Path, "/api/logs/")
	if name == "" {
		httpErr(w, "usage: /api/logs/{service}", 400)
		return
	}

	lines := 100
	if v := r.URL.Query().Get("lines"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			lines = n
		}
	}

	logFile := filepath.Join(logDir, name, name+".log")
	data, err := os.ReadFile(logFile)
	if err != nil {
		w.Header().Set("Content-Type", "text/plain")
		w.Write([]byte("no logs available"))
		return
	}

	allLines := strings.Split(string(data), "\n")
	start := len(allLines) - lines
	if start < 0 {
		start = 0
	}
	w.Header().Set("Content-Type", "text/plain")
	w.Write([]byte(strings.Join(allLines[start:], "\n")))
}

// --- Service lifecycle ---

func startService(name string, svc ServiceConfig) error {
	os.MkdirAll(runDir, 0755)

	st := getState(name)
	st.mu.Lock()
	st.manuallyStopped = false
	st.mu.Unlock()

	pidFile := filepath.Join(runDir, name+".pid")
	if pidData, err := os.ReadFile(pidFile); err == nil {
		pid, _ := strconv.Atoi(strings.TrimSpace(string(pidData)))
		if pid > 0 && processAlive(pid) {
			return fmt.Errorf("%s already running (pid %d)", name, pid)
		}
	}

	workdir := svc.Workdir
	if workdir == "" {
		abs, _ := filepath.Abs(filepath.Dir(configPath))
		workdir = abs
	}

	// Port conflict detection
	for _, port := range svc.Ports {
		if err := checkPort(port); err != nil {
			return fmt.Errorf("port %d: %w", port, err)
		}
	}

	// Build environment — clean by default, full inherit opt-in
	env := buildEnv(svc)

	metalMem := parseBytes(svc.Resources.MetalMemory)
	metalCache := parseBytes(svc.Resources.MetalCache)
	if metalMem > 0 {
		env = append(env, fmt.Sprintf("METALBOX_METAL_MEMORY=%d", metalMem))
	}
	if metalCache > 0 {
		env = append(env, fmt.Sprintf("METALBOX_METAL_CACHE=%d", metalCache))
	}

	// Metal wrapper injection
	cmdStr := metalWrapCommand(svc.Command, metalMem, metalCache, name)
	if cmdStr != svc.Command {
		log.Printf("[%s] Metal limits injected (mem=%d, cache=%d)", name, metalMem, metalCache)
	}

	// taskpolicy for background CPU
	if svc.Resources.CPUs == "background" {
		cmdStr = "taskpolicy -b " + cmdStr
	}

	// Sandbox wrapping
	if svc.Sandbox != nil {
		profile := generateSandboxProfile(svc.Sandbox, workdir, logDir)
		profilePath := filepath.Join(wrapperDir, name+"_sandbox.sb")
		os.MkdirAll(wrapperDir, 0755)
		os.WriteFile(profilePath, []byte(profile), 0644)
		cmdStr = fmt.Sprintf("sandbox-exec -f %s %s", profilePath, cmdStr)
		log.Printf("[%s] sandbox profile: %s", name, profilePath)
	}

	svcLogDir := filepath.Join(logDir, name)
	os.MkdirAll(svcLogDir, 0755)
	logFile, err := os.OpenFile(
		filepath.Join(svcLogDir, name+".log"),
		os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644,
	)
	if err != nil {
		return fmt.Errorf("log file: %w", err)
	}

	cmd := exec.Command("bash", "-c", cmdStr)
	cmd.Dir = workdir
	cmd.Env = env
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		logFile.Close()
		return fmt.Errorf("start failed: %w", err)
	}

	pid := cmd.Process.Pid
	os.WriteFile(pidFile, []byte(strconv.Itoa(pid)), 0644)

	st.mu.Lock()
	st.pid = pid
	st.healthy = nil
	addEvent(st, "started", fmt.Sprintf("pid %d", pid))
	if svc.Resources.CPUs == "background" {
		addEvent(st, "taskpolicy", "pinned to E-cores (background QoS)")
	}
	if cmdStr != svc.Command && (metalMem > 0 || metalCache > 0) {
		addEvent(st, "metal", fmt.Sprintf("memory=%s cache=%s", svc.Resources.MetalMemory, svc.Resources.MetalCache))
	}
	if !svc.EnvInherit {
		addEvent(st, "isolation", "clean environment (only declared vars)")
	}
	if svc.Sandbox != nil {
		addEvent(st, "sandbox", "filesystem restrictions active")
	}
	if len(svc.Ports) > 0 {
		addEvent(st, "ports", fmt.Sprintf("validated: %v", svc.Ports))
	}
	// Stop old goroutines if any
	if st.guardStop != nil {
		close(st.guardStop)
	}
	st.guardStop = make(chan struct{})
	if st.healthStop != nil {
		close(st.healthStop)
	}
	st.healthStop = make(chan struct{})
	st.mu.Unlock()

	log.Printf("[%s] started (pid %d, limit=%s, cpus=%s)", name, pid, svc.Resources.Memory, svc.Resources.CPUs)

	// RSS guard
	memLimit := parseBytes(svc.Resources.Memory)
	if memLimit > 0 {
		go rssGuard(name, pid, memLimit, svc, st)
	}

	// Health checker
	if svc.Healthcheck != nil {
		go healthChecker(name, svc, st)
	}

	// Wait for process in background, handle exit
	go func() {
		cmd.Wait()
		logFile.Close()
		exitCode := cmd.ProcessState.ExitCode()

		st.mu.Lock()
		addEvent(st, "exited", fmt.Sprintf("code %d", exitCode))
		st.mu.Unlock()

		// Auto-restart based on policy
		if shouldRestart(svc.Restart, exitCode, st) {
			st.mu.Lock()
			st.restarts++
			n := st.restarts
			backoff := time.Duration(math.Min(float64(n*n), 30)) * time.Second
			addEvent(st, "restarting", fmt.Sprintf("attempt %d, backoff %s", n, backoff))
			st.mu.Unlock()

			log.Printf("[%s] exited (%d), restarting in %s", name, exitCode, backoff)
			time.Sleep(backoff)
			os.Remove(pidFile)
			if err := startService(name, svc); err != nil {
				log.Printf("[%s] restart failed: %s", name, err)
			}
		}
	}()

	return nil
}

func shouldRestart(policy string, exitCode int, st *serviceState) bool {
	st.mu.Lock()
	defer st.mu.Unlock()
	if st.manuallyStopped {
		return false
	}
	switch policy {
	case "always":
		return true
	case "unless-stopped":
		return true
	case "on-failure":
		return exitCode != 0
	}
	return false
}

// --- Environment isolation ---

var minimalEnvKeys = []string{
	"PATH", "HOME", "USER", "SHELL", "LANG", "TERM",
	"TMPDIR", "XDG_RUNTIME_DIR", "LC_ALL", "LC_CTYPE",
}

func buildEnv(svc ServiceConfig) []string {
	var env []string

	if svc.EnvInherit {
		env = os.Environ()
	} else {
		// Clean env — only essential system vars
		for _, key := range minimalEnvKeys {
			if val, ok := os.LookupEnv(key); ok {
				env = append(env, key+"="+val)
			}
		}
	}

	env = append(env, "PYTHONUNBUFFERED=1")

	for k, v := range svc.Env {
		env = append(env, k+"="+v)
	}

	return env
}

// --- Port conflict detection ---

func checkPort(port int) error {
	// Check both loopback and wildcard to catch all bindings
	for _, host := range []string{"127.0.0.1", "0.0.0.0"} {
		addr := fmt.Sprintf("%s:%d", host, port)
		ln, err := net.Listen("tcp", addr)
		if err != nil {
			return fmt.Errorf("already in use (or no permission)")
		}
		ln.Close()
	}
	return nil
}

// --- Sandbox (macOS sandbox-exec) ---

func generateSandboxProfile(sb *SandboxConfig, workdir, logDir string) string {
	// Allow-default model: permit everything, then deny specific things.
	// deny-default is impractical — Python/Node/Go runtimes need hundreds
	// of low-level macOS operations (dyld, mach ports, IOKit, etc.)
	var b strings.Builder
	b.WriteString("(version 1)\n")
	b.WriteString("(allow default)\n\n")

	// Deny networking unless explicitly allowed
	if !sb.AllowNet {
		b.WriteString("; networking denied\n")
		b.WriteString("(deny network*)\n\n")
	}

	// Deny writes to filesystem EXCEPT allowed paths
	// This is the main isolation: process can read anywhere but only
	// write to workdir, log dir, tmp, and user-specified paths
	b.WriteString("; deny file writes everywhere\n")
	b.WriteString("(deny file-write* (subpath \"/\"))\n\n")

	// Re-allow writes to specific paths
	b.WriteString("; allow writes to workdir\n")
	b.WriteString(fmt.Sprintf("(allow file-write* (subpath \"%s\"))\n\n", workdir))

	b.WriteString("; allow writes to logs and tmp\n")
	b.WriteString(fmt.Sprintf("(allow file-write* (subpath \"%s\"))\n", logDir))
	b.WriteString("(allow file-write* (subpath \"/private/tmp\"))\n")
	b.WriteString("(allow file-write* (subpath \"/tmp\"))\n")
	b.WriteString("(allow file-write* (subpath \"/dev\"))\n")

	home, _ := os.UserHomeDir()
	// Python/uv need write access to cache dirs
	b.WriteString(fmt.Sprintf("(allow file-write* (subpath \"%s/Library/Caches\"))\n", home))
	b.WriteString(fmt.Sprintf("(allow file-write* (subpath \"%s/.cache\"))\n", home))
	b.WriteString("\n")

	// Read-only paths — deny writes explicitly
	if len(sb.ReadOnly) > 0 {
		b.WriteString("; user-specified read-only paths (deny writes)\n")
		for _, p := range sb.ReadOnly {
			b.WriteString(fmt.Sprintf("(deny file-write* (subpath \"%s\"))\n", p))
		}
		b.WriteString("\n")
	}

	// Extra read-write paths
	if len(sb.ReadWrite) > 0 {
		b.WriteString("; user-specified read-write paths\n")
		for _, p := range sb.ReadWrite {
			b.WriteString(fmt.Sprintf("(allow file-write* (subpath \"%s\"))\n", p))
		}
		b.WriteString("\n")
	}

	return b.String()
}

func stopService(name string) error {
	st := getState(name)
	st.mu.Lock()
	if st.guardStop != nil {
		close(st.guardStop)
		st.guardStop = nil
	}
	if st.healthStop != nil {
		close(st.healthStop)
		st.healthStop = nil
	}
	st.mu.Unlock()

	pidFile := filepath.Join(runDir, name+".pid")
	pidData, err := os.ReadFile(pidFile)
	if err != nil {
		return fmt.Errorf("%s not running", name)
	}
	pid, _ := strconv.Atoi(strings.TrimSpace(string(pidData)))
	if pid > 0 {
		killProcessTree(pid, syscall.SIGTERM)
		for i := 0; i < 10; i++ {
			time.Sleep(500 * time.Millisecond)
			if !anyTreeAlive(pid) {
				break
			}
		}
		if anyTreeAlive(pid) {
			killProcessTree(pid, syscall.SIGKILL)
		}
	}
	os.Remove(pidFile)

	st.mu.Lock()
	st.manuallyStopped = true
	addEvent(st, "stopped", "user stopped")
	st.pid = 0
	st.healthy = nil
	st.mu.Unlock()

	log.Printf("[%s] stopped", name)
	return nil
}

// --- RSS Guard ---

func rssGuard(name string, pid int, memLimit int64, svc ServiceConfig, st *serviceState) {
	if memLimit <= 0 {
		return
	}
	limitMB := float64(memLimit) / (1024 * 1024)
	log.Printf("[%s] guard: RSS limit %.0fMB", name, limitMB)

	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-st.guardStop:
			return
		case <-ticker.C:
			if !processAlive(pid) {
				return
			}

			rss, _ := getProcessStats(pid)
			if rss <= 0 {
				continue
			}

			if rss > memLimit {
				rssMB := float64(rss) / (1024 * 1024)
				log.Printf("[%s] memory %.0fMB > limit %.0fMB — killing process tree (pid %d)",
					name, rssMB, limitMB, pid)

				st.mu.Lock()
				addEvent(st, "oom-kill", fmt.Sprintf("memory %.0fMB > limit %.0fMB", rssMB, limitMB))
				st.mu.Unlock()

				killProcessTree(pid, syscall.SIGTERM)
				time.Sleep(5 * time.Second)
				if anyTreeAlive(pid) {
					killProcessTree(pid, syscall.SIGKILL)
				}
				os.Remove(filepath.Join(runDir, name+".pid"))
				return
			}
		}
	}
}

// --- Health checker ---

func healthChecker(name string, svc ServiceConfig, st *serviceState) {
	hc := svc.Healthcheck
	if hc == nil {
		return
	}

	interval := time.Duration(hc.Interval) * time.Second
	if interval == 0 {
		interval = 30 * time.Second
	}
	timeout := time.Duration(hc.Timeout) * time.Second
	if timeout == 0 {
		timeout = 10 * time.Second
	}
	retries := hc.Retries
	if retries == 0 {
		retries = 3
	}
	startPeriod := time.Duration(hc.StartPeriod) * time.Second

	// Wait for start period
	select {
	case <-st.healthStop:
		return
	case <-time.After(startPeriod):
	}

	log.Printf("[%s] health checker started (interval=%s, retries=%d)", name, interval, retries)
	failures := 0

	for {
		select {
		case <-st.healthStop:
			return
		case <-time.After(interval):
			ok := checkHealth(hc, timeout)

			st.mu.Lock()
			if ok {
				if st.healthy == nil || !*st.healthy {
					t := true
					st.healthy = &t
					addEvent(st, "healthy", "health check passed")
					log.Printf("[%s] healthy", name)
				}
				failures = 0
			} else {
				failures++
				log.Printf("[%s] health check failed (%d/%d)", name, failures, retries)
				if failures >= retries {
					f := false
					st.healthy = &f
					addEvent(st, "unhealthy", fmt.Sprintf("failed %d consecutive checks — restarting", retries))
					st.mu.Unlock()

					log.Printf("[%s] unhealthy — restarting", name)
					stopService(name)
					time.Sleep(1 * time.Second)
					startService(name, svc)
					return
				}
			}
			st.mu.Unlock()
		}
	}
}

func checkHealth(hc *HealthConfig, timeout time.Duration) bool {
	if hc.URL != "" {
		return checkHTTP(hc.URL, timeout)
	}
	if hc.TCP != "" {
		return checkTCP(hc.TCP, timeout)
	}
	if hc.Cmd != "" {
		return checkCmd(hc.Cmd, timeout)
	}
	return true
}

func checkHTTP(url string, timeout time.Duration) bool {
	client := &http.Client{Timeout: timeout}
	resp, err := client.Get(url)
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode >= 200 && resp.StatusCode < 400
}

func checkTCP(addr string, timeout time.Duration) bool {
	conn, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

func checkCmd(command string, timeout time.Duration) bool {
	cmd := exec.Command("bash", "-c", command)
	done := make(chan error, 1)
	go func() { done <- cmd.Run() }()

	select {
	case err := <-done:
		return err == nil
	case <-time.After(timeout):
		if cmd.Process != nil {
			cmd.Process.Kill()
		}
		return false
	}
}

// --- Events ---

func addEvent(st *serviceState, typ, msg string) {
	st.events = append(st.events, serviceEvent{
		Time:    time.Now(),
		Type:    typ,
		Message: msg,
	})
	if len(st.events) > 50 {
		st.events = st.events[len(st.events)-50:]
	}
}

// --- OS helpers ---

func processAlive(pid int) bool {
	return syscall.Kill(pid, 0) == nil
}

// getDescendants returns all descendant PIDs of the given pid (recursive).
func getDescendants(pid int) []int {
	var result []int
	out, err := exec.Command("pgrep", "-P", strconv.Itoa(pid)).Output()
	if err != nil {
		return result
	}
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		line = strings.TrimSpace(line)
		if child, err := strconv.Atoi(line); err == nil {
			result = append(result, child)
			result = append(result, getDescendants(child)...)
		}
	}
	return result
}

// killProcessTree kills a process and all its descendants, bottom-up.
func killProcessTree(pid int, sig syscall.Signal) {
	descendants := getDescendants(pid)
	// Kill bottom-up (children first) to prevent orphaning
	for i := len(descendants) - 1; i >= 0; i-- {
		syscall.Kill(descendants[i], sig)
	}
	syscall.Kill(pid, sig)
	// Also signal the process group in case any were missed
	syscall.Kill(-pid, sig)
}

// anyTreeAlive checks if any process in the tree is still running.
func anyTreeAlive(pid int) bool {
	if processAlive(pid) {
		return true
	}
	for _, child := range getDescendants(pid) {
		if processAlive(child) {
			return true
		}
	}
	return false
}

func getProcessStats(pid int) (rss int64, cpu float64) {
	allPids := append([]int{pid}, getDescendants(pid)...)

	// Try footprint (more accurate than ps RSS — includes compressed pages)
	pidStrs := make([]string, len(allPids))
	for i, p := range allPids {
		pidStrs[i] = strconv.Itoa(p)
	}
	fpOut, fpErr := exec.Command("footprint", pidStrs...).Output()
	if fpErr == nil {
		// Parse "Summary Footprint: NNN MB" or single "Footprint: NNN MB"
		for _, line := range strings.Split(string(fpOut), "\n") {
			line = strings.TrimSpace(line)
			// Match both "Footprint: 44 MB" and "Summary Footprint: 60 MB"
			if idx := strings.Index(line, "Footprint:"); idx >= 0 {
				rest := strings.TrimSpace(line[idx+len("Footprint:"):])
				// Parse "44 MB" or "1.2 GB" or "800 KB"
				parts := strings.Fields(rest)
				if len(parts) >= 2 {
					val, err := strconv.ParseFloat(parts[0], 64)
					if err == nil {
						switch strings.ToUpper(parts[1]) {
						case "KB":
							rss = int64(val * 1024)
						case "MB":
							rss = int64(val * 1024 * 1024)
						case "GB":
							rss = int64(val * 1024 * 1024 * 1024)
						}
					}
				}
			}
			// "Summary Footprint" overrides individual ones
			if strings.HasPrefix(line, "Summary Footprint:") && rss > 0 {
				break
			}
		}
	}

	// Fallback to ps RSS tree sum if footprint failed or returned 0
	if rss <= 0 {
		for _, p := range allPids {
			psOut, err := exec.Command("ps", "-o", "rss=", "-p", strconv.Itoa(p)).Output()
			if err == nil {
				r, _ := strconv.ParseInt(strings.TrimSpace(string(psOut)), 10, 64)
				rss += r * 1024
			}
		}
	}

	// CPU% from ps across all pids in tree
	for _, p := range allPids {
		cpuOut, err := exec.Command("ps", "-o", "pcpu=", "-p", strconv.Itoa(p)).Output()
		if err == nil {
			c, _ := strconv.ParseFloat(strings.TrimSpace(string(cpuOut)), 64)
			cpu += c
		}
	}

	return
}

func getUptime(pid int) string {
	out, err := exec.Command("ps", "-o", "etime=", "-p", strconv.Itoa(pid)).Output()
	if err != nil {
		return "-"
	}
	return strings.TrimSpace(string(out))
}

func httpErr(w http.ResponseWriter, msg string, code int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

func jsonOK(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}
