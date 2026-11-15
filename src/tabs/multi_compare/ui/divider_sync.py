"""Divider-settings <-> toolbar sync for ``MultiCompareWidget``.

Split out of ``widget.py`` to keep that class down to composition/wiring --
mirrors the ``drag_drop`` split. Every function here takes the widget as its
first argument and reads/writes its instance state directly.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor

from domain.qt_adapters import ensure_visible_qcolor
from domain.types import Color
from tabs.multi_compare.models import DEFAULT_DIVIDER_COLOR_RGBA, MultiCompareDividerSettings
from tabs.multi_compare.scene import actions

#: Pure camera actions never touch divider settings (their reducers only
#: replace zoom/pan), so syncing the divider toolbar per zoom/pan tick is
#: pure Qt churn (button setChecked/value/color + a queued resync timer on
#: every wheel tick of a gesture). Skipped here; every other action still
#: syncs. The canvas state, zoom indicator, and compositor settle sync below
#: are unaffected.
_VIEW_ONLY_ACTIONS = frozenset(
    {
        "multi_compare/set_zoom",
        "multi_compare/set_pan",
        "multi_compare/set_scene_zoom",
        "multi_compare/set_scene_pan",
        "multi_compare/reset_view",
    }
)


def on_store_change(widget, _action, new_state) -> None:
    widget.canvas.set_state(new_state)
    dim_toolbar = getattr(widget, "_focus_dim_toolbar", None)
    dim_footer = getattr(widget, "_focus_dim_footer", None)
    if dim_toolbar is not None and dim_footer is not None:
        focused = bool(new_state.is_focused)
        if focused != dim_toolbar.isVisible():
            if focused:
                widget._sync_focus_dim_overlays()
            dim_toolbar.setVisible(focused)
            dim_footer.setVisible(focused)
    # Indicator show/hide sits above the QRhi canvas; sync after set_state,
    # then poke another view update so reset-from-overlay cannot leave a
    # stale backing frame (see MultiCompareCanvasWidget.request_view_update).
    # Calling through the widget's own bound method (not the chrome module
    # function directly) so tests that monkeypatch `widget._sync_zoom_indicator`
    # still intercept it.
    widget._sync_zoom_indicator()
    action_type = getattr(_action, "type", "") or ""
    if action_type in _VIEW_ONLY_ACTIONS:
        from ui.canvas_infra.rhi.rhi_present_sync import schedule_compositor_sync

        widget.canvas.request_view_update()
        # Flush the Wayland/Vulkan catch-up on gesture settle — otherwise
        # the first flyout after zoom restacks and the image jumps while
        # the zoom % chip stays unchanged.
        schedule_compositor_sync(widget.canvas, reason=action_type)
    else:
        widget.sync_divider_toolbar()
    if widget._font_popup_open:
        widget._sync_font_settings_flyout()


def sync_divider_toolbar(widget) -> None:
    _sync_divider_toolbar(widget)
    queue_divider_toolbar_resync(widget)


def queue_divider_toolbar_resync(widget) -> None:
    if widget._divider_toolbar_sync_pending:
        return
    widget._divider_toolbar_sync_pending = True
    QTimer.singleShot(0, lambda: run_queued_divider_toolbar_sync(widget))


def run_queued_divider_toolbar_sync(widget) -> None:
    widget._divider_toolbar_sync_pending = False
    _sync_divider_toolbar(widget)


def _sync_divider_toolbar(widget) -> None:
    ds = widget.store.state.divider_settings
    btn = widget.toolbar.btn_divider_visible
    btn.blockSignals(True)
    btn.setChecked(not ds.visible)
    btn.blockSignals(False)
    width_btn = widget.toolbar.btn_divider_width
    if hasattr(width_btn, "get_value") and hasattr(width_btn, "set_value"):
        ui_value = ds.thickness if ds.visible else 0
        if width_btn.get_value() != ui_value:
            width_btn.blockSignals(True)
            width_btn.set_value(ui_value)
            width_btn.blockSignals(False)
    color = ensure_visible_qcolor(
        ds.color_rgba, fallback=Color(*DEFAULT_DIVIDER_COLOR_RGBA)
    )
    if hasattr(widget.toolbar.btn_divider_color, "setUnderlineColor"):
        widget.toolbar.btn_divider_color.setUnderlineColor(color)
    if hasattr(width_btn, "setUnderlineColor"):
        width_btn.setUnderlineColor(color)


def on_divider_visible_toggled(widget, visible: bool) -> None:
    ds = widget.store.state.divider_settings
    new_ds = MultiCompareDividerSettings(
        visible=bool(visible),
        thickness=ds.thickness,
        color_rgba=ds.color_rgba,
    )
    widget.store.dispatch(actions.set_divider_settings(new_ds))


def on_divider_width_changed(widget, width: int) -> None:
    ds = widget.store.state.divider_settings
    thickness = max(0, int(width))
    new_ds = MultiCompareDividerSettings(
        visible=thickness > 0,
        thickness=thickness if thickness > 0 else ds.thickness,
        color_rgba=ds.color_rgba,
    )
    if new_ds == ds:
        return
    widget.store.dispatch(actions.set_divider_settings(new_ds))


def apply_divider_color(widget, color: QColor) -> None:
    if color is None or not color.isValid():
        return
    visible = ensure_visible_qcolor(color, fallback=Color(*DEFAULT_DIVIDER_COLOR_RGBA))
    ds = widget.store.state.divider_settings
    new_ds = MultiCompareDividerSettings(
        visible=ds.visible,
        thickness=ds.thickness,
        color_rgba=(
            visible.red(),
            visible.green(),
            visible.blue(),
            visible.alpha(),
        ),
    )
    widget.store.dispatch(actions.set_divider_settings(new_ds))
