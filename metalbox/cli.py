"""MetalBox CLI — lightweight process containerization for macOS Apple Silicon."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

import click

from metalbox import __version__

BASE = "http://localhost:{port}"
DEFAULT_PORT = "9090"


def _find_dashboard() -> str | None:
    """Find the metalbox-dashboard binary."""
    pkg = Path(__file__).parent / "bin" / "metalbox-dashboard"
    if pkg.exists():
        return str(pkg)
    from shutil import which
    return which("metalbox-dashboard")


def _api(path: str, method: str = "GET", port: str = DEFAULT_PORT) -> dict | str:
    url = BASE.format(port=port) + path
    if method == "POST":
        req = urllib.request.Request(url, data=b"", method="POST")
    else:
        req = urllib.request.Request(url)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode()
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(body)
        return body
    except urllib.error.URLError:
        click.echo(f"error: cannot reach metalbox server on port {port}", err=True)
        click.echo("start it with: metalbox serve", err=True)
        sys.exit(1)


def _status_icon(status: str) -> str:
    if status == "running":
        return click.style("●", fg="green")
    if status == "stopped":
        return click.style("○", fg="white", dim=True)
    return click.style("●", fg="red")


def _require_binary():
    binary = _find_dashboard()
    if not binary:
        click.echo("error: metalbox-dashboard binary not found", err=True)
        click.echo("install with: pip install metalbox", err=True)
        sys.exit(1)
    return binary


class OrderedGroup(click.Group):
    """Click group that preserves command order."""

    def list_commands(self, ctx):
        return list(self.commands.keys())


HELP_TEXT = """
\b
MetalBox — native process containerization for macOS Apple Silicon.
Run ML workloads with Metal/MLX GPU access and Docker-like resource limits.

\b
Quick start:
  metalbox serve                         Start dashboard + web UI
  metalbox down                          Stop everything and exit
  metalbox top                           Interactive TUI (like lazydocker)
  metalbox run --memory 2g "python x.py" One-shot with limits
"""


@click.group(cls=OrderedGroup, help=HELP_TEXT)
@click.version_option(__version__, prog_name="metalbox")
@click.option("-p", "--port", default=DEFAULT_PORT, envvar="METALBOX_PORT",
              help="Dashboard port [default: 9090]")
@click.pass_context
def main(ctx, port):
    ctx.ensure_object(dict)
    ctx.obj["port"] = port


# ── Server ──

@main.command()
@click.option("-f", "--file", "config", default="metalbox.yml", help="Config file path")
@click.option("-p", "--port", default=DEFAULT_PORT, help="Dashboard port")
@click.option("-d", "--detach", is_flag=True, help="Run in background")
def serve(config, port, detach):
    """Start the dashboard server + web UI."""
    binary = _require_binary()
    config_abs = str(Path(config).resolve())
    env = {**os.environ, "METALBOX_CONFIG": config_abs, "METALBOX_PORT": port}

    if detach:
        proc = subprocess.Popen(
            [binary], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        click.echo(f"metalbox started on http://localhost:{port} (pid {proc.pid})")
        return

    click.echo(f"metalbox dashboard → http://localhost:{port}")
    try:
        subprocess.run([binary], env=env)
    except KeyboardInterrupt:
        pass


@main.command()
@click.pass_context
def down(ctx):
    """Stop all services and shut down the dashboard server."""
    import time as _time

    port = ctx.obj["port"]
    pid_file = Path.home() / ".metalbox" / "run" / "dashboard.pid"

    # Check if server is reachable (without _api's sys.exit behavior)
    url = BASE.format(port=port) + "/api/shutdown"
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        body = resp.read()
        if b'"ok"' not in body:
            raise urllib.error.URLError("not a metalbox server")
        # Wait for the server to actually exit
        for _ in range(10):
            _time.sleep(0.3)
            try:
                urllib.request.urlopen(
                    urllib.request.Request(BASE.format(port=port) + "/api/services"),
                    timeout=2,
                )
            except (urllib.error.URLError, ConnectionError):
                break
        pid_file.unlink(missing_ok=True)
        click.echo("metalbox stopped")
    except (urllib.error.URLError, ConnectionError):
        # Server not reachable — try PID file fallback
        if pid_file.exists():
            import signal as _signal
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, _signal.SIGTERM)
                click.echo(f"sent SIGTERM to dashboard (pid {pid})")
            except ProcessLookupError:
                click.echo("dashboard not running (stale pid file cleaned up)")
            pid_file.unlink(missing_ok=True)
        else:
            click.echo("metalbox is not running")


@main.command()
@click.pass_context
def top(ctx):
    """Interactive TUI dashboard (like lazydocker)."""
    binary = _require_binary()
    port = ctx.obj["port"]
    env = {**os.environ, "METALBOX_TUI": "1", "METALBOX_PORT": port}
    try:
        subprocess.run([binary], env=env)
    except KeyboardInterrupt:
        pass


# ── Services ──

@main.command()
@click.pass_context
def ps(ctx):
    """List services and resource usage."""
    port = ctx.obj["port"]
    services = _api("/api/services", port=port)
    if not services:
        click.echo("no services configured")
        return

    # Header
    click.echo(
        f"  {'SERVICE':<18} {'STATUS':<11} {'PID':<8} "
        f"{'RSS':<10} {'LIMIT':<10} {'CPU':<8} {'HEALTH':<8}"
    )
    click.echo("  " + "─" * 73)

    for s in services:
        icon = _status_icon(s.get("status", "unknown"))
        name = s["name"]
        status = s.get("status", "unknown")
        pid = str(s.get("pid") or "–")
        rss = f"{s['rss_mb']:.0f} MB" if s.get("rss_mb") else "–"
        limit = f"{s['limit_mb']:.0f} MB" if s.get("limit_mb") else "–"
        cpu = f"{s['cpu_percent']:.1f}%" if s.get("cpu_percent") is not None else "–"
        healthy = "–"
        if s.get("healthy") is True:
            healthy = click.style("✓", fg="green")
        elif s.get("healthy") is False:
            healthy = click.style("✗", fg="red")

        click.echo(
            f"  {icon} {name:<17} {status:<10} {pid:<8} "
            f"{rss:<10} {limit:<10} {cpu:<8} {healthy}"
        )

    # Summary
    running = sum(1 for s in services if s.get("status") == "running")
    total_rss = sum(s.get("rss_mb") or 0 for s in services)
    click.echo()
    click.echo(
        f"  {len(services)} services, "
        f"{click.style(str(running), fg='green')} running, "
        f"{total_rss:.0f} MB total"
    )


@main.command()
@click.argument("service")
@click.pass_context
def start(ctx, service):
    """Start a service."""
    port = ctx.obj["port"]
    result = _api(f"/api/services/{service}/start", method="POST", port=port)
    if isinstance(result, dict) and result.get("error"):
        click.echo(f"error: {result['error']}", err=True)
    else:
        click.echo(f"{_status_icon('running')} {service} started")


@main.command()
@click.argument("service")
@click.pass_context
def stop(ctx, service):
    """Stop a service."""
    port = ctx.obj["port"]
    result = _api(f"/api/services/{service}/stop", method="POST", port=port)
    if isinstance(result, dict) and result.get("error"):
        click.echo(f"error: {result['error']}", err=True)
    else:
        click.echo(f"{_status_icon('stopped')} {service} stopped")


@main.command()
@click.argument("service")
@click.pass_context
def restart(ctx, service):
    """Restart a service."""
    port = ctx.obj["port"]
    result = _api(f"/api/services/{service}/restart", method="POST", port=port)
    if isinstance(result, dict) and result.get("error"):
        click.echo(f"error: {result['error']}", err=True)
    else:
        click.echo(f"{_status_icon('running')} {service} restarted")


@main.command()
@click.argument("service")
@click.option("-n", "--lines", default=100, help="Number of lines")
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.pass_context
def logs(ctx, service, lines, follow):
    """View service logs."""
    import time as _time

    port = ctx.obj["port"]
    text = _api(f"/api/logs/{service}?lines={lines}", port=port)
    click.echo(text, nl=False)

    if follow:
        seen = len(text)
        try:
            while True:
                _time.sleep(1)
                text = _api(f"/api/logs/{service}?lines=500", port=port)
                if len(text) > seen:
                    click.echo(text[seen:], nl=False)
                    seen = len(text)
        except KeyboardInterrupt:
            pass


# ── One-shot ──

@main.command()
@click.argument("command", nargs=-1, required=True)
@click.option("-m", "--memory", default=None, help="RSS memory limit (e.g. 2g, 512m)")
@click.option("--metal-memory", default=None, help="Metal GPU memory limit")
@click.option("--metal-cache", default=None, help="Metal cache limit")
@click.option("--cpus", default="default", type=click.Choice(["default", "background"]),
              help="CPU policy")
@click.option("--name", default=None, help="Service name")
def run(command, memory, metal_memory, metal_cache, cpus, name):
    """Run a command with resource limits (no config file needed).

    \b
    Examples:
      metalbox run --memory 2g "python train.py"
      metalbox run -m 512m --metal-memory 1g "python -m uvicorn app:app"
      metalbox run --cpus background "python bench.py"
    """
    import tempfile

    cmd_str = " ".join(command)
    svc_name = name or "run-" + cmd_str.split()[0].split("/")[-1].replace(".", "-")[:20]

    lines = [
        "services:", f"  {svc_name}:", f"    command: {cmd_str}",
        f"    workdir: {os.getcwd()}", '    restart: "no"', "    resources:",
    ]
    if memory:
        lines.append(f"      memory: {memory}")
    if metal_memory:
        lines.append(f"      metal_memory: {metal_memory}")
    if metal_cache:
        lines.append(f"      metal_cache: {metal_cache}")
    if cpus != "default":
        lines.append(f"      cpus: {cpus}")

    binary = _require_binary()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False, prefix="metalbox-run-") as f:
        f.write("\n".join(lines) + "\n")
        tmp_config = f.name

    limits = []
    if memory:
        limits.append(f"mem={memory}")
    if metal_memory:
        limits.append(f"metal={metal_memory}")
    if cpus != "default":
        limits.append(f"cpus={cpus}")
    click.echo(f"metalbox run: {cmd_str}" + (f" [{', '.join(limits)}]" if limits else ""))

    env = {**os.environ, "METALBOX_CONFIG": tmp_config, "METALBOX_PORT": "0"}

    try:
        import time as _time
        proc = subprocess.Popen([binary], env=env)
        _time.sleep(1)
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
    finally:
        os.unlink(tmp_config)


if __name__ == "__main__":
    main()
