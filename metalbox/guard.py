"""Resource watchdog — monitors RSS and Metal memory, kills on exceed."""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

import psutil

log = logging.getLogger("metalbox.guard")

POLL_INTERVAL = 5  # seconds
GRACE_PERIOD = 5   # seconds between SIGTERM and SIGKILL


def _get_metal_memory() -> dict | None:
    try:
        import mlx.core as mx
        return {
            "active": mx.metal.get_active_memory(),
            "peak": mx.metal.get_peak_memory(),
            "cache": mx.metal.get_cache_memory(),
        }
    except (ImportError, AttributeError):
        return None


class ResourceGuard:
    def __init__(
        self,
        pid: int,
        memory_limit: int | None,
        metal_memory_limit: int | None = None,
        service_name: str = "",
        on_kill=None,
    ):
        self._pid = pid
        self._memory_limit = memory_limit
        self._metal_memory_limit = metal_memory_limit
        self._service_name = service_name
        self._on_kill = on_kill
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._metal_stats: dict | None = None

    def start(self):
        if not self._memory_limit and not self._metal_memory_limit:
            return
        self._thread = threading.Thread(
            target=self._watch, daemon=True, name=f"guard-{self._service_name}",
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def update_pid(self, pid: int):
        self._pid = pid

    @property
    def current_rss(self) -> int | None:
        try:
            return psutil.Process(self._pid).memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    @property
    def metal_memory(self) -> dict | None:
        return self._metal_stats

    def _watch(self):
        while not self._stop.is_set():
            self._stop.wait(POLL_INTERVAL)
            if self._stop.is_set():
                break

            self._metal_stats = _get_metal_memory()

            rss = self.current_rss
            if rss is None:
                continue

            if self._memory_limit and rss > self._memory_limit:
                rss_mb = rss / (1024 * 1024)
                limit_mb = self._memory_limit / (1024 * 1024)
                log.warning(
                    "[%s] RSS %.0fMB exceeds limit %.0fMB — killing",
                    self._service_name, rss_mb, limit_mb,
                )
                self._kill()
                if self._on_kill:
                    self._on_kill()
                continue

            if (self._metal_memory_limit and self._metal_stats
                    and self._metal_stats["active"] > self._metal_memory_limit):
                active_mb = self._metal_stats["active"] / (1024 * 1024)
                limit_mb = self._metal_memory_limit / (1024 * 1024)
                log.warning(
                    "[%s] Metal memory %.0fMB exceeds limit %.0fMB — killing",
                    self._service_name, active_mb, limit_mb,
                )
                self._kill()
                if self._on_kill:
                    self._on_kill()

    def _kill(self):
        try:
            os.kill(self._pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        time.sleep(GRACE_PERIOD)
        try:
            os.kill(self._pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
