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


@main.command()
@click.option("-f", "--file", "config", default="metalbox.yml", help="Config file")
@click.option("-w", "--watch", "do_watch", is_flag=True, help="Live refresh every 2s")
def stats(config, do_watch):
    """Live resource dashboard — RSS, Metal memory, CPU% per service."""
    import psutil
    from metalbox.supervisor import RUN_DIR
    from metalbox.metal import query_metal_memory

    cfg = load(config)

    def _render():
        click.echo(
            f"{'SERVICE':<16} {'PID':<8} {'RSS':<10} {'RSS LIM':<10} "
            f"{'METAL':<10} {'METAL LIM':<10} {'CPU%':<8}"
        )
        click.echo("-" * 82)
        for name, svc in cfg.services.items():
            pid_file = RUN_DIR / f"{name}.pid"
            pid = rss = cpu = None
            if pid_file.exists():
                pid = int(pid_file.read_text().strip())
                try:
                    p = psutil.Process(pid)
                    rss = p.memory_info().rss
                    cpu = p.cpu_percent(interval=0.1)
                except Exception:
                    pid = None

            pid_s = str(pid) if pid else "-"
            rss_s = f"{rss/(1024**2):.0f}MB" if rss else "-"
            rss_lim = f"{svc.resources.memory/(1024**2):.0f}MB" if svc.resources.memory else "-"
            metal_lim = f"{svc.resources.metal_memory/(1024**2):.0f}MB" if svc.resources.metal_memory else "-"
            cpu_s = f"{cpu:.1f}%" if cpu is not None else "-"

            metal_s = "-"
            metal = query_metal_memory()
            if metal:
                metal_s = f"{metal['active']/(1024**2):.0f}MB"

            click.echo(
                f"{name:<16} {pid_s:<8} {rss_s:<10} {rss_lim:<10} "
                f"{metal_s:<10} {metal_lim:<10} {cpu_s:<8}"
            )

    if do_watch:
        try:
            while True:
                click.clear()
                click.echo("metalbox stats (ctrl+c to stop)\n")
                _render()
                time.sleep(2)
        except KeyboardInterrupt:
            pass
    else:
        _render()


@main.command()
@click.option("-f", "--file", "config", default="metalbox.yml", help="Config file")
@click.option("-p", "--port", default="9090", help="Dashboard port")
def serve(config, port):
    """Start the web dashboard."""
    import shutil, subprocess, os
    from pathlib import Path

    dashboard_bin = shutil.which("metalbox-dashboard")
    if not dashboard_bin:
        pkg_dir = Path(__file__).parent.parent / "dashboard" / "metalbox-dashboard"
        if pkg_dir.exists():
            dashboard_bin = str(pkg_dir)
    if not dashboard_bin:
        click.echo("metalbox-dashboard binary not found — build it with:", err=True)
        click.echo("  cd dashboard && go build -o metalbox-dashboard .", err=True)
        sys.exit(1)

    env = {**os.environ, "METALBOX_CONFIG": str(Path(config).resolve()), "METALBOX_PORT": port}
    click.echo(f"starting dashboard on http://localhost:{port}")
    try:
        subprocess.run([dashboard_bin], env=env)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
