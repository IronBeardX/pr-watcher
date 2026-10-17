"""Configuración del watcher (JSON editable a mano o desde la TUI)."""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path

from prwatch import paths

# Sin valores por defecto ligados a un proyecto concreto: el asistente pide carpeta, workspace y repo.
DEFAULT_PROJECT_DIR = ""
DEFAULT_ENV_FILE = ""  # vacío = <project_dir>/.env
MAX_LOOKBACK_HOURS = 24 * 90

# Modos de lanzar Claude:
#   zsh-interactive → `zsh -i -c` (carga ~/.zshrc, así funcionan los alias de claude)
#   zsh             → `zsh -c` (sin .zshrc; el comando debe ser un ejecutable o una función exportada)
#   direct          → sin shell; el comando se parte con shlex
CLAUDE_SHELLS = ("zsh-interactive", "zsh", "direct")
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
TOOL_RE = re.compile(r"^mcp__[A-Za-z0-9_-]+?__[A-Za-z0-9_-]+$")
ATLASSIAN = "mcp__claude_ai_Atlassian_MCP"
SLACK = "mcp__claude_ai_Slack"
DEFAULT_REVIEW_CHANNEL = ""  # id del canal (C…) donde se anuncian los PRs
# Para publicar: escribir en Bitbucket (el guardián solo deja comentar/aprobar/pedir cambios en el PR en
# revisión), leer el canal de PRs (para encontrar el anuncio) y reaccionar en él.
PUBLISH_TOOLS = (
    f"{ATLASSIAN}__executeWrite",
    f"{SLACK}__slack_read_channel",
    f"{SLACK}__slack_read_thread",
    f"{SLACK}__slack_add_reaction",
)
DEFAULT_TOOLS = (
    f"{SLACK}__slack_send_message",
    # Solo lectura: el guardián además filtra qué operaciones de executeRead se permiten.
    f"{ATLASSIAN}__executeRead",
    f"{ATLASSIAN}__discover",
    f"{ATLASSIAN}__getJiraIssue",
    f"{ATLASSIAN}__getAccessibleAtlassianResources",
) + PUBLISH_TOOLS


@dataclass
class Config:
    setup_done: bool = False
    # Ventana: el watcher pide a Bitbucket los PRs actualizados en las últimas N horas (0 = todos los abiertos).
    lookback_hours: int = 24
    # Repo local del proyecto (para cuando Claude revise código; hoy solo se guarda y se valida).
    project_dir: str = DEFAULT_PROJECT_DIR
    # Skill de revisión: <project_dir>/.claude/skills/<review_skill>/SKILL.md (+ references/*.md).
    review_skill: str = "personal-pr-review"
    bitbucket_workspace: str = ""
    bitbucket_repo: str = ""
    destination_branch: str = "main"
    bitbucket_env_file: str = DEFAULT_ENV_FILE
    # Por defecto solo se revisan los PRs de los demás. Tu identidad sale de /user (permiso read:user) o de
    # este nombre (nickname o nombre visible en Bitbucket) si el token no tiene ese permiso.
    skip_my_prs: bool = True
    my_bitbucket_nickname: str = ""
    ignored_authors: list[str] = field(default_factory=list)
    skip_drafts: bool = True
    since: str = ""
    review_existing_on_first_run: bool = False
    interval_seconds: int = 180
    settle_minutes: int = 2
    max_reviews_per_tick: int = 3
    claude_command: str = "claude"
    claude_shell: str = "zsh-interactive"
    claude_model: str = "opus"
    claude_max_turns: int = 40
    claude_args: list[str] = field(default_factory=list)
    # Nombres EXACTOS de herramienta (servidor + herramienta). Nunca un prefijo de servidor entero.
    claude_allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    claude_timeout_seconds: int = 1200
    slack_user_id: str = ""
    # Publicar la revisión: comentario + Approve/Request changes en el PR, y lo mismo en el hilo del anuncio
    # del PR en el canal de Slack. Apagado = solo DM a ti mismo.
    post_to_bitbucket: bool = True
    slack_review_channel: str = DEFAULT_REVIEW_CHANNEL
    slack_reactions: bool = True
    # Proyectos de Jira cuyos tickets puede leer Claude (getJiraIssue); el resto se bloquea. Vacío = ninguno.
    jira_projects: list[str] = field(default_factory=list)
    keep_run_files: int = 200

    @property
    def env_path(self) -> Path:
        if self.bitbucket_env_file:
            return Path(self.bitbucket_env_file).expanduser()
        return self.project_path / ".env"

    @property
    def project_path(self) -> Path:
        return Path(self.project_dir).expanduser()

    @property
    def since_date(self) -> date | None:
        return date.fromisoformat(self.since) if self.since else None

    def validate(self) -> list[str]:
        errors = []
        if self.since:
            try:
                date.fromisoformat(self.since)
            except ValueError:
                errors.append(f"since debe ser AAAA-MM-DD, no {self.since!r}")
        if self.claude_shell not in CLAUDE_SHELLS:
            errors.append(f"claude_shell debe ser uno de {', '.join(CLAUDE_SHELLS)}")
        if self.interval_seconds < 30:
            errors.append("interval_seconds mínimo 30")
        if self.max_reviews_per_tick < 1:
            errors.append("max_reviews_per_tick mínimo 1")
        if not self.claude_command.strip():
            errors.append("claude_command no puede estar vacío")
        elif self.claude_shell == "direct":
            try:
                shlex.split(self.claude_command)
            except ValueError as e:
                errors.append(f"claude_command no se puede interpretar en modo direct ({e})")
        for t in self.claude_allowed_tools:
            if not TOOL_RE.match(t):
                errors.append(f"herramienta {t!r} no es un nombre exacto mcp__<servidor>__<herramienta>")
        if self.slack_user_id and not re.fullmatch(r"[UW][A-Z0-9]{5,}", self.slack_user_id):
            errors.append("slack_user_id debe ser un id de usuario de Slack (U…)")
        if self.slack_review_channel and not re.fullmatch(r"[CG][A-Z0-9]{5,}", self.slack_review_channel):
            errors.append("slack_review_channel debe ser un id de canal de Slack (C…)")
        if not 1 <= self.claude_max_turns <= 50:
            errors.append("claude_max_turns entre 1 y 50")
        if not 0 <= self.lookback_hours <= MAX_LOOKBACK_HOURS:
            errors.append(f"lookback_hours entre 0 y {MAX_LOOKBACK_HOURS}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,80}", self.review_skill or ""):
            errors.append("review_skill debe ser un nombre de skill (minúsculas, números, - y _)")
        if self.settle_minutes < 0 or self.keep_run_files < 10:
            errors.append("settle_minutes ≥ 0 y keep_run_files ≥ 10")
        for name in ("bitbucket_workspace", "bitbucket_repo"):
            v = getattr(self, name)
            if v and not SLUG_RE.fullmatch(v):
                errors.append(f"{name} solo admite letras, números, '.', '-' y '_'")
            elif self.setup_done and not v:
                errors.append(f"falta {name}")
        if self.setup_done and not self.project_dir.strip():
            errors.append("falta project_dir (la carpeta del repo local)")
        if self.setup_done and self.post_to_bitbucket and not self.slack_review_channel:
            errors.append("para publicar en Bitbucket y en el hilo hace falta slack_review_channel")
        for k in self.jira_projects:
            if not re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", k):
                errors.append(f"proyecto de Jira {k!r} no es una clave válida (p. ej. ABC)")
        return errors


def load() -> Config:
    p = paths.config_file()
    if not p.exists():
        cfg = Config()
        save(cfg)
        return cfg
    try:
        raw = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"config.json corrupto ({e}): {p}") from e
    known = {f.name for f in fields(Config)}
    cfg = Config(**{k: v for k, v in raw.items() if k in known})
    if "review_skill" not in raw:
        # Migración a la versión que revisa PRs: más turnos/tiempo, Opus y herramientas de lectura de Atlassian.
        cfg.claude_max_turns = max(cfg.claude_max_turns, 40)
        cfg.claude_timeout_seconds = max(cfg.claude_timeout_seconds, 1200)
        cfg.claude_model = cfg.claude_model or "opus"
        for t in DEFAULT_TOOLS:
            if t not in cfg.claude_allowed_tools:
                cfg.claude_allowed_tools.append(t)
        save(cfg)
    if "post_to_bitbucket" not in raw:
        # Migración a la versión que publica en Bitbucket y en el hilo del PR.
        for t in PUBLISH_TOOLS:
            if t not in cfg.claude_allowed_tools:
                cfg.claude_allowed_tools.append(t)
        save(cfg)
    return cfg


def skill_dir(cfg: Config) -> Path:
    return cfg.project_path / ".claude" / "skills" / cfg.review_skill


def save(cfg: Config) -> None:
    errors = cfg.validate()
    if errors:
        raise ValueError("; ".join(errors))
    paths.ensure_dirs()
    paths.write_private(paths.config_file(), json.dumps(asdict(cfg), indent=2, ensure_ascii=False) + "\n")


def read_env_file(path: Path) -> dict[str, str]:
    """Lee solo pares CLAVE=valor, sin ejecutar nada (no se hace `source` del archivo)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out
