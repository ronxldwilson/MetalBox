package main

import (
	"embed"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
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
)

type ServiceConfig struct {
	Command     string            `yaml:"command"`
	Workdir     string            `yaml:"workdir"`
	Env         map[string]string `yaml:"env"`
	Resources   ResourceConfig    `yaml:"resources"`
	Restart     string            `yaml:"restart"`
	Healthcheck *HealthConfig     `yaml:"healthcheck"`
	DependsOn   []string          `yaml:"depends_on"`
}

type ResourceConfig struct {
	Memory      string `yaml:"memory"`
	MetalMemory string `yaml:"metal_memory"`
	MetalCache  string `yaml:"metal_cache"`
	CPUs        string `yaml:"cpus"`
}

type HealthConfig struct {
	URL         string `yaml:"url"`
	Interval    int    `yaml:"interval"`
	Timeout     int    `yaml:"timeout"`
	Retries     int    `yaml:"retries"`
	StartPeriod int    `yaml:"start_period"`
}

type Config struct {
	Services map[string]ServiceConfig `yaml:"services"`
}

type ServiceStatus struct {
	Name     string   `json:"name"`
	PID      *int     `json:"pid"`
	Status   string   `json:"status"`
	RSS      *float64 `json:"rss_mb"`
	CPU      *float64 `json:"cpu_percent"`
	Limit    string   `json:"limit"`
	Uptime   string   `json:"uptime"`
	Command  string   `json:"command"`
	Restarts int      `json:"restarts"`
}

func main() {
	home, _ := os.UserHomeDir()
	runDir = filepath.Join(home, ".metalbox", "run")
	logDir = filepath.Join(home, ".metalbox", "logs")

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

	mux := http.NewServeMux()
	mux.HandleFunc("/api/services", handleServices)
	mux.HandleFunc("/api/services/", handleServiceAction)
	mux.HandleFunc("/api/logs/", handleLogs)
	mux.HandleFunc("/", handleIndex)

	log.Printf("metalbox dashboard on http://localhost:%s", port)
	log.Fatal(http.ListenAndServe(":"+port, mux))
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
		s := ServiceStatus{
			Name:    name,
			Status:  "stopped",
			Limit:   svc.Resources.Memory,
			Command: svc.Command,
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

	switch action {
	case "stop":
		pidFile := filepath.Join(runDir, name+".pid")
		pidData, err := os.ReadFile(pidFile)
		if err != nil {
			httpErr(w, "not running", 404)
			return
		}
		pid, _ := strconv.Atoi(strings.TrimSpace(string(pidData)))
		if pid > 0 {
			syscall.Kill(-pid, syscall.SIGTERM)
			time.Sleep(500 * time.Millisecond)
			os.Remove(pidFile)
		}
		jsonOK(w, map[string]string{"status": "stopped", "service": name})

	case "start", "restart":
		cmd := exec.Command("metalbox", action, name, "-f", configPath)
		out, err := cmd.CombinedOutput()
		if err != nil {
			httpErr(w, fmt.Sprintf("failed: %s\n%s", err, string(out)), 500)
			return
		}
		jsonOK(w, map[string]string{"status": "ok", "service": name, "output": string(out)})

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

func processAlive(pid int) bool {
	err := syscall.Kill(pid, 0)
	return err == nil
}

func getProcessStats(pid int) (rss int64, cpu float64) {
	// macOS: use ps to get RSS and %CPU
	out, err := exec.Command("ps", "-o", "rss=,pcpu=", "-p", strconv.Itoa(pid)).Output()
	if err != nil {
		return 0, -1
	}
	fields := strings.Fields(strings.TrimSpace(string(out)))
	if len(fields) >= 2 {
		r, _ := strconv.ParseInt(fields[0], 10, 64)
		rss = r * 1024 // ps reports in KB
		cpu, _ = strconv.ParseFloat(fields[1], 64)
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
