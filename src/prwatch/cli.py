"""`prwatch` sin argumentos abre la TUI; con subcomando hace la acción y sale."""
from __future__ import annotations

import argparse
import json
import os
import sys

from prwatch import actions, claude, config, service, state, tick
from prwatch.text import strip_ctrl


def _status() -> int:
    s = state.load()
    sv = service.status()
    cfg = config.load()
    paused = state.is_paused(s)
    level, text = actions.health(s, sv, cfg)
    print(f"salud:    {level.upper()} — {text}")
    if paused:
        print(f"modo:     en pausa{' hasta ' + _local(s['paused_until']) if s.get('paused_until') else ''}")
    elif sv["active"]:
        print("modo:     vigilando")
    else:
        print("modo:     timer parado (prwatch resume)" if sv["installed"] else "modo:     sin instalar (abre prwatch)")
    print(f"próximo:  {sv['next'].astimezone():%H:%M:%S}" if sv.get("next") else "próximo:  -")
    lt = s.get("last_tick") or {}
    print(f"último:   {lt.get('at', '-')} {'OK' if lt.get('ok', True) else 'ERROR'} {lt.get('message', '')}")
    ls = tick.last_success(s)
    print(f"último DM: {_local(ls['at']) + ' · PR ' + str(ls['pr']) + (' · ' + ls['link'] if ls['link'] else '') if ls else 'ninguno todavía'}")
    runs, usd, unk = tick.spend_today(s)
    print(f"hoy:      {runs} envíos a Claude (${usd:.2f}{f', +{unk} sin coste conocido' if unk else ''})")
    print(f"ventana:  {str(cfg.lookback_hours) + ' h' if cfg.lookback_hours else 'todos los abiertos'}"
          f"{' · desde ' + cfg.since if cfg.since else ''}")
    if not cfg.setup_done:
        print("aviso:    sin configurar — ejecuta `prwatch` para abrir el asistente")
    print(f"claude:   {cfg.claude_command} [{cfg.claude_shell}]")
    for key, rec in sorted(s["prs"].items(), key=lambda kv: -int(kv[0])):
        if rec.get("status") == "cerrado":
            continue
        print(f"  #{key:<6} {rec.get('status') or '-':<16} {claude.clean(rec.get('author'), 20):<20} {claude.clean(rec.get('title'), 60)}")
    return 0


def _local(ts: str | None) -> str:
    dt = state.parse_iso(ts)
    return dt.astimezone().strftime("%d/%m %H:%M:%S") if dt else "-"


def _last_run() -> int:
    from prwatch import paths
    files = sorted(paths.runs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        print("Todavía no hay ninguna llamada a Claude.")
        return 0
    r = json.loads(files[-1].read_text())
    c = lambda v, n=300: strip_ctrl(v, n)  # noqa: E731
    print(f"archivo:   {files[-1]}")
    print(f"cuándo:    {_local(r.get('at'))}   PR #{r.get('pr')} ({r.get('reason')})")
    print(f"resultado: {'OK' if r.get('ok') else 'FALLÓ'} — {c(r.get('summary'))}")
    print(f"claude:    código {r.get('returncode')} · {r.get('subtype')} · {r.get('num_turns')} turnos · "
          f"{r.get('duration_s')} s · coste {r.get('cost_usd') if r.get('cost_usd') is not None else '?'}")
    trace = r.get("trace") or []
    print(f"hooks:     {len(trace)} eventos" + ("  (ninguno: ¿se aplicó --settings?)" if not trace else ""))
    for e in trace:
        if e.get("phase") == "pre":
            print(f"   pide  {c(e.get('tool'), 80)} → {'BLOQUEADO: ' + c(e.get('reason'), 150) if e.get('blocked') else 'permitido'}")
            continue
        kind = e.get("kind") or ""
        what = {"bb_comment": "comentario BB", "bb_state": f"voto BB ({e.get('state')})", "thread": "hilo Slack",
                "dm": "DM", "reaction": f"reacción :{c(e.get('emoji'), 30)}:", "read_channel": "leyó el canal",
                "read_thread": "abrió el hilo"}.get(kind, "")
        extra = ""
        if kind in ("bb_comment", "bb_state", "thread", "dm", "reaction"):
            extra = " → " + (f"OK {c(e.get('link'), 150)}".rstrip() if e.get("sent")
                             else f"FALLÓ {c(e.get('error'), 200)}")
            if kind == "bb_comment" and e.get("sent"):
                extra += f" (id {e.get('comment_id')}, {e.get('verdict')}, {e.get('findings')} hallazgos)"
        elif kind == "read_channel":
            extra = f" → {len(e.get('candidates') or [])} mensajes enlazan el PR"
        elif kind == "read_thread":
            extra = f" → {'anuncio confirmado' if e.get('confirmed') else 'no enlaza el PR'} ({c(e.get('ts'), 20)})"
        print(f"   usó   {c(e.get('tool'), 80)}{(' ' + what) if what else ''}{extra}")
    if r.get("published"):
        from prwatch.claude import publish_summary
        print(f"publicado: {c(publish_summary(r['published']), 500)}")
    if r.get("permission_denials"):
        print(f"denegadas: {c(json.dumps(r['permission_denials'], ensure_ascii=False), 400)}")
    if r.get("result"):
        print(f"respuesta: {c(r['result'], 500)}")
    if r.get("stderr", "").strip():
        print(f"stderr:    {c(r['stderr'][-500:], 500)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="prwatch", description="Vigilante de PRs de Bitbucket")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="abre la interfaz (por defecto)")
    tp = sub.add_parser("tick", help="ejecuta un tick (lo usa el timer)")
    tp.add_argument("--manual", action="store_true", help="tick manual: escanea aunque esté en pausa")
    sub.add_parser("status", help="muestra el estado")
    sub.add_parser("enable", help="instala y arranca el timer de systemd (lo hace el asistente)")
    sub.add_parser("disable", help="alias de pause")
    sub.add_parser("remove-timer", help="para el timer y borra sus units de systemd (no desinstala la app)")
    pp = sub.add_parser("pause", help="para el timer: sin ticks automáticos (los manuales siguen)")
    pp.add_argument("minutes", nargs="?", type=int, help="minutos; sin valor = indefinida")
    sub.add_parser("resume", help="reanuda")
    sub.add_parser("run", help="lanza un tick manual ahora (también en pausa)")
    for name, hlp in (("review", "revisar ya un PR concreto"), ("ignore", "ignorar un PR"),
                      ("unignore", "dejar de ignorar un PR")):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("pr", type=int)
    sp = sub.add_parser("since", help="solo PRs creados desde AAAA-MM-DD (--clear para quitarlo)")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("date", nargs="?")
    g.add_argument("--clear", action="store_true")
    sub.add_parser("test-claude", help="manda un DM de prueba a Slack vía Claude")
    sub.add_parser("config-path", help="muestra dónde están config, prompt y estado")
    gp = sub.add_parser("guard", help=argparse.SUPPRESS)
    gp.add_argument("--post", action="store_true")
    lp = sub.add_parser("logs", help="últimos eventos del watcher")
    lp.add_argument("-n", type=int, default=30)
    sub.add_parser("last-run", help="detalle de la última llamada a Claude")

    a = p.parse_args(argv)
    os.umask(0o077)
    if a.cmd == "guard":
        from prwatch import guard
        return guard.main(post_phase=a.post)
    try:
        if a.cmd in (None, "tui"):
            from prwatch.tui import run_tui
            run_tui()
            return 0
        if a.cmd == "tick":
            print(json.dumps(tick.tick(config.load(), manual=a.manual), ensure_ascii=False))
            return 0
        if a.cmd == "status":
            if actions.resume_if_expired():
                print("(la pausa programada ya había terminado: reanudado)")
            return _status()
        if a.cmd == "logs":
            for ev in state.tail_events(max(1, a.n)):
                lvl = {"error": "✖", "warn": "▲"}.get(ev.get("level"), "·")
                print(f"{_local(ev.get('at'))} {lvl} {strip_ctrl(ev.get('msg', ''), 400)}")
            return 0
        if a.cmd == "last-run":
            return _last_run()
        if a.cmd == "config-path":
            from prwatch import paths, claude
            claude.ensure_prompt()
            print(f"config: {paths.config_file()}\nprompt: {paths.prompt_file()}\n"
                  f"estado: {paths.state_file()}\nlog:    {paths.events_file()}\nruns:   {paths.runs_dir()}")
            return 0
        if a.cmd == "remove-timer":
            service.remove_timer()
            print("units eliminadas")
            return 0
        if a.cmd == "run":
            print(service.run_now())
            return 0
        if a.cmd == "since":
            print(actions.set_since(None if a.clear else a.date))
            return 0
        fn = {"enable": actions.enable, "disable": lambda: actions.pause(None), "resume": actions.resume,
              "test-claude": actions.test_claude}.get(a.cmd)
        if fn:
            print(fn())
        elif a.cmd == "pause":
            print(actions.pause(a.minutes))
        elif a.cmd in ("review", "ignore", "unignore"):
            print(getattr(actions, a.cmd)(a.pr))
        return 0
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
