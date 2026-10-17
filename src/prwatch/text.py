"""Limpieza de texto no fiable (lo escriben otras personas o el modelo)."""
from __future__ import annotations

import re
from typing import Any

# C0/C1 (incluye ESC 0x1b y CSI 0x9b), DEL, bidi y zero-width: nada de secuencias de terminal
# (OSC 52 al portapapeles, cambiar título, borrar pantalla) ni texto que se reordena.
CTRL = re.compile("[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]")


def strip_ctrl(v: Any, limit: int | None = None) -> str:
    s = CTRL.sub(" ", str(v if v is not None else ""))
    return s[:limit] if limit else s
