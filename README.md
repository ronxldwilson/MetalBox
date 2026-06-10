# MetalBox

Lightweight process containerization for macOS Apple Silicon. Run ML workloads with Metal/MLX acceleration and Docker-like resource limits — without a Linux VM.

## The problem

Every container runtime on macOS (Docker, Podman, OrbStack, Lima) runs a Linux VM. Linux doesn't have Metal. So you can't use MLX, MPS, or any Metal-accelerated framework inside a container. You're stuck choosing between:

- **Docker** — real resource limits, but no GPU, 3x slower for ML inference
- **Native** — full Metal speed, but no resource limits, no lifecycle management, dangerous on a shared machine

MetalBox gives you both: **native Metal performance with container-like resource management.**

## How it works

MetalBox doesn't use VMs or kernel namespaces (macOS doesn't have them). It's a process supervisor that runs your workload as a native macOS process with enforced resource limits and Docker-like lifecycle management.

```
┌──────────────────────────────────────┐
│  metalbox                            │
│                                      │
│  ┌────────────┐  ┌────────────────┐  │
│  │ your app   │  │ resource guard │  │
│  │ (native    │◄─│ • RSS watchdog │  │
│  │  macOS     │  │ • Metal mem cap│  │
│  │  process)  │  │ • CPU policy   │  │
│  └────────────┘  └────────────────┘  │
│        │                             │
│   Metal / MLX / MPS                  │
│   (direct GPU access)               │
└──────────────────────────────────────┘
```

## Usage

```bash
metalbox up                  # start all services
metalbox down                # stop all, free resources
metalbox ps                  # show services + resource usage
metalbox logs myapp          # tail logs
metalbox logs myapp -f       # follow logs
metalbox restart myapp       # restart a service
metalbox run myapp           # run in foreground (for debugging)
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
| **Memory (RSS)** | psutil watchdog thread — kills process if RSS exceeds limit, supervisor restarts it | Yes (kill + restart) |
| **Metal memory** | `mx.metal.set_memory_limit()` — Metal refuses allocations beyond cap | Yes (Metal API) |
| **Metal cache** | `mx.metal.set_cache_limit()` — bounds compilation/KV cache | Yes (Metal API) |
| **CPU** | `taskpolicy -b` for background (E-cores only), or default (all cores) | Structural (not %) |

macOS has no cgroups. RSS limits (`ulimit -m`) are silently ignored by the kernel. MetalBox enforces memory limits by monitoring from a watchdog thread and killing the process if it exceeds the cap — then the supervisor restarts it. This is the only reliable approach on macOS.

## Installation

```bash
# from source
git clone https://github.com/ronxldwilson/metalbox.git
cd metalbox
pip install -e .

# or
brew install metalbox   # (planned)
```

Requires: macOS 14+ on Apple Silicon. Python 3.10+.

## Architecture

```
metalbox/
├── cli.py              # CLI entry point (click)
├── config.py           # YAML config parser + validation
├── supervisor.py       # process lifecycle (start, stop, restart, depends_on)
├── guard.py            # resource watchdog (RSS monitor, kill on exceed)
├── metal.py            # Metal memory limit injection (optional, for MLX workloads)
├── logger.py           # per-service log capture + rotation
├── healthcheck.py      # HTTP/TCP/CMD health checks
└── taskpolicy.py       # CPU policy wrapper (taskpolicy -b)
```

### Supervisor

Each service runs as a child process via `subprocess.Popen`. The supervisor:

- Tracks PID, start time, restart count
- Forwards SIGTERM/SIGINT to children on shutdown
- Implements restart policies (always, unless-stopped, on-failure, no)
- Respects `depends_on` ordering for startup
- Writes PID files to `~/.metalbox/run/<service>.pid`

### Resource guard

A daemon thread per service that polls `psutil.Process.memory_info().rss` every 5 seconds. If RSS exceeds the configured limit:

1. Calls `mx.metal.clear_cache()` (if Metal limits configured)
2. Rechecks RSS
3. If still over, sends SIGTERM → waits 5s → SIGKILL
4. Supervisor restarts per policy

### Metal memory injection

For MLX/Metal workloads, MetalBox can inject memory limits before the app loads models. Two approaches:

1. **Environment variable** — the app reads `METALBOX_METAL_MEMORY` and calls `mx.metal.set_memory_limit()` at startup
2. **Python wrapper** — MetalBox runs the app via a thin wrapper that sets Metal limits before importing the app module

### Health checks

Supports three modes:
- `url:` — HTTP GET, expects 2xx
- `tcp:` — port open check
- `cmd:` — run a command, check exit code 0

A service is "healthy" after passing `retries` consecutive checks. On failure, the service is restarted.

## Roadmap

### Phase 1 — Process manager with resource limits (MVP)
- [x] YAML config parsing (`metalbox.yml`)
- [ ] `metalbox up` / `down` / `ps` / `logs` / `restart`
- [ ] Process supervisor with restart policies
- [ ] RSS memory watchdog (psutil-based, kill + restart)
- [ ] CPU policy via `taskpolicy`
- [ ] `depends_on` ordering
- [ ] Health checks (HTTP)
- [ ] PID file management
- [ ] Log capture to `~/.metalbox/logs/<service>/`
- [ ] Env file support + variable substitution
- [ ] Graceful shutdown (SIGTERM cascade)

### Phase 2 — Metal integration
- [ ] Metal memory limit injection (`mx.metal.set_memory_limit`)
- [ ] Metal cache limit injection (`mx.metal.set_cache_limit`)
- [ ] Metal memory monitoring (`mx.metal.get_active_memory`)
- [ ] `metalbox stats` — live resource dashboard (RSS, Metal memory, CPU%)

### Phase 3 — Isolation
- [ ] Environment isolation (clean env, only pass declared vars)
- [ ] Working directory sandboxing
- [ ] Network port binding validation (fail-fast on conflicts)
- [ ] Filesystem: read-only mounts

### Phase 4 — Distribution (future)
- [ ] `Metalfile` (Dockerfile-like) for building app bundles
- [ ] OCI image import (extract layers, run natively)
- [ ] Registry push/pull
- [ ] Docker-compatible API socket (so Docker Desktop could list services)
- [ ] Homebrew formula

## Comparison

| | Docker | MetalBox | Native script |
|---|---|---|---|
| Metal / MLX / MPS | No | **Yes** | Yes |
| Memory limits | Hard (cgroups) | Hard (watchdog + kill) | None |
| CPU limits | Hard (cgroups) | Structural (E-cores) | None |
| Filesystem isolation | Full | Planned | None |
| Network isolation | Full | Planned | None |
| Lifecycle management | Full | Yes | Manual |
| Image distribution | Full | Planned | None |
| Works on Linux | Yes | No (macOS only) | No |

## License

MIT
