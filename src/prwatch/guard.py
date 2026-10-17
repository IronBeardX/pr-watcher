"""Hook PreToolUse de Claude Code: solo deja pasar las herramientas permitidas y los DMs a ti mismo.

Claude Code lo ejecuta antes de cada herramienta (vía --settings). Exit 2 = bloquear; el motivo va por
stderr y Claude lo ve. Ante cualquier duda, bloquea (fail-closed). Funciona incluso si la cuenta tiene
permisos amplios o bypassPermissions, porque los hooks se evalúan siempre.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Callable

from ._tlds import TLDS

SAFE_BUILTINS = {"ToolSearch"}
MESSAGING = re.compile(r"(send|schedule|post|draft|reply|update)", re.I)
TARGET_KEYS = ("channel_id", "channel")
TEXT_KEYS = ("message", "text")
ALLOWED_MSG_KEYS = set(TARGET_KEYS) | set(TEXT_KEYS)
MAX_REVIEW_MESSAGES = 3
MAX_REVIEW_CHARS = 3900
# Nunca, aunque alguien las añada a la lista: el revisor no escribe en Bitbucket/Jira.
WRITE_TOOL = re.compile(r"^(execute(Write|Destructive)|create|update|add|delete|edit|merge|approve|decline|"
                        r"request|transition|resolve|reopen|set|run|upload|move|publish)", re.I)
# Operaciones de lectura permitidas (lista exacta, no un patrón): todas del repo configurado.
READ_OPS = frozenset({
    "getBitbucketRepoPullRequest", "getBitbucketRepoPullRequestDiff", "listBitbucketRepoPullRequestComments",
    "listBitbucketRepoPullRequestTasks", "getBitbucketRepoFileContent", "getBitbucketRepoCommit",
    "getBitbucketRepoPullRequestMergeability", "listBitbucketRepoCommitReports", "getBitbucketRepoCommitReport",
    "listBitbucketRepoPipelines", "getBitbucketRepoPipeline", "listBitbucketRepoPipelineSteps",
})
SCOPE_KEYS = {"workspace", "repo", "repo_slug", "repository", "slug", "fullname", "full_name", "url", "href",
              "owner", "project"}
URL_RE = re.compile(r"https?://[^\s<>|()\]\[\"'`]+", re.I)
SCHEME_RE = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s<>|()\]\[\"'`]*", re.I)
BARE_SCHEME_RE = re.compile(r"(?i)\b(mailto|tel|sms|callto|slack|javascript|data|file|vbscript):(?=\S)")
DOMAIN_RE = re.compile(r"(?<![\w.-])(?:[a-z0-9_-]+\.)+[a-z0-9-]*[a-z][a-z0-9-]*", re.I)
EMAIL_RE = re.compile(r"[^\s@<>`]+@(?:[a-z0-9_-]+\.)+[a-z0-9-]*[a-z][a-z0-9-]*", re.I)
# Código entre comillas invertidas: Slack no enlaza dentro. Solo cuenta si las comillas no van pegadas a
# letras (a`x`b puede no formatearse como código), y el bloque ``` antes que el `.
CODE_RE = re.compile(r"(?<![\w`\\])```[\s\S]*?```(?![\w`])|(?<![\w`\\])`[^`\n]+`(?![\w`])")


def strip_code(text: str) -> str:
    """Quita los tramos de `código` (Slack no enlaza dentro). Un \\` escapado es una comilla literal, no
    abre código: se neutraliza antes, para que no esconda un dominio."""
    return CODE_RE.sub(" ", re.sub(r"\\+`", "'", text))

def _check_read_op(args: dict, workspace: str, repo: str) -> str | None:
    op = str(args.get("name") or "")
    if op not in READ_OPS:
        return f"operación de lectura no permitida: {op!r}"
    inputs = args.get("inputs")
    if not isinstance(inputs, dict):
        return "inputs con formato inesperado"
    if str(inputs.get("workspaceId")) != workspace or str(inputs.get("repoId")) != repo:
        return f"solo se puede leer {workspace}/{repo} (workspaceId y repoId obligatorios)"
    odd = {k for k in inputs if k.lower() in SCOPE_KEYS}
    if odd:
        return f"argumentos de alcance no permitidos: {', '.join(sorted(odd))}"
    return None


def _check_links(text: str, workspace: str, repo: str) -> str | None:
    """Sin enlaces fuera del repo: Slack convierte en enlace (y puede previsualizar) cualquier URL, dominio o
    email, con o sin esquema, así que un dominio ajeno en la revisión es una vía para sacar datos; y un
    enlace con texto falso en tu propio DM sería phishing. Solo se permiten URLs bajo
    https://bitbucket.org/<ws>/<repo>/. Rutas, identificadores y nombres de archivo van entre `comillas`."""
    hint = (" (pon rutas, archivos e identificadores entre `comillas invertidas` y los tickets por su clave, p. ej. ABC-123; "
            "sin enlaces ni emails)")
    # En todo el texto (también dentro de código, por si Slack lo interpretara igual): enlaces con texto
    # oculto, imágenes, menciones/avisos y <esquema:…>.
    if re.search(r"<[a-z][a-z0-9+.-]*:[^<>\n]*\|", text, re.I) or re.search(r"!\[[^\]\n]*\]\(", text):
        return "enlaces con texto oculto o imágenes no permitidos en la revisión" + hint
    if re.search(r"<[!@]", text):
        return "menciones y avisos de Slack (<@…>, <!channel>, <!here>…) no permitidos en la revisión" + hint
    if re.search(r"<[\w-]+(?:\.[\w-]+)+[^\s<>|]*\|", text):
        return "enlaces con texto oculto no permitidos en la revisión" + hint
    if re.search(r"<[a-z][a-z0-9+.-]*:", text, re.I):
        return "sintaxis de enlace <esquema:…> no permitida en la revisión" + hint
    # Fuera del código: cualquier <…> (HTML en Bitbucket, enlaces en Slack). Dentro de `código` los genéricos
    # de TypeScript o JSX (`Promise<Candidate[]>`, `<CandidateCard>`) son normales.
    prose_only = strip_code(text)
    if re.search(r"<[^<>\n]*\|[^<>\n]*>", prose_only) or "![" in prose_only:
        return "enlaces con texto oculto o imágenes no permitidos en la revisión" + hint
    if re.search(r"<(?!#)[^\s<>]", prose_only):
        return "sintaxis <…> no permitida fuera de `código` en la revisión" + hint
    allowed = f"https://bitbucket.org/{workspace}/{repo}/".lower()
    # Esquemas y URLs: en todo el texto, también dentro de código (conservador).
    for url in SCHEME_RE.findall(text):
        if not url.lower().startswith(allowed):
            return f"enlace no permitido en la revisión: {url[:80]}" + hint
    for url in URL_RE.findall(text):
        if not url.lower().startswith(allowed):
            return f"enlace no permitido en la revisión: {url[:80]}" + hint
    for target in re.findall(r"\]\(([^)\s]*)", text):
        if not target.lower().startswith(allowed):
            return f"enlace markdown no permitido: {target[:80]}" + hint
    rest = SCHEME_RE.sub(" ", text)
    if re.search(r"(?i)\b[a-z][a-z0-9+.-]*:\\\\", rest) or re.search(r"(?<![\w:])//[a-z0-9_-]+\.", rest):
        return "enlace sin esquema no permitido en la revisión" + hint
    # Dominios y emails: fuera del código, que Slack sí enlaza.
    prose = strip_code(rest)
    m = BARE_SCHEME_RE.search(prose)
    if m:
        return f"esquema no permitido en la revisión: {m.group(0)}" + hint
    m = EMAIL_RE.search(prose)
    if m:
        return f"email no permitido en la revisión: {m.group(0)[:60]}" + hint
    for m in DOMAIN_RE.finditer(prose):
        token = m.group(0).lower()
        tld = token.rsplit(".", 1)[-1]
        nxt = prose[m.end():m.end() + 1]
        if (tld in TLDS or tld.startswith("xn--") or token.startswith("www.")
                or (nxt in ("/", "?", "#", ":") and len(tld) >= 2 and tld.isalpha())):
            return f"dominio no permitido en la revisión: {m.group(0)[:60]}" + hint
    return None


WRITE_OPS = {
    # operación -> argumentos permitidos en inputs (todos obligatorios salvo los de alcance ya fijados)
    "addBitbucketRepoPullRequestComment": {"workspaceId", "repoId", "prId", "content"},
    "approveBitbucketRepoPullRequest": {"workspaceId", "repoId", "prId"},
    "requestChangesOnBitbucketRepoPullRequest": {"workspaceId", "repoId", "prId"},
}
STATE_OPS = {"approveBitbucketRepoPullRequest": "approved",
             "requestChangesOnBitbucketRepoPullRequest": "changes_requested"}
EXECUTE_KEYS = {"name", "inputs", "cloudId"}
MAX_COMMENT_CHARS = 20000
VERDICT_RE = re.compile(r"\*\*Verdict:\*\*\s*(Approve|Request Changes)\s*[—–-]+\s*(\d+)\s+findings?", re.I)
REACTIONS = {"eyes": None, "warning": "changes_requested", "white_check_mark": "approved"}
READ_CHANNEL_KEYS = {"channel_id", "limit", "cursor", "oldest", "latest", "response_format"}
READ_THREAD_KEYS = {"channel_id", "message_ts", "limit", "response_format"}  # sin paginar: el padre va primero
# Herramientas sin efectos y sin datos sensibles; todo lo demás necesita reglas propias en este archivo.
HARMLESS = {"discover", "getAccessibleAtlassianResources", "atlassianUserInfo"}
JIRA_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]{1,9})-\d{1,7}$")
TS_RE = re.compile(r"^\d{9,11}\.\d{1,7}$")


@dataclass
class Ctx:
    """Lo que el guardián sabe de esta ejecución (config + entorno + rastro de lo ya hecho)."""
    allowed: list[str]
    slack_user_id: str
    expected: str | None = None       # texto exacto (prueba) o cabecera obligatoria del DM
    mode: str = "exact"               # exact | review (solo DM) | publish (Bitbucket + hilo)
    workspace: str = ""
    repo: str = ""
    pr_id: int = 0
    head: str = ""
    channel: str = ""                 # canal de anuncios de PRs
    jira_projects: frozenset = frozenset()  # vacío = ningún ticket de Jira
    prior_ids: list[str] = field(default_factory=list)  # F-ids abiertos de la revisión anterior
    my_vote: str = ""                 # tu voto en Bitbucket antes de esta ejecución (lo mira el watcher)
    reactions: bool = True
    events: list[dict] = field(default_factory=list)   # rastro de esta ejecución
    done: dict = field(default_factory=dict)           # lo publicado en ejecuciones anteriores, mismo commit
    gate: Callable[[], str | None] | None = None       # comprueba en Bitbucket que el PR sigue abierto y sin moverse

    def posted(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("phase") == "post" and e.get("kind") == kind and e.get("sent")]

    def comment(self) -> dict | None:
        """Veredicto del comentario ya publicado (en esta ejecución o en una anterior del mismo commit)."""
        c = self.posted("bb_comment")
        if c:
            return c[-1]
        return self.done.get("comment") or None

    def state_done(self) -> str:
        s = self.posted("bb_state")
        return s[-1].get("state", "") if s else str(self.done.get("state") or "")

    def effective_vote(self) -> str:
        """El voto que de verdad queda: el marcado ahora o antes, o el que ya tenías si coincide con el
        veredicto publicado (en ese caso no hace falta volver a marcarlo)."""
        done = self.state_done()
        if done:
            return done
        c = self.comment() or {}
        want = {"approve": "approved", "request_changes": "changes_requested"}.get(str(c.get("verdict")), "")
        return self.my_vote if want and self.my_vote == want else ""

    def candidates(self) -> set[str]:
        """Mensajes del canal que enlazan este PR según slack_read_channel (texto: se puede falsificar)."""
        out: set[str] = set()
        for e in self.events:
            if e.get("phase") == "post" and e.get("kind") == "read_channel":
                out.update(str(t) for t in e.get("candidates") or [])
        return out

    def confirmed(self) -> set[str]:
        """Anuncios confirmados: slack_read_thread con ese ts devolvió un mensaje padre que enlaza el PR.
        Slack resuelve el ts, así que un bloque falsificado dentro de otro mensaje no pasa."""
        return {str(e.get("ts")) for e in self.events
                if e.get("phase") == "post" and e.get("kind") == "read_thread" and e.get("confirmed")}


def _prior_ids() -> list[str]:
    return re.findall(r"\bF\d{1,2}\b", os.environ.get("PRWATCH_PRIOR", ""))[:30]


STATUS_RE = re.compile(r"\b(not addressed|partially(?: addressed)?|addressed)\b", re.I)


def _segments(content: str, fid: str | None = None) -> list[str]:
    """Tramos ("F1 … addressed · F2 …" o una línea por hallazgo) que nombran un F-id."""
    out = []
    for ln in content.splitlines():
        for seg in re.split(r"[·;]", ln):
            if re.search(rf"\b{fid}\b" if fid else r"\bF\d{1,2}\b", seg):
                out.append(seg)
    return out


def _status(seg: str) -> str:
    """El estado es el ÚLTIMO addressed / partially / not addressed del tramo (el título puede contener esas
    palabras: "F1 [bug] Null case not addressed in parser — addressed"), sin contar lo que va entre
    paréntesis o en `código` (la evidencia: "addressed (`a.ts:3`, partially rewritten)")."""
    seg = strip_code(seg)
    while re.search(r"\([^()]*\)", seg):
        seg = re.sub(r"\([^()]*\)", " ", seg)
    found = STATUS_RE.findall(seg)
    if not found:
        return ""
    last = found[-1].lower()
    return "addressed" if last == "addressed" else ("partially" if last.startswith("partially") else "not addressed")


def _prior_unresolved(content: str, prior_ids: list[str]) -> bool:
    """En una re-revisión, cada hallazgo anterior tiene que salir como `F<n> … addressed` (no partially / not)."""
    for fid in prior_ids:
        segs = _segments(content, fid)
        if not segs or any(_status(seg) != "addressed" for seg in segs if _status(seg)) \
                or not any(_status(seg) == "addressed" for seg in segs):
            return True
    return False


def parse_verdict(content: str, prior_ids: list[str] | None = None) -> dict:
    """La línea **Verdict:** tiene que ser la primera y única (si hay dos, no vale ninguna)."""
    text = (content or "").lstrip()
    found = VERDICT_RE.findall(text)
    m = VERDICT_RE.match(text)
    if not m or len(found) != 1:
        return {}
    verdict = "approve" if m.group(1).lower() == "approve" else "request_changes"
    flagged = any(_status(seg) in ("partially", "not addressed") for seg in _segments(text))
    return {"verdict": verdict, "findings": int(m.group(2)),
            "prior_open": flagged or _prior_unresolved(text, prior_ids or []),
            "open_findings": comment_findings(text) if verdict == "request_changes" else []}


FINDING_LINE_RE = re.compile(r"^\s*[-*•]?\s*\**(F\d{1,2})\**\s*\[([a-z][a-z-]{1,19})\]\s*(.+)$", re.I)


def comment_findings(text: str) -> list[str]:
    """Los hallazgos tal y como quedaron publicados ("F1 [tests] título"), por si la respuesta final falla."""
    out = []
    for ln in text.splitlines():
        m = FINDING_LINE_RE.match(ln)
        if m:
            title = re.split(r"\s+[—–]\s+", m.group(3), maxsplit=1)[0].strip(" *")
            out.append(f"{m.group(1).upper()} [{m.group(2).lower()}] {title}"[:120])
    return out[:30]


def _check_write(args: dict, ctx: Ctx) -> str | None:
    if ctx.mode != "publish":
        return "este modo no publica en Bitbucket"
    if set(args) - EXECUTE_KEYS:
        return f"argumentos no permitidos: {', '.join(sorted(set(args) - EXECUTE_KEYS))}"
    op = str(args.get("name") or "")
    if op not in WRITE_OPS:
        return f"escritura no permitida: {op!r} (solo comentar, aprobar o pedir cambios en el PR en revisión)"
    inputs = args.get("inputs")
    if not isinstance(inputs, dict):
        return "inputs con formato inesperado"
    if set(inputs) != WRITE_OPS[op]:
        return f"{op} solo admite {', '.join(sorted(WRITE_OPS[op]))} (sin comentarios en línea ni borradores)"
    if str(inputs.get("workspaceId")) != ctx.workspace or str(inputs.get("repoId")) != ctx.repo:
        return f"solo se puede escribir en {ctx.workspace}/{ctx.repo}"
    if str(inputs.get("prId")) != str(ctx.pr_id) or not ctx.pr_id:
        return f"solo se puede escribir en el PR #{ctx.pr_id}"
    if op == "addBitbucketRepoPullRequestComment":
        if ctx.comment():
            return "ya se publicó el comentario de esta revisión en este commit; no se repite"
        content = inputs.get("content")
        if not isinstance(content, str) or not content.strip():
            return "comentario vacío"
        if len(content) > MAX_COMMENT_CHARS:
            return f"comentario de {len(content)} caracteres (máx {MAX_COMMENT_CHARS})"
        v = parse_verdict(content, ctx.prior_ids)
        if not v:
            return "el comentario debe incluir la línea **Verdict:** Approve|Request Changes — <n> findings"
        if v["verdict"] == "approve" and (v["findings"] or v["prior_open"]):
            if v["findings"]:
                return f"Approve con {v['findings']} hallazgos: si queda alguno, el veredicto es Request Changes"
            return ("Approve solo si cada hallazgo anterior aparece como `F<n> addressed` en su propia línea o "
                    f"tramo (ninguno partially / not addressed). Anteriores: {', '.join(ctx.prior_ids) or 'ninguno'}")
        if v["verdict"] == "request_changes" and v["findings"] == 0 and not v["prior_open"]:
            return "Request Changes sin hallazgos: si no queda nada, el veredicto es Approve"
        if re.search(r"@\{", content):
            return "menciones de Bitbucket (@{…}) no permitidas en el comentario"
        bad = _check_links(content, ctx.workspace, ctx.repo)
        if bad:
            return bad
    else:
        c = ctx.comment()
        if not c:
            return "primero el comentario con la revisión, después el estado"
        if ctx.state_done():
            return "el estado del PR ya se marcó en esta revisión"
        want = "approve" if STATE_OPS[op] == "approved" else "request_changes"
        if c.get("verdict") != want:
            return f"el comentario publicado dice {c.get('verdict')!r}: no coincide con {op}"
        if want == "approve" and (c.get("findings") or c.get("prior_open")):
            return "no se aprueba mientras quede algún hallazgo abierto"
    return _gate(ctx)


def _gate(ctx: Ctx) -> str | None:
    if ctx.gate is None:
        return "no puedo comprobar que el PR siga abierto; no se publica"
    return ctx.gate()


def _check_slack_send(args: dict, ctx: Ctx) -> str | None:
    if not ctx.slack_user_id:
        return "falta slack_user_id en la config de pr-watcher: no se envía nada"
    allowed_keys = ALLOWED_MSG_KEYS | ({"thread_ts"} if ctx.mode == "publish" else set())
    extra = set(args) - allowed_keys
    if extra:
        return f"argumentos no permitidos en el mensaje: {', '.join(sorted(extra))}"
    targets = [args[k] for k in TARGET_KEYS if k in args]
    if not targets:
        return "mensaje de Slack sin destinatario explícito"
    if len(set(map(str, targets))) != 1:
        return "destinatario ambiguo"
    target = str(targets[0])
    texts = [args[k] for k in TEXT_KEYS if k in args]
    if not ctx.expected:
        return "no hay texto esperado para esta ejecución; no se envía nada"
    if len(texts) != 1 or not isinstance(texts[0], str):
        return "el mensaje debe tener un único texto"
    text = texts[0]
    if ctx.mode == "exact":
        if target != ctx.slack_user_id:
            return f"solo se permite el DM a {ctx.slack_user_id}"
        if text.strip() != ctx.expected.strip():
            return "el texto del mensaje no coincide con el esperado; no se envía"
        return None
    if len(text) > MAX_REVIEW_CHARS:
        return f"mensaje de {len(text)} caracteres (máx {MAX_REVIEW_CHARS}); divídelo"
    sent = ctx.posted("dm") + ctx.posted("thread")
    if len(sent) >= MAX_REVIEW_MESSAGES:
        return f"ya se enviaron {MAX_REVIEW_MESSAGES} mensajes; no se envían más"
    if "thread_ts" in args:
        if ctx.mode != "publish" or target != ctx.channel or not ctx.channel:
            return f"las respuestas en hilo solo van al canal de PRs ({ctx.channel or 'sin configurar'})"
        ts = str(args.get("thread_ts") or "")
        if ts not in ctx.confirmed():
            return ("thread_ts no es un anuncio confirmado de este PR: búscalo con slack_read_channel y ábrelo "
                    "con slack_read_thread (channel_id del canal de PRs, message_ts del anuncio) antes de responder")
        if ctx.done.get("thread") and not ctx.posted("thread"):
            return "la revisión de este commit ya se publicó en el hilo; no se repite"
        if not ctx.comment():
            return "primero se publica en Bitbucket; el hilo cuenta lo que de verdad se publicó"
        if not ctx.posted("thread") and not re.search(rf"#{ctx.pr_id}(?!\d)", text):
            return f"el primer mensaje del hilo debe nombrar el PR (#{ctx.pr_id})"
        bad = _check_thread_claims(text, ctx)
        if bad:
            return bad
        bad = _check_links(text, ctx.workspace, ctx.repo)
        return bad or _gate(ctx)
    if target != ctx.slack_user_id:
        return f"fuera de un hilo del canal de PRs solo se permite el DM a {ctx.slack_user_id}"
    if not text.lstrip().startswith(ctx.expected.strip()):
        return "el mensaje de revisión debe empezar por la cabecera del watcher; no se envía"
    return _check_links(text.lstrip()[len(ctx.expected.strip()):], ctx.workspace, ctx.repo)


APPROVED_CLAIM = re.compile(r":white_check_mark:|:heavy_check_mark:|✅|\bapproved\b|\baprobad[oa]\b", re.I)
CHANGES_CLAIM = re.compile(r":warning:|⚠|\bchanges requested\b|\bcambios pedidos\b|\bpedid[oa]s? cambios\b", re.I)
MARKED_CLAIM = re.compile(r"\bmarked on bitbucket\b|\bmarcad[oa] en bitbucket\b", re.I)


def _check_thread_claims(text: str, ctx: Ctx) -> str | None:
    """Lo que el hilo dice del voto tiene que ser verdad: es tu nombre ante el equipo."""
    vote = ctx.effective_vote()
    # El título del PR va entre `comillas`: no cuenta (puede decir "Show approved candidates").
    head = strip_code("\n".join(text.strip().splitlines()[:2]))
    if APPROVED_CLAIM.search(head) and vote != "approved":
        return "el mensaje dice aprobado, pero el PR no está aprobado por ti en Bitbucket"
    if CHANGES_CLAIM.search(head) and vote != "changes_requested":
        return "el mensaje dice changes requested, pero ese voto no está marcado en Bitbucket"
    if MARKED_CLAIM.search(head) and not vote:
        return "no digas 'marked on Bitbucket': el voto no se marcó (usa 'commented on the PR')"
    return None


def _check_reaction(args: dict, ctx: Ctx) -> str | None:
    if ctx.mode != "publish" or not ctx.reactions:
        return "las reacciones están desactivadas en esta ejecución"
    if set(args) - {"channel_id", "message_ts", "emoji"}:
        return "argumentos no permitidos en la reacción"
    if str(args.get("channel_id")) != ctx.channel:
        return f"solo se reacciona en el canal de PRs ({ctx.channel})"
    if str(args.get("message_ts")) not in ctx.confirmed():
        return "solo se reacciona en el anuncio de este PR (ábrelo antes con slack_read_thread)"
    emoji = str(args.get("emoji") or "").strip(":")
    if emoji not in REACTIONS:
        return f"reacción no permitida: {emoji!r} (solo {', '.join(REACTIONS)})"
    need = REACTIONS[emoji]
    if need and ctx.effective_vote() != need:
        return f":{emoji}: solo después de marcar el PR como {need} en Bitbucket"
    return None if emoji == "eyes" else _gate(ctx)


def decide_ctx(payload: dict, ctx: Ctx) -> str | None:
    """Devuelve None si se permite, o el motivo del bloqueo."""
    tool = str(payload.get("tool_name") or "")
    if tool in SAFE_BUILTINS:
        return None
    short = tool.split("__")[-1]
    args = payload.get("tool_input") or {}
    if tool not in ctx.allowed:
        return f"herramienta {tool!r} no permitida por pr-watcher"
    if not isinstance(args, dict):
        return "argumentos con formato inesperado"
    if short == "executeWrite":
        return _check_write(args, ctx)
    if WRITE_TOOL.match(short):
        return f"{tool!r} escribe: solo se permiten las escrituras de executeWrite que conoce el guardián"
    if short == "executeRead":
        return _check_read_op(args, ctx.workspace, ctx.repo)
    if "slack" in tool.lower():
        if short == "slack_read_channel":
            if ctx.mode != "publish":
                return "leer canales no hace falta en este modo"
            if set(args) - READ_CHANNEL_KEYS:
                return "argumentos no permitidos al leer el canal"
            if str(args.get("channel_id")) != ctx.channel or not ctx.channel:
                return f"solo se puede leer el canal de PRs ({ctx.channel or 'sin configurar'})"
            return None
        if short == "slack_read_thread":
            if ctx.mode != "publish":
                return "leer hilos no hace falta en este modo"
            if set(args) - READ_THREAD_KEYS:
                return "argumentos no permitidos al leer el hilo"
            if str(args.get("channel_id")) != ctx.channel or not ctx.channel:
                return f"solo se pueden leer hilos del canal de PRs ({ctx.channel or 'sin configurar'})"
            if str(args.get("message_ts")) not in ctx.candidates():
                return "solo el hilo de un mensaje del canal que enlace este PR (búscalo con slack_read_channel)"
            return None
        if short == "slack_add_reaction":
            return _check_reaction(args, ctx)
        if MESSAGING.search(short):
            if short != "slack_send_message":
                return f"{tool!r}: solo se envía con slack_send_message"
            return _check_slack_send(args, ctx)
        return f"{tool!r}: herramienta de Slack sin reglas en el guardián"
    if short == "getJiraIssue":
        key = str(args.get("issueIdOrKey") or "").strip().upper()
        m = JIRA_KEY_RE.match(key)
        if not m or m.group(1) not in ctx.jira_projects:
            return f"solo tickets de {', '.join(sorted(ctx.jira_projects)) or 'ningún proyecto'}"
        return None
    if short in HARMLESS:
        return None
    return (f"{tool!r} no tiene reglas en el guardián: no se usa (añadirla a la lista no basta; "
            "cada herramienta necesita sus reglas en guard.py)")


def decide(payload: dict, allowed: list[str], slack_user_id: str, expected: str | None = None,
           mode: str = "exact", sent_count: int = 0, workspace: str = "", repo: str = "") -> str | None:
    """Compatibilidad: modo prueba/DM. `sent_count` = DMs ya enviados en esta ejecución."""
    events = [{"phase": "post", "kind": "dm", "sent": True}] * sent_count
    return decide_ctx(payload, Ctx(allowed, slack_user_id, expected, mode, workspace, repo, events=events))


def _read_events() -> list[dict]:
    path = os.environ.get("PRWATCH_TRACE")
    if not path:
        return []
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


LINK_RE = re.compile(r"https://[\w.-]*slack\.com/archives/[A-Za-z0-9]+/p\d+[^\s\"'\\]*")


def _trace(event: dict) -> None:
    """Añade una línea al rastro de esta ejecución (solo dentro de la carpeta de rastros del watcher)."""
    path = os.environ.get("PRWATCH_TRACE")
    if not path:
        return
    from prwatch import paths

    try:
        target = os.path.realpath(path)
        if os.path.dirname(target) != os.path.realpath(paths.traces_dir()):
            return
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _failed(raw, depth: int = 0) -> bool:
    """¿La herramienta devolvió un error? Mira también dentro del texto (los MCP suelen devolver el JSON del
    servicio como texto) y ante la duda cuenta como fallo: mejor reintentar que dar por hecho algo que no."""
    if depth > 6:
        return True
    if isinstance(raw, dict):
        if raw.get("isError") or raw.get("is_error") or raw.get("ok") is False or raw.get("error") \
                or raw.get("type") == "error" or raw.get("errors"):
            return True
        status = raw.get("status") or raw.get("statusCode")
        if isinstance(status, int) and status >= 400:
            return True
        # Solo se baja por el sobre del MCP (content/text/result): el cuerpo de un comentario creado
        # ("Error handling is missing…") no es un error.
        return any(_failed(raw[k], depth + 1) for k in ("content", "text", "result") if k in raw)
    if isinstance(raw, list):
        return any(_failed(v, depth + 1) for v in raw)
    if isinstance(raw, str):
        t = raw.strip()
        if t[:1] in ("{", "["):
            try:
                return _failed(json.loads(t), depth + 1)
            except (json.JSONDecodeError, RecursionError):
                pass
        return bool(re.match(r"(?i)(error|failed|forbidden|unauthori[sz]ed|conflict|not found)\b", t)) \
            or bool(re.match(r"\d{3}\b", t) and int(t[:3]) >= 400)
    return False


def _texts(raw) -> list[str]:
    """Todas las cadenas de una respuesta (los MCP devuelven texto dentro de content[].text)."""
    if isinstance(raw, str):
        t = raw.strip()
        if t[:1] in ("{", "[") and len(t) < 2_000_000:
            try:
                return _texts(json.loads(t))
            except (json.JSONDecodeError, RecursionError):
                pass
        return [raw]
    if isinstance(raw, dict):
        return [t for v in raw.values() for t in _texts(v)]
    if isinstance(raw, list):
        return [t for v in raw for t in _texts(v)]
    return []


def _find_id(raw, depth: int = 0):
    """Primer "id" numérico de la respuesta (el id del comentario creado)."""
    if depth > 6:
        return None
    if isinstance(raw, str):
        t = raw.strip()
        if t[:1] in ("{", "["):
            try:
                return _find_id(json.loads(t), depth + 1)
            except (json.JSONDecodeError, RecursionError):
                pass
        m = re.search(r"(?i)\bcomment\b[^\n]{0,40}?\bid\b\D{0,4}(\d{2,12})\b", t) or \
            re.search(r"(?i)\bid\b\s*[:=#]\s*(\d{2,12})\b", t)
        return int(m.group(1)) if m else None
    if isinstance(raw, dict):
        rid = raw.get("id")
        if isinstance(rid, int) and not isinstance(rid, bool):
            return rid
        if isinstance(rid, str) and rid.isdigit():
            return int(rid)
        vals = raw.values()
    elif isinstance(raw, list):
        vals = raw
    else:
        return None
    for v in vals:
        found = _find_id(v, depth + 1)
        if found is not None:
            return found
    return None


MSG_SPLIT = re.compile(r"(?==== Message from )")
MSG_TS = re.compile(r"Message TS:\s*(\d{9,11}\.\d{1,7})")


def _pr_url_re(pr_id: int, workspace: str, repo: str) -> re.Pattern:
    sl = r"\\*/"  # las barras pueden venir escapadas (\/) en el JSON de la respuesta
    return re.compile(rf"bitbucket\.org{sl}{re.escape(workspace)}{sl}{re.escape(repo)}{sl}pull-requests{sl}"
                      rf"{int(pr_id)}(?!\d)", re.I)


def thread_parent_links(raw, pr_id: int, workspace: str, repo: str) -> bool:
    """slack_read_thread: ¿el mensaje padre (lo que va tras "THREAD:" y antes de la primera respuesta) enlaza
    este PR? Slack resuelve el ts, así que esto confirma que ese ts es de verdad un mensaje sobre el PR."""
    if not pr_id or not workspace or not repo:
        return False
    pat = _pr_url_re(pr_id, workspace, repo)
    for text in _texts(raw):
        t = text.lstrip()
        if not t.startswith("THREAD:"):
            continue
        # Solo el primer bloque que empieza por THREAD: (el padre); las respuestas van tras "\n\n> ".
        parent = re.split(r"\n\s*\n> ", t[len("THREAD:"):], maxsplit=1)[0]
        return bool(pat.search(parent))
    return False


def thread_candidates(raw, pr_id: int, workspace: str, repo: str) -> list[str]:
    """Message TS de los mensajes de primer nivel del canal que enlazan este PR (solo candidatos: el texto
    de un mensaje podría imitar la cabecera de otro; slack_read_thread los confirma)."""
    if not pr_id:
        return []
    pat = _pr_url_re(pr_id, workspace, repo)
    out = []
    for text in _texts(raw):
        for block in MSG_SPLIT.split(text):
            m = MSG_TS.search(block)
            if m and pat.search(block):
                out.append(m.group(1))
    return out[:20]


def post() -> int:
    """PostToolUse: registra lo que se ejecutó y si salió bien. Nunca bloquea (la herramienta ya corrió)."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = str(payload.get("tool_name") or "")
        short = tool.split("__")[-1]
        args = payload.get("tool_input") or {}
        args = args if isinstance(args, dict) else {}
        raw = payload.get("tool_response")
        event: dict = {"phase": "post", "tool": tool[:120]}
        pr_id = int(os.environ.get("PRWATCH_PR_ID") or 0)
        if "slack" in tool.lower() and short in ("slack_read_channel", "slack_read_thread"):
            from prwatch import config
            cfg = config.load()
            if short == "slack_read_channel":
                event["kind"] = "read_channel"
                event["candidates"] = thread_candidates(raw, pr_id, cfg.bitbucket_workspace, cfg.bitbucket_repo)
            else:
                ts = str(args.get("message_ts") or "")
                event.update(kind="read_thread", ts=ts[:20],
                             confirmed=thread_parent_links(raw, pr_id, cfg.bitbucket_workspace, cfg.bitbucket_repo)
                             and str(args.get("channel_id")) == cfg.slack_review_channel)
        elif "slack" in tool.lower() and short == "slack_add_reaction":
            event.update(kind="reaction", sent=not _failed(raw), emoji=str(args.get("emoji", ""))[:30])
        elif _is_messaging(tool):
            resp = json.dumps(raw, ensure_ascii=False)
            m = LINK_RE.search(resp)
            event.update(kind="thread" if args.get("thread_ts") else "dm", sent=not _failed(raw),
                         link=m.group(0)[:300] if m else "", channel=str(args.get("channel_id", ""))[:40])
        elif short == "executeWrite":
            op = str(args.get("name") or "")
            inputs = args.get("inputs") if isinstance(args.get("inputs"), dict) else {}
            ok = not _failed(raw)
            if op == "addBitbucketRepoPullRequestComment":
                cid = _find_id(raw)
                # Sin id del comentario creado no se da por publicado (no se desbloquea el voto).
                ok = ok and cid is not None
                event.update(kind="bb_comment", sent=ok, comment_id=cid,
                             **parse_verdict(str(inputs.get("content") or ""), _prior_ids()))
            elif op in STATE_OPS:
                event.update(kind="bb_state", sent=ok, state=STATE_OPS[op])
            if not ok:
                event["error"] = " ".join(_texts(raw))[:300]
        _trace(event)
    except Exception:  # noqa: BLE001
        pass
    return 0


def make_gate(cfg, pr_id: int, head: str) -> Callable[[], str | None]:
    """Antes de cada escritura: el PR sigue abierto y en el commit revisado (si no, la revisión está vieja)."""
    def gate() -> str | None:
        from prwatch.bitbucket import BitbucketError, Client

        try:
            st = Client(cfg).pr_status(pr_id)
        except (BitbucketError, OSError, ValueError) as e:
            return f"no pude comprobar el PR en Bitbucket ({e}); no se publica"
        if st["state"] != "OPEN":
            return f"el PR #{pr_id} ya no está abierto ({st['state']}); no se publica"
        remote = st.get("head") or ""
        n = min(len(remote), len(head))
        if n < 12 or remote[:n] != head[:n]:
            return (f"el PR #{pr_id} tiene commits nuevos ({remote[:8] or '?'}); la revisión está vieja, "
                    "no se publica")
        return None
    return gate


def build_ctx(cfg) -> Ctx:
    try:
        done = json.loads(os.environ.get("PRWATCH_DONE") or "{}")
    except json.JSONDecodeError:
        done = {}
    pr_id = int(os.environ.get("PRWATCH_PR_ID") or 0)
    head = re.sub(r"[^0-9a-f]", "", os.environ.get("PRWATCH_HEAD", ""))[:40]
    return Ctx(
        allowed=cfg.claude_allowed_tools, slack_user_id=cfg.slack_user_id,
        expected=os.environ.get("PRWATCH_EXPECTED_MESSAGE"),
        mode=os.environ.get("PRWATCH_MESSAGE_MODE", "exact"),
        workspace=cfg.bitbucket_workspace, repo=cfg.bitbucket_repo, pr_id=pr_id, head=head,
        channel=cfg.slack_review_channel, reactions=cfg.slack_reactions, events=_read_events(),
        jira_projects=frozenset(k.upper() for k in cfg.jira_projects), prior_ids=_prior_ids(),
        my_vote=os.environ.get("PRWATCH_MY_VOTE", "") if os.environ.get("PRWATCH_MY_VOTE") in
        ("approved", "changes_requested") else "",
        done=done if isinstance(done, dict) else {}, gate=make_gate(cfg, pr_id, head),
    )


def _is_messaging(tool: str) -> bool:
    return "slack" in tool.lower() and bool(MESSAGING.search(tool.split("__")[-1]))


def main(post_phase: bool = False) -> int:
    if post_phase:
        return post()
    try:
        from prwatch import config, state

        payload = json.loads(sys.stdin.read() or "{}")
        cfg = config.load()
        reason = decide_ctx(payload, build_ctx(cfg))
    except Exception as e:  # noqa: BLE001 - fail-closed
        print(f"pr-watcher guard: error interno ({e}); bloqueado", file=sys.stderr)
        _trace({"phase": "pre", "tool": "?", "blocked": True, "reason": f"error interno: {e}"[:200]})
        return 2
    _trace({"phase": "pre", "tool": str(payload.get("tool_name"))[:120], "blocked": reason is not None,
            "reason": (reason or "")[:200]})
    if reason is None:
        return 0
    try:
        pr = os.environ.get("PRWATCH_PR_ID") or None
        state.log(f"Bloqueado {payload.get('tool_name')}" + (f" (PR #{pr})" if pr else "") + f": {reason}",
                  "warn", pr)
    except Exception:  # noqa: BLE001
        pass
    print(reason, file=sys.stderr)
    return 2
