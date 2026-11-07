"""Element inventory + in-process interaction for the control channel (MCP v2 P1).

The control channel's answer to "an agent must click by *element*, not by
pixels, and must be able to *see* what exists". Three modules, one concern each:

* :mod:`~core.control.elements.inventory` — flatten the live widget tree into
  interactive ``Element`` rows (buttons, inputs, views, cards, menu rows, tabs,
  live ``QRhiWidget`` canvases)
  with a stable id, role, label, rects, and the Find-Action id a widget is
  bound to.
* :mod:`~core.control.elements.resolve` — :func:`resolve_element`
  (retry-until-rendered).
* :mod:`~core.control.elements.interaction` — in-process delivery via
  ``QTest`` / ``QApplication.sendEvent``.
* here — the retry knobs, the public re-exports, and :class:`ElementMethodsMixin`
  (the four handler methods, kept next to the logic so ``handlers.py`` stays a
  thin dispatch layer).

**The id is opaque to clients.** It is derived deterministically from the
window identity + the widget's child-index path + the bound action id, so two
``ui_elements`` calls over an unchanged UI return the same ids and the other
three methods can resolve them. Do not parse it; pass it back verbatim.

**Rect semantics.** ``rect`` is *widget-local*: the clickable region in the
coordinate space of the widget that receives the event (``[0, 0, w, h]`` for a
plain widget, the row geometry inside a ``QMenu`` for a menu row).
``screen_rect`` maps that to global coordinates via ``QWidget.mapToGlobal``. On
Wayland screen coordinates are best-effort (the compositor owns global
placement); in-process delivery does **not** require the element to be on
screen, so a click still reaches its handler even when ``screen_rect`` is
wrong or off-display.

**Role mapping (toolkit widgets are first-class).** This app's UI is built from
``sli-ui-toolkit`` widgets, which are plain ``QWidget`` subclasses — not the
stock patents (``QAbstractButton``, ``QLineEdit``, …) an ``isinstance``-only
inventory recognises. Roles are therefore duck-typed by class name (and by
``__module__`` starting with ``sli_ui_toolkit``), with no toolkit import at
module load:

* ``*Button`` / ``*Switch`` / ``*RadioButton`` / ``*CheckBox`` → ``button``;
* ``*Slider`` / ``*SpinBox`` / ``*ComboBox`` / ``*LineEdit`` → ``input``;
* ``*TabItem`` / ``*TabBar`` / ``*TabStrip`` → ``tab``;
* a ``Button`` (or ``*Card*``) whose nearest ``*GridView`` ancestor makes it a
  grid cell → ``card`` (the image-gallery card case);
* a live ``QRhiWidget`` → ``canvas`` (the surface a tab's wheel/keys act on;
  addressable so ``press_keys target=<id>`` can zoom/pan it);
* ``ContextMenu`` / ``BaseFlyout`` / ``*Flyout`` (a ``flyout_group`` marker
  also counts) → ``menu``; each row widget inside an **open** toolkit menu →
  ``menu-row`` with its ``_text`` as label (a stock ``QMenu`` keeps the
  ``actionGeometry`` row path).

Labels come from the dumper's recipe (``devtools.ui_layout_dump._text_of``,
which resolves toolkit ``_text``) and fall back to the bound action's resolved
label when the widget carries no text of its own. Degenerate (zero-width or
zero-height) widgets are dropped from the inventory; hidden widgets with a real
rect stay with ``visible=false``.

**Scope.** ``scope`` is a **tab id**: elements are filtered to the subtree of
``TabRegistry().get_page(scope)``. A scope that is not an open tab returns an
empty list (never an error).

**Narrowing and content identity.** ``ui_elements`` also accepts ``role``
(exact), ``label`` (case-insensitive substring), ``properties`` (every entry
must equal the element's own property) and ``limit`` (≤
:data:`MAX_ELEMENT_LIMIT`); filters narrow in tree order and never change an
id. Each row carries a ``properties`` bag of the widget's serialisable
dynamic properties (``gallery_path`` for gallery cards) — see
``query.scalar_properties`` for the rule. The same bag backs the
``property:<name>=<value>`` reference the live tools accept in their ``id``
position: it is stable across tree reordering, unlike a positional id.

**Point targeting.** The ``id`` slot also accepts ``point:<x>,<y>`` in
**global screen coordinates** — the space ``screen_rect`` is in. It resolves
the widget a real pointer at that point would reach (``QApplication.widgetAt``,
which skips ``WA_TransparentForMouseEvents`` children), then the verb's normal
delivery applies: that widget gets the event, and an unaccepted wheel falls
through to the owning ``QAbstractScrollArea`` (``raw=true`` stops on the hit
widget). A widget the inventory never lists — a paint-only timeline band, an
overlay — is therefore addressable without the control layer naming a tab or a
widget class. An outside point is a named error listing the live window
bounds; it never falls back to the window.

GUI thread only: every function here touches live widgets and must run where
they live (the control server dispatches on the GUI thread). No threads.
"""

from __future__ import annotations

from typing import Any

from ..discovery import MethodError

from .inventory import (
    Element,
    collect_elements,
    inventory_payload,
    rect_list,
    screen_rect_of,
)
from .interaction import click_element, focus_element, hover_element, wheel_element
from .query import MAX_ELEMENT_LIMIT, filter_elements
from .resolve import POINT_PREFIX, PROPERTY_PREFIX, focus_target, key_target, resolve_element

#: Retry budget for an id that is not realized yet (FilmCraft's RETRY semantics:
#: an agent need not sleep between "open" and "click"). Read at call time by
#: ``inventory.resolve_element`` so tests can monkeypatch this attribute.
RETRY_TIMEOUT_MS = 3000

#: Step between inventory rescans inside the retry loop (event loop pumped).
RETRY_STEP_MS = 25


class ElementMethodsMixin:
    """The four element methods, mixed into ``ControlHandlers``.

    They live beside the logic they orchestrate so ``handlers.py`` stays a thin
    dispatch layer. ``self`` is a ``ControlHandlers`` (``_registry``/``_app``
    provided by the host class).
    """

    def ui_elements(self, params: dict[str, Any]) -> dict[str, Any]:
        properties = params.get("properties")
        if properties is not None and not isinstance(properties, dict):
            raise MethodError(
                "ui_elements 'properties' must be an object of name→value pairs",
                'e.g. properties={"gallery_path": "/gallery/card_0.png"}',
            )
        collected = collect_elements(
            registry=self._registry(),  # type: ignore[attr-defined]
            scope=params.get("scope"),
            include_disabled=bool(params.get("include_disabled", False)),
        )
        rows, matched = filter_elements(
            collected,
            role=params.get("role"),
            label=params.get("label"),
            properties=properties,
            limit=params.get("limit"),
        )
        return inventory_payload(rows, total=matched)

    def ui_click(self, params: dict[str, Any]) -> dict[str, Any]:
        target = resolve_element(self._registry(), params.get("id"))  # type: ignore[attr-defined]
        click_element(
            target,
            button=str(params.get("button") or "left"),
            count=params.get("count", 1),
        )
        return {
            "clicked": True,
            "id": target.id,
            "rect": rect_list(target.rect),
            "screen_rect": rect_list(target.screen_rect),
            "target": target.target,
            "surface": target.surface,
        }

    def ui_wheel(self, params: dict[str, Any]) -> dict[str, Any]:
        """Deliver wheel events to an element (scrolling, zoom, smooth streams).

        ``id`` is an inventory id, a ``property:<name>=<value>`` reference, or
        a ``point:<x>,<y>`` global screen point (the space ``screen_rect`` is
        in) that resolves the widget a real pointer would reach — a canvas the
        inventory skipped is still addressable. ``phase`` reproduces
        high-resolution wheels; ``ctrl``/``shift``/``alt`` carry modifiers,
        which every zoom gesture needs (an Image Compare canvas zooms on
        ``Ctrl+wheel`` only); ``raw`` keeps the event on the element instead of
        falling through to the owning scroll area. A canvas is never
        re-routed: it consumes the wheel itself.
        """
        from PySide6.QtCore import Qt

        target = resolve_element(self._registry(), params.get("id"))  # type: ignore[attr-defined]
        try:
            delta = int(params.get("delta", 120))
            count = int(params.get("count", 1))
        except (TypeError, ValueError):
            raise MethodError(
                "ui_wheel 'delta' and 'count' must be integers",
                "delta is angle units of one notch (120 = one step); count repeats it",
            )
        phase = str(params.get("phase") or "none")
        raw = bool(params.get("raw", False))
        modifiers = Qt.KeyboardModifier.NoModifier
        held: list[str] = []
        if params.get("ctrl"):
            modifiers |= Qt.KeyboardModifier.ControlModifier
            held.append("ctrl")
        if params.get("shift"):
            modifiers |= Qt.KeyboardModifier.ShiftModifier
            held.append("shift")
        if params.get("alt"):
            modifiers |= Qt.KeyboardModifier.AltModifier
            held.append("alt")
        wheel_element(
            target,
            delta=delta,
            phase=phase,
            count=count,
            raw=raw,
            modifiers=modifiers,
        )
        return {
            "sent": max(1, count),
            "delta": delta,
            "phase": phase.lower(),
            "raw": raw,
            "modifiers": held,
            "id": target.id,
            "rect": rect_list(target.rect),
            "target": target.target,
            "surface": target.surface,
        }

    def ui_hover(self, params: dict[str, Any]) -> dict[str, Any]:
        target = resolve_element(self._registry(), params.get("id"))  # type: ignore[attr-defined]
        hover_element(target)
        return {
            "hovered": True,
            "id": target.id,
            "rect": rect_list(target.rect),
            "target": target.target,
        }

    def ui_focus(self, params: dict[str, Any]) -> dict[str, Any]:
        target = resolve_element(self._registry(), params.get("id"))  # type: ignore[attr-defined]
        focus_element(target)
        from PySide6.QtWidgets import QApplication

        instance = getattr(self, "_app", None) or QApplication.instance()
        focused = None
        if instance is not None:
            try:
                focused = instance.focusWidget()
            except Exception:
                focused = None
        label = ""
        if focused is not None:
            try:
                label = f"{type(focused).__name__}:{focused.objectName()}"
            except Exception:
                label = type(focused).__name__
        return {
            "focused": True,
            "id": target.id,
            "rect": rect_list(target.rect),
            "target": target.target,
            "focus_widget": label,
        }


__all__ = [
    "Element",
    "ElementMethodsMixin",
    "MAX_ELEMENT_LIMIT",
    "POINT_PREFIX",
    "PROPERTY_PREFIX",
    "RETRY_STEP_MS",
    "RETRY_TIMEOUT_MS",
    "click_element",
    "collect_elements",
    "filter_elements",
    "focus_element",
    "hover_element",
    "wheel_element",
    "inventory_payload",
    "focus_target",
    "key_target",
    "rect_list",
    "resolve_element",
    "screen_rect_of",
]
