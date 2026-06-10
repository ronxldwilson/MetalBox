"""MetalBox CLI — Docker-like process management for macOS."""
from __future__ import annotations

import logging
import sys
import time

import click

from metalbox import __version__
from metalbox.config import load
from metalbox.logger import follow, tail
from metalbox.supervisor import Supervisor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)


def _load(config: str) -> tuple:
    cfg = load(config)
    return cfg, Supervisor(cfg)


@click.group()
@click.version_option(__version__)
def main():
    """MetalBox — lightweight process containerization for macOS Apple Silicon."""
    pass


@main.command()
@click.argument("services", nargs=-1)
@click.option("-f", "--file", "config", default="metalbox.yml", help="Config file")
@click.option("-d", "--detach", is_flag=True, help="Run in background")
def up(services, config, detach):
    """Start services."""
    cfg, sup = _load(config)
    targets = list(services) if services else None
    sup.up(targets)
    if detach:
        click.echo("services started in background")
        pids = {s["name"]: s["pid"] for s in sup.ps()}
        for name, pid in pids.items():
            click.echo(f"  {name}: pid {pid}")
        return
    try:
        sup.wait()
    except KeyboardInterrupt:
        sup.down()


@main.command()
@click.argument("services", nargs=-1)
@click.option("-f", "--file", "config", default="metalbox.yml", help="Config file")
def down(services, config):
    """Stop services."""
    cfg = load(config)
    from metalbox.supervisor import RUN_DIR
    import os, signal, psutil

    targets = list(services) if services else list(cfg.services.keys())
    for name in reversed(targets):
        pid_file = RUN_DIR / f"{name}.pid"
        if not pid_file.exists():
            click.echo(f"  {name}: not running")
            continue
        pid = int(pid_file.read_text().strip())
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            click.echo(f"  {name}: stopped (pid {pid})")
        except (ProcessLookupError, PermissionError):
            click.echo(f"  {name}: already dead")
        pid_file.unlink(missing_ok=True)


@main.command()
@click.option("-f", "--file", "config", default="metalbox.yml", help="Config file")
def ps(config):
    """Show running services."""
    cfg = load(config)
    from metalbox.supervisor import RUN_DIR

    click.echo(f"{'SERVICE':<20} {'PID':<8} {'STATUS':<12} {'RSS':<10} {'LIMIT':<10}")
    click.echo("-" * 60)

    for name in cfg.services:
        pid_file = RUN_DIR / f"{name}.pid"
        pid = None
        status = "stopped"
        rss_str = "-"
        limit = cfg.services[name].resources.memory

        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            try:
                import psutil
                p = psutil.Process(pid)
                status = "running"
                rss = p.memory_info().rss
                rss_str = f"{rss / (1024*1024):.0f}MB"
            except Exception:
                status = "dead"
                pid_file.unlink(missing_ok=True)

        limit_str = f"{limit / (1024*1024):.0f}MB" if limit else "-"
        pid_str = str(pid) if pid else "-"
        click.echo(f"{name:<20} {pid_str:<8} {status:<12} {rss_str:<10} {limit_str:<10}")


@main.command()
@click.argument("service")
@click.option("-f", "--follow", "do_follow", is_flag=True, help="Follow log output")
@click.option("-n", "--lines", default=50, help="Number of lines to show")
@click.option("--file", "config", default="metalbox.yml", help="Config file")
def logs(service, do_follow, lines, config):
    """Show service logs."""
    if do_follow:
        click.echo(f"following {service} logs (ctrl+c to stop)...")
        try:
            follow(service, lambda line: click.echo(line))
        except KeyboardInterrupt:
            pass
    else:
        click.echo(tail(service, lines))


@main.command()
@click.argument("service")
@click.option("-f", "--file", "config", default="metalbox.yml", help="Config file")
def restart(service, config):
    """Restart a service."""
    cfg, sup = _load(config)
    sup.restart(service)
    click.echo(f"{service} restarted")


@main.command()
@click.argument("service")
@click.option("-f", "--file", "config", default="metalbox.yml", help="Config file")
def run(service, config):
    """Run a single service in foreground (for debugging)."""
    import subprocess, os
    from metalbox.taskpolicy import wrap_command

    cfg = load(config)
    svc = cfg.services.get(service)
    if not svc:
        click.echo(f"unknown service: {service}", err=True)
        sys.exit(1)

    cmd = wrap_command(svc.command, svc.resources.cpus)
    env = {**os.environ, **svc.env}
    click.echo(f"running {service}: {svc.command}")
    try:
        proc = subprocess.run(cmd, cwd=svc.workdir, env=env)
        sys.exit(proc.returncode)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
