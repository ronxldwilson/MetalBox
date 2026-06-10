"""CPU policy wrapper using macOS taskpolicy."""
from __future__ import annotations

import shutil
import subprocess


def wrap_command(command: str, cpus: str) -> list[str]:
    """Wrap a command with taskpolicy if needed.

    cpus="background" → taskpolicy -b (E-cores only)
    cpus="default" → no wrapping
    """
    if cpus == "background" and shutil.which("taskpolicy"):
        return ["taskpolicy", "-b", "bash", "-c", command]
    return ["bash", "-c", command]


def set_background(pid: int) -> bool:
    """Set a running process to background QoS (E-cores only)."""
    if not shutil.which("taskpolicy"):
        return False
    try:
        subprocess.run(
            ["taskpolicy", "-b", "-p", str(pid)],
            check=True, capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
