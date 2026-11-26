from __future__ import annotations

import logging

import shiboken6 as sip
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from shared_toolkit.ui.in_window_surface import (
    create_shadow_surface,
    paint_shadowed_surface,
)
from core.log_names import APP_LOGGER_NAME
from sli_ui_toolkit.managers import scaled_px
from sli_ui_toolkit.theme import ThemeManager
from sli_ui_toolkit.ui.managers.ui_font import apply_text_color, apply_ui_font

logger = logging.getLogger(APP_LOGGER_NAME)

class _PopupBubble(QWidget):
    SHADOW_RADIUS = 8
    CONTENT_RADIUS = 6

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._layout, self.container, self.content_layout = create_shadow_surface(
            self,
            shadow_radius=self.SHADOW_RADIUS,
            container_object_name="ValuePopupContainer",
            content_margins=(0, 0, 0, 0),
            content_spacing=0,
        )
        self._layout.setSpacing(0)
        # The bubble paints the container surface (flyout tokens, 1px border,
        # scaled radius) itself in paintEvent — the retired
        # `#ValuePopupContainer` QSS rule is gone, so the container must stay
        # transparent instead of styling itself.
        self.container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        self._theme_manager = ThemeManager.get_instance()
        self._surface_color = QColor()
        self._border_color = QColor()
        self._apply_surface_colors()
        try:
            self._theme_manager.theme_changed.connect(self._on_theme_changed)
        except Exception:
            pass

        self.label = QLabel(self.container)
        self.label.setObjectName("ValuePopupLabel")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setContentsMargins(scaled_px(4), scaled_px(4), scaled_px(4), scaled_px(4))
        self._apply_label_typography()
        self.content_layout.addWidget(self.label)
        self.hide()

    def _apply_label_typography(self) -> None:
        apply_ui_font(self.label, pixel_size=14, bold=True)
        apply_text_color(
            self.label, ThemeManager.get_instance().get_color("dialog.text")
        )

    def _apply_surface_colors(self) -> None:
        """Re-read the flyout tokens; fall back to the palette Window role."""
        try:
            bg = self._theme_manager.get_color("flyout.background")
            border = self._theme_manager.get_color("flyout.border")
        except Exception:
            bg = border = None
        fallback = QColor(self.palette().window().color())
        self._surface_color = (
            QColor(bg) if bg is not None and bg.isValid() else fallback
        )
        self._border_color = (
            QColor(border)
            if border is not None and border.isValid()
            else QColor(fallback).darker(120)
        )

    def _on_theme_changed(self, *_args) -> None:
        """Re-tint the surface after a theme switch. Bound method, so the
        connection dies with the widget."""
        try:
            self._apply_surface_colors()
            self.update()
        except RuntimeError:
            pass

    def set_content(
        self,
        *,
        text: str = "",
        pixmap: QPixmap | None = None,
        size: QSize | None = None,
    ) -> None:
        if pixmap is not None:
            self.label.setText("")
            self.label.setPixmap(pixmap)
        else:
            self.label.setPixmap(QPixmap())
            self.label.setText(text)

        if size is not None:
            self.label.setFixedSize(size)
        else:
            self.label.adjustSize()

        self.adjustSize()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_shadowed_surface(
            painter,
            self.container.geometry(),
            shadow_radius=self.SHADOW_RADIUS,
            corner_radius=self.CONTENT_RADIUS,
        )
        # Container chrome the retired `#ValuePopupContainer` QSS rule used
        # to paint: fill + 1px border at the scaled corner radius. Painted
        # here (under the container/label) because the container itself is
        # a plain transparent QWidget now.
        radius = scaled_px(self.CONTENT_RADIUS)
        painter.setPen(QPen(self._border_color, 1))
        painter.setBrush(self._surface_color)
        painter.drawRoundedRect(
            QRectF(self.container.geometry()).adjusted(0.5, 0.5, -0.5, -0.5),
            radius,
            radius,
        )
        painter.end()

def get_overlay_layer(widget: QWidget | None):
    current = widget
    while current is not None:
        overlay = getattr(current, "overlay_layer", None)
        if overlay is not None:
            return overlay
        current = current.parentWidget()

    if widget is not None:
        window = widget.window()
        return getattr(window, "overlay_layer", None)
    return None


def _resolve_content_host(widget: QWidget | None) -> QWidget | None:
    """workspace_content_widget below the tab strip, or None (startup)."""
    if widget is None or not sip.isValid(widget):
        return None
    def _valid(obj):
        return obj is not None and sip.isValid(obj)

    try:
        current: QWidget | None = widget
        while current is not None:
            host = getattr(current, "workspace_content_widget", None)
            if _valid(host):
                return host
            if _valid(getattr(current, "content_overlay_layer", None)):
                return current  # established content host carries its layer
            current = current.parentWidget()
        window = widget.window()
    except RuntimeError:
        return None
    if not _valid(window):
        return None
    for owner in (window, getattr(window, "ui", None)):
        host = getattr(owner, "workspace_content_widget", None) if owner is not None else None
        if _valid(host):
            return host
    return None


def get_content_overlay_layer(widget: QWidget | None) -> OverlayLayer | None:
    """Content layer (host = workspace_content_widget); window layer on startup."""
    host = _resolve_content_host(widget)
    if host is None:
        logger.warning(
            "get_content_overlay_layer: workspace_content_widget not built yet, "
            "falling back to the window overlay layer (startup)"
        )
        return get_overlay_layer(widget)
    cached = getattr(host, "content_overlay_layer", None)
    if cached is not None and sip.isValid(cached):
        return cached
    layer = OverlayLayer(host)
    layer._is_content_layer = True
    try:
        host.content_overlay_layer = layer
    except Exception:
        pass
    return layer


def content_bounds(widget: QWidget | None) -> QRect:
    """Host-local content rect; NEVER window.rect() (includes the strip band)."""
    host = _resolve_content_host(widget)
    if host is not None:
        try:
            return QRect(host.rect())
        except RuntimeError:
            pass
    if widget is not None and sip.isValid(widget):
        try:
            window = widget.window()
            if window is not None and sip.isValid(window):
                return QRect(window.rect())  # startup fallback only
        except RuntimeError:
            pass
    return QRect()

class OverlayLayer(QObject):
    def __init__(self, host: QWidget):
        super().__init__(host)
        self._host = host
        # True only for content layers (via get_content_overlay_layer); window
        # layers keep the legacy window-bounds clamp for menus/dialogs/CSD.
        self._is_content_layer = False
        self._value_popups: dict[str, _PopupBubble] = {}
        self._popup_timers: dict[str, QTimer] = {}
        self._host.installEventFilter(self)

    @staticmethod
    def _is_deleted(obj) -> bool:
        return obj is None or not sip.isValid(obj)

    @property
    def host(self) -> QWidget:
        return self._host

    def attach(self, widget: QWidget):
        if widget is None or widget.parentWidget() is self._host:
            resource_manager = getattr(self._host, "ui_resource_manager", None)
            if resource_manager is not None and widget is not None:
                resource_manager.register_widget(
                    widget,
                    name=f"overlay:{type(widget).__name__}",
                )
            return

        was_visible = widget.isVisible()
        geometry = widget.geometry()
        widget.setParent(self._host)
        resource_manager = getattr(self._host, "ui_resource_manager", None)
        if resource_manager is not None:
            resource_manager.register_widget(
                widget,
                name=f"overlay:{type(widget).__name__}",
            )
        if geometry.isValid():
            widget.setGeometry(geometry)
        if was_visible:
            widget.show()
            widget.raise_()
        else:
            widget.hide()

    def anchor_rect(self, anchor_widget: QWidget) -> QRect:
        # Global-difference mapping, NOT anchor_widget.mapTo(self._host):
        # when the anchor lives in a top-level child window whose QObject
        # parent is the host (e.g. the Settings dialog parented to the main
        # window), Qt's mapTo() treats the host as sitting at (0, 0) and
        # returns the anchor's *global* position instead of host-local
        # coordinates. Every in-window flyout anchored to such a dialog then
        # lands far off the anchor and gets clamped into the host's
        # bottom-right corner. mapToGlobal() - mapToGlobal() is correct for
        # any host (top-level or not).
        top_left = anchor_widget.mapToGlobal(QPoint(0, 0)) - self._host.mapToGlobal(QPoint(0, 0))
        return QRect(top_left, anchor_widget.size())

    def anchor_global_rect(self, anchor_widget: QWidget) -> QRect:
        top_left = anchor_widget.mapToGlobal(QPoint(0, 0))
        return QRect(top_left, anchor_widget.size())

    def clamp_rect(self, rect: QRect, margin: int = 8) -> QRect:
        bounds_host = self._host
        if getattr(self, "_is_content_layer", False):
            resolved = _resolve_content_host(self._host)
            if resolved is not None:
                bounds_host = resolved
        try:
            bounds = bounds_host.rect().adjusted(margin, margin, -margin, -margin)
        except RuntimeError:
            return rect
        if bounds.width() <= 0 or bounds.height() <= 0:
            return rect

        width = min(rect.width(), bounds.width())
        height = min(rect.height(), bounds.height())
        x = max(bounds.left(), min(rect.x(), bounds.right() - width + 1))
        y = max(bounds.top(), min(rect.y(), bounds.bottom() - height + 1))
        return QRect(x, y, width, height)

    def place_global_rect_relative_to_anchor(
        self,
        anchor_widget: QWidget,
        size: QSize,
        position: str = "top",
        offset: int = 6,
        margin: int = 8,
    ) -> QRect:
        anchor_rect = self.anchor_global_rect(anchor_widget)
        host_window = self._host.window()
        bounds = host_window.frameGeometry().adjusted(margin, margin, -margin, -margin)
        # `size` is the popup's outer size (already includes SHADOW_RADIUS
        # padding on every side), so without this correction the visible bubble ends up
        # offset + SHADOW_RADIUS from the anchor instead of just `offset` —
        # nearly double the intended gap for offset=6/SHADOW_RADIUS=8.
        offset = max(offset, _PopupBubble.SHADOW_RADIUS) - _PopupBubble.SHADOW_RADIUS

        if position == "bottom":
            x = anchor_rect.x() + (anchor_rect.width() - size.width()) // 2
            y = anchor_rect.bottom() + 1 + offset
        elif position == "left-top":
            x = anchor_rect.left() - size.width() - offset
            y = anchor_rect.top() - size.height() - offset
        else:
            x = anchor_rect.x() + (anchor_rect.width() - size.width()) // 2
            y = anchor_rect.y() - size.height() - offset

        width = min(size.width(), bounds.width())
        height = min(size.height(), bounds.height())
        x = max(bounds.left(), min(x, bounds.right() - width + 1))
        y = max(bounds.top(), min(y, bounds.bottom() - height + 1))
        return QRect(x, y, width, height)

    def ensure_visible(self, widget: QWidget, margin: int = 8):
        widget.setGeometry(self.clamp_rect(widget.geometry(), margin=margin))

    def contains_global(self, widget: QWidget, global_pos) -> bool:
        if widget is None or not widget.isVisible():
            return False
        local = widget.mapFromGlobal(global_pos)
        if hasattr(local, 'toPoint'):
            local = local.toPoint()
        return widget.rect().contains(local)

    def _popup_label(self, key: str) -> _PopupBubble:
        popup = self._value_popups.get(key)
        if popup is not None:
            if self._is_deleted(popup):
                self._forget_popup(key)
                popup = None
        if popup is not None:
            try:
                popup.parentWidget()
            except RuntimeError:
                self._forget_popup(key)
                popup = None
        if popup is None:
            popup = _PopupBubble(self._host.window())
            popup.hide()
            self._value_popups[key] = popup
            popup.destroyed.connect(lambda *_args, popup_key=key: self._forget_popup(popup_key))
            resource_manager = getattr(self._host, "ui_resource_manager", None)
            if resource_manager is not None:
                resource_manager.register_widget(
                    popup,
                    name=f"overlay_popup:{key}",
                )
        return popup

    def _popup_timer(self, key: str) -> QTimer:
        timer = self._popup_timers.get(key)
        if timer is not None and self._is_deleted(timer):
            self._popup_timers.pop(key, None)
            timer = None
        if timer is not None:
            try:
                timer.parent()
            except RuntimeError:
                self._popup_timers.pop(key, None)
                timer = None
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda popup_key=key: self.hide_popup(popup_key))
            self._popup_timers[key] = timer
            timer.destroyed.connect(lambda *_args, popup_key=key: self._popup_timers.pop(popup_key, None))
        return timer

    def _hide_other_popups(self, keep_key: str) -> None:
        for key in list(self._value_popups.keys()):
            if key != keep_key:
                self.hide_popup(key)

    def show_popup(
        self,
        key: str,
        anchor_widget: QWidget,
        *,
        text: str = "",
        pixmap: QPixmap | None = None,
        size: QSize | None = None,
        position: str = "top",
        offset: int = 6,
        timeout_ms: int = 800,
    ):

        self._hide_other_popups(key)
        popup = self._popup_label(key)
        try:
            popup.set_content(text=text, pixmap=pixmap, size=size)
            popup._apply_label_typography()
            popup.label.update()
            popup.setGeometry(
                self.place_global_rect_relative_to_anchor(
                    anchor_widget,
                    popup.size(),
                    position=position,
                    offset=offset,
                )
            )
            popup.show()
            popup.raise_()
        except RuntimeError:
            self._forget_popup(key)
            return

        timer = self._popup_timer(key)
        if self._is_deleted(timer):
            self._popup_timers.pop(key, None)
            return
        timer.stop()
        if timeout_ms > 0:
            timer.start(timeout_ms)

    def _forget_popup(self, key: str):
        self._value_popups.pop(key, None)
        timer = self._popup_timers.pop(key, None)
        if timer is not None and not self._is_deleted(timer):
            try:
                timer.stop()
            except RuntimeError:
                pass

    def hide_popup(self, key: str):
        popup = self._value_popups.get(key)
        if popup is not None and self._is_deleted(popup):
            self._value_popups.pop(key, None)
            popup = None
        if popup is not None:
            try:
                popup.hide()
            except RuntimeError:
                pass
        timer = self._popup_timers.get(key)
        if timer is not None and self._is_deleted(timer):
            self._popup_timers.pop(key, None)
            timer = None
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                self._popup_timers.pop(key, None)
        if popup is None:
            self._forget_popup(key)
            return
        try:
            popup.parentWidget()
        except RuntimeError:
            self._forget_popup(key)

    def hide_all_popups(self):
        for key in list(self._value_popups.keys()):
            self.hide_popup(key)

    def eventFilter(self, watched, event):
        if watched is self._host:
            if event.type() in (
                QEvent.Type.Resize,
                QEvent.Type.Move,
                QEvent.Type.Hide,
                QEvent.Type.Close,
                QEvent.Type.WindowStateChange,
                QEvent.Type.Leave,
            ):
                self.hide_all_popups()
        return super().eventFilter(watched, event)