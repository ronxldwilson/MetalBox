# MetalBox

Lightweight process containerization for macOS Apple Silicon. Run ML workloads with Metal/MLX acceleration and Docker-like resource limits — without a Linux VM.

## The problem

Every container runtime on macOS (Docker, Podman, OrbStack, Lima) runs a Linux VM. Linux doesn't have Metal. So you can't use MLX, MPS, or any Metal-accelerated framework inside a container. You're stuck choosing between:

- **Docker** — real resource limits, but no GPU, 3x slower for ML inference
- **Native** — full Metal speed, but no resource limits, no lifecycle management, dangerous on a shared machine

MetalBox gives you both: **native Metal performance with container-like resource management.**

## How it works

MetalBox is a Go server that runs your workloads as native macOS processes with enforced resource limits, health checks, and a web dashboard for monitoring.

```
┌──────────────────────────────────────┐
│  metalbox-dashboard (Go binary)      │
│                                      │
│  ┌────────────┐  ┌────────────────┐  │
│  │ your app   │  │ resource guard │  │
│  │ (native    │◄─│ • RSS watchdog │  │
│  │  macOS     │  │ • Metal mem cap│  │
│  │  process)  │  │ • CPU policy   │  │
│  └────────────┘  └────────────────┘  │
│        │              │              │
│   Metal / MLX / MPS   │ health checks│
│   (direct GPU access) │ log capture  │
│                       │ auto-restart │
│  ┌─────────────────────────────────┐ │
│  │  web dashboard (localhost:9090) │ │
│  │  start/stop/restart • logs      │ │
│  │  RSS/CPU graphs • events        │ │
│  └─────────────────────────────────┘ │
└──────────────────────────────────────┘
```

## Quick start

```bash
# Build the dashboard server
cd dashboard
go build -o metalbox-dashboard .

# Create a metalbox.yml (see Config below)
# Start the dashboard
./metalbox-dashboard

# Open http://localhost:9090
```

## Web dashboard

The dashboard runs on `localhost:9090` and provides:

- **Service overview** — status, PID, RSS, CPU%, memory usage bars
- **Start / Stop / Restart** buttons per service
- **Log viewer** with auto-refresh
- **Event timeline** — starts, stops, OOM kills, health check failures, restarts
- **Auto-refresh** every 3 seconds

## CLI

A thin Python CLI is also available, talking to the dashboard API:

```bash
pip install -e .

metalbox ps                  # show services + resource usage
metalbox start myapp         # start a service
metalbox stop myapp          # stop a service
metalbox restart myapp       # restart a service
metalbox logs myapp          # view logs
```

## Config

`metalbox.yml` in your project directory:

```yaml
services:
  inference:
    command: python -m uvicorn app:app --host 0.0.0.0 --port 8080
    workdir: /path/to/your/project
    env:
      MODEL_CACHE: /tmp/models
    resources:
      memory: 2.5g          # hard RSS limit — process killed + restarted if exceeded
      metal_memory: 2g      # Metal heap cap (mx.metal.set_memory_limit)
      metal_cache: 512m     # Metal cache cap (mx.metal.set_cache_limit)
      cpus: background      # "background" = E-cores only, "default" = all cores
    restart: unless-stopped  # always | unless-stopped | on-failure | no
    healthcheck:
      url: http://127.0.0.1:8080/healthz
      interval: 30
      timeout: 10
      retries: 3
      start_period: 120

  proxy:
    command: caddy reverse-proxy --from :443 --to :8080
    resources:
      memory: 128m
    restart: always
    depends_on:
      - inference
```

## How resource limits work

| Resource | Mechanism | Hard? |
|----------|-----------|-------|
| **Memory (RSS)** | Go watchdog goroutine polls `ps` every 5s — kills process group if RSS exceeds limit, auto-restarts per policy | Yes (kill + restart) |
| **Metal memory** | Wrapper injection: auto-generates a Python script calling `mx.metal.set_memory_limit()` before your app imports anything | Yes (Metal API) |
| **Metal cache** | Same wrapper calls `mx.metal.set_cache_limit()` | Yes (Metal API) |
| **CPU** | `taskpolicy -b` for background QoS (E-cores only), or default (all cores) | Structural (not %) |
| **Health checks** | HTTP GET / TCP connect / shell command — restart on consecutive failures | Configurable retries |

macOS has no cgroups. RSS limits (`ulimit -m`) are silently ignored by the kernel. MetalBox enforces memory limits by monitoring RSS and killing the process if it exceeds the cap — then auto-restarting per the configured restart policy. This is the only reliable approach on macOS.

### Metal memory injection

For Python/MLX workloads, MetalBox auto-generates a wrapper script that calls `mx.metal.set_memory_limit()` and `mx.metal.set_cache_limit()` before your app loads any models. The wrapper is transparent — it detects `python -m module` and `python script.py` patterns (including `uv run python` prefix) and rewrites the command to go through the wrapper first.

## Architecture

```
metalbox/
├── dashboard/
│   ├── main.go             # Go server — process supervisor, RSS guard,
│   │                       #   health checks, Metal wrapper, REST API
│   └── static/
│       └── index.html      # embedded web dashboard (single file)
├── metalbox/
│   ├── __init__.py
│   └── cli.py              # thin Python CLI (talks to Go server API)
├── pyproject.toml           # Python CLI package config
└── metalbox.yml             # your service config (gitignored)
```

The Go binary is the runtime — it handles everything:
- Process lifecycle (start, stop, restart, PID files)
- RSS memory watchdog (kill + restart on exceed)
- Metal/MLX memory limit injection (auto-generated Python wrapper)
- CPU policy via `taskpolicy`
- Health checks (HTTP, TCP, command)
- Log capture to `~/.metalbox/logs/`
- Web dashboard + REST API
- Event tracking (OOM kills, health failures, restarts)

The Python CLI is optional — a thin client that calls the REST API for terminal use.

## Comparison

| | Docker | MetalBox | Native script |
|---|---|---|---|
| Metal / MLX / MPS | No | **Yes** | Yes |
| Memory limits | Hard (cgroups) | Hard (watchdog + kill) | None |
| CPU limits | Hard (cgroups) | Structural (E-cores) | None |
| Health checks | Yes | Yes (HTTP/TCP/CMD) | None |
| Web dashboard | Docker Desktop | Yes (localhost:9090) | None |
| Lifecycle management | Full | Yes | Manual |
| Filesystem isolation | Full | No (macOS limitation) | None |
| Network isolation | Full | No (macOS limitation) | None |
| Works on Linux | Yes | No (macOS only) | No |

## Requirements

- macOS 14+ on Apple Silicon
- Go 1.21+ (to build the dashboard)
- Python 3.10+ (optional, for CLI only)

## License

MIT
