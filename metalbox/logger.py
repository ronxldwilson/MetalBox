"""Per-service log capture and rotation."""
from __future__ import annotations

import os
import threading
from pathlib import Path

LOG_DIR = Path.home() / ".metalbox" / "logs"
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_LOG_FILES = 3


def _ensure_dir(service: str) -> Path:
    d = LOG_DIR / service
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rotate(path: Path, service: str):
    d = _ensure_dir(service)
    for i in range(MAX_LOG_FILES - 1, 0, -1):
        old = d / f"{service}.log.{i}"
        new = d / f"{service}.log.{i + 1}"
        if old.exists():
            if i + 1 > MAX_LOG_FILES:
                old.unlink()
            else:
                old.rename(new)
    if path.exists():
        path.rename(d / f"{service}.log.1")


def log_path(service: str) -> Path:
    d = _ensure_dir(service)
    return d / f"{service}.log"


def open_log(service: str):
    p = log_path(service)
    if p.exists() and p.stat().st_size > MAX_LOG_SIZE:
        _rotate(p, service)
    return open(p, "a")


def tail(service: str, lines: int = 50) -> str:
    p = log_path(service)
    if not p.exists():
        return f"no logs for {service}"
    all_lines = p.read_text().splitlines()
    return "\n".join(all_lines[-lines:])


def follow(service: str, callback):
    """Follow log file, calling callback(line) for each new line. Blocking."""
    p = log_path(service)
    if not p.exists():
        return
    with open(p) as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                callback(line.rstrip())
            else:
                threading.Event().wait(0.5)
