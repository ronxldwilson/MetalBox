"""MetalBox CLI — thin client + server launcher."""
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
    # 1. Bundled in package
    pkg = Path(__file__).parent / "bin" / "metalbox-dashboard"
    if pkg.exists():
        return str(pkg)
    # 2. On PATH
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


@click.group()
@click.version_option(__version__)
@click.option("-p", "--port", default=DEFAULT_PORT, envvar="METALBOX_PORT", help="Dashboard server port")
@click.pass_context
def main(ctx, port):
    """MetalBox — lightweight process containerization for macOS Apple Silicon."""
    ctx.ensure_object(dict)
    ctx.obj["port"] = port


@main.command()
@click.option("-f", "--file", "config", default="metalbox.yml", help="Config file")
@click.option("-p", "--port", default=DEFAULT_PORT, help="Dashboard port")
@click.option("-d", "--detach", is_flag=True, help="Run in background")
def serve(config, port, detach):
    """Start the metalbox dashboard server."""
    binary = _find_dashboard()
    if not binary:
        click.echo("error: metalbox-dashboard binary not found", err=True)
        click.echo("install metalbox with: pip install metalbox", err=True)
        sys.exit(1)

    config_abs = str(Path(config).resolve())
    env = {**os.environ, "METALBOX_CONFIG": config_abs, "METALBOX_PORT": port}

    if detach:
        proc = subprocess.Popen(
            [binary], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        click.echo(f"metalbox dashboard started on http://localhost:{port} (pid {proc.pid})")
        return

    click.echo(f"metalbox dashboard on http://localhost:{port}")
    try:
        subprocess.run([binary], env=env)
    except KeyboardInterrupt:
        pass


@main.command()
@click.pass_context
def ps(ctx):
    """Show running services."""
    port = ctx.obj["port"]
    services = _api("/api/services", port=port)
    if not services:
        click.echo("no services configured")
        return

    click.echo(f"{'SERVICE':<20} {'PID':<8} {'STATUS':<12} {'RSS':<10} {'LIMIT':<10} {'HEALTH':<8}")
    click.echo("-" * 68)
    for s in services:
        pid = str(s.get("pid") or "-")
        status = s.get("status", "unknown")
        rss = f"{s['rss_mb']:.0f}MB" if s.get("rss_mb") else "-"
        limit = f"{s['limit_mb']:.0f}MB" if s.get("limit_mb") else "-"
        healthy = "-"
        if s.get("healthy") is True:
            healthy = "ok"
        elif s.get("healthy") is False:
            healthy = "FAIL"
        click.echo(f"{s['name']:<20} {pid:<8} {status:<12} {rss:<10} {limit:<10} {healthy:<8}")


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
        click.echo(f"{service} started")


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
        click.echo(f"{service} stopped")


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
        click.echo(f"{service} restarted")


@main.command()
@click.argument("service")
@click.option("-n", "--lines", default=100, help="Number of lines")
@click.pass_context
def logs(ctx, service, lines):
    """Show service logs."""
    port = ctx.obj["port"]
    text = _api(f"/api/logs/{service}?lines={lines}", port=port)
    click.echo(text)


@main.command()
@click.argument("command", nargs=-1, required=True)
@click.option("--memory", "-m", default=None, help="RSS memory limit (e.g. 2g, 512m)")
@click.option("--metal-memory", default=None, help="Metal GPU memory limit")
@click.option("--metal-cache", default=None, help="Metal cache limit")
@click.option("--cpus", default="default", help="CPU policy: default or background")
@click.option("--name", default=None, help="Service name (default: derived from command)")
def run(command, memory, metal_memory, metal_cache, cpus, name):
    """Run a one-shot command with resource limits (no server needed).

    \b
    Examples:
      metalbox run --memory 2g "python train.py"
      metalbox run --memory 512m --metal-memory 1g "python -m uvicorn app:app"
      metalbox run --cpus background "python bench.py"
    """
    import tempfile

    cmd_str = " ".join(command)
    svc_name = name or "run-" + cmd_str.split()[0].split("/")[-1].replace(".", "-")[:20]

    # Build YAML manually to avoid pyyaml dependency
    lines = ["services:", f"  {svc_name}:", f"    command: {cmd_str}", f"    workdir: {os.getcwd()}", '    restart: "no"', "    resources:"]
    if memory:
        lines.append(f"      memory: {memory}")
    if metal_memory:
        lines.append(f"      metal_memory: {metal_memory}")
    if metal_cache:
        lines.append(f"      metal_cache: {metal_cache}")
    if cpus != "default":
        lines.append(f"      cpus: {cpus}")

    binary = _find_dashboard()
    if not binary:
        click.echo("error: metalbox-dashboard binary not found", err=True)
        sys.exit(1)

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
        proc = subprocess.Popen([binary], env=env)
        # Give it time to start the service
        import time
        time.sleep(1)
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
    finally:
        os.unlink(tmp_config)


if __name__ == "__main__":
    main()
