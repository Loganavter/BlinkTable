"""W3e: live canvas plan sizes follow the effective crop box.

With full-frame display images, ``build_canvas_plan`` sized the live canvas
from the full frame while the letterbox content is box-clipped (envelope,
uploads, Store geometry all fit the box) — fit-zoom/pads then work off the
wrong base whenever the box is active.

Bordered pair (real files, warmed ``CropService`` on the pipeline slot) →
live plan must be box-derived (canvas, content layout, clipped views, clip
rect), never full-frame. A ``None`` box (no service) must behave IDENTICALLY
to today (same objects, full-frame sizes).
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image

from core.state_management.dispatcher import Dispatcher
from core.store import Store
from core.store_viewport import SessionData
from shared.image_processing.image_dims import get_image_dims
from tabs.image_compare.canvas.presentation.plan_builder import build_canvas_plan
from tabs.image_compare.state.document import DocumentModel, ImageItem
from tabs.image_compare.state.models import (
    ImageSessionState,
    PipelineCacheState,
    RenderCacheState,
)
from ui.canvas_presentation.plan import resolve_plan_logical_image_rect

FULL = (200, 160)
# Fixture from test_fullframe_decode / test_box_clipped_geometry: 120x80
# content at (40, 40) inside a 200x160 frame.
BOX = (40, 40, 160, 120)
BOX_DIMS = (120, 80)


@pytest.fixture(autouse=True)
def _register_image_compare_canvas_features():
    from tabs.image_compare.tab import ImageCompareTab

    ImageCompareTab().register_canvas_features()


def _bordered_png(path):
    canvas = Image.new("RGBA", FULL, (0, 0, 0, 255))
    canvas.paste(
        Image.new("RGBA", BOX_DIMS, (200, 180, 160, 255)), (BOX[0], BOX[1])
    )
    canvas.save(str(path))
    return str(path)


def _make_store(p1, p2, disp1, disp2, crop_service=None):
    import tabs.image_compare.bootstrap_reducers  # noqa: F401

    store = Store()
    store.create_workspace_session(session_type="image_compare", activate=True)
    store.set_session_state_slot(
        "document",
        DocumentModel(
            image_list1=[ImageItem(path=p1, display_name="a")],
            image_list2=[ImageItem(path=p2, display_name="b")],
            current_index1=0,
            current_index2=0,
        ),
    )
    store.viewport.session_data = SessionData(
        image_state=ImageSessionState(), render_cache=RenderCacheState()
    )
    store.state_changed = type(
        "Sig", (), {"emit": staticmethod(lambda *_a, **_k: None)}
    )()
    store.set_dispatcher(Dispatcher(store))
    pipe = PipelineCacheState()
    if crop_service is not None:
        # Same seam texture_parts.crop_clip.crop_service_for_widget reads
        # (Store pipeline slot ``crop_service``); absent -> None -> parity.
        pipe.crop_service = crop_service
    store.set_session_state_slot("pipeline", pipe)
    store.viewport.session_data.image_state.image1 = disp1
    store.viewport.session_data.image_state.image2 = disp2
    return store


def _warmed_service(p1, p2):
    from shared.image_processing.autocrop import CropService

    svc = CropService()
    box1, box2 = svc.get(p1), svc.get(p2)
    assert box1 is not None and box2 is not None, "fixture needs detectable borders"
    assert (box1.left, box1.top, box1.right, box1.bottom) == BOX
    assert (box2.left, box2.top, box2.right, box2.bottom) == BOX
    return svc


def test_live_plan_box_active_sizes_box_derived(tmp_path):
    p1 = _bordered_png(tmp_path / "a.png")
    p2 = _bordered_png(tmp_path / "b.png")
    svc = _warmed_service(p1, p2)
    disp1 = Image.open(p1).convert("RGBA")
    disp2 = Image.open(p2).convert("RGBA")
    store = _make_store(p1, p2, disp1, disp2, crop_service=svc)

    plan = build_canvas_plan(store, disp1, disp2)

    # Canvas/content frame follows the box-clipped content, not the frame.
    assert (plan.canvas_w, plan.canvas_h) == BOX_DIMS
    assert (plan.canvas_w, plan.canvas_h) != FULL
    # Display views fed downstream are box-clipped (same pixels as cropping).
    assert get_image_dims(plan.image1) == BOX_DIMS
    assert get_image_dims(plan.image2) == BOX_DIMS
    assert plan.image1.crop((0, 0, *BOX_DIMS)).tobytes() == (
        disp1.crop(BOX).tobytes()
    )
    # Sources stay full-frame (diff/SSIM/export correctness).
    assert get_image_dims(plan.source_image1) == FULL
    assert get_image_dims(plan.source_image2) == FULL
    # Clip rect + logical image rect narrow to the box content.
    assert tuple(store.runtime_cache.overlay_clip_rect) == (0, 0, *BOX_DIMS)
    assert resolve_plan_logical_image_rect(plan) == (0, 0, *BOX_DIMS)
    if plan.overlay_layout is not None:
        assert (
            plan.overlay_layout.content_width,
            plan.overlay_layout.content_height,
        ) == BOX_DIMS


def test_live_plan_none_box_parity_with_today(tmp_path):
    p1 = _bordered_png(tmp_path / "a.png")
    p2 = _bordered_png(tmp_path / "b.png")
    disp1 = Image.open(p1).convert("RGBA")
    disp2 = Image.open(p2).convert("RGBA")
    store = _make_store(p1, p2, disp1, disp2, crop_service=None)

    plan = build_canvas_plan(store, disp1, disp2)

    assert (plan.canvas_w, plan.canvas_h) == FULL
    assert plan.image1 is disp1
    assert plan.image2 is disp2
    assert tuple(store.runtime_cache.overlay_clip_rect) == (0, 0, *FULL)
    assert resolve_plan_logical_image_rect(plan) == (0, 0, *FULL)


def test_production_live_path_sizes_the_canvas_from_the_crop_box(tmp_path, monkeypatch):
    """The LIVE plan follows the box even though the Store slot carries no service.

    Production shape: ``PipelineCacheState`` holds the pixel/preview/unify
    mirrors and nothing else, so ``crop_service`` is fetched from the session
    controller (``set_session_controller`` backlink on the canvas widget).
    Reading only the Store slot made every cropped pair keep the uncropped
    source aspect on the canvas basis while the letterbox fitted the box —
    the owner report "cropping black areas does not recalculate the aspect
    ratio of the whole displayed comparison".
    """
    from types import SimpleNamespace

    from tabs.image_compare.canvas.presentation.surface import apply_store_to_canvas

    p1 = _bordered_png(tmp_path / "a.png")
    p2 = _bordered_png(tmp_path / "b.png")
    svc = _warmed_service(p1, p2)
    disp1 = Image.open(p1).convert("RGBA")
    disp2 = Image.open(p2).convert("RGBA")
    # No ``crop_service`` on the pipeline slot — exactly what the app builds.
    store = _make_store(p1, p2, disp1, disp2)
    canvas = SimpleNamespace(
        _pending_session_controller=SimpleNamespace(_get_crop_service=lambda: svc),
        runtime_state=SimpleNamespace(_store=store, _source_image_ids=(p1, p2)),
    )
    captured: dict = {}
    monkeypatch.setattr(
        "ui.canvas_presentation.plan_applicator.apply_canvas_render_plan",
        lambda _canvas, plan, **_kw: captured.setdefault("plan", plan),
    )

    apply_store_to_canvas(canvas, store, disp1, disp2)

    plan = captured["plan"]
    assert (plan.canvas_w, plan.canvas_h) == BOX_DIMS
    assert (plan.canvas_w, plan.canvas_h) != FULL
    assert get_image_dims(plan.image1) == BOX_DIMS
    assert get_image_dims(plan.image2) == BOX_DIMS
    assert tuple(store.runtime_cache.overlay_clip_rect) == (0, 0, *BOX_DIMS)
