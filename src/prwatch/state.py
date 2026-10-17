"""Estado compartido entre el timer y la TUI: un JSON protegido con flock, más el log de eventos."""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from prwatch import paths

EMPTY: dict[str, Any] = {
    "version": 1,
    "paused": False,
    "paused_until": None,
    "initialized": False,
    "me": None,
    "me_retry_at": None,
    "last_tick": None,
    "error": None,          # {"text", "since", "count", "last_logged"} — Bitbucket/red
    "config_error": None,   # misma forma — configuración incompleta
    "claude_error": None,   # todos los envíos de un tick fallaron
    "crash_error": None,    # el tick lanzó una excepción
    "backoff_until": None,  # tras un 429 de Bitbucket
    "forced": [],
    "test_runs": [],
    "prs": {},
}

ROTATE_BYTES = 2_000_000
ERROR_RELOG_SECONDS = 3600


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class StateError(RuntimeError):
    pass


def _read() -> dict[str, Any]:
    p = paths.state_file()
    if not p.exists():
        return json.loads(json.dumps(EMPTY))
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise StateError(f"state.json corrupto ({e}); bórralo o arréglalo: {p}") from e
    for k, v in EMPTY.items():
        data.setdefault(k, json.loads(json.dumps(v)))
    return data


def load() -> dict[str, Any]:
    """Lectura sin bloqueo (la escritura es atómica con replace)."""
    paths.ensure_dirs()
    return _read()


@contextmanager
def locked() -> Iterator[dict[str, Any]]:
    """Lee-modifica-escribe con lock exclusivo. Mantenerlo corto: nunca durante una llamada a Claude."""
    paths.ensure_dirs()
    fd = os.open(paths.lock_file(), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        data = _read()
        yield data
        paths.write_private(paths.state_file(), json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def is_paused(data: dict[str, Any]) -> bool:
    if not data.get("paused"):
        return False
    until = parse_iso(data.get("paused_until"))
    return until is None or until > now()


# ─── log de eventos ───

def log(msg: str, level: str = "info", pr: int | str | None = None, **extra: Any) -> None:
    paths.ensure_dirs()
    line: dict[str, Any] = {"at": now_iso(), "level": level, "msg": msg}
    if pr is not None:
        line["pr"] = int(pr)
    line.update({k: v for k, v in extra.items() if v is not None})
    fd = os.open(paths.events_lock_file(), os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        p = paths.events_file()
        efd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(efd, "a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        if p.stat().st_size > ROTATE_BYTES:
            data = p.read_bytes()[-(ROTATE_BYTES // 2):]
            data = data[data.find(b"\n") + 1:]
            paths.write_private(p, data.decode(errors="replace"))


def read_events_since(offset: int) -> tuple[list[dict[str, Any]], int, bool]:
    """Eventos nuevos desde un offset de bytes. Devuelve (eventos, nuevo_offset, rotado)."""
    p = paths.events_file()
    if not p.exists():
        return [], 0, offset > 0
    size = p.stat().st_size
    rotated = size < offset
    start = 0 if rotated else offset
    with open(p, "rb") as f:
        f.seek(start)
        chunk = f.read()
    end = chunk.rfind(b"\n") + 1  # nunca leer una línea a medio escribir
    out = []
    for ln in chunk[:end].splitlines():
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out, start + end, rotated


def tail_events(n: int = 200) -> list[dict[str, Any]]:
    events, _, _ = read_events_since(0)
    return events[-n:]


# ─── errores repetidos: se registran al cambiar y como mucho una vez por hora ───

def report_error(s: dict[str, Any], text: str, key: str = "error", kind: str | None = None,
                 **extra: Any) -> None:
    """Registra un error persistente. Se de-duplica por `kind` (o por el texto si no hay kind)."""
    err = s.get(key) or {}
    t = now()
    ident = kind or text
    if err.get("kind", err.get("text")) != ident:
        s[key] = {"text": text, "kind": ident, "since": now_iso(), "count": 1, "last_logged": now_iso()}
        log(text, "error", **extra)
        return
    err["text"] = text
    err["count"] = err.get("count", 0) + 1
    last = parse_iso(err.get("last_logged"))
    if not last or t - last > timedelta(seconds=ERROR_RELOG_SECONDS):
        err["last_logged"] = now_iso()
        log(f"Sigue fallando desde {err['since']} ({err['count']} ticks): {text}", "error")
    s[key] = err


def clear_error(s: dict[str, Any], key: str = "error") -> None:
    err = s.get(key)
    if err:
        log(f"Resuelto tras {err.get('count', 1)} ticks: {err.get('text', '')[:120]}")
        s[key] = None
