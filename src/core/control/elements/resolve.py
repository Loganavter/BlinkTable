"""Id → ``Element`` resolution with retry-until-rendered semantics.

An unknown/not-yet-realized element is retried for ~3 s (rescanning the
inventory, pumping the event loop) before failing — so an agent need not sleep
between "open" and "click". A failure raises ``MethodError`` listing close
matches and a hint to call ``ui_elements``.
"""

from __future__ import annotations

import difflib
import time
from typing import Any

from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from ..discovery import MethodError
from .inventory import (
    Element,
    _is_popup_window,
    _make_element,
    _top_level_widgets,
    _window_keys,
    collect_elements,
)
from .query import property_value_matches
from .roles import _label_of, _role_of

#: Prefix of a stable property reference (``property:<name>=<value>``). The
#: reference live-only tools accept in the same ``id`` position as an
#: inventory id, so an agent need not hold a positional child-index path.
PROPERTY_PREFIX = "property:"

#: Prefix of a screen-point reference (``point:<x>,<y>``). Same ``id`` slot:
#: it names the widget a real pointer at that global point would reach, so a
#: verb can act on a widget the inventory never registered (a paint-only
#: timeline band, a mouse-transparent overlay) without the control layer
#: naming a tab or widget class.
POINT_PREFIX = "point:"


def _parse_point_needle(needle: str) -> QPoint | None:
    """``point:<x>,<y>`` → ``QPoint`` (global screen), else ``None``.

    Malformed (no comma, empty/float/non-numeric halves) is a named error
    immediately: it can never start matching later, and a wheel silently aimed
    at a typo'd position would be blamed on the widget.
    """
    if not needle.startswith(POINT_PREFIX):
        return None
    body = needle[len(POINT_PREFIX) :].strip()
    x_text, separator, y_text = body.partition(",")
    try:
        if not separator:
            raise ValueError("no comma")
        return QPoint(int(x_text.strip()), int(y_text.strip()))
    except (ValueError, OverflowError):
        raise MethodError(
            f"point target is malformed (needs x,y): {needle!r}",
            "point:<x>,<y> takes INTEGER global screen coordinates, "
            "e.g. point:640,480 — the space a ui_elements row's screen_rect is in",
        ) from None


def _window_bounds_hint() -> str:
    """Visible top-level windows as ``key=[x, y, w, h]`` (global), for a miss."""
    windows = _top_level_widgets()
    bounds: list[str] = []
    for window, key in zip(windows, _window_keys(windows)):
        try:
            if not window.isVisible():
                continue
            rect = window.frameGeometry()
        except Exception:
            continue
        bounds.append(
            f"{key}=[{rect.x()}, {rect.y()}, {rect.width()}, {rect.height()}]"
        )
    return "; ".join(bounds) if bounds else "no visible windows"


def _element_at_point(point: QPoint) -> Element:
    """The ``Element`` for the widget a real pointer at global *point* reaches.

    One attempt, no retry budget: a point is not an id that gets realized later
    — if no live window covers it, the caller needs the live window bounds (and
    a fresh ``ui_elements``) rather than three seconds of the same miss.
    """
    from .interaction import widget_at_global

    widget = widget_at_global(point)
    if widget is None or not widget.isVisible():
        raise MethodError(
            f"no widget at point ({point.x()}, {point.y()})",
            "point:<x>,<y> takes GLOBAL screen coordinates — the space of a "
            "ui_elements row's screen_rect (add a widget-local offset from rect "
            "to it); no live window covers that point. Live windows: "
            + _window_bounds_hint(),
        )
    return _make_element(
        widget=widget,
        element_id=f"{POINT_PREFIX}{point.x()},{point.y()}",
        role=_role_of(widget) or "widget",
        label=_label_of(widget, None),
        rect=widget.rect(),
        window=_window_key_for(widget),
        surface="popup" if _is_popup_window(widget.window()) else "window",
        action=None,
        point=point,
    )


def _parse_property_needle(needle: str) -> tuple[str, str] | None:
    """``property:<name>=<value>`` → ``(name, value)``, else ``None``.

    A malformed prefix (no ``=``, empty name) is a named error immediately: it
    can never start matching later, so retrying it would only waste the budget.
    """
    if not needle.startswith(PROPERTY_PREFIX):
        return None
    body = needle[len(PROPERTY_PREFIX) :]
    name, separator, value = body.partition("=")
    if not separator or not name:
        raise MethodError(
            f"{PROPERTY_PREFIX} target is malformed (needs name=value): {needle!r}",
            "property target needs name=value, e.g. property:gallery_path=/gallery/card_0.png",
        )
    return name, value


def _matches_property(element: Element, name: str, value: str) -> bool:
    """True when *element*'s own property *name* equals *value*."""
    properties = getattr(element, "properties", None)
    if not isinstance(properties, dict) or name not in properties:
        return False
    return property_value_matches(properties[name], value)


def _window_key_for(widget: QWidget) -> str:
    """Window identity of *widget*'s top-level, matching ``collect_elements``."""
    windows = _top_level_widgets()
    keys = _window_keys(windows)
    try:
        top = widget.window()
    except Exception:
        top = widget
    for window, key in zip(windows, keys):
        if window is top:
            return key
    try:
        return str(top.objectName() or type(top).__name__ or "window")
    except Exception:
        return "window"


def _resolve_via_action(registry: Any, needle: str) -> Element | None:
    """Action-first fallback: the registry's bound widget for ``@<action id>``."""
    if "@" not in needle:
        return None
    action_id = needle.rsplit("@", 1)[-1]
    try:
        action = registry.get(action_id)
    except Exception:
        action = None
    if action is None:
        return None
    target = getattr(action, "target", None)
    try:
        widget = target.resolve_widget_instance() if target is not None else None
    except Exception:
        widget = None
    if not isinstance(widget, QWidget):
        return None
    return _make_element(
        widget=widget,
        element_id=needle,
        role=_role_of(widget) or "action",
        label=_label_of(widget, action),
        rect=widget.rect(),
        window=_window_key_for(widget),
        surface="popup" if _is_popup_window(widget.window()) else "window",
        action=action,
    )


def _property_hint(elements: list[Element], name: str, value: str) -> str:
    """Actionable hint for a property reference that never matched.

    A typo'd name gets close known names; a real name gets the values that do
    exist, so the client can correct the reference without another full dump.
    """
    known: set[str] = set()
    values: set[str] = set()
    for element in elements:
        properties = getattr(element, "properties", None)
        if not isinstance(properties, dict):
            continue
        known.update(str(key) for key in properties)
        if name in properties:
            values.add(str(properties[name]))
    if name not in known:
        close = difflib.get_close_matches(name, sorted(known), n=5, cutoff=0.5)
        hint = "no element exposes that property"
        if close:
            hint = "did you mean: " + ", ".join(close) + "? (" + hint + ")"
        return hint + "; call ui_elements — rows carry a properties bag"
    shown = sorted(values)[:5]
    hint = f"property {name!r} exists but no element has that value"
    if shown:
        hint += "; known values: " + ", ".join(shown)
    return hint + "; call ui_elements to list the current rows"


def resolve_element(
    registry: Any, needle: Any, *, timeout_ms: int | None = None
) -> Element:
    """Resolve *needle* to an element, retrying until the UI realizes it.

    *needle* is an inventory id, ``@<action id>``, a stable property
    reference ``property:<name>=<value>`` (e.g.
    ``property:gallery_path=/gallery/card_0.png``), or a screen-point
    reference ``point:<x>,<y>``. The property form matches the element's own
    ``properties`` bag, so it survives tree reordering that changes positional
    ids; the first match in tree order wins. The point form resolves the widget
    a real pointer at that **global** point (the space of ``screen_rect``)
    would reach, via ``QApplication.widgetAt``, so a widget the inventory never
    lists — a paint-only timeline band, a mouse-transparent overlay — is
    addressable without naming a tab or class; it does not retry (a miss means
    no live window covers the point).

    Raises ``MethodError`` (with close matches + a ``ui_elements`` hint) when
    the id never appears within the retry budget. The event loop is pumped
    between rescans, so a widget created by a queued/timer callback is found.
    """
    from core.control import elements as package

    if not isinstance(needle, str) or not needle.strip():
        raise MethodError(
            "ui_* needs a non-empty 'id'",
            "call ui_elements and pass one of the returned ids verbatim",
        )
    point_needle = _parse_point_needle(needle)
    if point_needle is not None:
        return _element_at_point(point_needle)
    property_needle = _parse_property_needle(needle)
    budget = package.RETRY_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)
    step = int(getattr(package, "RETRY_STEP_MS", 25))
    deadline = time.monotonic() + max(0, budget) / 1000.0
    elements: list[Element] = []
    while True:
        elements = collect_elements(registry=registry)
        if property_needle is not None:
            name, value = property_needle
            for element in elements:
                if _matches_property(element, name, value):
                    return element
        else:
            for element in elements:
                if element.id == needle:
                    return element
            fallback = _resolve_via_action(registry, needle)
            if fallback is not None:
                return fallback
        if time.monotonic() >= deadline:
            break
        try:
            QTest.qWait(step)
        except Exception:
            time.sleep(step / 1000.0)
    if property_needle is not None:
        name, value = property_needle
        raise MethodError(
            f"no element with property {name}={value!r}",
            _property_hint(elements, name, value),
        )
    candidates = [element.id for element in elements]
    close = difflib.get_close_matches(needle, candidates, n=5, cutoff=0.5)
    hint = "call ui_elements to list the current ids"
    if close:
        hint = "did you mean: " + ", ".join(close) + "? (" + hint + ")"
    raise MethodError(f"no element with id {needle!r}", hint)


def key_target(registry: Any, needle: Any, *, fallback: Any = None) -> Any:
    """The widget ``press_keys`` should type into.

    A named element id (from ``ui_elements``) types into **that** widget
    directly. Without this, keys always went to the window's focus widget,
    which is not where a canvas listens: a live QRhi canvas is not in the
    window's focus chain, so canvas shortcuts (multi-compare zoom ``=``/``-``,
    reset ``0``) were delivered to ``MainWindow`` and silently did nothing
    (live 2026-10-17 -- the whole point of naming the canvas is that keys must
    be able to reach it). No id keeps the old behaviour (``fallback``).
    """
    if needle is None or (isinstance(needle, str) and not needle.strip()):
        return fallback
    return resolve_element(registry, needle).widget


def focus_target(app: Any, window: Any) -> Any:
    """The active window's focus widget, else *window*.

    ``press_keys``' default when no element id is named: keys go to whatever
    holds focus in the running window (the historical behaviour), falling
    back to the main window so a window-level shortcut still lands. Never
    raises.
    """
    from PySide6.QtWidgets import QApplication, QWidget

    instance = app if app is not None else QApplication.instance()
    focused = None
    if instance is not None:
        try:
            active = instance.activeWindow()
            focused = active.focusWidget() if active is not None else None
        except Exception:
            focused = None
    if focused is None and isinstance(window, QWidget):
        focused = window
    return focused
