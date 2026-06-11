package main

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// --- styles ---

var (
	subtle    = lipgloss.NewStyle().Foreground(lipgloss.Color("241"))
	highlight = lipgloss.NewStyle().Foreground(lipgloss.Color("39"))
	green     = lipgloss.NewStyle().Foreground(lipgloss.Color("42"))
	red       = lipgloss.NewStyle().Foreground(lipgloss.Color("196"))
	yellow    = lipgloss.NewStyle().Foreground(lipgloss.Color("214"))
	bold      = lipgloss.NewStyle().Bold(true)
	dimmed    = lipgloss.NewStyle().Foreground(lipgloss.Color("243"))

	sidebarStyle = lipgloss.NewStyle().
			Padding(1, 1).
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("62"))

	mainStyle = lipgloss.NewStyle().
			Padding(1, 2).
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("62"))

	activeTabStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("39")).
			Bold(true).
			Underline(true)

	inactiveTabStyle = lipgloss.NewStyle().
				Foreground(lipgloss.Color("241"))

	statusBar = lipgloss.NewStyle().
			Foreground(lipgloss.Color("241")).
			Background(lipgloss.Color("236")).
			Padding(0, 1)

	titleStyle = lipgloss.NewStyle().
			Bold(true).
			Foreground(lipgloss.Color("39")).
			Padding(0, 1)
)

// --- messages ---

type tickMsg time.Time
type servicesMsg []ServiceStatus
type logsMsg string
type errMsg struct{ err error }

func (e errMsg) Error() string { return e.err.Error() }

// --- model ---

type tuiTab int

const (
	tabLogs tuiTab = iota
	tabStats
	tabEvents
)

type tuiModel struct {
	port       string
	services   []ServiceStatus
	selected   int
	tab        tuiTab
	logs       string
	width      int
	height     int
	logScroll  int
	err        error
	quitting   bool
	lastFetch  time.Time
	focusPanel int // 0=sidebar, 1=main
}

func newTuiModel(port string) tuiModel {
	return tuiModel{
		port:  port,
		tab:   tabLogs,
		width: 120, height: 40,
	}
}

// --- commands ---

func fetchServicesCmd(port string) tea.Cmd {
	return func() tea.Msg {
		resp, err := http.Get(fmt.Sprintf("http://localhost:%s/api/services", port))
		if err != nil {
			return errMsg{err}
		}
		defer resp.Body.Close()
		var svcs []ServiceStatus
		if err := json.NewDecoder(resp.Body).Decode(&svcs); err != nil {
			return errMsg{err}
		}
		return servicesMsg(svcs)
	}
}

func fetchLogsCmd(port, name string) tea.Cmd {
	return func() tea.Msg {
		resp, err := http.Get(fmt.Sprintf("http://localhost:%s/api/logs/%s?lines=100", port, name))
		if err != nil {
			return logsMsg("failed to fetch logs")
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		return logsMsg(string(body))
	}
}

func serviceActionCmd(port, name, action string) tea.Cmd {
	return func() tea.Msg {
		req, _ := http.NewRequest("POST", fmt.Sprintf("http://localhost:%s/api/services/%s/%s", port, name, action), nil)
		http.DefaultClient.Do(req)
		time.Sleep(500 * time.Millisecond)
		return tickMsg(time.Now())
	}
}

func tickCmd() tea.Cmd {
	return tea.Tick(2*time.Second, func(t time.Time) tea.Msg {
		return tickMsg(t)
	})
}

// --- bubbletea interface ---

func (m tuiModel) Init() tea.Cmd {
	return tea.Batch(fetchServicesCmd(m.port), tickCmd())
}

func (m tuiModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.KeyMsg:
		return m.handleKey(msg)

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		return m, nil

	case tickMsg:
		m.lastFetch = time.Time(msg)
		cmds := []tea.Cmd{fetchServicesCmd(m.port), tickCmd()}
		if len(m.services) > 0 && m.selected < len(m.services) {
			cmds = append(cmds, fetchLogsCmd(m.port, m.services[m.selected].Name))
		}
		return m, tea.Batch(cmds...)

	case servicesMsg:
		m.services = []ServiceStatus(msg)
		m.err = nil
		if m.selected >= len(m.services) && len(m.services) > 0 {
			m.selected = len(m.services) - 1
		}
		return m, nil

	case logsMsg:
		m.logs = string(msg)
		return m, nil

	case errMsg:
		m.err = msg.err
		return m, nil
	}

	return m, nil
}

func (m tuiModel) handleKey(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch msg.String() {
	case "q", "ctrl+c":
		m.quitting = true
		return m, tea.Quit

	case "tab":
		m.focusPanel = (m.focusPanel + 1) % 2
		return m, nil

	case "up", "k":
		if m.focusPanel == 0 {
			if m.selected > 0 {
				m.selected--
				m.logScroll = 0
			}
		} else {
			if m.logScroll > 0 {
				m.logScroll--
			}
		}
		return m, nil

	case "down", "j":
		if m.focusPanel == 0 {
			if m.selected < len(m.services)-1 {
				m.selected++
				m.logScroll = 0
			}
		} else {
			m.logScroll++
		}
		return m, nil

	case "1":
		m.tab = tabLogs
		return m, nil
	case "2":
		m.tab = tabStats
		return m, nil
	case "3":
		m.tab = tabEvents
		return m, nil

	case "s":
		if len(m.services) > 0 {
			svc := m.services[m.selected]
			if svc.Status == "running" {
				return m, serviceActionCmd(m.port, svc.Name, "stop")
			} else {
				return m, serviceActionCmd(m.port, svc.Name, "start")
			}
		}

	case "r":
		if len(m.services) > 0 {
			return m, serviceActionCmd(m.port, m.services[m.selected].Name, "restart")
		}

	case "enter":
		m.focusPanel = 1
		return m, nil
	}

	return m, nil
}

func (m tuiModel) View() string {
	if m.quitting {
		return ""
	}

	if m.err != nil && len(m.services) == 0 {
		return fmt.Sprintf("\n  %s Cannot connect to MetalBox on port %s\n\n  %s\n\n  Press q to quit.\n",
			red.Render("●"), m.port, dimmed.Render(m.err.Error()))
	}

	sidebar := m.renderSidebar()
	main := m.renderMain()
	help := m.renderHelp()

	sideW := 32
	mainW := m.width - sideW - 6
	if mainW < 40 {
		mainW = 40
	}
	contentH := m.height - 4

	sidebarRendered := sidebarStyle.
		Width(sideW).
		Height(contentH).
		Render(sidebar)

	borderColor := lipgloss.Color("62")
	if m.focusPanel == 1 {
		borderColor = lipgloss.Color("39")
	}
	mainRendered := mainStyle.
		Width(mainW).
		Height(contentH).
		BorderForeground(borderColor).
		Render(main)

	if m.focusPanel == 0 {
		sidebarRendered = sidebarStyle.
			Width(sideW).
			Height(contentH).
			BorderForeground(lipgloss.Color("39")).
			Render(sidebar)
	}

	content := lipgloss.JoinHorizontal(lipgloss.Top, sidebarRendered, mainRendered)

	return lipgloss.JoinVertical(lipgloss.Left,
		m.renderHeader(),
		content,
		help,
	)
}

func (m tuiModel) renderHeader() string {
	running := 0
	totalRSS := 0.0
	for _, s := range m.services {
		if s.Status == "running" {
			running++
		}
		if s.RSS != nil {
			totalRSS += *s.RSS
		}
	}

	left := titleStyle.Render("⬡ MetalBox")
	right := subtle.Render(fmt.Sprintf(
		"%d services  %s running  %s total",
		len(m.services),
		green.Render(fmt.Sprintf("%d", running)),
		bold.Render(fmt.Sprintf("%.0f MB", totalRSS)),
	))

	gap := m.width - lipgloss.Width(left) - lipgloss.Width(right) - 2
	if gap < 0 {
		gap = 0
	}

	return left + strings.Repeat(" ", gap) + right
}

func (m tuiModel) renderSidebar() string {
	var b strings.Builder
	b.WriteString(bold.Render("SERVICES") + "\n\n")

	for i, s := range m.services {
		cursor := "  "
		nameStyle := lipgloss.NewStyle()
		if i == m.selected {
			cursor = highlight.Render("▸ ")
			nameStyle = bold
		}

		var dot string
		switch s.Status {
		case "running":
			dot = green.Render("●")
		case "stopped":
			dot = subtle.Render("○")
		default:
			dot = red.Render("●")
		}

		name := nameStyle.Render(s.Name)

		info := ""
		if s.RSS != nil {
			info = dimmed.Render(fmt.Sprintf(" %.0fMB", *s.RSS))
		}
		if s.Status != "running" {
			info = dimmed.Render(" " + s.Status)
		}

		b.WriteString(fmt.Sprintf("%s%s %s%s\n", cursor, dot, name, info))
	}

	return b.String()
}

func (m tuiModel) renderMain() string {
	if len(m.services) == 0 {
		return dimmed.Render("no services")
	}

	svc := m.services[m.selected]

	// Tabs
	tabs := []string{"Logs [1]", "Stats [2]", "Events [3]"}
	var tabLine strings.Builder
	for i, t := range tabs {
		if tuiTab(i) == m.tab {
			tabLine.WriteString(activeTabStyle.Render(t))
		} else {
			tabLine.WriteString(inactiveTabStyle.Render(t))
		}
		tabLine.WriteString("  ")
	}

	header := bold.Render(svc.Name) + "  " +
		m.renderStatusBadge(svc.Status) + "\n" +
		tabLine.String() + "\n" +
		dimmed.Render(strings.Repeat("─", 50)) + "\n"

	var content string
	switch m.tab {
	case tabLogs:
		content = m.renderLogs()
	case tabStats:
		content = m.renderStats(svc)
	case tabEvents:
		content = m.renderEvents(svc)
	}

	return header + content
}

func (m tuiModel) renderStatusBadge(status string) string {
	switch status {
	case "running":
		return green.Render("[running]")
	case "stopped":
		return subtle.Render("[stopped]")
	default:
		return red.Render("[" + status + "]")
	}
}

func (m tuiModel) renderLogs() string {
	if m.logs == "" {
		return dimmed.Render("no logs yet")
	}

	lines := strings.Split(m.logs, "\n")
	maxLines := m.height - 12
	if maxLines < 5 {
		maxLines = 5
	}

	if m.logScroll > len(lines)-maxLines {
		if len(lines) > maxLines {
			// Don't actually mutate m here, just clamp for display
		}
	}

	start := len(lines) - maxLines - m.logScroll
	if start < 0 {
		start = 0
	}
	end := start + maxLines
	if end > len(lines) {
		end = len(lines)
	}

	visible := lines[start:end]
	var b strings.Builder
	for _, line := range visible {
		b.WriteString(dimmed.Render(line) + "\n")
	}

	scrollInfo := dimmed.Render(fmt.Sprintf("─── %d/%d lines ───", end, len(lines)))
	return b.String() + scrollInfo
}

func (m tuiModel) renderStats(svc ServiceStatus) string {
	var b strings.Builder

	// Process info
	pid := "-"
	if svc.PID != nil {
		pid = fmt.Sprintf("%d", *svc.PID)
	}
	b.WriteString(fmt.Sprintf("  PID        %s\n", bold.Render(pid)))
	b.WriteString(fmt.Sprintf("  Uptime     %s\n", bold.Render(svc.Uptime)))
	b.WriteString(fmt.Sprintf("  Restarts   %s\n", bold.Render(fmt.Sprintf("%d", svc.Restarts))))
	b.WriteString(fmt.Sprintf("  CPU Mode   %s\n\n", bold.Render(svc.CPUMode)))

	// Memory
	b.WriteString(bold.Render("  MEMORY\n"))
	rss := 0.0
	if svc.RSS != nil {
		rss = *svc.RSS
	}
	limit := 0.0
	if svc.LimitMB != nil {
		limit = *svc.LimitMB
	}
	b.WriteString(fmt.Sprintf("  RSS        %s", bold.Render(fmt.Sprintf("%.0f MB", rss))))
	if limit > 0 {
		b.WriteString(fmt.Sprintf(" / %.0f MB", limit))
	}
	b.WriteString("\n")

	if limit > 0 {
		pct := rss / limit * 100
		bar := renderBar(pct, 30)
		b.WriteString(fmt.Sprintf("  Usage      %s %.1f%%\n", bar, pct))
	}

	// CPU
	b.WriteString(fmt.Sprintf("\n"))
	b.WriteString(bold.Render("  CPU\n"))
	cpu := 0.0
	if svc.CPU != nil {
		cpu = *svc.CPU
	}
	b.WriteString(fmt.Sprintf("  Usage      %s\n", bold.Render(fmt.Sprintf("%.1f%%", cpu))))

	// GPU
	if svc.GPU != nil && svc.GPU.ActiveMB != nil {
		b.WriteString(fmt.Sprintf("\n"))
		b.WriteString(yellow.Render("  GPU (Metal)\n"))
		b.WriteString(fmt.Sprintf("  Active     %s\n", bold.Render(fmt.Sprintf("%.0f MB", *svc.GPU.ActiveMB))))
		if svc.GPU.PeakMB != nil {
			b.WriteString(fmt.Sprintf("  Peak       %s\n", bold.Render(fmt.Sprintf("%.0f MB", *svc.GPU.PeakMB))))
		}
		if svc.GPU.CacheMB != nil {
			b.WriteString(fmt.Sprintf("  Cache      %s\n", bold.Render(fmt.Sprintf("%.0f MB", *svc.GPU.CacheMB))))
		}
		if svc.GPU.LimitMB != nil {
			b.WriteString(fmt.Sprintf("  Limit      %s\n", bold.Render(fmt.Sprintf("%.0f MB", *svc.GPU.LimitMB))))
		}
	}

	// History sparkline
	if len(svc.History) > 1 {
		b.WriteString(fmt.Sprintf("\n"))
		b.WriteString(bold.Render("  HISTORY (RSS)\n"))
		b.WriteString("  " + renderSparkline(svc.History, 40) + "\n")
	}

	return b.String()
}

func (m tuiModel) renderEvents(svc ServiceStatus) string {
	if len(svc.Events) == 0 {
		return dimmed.Render("no events yet")
	}

	var b strings.Builder
	maxEvents := m.height - 12
	if maxEvents < 5 {
		maxEvents = 5
	}

	start := 0
	if len(svc.Events) > maxEvents {
		start = len(svc.Events) - maxEvents
	}

	for _, e := range svc.Events[start:] {
		t := e.Time.Format("15:04:05")
		var typeStr string
		switch e.Type {
		case "started":
			typeStr = green.Render(padRight(e.Type, 12))
		case "stopped", "exited":
			typeStr = subtle.Render(padRight(e.Type, 12))
		case "oom-kill":
			typeStr = red.Render(padRight(e.Type, 12))
		case "restarting":
			typeStr = yellow.Render(padRight(e.Type, 12))
		default:
			typeStr = highlight.Render(padRight(e.Type, 12))
		}
		b.WriteString(fmt.Sprintf("  %s  %s  %s\n", dimmed.Render(t), typeStr, e.Message))
	}

	return b.String()
}

func (m tuiModel) renderHelp() string {
	help := "  ↑/↓ navigate  tab switch panel  1/2/3 tabs  "
	if len(m.services) > 0 {
		svc := m.services[m.selected]
		if svc.Status == "running" {
			help += "s stop  r restart  "
		} else {
			help += "s start  "
		}
	}
	help += "q quit"

	return statusBar.Width(m.width).Render(help)
}

// --- rendering helpers ---

func renderBar(pct float64, width int) string {
	filled := int(math.Round(pct / 100 * float64(width)))
	if filled > width {
		filled = width
	}
	if filled < 0 {
		filled = 0
	}

	barStyle := green
	if pct > 70 {
		barStyle = yellow
	}
	if pct > 90 {
		barStyle = red
	}

	return barStyle.Render(strings.Repeat("█", filled)) +
		dimmed.Render(strings.Repeat("░", width-filled))
}

func renderSparkline(history []HistorySample, width int) string {
	blocks := []string{"▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"}

	data := make([]float64, len(history))
	maxVal := 0.0
	for i, h := range history {
		data[i] = h.RSSMB
		if h.RSSMB > maxVal {
			maxVal = h.RSSMB
		}
	}
	if maxVal == 0 {
		maxVal = 1
	}

	// Resample if needed
	if len(data) > width {
		step := float64(len(data)) / float64(width)
		resampled := make([]float64, width)
		for i := 0; i < width; i++ {
			idx := int(float64(i) * step)
			if idx >= len(data) {
				idx = len(data) - 1
			}
			resampled[i] = data[idx]
		}
		data = resampled
	}

	var b strings.Builder
	for _, v := range data {
		idx := int(v / maxVal * 7)
		if idx > 7 {
			idx = 7
		}
		if idx < 0 {
			idx = 0
		}
		b.WriteString(highlight.Render(blocks[idx]))
	}

	return b.String()
}

func padRight(s string, width int) string {
	if len(s) >= width {
		return s
	}
	return s + strings.Repeat(" ", width-len(s))
}

// --- entry point ---

func runTUI(port string) error {
	p := tea.NewProgram(
		newTuiModel(port),
		tea.WithAltScreen(),
	)
	_, err := p.Run()
	return err
}
