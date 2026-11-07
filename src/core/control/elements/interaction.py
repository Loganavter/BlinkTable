"""In-process pointer delivery at an element's position (no OS-level injection).

``QTest``/``QApplication.sendEvent`` on the GUI thread, exactly where a real
input event lands. A user right-click is a mouse press/release *and* a
context-menu event, so :func:`click_element` delivers both — a widget that acts
on the mouse path and one that uses ``CustomContextMenu`` behave identically.

An element resolved from a ``point:<x>,<y>`` reference is the widget a real
pointer at that global point reaches, and its events land there; inventory
rows keep the historical centre convention. A point-addressed wheel goes to
that widget first and only falls through to the owning ``QAbstractScrollArea``
when the widget ignores it (see :func:`wheel_element`).
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtGui import QContextMenuEvent, QEnterEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from ..discovery import MethodError
from .inventory import Element

#: ``ui_click`` button names → ``Qt.MouseButton``.
_BUTTONS = {
    "left": Qt.MouseButton.LeftButton,
    "right": Qt.MouseButton.RightButton,
    "middle": Qt.MouseButton.MiddleButton,
}


def _send_context_menu(widget: QWidget, pos: QPoint) -> None:
    """Send the context-menu event Qt's platform layer adds to a right-click."""
    try:
        event = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, pos, widget.mapToGlobal(pos)
        )
    except Exception:
        return
    QApplication.sendEvent(widget, event)


def widget_at_global(point: QPoint) -> QWidget | None:
    """The widget a real pointer at global *point* would reach, or ``None``.

    ``QApplication.widgetAt`` is the same hit test the platform layer uses: it
    descends the top-level under the point in z-order, skipping children marked
    ``WA_TransparentForMouseEvents`` (a paint-only overlay is exactly that, and
    a wheel aimed at it must land on the widget behind it) and returns ``None``
    when no live window covers the point. Global screen coordinates are the
    space a ``ui_elements`` row's ``screen_rect`` is in.
    """
    try:
        widget = QApplication.widgetAt(point)
    except Exception:
        return None
    return widget if isinstance(widget, QWidget) else None


def _element_local_pos(element: Element, widget: QWidget) -> QPoint:
    """Event position for *element* inside *widget*.

    An element resolved from a ``point:<x>,<y>`` reference carries that global
    point and a pointer event must land there (a widget may zoom around the
    cursor, not its centre). Inventory rows carry no point and keep the
    historical widget-local centre.
    """
    point = getattr(element, "point", None)
    if point is not None:
        try:
            return widget.mapFromGlobal(point)
        except Exception:
            pass
    return element.rect.center()


def click_element(element: Element, *, button: str = "left", count: int = 1) -> None:
    """Deliver a click to *element* in-process, at its widget-local centre."""
    qt_button = _BUTTONS.get(str(button).lower())
    if qt_button is None:
        raise MethodError(
            f"unknown mouse button {button!r}", "use one of: left, right, middle"
        )
    try:
        clicks = max(1, int(count))
    except (TypeError, ValueError):
        clicks = 1
    widget = element.widget
    pos = _element_local_pos(element, widget)
    if clicks == 2:
        QTest.mouseDClick(widget, qt_button, pos=pos)
        if qt_button == Qt.MouseButton.RightButton:
            _send_context_menu(widget, pos)
        return
    for _ in range(clicks):
        QTest.mouseClick(widget, qt_button, pos=pos)
        if qt_button == Qt.MouseButton.RightButton:
            _send_context_menu(widget, pos)


def hover_element(element: Element) -> None:
    """Move the pointer over *element* and deliver a real enter event."""
    widget = element.widget
    pos = _element_local_pos(element, widget)
    try:
        QTest.mouseMove(widget, pos)
    except Exception:
        pass
    try:
        global_pos = widget.mapToGlobal(pos)
        local = QPointF(pos)
        QApplication.sendEvent(
            widget, QEnterEvent(local, local, QPointF(global_pos))
        )
    except Exception:
        pass


def focus_element(element: Element) -> None:
    """Give keyboard focus to *element*'s widget."""
    try:
        element.widget.setFocus(Qt.FocusReason.OtherFocusReason)
    except Exception as exc:
        raise MethodError(
            f"could not focus {element.id!r}: {exc}",
            "the element may not accept keyboard focus",
        )


def wheel_target(widget, *, raw: bool = False):
    """The widget a wheel over *widget* must be delivered to.

    A wheel over an element means "scroll whatever owns it": the owning
    ``QAbstractScrollArea`` handles the wheel on its VIEWPORT, and an
    interactive child may accept-and-ignore it first (a toolkit Button returns
    without scrolling), so the event is routed to the nearest ancestor scroll
    area's viewport. Two exceptions keep the event where it belongs:
    ``raw=True`` (the caller knows the widget consumes the wheel) and a live
    RHI canvas, which owns its ``Ctrl+wheel`` zoom and must never be routed to
    an ancestor.
    """
    if raw or _is_canvas_widget(widget):
        return widget
    try:
        from PySide6.QtWidgets import QAbstractScrollArea

        area = widget if isinstance(widget, QAbstractScrollArea) else None
        parent = widget.parentWidget() if area is None else None
        while area is None and parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                area = parent
                break
            parent = parent.parentWidget()
        if area is not None:
            return area.viewport() or area
    except Exception:
        return widget
    return widget


def _is_canvas_widget(widget) -> bool:
    """True for a live RHI canvas — it consumes the wheel itself.

    Routing a canvas' wheel to an ancestor scroll area (the default for every
    other element) would steal the ``Ctrl+wheel`` zoom the canvas owns.
    """
    try:
        from PySide6.QtWidgets import QRhiWidget

        return isinstance(widget, QRhiWidget)
    except Exception:
        return False


#: ``ui_wheel`` phase names → Qt scroll phase (the toolkit's glide keys off it).
_WHEEL_PHASES = {
    "none": Qt.ScrollPhase.NoScrollPhase,
    "begin": Qt.ScrollPhase.ScrollBegin,
    "update": Qt.ScrollPhase.ScrollUpdate,
    "end": Qt.ScrollPhase.ScrollEnd,
}


def _send_wheel(
    widget: QWidget,
    element: Element,
    *,
    delta: int,
    phase: Qt.ScrollPhase,
    modifiers: Qt.KeyboardModifier,
) -> bool:
    """Send one wheel event at *element*'s position and report if it was taken.

    The event is re-created per receiver because delivery may be attempted on
    two widgets (the hit widget, then its owning scroll area) and one event
    object carries the first receiver's accepted state.
    """
    pos = _element_local_pos(element, widget)
    event = QWheelEvent(
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        QPoint(0, 0),
        QPoint(0, int(delta)),
        Qt.MouseButton.NoButton,
        modifiers,
        phase,
        False,
    )
    QApplication.sendEvent(widget, event)
    return bool(event.isAccepted())


def wheel_element(
    element: Element,
    *,
    delta: int = 120,
    phase: str = "none",
    count: int = 1,
    raw: bool = False,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    """Deliver wheel events to *element* in-process (one angle-delta step each).

    ``phase`` selects the Qt scroll phase: ``none`` (a plain notch, what a
    discrete mouse wheel sends), ``begin``/``update``/``end`` (what touchpads
    and smooth-scroll compositors send). High-resolution wheels mark every
    event ``begin``, which is the stream that used to starve the scroll glide
    — this verb is how a caller reproduces it without a physical device.

    An **id-addressed** element is routed straight to the owning
    ``QAbstractScrollArea``'s viewport (``wheel_target``): an inventory row
    names a control, not the spot under a cursor, and a toolkit Button can
    accept-and-ignore the wheel without scrolling.

    A **point-addressed** element (``point:<x>,<y>``) follows the real pointer
    path instead: the widget under the point gets the event first, and only an
    *unaccepted* wheel falls through to the owning scroll area. That is what
    lets a timeline's own ``Ctrl+wheel`` zoom run (it lives on the widget under
    the cursor) while a plain background still scrolls its area.
    ``raw=true`` keeps the event on the hit widget either way.
    """
    qt_phase = _WHEEL_PHASES.get(str(phase or "none").lower())
    if qt_phase is None:
        raise MethodError(
            f"unknown wheel phase {phase!r}",
            "use one of: " + ", ".join(sorted(_WHEEL_PHASES)),
        )
    hit = element.widget
    if getattr(element, "point", None) is not None and not raw:
        owner = wheel_target(hit)
        receivers = [hit] if owner is hit else [hit, owner]
    else:
        receivers = [wheel_target(hit, raw=raw)]
    for _ in range(max(1, int(count))):
        for receiver in receivers:
            if _send_wheel(
                receiver,
                element,
                delta=delta,
                phase=qt_phase,
                modifiers=modifiers,
            ):
                break

