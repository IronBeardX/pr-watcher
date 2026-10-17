"""Dónde vive cada cosa (XDG). PRWATCH_HOME lo redirige todo a una carpeta, útil para pruebas."""
from __future__ import annotations

import os
from pathlib import Path

APP = "pr-watcher"


def _xdg(var: str, default: str) -> Path:
    override = os.environ.get("PRWATCH_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get(var) or Path.home() / default) / APP


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def config_file() -> Path:
    return config_dir() / "config.json"


def prompt_file() -> Path:
    return config_dir() / "prompt.md"


def state_file() -> Path:
    return state_dir() / "state.json"


def lock_file() -> Path:
    return state_dir() / "state.lock"


def tick_lock_file() -> Path:
    return state_dir() / "tick.lock"


def events_file() -> Path:
    return state_dir() / "events.log"


def events_lock_file() -> Path:
    return state_dir() / "events.lock"


def runs_dir() -> Path:
    return state_dir() / "runs"


def claude_cwd() -> Path:
    """Directorio vacío y privado donde corre Claude: así no hereda ningún proyecto ni sus ficheros."""
    return state_dir() / "claude-cwd"


def traces_dir() -> Path:
    """Rastro por ejecución de Claude que escriben los hooks: qué herramientas pidió y qué se envió."""
    return state_dir() / "traces"


def guard_settings_file() -> Path:
    return state_dir() / "claude-settings.json"


def ensure_dirs() -> None:
    for d in (config_dir(), state_dir(), runs_dir(), claude_cwd(), traces_dir()):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def write_private(path: Path, text: str) -> None:
    """Escritura atómica con permisos 0600."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
