"""Interfaz de terminal (Textual) para controlar el watcher."""
from __future__ import annotations

import subprocess
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any, Callable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, Log, Select,
                             Static, Switch)

from prwatch import actions, config, paths, service, state, tick
from prwatch.config import CLAUDE_SHELLS, Config
from prwatch.widgets import ToolList
from prwatch.text import strip_ctrl as safe
from prwatch.wizard import WizardScreen

STATUS_STYLE = {
    "revisando": "bold yellow",
    "pendiente": "bold cyan",
    "falló": "bold red",
    "revisado": "green",
    "baseline": "blue",
    "esperando": "cyan",
    "draft": "dim",
    "ignorado": "dim strike",
    "mío": "dim",
    "autor ignorado": "dim",
    "anterior a fecha": "dim",
    "cerrado": "dim",
}
STATUS_ORDER = ["revisando", "pendiente", "esperando", "falló", "revisado", "baseline", "draft"]
HIDDEN_BY_DEFAULT = {"cerrado", "ignorado", "mío", "autor ignorado", "anterior a fecha", "draft"}
LEVEL_MARK = {"error": "✖ ", "warn": "▲ "}


def local(ts: str | datetime | None, fmt: str = "%d/%m %H:%M") -> str:
    if not ts:
        return "-"
    dt = ts if isinstance(ts, datetime) else state.parse_iso(ts)
    return dt.astimezone().strftime(fmt) if dt else "-"


def ago(ts: str | None) -> str:
    dt = state.parse_iso(ts)
    if not dt:
        return "-"
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    if s < 60:
        return f"hace {s}s"
    if s < 3600:
        return f"hace {s // 60}m"
    if s < 86400:
        return f"hace {s // 3600}h"
    return f"hace {s // 86400}d"


def until(dt: datetime | None) -> str:
    if not dt:
        return "-"
    s = int((dt - datetime.now(timezone.utc)).total_seconds())
    return f"{local(dt, '%H:%M:%S')} (en {max(s, 0) // 60}m {max(s, 0) % 60:02d}s)"


# ─────────────────────────── Modales ───────────────────────────

class AskModal(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, title: str, hint: str, value: str = "",
                 validate: Callable[[str], str | None] | None = None):
        super().__init__()
        self.title_text, self.hint, self.value, self.validate = title, hint, value, validate

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Label(self.hint, classes="hint")
            yield Input(value=self.value, id="ask")
            yield Label("", id="err", classes="err")

    @on(Input.Submitted)
    def submit(self, ev: Input.Submitted) -> None:
        v = ev.value.strip()
        err = self.validate(v) if self.validate else None
        if err:
            self.query_one("#err", Label).update(err)
            return
        self.dismiss(v)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _pr_number(v: str) -> str | None:
    return None if v.lstrip("#").isdigit() else "Escribe un número de PR, p. ej. 3567"


def _date_or_empty(v: str) -> str | None:
    if not v:
        return None
    try:
        date.fromisoformat(v)
        return None
    except ValueError:
        return "Formato AAAA-MM-DD (o vacío para quitar el filtro)"


def _minutes_or_empty(v: str) -> str | None:
    return None if not v or v.isdigit() else "Minutos (número) o vacío para pausa indefinida"


class DetailModal(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,enter", "close", "Cerrar")]

    def __init__(self, rec: dict[str, Any]):
        super().__init__()
        self.rec = rec

    def compose(self) -> ComposeResult:
        r = self.rec
        t = Text()
        t.append(f"PR #{r.get('id')}  ", style="bold")
        t.append(f"{safe(r.get('title'))}\n\n")
        for label, key in (("Autor", "author"), ("Estado bot", "status"), ("Rama", "source_branch"),
                           ("Destino", "destination"), ("Commit", "head"), ("Revisado en", "reviewed_head"),
                           ("Enlace", "url")):
            t.append(f"{label:<12}", style="dim")
            t.append(f"{safe(r.get(key)) or '-'}\n")
        t.append(f"{'Draft':<12}", style="dim")
        t.append(f"{'sí' if r.get('draft') else 'no'}\n")
        t.append(f"{'Creado':<12}", style="dim")
        t.append(f"{local(r.get('created_on'))}\n")
        t.append(f"{'Actualizado':<12}", style="dim")
        t.append(f"{local(r.get('updated_on'))}\n\n")
        t.append("Envíos a Claude\n", style="bold")
        reviews = r.get("reviews") or []
        if not reviews:
            t.append("  (ninguno todavía)\n", style="dim")
        for rv in reviews[-10:][::-1]:
            ok = bool(rv.get("ok"))
            t.append(f"  {local(rv.get('at'))}  ", style="dim")
            t.append("OK  " if ok else "FALLÓ  ", style="green" if ok else "red")
            cost = f" ${rv['cost_usd']:.3f}" if rv.get("cost_usd") is not None else " coste ?"
            t.append(f"{rv.get('reason')} · {rv.get('duration_s') or '?'}s{cost} · {str(rv.get('head') or '')[:12]}\n")
            if rv.get("verdict"):
                t.append(f"    veredicto: {rv['verdict']} · {rv.get('findings') or 0} hallazgos\n")
            for f in (rv.get("open_findings") or [])[:10]:
                t.append(f"    {safe(f, 160)}\n", style="yellow")
            if rv.get("unchanged"):
                t.append("    sin cambios desde la revisión anterior: no se publicó nada\n", style="dim")
            elif rv.get("published"):
                from prwatch.claude import publish_summary
                bad = isinstance(rv["published"], dict) and (rv["published"].get("errors") or rv["published"].get("blocked"))
                t.append(f"    publicado: {safe(publish_summary(rv['published']), 300)}\n",
                         style="yellow" if bad else "green")
            if rv.get("link"):
                t.append(f"    {safe(rv['link'], 200)}\n", style="dim")
            if not ok:
                t.append(f"    {safe(rv.get('summary'))}\n", style="red")
            if rv.get("run_file"):
                t.append(f"    {rv['run_file']}\n", style="dim")
        with VerticalScroll(classes="dialog wide"):
            yield Static(t)
            yield Label("Esc para cerrar", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


class SettingsModal(ModalScreen[Config | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancelar"), Binding("ctrl+s", "save", "Guardar")]

    TEXT_FIELDS = [
        ("claude_command", "Comando de Claude", "Alias o ejecutable, p. ej. claude"),
        ("claude_model", "Modelo (opcional)", "Vacío = el de la cuenta. Ej: opus, sonnet"),
        ("slack_user_id", "Tu usuario de Slack (obligatorio)",
         "Ej: U0123ABC (Slack → tu perfil → ⋮ → Copiar ID de miembro). Solo se permiten DMs a este id"),
        ("slack_review_channel", "Canal de anuncios de PRs",
         "Id del canal (C…) donde se anuncian los PRs; la revisión va en el hilo del anuncio"),
        ("claude_max_turns", "Máx. turnos por llamada", ""),
        ("claude_timeout_seconds", "Timeout por llamada (s)", ""),
        ("claude_args", "Argumentos extra", "Separados por espacio (opcional)"),
        ("lookback_hours", "Ventana de búsqueda (horas)", "PRs con actividad en las últimas N horas (0 = todos los abiertos)"),
        ("interval_seconds", "Intervalo del timer (s)", "Mínimo 30"),
        ("settle_minutes", "Esperar tras el último push (min)", "Junta varios pushes seguidos en un solo envío"),
        ("max_reviews_per_tick", "Máx. envíos a Claude por tick", ""),
        ("keep_run_files", "Runs guardados", "Se borran los más antiguos"),
        ("since", "Solo PRs creados desde", "AAAA-MM-DD o vacío"),
        ("ignored_authors", "Autores ignorados", "Nombres o nicknames separados por coma"),
        ("my_bitbucket_nickname", "Mi nickname de Bitbucket",
         "Tu nickname o nombre visible en Bitbucket. Hace falta si el token no tiene el permiso read:user"),
        ("project_dir", "Carpeta del proyecto", "Repo local; de ahí sale la skill de revisión"),
        ("review_skill", "Skill de revisión", "Carpeta en <proyecto>/.claude/skills/ (p. ej. personal-pr-review)"),
        ("bitbucket_env_file", "Archivo .env con BB_EMAIL/BB_TOKEN", ""),
        ("bitbucket_workspace", "Workspace", ""),
        ("bitbucket_repo", "Repositorio", ""),
        ("jira_projects", "Proyectos de Jira", "Claves separadas por coma (p. ej. ABC); vacío = ningún ticket"),
        ("destination_branch", "Rama destino", ""),
    ]
    BOOL_FIELDS = [
        ("post_to_bitbucket", "Publicar la revisión en Bitbucket y en el hilo del PR (apagado = solo DM a mí)"),
        ("slack_reactions", "Reaccionar en el anuncio del PR (👀 al empezar, ⚠️ / ✅ al terminar)"),
        ("skip_my_prs", "Ignorar mis propios PRs (solo revisar los de los demás)"),
        ("skip_drafts", "Ignorar PRs en draft hasta que pasen a ready"),
        ("review_existing_on_first_run", "Solo sin ventana (0 h): en el primer arranque, revisar también los PRs ya abiertos"),
    ]

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    @staticmethod
    def _as_text(v: Any, name: str) -> str:
        if isinstance(v, list):
            return " ".join(v) if name == "claude_args" else ", ".join(v)
        return str(v)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide", id="settings-dialog"):
            yield Label("Ajustes", classes="dialog-title")
            with VerticalScroll(id="settings-body"):
                yield Label("Modo de shell para lanzar Claude", classes="field")
                yield Label("zsh-interactive carga tu ~/.zshrc (necesario para alias)", classes="hint")
                yield Select([(s, s) for s in CLAUDE_SHELLS], value=self.cfg.claude_shell,
                             allow_blank=False, id="claude_shell")
                for name, label, hint in self.TEXT_FIELDS:
                    yield Label(label, classes="field")
                    if hint:
                        yield Label(hint, classes="hint")
                    yield Input(self._as_text(getattr(self.cfg, name), name), id=name)
                yield Label("Herramientas permitidas", classes="field")
                yield Label("Nombres exactos que Claude puede usar (míralos con /mcp). El guardián decide además "
                            "qué puede hacer con cada una.", classes="hint")
                yield ToolList(self.cfg.claude_allowed_tools, id="tools")
                for name, label in self.BOOL_FIELDS:
                    with Horizontal(classes="switch-row"):
                        yield Switch(value=getattr(self.cfg, name), id=name)
                        yield Label(label)
            yield Label("", id="err", classes="err")
            with Horizontal(classes="buttons"):
                yield Button("Guardar (ctrl+s)", variant="primary", id="save")
                yield Button("Cancelar (esc)", id="cancel")

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        data = asdict(self.cfg)
        data["claude_shell"] = self.query_one("#claude_shell", Select).value
        try:
            for name, _, _ in self.TEXT_FIELDS:
                raw = self.query_one(f"#{name}", Input).value.strip()
                cur = getattr(self.cfg, name)
                if isinstance(cur, list):
                    sep = None if name == "claude_args" else ","
                    data[name] = [x.strip() for x in raw.split(sep) if x.strip()]
                    if name == "jira_projects":
                        data[name] = [x.upper() for x in data[name]]
                elif isinstance(cur, bool):
                    pass
                elif isinstance(cur, float):
                    data[name] = float(raw)
                elif isinstance(cur, int):
                    data[name] = int(raw)
                else:
                    data[name] = raw
        except ValueError:
            self.query_one("#err", Label).update("Los campos numéricos deben ser números")
            return
        for name, _ in self.BOOL_FIELDS:
            data[name] = self.query_one(f"#{name}", Switch).value
        data["claude_allowed_tools"] = list(self.query_one("#tools", ToolList).tools)
        new = Config(**data)
        errors = new.validate()
        if errors:
            self.query_one("#err", Label).update(" · ".join(errors))
            return
        self.dismiss(new)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ─────────────────────────── App ───────────────────────────

class WatcherApp(App):
    TITLE = "pr-watcher"
    CSS = """
    Screen { layout: vertical; }
    #status { height: auto; padding: 0 1; border: round $primary; }
    #prs { height: 1fr; }
    #log { height: 12; border: round $panel; }
    .dialog { width: 70; height: auto; max-height: 90%; padding: 1 2; border: thick $primary;
              background: $surface; }
    .dialog.wide { width: 100; }
    AskModal, DetailModal, SettingsModal { align: center middle; }
    .dialog-title { text-style: bold; margin-bottom: 1; }
    .hint { color: $text-muted; }
    .field { margin-top: 1; text-style: bold; }
    .err { color: $error; }
    #settings-dialog { height: 90%; }
    #settings-body { height: 1fr; }
    .switch-row { height: auto; margin-top: 1; }
    .switch-row Label { padding: 1 1; }
    /* Texto seleccionado legible en cualquier tema (si no, a veces sale del mismo color que el fondo). */
    Screen > .screen--selection { background: #2f6db5; color: #ffffff; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 2; }
    """

    BINDINGS = [
        Binding("p", "toggle_pause", "Pausa/Reanudar"),
        Binding("P", "pause_for", "Pausa X min", show=False),
        Binding("t", "tick_now", "Tick ya"),
        Binding("r", "review_selected", "Revisar"),
        Binding("R", "review_number", "Revisar #", show=False),
        Binding("i", "ignore_selected", "Ignorar"),
        Binding("I", "ignore_number", "Ignorar #", show=False),
        Binding("f", "since", "Fecha"),
        Binding("c", "settings", "Ajustes"),
        Binding("w", "wizard", "Asistente", show=False),
        Binding("x", "test_claude", "Probar"),
        Binding("h", "toggle_hidden", "Ocultos"),
        Binding("o", "open_selected", "Abrir", show=False),
        Binding("question_mark", "help", "Ayuda"),
        Binding("y", "copy_log", "Copiar log"),
        Binding("q", "quit", "Salir"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.show_all = False
        self.svc: dict[str, Any] = {"installed": False, "active": False, "running": False, "next": None}
        self.log_pos = 0
        self.destination = "main"
        self.rows: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        yield DataTable(id="prs", cursor_type="row", zebra_stripes=True)
        # Log (texto plano) en vez de RichLog: se puede seleccionar con el ratón y copiar.
        yield Log(id="log", max_lines=1000)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#prs", DataTable)
        for col, key, width in (("#", "id", 6), ("Estado", "status", None),
                                ("Autor", "author", 20), ("Título", "title", None),
                                ("Actualizado", "updated", 11), ("Último envío", "last", 14)):
            table.add_column(col, key=key, width=width)
        self.query_one("#log", Log).border_title = "Actividad · ratón para seleccionar, ctrl+c copia, y copia todo"
        table.border_title = "Pull requests"
        self.refresh_state()
        self.refresh_service()
        self.set_interval(2, self.refresh_state)
        self.set_interval(5, self.refresh_service)
        cfg = config.load()
        if not cfg.setup_done:
            self.push_screen(WizardScreen(cfg, first_run=True), self._wizard_done)

    def _wizard_done(self, done: bool | None) -> None:
        self.rows = []
        self.refresh_service()

    def action_wizard(self) -> None:
        self.push_screen(WizardScreen(config.load(), first_run=False), self._wizard_done)

    # ── refresco ──
    @work(thread=True, exclusive=True, group="svc")
    def refresh_service(self) -> None:
        st = service.status()
        self.call_from_thread(self._set_svc, st)

    def _set_svc(self, st: dict[str, Any]) -> None:
        self.svc = st
        try:
            if actions.resume_if_expired():
                self.notify("La pausa programada ya había terminado: reanudado", title="Pausa")
        except Exception as e:  # noqa: BLE001
            self.notify(f"No pude reanudar: {e}", severity="error")
        self.refresh_state()

    def refresh_state(self) -> None:
        try:
            s = state.load()
            cfg = config.load()
        except Exception as e:  # noqa: BLE001 - estado a medio escribir: se reintenta en 2s
            self.notify(f"No pude leer el estado: {e}", severity="warning")
            return
        self._render_status(s, cfg)
        self._render_table(s)
        self._render_log()

    def _render_status(self, s: dict[str, Any], cfg: Config) -> None:
        sv = self.svc
        t = Text()
        t.append("Estado ", style="dim")
        if state.is_paused(s):
            hasta = f" hasta {local(s['paused_until'], '%H:%M')}" if s.get("paused_until") else ""
            t.append(f"⏸ EN PAUSA{hasta}", style="bold yellow")
            t.append("  (t lanza un tick manual)", style="dim")
        elif sv.get("active"):
            t.append("▶ vigilando", style="bold green")
            t.append("   Próximo ", style="dim")
            t.append(until(sv.get("next")))
        else:
            t.append("○ timer parado · p para arrancarlo", style="bold red")
        if sv.get("running") or tick.tick_running():
            t.append("   ⟳ tick en curso", style="bold cyan")
        lt = s.get("last_tick") or {}
        t.append("\nÚltimo tick ", style="dim")
        t.append(f"{local(lt.get('at'), '%H:%M:%S')} ({ago(lt.get('at'))})")
        t.append("   Ventana ", style="dim")
        t.append(f"{cfg.lookback_hours} h" if cfg.lookback_hours else "todos los abiertos")
        if cfg.since:
            t.append("   Desde ", style="dim")
            t.append(cfg.since)
        t.append("   Cada ", style="dim")
        t.append(f"{cfg.interval_seconds}s")
        t.append("\nClaude ", style="dim")
        t.append(f"{cfg.claude_command}", style="bold")
        t.append(f" [{cfg.claude_shell}{', ' + cfg.claude_model if cfg.claude_model else ''}]", style="dim")
        if s.get("forced"):
            t.append("   En cola manual ", style="dim")
            t.append(", ".join(f"#{x}" for x in s["forced"]), style="cyan")
        ls = tick.last_success(s)
        t.append("\nÚltimo DM ", style="dim")
        if ls:
            t.append(f"✓ PR {ls['pr']} · {local(ls['at'], '%d/%m %H:%M')} ({ago(ls['at'])})", style="green")
            if ls.get("link"):
                t.append(f"  {safe(ls['link'], 120)}", style="dim")
        else:
            t.append("ninguno todavía", style="yellow")
        level, text = actions.health(s, sv, cfg)
        t.append("\nSalud ", style="dim")
        t.append(_health_label(level, text),
                 style={"ok": "green", "warn": "yellow", "error": "bold red"}[level])
        self.query_one("#status", Static).update(t)

    def _render_table(self, s: dict[str, Any]) -> None:
        self.destination = config.load().destination_branch
        table = self.query_one("#prs", DataTable)
        recs = [r for r in s["prs"].values() if "id" in r]
        if not self.show_all:
            recs = [r for r in recs if r.get("status") not in HIDDEN_BY_DEFAULT]

        def order(r: dict[str, Any]) -> tuple[int, int]:
            st = r.get("status", "")
            return (STATUS_ORDER.index(st) if st in STATUS_ORDER else 99, -int(r["id"]))

        recs.sort(key=order)
        keys = [str(r["id"]) for r in recs]
        selected = self.selected_key()
        if keys != self.rows:
            table.clear()
            for r in recs:
                table.add_row(*self._cells(r), key=str(r["id"]))
            self.rows = keys
            if selected in keys:
                table.move_cursor(row=keys.index(selected))
        else:
            for r in recs:
                for col, val in zip(("id", "status", "author", "title", "updated", "last"),
                                    self._cells(r)):
                    table.update_cell(str(r["id"]), col, val)
        hidden = len(s["prs"]) - len(recs)
        table.border_subtitle = (f"{len(recs)} visibles · {hidden} ocultos (h)" if not self.show_all
                                 else f"{len(recs)} en total · h para ocultar cerrados/ignorados")

    def _cells(self, r: dict[str, Any]) -> tuple:
        st = r.get("status") or "-"
        if st == "falló":
            n = r.get("fail_count", 0)
            st = "falló (sin reintentos)" if n >= tick.MAX_FAILS_PER_HEAD else f"falló {n}/{tick.MAX_FAILS_PER_HEAD}"
        style = STATUS_STYLE.get(r.get("status") or "", "")
        dest = r.get("destination")
        if dest and dest != self.destination:
            # Revisado a mano hacia otra rama: el escaneo automático no lo sigue ni lo reintenta.
            st = f"{st} · →{safe(dest, 12)} (no se sigue)"
        reviews = r.get("reviews") or []
        last = reviews[-1] if reviews else None
        last_txt = Text("-", style="dim")
        if last:
            v = {"approve": " aprobar", "request_changes": " cambios"}.get(last.get("verdict") or "", "")
            n = f"({last['findings']})" if last.get("findings") else ""
            pub = last.get("published") or {}
            if last.get("unchanged"):
                v, n = " sin cambios", ""
            mark = " BB" if pub.get("comment") else ""
            if pub.get("errors") or pub.get("blocked"):
                mark += "!"
            last_txt = Text(f"{'✓' if last.get('ok') else '✗'}{v}{n}{mark} {local(last.get('at'))}",
                            style=("yellow" if pub.get("errors") or pub.get("blocked") else "green") if last.get("ok") else "red")
        return (
            Text(str(r["id"]), style="bold"),
            Text(st, style=style),
            Text(safe(r.get("author"), 20)),
            Text(safe(r.get("title"))),
            Text(local(r.get("updated_on"))),
            last_txt,
        )

    def _render_log(self) -> None:
        log = self.query_one("#log", Log)
        first = self.log_pos == 0
        events, self.log_pos, rotated = state.read_events_since(self.log_pos)
        if rotated:
            log.clear()
        if first:
            events = events[-300:]
        if events:
            log.write_lines([_event_line(ev) for ev in events])

    # ── utilidades ──
    def selected_key(self) -> str | None:
        table = self.query_one("#prs", DataTable)
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            return None

    def _need_selection(self) -> int | None:
        key = self.selected_key()
        if key is None:
            self.notify("No hay ningún PR seleccionado", severity="warning")
            return None
        return int(key)

    @work(thread=True)
    def do_action(self, fn: Callable[[], str], label: str = "") -> None:
        try:
            msg = fn()
            self.call_from_thread(self.notify, msg, title=label or None)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self.notify, str(e), title=label or "Error", severity="error",
                                  timeout=10)
        self.call_from_thread(self.refresh_service)

    # ── acciones ──
    def action_toggle_pause(self) -> None:
        # Reanudar también si el timer está parado sin pausa (p. ej. tras `prwatch remove-timer`).
        if state.is_paused(state.load()) or not self.svc.get("active"):
            self.do_action(actions.resume, "Reanudar")
        else:
            self.do_action(lambda: actions.pause(None), "Pausa")

    def action_pause_for(self) -> None:
        def done(v: str | None) -> None:
            if v is not None:
                self.do_action(lambda: actions.pause(int(v) if v else None), "Pausa")
        self.push_screen(AskModal("Pausar", "Minutos de pausa (vacío = hasta que reanudes)",
                                  "60", _minutes_or_empty), done)

    def action_tick_now(self) -> None:
        self.do_action(service.run_now, "Tick")

    def action_review_selected(self) -> None:
        pr = self._need_selection()
        if pr is not None:
            self.do_action(lambda: actions.review(pr), "Revisión manual")

    def action_review_number(self) -> None:
        def done(v: str | None) -> None:
            if v:
                n = int(v.lstrip("#"))
                self.do_action(lambda: actions.review(n), "Revisión manual")
        self.push_screen(AskModal("Revisar un PR concreto", "Número del PR (se revisa ya, aunque esté en pausa)",
                                  validate=_pr_number), done)

    def action_ignore_selected(self) -> None:
        pr = self._need_selection()
        if pr is not None:
            self.do_action(lambda: actions.toggle_ignore(pr), "Ignorar")

    def action_ignore_number(self) -> None:
        def done(v: str | None) -> None:
            if v:
                n = int(v.lstrip("#"))
                self.do_action(lambda: actions.toggle_ignore(n), "Ignorar")
        self.push_screen(AskModal("Ignorar / dejar de ignorar", "Número del PR", validate=_pr_number), done)

    def action_since(self) -> None:
        def done(v: str | None) -> None:
            if v is not None:
                self.do_action(lambda: actions.set_since(v or None), "Filtro de fecha")
        self.push_screen(AskModal("Solo PRs creados desde…", "AAAA-MM-DD · vacío para quitar el filtro",
                                  config.load().since, _date_or_empty), done)

    def action_settings(self) -> None:
        def done(new: Config | None) -> None:
            if new is None:
                return
            def save() -> str:
                config.save(new)
                service.apply_config(new)
                state.log("Ajustes guardados")
                return "Ajustes guardados"
            self.do_action(save, "Ajustes")
        self.push_screen(SettingsModal(config.load()), done)

    def action_test_claude(self) -> None:
        self.notify("Lanzando Claude con un DM de prueba… (puede tardar un minuto)", title="Probar")
        self.do_action(actions.test_claude, "Probar")

    def action_toggle_hidden(self) -> None:
        self.show_all = not self.show_all
        self.rows = []
        self.refresh_state()

    def action_detail(self) -> None:
        key = self.selected_key()
        if key:
            rec = state.load()["prs"].get(key)
            if rec:
                self.push_screen(DetailModal(rec))

    @on(DataTable.RowSelected)
    def _row_selected(self, _: DataTable.RowSelected) -> None:
        self.action_detail()

    def action_open_selected(self) -> None:
        key = self.selected_key()
        url = (state.load()["prs"].get(key) or {}).get("url") if key else None
        if url:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)

    def action_copy_log(self) -> None:
        lines = [_event_line(ev) for ev in state.tail_events(200)]
        self.copy_to_clipboard("\n".join(lines) + "\n")
        self.notify(f"{len(lines)} líneas de actividad copiadas al portapapeles", title="Copiado")

    def action_help(self) -> None:
        self.notify(
            "p pausa/reanudar (para el timer) · P pausa X min · t tick manual (también en pausa)\n"
            "r revisar seleccionado · R revisar por número · i ignorar seleccionado · I ignorar por número\n"
            "f fecha mínima · c ajustes · w asistente · x DM de prueba · h ver ocultos · enter detalle · o abrir\n"
            f"Prompt editable: {paths.prompt_file()}",
            title="Atajos", timeout=15,
        )


def _event_line(ev: dict[str, Any]) -> str:
    mark = LEVEL_MARK.get(ev.get("level", "info"), "  ")
    return f"{local(ev.get('at'), '%d/%m %H:%M:%S')}  {mark}{safe(ev.get('msg', ''), 600)}"


def _health_label(level: str, text: str) -> str:
    """Salud solo dice si hay problema; el detalle está en Actividad (o en journalctl si es del servicio)."""
    if level == "ok":
        return "● sin errores"
    if text.startswith("el servicio falló"):
        return "✖ el servicio falló · journalctl --user -u pr-watcher -n 50"
    if text.startswith("el timer está activo"):
        return "✖ no hay ticks recientes (¿sesión cerrada?)"
    if text.startswith("timer activo; esperando"):
        return "▲ esperando el primer tick"
    if level == "warn" and "usuario de Bitbucket" in text:
        return "▲ no sé quién eres en Bitbucket · Ajustes (c) → Mi nickname"
    return "✖ con errores · detalle en Actividad" if level == "error" else "▲ aviso · detalle en Actividad"


def run_tui() -> None:
    import os
    os.umask(0o077)
    paths.ensure_dirs()
    config.load()
    WatcherApp().run()
