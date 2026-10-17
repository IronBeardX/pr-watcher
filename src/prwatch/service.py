"""Gestiona el timer de systemd --user que ejecuta `prwatch tick` cada N segundos."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from prwatch import paths
from prwatch.config import Config

UNIT = "pr-watcher"


def unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def service_path() -> Path:
    return unit_dir() / f"{UNIT}.service"


def timer_path() -> Path:
    return unit_dir() / f"{UNIT}.timer"


def _esc(v: str) -> str:
    return v.replace("%", "%%").replace('"', '\\"')


def render_units(cfg: Config) -> tuple[str, str]:
    env_lines = [f'Environment="PATH={_esc(os.environ.get("PATH", "/usr/bin:/bin"))}"']
    if os.environ.get("PRWATCH_HOME"):
        env_lines.append(f'Environment="PRWATCH_HOME={_esc(os.environ["PRWATCH_HOME"])}"')
    timeout = cfg.claude_timeout_seconds * (cfg.max_reviews_per_tick + 1) + 120
    service = f"""\
[Unit]
Description=pr-watcher: revisar si hay PRs nuevos o actualizados

[Service]
Type=oneshot
ExecStart="{_esc(sys.executable)}" -m prwatch tick
WorkingDirectory={_esc(str(paths.claude_cwd()))}
UMask=0077
{chr(10).join(env_lines)}
TimeoutStartSec={timeout}
Nice=10
"""
    timer = f"""\
[Unit]
Description=pr-watcher: cada {cfg.interval_seconds}s

[Timer]
OnActiveSec=10s
OnBootSec=1min
OnUnitInactiveSec={cfg.interval_seconds}s
AccuracySec=15s
Unit={UNIT}.service

[Install]
WantedBy=timers.target
"""
    return service, timer


def systemctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def installed() -> bool:
    return service_path().exists() and timer_path().exists()


def write_units(cfg: Config) -> None:
    paths.ensure_dirs()
    unit_dir().mkdir(parents=True, exist_ok=True)
    service, timer = render_units(cfg)
    service_path().write_text(service)
    timer_path().write_text(timer)
    systemctl("daemon-reload")


def _check(cp: subprocess.CompletedProcess) -> None:
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout).strip() or "systemctl falló")


def enable(cfg: Config) -> None:
    write_units(cfg)
    _check(systemctl("enable", "--now", f"{UNIT}.timer"))


def disable() -> None:
    if installed():
        _check(systemctl("disable", "--now", f"{UNIT}.timer"))


RESUME_UNIT = f"{UNIT}-resume"


def pause_timer() -> None:
    """Para el timer de verdad (y que siga parado tras reiniciar): no se encola ningún tick automático."""
    if installed():
        _check(systemctl("disable", "--now", f"{UNIT}.timer"))


def resume_timer(cfg: Config) -> None:
    cancel_scheduled_resume()
    enable(cfg)


def schedule_resume(minutes: int) -> None:
    """Reanudación automática: con el timer parado no hay tick que pueda hacerlo, así que se le pide a systemd.

    Es un timer transitorio: no sobrevive a un reinicio. Si se pierde, la TUI y `prwatch status`
    reanudan al ver que la hora de fin ya pasó.
    """
    cancel_scheduled_resume()
    argv = ["systemd-run", "--user", f"--unit={RESUME_UNIT}", f"--on-active={int(minutes)}m",
            "--collect", "--quiet"]
    if os.environ.get("PRWATCH_HOME"):
        argv.append(f"--setenv=PRWATCH_HOME={os.environ['PRWATCH_HOME']}")
    argv += [sys.executable, "-m", "prwatch", "resume"]
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"no pude programar la reanudación: {e}") from e
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout).strip() or "systemd-run falló")


def cancel_scheduled_resume() -> None:
    systemctl("stop", f"{RESUME_UNIT}.timer")


def apply_config(cfg: Config) -> None:
    """Reescribe las units si existen (p. ej. cambió el intervalo) y reinicia el timer si estaba activo."""
    if not installed():
        return
    write_units(cfg)
    if systemctl("is-active", f"{UNIT}.timer").stdout.strip() == "active":
        systemctl("restart", f"{UNIT}.timer")


def remove_timer() -> None:
    if installed():
        systemctl("disable", "--now", f"{UNIT}.timer")
        service_path().unlink(missing_ok=True)
        timer_path().unlink(missing_ok=True)
        systemctl("daemon-reload")


def run_now() -> str:
    """Tick manual completo ya mismo. Funciona también en pausa (la pausa solo para los automáticos)."""
    paths.ensure_dirs()
    subprocess.Popen(
        [sys.executable, "-m", "prwatch", "tick", "--manual"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, cwd=str(paths.claude_cwd()),
    )
    return "tick manual lanzado"


def _next_run() -> datetime | None:
    cp = systemctl("list-timers", f"{UNIT}.timer", "--all", "--output=json")
    try:
        rows = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for row in rows:
        nxt = row.get("next")
        if row.get("unit") == f"{UNIT}.timer" and nxt:
            return datetime.fromtimestamp(nxt / 1_000_000, tz=timezone.utc)
    return None


def _show(unit: str, *props: str) -> dict[str, str]:
    out = systemctl("show", unit, *(f"-p{p}" for p in props)).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def status() -> dict[str, object]:
    """Estado del timer y del último resultado del servicio (detecta fallos que no llegan a events.log)."""
    if not installed():
        return {"installed": False, "enabled": False, "active": False, "running": False, "next": None,
                "result": "", "exit_status": ""}
    timer, svc = f"{UNIT}.timer", f"{UNIT}.service"
    active = systemctl("is-active", timer).stdout.strip() == "active"
    info = _show(svc, "Result", "ExecMainStatus", "ActiveState")
    return {
        "installed": True,
        "enabled": systemctl("is-enabled", timer).stdout.strip() == "enabled",
        "active": active,
        "running": info.get("ActiveState") == "activating",
        "next": _next_run() if active else None,
        "result": info.get("Result", ""),
        "exit_status": info.get("ExecMainStatus", ""),
    }


def service_failed(st: dict[str, object]) -> bool:
    return bool(st.get("installed")) and st.get("result") not in ("", "success")
