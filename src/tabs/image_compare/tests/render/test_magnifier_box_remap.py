"""W3b regression: the magnifier maps its capture over the crop box.

Pixel stores are FULL-FRAME (W1+W2); the magnifier must sample the detected
CROP BOX, not the store — on both the pixel-box path
(``geometry.drawing_coords``: capture offsets by ``box_origin``, ``eff_rel``
maps over box size) and the live UV path (``geometry.layout_plan``:
``uv_rect`` remaps box→full-image UV). Box resolution goes only through
``effective_crop_box_for_path`` over warmed services; a missing box must
leave behavior IDENTICAL to today (None-box parity pins below use
hand-computed legacy numbers).
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

from PIL import Image

from core.store import Store
from domain.types import Point
from shared.image_processing.autocrop import CropService
from tabs.image_compare.canvas.features.magnifier.geometry.box_remap import (
    capture_window_px,
    remap_uv_rect,
    resolve_crop_boxes_for_store,
)
from tabs.image_compare.canvas.features.magnifier.geometry.drawing_coords import (
    get_magnifier_drawing_coords,
)
from tabs.image_compare.canvas.features.magnifier.state.feature_state import (
    get_magnifier_widget_state,
)
from tabs.image_compare.canvas.features.magnifier.state.models import MagnifierModel
from tabs.image_compare.pipeline.crop_box import effective_crop_box_for_path
from tabs.image_compare.state.document import DocumentModel, ImageItem


def _bordered_png(path):
    canvas = Image.new("RGBA", (200, 160), (0, 0, 0, 255))
    canvas.paste(Image.new("RGBA", (120, 80), (200, 180, 160, 255)), (40, 40))
    canvas.save(path)
    return str(path)


def _store_with_images(path, *, size=(200, 160)):
    store = Store()
    # Auto-crop is OFF by default since 2026-11-18, so this suite (which is
    # about a crop box being ACTIVE) opts in explicitly. The opt-out case below
    # flips it back to prove the resolver honours the flag.
    store.settings.auto_crop_black_borders = True
    store.create_workspace_session(session_type="image_compare", activate=True)
    doc = DocumentModel(
        image_list1=[ImageItem(path=path, display_name="a")],
        image_list2=[ImageItem(path=path, display_name="b")],
        current_index1=0,
        current_index2=0,
    )
    store.set_session_state_slot("document", doc, emit_scope="")
    fake1 = SimpleNamespace(size=size)
    fake2 = SimpleNamespace(size=size)
    store.viewport.session_data.image_state = SimpleNamespace(image1=fake1, image2=fake2)
    state = get_magnifier_widget_state(store.viewport.view_state)
    model = MagnifierModel(
        id="m1",
        visible=True,
        position=Point(0.25, 0.75),
        capture_size_relative=0.2,
    )
    state.models[model.id] = model
    state.active_id = model.id
    return store


def test_pixel_path_capture_maps_over_box_offset_store(tmp_path):
    """Boxed capture centers on box_origin + eff_rel * box_size."""
    path = _bordered_png(tmp_path / "bordered.png")
    svc = CropService()
    box = effective_crop_box_for_path(path, crop_service=svc)
    assert box is not None  # warmed real detection
    store = _store_with_images(path)

    crop_box1, crop_box2, *_ = get_magnifier_drawing_coords(store, 400, 300)

    # spec relationship: eff_rel maps over box size, offset by box origin
    frac_w, frac_h = 58.0 / 400, 58.0 / 300
    for crop_box in (crop_box1, crop_box2):
        cx = box.left + 0.25 * box.width
        cy = box.top + 0.75 * box.height
        w = int(round(frac_w * box.width))
        h = int(round(frac_h * box.height))
        if w % 2 != 0:
            w += 1
        if h % 2 != 0:
            h += 1
        left, top = int(round(cx - w / 2.0)), int(round(cy - h / 2.0))
        assert crop_box == (left, top, left + w, top + h)
        # window lives inside the box bounds, not just the full store
        assert left >= box.left and top >= box.top
        assert left + w <= box.right and top + h <= box.bottom
    # remap actually engaged (box is strictly inside the 200x160 frame)
    assert crop_box1 != (35, 104, 65, 136)


def test_pixel_path_none_box_matches_legacy_numbers(tmp_path):
    """No warmed box → hand-computed legacy full-store math, exactly."""
    path = _bordered_png(tmp_path / "bordered.png")
    store = _store_with_images(path)  # CropService never warmed for path

    crop_box1, crop_box2, *_ = get_magnifier_drawing_coords(store, 400, 300)

    assert crop_box1 == (35, 104, 65, 136)
    assert crop_box2 == (35, 104, 65, 136)


def test_resolve_cold_service_and_crop_off_yield_none(tmp_path):
    """Cold cache (no IO on GUI thread) and crop-OFF both resolve to None."""
    path = _bordered_png(tmp_path / "bordered.png")
    cold = CropService()  # never warmed for path
    assert not cold._has_cached(path)
    store = _store_with_images(path)
    assert resolve_crop_boxes_for_store(store) == (None, None)

    warmed = CropService()
    assert effective_crop_box_for_path(path, crop_service=warmed) is not None
    assert resolve_crop_boxes_for_store(store) != (None, None)
    try:
        store.settings.auto_crop_black_borders = False
    except Exception:
        pytest.skip("settings flag not mutable in this build")
    assert resolve_crop_boxes_for_store(store) == (None, None)


def test_capture_window_px_none_is_legacy_math():
    assert capture_window_px(
        eff_rel_x=0.25, eff_rel_y=0.75, frac_w=58.0 / 400, frac_h=58.0 / 300,
        full_w=200, full_h=160, box=None,
    ) == (50.0, 120.0, 30, 32, 35, 104, 65, 136)


def test_remap_uv_rect_full_frame_box_is_identity():
    assert remap_uv_rect(0.5, 0.5, 0.1, 0.1, (0, 0, 100, 100), 100, 100) == pytest.approx(
        (0.4, 0.4, 0.6, 0.6)
    )
    assert remap_uv_rect(0.5, 0.5, 0.1, 0.1, None, 100, 100) is None


def _layout_viewport(model):
    view_state = SimpleNamespace(
        diff_mode="off",
        channel_view_mode="RGB",
        canvas_widget_state={},
    )
    render_config = SimpleNamespace(interpolation_method="BILINEAR")
    return SimpleNamespace(view_state=view_state, render_config=render_config)


def _patch_layout_plan(monkeypatch, model):
    import tabs.image_compare.canvas.features.magnifier.geometry.layout_plan as layout_plan
    from domain.types import Color

    monkeypatch.setattr(layout_plan, "iter_magnifier_models", lambda view, render: [model])
    monkeypatch.setattr(layout_plan, "active_magnifier_id", lambda view: "m1")
    monkeypatch.setattr(
        layout_plan,
        "read_canvas_feature_color_by_setting_key",
        lambda session_type, viewport, key: Color(255, 50, 100, 230),
    )
    monkeypatch.setattr(
        layout_plan,
        "active_or_default_border_color",
        lambda view: Color(255, 255, 255, 248),
    )
    return layout_plan


def test_live_uv_path_remaps_box_to_full_uv(monkeypatch):
    """Side 1 samples the box region; side 2 (no box) keeps legacy UV."""
    from tabs.image_compare.canvas.features.magnifier.geometry.layout_plan import (
        build_magnifier_layout,
    )

    model = MagnifierModel(
        id="m1",
        position=Point(0.5, 0.5),
        capture_size_relative=0.2,
        visible_left=True,
        visible_center=False,
        visible_right=False,
    )
    layout_plan = _patch_layout_plan(monkeypatch, model)
    _ = layout_plan

    layout = build_magnifier_layout(
        _layout_viewport(model),
        width=100,
        height=100,
        canvas_width=100,
        canvas_height=100,
        divider_thickness_px=2,
        crop_box1=(20, 10, 60, 50),
        crop_box2=None,
        full_size1=(100, 100),
        full_size2=(100, 100),
    )

    assert layout is not None
    assert len(layout.slots) == 1
    slot = layout.slots[0]
    # capture 0.2 on a 100 px canvas -> 10 px radius; the box path remaps that
    # footprint into full-image UV (side 2 has no box and keeps plain UV).
    assert layout.capture_circles[0].radius == pytest.approx(10.0)
    assert tuple(slot.uv_rect) == pytest.approx((0.36, 0.26, 0.44, 0.34))
    assert tuple(slot.uv_rect2) == pytest.approx((0.4, 0.4, 0.6, 0.6))


def test_live_uv_path_none_box_matches_the_capture_ring(monkeypatch):
    from tabs.image_compare.canvas.features.magnifier.geometry.layout_plan import (
        build_magnifier_layout,
    )

    model = MagnifierModel(
        id="m1",
        position=Point(0.5, 0.5),
        capture_size_relative=0.2,
        visible_left=True,
        visible_center=False,
        visible_right=False,
    )
    _patch_layout_plan(monkeypatch, model)

    layout = build_magnifier_layout(
        _layout_viewport(model),
        width=100,
        height=100,
        canvas_width=100,
        canvas_height=100,
        divider_thickness_px=2,
    )

    assert layout is not None
    assert len(layout.slots) == 1
    slot = layout.slots[0]
    # No crop box -> the sampled rect IS the capture ring (capture 0.2 on a
    # 100 px canvas = 10 px radius).
    assert layout.capture_circles[0].radius == pytest.approx(10.0)
    assert tuple(slot.uv_rect) == pytest.approx((0.4, 0.4, 0.6, 0.6))
    assert tuple(slot.uv_rect2) == pytest.approx((0.4, 0.4, 0.6, 0.6))
