"""Parse and validate metalbox.yml config."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _parse_bytes(val: str | int) -> int:
    if isinstance(val, int):
        return val
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([kmgtKMGT])[bB]?$", str(val).strip())
    if not m:
        raise ValueError(f"invalid size: {val}")
    num, unit = float(m.group(1)), m.group(2).lower()
    mult = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    return int(num * mult[unit])


def _sub_env(val: str) -> str:
    def _replace(m):
        key = m.group(1) or m.group(2)
        return os.environ.get(key, "")
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", _replace, val)


@dataclass
class Resources:
    memory: int | None = None
    metal_memory: int | None = None
    metal_cache: int | None = None
    cpus: str = "default"


@dataclass
class Healthcheck:
    url: str | None = None
    tcp: str | None = None
    cmd: str | None = None
    interval: int = 30
    timeout: int = 10
    retries: int = 3
    start_period: int = 60


@dataclass
class Service:
    name: str
    command: str
    workdir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_file: str | None = None
    resources: Resources = field(default_factory=Resources)
    restart: str = "no"
    healthcheck: Healthcheck | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Config:
    services: dict[str, Service]
    project_dir: Path


def _load_env_file(path: str, project_dir: Path) -> dict[str, str]:
    p = Path(path) if Path(path).is_absolute() else project_dir / path
    env = {}
    if not p.exists():
        return env
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def load(path: str | Path = "metalbox.yml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    project_dir = p.parent.resolve()
    raw = yaml.safe_load(p.read_text())
    if not raw or "services" not in raw:
        raise ValueError("metalbox.yml must have a 'services' key")

    services = {}
    for name, svc in raw["services"].items():
        if "command" not in svc:
            raise ValueError(f"service '{name}' missing 'command'")

        res_raw = svc.get("resources", {})
        resources = Resources(
            memory=_parse_bytes(res_raw["memory"]) if "memory" in res_raw else None,
            metal_memory=_parse_bytes(res_raw["metal_memory"]) if "metal_memory" in res_raw else None,
            metal_cache=_parse_bytes(res_raw["metal_cache"]) if "metal_cache" in res_raw else None,
            cpus=res_raw.get("cpus", "default"),
        )

        hc_raw = svc.get("healthcheck")
        healthcheck = None
        if hc_raw:
            healthcheck = Healthcheck(
                url=hc_raw.get("url"),
                tcp=hc_raw.get("tcp"),
                cmd=hc_raw.get("cmd"),
                interval=hc_raw.get("interval", 30),
                timeout=hc_raw.get("timeout", 10),
                retries=hc_raw.get("retries", 3),
                start_period=hc_raw.get("start_period", 60),
            )

        env = {}
        if svc.get("env_file"):
            env.update(_load_env_file(svc["env_file"], project_dir))
        for k, v in svc.get("env", {}).items():
            env[k] = _sub_env(str(v))

        services[name] = Service(
            name=name,
            command=_sub_env(svc["command"]),
            workdir=svc.get("workdir", str(project_dir)),
            env=env,
            env_file=svc.get("env_file"),
            resources=resources,
            restart=svc.get("restart", "no"),
            healthcheck=healthcheck,
            depends_on=svc.get("depends_on", []),
        )

    return Config(services=services, project_dir=project_dir)
