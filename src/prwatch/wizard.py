"""Asistente de primera configuración (se abre solo si config.setup_done es False, o con la tecla w)."""
from __future__ import annotations

import re
from dataclasses import replace

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, ContentSwitcher, Input, Label, Select, Static, Switch

from prwatch import actions, claude, config, service, state
from prwatch.config import CLAUDE_SHELLS, MAX_LOOKBACK_HOURS, Config, read_env_file
from prwatch.text import strip_ctrl
from prwatch.widgets import ToolList

STEPS = [
    ("bienvenida", "Inicio"),
    ("claude", "Claude"),
    ("ventana", "Ventana"),
    ("slack", "Slack"),
    ("proyecto", "Proyecto"),
    ("resumen", "Resumen"),
]


def _hours_text(h: int) -> str:
    if h == 0:
        return "todos los PRs abiertos (sin ventana)"
    if h % 24 == 0:
        d = h // 24
        return f"las últimas {h} h ({d} día{'s' if d > 1 else ''})"
    return f"las últimas {h} h"


class WizardScreen(Screen[bool]):
    """Devuelve True si se completó, False si se canceló."""

    BINDINGS = [Binding("escape", "cancel", "Salir del asistente")]

    CSS = """
    WizardScreen { align: center middle; }
    #wiz { width: 96; max-width: 100%; height: 90%; border: thick $primary; padding: 1 2;
           background: $surface; }
    #wiz-steps { height: auto; margin-bottom: 1; }
    #wiz-title { text-style: bold; height: auto; margin-bottom: 1; }
    #wiz-body { height: 1fr; }
    #wiz-body > Vertical { height: auto; }
    #wiz Label { width: 100%; }
    .hint { color: $text-muted; }
    .field { margin-top: 1; text-style: bold; }
    .check { margin-top: 1; height: auto; }
    .ok { color: $success; }
    .bad { color: $error; }
    #wiz-err { color: $error; height: auto; }
    #wiz-nav { height: auto; margin-top: 1; }
    #wiz-nav Button { margin-right: 2; }
    .switch-row { height: auto; margin-top: 1; }
    .switch-row Label { padding: 1 1; }
    #w-workspace, #w-repo { width: 1fr; }
    """

    def __init__(self, cfg: Config, first_run: bool):
        super().__init__()
        self.cfg = replace(cfg)
        self.original = replace(cfg)
        self.saved_for_test = False
        self.first_run = first_run
        self.step = 0

    # ── composición ──
    def compose(self) -> ComposeResult:
        c = self.cfg
        with Vertical(id="wiz"):
            yield Static(id="wiz-steps")
            yield Label("", id="wiz-title")
            with VerticalScroll(id="wiz-body"):
                with ContentSwitcher(initial="bienvenida", id="wiz-switch"):
                    with Vertical(id="bienvenida"):
                        yield Static(Text(
                            "pr-watcher vigila los pull requests de Bitbucket sin gastar tokens y solo "
                            "llama a Claude cuando hay un PR nuevo, con commits nuevos o que pasa de draft a ready.\n\n"
                            "Este asistente configura lo imprescindible:\n"
                            "  1. El comando con el que se lanza Claude (tu cuenta de trabajo).\n"
                            "  2. La ventana de tiempo en la que se buscan PRs.\n"
                            "  3. Slack: tu usuario (DMs) y el canal donde se anuncian los PRs.\n"
                            "  4. La carpeta del proyecto (y de ahí, las credenciales de Bitbucket).\n\n"
                            "Todo se puede cambiar después en Ajustes (tecla c) o repitiendo el asistente (tecla w)."))
                    with Vertical(id="claude"):
                        yield Label("Comando para lanzar Claude", classes="field")
                        yield Label("El alias o ejecutable de la cuenta que tiene los conectores de trabajo.",
                                    classes="hint")
                        yield Input(c.claude_command, id="w-cmd", placeholder="claude")
                        yield Label("Cómo se lanza", classes="field")
                        yield Label("zsh-interactive carga tu ~/.zshrc: necesario si el comando es un alias.",
                                    classes="hint")
                        yield Select([(s, s) for s in CLAUDE_SHELLS], value=c.claude_shell, allow_blank=False,
                                     id="w-shell")
                        yield Button("Comprobar comando (no gasta tokens)", id="w-check-cmd", classes="check")
                        yield Label("", id="w-cmd-result")
                    with Vertical(id="ventana"):
                        yield Label("Ventana de búsqueda (horas)", classes="field")
                        yield Label("El watcher pide a Bitbucket los PRs con actividad entre (ahora − ventana) y "
                                    "ahora. Ejemplos: 4, 24 (1 día), 168 (1 semana). 0 = todos los abiertos.",
                                    classes="hint")
                        yield Input(str(c.lookback_hours), id="w-hours", type="integer")
                        yield Label("", id="w-hours-preview", classes="hint")
                        yield Label("Cada cuántos minutos consulta", classes="field")
                        yield Label("Consultar no gasta tokens; solo es una llamada a la API de Bitbucket.",
                                    classes="hint")
                        yield Input(str(max(1, c.interval_seconds // 60)), id="w-every", type="integer")
                    with Vertical(id="slack"):
                        yield Label("Tu usuario de Slack", classes="field")
                        yield Label("Slack → tu foto → Perfil → ⋮ → Copiar ID de miembro (empieza por U). "
                                    "Es el único destino al que el guardián deja escribir.", classes="hint")
                        yield Input(c.slack_user_id, id="w-slack", placeholder="U0123ABCDEF")
                        yield Label("Canal de anuncios de PRs", classes="field")
                        yield Label("Id del canal (C…) donde el equipo anuncia los PRs. La revisión se publica en el "
                                    "hilo del anuncio.", classes="hint")
                        yield Input(c.slack_review_channel, id="w-channel", placeholder="C0123ABCDEF")
                        with Horizontal(classes="switch-row"):
                            yield Switch(value=c.post_to_bitbucket, id="w-post")
                            yield Label("Publicar en Bitbucket y en el hilo del PR (apagado = solo DM a mí)")
                        yield Label("Herramientas que puede usar Claude", classes="field")
                        yield Label("Nombres exactos; los ves ejecutando /mcp dentro de tu comando de Claude.",
                                    classes="hint")
                        yield ToolList(c.claude_allowed_tools, id="w-tools")
                    with Vertical(id="proyecto"):
                        yield Label("Carpeta del proyecto", classes="field")
                        yield Label("El clon local del repo que vas a vigilar. Se usará cuando Claude revise código; "
                                    "ahora sirve para encontrar el .env con BB_EMAIL y BB_TOKEN.", classes="hint")
                        yield Input(c.project_dir, id="w-project", placeholder="~/proyectos/mi-repo")
                        yield Label("", id="w-project-info")
                        yield Label("Repositorio en Bitbucket", classes="field")
                        yield Label("Workspace y repo tal como salen en la URL: bitbucket.org/<workspace>/<repo>.",
                                    classes="hint")
                        with Horizontal(classes="switch-row"):
                            yield Input(c.bitbucket_workspace, id="w-workspace", placeholder="mi-workspace")
                            yield Input(c.bitbucket_repo, id="w-repo", placeholder="mi-repo")
                        yield Label("Proyectos de Jira (opcional)", classes="field")
                        yield Label("Claves separadas por comas (p. ej. ABC, OPS). Claude solo podrá leer tickets de "
                                    "esos proyectos; vacío = ninguno.", classes="hint")
                        yield Input(", ".join(c.jira_projects), id="w-jira", placeholder="ABC")
                        yield Label("Skill de revisión", classes="field")
                        yield Label("Nombre de la carpeta en <proyecto>/.claude/skills/ con la que Claude revisa.",
                                    classes="hint")
                        yield Input(c.review_skill, id="w-skill", placeholder="personal-pr-review")
                        yield Button("Comprobar Bitbucket (no gasta tokens)", id="w-check-bb", classes="check")
                        yield Label("", id="w-bb-result")
                        yield Label("Tu nombre en Bitbucket", classes="field")
                        yield Label("Para reconocer tus PRs e ignorarlos. Se rellena solo al comprobar Bitbucket "
                                    "si el token tiene el permiso read:user; si no, escribe tu nombre visible.",
                                    classes="hint")
                        yield Input(c.my_bitbucket_nickname, id="w-nick", placeholder="Nombre Apellido")
                        with Horizontal(classes="switch-row"):
                            yield Switch(value=c.skip_my_prs, id="w-skip-mine")
                            yield Label("Ignorar mis propios PRs (solo revisar los de los demás)")
                    with Vertical(id="resumen"):
                        yield Static(id="w-summary")
                        with Horizontal(classes="switch-row"):
                            yield Switch(value=True, id="w-enable")
                            yield Label("Activar el watcher (timer de systemd) al terminar")
                        yield Button("Enviar DM de prueba ahora (gasta unos céntimos)", id="w-test",
                                     classes="check")
                        yield Label("", id="w-test-result")
            yield Label("", id="wiz-err")
            with Horizontal(id="wiz-nav"):
                yield Button("← Atrás", id="w-back")
                yield Button("Siguiente →", variant="primary", id="w-next")

    def on_mount(self) -> None:
        self._show()

    # ── navegación ──
    def _show(self) -> None:
        key, title = STEPS[self.step]
        self.query_one("#wiz-switch", ContentSwitcher).current = key
        t = Text()
        for i, (_, name) in enumerate(STEPS):
            style = "bold reverse" if i == self.step else ("green" if i < self.step else "dim")
            t.append(f" {i + 1}. {name} ", style=style)
            if i < len(STEPS) - 1:
                t.append(" ")
        self.query_one("#wiz-steps", Static).update(t)
        self.query_one("#wiz-title", Label).update(f"Paso {self.step + 1} de {len(STEPS)} · {title}")
        self.query_one("#w-back", Button).disabled = self.step == 0
        last = self.step == len(STEPS) - 1
        self.query_one("#w-next", Button).label = "Terminar ✓" if last else "Siguiente →"
        self.query_one("#wiz-err", Label).update("")
        if key == "ventana":
            self._preview_hours()
        if key == "proyecto":
            self._project_info()
        if key == "resumen":
            self._summary()

    @on(Button.Pressed, "#w-back")
    def back(self) -> None:
        if self.step > 0:
            self.step -= 1
            self._show()

    @on(Button.Pressed, "#w-next")
    def next(self) -> None:
        err = self._collect(STEPS[self.step][0])
        if err:
            self.query_one("#wiz-err", Label).update(err)
            return
        if self.step < len(STEPS) - 1:
            self.step += 1
            self._show()
        else:
            self._finish()

    def action_cancel(self) -> None:
        if self.saved_for_test:
            # La prueba de DM guardó la config del asistente; al cancelar se vuelve a la anterior.
            try:
                config.save(self.original)
            except ValueError:
                pass
        if self.first_run:
            self.app.exit(message="Asistente sin terminar: vuelve a ejecutar prwatch para completarlo.")
        else:
            self.dismiss(False)

    # ── recogida y validación por paso ──
    def _val(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _collect(self, key: str) -> str | None:
        c = self.cfg
        if key == "claude":
            c.claude_command = self._val("w-cmd")
            c.claude_shell = self.query_one("#w-shell", Select).value
            if not c.claude_command:
                return "Escribe el comando de Claude"
        elif key == "ventana":
            try:
                c.lookback_hours = int(self._val("w-hours") or "0")
                every = int(self._val("w-every") or "0")
            except ValueError:
                return "Escribe números enteros"
            if not 0 <= c.lookback_hours <= MAX_LOOKBACK_HOURS:
                return f"La ventana va de 0 a {MAX_LOOKBACK_HOURS} horas"
            if every < 1:
                return "Consulta como mínimo cada minuto"
            c.interval_seconds = max(30, every * 60)
        elif key == "slack":
            c.slack_user_id = self._val("w-slack").upper()
            c.claude_allowed_tools = list(self.query_one("#w-tools", ToolList).tools)
            c.slack_review_channel = self._val("w-channel").upper()
            c.post_to_bitbucket = self.query_one("#w-post", Switch).value
            if not re.fullmatch(r"[UW][A-Z0-9]{5,}", c.slack_user_id):
                return "El id de Slack empieza por U y solo tiene letras mayúsculas y números"
            if not c.claude_allowed_tools:
                return "Hace falta al menos la herramienta de enviar mensajes"
        elif key == "proyecto":
            c.project_dir = self._val("w-project")
            p = c.project_path
            if not c.project_dir:
                return "Escribe la carpeta del proyecto"
            if not p.is_dir():
                return f"No existe la carpeta {p}"
            if (p / ".env").exists():
                c.bitbucket_env_file = str(p / ".env")
            c.bitbucket_workspace = self._val("w-workspace")
            c.bitbucket_repo = self._val("w-repo")
            if not c.bitbucket_workspace or not c.bitbucket_repo:
                return "Escribe el workspace y el repo de Bitbucket"
            c.jira_projects = [k.strip().upper() for k in self._val("w-jira").split(",") if k.strip()]
            c.my_bitbucket_nickname = self._val("w-nick")
            c.review_skill = self._val("w-skill")
            if not (config.skill_dir(c) / "SKILL.md").is_file():
                return f"No encuentro la skill: {config.skill_dir(c) / 'SKILL.md'}"
            c.skip_my_prs = self.query_one("#w-skip-mine", Switch).value
        errors = c.validate()
        return " · ".join(errors) if errors else None

    # ── ayudas de cada paso ──
    @on(Input.Changed, "#w-hours")
    def _preview_hours(self) -> None:
        try:
            h = int(self._val("w-hours") or "0")
            self.query_one("#w-hours-preview", Label).update(f"Se buscarán PRs con actividad en {_hours_text(h)}.")
        except ValueError:
            self.query_one("#w-hours-preview", Label).update("")

    @on(Input.Changed, "#w-project")
    def _project_info(self) -> None:
        p = config.Config(project_dir=self._val("w-project")).project_path
        lbl = self.query_one("#w-project-info", Label)
        if not self._val("w-project"):
            lbl.update(Text("▲ escribe la carpeta del repo", style="yellow"))
            return
        if not p.is_dir():
            lbl.update(Text("✗ la carpeta no existe", style="red"))
            return
        t = Text()
        t.append("✓ carpeta encontrada" if (p / ".git").exists() else "▲ existe, pero no parece un repo git",
                 style="green" if (p / ".git").exists() else "yellow")
        env = read_env_file(p / ".env")
        has = bool(env.get("BB_EMAIL") and env.get("BB_TOKEN"))
        t.append("\n")
        t.append(f"✓ .env con BB_EMAIL y BB_TOKEN ({p / '.env'})" if has
                 else "▲ no encontré BB_EMAIL/BB_TOKEN en su .env (configúralo luego en Ajustes)",
                 style="green" if has else "yellow")
        lbl.update(t)

    def _summary(self) -> None:
        c = self.cfg
        t = Text()
        rows = [
            ("Claude", f"{c.claude_command}  [{c.claude_shell}]"),
            ("Ventana", f"PRs con actividad en {_hours_text(c.lookback_hours)}"),
            ("Consulta", f"cada {c.interval_seconds // 60} min (sin tokens)"),
            ("Publicar", f"Bitbucket + hilo en {c.slack_review_channel}" if c.post_to_bitbucket
             else f"solo DM a {c.slack_user_id}"),
            ("Herramientas", ", ".join(c.claude_allowed_tools)),
            ("Proyecto", str(c.project_path)),
            ("Revisión", f"skill {c.review_skill} · modelo {c.claude_model or 'el de la cuenta'}"),
            ("Bitbucket", f"{c.bitbucket_workspace}/{c.bitbucket_repo} → {c.destination_branch}  (.env: {c.env_path})"),
        ]
        for k, v in rows:
            t.append(f"{k:<13}", style="dim")
            t.append(strip_ctrl(v) + "\n")
        if c.lookback_hours:
            t.append("\nAl activar, los PRs abiertos con actividad en la ventana se mandan a Claude en los "
                     f"próximos ticks (máx {c.max_reviews_per_tick} por tick).", style="yellow")
        self.query_one("#w-summary", Static).update(t)

    # ── comprobaciones (en hilo aparte) ──
    @on(Button.Pressed, "#w-check-cmd")
    def _check_cmd(self) -> None:
        if self._collect("claude") is None:
            self.query_one("#w-cmd-result", Label).update("Comprobando…")
            self._run_check_cmd(replace(self.cfg))

    @work(thread=True, exclusive=True, group="wiz-cmd")
    def _run_check_cmd(self, cfg: Config) -> None:
        try:
            ok, text = claude.check_command(cfg)
        except Exception as e:  # noqa: BLE001 - nunca tumbar la TUI por una comprobación
            ok, text = False, f"error comprobando: {e}"
        msg = Text(("✓ " if ok else "✗ ") + text, style="green" if ok else "red")
        if not ok and cfg.claude_shell != "zsh-interactive":
            msg.append("\n¿Es un alias? Prueba el modo zsh-interactive.", style="yellow")
        self.app.call_from_thread(self.query_one("#w-cmd-result", Label).update, msg)

    @on(Button.Pressed, "#w-check-bb")
    def _check_bb(self) -> None:
        err = self._collect("proyecto")
        if err:
            self.query_one("#w-bb-result", Label).update(Text(err, style="red"))
            return
        self.query_one("#w-bb-result", Label).update("Comprobando…")
        self._run_check_bb(replace(self.cfg))

    @work(thread=True, exclusive=True, group="wiz-bb")
    def _run_check_bb(self, cfg: Config) -> None:
        from prwatch.bitbucket import BitbucketError, Client
        client = None
        try:
            client = Client(cfg)
            me = client.me()
            name = strip_ctrl(me.get("display_name"), 60)
            msg = Text(f"✓ conectado como {name}", style="green")
            with state.locked() as s:
                s["me"], s["me_retry_at"] = me, None
            self.app.call_from_thread(self._fill_nick, name)
        except BitbucketError as e:
            msg = Text(f"✗ {strip_ctrl(str(e), 300)}", style="red")
            if client is not None and "403" in str(e):
                # /user sin permiso no impide vigilar: probamos lo que de verdad usa el watcher.
                try:
                    client.open_prs()
                    msg = Text("✓ puede leer los PRs. ▲ El token no tiene permiso read:user: escribe abajo tu "
                               "nombre en Bitbucket para que se ignoren tus PRs.", style="yellow")
                except BitbucketError as e2:
                    msg = Text(f"✗ {strip_ctrl(str(e2), 300)}", style="red")
        self.app.call_from_thread(self.query_one("#w-bb-result", Label).update, msg)

    def _fill_nick(self, name: str) -> None:
        inp = self.query_one("#w-nick", Input)
        if not inp.value.strip():
            inp.value = name

    @on(Button.Pressed, "#w-test")
    def _test(self) -> None:
        try:
            config.save(self.cfg)  # la prueba (y el guardián) leen la config del disco
            self.saved_for_test = True
        except ValueError as e:
            self.query_one("#w-test-result", Label).update(Text(str(e), style="red"))
            return
        self.query_one("#w-test-result", Label).update("Lanzando Claude… (puede tardar un minuto)")
        self._run_test()

    @work(thread=True, exclusive=True, group="wiz-test")
    def _run_test(self) -> None:
        try:
            out = actions.test_claude()
            ok = out.startswith("OK")
        except Exception as e:  # noqa: BLE001
            out, ok = str(e), False
        msg = Text(("✓ " if ok else "✗ ") + strip_ctrl(out, 400), style="green" if ok else "red")
        if ok:
            msg.append("\nRevisa tu DM en Slack.", style="green")
        self.app.call_from_thread(self.query_one("#w-test-result", Label).update, msg)

    # ── final ──
    def _finish(self) -> None:
        self.cfg.setup_done = True
        try:
            config.save(self.cfg)
        except ValueError as e:
            self.query_one("#wiz-err", Label).update(str(e))
            return
        state.log("Configuración guardada desde el asistente")
        enable = self.query_one("#w-enable", Switch).value
        self._apply(enable)

    @work(thread=True, exclusive=True, group="wiz-finish")
    def _apply(self, enable: bool) -> None:
        msg, ok = "Configuración guardada", True
        try:
            service.apply_config(self.cfg)
            if enable:
                msg += " · " + actions.enable()
        except Exception as e:  # noqa: BLE001
            msg, ok = f"Guardado, pero no pude activar el timer: {e}", False
        self.app.call_from_thread(self.app.notify, msg, severity="information" if ok else "error")
        self.app.call_from_thread(self.dismiss, True)
