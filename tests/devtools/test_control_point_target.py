"""``point:<x>,<y>`` targets — aim a pointer verb at a place, not at an id.

The control surface could only address **registered** elements (``ui_elements``
rows). A live-only widget the inventory never lists — the video editor's
paint-only timeline band, a mouse-transparent overlay laid over a canvas — had
no address at all, so the whole class of "aim a wheel/click at this part of the
live window" verification was impossible (measured live 2026-11-06: a wheel
addressed to the band left the ruler unchanged over ten notches in both
directions, because the band is a mouse-transparent child and the timeline
widget is not an element).

Pinned here (red against today's resolver, which only matches ids/properties):

* a ``point:<x>,<y>`` reference (global screen coordinates — the space a
  ``ui_elements`` row's ``screen_rect`` is in) resolves the widget a real
  pointer at that point reaches, through ``QApplication.widgetAt``;
* the wheel lands on the **child under the point**, not on its container, and
  carries that same position (a widget may zoom around the cursor);
* a wheel over a ``WA_TransparentForMouseEvents`` child lands on the widget
  behind it — the exact live geometry of the timeline band;
* a point-addressed wheel reaches the widget under the point first and falls
  through to the owning ``QAbstractScrollArea`` only when that widget ignores
  it (a plain background scrolls; a widget owning ``Ctrl+wheel`` zoom does
  not get its event stolen); ``raw=true`` stops on the hit widget;
* an outside/malformed point is a named ``MethodError`` carrying the live
  window bounds — never a silent delivery to the window;
* the id-addressed form is unchanged: an inventory id still resolves and its
  wheel lands at the widget-local centre (no ``point`` on the element).

Runs offscreen only — no Xvfb (the supervisor runs the live proof).
"""

from __future__ import annotations

import pytest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QApplication,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.control.handlers import ControlHandlers, MethodError
from core.control.elements import collect_elements, resolve_element, wheel_element
from core.control.elements.interaction import click_element


class _WheelRecorder(QWidget):
    """A widget that records every wheel event it receives.

    ``accept=False`` leaves Qt's default ``event.ignore()`` in place — the
    plain-background case whose wheel must fall through to the owning scroll
    area. ``accept=True`` is the timeline case: the widget under the cursor
    owns the wheel (``Ctrl+wheel`` zoom), so the area must not also scroll.
    """

    def __init__(self, parent: QWidget | None = None, *, accept: bool = True) -> None:
        super().__init__(parent)
        self.positions: list[QPoint] = []
        self.accept = accept

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt spelling
        self.positions.append(event.position().toPoint())
        if self.accept:
            event.accept()
        else:
            event.ignore()


class _WheelButton(QPushButton):
    """An inventory-recognised widget (role ``button``) that records wheels."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("wheel", parent)
        self.positions: list[QPoint] = []

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt spelling
        self.positions.append(event.position().toPoint())
        event.accept()


class _ClickRecorder(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.positions: list[QPoint] = []

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt spelling
        self.positions.append(event.position().toPoint())
        event.accept()


class _Registry:
    """Minimal action registry for ``resolve_element`` (no actions here)."""

    def all_actions(self):
        return []

    def get(self, action_id):  # noqa: ANN001 - protocol stub
        return None


def _pump() -> None:
    for _ in range(3):
        QApplication.processEvents()


@pytest.fixture()
def window(qapp):
    widget = QWidget()
    widget.setGeometry(120, 80, 400, 300)
    widget.show()
    _pump()
    try:
        yield widget
    finally:
        widget.close()
        widget.deleteLater()
        _pump()


def _point_ref(widget: QWidget, local: QPoint) -> str:
    """Global ``point:<x>,<y>`` reference for *local* inside *widget*."""
    global_point = widget.mapToGlobal(local)
    return f"point:{global_point.x()},{global_point.y()}"


# -- the point reaches the child under it, not the container -------------------


def test_point_wheel_reaches_the_child_under_the_point(window: QWidget) -> None:
    container = _WheelRecorder(window)
    container.setGeometry(0, 0, 400, 300)
    left = _WheelRecorder(container)
    left.setGeometry(0, 0, 200, 300)
    right = _WheelRecorder(container)
    right.setGeometry(200, 0, 200, 300)
    for widget in (container, left, right):
        widget.show()
    _pump()

    element = resolve_element(None, _point_ref(right, QPoint(50, 60)))
    assert element.widget is right, (
        "the point resolved to the container/sibling instead of the child under it"
    )
    wheel_element(element, delta=-120, count=2)

    assert len(right.positions) == 2, "the child under the point saw no wheel"
    assert right.positions[-1] == QPoint(50, 60), (
        "the wheel was delivered at the widget centre instead of the aimed point"
    )
    assert not left.positions, "the sibling under no point received the wheel"
    assert not container.positions, "the container swallowed a child-addressed wheel"


def test_point_click_lands_at_the_aimed_point(window: QWidget) -> None:
    """The same reference works for click (where cheap, a click position)."""
    child = _ClickRecorder(window)
    child.setGeometry(0, 0, 400, 300)
    child.show()
    _pump()

    element = resolve_element(None, _point_ref(child, QPoint(120, 90)))
    click_element(element)

    assert child.positions == [QPoint(120, 90)], (
        "a point-addressed click did not land where it was aimed"
    )


# -- mouse-transparent overlays -------------------------------------------------


def test_point_wheel_over_a_transparent_child_lands_behind(window: QWidget) -> None:
    """A paint-only overlay must not eat the wheel — the live band geometry."""
    behind = _WheelRecorder(window)
    behind.setGeometry(0, 0, 400, 300)
    overlay = _WheelRecorder(window)
    overlay.setGeometry(20, 20, 200, 120)
    overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    for widget in (behind, overlay):
        widget.show()
    _pump()

    element = resolve_element(None, _point_ref(overlay, QPoint(40, 40)))
    assert element.widget is behind, (
        "a wheel aimed at a WA_TransparentForMouseEvents child did not fall "
        "through to the widget behind it"
    )
    wheel_element(element, delta=120)
    assert len(behind.positions) == 1, "the widget behind a transparent child got no wheel"
    assert not overlay.positions, "the mouse-transparent child received the wheel"


# -- owning scroll area routing, and raw ---------------------------------------


def _tall_scroll_area(
    parent: QWidget, *, accept: bool = True
) -> tuple[QScrollArea, QWidget, _WheelRecorder]:
    area = QScrollArea(parent)
    area.setGeometry(0, 0, 300, 200)
    host = QWidget()
    host.setFixedSize(600, 4000)
    QVBoxLayout(host)
    area.setWidget(host)
    area.setWidgetResizable(False)
    child = _WheelRecorder(host, accept=accept)
    child.setGeometry(0, 0, 280, 300)
    child.show()
    area.show()
    _pump()
    return area, host, child


def test_point_wheel_falls_through_an_ignoring_child_to_the_owner(
    window: QWidget,
) -> None:
    """A plain background widget ignores the wheel; the owning area scrolls."""
    area, _host, child = _tall_scroll_area(window, accept=False)
    _pump()

    element = resolve_element(None, _point_ref(child, QPoint(40, 40)))
    assert element.widget is child
    wheel_element(element, delta=-120, count=10)
    _pump()

    assert child.positions, "the widget under the point never saw the wheel"
    assert area.verticalScrollBar().value() > 0, (
        "an unaccepted point-addressed wheel did not fall through to the "
        "owning scroll area"
    )


def test_point_wheel_consumed_by_the_hit_widget_does_not_scroll_the_owner(
    window: QWidget,
) -> None:
    """The timeline case: the widget under the cursor owns the wheel (zoom)."""
    area, _host, child = _tall_scroll_area(window, accept=True)
    _pump()

    element = resolve_element(None, _point_ref(child, QPoint(40, 40)))
    wheel_element(element, delta=-120, count=3)
    _pump()

    assert len(child.positions) == 3, "the accepting widget under the point got no wheel"
    assert area.verticalScrollBar().value() == 0, (
        "an accepted point-addressed wheel was also delivered to the owning area"
    )


def test_point_wheel_raw_keeps_the_event_on_the_hit_widget(window: QWidget) -> None:
    area, _host, child = _tall_scroll_area(window, accept=False)
    _pump()

    element = resolve_element(None, _point_ref(child, QPoint(40, 40)))
    wheel_element(element, delta=-120, count=2, raw=True)
    _pump()

    assert len(child.positions) == 2, "raw=true did not keep the wheel on the hit widget"
    assert area.verticalScrollBar().value() == 0, "raw=true still scrolled the owner"


# -- named refusals --------------------------------------------------------------


def test_point_outside_every_window_fails_by_name(window: QWidget) -> None:
    with pytest.raises(MethodError) as excinfo:
        resolve_element(None, "point:9000,9000")

    message = str(excinfo.value)
    assert "no widget at point (9000, 9000)" in message
    hint = getattr(excinfo.value, "hint", "") or ""
    assert "screen_rect" in hint, "the refusal must name the coordinate space"
    assert "Live windows:" in hint, "the refusal must list the live window bounds"


def test_malformed_point_is_a_named_refusal() -> None:
    with pytest.raises(MethodError) as excinfo:
        resolve_element(None, "point:12")
    assert "malformed" in str(excinfo.value)


# -- the existing id-addressed form is unchanged --------------------------------


def test_id_addressed_wheel_still_lands_at_the_widget_centre(window: QWidget) -> None:
    target = _WheelButton(window)
    target.setGeometry(10, 10, 200, 100)
    target.show()
    _pump()

    rows = [
        element
        for element in collect_elements(registry=_Registry())
        if element.widget is target
    ]
    assert rows, "the wheel recorder was not collected into the inventory"
    inventory_id = rows[0].id

    element = resolve_element(None, inventory_id)
    assert element.widget is target
    assert element.point is None, "an inventory row must not carry a point reference"

    wheel_element(element, delta=-120)
    assert target.positions == [element.rect.center()], (
        "an id-addressed wheel no longer lands at the widget-local centre"
    )


# -- the verb itself (handler end-to-end) ---------------------------------------


def test_ui_wheel_accepts_a_point_reference(window: QWidget, tmp_path) -> None:
    """The published verb takes the point in its ``id`` slot, unchanged schema."""
    child = _WheelRecorder(window)
    child.setGeometry(0, 0, 400, 300)
    child.show()
    _pump()

    handlers = ControlHandlers(
        window=window,
        app=QApplication.instance(),
        artifacts_dir=tmp_path / "artifacts",
        log_ring=None,
        registry_provider=lambda: _Registry(),
    )
    point_ref = _point_ref(child, QPoint(30, 40))
    result = handlers.ui_wheel({"id": point_ref, "delta": 120, "ctrl": True})

    assert result["id"] == point_ref
    assert result["target"] == "_WheelRecorder"
    assert child.positions == [QPoint(30, 40)]

