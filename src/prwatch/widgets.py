"""Widgets compartidos por la TUI y el asistente."""
from __future__ import annotations

from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Button, Input, Label, OptionList

from prwatch.config import DEFAULT_TOOLS, TOOL_RE


class ToolList(Widget):
    """Editor de la lista de herramientas, al estilo del PATH de Windows: una por línea, con añadir,
    editar, quitar, subir, bajar y restaurar los valores por defecto."""

    DEFAULT_CSS = """
    ToolList { height: auto; }
    ToolList OptionList { height: 10; }
    ToolList .tool-buttons { height: auto; }
    ToolList .tool-buttons Button { min-width: 8; margin-right: 1; }
    """

    def __init__(self, tools: list[str], **kw: Any):
        super().__init__(**kw)
        self.tools = list(tools)

    def compose(self) -> ComposeResult:
        yield OptionList(*self.tools, id="tool-options")
        yield Input(placeholder="mcp__<servidor>__<herramienta>", id="tool-input")
        with Horizontal(classes="tool-buttons"):
            yield Button("Añadir", id="tool-add")
            yield Button("Reemplazar", id="tool-edit")
            yield Button("Quitar", id="tool-del")
            yield Button("↑", id="tool-up")
            yield Button("↓", id="tool-down")
            yield Button("Por defecto", id="tool-reset")
        yield Label("", id="tool-err", classes="err")

    def _refresh(self, select: int | None = None) -> None:
        ol = self.query_one("#tool-options", OptionList)
        ol.clear_options()
        ol.add_options(self.tools)
        if select is not None and self.tools:
            ol.highlighted = max(0, min(select, len(self.tools) - 1))

    def _selected(self) -> int | None:
        i = self.query_one("#tool-options", OptionList).highlighted
        return i if i is not None and 0 <= i < len(self.tools) else None

    def _error(self, msg: str) -> None:
        self.query_one("#tool-err", Label).update(msg)

    @on(OptionList.OptionHighlighted, "#tool-options")
    def _pick(self) -> None:
        i = self._selected()
        if i is not None:
            self.query_one("#tool-input", Input).value = self.tools[i]

    def _value(self) -> str | None:
        v = self.query_one("#tool-input", Input).value.strip()
        if not TOOL_RE.match(v):
            self._error("Nombre exacto: mcp__<servidor>__<herramienta> (míralo con /mcp)")
            return None
        self._error("")
        return v

    @on(Button.Pressed, "#tool-add")
    @on(Input.Submitted, "#tool-input")
    def add(self) -> None:
        v = self._value()
        if v is None:
            return
        if v in self.tools:
            self._error("Ya está en la lista")
            return
        self.tools.append(v)
        self._refresh(len(self.tools) - 1)

    @on(Button.Pressed, "#tool-edit")
    def edit(self) -> None:
        i, v = self._selected(), self._value()
        if i is None or v is None:
            return
        if v in self.tools and self.tools.index(v) != i:
            self._error("Ya está en la lista")
            return
        self.tools[i] = v
        self._refresh(i)

    @on(Button.Pressed, "#tool-del")
    def delete(self) -> None:
        i = self._selected()
        if i is not None:
            del self.tools[i]
            self._refresh(i)

    @on(Button.Pressed, "#tool-up")
    def up(self) -> None:
        i = self._selected()
        if i:
            self.tools[i - 1], self.tools[i] = self.tools[i], self.tools[i - 1]
            self._refresh(i - 1)

    @on(Button.Pressed, "#tool-down")
    def down(self) -> None:
        i = self._selected()
        if i is not None and i < len(self.tools) - 1:
            self.tools[i + 1], self.tools[i] = self.tools[i], self.tools[i + 1]
            self._refresh(i + 1)

    @on(Button.Pressed, "#tool-reset")
    def reset(self) -> None:
        self.tools = list(DEFAULT_TOOLS)
        self._refresh(0)
