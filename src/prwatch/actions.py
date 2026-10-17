"""Acciones del usuario, compartidas por la CLI y la TUI."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from prwatch import claude, config, service, state


def enable() -> str:
    cfg = config.load()
    if not cfg.setup_done:
        raise ValueError("Primero completa el asistente: ejecuta `prwatch`")
    service.resume_timer(cfg)
    with state.locked() as s:
        s["paused"], s["paused_until"] = False, None
    state.log(f"Watcher vigilando (cada {cfg.interval_seconds}s)")
    return "vigilando"


def pause(minutes: int | None = None) -> str:
    """Para el timer: no hay ticks automáticos. `t` (tick manual) y `r`/`R` siguen funcionando."""
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds") \
        if minutes else None
    service.pause_timer()
    with state.locked() as s:
        s["paused"], s["paused_until"] = True, until
    note = ""
    if minutes:
        try:
            service.schedule_resume(minutes)
        except RuntimeError as e:
            note = f" (no pude programar la reanudación automática: {e}; reanuda a mano con p)"
    msg = (f"en pausa {minutes} min" if minutes else "en pausa hasta que lo reanudes") + note
    state.log(f"Watcher {msg}")
    return msg


def resume() -> str:
    cfg = config.load()
    if not cfg.setup_done:
        raise ValueError("Primero completa el asistente: ejecuta `prwatch`")
    service.resume_timer(cfg)
    with state.locked() as s:
        s["paused"], s["paused_until"] = False, None
    state.log("Watcher reanudado")
    return "reanudado"


def resume_if_expired() -> bool:
    """La reanudación programada se pierde si el PC se reinicia: si la hora de fin ya pasó, reanuda."""
    s = state.load()
    until = state.parse_iso(s.get("paused_until"))
    if s.get("paused") and until and until <= datetime.now(timezone.utc):
        resume()
        return True
    return False


def review(pr_id: int, run_now: bool = True) -> str:
    with state.locked() as s:
        if pr_id not in s["forced"]:
            s["forced"].append(pr_id)
        rec = s["prs"].get(str(pr_id))
        if rec:
            rec["fail_count"] = 0
    state.log(f"Revisión manual pedida para PR #{pr_id}", pr=pr_id)
    how = service.run_now() if run_now else "se hará en el próximo tick"
    return f"PR #{pr_id} en cola ({how})"


def ignore(pr_id: int) -> str:
    with state.locked() as s:
        rec = s["prs"].setdefault(str(pr_id), {"id": pr_id})
        rec["ignored"] = True
        rec["status"] = "ignorado"
        s["forced"] = [x for x in s["forced"] if int(x) != pr_id]
    state.log(f"PR #{pr_id} ignorado", pr=pr_id)
    return f"PR #{pr_id} ignorado"


def unignore(pr_id: int) -> str:
    with state.locked() as s:
        rec = s["prs"].get(str(pr_id))
        if rec:
            rec["ignored"] = False
            rec["status"] = ""
    state.log(f"PR #{pr_id} ya no se ignora", pr=pr_id)
    return f"PR #{pr_id} ya no se ignora"


def toggle_ignore(pr_id: int) -> str:
    rec = state.load()["prs"].get(str(pr_id)) or {}
    return unignore(pr_id) if rec.get("ignored") else ignore(pr_id)


def set_since(value: str | None) -> str:
    cfg = config.load()
    if value:
        date.fromisoformat(value)
    cfg.since = value or ""
    config.save(cfg)
    msg = f"solo PRs creados desde {value}" if value else "sin filtro de fecha"
    state.log(f"Filtro de fecha: {msg}")
    return msg


def test_claude() -> str:
    """Manda un DM de prueba a través de Claude: valida comando, cuenta y conector de Slack."""
    cfg = config.load()
    if not cfg.slack_user_id:
        raise ValueError("Configura tu usuario de Slack (Ajustes → slack_user_id, U…): "
                         "el guardián solo deja enviar DMs a ese id")
    fake = {"id": 0, "title": "mensaje de prueba", "author": "pr-watcher",
            "url": "", "head": "", "source_branch": "-", "destination": cfg.destination_branch}
    state.log("Probando Claude: DM de prueba en Slack")
    r = claude.run(cfg, fake, "test")
    level = "info" if r.ok else "error"
    with state.locked() as s:
        # Las pruebas también gastan: cuentan en el total de envíos del día.
        s["test_runs"] = (s.get("test_runs") or [])[-49:] + [{
            "at": state.now_iso(), "ok": r.ok, "summary": r.summary, "cost_usd": r.cost_usd,
            "duration_s": round(r.duration_s, 1), "run_file": r.run_file, "link": r.link}]
        if r.ok:
            state.clear_error(s, "claude_error")
    state.log(f"Prueba de Claude: {'OK' if r.ok else 'FALLÓ'} en {r.duration_s:.0f}s — {r.summary}", level)
    return f"{'OK' if r.ok else 'FALLÓ'}: {r.summary} (detalle: {r.run_file})"


def _hhmm(ts: str | None) -> str:
    dt = state.parse_iso(ts)
    return dt.astimezone().strftime("%d/%m %H:%M") if dt else "?"


def health(s: dict, svc: dict, cfg) -> tuple[str, str]:
    """('ok'|'warn'|'error', texto) para la barra de estado y `prwatch status`."""
    from prwatch import service as _svc

    if _svc.service_failed(svc):
        crash = (s.get("crash_error") or {}).get("text")
        detail = crash or f"Result={svc.get('result')}, código {svc.get('exit_status')}"
        return "error", f"el servicio falló ({detail}); mira: journalctl --user -u pr-watcher -n 50"
    lt = s.get("last_tick") or {}
    at = state.parse_iso(lt.get("at"))
    if svc.get("active") and not svc.get("running"):
        limit = timedelta(seconds=2 * cfg.interval_seconds + 120)
        now = datetime.now(timezone.utc)
        nxt = svc.get("next")
        # Tras suspender el portátil el timer monotónico aún no ha vencido: eso no es un fallo.
        overdue = nxt is None or nxt < now - timedelta(seconds=60)
        if at is None:
            return "warn", "timer activo; esperando el primer tick"
        if now - at > limit and overdue:
            return "error", "el timer está activo pero no hay ticks recientes (¿sesión cerrada o servicio roto?)"
    err = (s.get("crash_error") or s.get("error") or s.get("config_error") or s.get("claude_error"))
    if err:
        return "error", f"desde {_hhmm(err.get('since'))} ({err.get('count')} ticks): {err.get('text')}"
    me = s.get("me") or {}
    if cfg.skip_my_prs and not me.get("uuid") and not cfg.my_bitbucket_nickname.strip():
        return "warn", ("no sé cuál es tu usuario de Bitbucket, así que tus PRs se tratan como de otros: "
                        "pon tu nombre en Ajustes (c) → Mi nickname de Bitbucket")
    if lt and not lt.get("ok", True):
        return "warn", lt.get("message", "último tick con error")
    return "ok", "sin errores"
