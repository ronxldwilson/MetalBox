"""MetalBox CLI — thin client for the metalbox dashboard server."""
from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error

import click

from metalbox import __version__

BASE = "http://localhost:{port}"
DEFAULT_PORT = "9090"


def _api(path: str, method: str = "GET", port: str = DEFAULT_PORT) -> dict | str:
    url = BASE.format(port=port) + path
    req = urllib.request.Request(url, method=method.encode() if method != "GET" else None)
    if method == "POST":
        req = urllib.request.Request(url, data=b"", method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode()
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(body)
        return body
    except urllib.error.URLError as e:
        click.echo(f"error: cannot reach metalbox server on port {port}", err=True)
        click.echo("is `metalbox-dashboard` running?", err=True)
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


if __name__ == "__main__":
    main()
