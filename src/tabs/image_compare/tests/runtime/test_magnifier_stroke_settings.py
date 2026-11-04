"""Loupe outline + detection-area thickness are settings-window values.

Owner request 2026-11-04. Two different strokes with two different homes:

* the loupe's own outline is a per-instance ``MagnifierModel.border_thickness``
  (it already existed and already reached the render; nothing exposed it);
* the detection ring is the loupe's sampling area, drawn by the capture pass
  from the canvas style, so it gets a feature-state value instead.

The settings dialog carries them through ``tab_extras`` (the host layer may not
name a tab concept — ``test_ui_tab_sandbox``), and the UI reads the current
values through a capability alias (``test_canvas_features_imports``).
"""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from tabs.image_compare.canvas.features.magnifier.commands.settings import (
    settings_get_stroke,
    settings_set_stroke,
)
from tabs.image_compare.canvas.features.magnifier.state.feature_state import (
    get_magnifier_widget_state,
)
from tabs.image_compare.canvas.features.magnifier.state.models import MagnifierModel


def _store(*models):
    view_state = SimpleNamespace()
    state = get_magnifier_widget_state(view_state)
    for index, model in enumerate(models):
        state.models[f"m{index}"] = model
    store = SimpleNamespace(
        viewport=SimpleNamespace(view_state=view_state),
        invalidated=0,
        emitted=0,
    )

    def _invalidate():
        store.invalidated += 1

    def _emit(*_a, **_k):
        store.emitted += 1

    store.invalidate_render_cache = _invalidate
    store.emit_state_change = _emit
    return store


def test_the_setting_writes_every_live_loupe():
    first, second = MagnifierModel(), MagnifierModel()
    store = _store(first, second)

    changed = settings_set_stroke(store, 5, 7)

    assert changed == {"border_thickness": 5, "capture_thickness": 7}
    assert (first.border_thickness, second.border_thickness) == (5, 5)
    assert store.invalidated == 1 and store.emitted == 1


def test_an_unchanged_value_does_not_dirty_the_frame():
    """The second apply of the same values is a no-op (the first one seeds them)."""
    store = _store(MagnifierModel(border_thickness=2))
    settings_set_stroke(store, 2, 2)

    assert settings_set_stroke(store, 2, 2) == {}


def test_the_dialog_reads_the_values_back_through_the_alias():
    """The dialog seeds from the feature state (what the row writes), not a model."""
    store = _store(MagnifierModel())
    settings_set_stroke(store, 4, 6)

    assert settings_get_stroke(store) == {
        "border_thickness": 4,
        "capture_thickness": 6,
    }


def test_the_detection_ring_uses_its_own_thickness():
    """Red before the setting: the ring was drawn from the canvas style only."""
    from tabs.image_compare.canvas.features.capture.overlay_model import (
        resolve_capture_rings,
    )

    app = QApplication.instance() or QApplication([])  # noqa: F841
    store = _store()
    get_magnifier_widget_state(store.viewport.view_state).default_capture_thickness = 9
    widget = SimpleNamespace(runtime_state=SimpleNamespace(_store=store))
    ctx = SimpleNamespace(
        resolved_style=SimpleNamespace(annotation_ring_stroke_px=2.0),
        zoom_level=1.0,
        pan_offset_x=0.0,
        pan_offset_y=0.0,
        canvas_width=1000,
        canvas_height=800,
        canvas_offset_x=0.0,
        canvas_offset_y=0.0,
        feature_overlay=None,
        scene_frame=SimpleNamespace(feature_payloads={}),
    )
    widget.runtime_state._capture_circles = (
        (SimpleNamespace(x=lambda: 100.0, y=lambda: 100.0), 40.0, QColor(255, 0, 0, 255)),
    )

    rings = resolve_capture_rings(ctx, widget)

    assert rings, "the ring must still be produced"
    assert rings[0].line_width == 9.0


def test_an_untouched_setting_keeps_the_canvas_style_width():
    from tabs.image_compare.canvas.features.capture.overlay_model import (
        resolve_capture_rings,
    )

    app = QApplication.instance() or QApplication([])  # noqa: F841
    store = _store()
    assert get_magnifier_widget_state(
        store.viewport.view_state
    ).default_capture_thickness is None
    widget = SimpleNamespace(runtime_state=SimpleNamespace(_store=store))
    ctx = SimpleNamespace(
        resolved_style=SimpleNamespace(annotation_ring_stroke_px=2.0),
        zoom_level=1.0,
        pan_offset_x=0.0,
        pan_offset_y=0.0,
        canvas_width=1000,
        canvas_height=800,
        canvas_offset_x=0.0,
        canvas_offset_y=0.0,
        feature_overlay=None,
        scene_frame=SimpleNamespace(feature_payloads={}),
    )
    widget.runtime_state._capture_circles = (
        (SimpleNamespace(x=lambda: 100.0, y=lambda: 100.0), 40.0, QColor(255, 0, 0, 255)),
    )

    rings = resolve_capture_rings(ctx, widget)

    assert rings and rings[0].line_width == 2.0
