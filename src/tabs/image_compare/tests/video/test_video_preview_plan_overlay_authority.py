"""A recorded snapshot plan owns its overlay state; the preview may not re-derive it.

Export applies a recorded plan with ``store=None``: the plan's magnifier
layout, diff mode, guides and capture colour reach the canvas exactly as
recorded. The video-editor PREVIEW applies the *same* plan through the real
``PreviewCoordinator`` (schedule -> prepare -> apply) but binds the plan's
store, and two apply-path lines used to throw the recorded overlay away:

1. ``engine.cache.display_tier_service.apply_overlays`` skipped the plan's
   guides/capture params whenever a store was present and then ran the live
   recompute (``apply_feature_live_runtime_overlays``), which rebuilt — or,
   when it could not build a layout, nulled — the overlay the plan carried.
2. ``...magnifier.workers.scene_update.rebuild_magnifier_overlay`` replaced
   ``_active_render_plan`` with its own layout, and on ``layout is None``
   wrote ``overlay_layout=None`` plus ``reset_canvas_overlays``.

The rule pinned here, in both places: ``plan.overlay_layout is not None``
⇒ the plan owns the overlay state. A plan WITHOUT a layout keeps the live
recompute exactly as before — ``test_plan_without_overlay_layout_still_follows_live_recompute``
is the anti-freeze half of the same seam.

Red before the fix (measured on this base commit): the preview test fails on
``canvas._active_render_plan is plan`` — the live recompute replaced the plan
(and, through ``reset_canvas_overlays``, emptied ``_guide_sets`` /
``_capture_circles`` and deactivated the GPU overlay), and on
``state._guides_thickness == PLAN_GUIDES_THICKNESS`` — the plan's recorded
guides params were skipped for a store-bound apply. The parity test fails the
same way (store-bound signature != export signature for one plan). The
``rebuild_magnifier_overlay`` test fails on all three assertions.

The plan is built by the real ``build_canvas_plan`` from a real ``Store``
(magnifier visible, guides on, ``diff_mode="highlight"``) and carries a real
``OverlayLayout``; the payload images stand for the snapshot renderer's
diff-baked composite. Everything is applied to a real ``CanvasWidget``
offscreen: no RHI device is needed for the runtime geometry/overlay state.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace

import pytest
from dataclasses import replace
from PIL import Image
from PySide6.QtCore import QObject
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from core.state_management.dispatcher import Dispatcher
from core.store import Store
from core.store_viewport import SessionData
from tabs.image_compare.canvas.features.capture.state.feature_state import (
    get_capture_widget_state,
)
from tabs.image_compare.canvas.features.guides.state.feature_state import (
    get_guides_widget_state,
)
from tabs.image_compare.canvas.features.magnifier.state.store import (
    add_magnifier_model,
    set_default_magnifier_size,
    set_magnifier_enabled_flag,
)
from tabs.image_compare.canvas.features.magnifier.workers import scene_update
from tabs.image_compare.canvas.features.magnifier.render.overlay import (
    _StorePresenterAdapter,
)
from tabs.image_compare.canvas.presentation.plan_applicator import (
    apply_canvas_render_plan,
)
from tabs.image_compare.canvas.presentation.plan_builder import build_canvas_plan
from tabs.image_compare.plugins.video_editor.presenter_parts.preview import (
    PreviewCoordinator,
)
from tabs.image_compare.state.document import DocumentModel, ImageItem
from tabs.image_compare.state.models import (
    ImageSessionState,
    PipelineCacheState,
    RenderCacheState,
)

_PREVIEW = (400, 300)
_SOURCE = (800, 600)
# The snapshot renderer's diff-baked composite (highlight mode paints red).
_BAKED = (250, 20, 40, 255)
_BAKED2 = (20, 250, 40, 255)
_FPS = 10
# Recorded guides width — deliberately different from the live store's own
# guides state, so "the plan's params were applied" is observable.
PLAN_GUIDES_THICKNESS = 7

_SIZE_RELATIVE = 0.2


@pytest.fixture(autouse=True)
def _register_image_compare_canvas_features():
    from tabs.image_compare.tab import ImageCompareTab
    from tabs.registry import get_shared_tab_registry

    ImageCompareTab().register_canvas_features()
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


def _color_tuple(color):
    return (
        int(color.red()),
        int(color.green()),
        int(color.blue()),
        int(color.alpha()),
    )


def _recording_store():
    """The recorded session's store: magnifier visible, guides + diff set."""
    import tabs.image_compare.bootstrap_reducers  # noqa: F401

    store = Store()
    store.create_workspace_session(session_type="image_compare", activate=True)
    paths = ("/rec/a.png", "/rec/b.png")
    store.set_session_state_slot(
        "document",
        DocumentModel(
            image_list1=[ImageItem(path=paths[0], display_name="a")],
            image_list2=[ImageItem(path=paths[1], display_name="b")],
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
    store.set_session_state_slot("pipeline", PipelineCacheState())
    # The live rebuild resolves the pair through this warmed cache when the
    # canvas carries no ``_source_pil_images`` (``CanvasWidget`` has no such
    # attribute; the runtime state does) — that is the live path this seam
    # must keep alive.
    pipe = store.get_session_state_slot("pipeline")
    from tabs.image_compare.pipeline.cache import _pixel_key

    for path, color in (("/rec/a.png", _BAKED), ("/rec/b.png", _BAKED2)):
        qimage = QImage(_SOURCE[0], _SOURCE[1], QImage.Format.Format_RGBA8888)
        qimage.fill(QColor(*color))
        pipe.pixel[_pixel_key(path, None, None)] = qimage

    view_state = store.viewport.view_state
    view_state.diff_mode = "highlight"
    set_magnifier_enabled_flag(view_state, True)
    set_default_magnifier_size(view_state, _SIZE_RELATIVE)
    add_magnifier_model(view_state, store.viewport.render_config)
    guides_state = get_guides_widget_state(view_state)
    guides_state.enabled = True
    # The live store's own width — the plan below records a different one.
    guides_state.thickness = 1
    get_capture_widget_state(view_state)
    return store


def _baked_pair():
    return (
        Image.new("RGBA", _SOURCE, _BAKED),
        Image.new("RGBA", _SOURCE, _BAKED2),
    )


def _snapshot_plan(store):
    """The recorded plan as the snapshot renderer hands it to the preview."""
    baked1, baked2 = _baked_pair()
    plan = build_canvas_plan(
        store,
        baked1,
        baked2,
        guides_thickness=PLAN_GUIDES_THICKNESS,
    )
    assert plan.overlay_layout is not None, "the recorded plan must carry overlays"
    assert plan.overlay_layout.slots, "the recorded plan must carry loupe slots"
    return plan


class _SnapshotExporter:
    """Exporter double: returns the recorded plan + its store, like the real one."""

    def __init__(self, plan, store):
        self.plan = plan
        self.store = store
        self.prepare_calls = 0

    def prepare_snapshot_canvas_frame(self, snap, out_w, out_h, **kwargs):
        self.prepare_calls += 1
        return SimpleNamespace(
            plan=self.plan,
            store=self.store,
            output_width=out_w,
            output_height=out_h,
            image_dest_x=0,
            image_dest_y=0,
            debug={},
        )


class _EditorService:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def get_snapshot_at(self, _index):
        return self._snapshot


class _DeferredPreparePool:
    """Captures workers instead of running them (deterministic delivery)."""

    def __init__(self):
        self.pending: list = []

    def start(self, worker):
        self.pending.append(worker)

    def take(self, count: int = 1):
        taken, self.pending = self.pending[:count], self.pending[count:]
        return taken


def _canvas():
    from tabs.image_compare.canvas.widget import CanvasWidget

    _app()
    canvas = CanvasWidget()
    canvas.resize(*_PREVIEW)
    return canvas


def _center(image):
    return tuple(image.convert("RGBA").getpixel((image.width // 2, image.height // 2)))


def _overlay_signature(canvas) -> dict:
    """What a viewer would see of the loupe/diff/guides overlay, in widget px."""
    state = canvas.runtime_state
    gpu = state._feature_overlay_gpu
    return {
        "gpu_active": bool(gpu._gpu_active),
        "diff_mode": int(gpu._gpu_diff_mode),
        "slots": tuple(
            (
                round(float(slot["center"].x()), 2),
                round(float(slot["center"].y()), 2),
                round(float(slot["radius"]), 2),
                int(slot["source"]),
                bool(slot["is_combined"]),
                round(float(slot["divider_thickness_uv"]), 6),
            )
            for slot in gpu._gpu_slots
            if slot is not None
        ),
        "quads": tuple(q is not None for q in gpu._quads),
        "overlay_radius": round(float(gpu._radius), 2),
        "capture_circles": tuple(
            (
                round(float(point.x()), 2),
                round(float(point.y()), 2),
                round(float(radius), 2),
                _color_tuple(color),
            )
            for point, radius, color in state._capture_circles
        ),
        "guide_sets": tuple(
            (
                round(float(guide[0].x()), 2),
                round(float(guide[0].y()), 2),
                round(float(guide[1]), 2),
                tuple((round(float(p.x()), 2), round(float(p.y()), 2)) for p in guide[2]),
                int(guide[5]),
                _color_tuple(guide[4]),
            )
            for guide in state._guide_sets
        ),
        "guides": (
            bool(state._show_guides),
            _color_tuple(state._laser_color),
            int(state._guides_thickness),
        ),
        "capture_color": _color_tuple(state._capture_color),
    }


def _overlay_geometry_signature(canvas) -> dict:
    """The applied loupe/diff geometry (no guide params — the scene apply owns those)."""
    state = canvas.runtime_state
    gpu = state._feature_overlay_gpu
    return {
        "gpu_active": bool(gpu._gpu_active),
        "diff_mode": int(gpu._gpu_diff_mode),
        "slots": tuple(
            (
                round(float(slot["center"].x()), 2),
                round(float(slot["center"].y()), 2),
                round(float(slot["radius"]), 2),
                int(slot["source"]),
                bool(slot["is_combined"]),
            )
            for slot in gpu._gpu_slots
            if slot is not None
        ),
        "quads": tuple(q is not None for q in gpu._quads),
        "overlay_radius": round(float(gpu._radius), 2),
        "capture_circles": tuple(
            (
                round(float(point.x()), 2),
                round(float(point.y()), 2),
                round(float(radius), 2),
            )
            for point, radius, _color in state._capture_circles
        ),
    }


class _Harness:
    """Real PreviewCoordinator over a real CanvasWidget and a recorded plan."""

    def __init__(self, monkeypatch):
        self.store = _recording_store()
        self.plan = _snapshot_plan(self.store)
        self.exporter = _SnapshotExporter(self.plan, self.store)
        self.snapshot = SimpleNamespace(
            image1_path="/rec/a.png",
            image2_path="/rec/b.png",
            timestamp=0.0,
            viewport_state=SimpleNamespace(
                index=0, render_config=None, view_state=None
            ),
            settings_state=SimpleNamespace(index=0),
        )
        self.canvas = _canvas()
        self.view = SimpleNamespace(
            preview_label=self.canvas,
            get_preview_size=lambda: _PREVIEW,
            fit_content_fill_color=None,
        )
        self.live_calls: list = []
        # The live overlay recompute is what used to replace/null the plan.
        # ``apply_magnifier_overlay`` imports it inside the call, so patching
        # the module attribute intercepts the real live path. The spy calls
        # through, so "it ran" also means "it did its work".
        real_rebuild = scene_update.rebuild_magnifier_overlay

        def _spy(presenter):
            self.live_calls.append(presenter)
            return real_rebuild(presenter)

        monkeypatch.setattr(scene_update, "rebuild_magnifier_overlay", _spy)
        self.timer_parent = QObject()
        self.coordinator = PreviewCoordinator(
            view=self.view,
            export_controller=SimpleNamespace(video_exporter=self.exporter),
            playback_engine=SimpleNamespace(
                is_playing=lambda: False,
                get_current_frame=lambda: 0,
            ),
            model=SimpleNamespace(
                width=_PREVIEW[0], height=_PREVIEW[1], aspect_ratio_locked=False
            ),
            editor_service=_EditorService(self.snapshot),
            timer_parent=self.timer_parent,
            emit_preview_ready=lambda: None,
        )
        self.pool = _DeferredPreparePool()
        self.coordinator._prepare_pool = self.pool

    def render(self):
        """The real chain: schedule -> heavy update -> prepare -> apply."""
        self.coordinator.schedule_update()
        QTest.qWait(50)
        for worker in self.pool.take(len(self.pool.pending)):
            worker.run()
        return self.canvas

    def teardown(self):
        self.coordinator.on_view_destroyed()


def test_preview_keeps_the_plan_carried_overlay_state(monkeypatch):
    """Preview(applied with a store) == the recorded plan's overlay state."""
    harness = _Harness(monkeypatch)
    plan = harness.plan
    canvas = harness.render()
    state = canvas.runtime_state

    assert harness.exporter.prepare_calls == 1, "the preview never prepared a frame"
    assert harness.live_calls == [], (
        "a plan that carries its overlay_layout must not run the live overlay"
        " recompute (it replaces or nulls the recorded overlay)"
    )

    applied = canvas._active_render_plan
    assert applied is plan, (
        "the apply replaced the recorded plan with a live-recomputed one"
    )
    layout = applied.overlay_layout
    assert layout is not None and layout.slots, (
        "the recorded overlay layout was nulled by the apply"
    )

    # The plan's baked pair (highlight diff in the pixels) is what is shown.
    assert state._stored_pil_images[0] is not None
    assert _center(state._stored_pil_images[0]) == _BAKED

    # The loupe quads reached the canvas and the recorded diff mode is live.
    gpu = state._feature_overlay_gpu
    assert gpu._gpu_active is True, "no GPU overlay was applied"
    assert any(slot is not None for slot in gpu._gpu_slots)
    assert any(quad is not None for quad in gpu._quads)
    assert int(gpu._gpu_diff_mode) == int(layout.diff_mode) == 1

    # The plan's own guides/capture params, not the live scene's defaults.
    assert state._show_guides is True
    assert int(state._guides_thickness) == PLAN_GUIDES_THICKNESS
    assert state._guide_sets, "the recorded guide sets were cleared"
    assert _color_tuple(state._capture_color) == _color_tuple(plan.capture_color)

    harness.teardown()


def test_export_and_preview_apply_the_same_overlay_signature(monkeypatch):
    """One recorded plan: store=None (export) and store=prepared.store (preview)."""
    harness = _Harness(monkeypatch)
    plan = harness.plan

    export_canvas = _canvas()
    apply_canvas_render_plan(
        export_canvas, plan, store=None, clip_overlays_to_image_bounds=False
    )
    export_signature = _overlay_signature(export_canvas)

    preview_canvas = harness.render()
    preview_signature = _overlay_signature(preview_canvas)

    assert preview_signature == export_signature, (
        "the preview apply lost the recorded overlay state the export keeps:"
        f"\n export ={export_signature}\n preview={preview_signature}"
    )
    assert export_signature["gpu_active"] is True
    assert export_signature["guide_sets"], "the recorded guides never reached export"

    harness.teardown()


def test_plan_without_overlay_layout_still_follows_live_recompute(monkeypatch):
    """Anti-freeze: a plan with NO overlay_layout keeps today's live path.

    The preview store's magnifier is visible, so the live recompute must run
    (``rebuild_magnifier_overlay`` is called) and take over the plan — that is
    the behaviour of every live frame, which this seam must not freeze.
    """
    harness = _Harness(monkeypatch)
    store = harness.store
    baked1, baked2 = _baked_pair()
    # No overlay layout: the frame is not the recorded overlay's owner (this
    # is the shape of a live plan built while the overlay was off).
    harness.plan = replace(
        build_canvas_plan(store, baked1, baked2), overlay_layout=None
    )
    assert harness.plan.overlay_layout is None
    harness.exporter.plan = harness.plan

    canvas = harness.render()

    assert harness.live_calls, (
        "a plan without overlay_layout must still run the live overlay recompute"
    )
    assert canvas._active_render_plan is not harness.plan, (
        "the live recompute must own an overlay-less plan's overlay state"
    )

    harness.teardown()


def test_live_recompute_never_nulls_a_plan_carried_overlay(monkeypatch):
    """``rebuild_magnifier_overlay`` must not clear an overlay the plan carries.

    The live recompute can fail to build a layout (transient empty state, no
    renderable magnifier in the live store). That branch used to write
    ``_dc_replace(plan, overlay_layout=None)`` and ``reset_canvas_overlays``,
    nulling an overlay the plan already carried and that the live path never
    built. The paired assertion below is the anti-freeze half: for a plan
    WITHOUT a layout the very same branch must keep nulling and resetting.
    """
    store = _recording_store()
    plan = _snapshot_plan(store)
    # Reference: the export-style apply is the plan-faithful overlay state.
    reference_canvas = _canvas()
    apply_canvas_render_plan(
        reference_canvas, plan, store=None, clip_overlays_to_image_bounds=False
    )
    before = _overlay_geometry_signature(reference_canvas)
    assert before["gpu_active"] is True and before["slots"]

    # The live rebuild cannot produce a layout for this frame.
    monkeypatch.setattr(
        scene_update, "build_magnifier_layout", lambda *_a, **_k: None
    )

    canvas = _canvas()
    apply_canvas_render_plan(
        canvas, plan, store=store, clip_overlays_to_image_bounds=True
    )
    presenter = _StorePresenterAdapter(store, canvas)

    scene_update.rebuild_magnifier_overlay(presenter)

    assert canvas._active_render_plan is plan, (
        "the live recompute replaced a plan that already carried its overlay"
    )
    assert plan.overlay_layout is not None
    assert _overlay_geometry_signature(canvas) == before, (
        "the live recompute cleared/reset the plan-carried overlay state"
    )

    # Anti-freeze: without an overlay layout the live path still owns the
    # decision — it nulls the plan and resets the canvas overlays.
    live_plan = replace(plan, overlay_layout=None)
    live_canvas = _canvas()
    apply_canvas_render_plan(
        live_canvas, live_plan, store=store, clip_overlays_to_image_bounds=True
    )
    scene_update.rebuild_magnifier_overlay(
        _StorePresenterAdapter(store, live_canvas)
    )
    assert live_canvas._active_render_plan is not live_plan
    assert live_canvas._active_render_plan.overlay_layout is None
    assert _overlay_geometry_signature(live_canvas)["gpu_active"] is False
