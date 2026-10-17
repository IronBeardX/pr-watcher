"""Lectura mínima de la API de Bitbucket Cloud. Solo lectura: el watcher nunca escribe en Bitbucket."""
from __future__ import annotations

import base64
import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from prwatch.config import Config, read_env_file
from prwatch.text import strip_ctrl

API = "https://api.bitbucket.org/2.0"


class BitbucketError(RuntimeError):
    def __init__(self, msg: str, retry_after: int | None = None, kind: str = "other"):
        super().__init__(msg)
        self.retry_after = retry_after
        self.kind = kind


class Client:
    def __init__(self, cfg: Config):
        env = read_env_file(cfg.env_path)
        email = os.environ.get("BB_EMAIL") or env.get("BB_EMAIL")
        token = os.environ.get("BB_TOKEN") or env.get("BB_TOKEN")
        if not email or not token:
            raise BitbucketError(
                f"Faltan BB_EMAIL/BB_TOKEN (ni en el entorno ni en {cfg.env_path})", kind="credentials"
            )
        self._auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        self.base = f"{API}/repositories/{cfg.bitbucket_workspace}/{cfg.bitbucket_repo}"
        self.cfg = cfg

    def _get(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(
            url, headers={"Authorization": self._auth, "Accept": "application/json"}
        )
        short = url.split("?")[0]
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200].replace("\n", " ")
            retry = None
            if e.code == 429:
                try:
                    retry = int(e.headers.get("Retry-After") or 300)
                except ValueError:
                    retry = 300
            hint = {401: " (token inválido o caducado)", 403: " (el token no tiene permiso/scope)",
                    429: " (límite de peticiones)"}.get(e.code, "")
            raise BitbucketError(f"Bitbucket HTTP {e.code}{hint} en {short}: {body}", retry,
                                 f"http{e.code}") from e
        except urllib.error.URLError as e:
            raise BitbucketError(f"Sin conexión con Bitbucket: {e.reason}", kind="network") from e
        except (OSError, http.client.HTTPException) as e:
            raise BitbucketError(f"Error de red con Bitbucket ({type(e).__name__}): {e}",
                                 kind="network") from e
        except ValueError as e:
            raise BitbucketError(f"Bitbucket devolvió algo que no es JSON en {short}", kind="json") from e
        if not isinstance(data, dict):
            raise BitbucketError(f"Respuesta inesperada de Bitbucket en {short}")
        return data

    def me(self) -> dict[str, Any]:
        u = self._get(f"{API}/user")
        return {
            "uuid": u.get("uuid"),
            "nickname": u.get("nickname"),
            "display_name": u.get("display_name"),
        }

    def open_prs(self, updated_since: datetime | None = None) -> list[dict[str, Any]]:
        """PRs hacia la rama destino.

        Sin ventana: solo los abiertos. Con ventana: los actualizados desde `updated_since` en cualquier
        estado, para saber también cuáles se fusionaron o cerraron dentro de la ventana.
        """
        dest = f'destination.branch.name="{self.cfg.destination_branch}"'
        params: list[tuple[str, Any]] = [("pagelen", 50), ("sort", "-updated_on")]
        if updated_since is None:
            params.append(("q", f'state="OPEN" AND {dest}'))
        else:
            ts = updated_since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            params.append(("q", f"{dest} AND updated_on > {ts}"))
            params += [("state", st) for st in ("OPEN", "MERGED", "DECLINED", "SUPERSEDED")]
        url = f"{self.base}/pullrequests?" + urllib.parse.urlencode(params)
        out: list[dict[str, Any]] = []
        for _ in range(10):  # tope de seguridad: 500 PRs
            page = self._get(url)
            out.extend(normalize(p) for p in page.get("values", []))
            url = page.get("next")
            if not url:
                break
        return out

    def ci_summary(self, commit: str) -> str:
        """Resumen de los checks del commit (CodeBuild/Pipelines publican commit statuses)."""
        commit = "".join(c for c in str(commit) if c in "0123456789abcdef")[:40]
        if not commit:
            return ""
        data = self._get(f"{self.base}/commit/{commit}/statuses?pagelen=100")
        states: dict[str, list[str]] = {}
        for st in data.get("values", []):
            states.setdefault(str(st.get("state")), []).append(strip_ctrl(st.get("name"), 80))
        total = sum(len(v) for v in states.values())
        if not total:
            return "sin checks de CI en este commit"
        ok = len(states.get("SUCCESSFUL", []))
        parts = [f"{ok}/{total} en verde"]
        if states.get("INPROGRESS"):
            parts.append(f"{len(states['INPROGRESS'])} en curso")
        bad = states.get("FAILED", []) + states.get("STOPPED", [])
        if bad:
            parts.append("fallidos: " + ", ".join(bad[:10]))
        return " · ".join(parts)

    def pr(self, pr_id: int) -> dict[str, Any]:
        return normalize(self._get(f"{self.base}/pullrequests/{int(pr_id)}"))

    def pr_status(self, pr_id: int, me: set[str] | None = None) -> dict[str, Any]:
        """Estado, commit de cabeza y mi voto actual (approved / changes_requested / "") en el PR.

        `me` = identificadores con los que te reconozco (uuid y nickname), en minúsculas.
        """
        raw = self._get(f"{self.base}/pullrequests/{int(pr_id)}")
        pr = normalize(raw)
        mine = ""
        for part in raw.get("participants") or []:
            user = part.get("user") or {}
            # uuid o nickname, nunca el nombre visible (dos personas pueden llamarse igual).
            ids = {str(user.get(k) or "").lower() for k in ("uuid", "nickname")} - {""}
            if me and ids & me:
                if part.get("state") in ("approved", "changes_requested"):
                    mine = part["state"]
                elif part.get("approved"):
                    mine = "approved"
        return {"state": pr["state"], "head": pr["head"], "my_vote": mine}


def normalize(p: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(p, dict) or "id" not in p:
        raise BitbucketError("PR sin id en la respuesta de Bitbucket")
    author = p.get("author") or {}
    return _clean_fields({
        "id": int(p["id"]),
        "title": p.get("title", ""),
        "state": p.get("state", ""),
        "draft": bool(p.get("draft", False)),
        "head": ((p.get("source") or {}).get("commit") or {}).get("hash", ""),
        "source_branch": ((p.get("source") or {}).get("branch") or {}).get("name", ""),
        "destination": ((p.get("destination") or {}).get("branch") or {}).get("name", ""),
        "author": author.get("display_name", ""),
        "author_nickname": author.get("nickname", ""),
        "author_uuid": author.get("uuid", ""),
        "created_on": p.get("created_on", ""),
        "updated_on": p.get("updated_on", ""),
        "url": ((p.get("links") or {}).get("html") or {}).get("href", ""),
    })


def _clean_fields(d: dict[str, Any]) -> dict[str, Any]:
    """Quita caracteres de control al entrar: el título y el autor los escribe otra persona."""
    for k in ("title", "author", "author_nickname", "source_branch", "destination", "url", "state",
              "head", "created_on", "updated_on", "author_uuid"):
        d[k] = strip_ctrl(d.get(k), 500)
    return d
