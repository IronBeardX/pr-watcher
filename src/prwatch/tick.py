"""Un tick: mirar Bitbucket, decidir qué toca revisar y, solo entonces, llamar a Claude."""
from __future__ import annotations

import fcntl
import os
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from prwatch import claude, paths, state
from prwatch.bitbucket import BitbucketError, Client
from prwatch.config import Config

MAX_FAILS_PER_HEAD = 2
ME_RETRY_MINUTES = 60
MAX_REVIEWS_KEPT = 50
FORGET_CLOSED_DAYS = 30
MAX_STRANDED_FETCH = 10      # PRs pendientes fuera de la ventana que se re-consultan por tick
MAX_VERIFY_FETCH = 5         # PRs ya vistos fuera de la ventana cuyo estado se re-comprueba por tick
VERIFY_EVERY_MINUTES = 60
MAX_REVIEWED_MARKERS = 500
HEARTBEAT_HOURS = 1

# Estados que ve la TUI
ST_BASELINE = "baseline"      # ya estaba abierto en el primer arranque; se revisa si cambia
ST_DRAFT = "draft"
ST_SETTLING = "esperando"     # actualizado hace poco: se espera por si llegan más pushes
ST_PENDING = "pendiente"
ST_REVIEWING = "revisando"
ST_REVIEWED = "revisado"
ST_FAILED = "falló"
ST_IGNORED = "ignorado"
ST_MINE = "mío"
ST_AUTHOR = "autor ignorado"
ST_OLD = "anterior a fecha"
ST_CLOSED = "cerrado"
QUIET_TRANSITIONS = {ST_PENDING, ST_REVIEWING, ST_SETTLING}


def _is_mine(pr: dict[str, Any], me: dict[str, Any] | None, cfg: Config) -> bool:
    if me and me.get("uuid") and pr.get("author_uuid") == me["uuid"]:
        return True
    nick = cfg.my_bitbucket_nickname.strip().lower()
    return bool(nick) and nick in (pr.get("author_nickname", "").lower(), pr.get("author", "").lower())


def _author_ignored(pr: dict[str, Any], cfg: Config) -> bool:
    names = {a.strip().lower() for a in cfg.ignored_authors if a.strip()}
    return bool(names & {pr.get("author", "").lower(), pr.get("author_nickname", "").lower()})


def _before_since(pr: dict[str, Any], cfg: Config) -> bool:
    since = cfg.since_date
    created = state.parse_iso(pr.get("created_on"))
    return bool(since and created and created.astimezone().date() < since)


def decide(rec: dict[str, Any], pr: dict[str, Any], cfg: Config, me: dict[str, Any] | None,
           first_run: bool, baseline_cutoff: Any = None, is_new_record: bool = False) -> str | None:
    """Actualiza rec['status'] y devuelve el motivo de revisión ('new'|'updated'|'ready') o None."""
    head = pr["head"]
    if rec.get("ignored"):
        rec["status"] = ST_IGNORED
        return None
    if cfg.skip_my_prs and _is_mine(pr, me, cfg):
        rec["status"] = ST_MINE
        return None
    if _author_ignored(pr, cfg):
        rec["status"] = ST_AUTHOR
        return None
    if _before_since(pr, cfg):
        rec["status"] = ST_OLD
        return None
    if pr["draft"] and cfg.skip_drafts:
        rec["status"] = ST_DRAFT
        rec["was_draft"] = True
        return None
    if rec.get("reviewed_head") == head:
        rec["status"] = ST_REVIEWED
        return None
    if rec.get("fail_head") == head and rec.get("fail_count", 0) >= MAX_FAILS_PER_HEAD:
        rec["status"] = ST_FAILED
        return None
    if rec.get("was_draft"):
        return "ready"
    upd = state.parse_iso(pr.get("updated_on"))
    if (baseline_cutoff and is_new_record and not rec.get("reviewed_head") and not rec.get("baseline_head")
            and upd and upd < baseline_cutoff):
        # Aparece solo porque se amplió la ventana: punto de partida, no ráfaga de envíos.
        rec["baseline_head"] = head
        rec["status"] = ST_BASELINE
        return None
    if first_run and not cfg.review_existing_on_first_run and not rec.get("reviewed_head"):
        rec["baseline_head"] = head
        rec["status"] = ST_BASELINE
        return None
    if rec.get("baseline_head") == head:
        rec["status"] = ST_BASELINE
        return None
    if rec.get("reviewed_head") or rec.get("baseline_head"):
        return "updated"
    return "new"


def _merge_pr(rec: dict[str, Any], pr: dict[str, Any]) -> None:
    for k in ("id", "title", "author", "author_nickname", "draft", "head", "source_branch",
              "destination", "created_on", "updated_on", "url"):
        rec[k] = pr[k]
    rec["last_seen"] = state.now_iso()


def _settled(pr: dict[str, Any], cfg: Config) -> bool:
    upd = state.parse_iso(pr.get("updated_on"))
    return not upd or cfg.settle_minutes == 0 or state.now() - upd >= timedelta(minutes=cfg.settle_minutes)


def _all_runs(s: dict[str, Any]):
    for rec in s["prs"].values():
        yield from rec.get("reviews") or []
    yield from s.get("test_runs") or []


def spend_today(s: dict[str, Any]) -> tuple[int, float, int]:
    """(envíos de hoy, USD conocidos, envíos sin coste conocido). Incluye las pruebas de Claude."""
    today = state.now().astimezone().date()
    runs, usd, unknown = 0, 0.0, 0
    for rv in _all_runs(s):
        at = state.parse_iso(rv.get("at"))
        if at and at.astimezone().date() == today:
            runs += 1
            if rv.get("cost_usd") is None:
                unknown += 1
            else:
                usd += float(rv["cost_usd"])
    return runs, usd, unknown


def spend_window(s: dict[str, Any], days: int) -> tuple[float, int]:
    start = state.now() - timedelta(days=days)
    usd, unknown = 0.0, 0
    for rv in _all_runs(s):
        at = state.parse_iso(rv.get("at"))
        if at and at > start:
            if rv.get("cost_usd") is None:
                unknown += 1
            else:
                usd += float(rv["cost_usd"])
    return usd, unknown


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


class _TickLock:
    def __enter__(self):
        paths.ensure_dirs()
        fd = os.open(paths.tick_lock_file(), os.O_WRONLY | os.O_CREAT, 0o600)
        self.f = os.fdopen(fd, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except BlockingIOError:
            self.acquired = False
        return self

    def __exit__(self, *exc):
        if self.acquired:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def tick_running() -> bool:
    """¿Hay un tick en curso (automático o manual)? Se mira el lock sin quedárselo."""
    with _TickLock() as lock:
        return not lock.acquired


def tick(cfg: Config, client: Client | None = None,
         runner: Callable[..., claude.RunResult] = claude.run, manual: bool = False) -> dict[str, Any]:
    with _TickLock() as lock:
        if not lock.acquired:
            return {"skipped": "otro tick sigue en curso"}
        try:
            return _tick(cfg, client, runner, manual)
        except Exception as e:  # noqa: BLE001 - cualquier fallo debe quedar visible en la TUI
            tb = traceback.format_exc()
            try:
                with state.locked() as s:
                    # Se registra una vez (con traceback) y luego como mucho cada hora.
                    state.report_error(s, f"El tick falló: {e!r}"[:300], "crash_error",
                                       kind=f"{type(e).__name__}:{e}"[:200], traceback=tb[-3000:])
                    _recover_inflight(s, force=True)
                    s["last_tick"] = {"at": state.now_iso(), "ok": False,
                                      "message": f"tick falló: {e!r}"[:300], "open_count": None}
            except Exception:  # noqa: BLE001 - p. ej. state.json corrupto: queda en journald
                pass
            raise


def _finish(ok: bool, message: str, open_count: int | None = None, clear_crash: bool = True) -> dict[str, Any]:
    with state.locked() as s:
        if clear_crash:  # solo si el código que pudo fallar se ejecutó de verdad
            state.clear_error(s, "crash_error")
        s["last_tick"] = {"at": state.now_iso(), "ok": ok, "message": message, "open_count": open_count}
        # Latido: aunque no pase nada, una línea por hora confirma en el log que el watcher sigue vivo.
        s["ticks_since_heartbeat"] = s.get("ticks_since_heartbeat", 0) + 1
        hb = state.parse_iso(s.get("heartbeat_at"))
        if ok and (hb is None or state.now() - hb >= timedelta(hours=HEARTBEAT_HOURS)):
            state.log(f"Sigo vigilando: {s['ticks_since_heartbeat']} ticks desde el último aviso · último: {message}")
            s["heartbeat_at"] = state.now_iso()
            s["ticks_since_heartbeat"] = 0
    return {"ok": ok, "message": message}


def last_success(s: dict[str, Any]) -> dict[str, Any] | None:
    """El último envío a Claude que acabó bien (PR real o prueba), para enseñarlo en la TUI y en status."""
    best = None
    for key, rec in s["prs"].items():
        for rv in rec.get("reviews") or []:
            if rv.get("ok") and (best is None or rv.get("at", "") > best["at"]):
                best = {"at": rv.get("at", ""), "pr": key, "link": rv.get("link", "")}
    for rv in s.get("test_runs") or []:
        if rv.get("ok") and (best is None or rv.get("at", "") > best["at"]):
            best = {"at": rv.get("at", ""), "pr": "prueba", "link": rv.get("link", "")}
    return best


def _recover_inflight(s: dict[str, Any], force: bool = False) -> None:
    """Registros 'revisando' cuyo proceso ya no existe (reinicio, kill, timeout de systemd)."""
    for key, rec in s["prs"].items():
        inf = rec.get("inflight")
        if not inf:
            continue
        if force or not _pid_alive(inf.get("pid")) or inf.get("pid") == os.getpid():
            rec["inflight"] = None
            rec["status"] = ST_FAILED
            head = inf.get("head")
            pub: dict[str, Any] = {}
            tf = inf.get("trace")
            if tf and Path(tf).resolve().parent == paths.traces_dir().resolve():
                # Si el Claude de ese intento sigue vivo (el watcher murió sin matarlo), se mata antes de
                # leer su rastro: si no, podría seguir publicando con un rastro que ya nadie mira.
                pidf = Path(tf + ".pid")
                try:
                    pid = int(pidf.read_text().strip())
                    # Solo si de verdad es nuestro (el pid se pudo reutilizar): su entorno lleva este rastro.
                    env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                    if f"PRWATCH_TRACE={tf}".encode() in env and claude.kill_group(pid):
                        state.log(f"PR #{key}: maté un Claude que seguía vivo de un intento interrumpido",
                                  "warn", key)
                except (OSError, ValueError):
                    pass
                pidf.unlink(missing_ok=True)
                done = (rec.get("published") or {}).get(head) or {}
                pub = claude.published_from(claude.read_trace(Path(tf), limit=None), done)
                if pub.get("now"):
                    rec["published"] = {head: {k: v for k, v in pub.items() if k in PUBLISHED_KEYS}}
            what = f"; ya había publicado: {claude.publish_summary(pub)}" if pub.get("now") else ""
            rec.setdefault("reviews", []).append({
                "at": state.now_iso(), "head": head, "reason": inf.get("reason"), "ok": False,
                "summary": f"envío interrumpido (el proceso murió antes de terminar){what}", "cost_usd": None,
                "duration_s": None, "run_file": None, "published": pub or None,
            })
            state.log(f"PR #{key}: envío a Claude interrumpido{what}; cuenta como intento fallido "
                      "(el siguiente intento no repite lo ya publicado)", "error", key)


def _tick(cfg: Config, client: Client | None, runner, manual: bool = False) -> dict[str, Any]:
    if not cfg.setup_done:
        return _finish(True, "sin configurar: abre `prwatch` y completa el asistente", clear_crash=False)
    with state.locked() as s:
        # Tenemos el lock de tick: cualquier 'inflight' es de un tick que murió (aunque su pid se reutilizara).
        _recover_inflight(s, force=True)
        if cfg.slack_user_id:
            state.clear_error(s, "config_error")
        # La pausa solo frena los ticks automáticos; un tick manual hace el escaneo completo.
        paused = state.is_paused(s) and not manual
        forced = list(dict.fromkeys(int(x) for x in s.get("forced", [])))
        me = s.get("me")
        me_retry = state.parse_iso(s.get("me_retry_at"))
        first_run = not s.get("initialized")
        prev_window = s.get("window_hours")
        baseline_cutoff = state.parse_iso(s.get("baseline_cutoff"))
        if not first_run and prev_window is not None and prev_window != cfg.lookback_hours:
            # Al ampliar la ventana (o pasar a 0 = todo), lo que solo aparece por eso no se manda en ráfaga.
            grew = cfg.lookback_hours == 0 or (prev_window and cfg.lookback_hours > prev_window)
            if prev_window and grew:
                baseline_cutoff = state.now() - timedelta(hours=prev_window)
                s["baseline_cutoff"] = baseline_cutoff.isoformat()
                state.log(f"Ventana cambiada de {prev_window} h a {cfg.lookback_hours or 'todo'}: los PRs que "
                          "aparecen solo por ampliarla quedan como punto de partida")
        s["window_hours"] = cfg.lookback_hours
        # Pasada la ventana anterior entera, ya nada puede aparecer "solo por ampliarla".
        if baseline_cutoff and cfg.lookback_hours and \
                state.now() - timedelta(hours=cfg.lookback_hours) > baseline_cutoff:
            baseline_cutoff = None
            s["baseline_cutoff"] = None
        backoff = state.parse_iso(s.get("backoff_until"))

    if paused and not forced:
        return _finish(True, "en pausa", clear_crash=False)
    if backoff and backoff > state.now() and not forced:
        return _finish(False, f"esperando por límite de Bitbucket hasta {backoff.astimezone():%H:%M}",
                       clear_crash=False)

    try:
        client = client or Client(cfg)
    except BitbucketError as e:
        with state.locked() as s:
            state.report_error(s, str(e), kind=e.kind)
        return _finish(False, str(e))

    if not me and (me_retry is None or me_retry <= state.now()):
        try:
            me = client.me()
            with state.locked() as s:
                s["me"], s["me_retry_at"] = me, None
        except BitbucketError as e:
            with state.locked() as s:
                s["me_retry_at"] = (state.now() + timedelta(minutes=ME_RETRY_MINUTES)).isoformat()
            state.log(f"No pude saber quién soy en Bitbucket ({e}); reintento en {ME_RETRY_MINUTES} min. "
                      "Mientras, configura my_bitbucket_nickname para no revisar tus propios PRs", "warn")

    queue: list[tuple[dict[str, Any], str]] = []
    open_count = None
    scan_error = None

    # 1) Revisiones pedidas a mano: se hacen aunque esté en pausa y sin filtros.
    for pr_id in forced:
        try:
            pr = client.pr(pr_id)
        except BitbucketError as e:
            state.log(f"No pude leer el PR #{pr_id}: {e}", "error", pr_id)
            continue
        if pr["state"] != "OPEN":
            state.log(f"PR #{pr_id} está {pr['state']}; no lo reviso", "warn", pr_id)
            continue
        if pr["destination"] != cfg.destination_branch:
            state.log(f"PR #{pr_id} apunta a {clean(pr['destination'])}, no a {cfg.destination_branch}: "
                      "lo reviso igual porque lo pediste, pero el escaneo automático no lo sigue", "warn", pr_id)
        queue.append((pr, "manual"))
    with state.locked() as s:
        s["forced"] = [x for x in s.get("forced", []) if int(x) not in forced]

    # 2) Escaneo automático (dentro de la ventana de horas, si hay).
    window_start = state.now() - timedelta(hours=cfg.lookback_hours) if cfg.lookback_hours else None
    closed_in_window: dict[str, str] = {}
    verified: list[str] = []
    if not paused and not (backoff and backoff > state.now()):
        try:
            prs = client.open_prs(window_start)
            if window_start is not None:
                prs = [p for p in prs if (state.parse_iso(p["updated_on"]) or window_start) >= window_start]
                prs += _stranded(client, cfg, {p["id"] for p in prs} | set(forced))
                verified_closed, verified = _verify_unseen(client, cfg, {p["id"] for p in prs} | set(forced))
                closed_in_window = {str(p["id"]): p["state"] for p in prs if p["state"] != "OPEN"}
                closed_in_window.update(verified_closed)
                # Un PR re-consultado que ya no apunta a la rama destino sale del alcance: como cerrado.
                for p in prs:
                    if p["state"] == "OPEN" and p["destination"] != cfg.destination_branch:
                        closed_in_window[str(p["id"])] = "cambiado de rama"
                prs = [p for p in prs if str(p["id"]) not in closed_in_window]
                prs = [p for p in prs if p["state"] == "OPEN"]
            with state.locked() as s:
                state.clear_error(s)
                s["backoff_until"] = None
        except BitbucketError as e:
            prs, scan_error = None, str(e)
            with state.locked() as s:
                state.report_error(s, scan_error, kind=e.kind)
                if e.retry_after:
                    s["backoff_until"] = (state.now() + timedelta(seconds=e.retry_after)).isoformat()
        if prs is not None:
            open_count = len(prs)
            auto: list[tuple[dict[str, Any], str]] = []
            open_ids = {str(p["id"]) for p in prs}
            forced_ids = {p["id"] for p, _ in queue}
            with state.locked() as s:
                for key in verified:
                    if key in s["prs"]:
                        s["prs"][key]["checked_at"] = state.now_iso()
                for pr in prs:
                    rec = s["prs"].setdefault(str(pr["id"]), {})
                    is_new_record = "last_seen" not in rec
                    marker = (s.get("reviewed_heads") or {}).get(str(pr["id"]))
                    if is_new_record and marker and not rec.get("reviewed_head"):
                        rec["reviewed_head"] = marker  # ya se revisó antes de olvidarlo: no pagar otra vez
                    _merge_pr(rec, pr)
                    before = rec.get("status")
                    # Con ventana, el primer arranque revisa lo que hay en ella (la ventana ya acota la ráfaga).
                    reason = decide(rec, pr, cfg, s.get("me"), first_run and window_start is None,
                                    baseline_cutoff, is_new_record)
                    if reason and pr["id"] not in forced_ids:
                        if not _settled(pr, cfg):
                            rec["status"] = ST_SETTLING
                        else:
                            rec["status"] = ST_PENDING
                            auto.append((pr, reason))
                    after = rec.get("status")
                    if first_run and window_start is None:
                        continue
                    if is_new_record:
                        state.log(f"Nuevo PR #{pr['id']} de {clean(pr['author'])}: {after or 'detectado'}",
                                  pr=pr["id"])
                    elif after != before and after not in QUIET_TRANSITIONS:
                        state.log(f"PR #{pr['id']}: {before or '-'} → {after}", pr=pr["id"])
                forget_before = state.now() - timedelta(days=FORGET_CLOSED_DAYS)
                for key, rec in list(s["prs"].items()):
                    off_branch = rec.get("destination") not in (None, "", cfg.destination_branch)
                    if off_branch:
                        # Revisado a mano hacia otra rama: el escaneo no lo ve, así que no se puede saber si
                        # se cerró. No se marca 'cerrado' (sería falso); se olvida tras 30 días sin verlo.
                        seen = state.parse_iso(rec.get("last_seen"))
                        if seen and seen < forget_before:
                            del s["prs"][key]
                        continue
                    if window_start is None:
                        gone = key not in open_ids
                        why = "(cerrado, fusionado o cambiado de rama)"
                    else:
                        # Con ventana, no aparecer solo significa "sin actividad reciente": solo se cierra
                        # lo que Bitbucket dice que se fusionó o se cerró dentro de la ventana.
                        gone = key in closed_in_window
                        why = f"({closed_in_window.get(key, '').lower()})"
                    if gone and rec.get("status") != ST_CLOSED and int(key) not in forced_ids:
                        rec["status"] = ST_CLOSED
                        rec["closed_at"] = state.now_iso()
                        if rec.get("title"):
                            state.log(f"PR #{key} ya no aparece abierto hacia {cfg.destination_branch} {why}",
                                      pr=key)
                    closed_at = state.parse_iso(rec.get("closed_at") or rec.get("last_seen"))
                    if rec.get("status") == ST_CLOSED and closed_at and closed_at < forget_before:
                        del s["prs"][key]
                        continue
                    seen = state.parse_iso(rec.get("last_seen"))
                    if (window_start is not None and seen and seen < forget_before
                            and int(key) not in forced_ids and not rec.get("ignored")):
                        if rec.get("reviewed_head"):
                            marks = s.setdefault("reviewed_heads", {})
                            marks[key] = rec["reviewed_head"]
                            for old in list(marks)[:-MAX_REVIEWED_MARKERS]:
                                del marks[old]
                        del s["prs"][key]  # lleva 30 días fuera de la ventana
                if first_run:
                    s["initialized"] = True
                    if window_start is None:
                        state.log(f"Primer arranque: {len(prs)} PRs abiertos marcados como punto de partida")
                    else:
                        state.log(f"Primer arranque: {len(prs)} PRs abiertos con actividad en las últimas "
                                  f"{cfg.lookback_hours} h; se mandan a Claude (máx {cfg.max_reviews_per_tick} por tick)")
            auto.sort(key=lambda t: t[0]["updated_on"])
            budget = cfg.max_reviews_per_tick
            queue += auto[:budget]
            if len(auto) > budget:
                state.log(f"{len(auto) - budget} PRs esperan al siguiente tick "
                          f"(máx {cfg.max_reviews_per_tick} por tick)")

    # 3) Llamar a Claude, uno a uno, sin tener el lock del estado.
    if queue and not cfg.slack_user_id:
        with state.locked() as s:
            state.report_error(s, "Falta slack_user_id en Ajustes: no se manda nada a Claude "
                                  "(el guardián bloquearía el DM y se gastarían tokens)", "config_error")
            for pr, _ in queue:
                s["prs"].setdefault(str(pr["id"]), {})["status"] = ST_PENDING
                if _ == "manual":
                    s["forced"].append(pr["id"])
        queue = []
    done = attempted = 0
    last_failure = ""
    for pr, reason in queue:
        key = str(pr["id"])
        with state.locked() as s:
            rec = s["prs"].setdefault(key, {})
            _merge_pr(rec, pr)
            rec["status"] = ST_REVIEWING
            prior_ok = next((rv for rv in reversed(rec.get("reviews") or []) if rv.get("ok")), None)
            # Lo ya publicado en ESTE commit (por un intento anterior): el guardián no deja repetirlo.
            done_here = dict((rec.get("published") or {}).get(pr["head"]) or {})
            me_ids = {str((s.get("me") or {}).get(k) or "").lower() for k in ("uuid", "nickname")} - {""}
            if cfg.my_bitbucket_nickname.strip():
                me_ids.add(cfg.my_bitbucket_nickname.strip().lower())
            # El intento cuenta ANTES de llamar: si el proceso muere, no se repite gratis.
            same = rec.get("fail_head") == pr["head"]
            rec["fail_head"] = pr["head"]
            rec["fail_count"] = (rec.get("fail_count", 0) + 1) if same else 1
            trace_file = str(claude.new_trace_file())
            # El rastro va en el registro: si el proceso muere, el siguiente tick sabe qué llegó a publicarse.
            rec["inflight"] = {"head": pr["head"], "reason": reason, "pid": os.getpid(),
                               "at": state.now_iso(), "trace": trace_file}
        state.log(f"Mandando PR #{pr['id']} a Claude ({claude.REASON_TEXT.get(reason, reason)})", pr=pr["id"])
        attempted += 1
        extra: dict[str, Any] = {}
        if prior_ok and prior_ok.get("head") != pr["head"] and prior_ok.get("open_findings"):
            extra = {"prior": prior_ok["open_findings"], "prior_head": prior_ok.get("head", "")}
        try:
            extra["ci"] = client.ci_summary(pr["head"])
        except (BitbucketError, AttributeError):
            pass  # sin CI: la revisión lo dirá como "no verificado"
        if cfg.post_to_bitbucket and reason != "test":
            extra["done"] = done_here
            try:
                extra["my_vote"] = client.pr_status(pr["id"], me_ids)["my_vote"]
            except (BitbucketError, AttributeError, KeyError):
                pass
        extra["trace_file"] = trace_file
        result = runner(cfg, pr, reason, **extra)
        if cfg.post_to_bitbucket and reason != "test" and result.published.get("comment"):
            if me_ids:
                _verify_vote(client, pr["id"], me_ids, result)
            else:
                state.log(f"PR #{pr['id']}: no sé quién eres en Bitbucket (pon tu nickname en Ajustes); "
                          "el voto queda sin verificar", "warn", pr["id"])
        with state.locked() as s:
            rec = s["prs"][key]
            rec["inflight"] = None
            rec.setdefault("reviews", []).append({
                "at": state.now_iso(), "head": pr["head"], "reason": reason, "ok": result.ok,
                "summary": result.summary, "cost_usd": result.cost_usd,
                "tokens": result.tokens, "duration_s": round(result.duration_s, 1),
                "run_file": result.run_file, "link": result.link,
                "verdict": result.verdict, "findings": result.findings,
                # Si la respuesta final no se pudo leer, se conservan los hallazgos abiertos anteriores.
                "open_findings": result.open_findings if result.verdict
                else ((prior_ok or {}).get("open_findings") or []),
                "unparsed": bool(result.ok and not result.verdict),
                "published": result.published or None, "unchanged": result.unchanged,
            })
            if result.published:
                # Solo se guarda lo del commit actual: un commit nuevo es una revisión nueva.
                rec["published"] = {pr["head"]: {k: v for k, v in result.published.items()
                                                 if k in PUBLISHED_KEYS}}
            rec["reviews"] = rec["reviews"][-MAX_REVIEWS_KEPT:]
            if result.ok:
                rec.update(status=ST_REVIEWED, reviewed_head=pr["head"], was_draft=False,
                           fail_head=None, fail_count=0)
                done += 1
            else:
                rec["status"] = ST_FAILED
                last_failure = result.summary
            attempts = rec.get("fail_count", 0)
        if result.ok and not result.verdict and reason != "test":
            state.log(f"PR #{pr['id']}: revisión enviada, pero no pude leer el veredicto final de Claude; "
                      "conservo los hallazgos abiertos anteriores", "warn", pr["id"])
        if result.published.get("errors"):
            state.log(f"PR #{pr['id']}: algo no se pudo publicar: {'; '.join(result.published['errors'])}",
                      "warn", pr["id"])
        cost = f"${result.cost_usd:.3f}" if result.cost_usd is not None else "coste desconocido"
        retry = "" if result.ok else (
            f" · intento {attempts}/{MAX_FAILS_PER_HEAD}" +
            (" (sin más reintentos hasta un commit nuevo o revisión manual)" if attempts >= MAX_FAILS_PER_HEAD else ""))
        state.log(
            f"PR #{pr['id']}: {'OK' if result.ok else 'FALLÓ'} en {result.duration_s:.0f}s · {cost}{retry} — {result.summary}",
            "info" if result.ok else "error", pr["id"], cost_usd=result.cost_usd, tokens=result.tokens or None,
        )

    if queue:
        with state.locked() as s:
            if done:
                state.clear_error(s, "claude_error")
            elif attempted:
                state.report_error(s, f"Ningún envío a Claude funcionó: {last_failure}"[:300],
                                   "claude_error", kind=last_failure[:60])
    if paused:
        msg = "en pausa"
    elif scan_error:
        msg = f"error: {scan_error}"[:200]
    else:
        msg = f"{open_count} abiertos" + (f" con actividad en {cfg.lookback_hours} h" if window_start else "")
    if attempted:
        msg += f" · {done}/{attempted} enviados a Claude OK"
    return _finish(scan_error is None and done == attempted, msg, open_count)


def _verify_vote(client: Client, pr_id: int, me_ids: set[str], result: Any) -> None:
    """Tras publicar, el watcher mira en Bitbucket (con su token) cuál es de verdad tu voto."""
    try:
        vote = client.pr_status(pr_id, me_ids)["my_vote"]
    except (BitbucketError, AttributeError, KeyError) as e:
        state.log(f"PR #{pr_id}: no pude comprobar tu voto en Bitbucket ({e}); queda sin verificar", "warn", pr_id)
        return
    pub = result.published
    want = "approved" if (pub.get("comment") or {}).get("verdict") == "approve" else "changes_requested"
    if pub.get("state") and vote != pub["state"]:
        state.log(f"PR #{pr_id}: Claude marcó {pub['state']} pero Bitbucket muestra tu voto como "
                  f"{vote or 'ninguno'}; no lo doy por hecho", "error", pr_id)
        pub.pop("state", None)
        pub.setdefault("errors", []).append(f"bb_state: Bitbucket muestra {vote or 'ningún voto'}")
        if result.ok:
            result.ok = False
            result.summary = "publicado a medias (el voto no aparece en Bitbucket): " + result.summary
        claude.update_run_file(result.run_file, ok=result.ok, summary=result.summary, published=pub)
    if want == "changes_requested" and vote == "approved":
        state.log(f"PR #{pr_id}: ⚠ sigue tu APROBACIÓN de un commit anterior aunque la revisión pide cambios; "
                  "el conector no puede retirarla: quítala a mano en Bitbucket", "error", pr_id)
        pub.setdefault("errors", []).append("sigue tu aprobación anterior: quítala a mano")
        claude.update_run_file(result.run_file, ok=result.ok, summary=result.summary, published=pub)


PUBLISHED_KEYS = ("comment", "comment_id", "state", "thread", "thread_link", "dm", "reactions")
STRANDED = {ST_PENDING, ST_SETTLING, ST_FAILED, ST_REVIEWING}
NOT_VERIFIED = STRANDED | {ST_CLOSED, ST_IGNORED}


def _verify_unseen(client: Client, cfg: Config, seen: set[int]) -> tuple[dict[str, str], list[str]]:
    """Re-comprueba (pocos por tick, cada hora como mucho) PRs ya conocidos que no salen en la ventana.

    Así un PR que se fusionó o cerró sin pasar por la ventana (p. ej. con el watcher apagado) no se
    queda en la tabla como si siguiera abierto. Devuelve ({id: motivo de cierre}, [ids comprobados]).
    """
    s = state.load()
    limit = state.now() - timedelta(minutes=VERIFY_EVERY_MINUTES)
    cands = [(k, r) for k, r in s["prs"].items()
             if int(k) not in seen and r.get("status") not in NOT_VERIFIED
             and r.get("destination") in (None, "", cfg.destination_branch)
             and (state.parse_iso(r.get("checked_at")) or state.parse_iso("2000-01-01T00:00:00+00:00")) < limit]
    cands.sort(key=lambda kr: kr[1].get("checked_at") or "")
    closed: dict[str, str] = {}
    checked: list[str] = []
    for key, _ in cands[:MAX_VERIFY_FETCH]:
        try:
            pr = client.pr(int(key))
        except BitbucketError as e:
            if "404" in str(e):
                closed[key] = "ya no existe"
            continue
        checked.append(key)
        if pr["state"] != "OPEN":
            closed[key] = pr["state"]
        elif pr["destination"] != cfg.destination_branch:
            closed[key] = "cambiado de rama"
    return closed, checked


def _stranded(client: Client, cfg: Config, seen: set[int]) -> list[dict[str, Any]]:
    """PRs que esperaban algo (turno, asentarse, reintento) y han salido de la ventana sin actividad.

    Se re-consultan uno a uno (con tope por tick) para que no se queden atascados hasta olvidarse.
    """
    s = state.load()
    ids = [int(k) for k, r in s["prs"].items()
           if r.get("status") in STRANDED and int(k) not in seen
           and r.get("destination") in (None, "", cfg.destination_branch)
           and not (r.get("status") == ST_FAILED and r.get("fail_count", 0) >= MAX_FAILS_PER_HEAD)]
    out = []
    for pr_id in ids[:MAX_STRANDED_FETCH]:
        try:
            out.append(client.pr(pr_id))
        except BitbucketError as e:
            state.log(f"No pude re-consultar el PR #{pr_id}: {e}", "warn", pr_id)
    return out


def clean(v: Any) -> str:
    return claude.clean(v, 80)
