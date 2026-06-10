"""Health check runners — HTTP, TCP, and command-based."""
from __future__ import annotations

import logging
import socket
import subprocess
import threading
import urllib.request
from urllib.error import URLError

from metalbox.config import Healthcheck

log = logging.getLogger("metalbox.healthcheck")


def check_once(hc: Healthcheck) -> bool:
    if hc.url:
        return _check_http(hc.url, hc.timeout)
    if hc.tcp:
        return _check_tcp(hc.tcp, hc.timeout)
    if hc.cmd:
        return _check_cmd(hc.cmd, hc.timeout)
    return True


def _check_http(url: str, timeout: int) -> bool:
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return 200 <= resp.status < 400
    except (URLError, OSError, TimeoutError):
        return False


def _check_tcp(addr: str, timeout: int) -> bool:
    host, _, port = addr.rpartition(":")
    if not host:
        host = "127.0.0.1"
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except (OSError, TimeoutError):
        return False


def _check_cmd(cmd: str, timeout: int) -> bool:
    try:
        r = subprocess.run(cmd, shell=True, timeout=timeout, capture_output=True)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


class HealthMonitor:
    """Periodically runs health checks in a background thread."""

    def __init__(self, hc: Healthcheck, service_name: str, on_unhealthy=None):
        self._hc = hc
        self._service_name = service_name
        self._on_unhealthy = on_unhealthy
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_failures = 0
        self._healthy = False
        self._in_start_period = True

    @property
    def healthy(self) -> bool:
        return self._healthy

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"health-{self._service_name}",
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        self._stop.wait(self._hc.start_period)
        self._in_start_period = False
        while not self._stop.is_set():
            ok = check_once(self._hc)
            if ok:
                if not self._healthy:
                    log.info("[%s] healthy", self._service_name)
                self._healthy = True
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                log.warning(
                    "[%s] health check failed (%d/%d)",
                    self._service_name, self._consecutive_failures, self._hc.retries,
                )
                if self._consecutive_failures >= self._hc.retries:
                    self._healthy = False
                    if self._on_unhealthy:
                        self._on_unhealthy()
                    self._consecutive_failures = 0
            self._stop.wait(self._hc.interval)
