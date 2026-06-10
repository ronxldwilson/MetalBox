"""Metal memory limit injection for MLX workloads.

Two modes:
1. Wrapper mode — metalbox runs your app via a thin Python wrapper that
   sets Metal limits before importing the app module.
2. Env mode — metalbox sets METALBOX_METAL_MEMORY / METALBOX_METAL_CACHE
   env vars; the app reads them at startup (no wrapper needed).

The wrapper is a temp .py file that calls mx.metal.set_memory_limit()
and mx.metal.set_cache_limit() before exec'ing the real command.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

WRAPPER_DIR = Path.home() / ".metalbox" / "wrappers"

WRAPPER_TEMPLATE = '''\
import os, sys

_metal_memory = {metal_memory}
_metal_cache = {metal_cache}

try:
    import mlx.core as mx
    if _metal_memory:
        mx.metal.set_memory_limit(_metal_memory)
    if _metal_cache:
        mx.metal.set_cache_limit(_metal_cache)
except ImportError:
    pass

# exec the real module
sys.argv = {argv}
from runpy import run_module
run_module({module!r}, run_name="__main__")
'''

WRAPPER_SCRIPT_TEMPLATE = '''\
import os, sys

_metal_memory = {metal_memory}
_metal_cache = {metal_cache}

try:
    import mlx.core as mx
    if _metal_memory:
        mx.metal.set_memory_limit(_metal_memory)
    if _metal_cache:
        mx.metal.set_cache_limit(_metal_cache)
except ImportError:
    pass

# exec the real script
sys.argv = {argv}
script = {script!r}
with open(script) as f:
    code = compile(f.read(), script, "exec")
exec(code, {{"__name__": "__main__", "__file__": script}})
'''


def needs_wrapper(metal_memory: int | None, metal_cache: int | None) -> bool:
    return bool(metal_memory or metal_cache)


def wrap_command(command: str, metal_memory: int | None, metal_cache: int | None, service_name: str) -> str | None:
    """Rewrite a python command to inject Metal limits via a wrapper script.

    Returns the rewritten command, or None if no wrapping needed.
    Handles:
      - python -m module args...
      - python script.py args...
      - uv run python -m module args...
      - uv run python script.py args...
    """
    if not needs_wrapper(metal_memory, metal_cache):
        return None

    WRAPPER_DIR.mkdir(parents=True, exist_ok=True)

    parts = command.split()
    uv_prefix = ""
    py_idx = _find_python(parts)
    if py_idx is None:
        return None

    if py_idx > 0:
        uv_prefix = " ".join(parts[:py_idx]) + " "

    python = parts[py_idx]
    rest = parts[py_idx + 1:]

    if rest and rest[0] == "-m" and len(rest) >= 2:
        module = rest[1]
        argv = [module] + rest[2:]
        wrapper = WRAPPER_TEMPLATE.format(
            metal_memory=metal_memory or 0,
            metal_cache=metal_cache or 0,
            module=module,
            argv=repr(argv),
        )
    elif rest and not rest[0].startswith("-"):
        script = rest[0]
        argv = rest
        wrapper = WRAPPER_SCRIPT_TEMPLATE.format(
            metal_memory=metal_memory or 0,
            metal_cache=metal_cache or 0,
            script=script,
            argv=repr(argv),
        )
    else:
        return None

    wrapper_path = WRAPPER_DIR / f"{service_name}_metal_wrapper.py"
    wrapper_path.write_text(wrapper)

    return f"{uv_prefix}{python} {wrapper_path}"


def _find_python(parts: list[str]) -> int | None:
    for i, p in enumerate(parts):
        base = os.path.basename(p)
        if base.startswith("python"):
            return i
    return None


def query_metal_memory() -> dict | None:
    """Query current Metal memory usage. Returns None if mlx not available."""
    try:
        import mlx.core as mx
        return {
            "active": mx.metal.get_active_memory(),
            "peak": mx.metal.get_peak_memory(),
            "cache": mx.metal.get_cache_memory(),
        }
    except (ImportError, AttributeError):
        return None
