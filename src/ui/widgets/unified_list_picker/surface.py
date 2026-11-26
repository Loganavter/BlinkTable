"""Token-painted flyout container surface — retired QSS replacement.

The unified picker's container was styled by the ``QWidget#FlyoutWidget``
rules in base.qss / widgets.qss (now deleted). A plain ``QWidget`` could
only paint those rules because Qt sets ``WA_StyledBackground`` for the
exact ``QWidget`` class; app QSS rules silently no-op on custom subclasses
(the Settings sidebar black-substrate bug). Painting the surface from the
theme tokens in ``paintEvent`` (THEMING.md's explicit-paint pattern) makes
the container own its chrome like ``ThemedSurface`` / ``IconListWidget`` do.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QWidget

from sli_ui_toolkit.managers import scaled_px
from sli_ui_toolkit.widgets import ThemedWidget

from ui.theming import try_resolve_theme_color


class FlyoutSurfaceWidget(ThemedWidget, QWidget):
    """Container surface: ``flyout.background`` fill + 1px ``flyout.border``.

    Corner radius is ``scaled_px(8)`` (the retired QSS radius, px-scaled by
    the same factor the QSS px pass applied). When the ``surfaceRole``
    dynamic property is ``"transparent"`` (double-list mode — the panels own
    their surfaces) nothing is painted, so the picker's shadow pass shows
    through. The property is re-read on every paint; the widget also
    repaints on ``DynamicPropertyChange`` so ``setProperty`` alone re-renders.
    """

    RADIUS_PX = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        self._background_color = QColor()
        self._border_color = QColor()
        super().__init__(parent)

    def on_theme_changed(self) -> None:
        """Re-read the flyout tokens; fall back to the palette Window role."""
        try:
            bg = try_resolve_theme_color(self._theme_manager, "flyout.background")
            border = try_resolve_theme_color(self._theme_manager, "flyout.border")
        except Exception:
            bg = border = None
        fallback = QColor(self.palette().window().color())
        self._background_color = (
            QColor(bg) if bg is not None and bg.isValid() else fallback
        )
        self._border_color = (
            QColor(border)
            if border is not None and border.isValid()
            else QColor(fallback).darker(120)
        )
        super().on_theme_changed()

    def event(self, event) -> bool:  # noqa: N802 - Qt override
        if (
            event.type() == QEvent.Type.DynamicPropertyChange
            and event.propertyName() == b"surfaceRole"
        ):
            self.update()
        return super().event(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.property("surfaceRole") == "transparent":
            return
        radius = scaled_px(self.RADIUS_PX)
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
            radius,
            radius,
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self._border_color, 1))
        painter.setBrush(self._background_color)
        painter.drawPath(path)
        painter.end()


def pin_scroll_area_transparency(root: QWidget) -> None:
    """Pin scroll-area backgrounds transparent under *root*.

    ``QScrollArea.setWidget`` flips ``autoFillBackground`` on for the
    content widget and the viewport autofills by default — both would paint
    the QPalette Base/Window role as a solid rectangle over the token-painted
    flyout surface (same quirk the toolkit ``IconListWidget`` fix pins, and
    what the retired ``QWidget#FlyoutWidget QScrollArea`` QSS rule used to
    neutralize). Call after the widget tree under *root* is built; the
    picker runs it once after panel construction.
    """
    for area in root.findChildren(QAbstractScrollArea):
        area.setAutoFillBackground(False)
        viewport = area.viewport()
        if viewport is not None:
            viewport.setAutoFillBackground(False)
        content = area.widget()
        if content is not None:
            content.setAutoFillBackground(False)