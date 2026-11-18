"""The comparison envelope is SOURCE space, carried in the plan.

Owner report (2026-10-27): replacing ONE image of an existing pair makes the
whole composition jump for a frame or two before it settles. Measured live on
the supervisor's instance (Xvfb + vulkan, pair 2000x1000 + 1000x1400, slot 1
replaced): transient canvas ``[1024,1024]`` with loupe radius ``48.40``, settled
``[1600,1400]`` with ``51.74``; and one frame with the magnifier object present,
``visible=True``, zero circles.

Why: every preview is normalised per side to a 1024 long edge, so the envelope
``max(w1,w2) x max(h1,h2)`` of a PREVIEW pair has a different aspect than the
envelope of the SOURCE pair (2000x1400 = 1.43 vs 1024x1024 = 1.0). While a
preview tier is on screen the canvas basis (``plan.canvas_w/h``), the drawn
content rect, the split position and every canvas-px overlay (the loupe glass
included) were fitted to that preview aspect, then re-fitted when the unified
store landed.

This file pins the design that removes the guesswork:

* ``build_canvas_plan`` sizes the live basis from the plan's SOURCE roles —
  each side's own decoded pixels (``presentation/source_tier.py``) — and records
  the resulting ``(w1,h1,w2,h2)`` as ``plan.envelope_dims``.
* every downstream owner that had been re-deriving the envelope from whatever
  tier is installed (``update_common_letterbox_geometry``,
  ``refresh_live_content_rect``) consumes that field instead.
* fallback is today's behaviour, byte-identical: no live default plan
  (export/snapshot) and an active crop box (display dims are the truth there)
  both leave ``envelope_dims is None``.
* a side whose source tier is not decoded yet must never be mixed with the
  other side's source dims: a PREVIEW is normalised to a 1024 long edge, so
  ``max(preview1, source2)`` is a frame that describes neither pair (measured
  live 2026-10-27: ``canvas [1024,1400]`` between ``[1600,1400]`` frames).
  Such a plan carries ``envelope_hold`` and the live apply keeps the previous
  consistent frame untouched instead of presenting the hybrid.
* the parallel frame: a declared-visible magnifier must never lose its circles
  when the scene is rebuilt over a transiently empty image state (a slot
  replace clears the side before the preview lands) — the previous scene's
  overlay geometry is kept.

The frames are reproduced offscreen through the real apply chain
(``build_canvas_plan`` → ``apply_canvas_render_plan`` on a real ``CanvasWidget``
→ ``upload_pil_images`` → ``update_common_letterbox_geometry`` → the magnifier
plan overlay) and the assertion reads the drawn widget-px glass radius
(``runtime_state._feature_overlay_gpu._radius``) plus the drawn content rect —
not canvas-px intermediates.
"""

from __future__ import annotations

import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtGui import QColor, QImage

from core.state_management.dispatcher import Dispatcher
from core.store import Store
from core.store_viewport import SessionData
from tabs.image_compare.canvas.features.magnifier.state.store import (
    add_magnifier_model,
    set_default_magnifier_size,
    set_magnifier_enabled_flag,
)
from tabs.image_compare.canvas.presentation.plan_applicator import (
    apply_canvas_render_plan,
)
from tabs.image_compare.canvas.presentation.plan_builder import build_canvas_plan
from tabs.image_compare.state.document import DocumentModel, ImageItem
from tabs.image_compare.state.models import (
    ImageSessionState,
    PipelineCacheState,
    RenderCacheState,
)

WIDGET = (1200, 900)

# The real pair: landscape + portrait, so the source envelope is NOT square.
SOURCE_LEFT = (2000, 1000)
SOURCE_RIGHT = (1000, 1400)
SOURCE_ENVELOPE = (2000, 1400)
# Post-replace transient: each side's own PREVIEW, 1024 long edge, aspect kept.
PREVIEW_LEFT = (1024, 512)
PREVIEW_RIGHT = (731, 1024)
# Settled: unify resamples both sides onto the pair envelope.
UNIFIED = SOURCE_ENVELOPE

SIZE_RELATIVE = 0.2


@pytest.fixture(autouse=True)
def _register_image_compare_canvas_features():
    from tabs.image_compare.tab import ImageCompareTab
    from tabs.registry import get_shared_tab_registry

    ImageCompareTab().register_canvas_features()
    # ``build_canvas_scene`` asks the shared registry for the tab's
    # session-content service; a bare Store built here never went
    # through the real session-activation flow, so force the active tab for
    # the duration of each test (same recipe as test_export_diff_support).
    registry = get_shared_tab_registry()
    previous_active = registry._active_session_type
    registry._active_session_type = "image_compare"
    try:
        yield
    finally:
        registry._active_session_type = previous_active


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _pil(size, color):
    return Image.new("RGBA", size, color)


def _qimage(size, color):
    """A preview-tier display image — ``is_preview_tier`` is QImage-only."""
    q = QImage(int(size[0]), int(size[1]), QImage.Format.Format_RGBA8888)
    q.fill(QColor(*color))
    return q


def _source_tier_key(path):
    """Cache key for a side's own pixel tier, as ``pipeline/cache`` mints it."""
    from tabs.image_compare.pipeline.cache import _pixel_key

    return _pixel_key(path, None, None)


def _store(
    display1,
    display2,
    *,
    source1=None,
    source2=None,
    crop_service=None,
    paths=("/gallery/wide_landscape.png", "/gallery/tall_portrait.png"),
):
    """A real Store: document paths, the source tier in ``pipeline.pixel``,
    the display pair (preview/unified) in ``image_state``."""
    import tabs.image_compare.bootstrap_reducers  # noqa: F401

    store = Store()
    store.create_workspace_session(session_type="image_compare", activate=True)
    p1, p2 = paths
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
        # Same seam texture_parts.crop_clip.crop_service_for_widget reads.
        pipe.crop_service = crop_service
    store.set_session_state_slot("pipeline", pipe)
    store.viewport.session_data.image_state.image1 = display1
    store.viewport.session_data.image_state.image2 = display2
    # The side's OWN decoded pixels: exactly what source_tier.original_pixel_tier
    # resolves. Absent -> the SOURCE role falls back to the display tier.
    if source1 is not None:
        pipe.pixel[_source_tier_key(p1)] = source1
    if source2 is not None:
        pipe.pixel[_source_tier_key(p2)] = source2
    view_state = store.viewport.view_state
    set_magnifier_enabled_flag(view_state, True)
    set_default_magnifier_size(view_state, SIZE_RELATIVE)
    add_magnifier_model(view_state, store.viewport.render_config)
    return store


def _canvas(store):
    from tabs.image_compare.canvas.widget import CanvasWidget

    _app()
    canvas = CanvasWidget()
    canvas.resize(*WIDGET)
    return canvas


def _frame(store, display1, display2):
    """One real frame: plan → apply (upload + letterbox + overlays)."""
    plan = build_canvas_plan(store, display1, display2, preserve_zoom=True)
    canvas = _canvas(store)
    apply_canvas_render_plan(canvas, plan, store=store)
    state = canvas.runtime_state
    return {
        "plan": plan,
        "content_rect": tuple(state._content_rect_px or ()),
        "letterbox": [tuple(p) if p is not None else None for p in state._letterbox_params],
        "glass_radius_px": float(state._feature_overlay_gpu._radius),
        "split": float(getattr(plan.render_scene, "split_position_visual", 0.0)),
    }


def _apply_frame(store, canvas, display1, display2):
    """Apply one real frame onto an EXISTING canvas (the live apply seam)."""
    from tabs.image_compare.canvas.presentation.surface import apply_store_to_canvas

    apply_store_to_canvas(canvas, store, display1, display2)
    state = canvas.runtime_state
    return {
        "plan": getattr(canvas, "_active_render_plan", None),
        "content_rect": tuple(state._content_rect_px or ()),
        "letterbox": [tuple(p) if p is not None else None for p in state._letterbox_params],
        "glass_radius_px": float(state._feature_overlay_gpu._radius),
    }


def _preview_pair_store():
    return _store(
        _qimage(PREVIEW_LEFT, (200, 40, 40, 255)),
        _qimage(PREVIEW_RIGHT, (40, 200, 40, 255)),
        source1=_pil(SOURCE_LEFT, (200, 40, 40, 255)),
        source2=_pil(SOURCE_RIGHT, (40, 200, 40, 255)),
    )


# -- (a) the plan carries SOURCE dims while the display pair is preview-tier --


def test_plan_envelope_is_source_space_while_display_is_preview_tier():
    store = _preview_pair_store()
    preview = _frame(
        store, _qimage(PREVIEW_LEFT, (200, 40, 40, 255)), _qimage(PREVIEW_RIGHT, (40, 200, 40, 255))
    )

    assert preview["plan"].envelope_dims == (*SOURCE_LEFT, *SOURCE_RIGHT), (
        "the plan must carry the SOURCE envelope (w1,h1,w2,h2), not the"
        f" installed tier's: got {preview['plan'].envelope_dims!r}"
    )
    # The canvas basis is that envelope — the aspect the settled pair will have.
    assert (preview["plan"].canvas_w, preview["plan"].canvas_h) == SOURCE_ENVELOPE


def test_preview_pair_first_paint_is_not_held():
    """Both sides preview-tier is a self-consistent PAIR — first paint applies.

    The hold must be about a MIX (one side's guess against the other side's
    real dims), not about previews in general: holding here would delay the
    first paint of every pair load until the full decode lands.
    """
    store = _store(
        _qimage(PREVIEW_LEFT, (200, 40, 40, 255)),
        _qimage(PREVIEW_RIGHT, (40, 200, 40, 255)),
    )
    display1 = _qimage(PREVIEW_LEFT, (200, 40, 40, 255))
    display2 = _qimage(PREVIEW_RIGHT, (40, 200, 40, 255))
    plan = build_canvas_plan(store, display1, display2, preserve_zoom=True)
    assert plan.envelope_hold is False
    assert plan.envelope_dims == (*PREVIEW_LEFT, *PREVIEW_RIGHT)

    canvas = _canvas(store)
    applied = _apply_frame(store, canvas, display1, display2)
    assert applied["plan"] is not None, "the preview pair must be presented"
    assert applied["plan"].envelope_hold is False
    assert applied["content_rect"], "the preview pair must get a drawn rect"


def test_a_side_without_a_decoded_source_holds_instead_of_mixing():
    """One side's source not decoded yet: no mixed envelope, hold the frame.

    ``max(w1,w2) x max(h1,h2)`` over ``(preview1, source2)`` describes neither
    pair: a preview is normalised to a 1024 long edge, so the transient canvas
    basis is ``[1024,1400]`` between two settled ``[1600,1400]`` frames
    (measured live 2026-10-27). The plan must state that its basis is a preview
    guess (``envelope_hold``, ``envelope_dims is None``) and the live apply must
    keep the previous consistent frame byte-identically.

    Red on HEAD: the plan advertises the hybrid ``(source1, preview2)`` and the
    apply draws it.
    """
    store = _preview_pair_store()
    canvas = _canvas(store)
    display1 = _qimage(PREVIEW_LEFT, (200, 40, 40, 255))
    display2 = _qimage(PREVIEW_RIGHT, (40, 200, 40, 255))
    settled = _apply_frame(store, canvas, display1, display2)
    assert settled["plan"].envelope_dims == (*SOURCE_LEFT, *SOURCE_RIGHT)

    # Replace slot 1: the pending load has installed only the new side's
    # preview tier so far, while slot 2 still holds its own decoded pixels.
    document = store.get_session_state_slot("document")
    pipeline = store.get_session_state_slot("pipeline")
    pipeline.pixel.pop(_source_tier_key(document.image1_path), None)
    new_preview = _qimage((1024, 768), (200, 40, 40, 255))  # C 1600x1200
    store.viewport.session_data.image_state.image1 = new_preview

    mixed = build_canvas_plan(store, new_preview, display2, preserve_zoom=True)
    assert mixed.envelope_dims is None, (
        "a mixed preview/source envelope must not be advertised as the pair"
        f" envelope: got {mixed.envelope_dims!r}"
    )
    assert mixed.envelope_hold is True, (
        "a frame whose basis is one side's preview guess must be held"
    )
    assert getattr(settled["plan"], "envelope_hold", True) is False, (
        "a frame with both sides' own pixels must not be held"
    )

    held = _apply_frame(store, canvas, new_preview, display2)
    assert held["plan"] is settled["plan"], (
        "the apply must keep the previous plan, not install the hybrid one"
    )
    assert held["content_rect"] == settled["content_rect"]
    assert held["letterbox"] == settled["letterbox"]
    assert held["glass_radius_px"] == settled["glass_radius_px"], (
        "a held frame repeats byte-identically: no re-fit, no geometry change —"
        f" {held['glass_radius_px']:.3f}px vs {settled['glass_radius_px']:.3f}px"
    )


# -- (b) the drawn loupe glass is a function of the letterboxed content only --


def test_drawn_loupe_radius_is_identical_on_preview_and_unified_frames():
    store = _preview_pair_store()
    preview = _frame(
        store, _qimage(PREVIEW_LEFT, (200, 40, 40, 255)), _qimage(PREVIEW_RIGHT, (40, 200, 40, 255))
    )
    settled = _frame(
        store, _qimage(UNIFIED, (200, 40, 40, 255)), _qimage(UNIFIED, (40, 200, 40, 255))
    )

    assert preview["content_rect"] == settled["content_rect"], (
        "the drawn content rect must not follow the installed tier: preview"
        f" {preview['content_rect']} vs settled {settled['content_rect']}"
    )
    assert preview["glass_radius_px"] == pytest.approx(
        settled["glass_radius_px"], rel=1e-6, abs=1e-6
    ), (
        "the loupe glass must be identical on the preview frame and the"
        f" unified one: {preview['glass_radius_px']:.3f}px vs"
        f" {settled['glass_radius_px']:.3f}px"
    )
    assert preview["split"] == pytest.approx(settled["split"], abs=1e-9)
    # size_relative over the canonical basis: the glass is
    # ``size_relative/2 * sqrt(canvas_w*canvas_h)`` canvas-px, converted by the
    # letterbox scale ``min(dw/canvas_w, dh/canvas_h)`` (= 0.6 here).
    assert settled["glass_radius_px"] == pytest.approx(
        0.5 * SIZE_RELATIVE * math.sqrt(SOURCE_ENVELOPE[0] * SOURCE_ENVELOPE[1]) * 0.6,
        rel=1e-6,
    )


# -- (c) the cases that must stay byte-identical to HEAD ---------------------


def test_same_size_pair_is_unchanged():
    """Equal dims: source envelope == display envelope, so nothing moves."""
    store = _store(
        _pil((800, 600), (200, 40, 40, 255)),
        _pil((800, 600), (40, 200, 40, 255)),
        source1=_pil((800, 600), (200, 40, 40, 255)),
        source2=_pil((800, 600), (40, 200, 40, 255)),
    )
    frame = _frame(
        store, _pil((800, 600), (200, 40, 40, 255)), _pil((800, 600), (40, 200, 40, 255))
    )
    # HEAD numbers for this pair: canvas == display dims, envelope 800x600 fitted
    # into the 1200x900 widget → 1200x900 (same 4:3 aspect), so the glass is
    # size_relative/2 * sqrt(800*600) * 1.5 canvas→widget.
    assert (frame["plan"].canvas_w, frame["plan"].canvas_h) == (800, 600)
    assert frame["content_rect"] == (0, 0, 1200, 900)
    assert frame["glass_radius_px"] == pytest.approx(
        0.5 * SIZE_RELATIVE * math.sqrt(800 * 600) * 1.5, rel=1e-6
    )
    # ... and the source-carrying plan matches a plan whose rounds resolve to the
    # display tier (what HEAD effectively sized from), member by member.
    head_store = _store(
        _pil((800, 600), (200, 40, 40, 255)),
        _pil((800, 600), (40, 200, 40, 255)),
    )
    head = _frame(
        head_store, _pil((800, 600), (200, 40, 40, 255)), _pil((800, 600), (40, 200, 40, 255))
    )
    assert head["plan"].envelope_dims == frame["plan"].envelope_dims
    assert head["content_rect"] == frame["content_rect"]
    assert head["letterbox"] == frame["letterbox"]
    assert head["glass_radius_px"] == frame["glass_radius_px"]
    assert (head["plan"].canvas_w, head["plan"].canvas_h) == (
        frame["plan"].canvas_w,
        frame["plan"].canvas_h,
    )


def _bordered_png(path):
    """120x80 content at (40,40) inside a 200x160 frame (autocrop fixture)."""
    canvas = Image.new("RGBA", (200, 160), (0, 0, 0, 255))
    canvas.paste(Image.new("RGBA", (120, 80), (200, 180, 160, 255)), (40, 40))
    canvas.save(str(path))
    return str(path)


def test_crop_plan_keeps_the_display_truth(tmp_path):
    """A crop box is active: display (box-clipped) dims stay the basis."""
    from shared.image_processing.autocrop import CropService

    p1 = _bordered_png(tmp_path / "a.png")
    p2 = _bordered_png(tmp_path / "b.png")
    svc = CropService()
    assert svc.get(p1) is not None and svc.get(p2) is not None
    disp1 = Image.open(p1).convert("RGBA")
    disp2 = Image.open(p2).convert("RGBA")
    store = _store(disp1, disp2, crop_service=svc, paths=(p1, p2))

    plan = build_canvas_plan(store, disp1, disp2, preserve_zoom=True)

    # The plan ADVERTISES the box-clipped envelope (2026-11-18). It is not a
    # "source-space" guess here: the crop box is already applied, so these are
    # the source dims clipped by it. Carrying them is what stops the rect owner
    # from falling back to the installed display tier's UNCROPPED size (a lazy
    # ``TiledPixelStore`` reports ``[512, 512]`` for a 312x512 crop). The
    # letterbox owner still IGNORES this kwarg whenever boxes are active
    # (``update_common_letterbox_geometry``: ``_plan_envelope`` is nulled under
    # a box), so nothing is double-clipped.
    assert plan.envelope_dims == (120, 80, 120, 80)
    assert (plan.canvas_w, plan.canvas_h) == (120, 80)
    assert tuple(store.runtime_cache.overlay_clip_rect) == (0, 0, 120, 80)
    # The drawn letterbox keeps fitting the display pair (HEAD-identical):
    # 120x80 fitted into 1200x900 → 1200x800, centred vertically.
    frame = _frame(store, disp1, disp2)
    assert frame["content_rect"] == (0, 50, 1200, 800)


def test_export_shaped_plan_has_no_envelope():
    """Export/snapshot plans pass target_size — today's path, no envelope."""
    store = _preview_pair_store()
    display1 = _qimage(PREVIEW_LEFT, (200, 40, 40, 255))
    display2 = _qimage(PREVIEW_RIGHT, (40, 200, 40, 255))
    plan = build_canvas_plan(
        store, display1, display2, target_size=(500, 400), preserve_zoom=True
    )
    assert plan.envelope_dims is None
    assert (plan.canvas_w, plan.canvas_h) == (500, 400)


# -- (d) a declared-visible magnifier is never rendered away for a frame -----


def test_declared_visible_magnifier_keeps_its_circles_across_a_transiently_empty_frame():
    """A declared-visible loupe must keep its overlay on a transiently empty frame.

    Replacing a slot dispatches ``ClearImageSlotDataAction`` before the new
    display lands, so ``image_state.image1`` is briefly ``None`` while the
    document still names the pair. ``build_canvas_scene`` answered that frame
    with an EMPTY graph, which drops every overlay object — measured live
    2026-10-27 as ``magnifiers[0].visible == True`` with ``circles == []``.
    Red on HEAD: the held graph has no magnifier at all.

    The frame is rebuilt through the real scene builder (the seam that produces
    the scene-graph circles the live probe reads), not by skipping the rebuild.
    """
    from ui.canvas_infra.scene.builder import build_canvas_scene

    store = _preview_pair_store()
    canvas = _canvas(store)
    display1 = _qimage(PREVIEW_LEFT, (200, 40, 40, 255))
    display2 = _qimage(PREVIEW_RIGHT, (40, 200, 40, 255))
    _apply_frame(store, canvas, display1, display2)

    def _magnifiers(graph):
        return [
            obj for obj in graph.objects if getattr(obj, "kind", None) == "magnifier"
        ]

    label_width, label_height = WIDGET
    settled_graph = build_canvas_scene(
        store,
        image_label=canvas,
        label_width=label_width,
        label_height=label_height,
    )
    canvas.runtime_state._canvas_scene_graph = settled_graph
    settled = _magnifiers(settled_graph)
    assert settled, "precondition: the settled frame builds the declared-visible loupe"
    assert settled[0].visible
    assert settled[0].circles, (
        "precondition: a visible loupe carries circles on a settled frame"
    )

    # The replace's clear moment: the slot's display is momentarily gone, the
    # document still names the pair (see use_cases/slot.py's
    # ClearImageSlotDataAction before the preview lands).
    store.viewport.session_data.image_state.image1 = None
    held_graph = build_canvas_scene(
        store,
        image_label=canvas,
        label_width=label_width,
        label_height=label_height,
    )

    held = _magnifiers(held_graph)
    assert held, (
        "a declared-visible magnifier must not be dropped by a transiently empty"
        " frame: the scene graph came back without any magnifier object"
    )
    assert held[0].visible
    assert held[0].circles, (
        "the previous overlay geometry must be kept, not rebuilt empty:"
        f" visible={held[0].visible} circles={held[0].circles!r}"
    )
    assert held[0].circles == settled[0].circles, (
        "a held frame repeats the previous overlay byte-identically"
    )

    # A one-sided document is NOT a replace in flight: the user's content
    # changed, so the pair's overlay may go (the hold must not freeze it).
    store.set_session_state_slot(
        "document",
        DocumentModel(
            image_list1=[],
            image_list2=[ImageItem(path="/gallery/tall_portrait.png", display_name="b")],
            current_index1=-1,
            current_index2=0,
        ),
    )
    one_sided = build_canvas_scene(
        store,
        image_label=canvas,
        label_width=label_width,
        label_height=label_height,
    )
    assert not _magnifiers(one_sided), (
        "a genuinely one-sided document must not keep the pair's overlay alive"
    )
