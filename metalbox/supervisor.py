"""Process supervisor — start, stop, restart services with resource guards."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from metalbox.config import Config, Service
from metalbox.guard import ResourceGuard
from metalbox.healthcheck import HealthMonitor
from metalbox.logger import open_log
from metalbox.taskpolicy import wrap_command

log = logging.getLogger("metalbox")

RUN_DIR = Path.home() / ".metalbox" / "run"


@dataclass
class ServiceState:
    service: Service
    process: subprocess.Popen | None = None
    guard: ResourceGuard | None = None
    health: HealthMonitor | None = None
    log_file: object = None
    restarts: int = 0
    stopped_by_user: bool = False
    started_at: float = 0.0
    _restart_lock: threading.Lock = field(default_factory=threading.Lock)


class Supervisor:
    def __init__(self, config: Config):
        self._config = config
        self._states: dict[str, ServiceState] = {}
        self._shutdown = threading.Event()

    def up(self, services: list[str] | None = None):
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        targets = self._resolve_order(services)
        for name in targets:
            if name in self._states and self._states[name].process and self._states[name].process.poll() is None:
                log.info("[%s] already running (pid %d)", name, self._states[name].process.pid)
                continue
            self._start_service(name)

    def down(self, services: list[str] | None = None):
        targets = list(reversed(self._resolve_order(services)))
        for name in targets:
            self._stop_service(name)
        if not services:
            self._shutdown.set()

    def restart(self, name: str):
        self._stop_service(name)
        self._start_service(name)

    def ps(self) -> list[dict]:
        rows = []
        for name, st in self._states.items():
            pid = st.process.pid if st.process and st.process.poll() is None else None
            rss = None
            if st.guard:
                rss = st.guard.current_rss
            rows.append({
                "name": name,
                "pid": pid,
                "status": self._status(st),
                "rss_mb": round(rss / (1024 * 1024), 1) if rss else None,
                "limit_mb": round(st.service.resources.memory / (1024 * 1024), 1) if st.service.resources.memory else None,
                "restarts": st.restarts,
                "healthy": st.health.healthy if st.health else None,
                "uptime": time.time() - st.started_at if pid and st.started_at else 0,
            })
        return rows

    def wait(self):
        signal.signal(signal.SIGTERM, lambda *_: self.down())
        signal.signal(signal.SIGINT, lambda *_: self.down())
        while not self._shutdown.is_set():
            self._shutdown.wait(1)
            for name, st in list(self._states.items()):
                if st.stopped_by_user:
                    continue
                if st.process and st.process.poll() is not None:
                    exit_code = st.process.returncode
                    log.info("[%s] exited with code %d", name, exit_code)
                    should_restart = (
                        st.service.restart == "always"
                        or (st.service.restart == "unless-stopped" and not st.stopped_by_user)
                        or (st.service.restart == "on-failure" and exit_code != 0)
                    )
                    if should_restart:
                        st.restarts += 1
                        backoff = min(2 ** st.restarts, 30)
                        log.info("[%s] restarting in %ds (attempt %d)", name, backoff, st.restarts)
                        time.sleep(backoff)
                        self._start_service(name)

    def _start_service(self, name: str):
        svc = self._config.services[name]
        cmd = wrap_command(svc.command, svc.resources.cpus)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", **svc.env}

        if svc.resources.metal_memory:
            env["METALBOX_METAL_MEMORY"] = str(svc.resources.metal_memory)
        if svc.resources.metal_cache:
            env["METALBOX_METAL_CACHE"] = str(svc.resources.metal_cache)

        log_f = open_log(name)
        log.info("[%s] starting: %s", name, svc.command)

        proc = subprocess.Popen(
            cmd,
            cwd=svc.workdir,
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )

        pid_file = RUN_DIR / f"{name}.pid"
        pid_file.write_text(str(proc.pid))

        state = self._states.get(name)
        if state:
            state.process = proc
            state.log_file = log_f
            state.stopped_by_user = False
            state.started_at = time.time()
        else:
            state = ServiceState(
                service=svc, process=proc, log_file=log_f, started_at=time.time(),
            )
            self._states[name] = state

        guard = ResourceGuard(
            pid=proc.pid,
            memory_limit=svc.resources.memory,
            service_name=name,
        )
        guard.start()
        state.guard = guard

        if svc.healthcheck:
            hm = HealthMonitor(
                hc=svc.healthcheck,
                service_name=name,
                on_unhealthy=lambda n=name: self._on_unhealthy(n),
            )
            hm.start()
            state.health = hm

        log.info("[%s] started (pid %d)", name, proc.pid)

    def _stop_service(self, name: str):
        st = self._states.get(name)
        if not st or not st.process:
            return

        st.stopped_by_user = True

        if st.health:
            st.health.stop()
        if st.guard:
            st.guard.stop()

        if st.process.poll() is None:
            log.info("[%s] stopping (pid %d)", name, st.process.pid)
            try:
                os.killpg(os.getpgid(st.process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                st.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("[%s] force killing", name)
                try:
                    os.killpg(os.getpgid(st.process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        if st.log_file:
            try:
                st.log_file.close()
            except Exception:
                pass

        pid_file = RUN_DIR / f"{name}.pid"
        pid_file.unlink(missing_ok=True)
        log.info("[%s] stopped", name)

    def _on_unhealthy(self, name: str):
        st = self._states.get(name)
        if not st or st.stopped_by_user:
            return
        log.warning("[%s] unhealthy — restarting", name)
        with st._restart_lock:
            self._stop_service(name)
            st.stopped_by_user = False
            st.restarts += 1
            self._start_service(name)

    def _status(self, st: ServiceState) -> str:
        if st.stopped_by_user:
            return "stopped"
        if not st.process:
            return "created"
        rc = st.process.poll()
        if rc is None:
            if st.health and not st.health.healthy and not st.health._in_start_period:
                return "unhealthy"
            return "running"
        return f"exited ({rc})"

    def _resolve_order(self, names: list[str] | None = None) -> list[str]:
        targets = names or list(self._config.services.keys())
        ordered = []
        visited = set()

        def visit(n):
            if n in visited:
                return
            visited.add(n)
            svc = self._config.services.get(n)
            if not svc:
                raise ValueError(f"unknown service: {n}")
            for dep in svc.depends_on:
                visit(dep)
            ordered.append(n)

        for t in targets:
            visit(t)
        return ordered
