"""Video preview render sizing: respects device pixel ratio and preview scale,
quality scales large previews, uses the main renderer (not thumbnail), and
prescale targets the exact output size.

Dogma source: docs/dev/rendering/rendering-model.md (the snapshot
replay path feeds the same passes as the live path — export/preview/video
logic with no live counterpart is the wrong fix).
"""

from __future__ import annotations
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtCore import QObject

def _build_preview_coordinator(*, device_pixel_ratio: float = 1.0, emit_fit_content_available=None):
    from tabs.image_compare.plugins.video_editor.presenter_parts.preview import PreviewCoordinator

    timer_parent = QObject()
    view = SimpleNamespace(
        preview_label=SimpleNamespace(devicePixelRatioF=lambda: device_pixel_ratio),
        get_preview_size=lambda: (640, 360),
    )
    coordinator = PreviewCoordinator(
        view=view,
        export_controller=None,
        playback_engine=SimpleNamespace(is_playing=lambda: False),
        model=SimpleNamespace(width=1920, height=1080),
        editor_service=SimpleNamespace(),
        timer_parent=timer_parent,
        emit_preview_ready=lambda: None,
        emit_fit_content_available=emit_fit_content_available,
    )
    # Keep QObject parent alive with the coordinator (timers bind to it).
    coordinator._test_timer_parent = timer_parent
    return coordinator


def test_fit_content_disabled_when_canvas_never_leaves_unit():
    """No pads, canvas stays 0..1 across all snapshots -> nothing to uncrop."""
    from tabs.image_compare.plugins.video_editor.services.video_export.models import (
        GlobalCanvasBounds,
    )

    events: list[bool] = []
    coordinator = _build_preview_coordinator(emit_fit_content_available=events.append)

    unit_bounds = GlobalCanvasBounds(
        pad_left=0, pad_right=0, pad_top=0, pad_bottom=0,
        base_width=100, base_height=100,
    )
    coordinator.on_global_bounds_calculated(unit_bounds)

    assert events == [False]
    coordinator.on_view_destroyed()


def test_fit_content_enabled_when_canvas_extends_beyond_unit():
    from tabs.image_compare.plugins.video_editor.services.video_export.models import (
        GlobalCanvasBounds,
    )

    events: list[bool] = []
    coordinator = _build_preview_coordinator(emit_fit_content_available=events.append)

    padded_bounds = GlobalCanvasBounds(
        pad_left=10, pad_right=0, pad_top=0, pad_bottom=0,
        base_width=100, base_height=100,
    )
    coordinator.on_global_bounds_calculated(padded_bounds)

    assert events == [True]
    # on_global_bounds_calculated() unconditionally arms the real (1ms
    # singleshot) preview-update timer via schedule_update(). Left running,
    # it fires later against this torn-down coordinator's incomplete
    # playback_engine stub, surfacing as an unrelated test's failure via
    # pytest-qt's Qt-event-loop exception capture. Tear down like real
    # view-destruction does so any pending fire is a no-op.
    coordinator.on_view_destroyed()


def test_fit_content_stays_available_when_bounds_unknown():
    """No snapshots / calculation failure -> don't lock the button off."""
    events: list[bool] = []
    coordinator = _build_preview_coordinator(emit_fit_content_available=events.append)

    coordinator.on_global_bounds_calculated(None)
    coordinator.on_bounds_calculation_error(RuntimeError("boom"))

    assert events == [True, True]
    coordinator.on_view_destroyed()

def test_preview_render_size_uses_device_pixel_ratio():
    coordinator = _build_preview_coordinator(device_pixel_ratio=2.0)

    render_w, render_h = coordinator._resolve_preview_render_size(640, 360)

    assert (render_w, render_h) == (1920, 1080)

def test_preview_render_size_respects_preview_scale():
    coordinator = _build_preview_coordinator(device_pixel_ratio=2.0)
    coordinator.preview_render_scale = 0.5

    render_w, render_h = coordinator._resolve_preview_render_size(640, 360)

    assert (render_w, render_h) == (960, 540)

def test_preview_render_size_quality_scales_large_preview():
    coordinator = _build_preview_coordinator(device_pixel_ratio=1.0)
    coordinator.model.width = 1451
    coordinator.model.height = 1080

    coordinator.preview_render_scale = 1.0
    full_w, full_h = coordinator._resolve_preview_render_size(1954, 1066)

    coordinator.preview_render_scale = 0.5
    half_w, half_h = coordinator._resolve_preview_render_size(1954, 1066)

    assert (full_w, full_h) == (1451, 1080)
    assert half_w < full_w
    assert half_h < full_h
    assert (half_w, half_h) == (726, 540)


def test_window_resize_refits_cached_plan_without_clearing_cache(monkeypatch):
    """Dragging the window must not drop the prepare cache or force GPU prepare."""
    from tabs.image_compare.plugins.video_editor.presenter_parts import preview as preview_module

    coordinator = _build_preview_coordinator()
    coordinator.playback_engine = SimpleNamespace(is_playing=lambda: False)
    plan = object()
    store = object()
    coordinator._preview_frame_cache = {
        "prepare_key": ("sig", 100, 50, False, None, None),
        "plan": plan,
        "store": store,
        "request_key": None,
        "frame_pil": None,
    }
    coordinator._last_render_params = (3, 640, 360)
    apply_calls: list[dict] = []

    def _capture_apply(canvas, applied_plan, **kwargs):
        apply_calls.append({"plan": applied_plan, **kwargs})

    monkeypatch.setattr(preview_module, "apply_canvas_render_plan", _capture_apply)
    schedule_calls = []
    monkeypatch.setattr(coordinator, "schedule_update", lambda: schedule_calls.append(1))
    settle_pings: list[int] = []

    def _capture_ping():
        settle_pings.append(1)
        coordinator._refit_cached_preview_to_widget()

    monkeypatch.setattr(coordinator._resize_settle, "ping", _capture_ping)

    coordinator.on_window_resized()

    assert coordinator._preview_frame_cache["plan"] is plan
    assert apply_calls == [
        {
            "plan": plan,
            "store": store,
            "clip_overlays_to_image_bounds": True,
        }
    ]
    assert settle_pings == [1]
    assert schedule_calls == []

    coordinator._on_resize_gpu_settled()
    assert schedule_calls == [1]
    assert coordinator._last_render_params == (3, None, None)


def test_render_preview_gpu_refits_when_only_display_size_changes(monkeypatch):
    from tabs.image_compare.plugins.video_editor.presenter_parts import preview as preview_module

    coordinator = _build_preview_coordinator()
    plan = object()
    store = object()
    prepare_key = (
        ("p1", "p2", 0.0, "vp", "settings"),
        1920,
        1080,
        False,
        None,
        None,
    )
    coordinator._preview_frame_cache = {
        "prepare_key": prepare_key,
        "plan": plan,
        "store": store,
        "request_key": None,
        "frame_pil": None,
    }
    coordinator._render_task_id = 1
    apply_calls: list[object] = []
    monkeypatch.setattr(
        preview_module,
        "apply_canvas_render_plan",
        lambda _c, applied_plan, **_kw: apply_calls.append(applied_plan),
    )
    monkeypatch.setattr(
        coordinator,
        "_resolve_preview_render_size",
        lambda _w, _h: (1920, 1080),
    )
    monkeypatch.setattr(coordinator, "get_preview_size_safe", lambda: (800, 450))
    monkeypatch.setattr(
        coordinator,
        "_resolve_fill_color_tuple",
        lambda: None,
    )

    class FakeExporter:
        def prepare_snapshot_canvas_frame(self, *args, **kwargs):
            raise AssertionError("prepare must not run on display-only resize")

    coordinator.export_controller = SimpleNamespace(video_exporter=FakeExporter())
    snap = SimpleNamespace(
        image1_path="p1",
        image2_path="p2",
        timestamp=0.0,
        viewport_state=object(),
        settings_state=object(),
    )
    monkeypatch.setattr(
        preview_module,
        "viewport_fingerprint",
        lambda _vp: "vp",
    )
    monkeypatch.setattr(preview_module, "frozen_value", lambda _s: "settings")

    assert coordinator.render_preview_gpu(snap) is True
    assert apply_calls == [plan]


def test_preview_scene_clips_overlays_only_in_crop_mode(monkeypatch):
    """``_prepare_preview_frame`` (background-safe) + ``_apply_prepared_preview_scene``
    (GUI-thread only) replace the old single-shot ``_apply_preview_scene`` — see
    render_preview_gpu's async prepare path."""
    from tabs.image_compare.plugins.video_editor.presenter_parts import preview as preview_module

    coordinator = _build_preview_coordinator()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        preview_module,
        "apply_canvas_render_plan",
        lambda _canvas, _plan, **kwargs: captured.update(kwargs),
    )

    class FakeExporter:
        def prepare_snapshot_canvas_frame(self, snap, out_w, out_h, **kwargs):
            return SimpleNamespace(plan=object(), store=object())

    exporter = FakeExporter()
    coordinator.view.preview_label = SimpleNamespace()

    coordinator.fit_content_mode = False
    prepared = coordinator._prepare_preview_frame(exporter, object(), 640, 360, None, None)
    coordinator._apply_prepared_preview_scene(prepared, ("req",), 640, 360)
    assert captured["clip_overlays_to_image_bounds"] is True

    coordinator.fit_content_mode = True
    prepared = coordinator._prepare_preview_frame(exporter, object(), 640, 360, object(), None)
    coordinator._apply_prepared_preview_scene(prepared, ("req2",), 640, 360)
    assert captured["clip_overlays_to_image_bounds"] is False


def test_preview_scene_uses_main_renderer_not_thumbnail(monkeypatch):
    from tabs.image_compare.plugins.video_editor.presenter_parts import preview as preview_module

    coordinator = _build_preview_coordinator()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        preview_module,
        "apply_canvas_render_plan",
        lambda *args, **kwargs: captured.setdefault("applied", True),
    )

    class FakeExporter:
        def prepare_snapshot_canvas_frame(self, snap, out_w, out_h, **kwargs):
            captured["snap"] = snap
            captured["out_w"] = out_w
            captured["out_h"] = out_h
            # #64 retired the per-frame thumbnail pipeline AND the exporter's
            # thumbnail mode, so the guarantee is no longer "the flag says
            # False" — it is that this seam HAS no thumbnail mode to select.
            captured["kwargs"] = dict(kwargs)
            return SimpleNamespace(plan=object(), store=object())

    coordinator.view.preview_label = SimpleNamespace()

    snap = object()
    prepared = coordinator._prepare_preview_frame(FakeExporter(), snap, 640, 360, None, None)
    applied = coordinator._apply_prepared_preview_scene(prepared, ("req",), 640, 360)

    assert applied is True
    assert captured["applied"] is True
    assert "thumbnail" not in captured["kwargs"], (
        "the preview must not pass a thumbnail flag: #64 removed the mode from "
        "the exporter, and a stale kwarg raised TypeError on every frame"
    )
    assert captured["snap"] is snap
    assert (captured["out_w"], captured["out_h"]) == (640, 360)

def test_preview_keys_change_with_snapshot_content():
    """Content identity: the prepare/request keys include the snapshot's content
    signature, so a different frame can never reuse a cached plan (the stale /
    neighbour-frame risk). Key STABILITY was tested; content-sensitivity was not.
    """
    from tabs.image_compare.plugins.video_editor.presenter_parts.preview import (
        PreviewCoordinator,
    )

    common = (640, 360, False, None, None, 320, 180)
    sig_a = ("/a.png", "/b.png", 1.0, "vp", "st")
    sig_b = ("/a.png", "/b.png", 2.0, "vp", "st")  # different frame timestamp
    prep_a, req_a = PreviewCoordinator._build_preview_keys(sig_a, *common)
    prep_b, req_b = PreviewCoordinator._build_preview_keys(sig_b, *common)
    assert prep_a != prep_b
    assert req_a != req_b
    # Same signature -> stable key (guards the other direction).
    assert PreviewCoordinator._build_preview_keys(sig_a, *common) == (prep_a, req_a)


def test_preview_scene_forces_canvas_read_only(monkeypatch):
    """set_read_only(True) happens synchronously in render_preview_gpu, before
    the (background-threaded) prepare is even submitted."""
    from tabs.image_compare.plugins.video_editor.presenter_parts import preview as preview_module

    coordinator = _build_preview_coordinator()
    read_only_calls = []
    monkeypatch.setattr(
        preview_module,
        "apply_canvas_render_plan",
        lambda *args, **kwargs: None,
    )

    class FakeExporter:
        def prepare_snapshot_canvas_frame(self, snap, out_w, out_h, **kwargs):
            return SimpleNamespace(plan=object(), store=object())

    coordinator.export_controller = SimpleNamespace(video_exporter=FakeExporter())
    coordinator.view.preview_label = SimpleNamespace(
        set_read_only=lambda value: read_only_calls.append(value),
    )
    monkeypatch.setattr(preview_module, "viewport_fingerprint", lambda _vp: "vp")
    monkeypatch.setattr(preview_module, "frozen_value", lambda _s: "settings")

    # Cache-miss path (no matching prepare_key) submits async and returns
    # False right away — set_read_only must already have fired by then.
    assert coordinator.render_preview_gpu(
        SimpleNamespace(
            image1_path="p1",
            image2_path="p2",
            timestamp=0.0,
            viewport_state=object(),
            settings_state=object(),
        )
    ) is False
    assert read_only_calls == [True]
    # The prepare worker is real and asynchronous (see render_preview_gpu's
    # docstring); its result callback may land well after this test (and its
    # monkeypatches) has already returned, hitting the real
    # apply_canvas_render_plan with a fake plan object and blowing up on a
    # later, unrelated test via pytest-qt's Qt-event-loop exception capture.
    # Production code already guards late-arriving callbacks with
    # has_live_view(); invalidate the view the same way real teardown does
    # so any eventual delivery is a safe no-op regardless of timing.
    coordinator.on_view_destroyed()


def test_preview_scene_enables_plan_fill_clear(monkeypatch):
    """Preview never clears the whole pane with fill — pads use shader fill."""
    from tabs.image_compare.plugins.video_editor.presenter_parts import preview as preview_module

    coordinator = _build_preview_coordinator()
    monkeypatch.setattr(
        preview_module,
        "apply_canvas_render_plan",
        lambda *args, **kwargs: None,
    )

    class FakeExporter:
        def prepare_snapshot_canvas_frame(self, snap, out_w, out_h, **kwargs):
            return SimpleNamespace(
                plan=SimpleNamespace(
                    canvas_w=640,
                    canvas_h=360,
                    image1=None,
                    fill_rgba=(9, 8, 7, 255),
                    image_is_padded_composite=False,
                    render_scene=SimpleNamespace(overlay_clip_rect=(10, 5, 600, 340)),
                ),
                store=object(),
                output_width=640,
                output_height=360,
                image_dest_x=0,
                image_dest_y=0,
                debug={},
            )

    canvas = SimpleNamespace(set_read_only=lambda _v: None)
    exporter = FakeExporter()
    coordinator.view.preview_label = canvas

    for fit in (False, True):
        coordinator.fit_content_mode = fit
        prepared = coordinator._prepare_preview_frame(
            exporter, object(), 640, 360, object() if fit else None, (9, 8, 7, 255)
        )
        assert coordinator._apply_prepared_preview_scene(prepared, ("req", fit), 640, 360)
        assert canvas._use_plan_fill_clear is False


def test_preview_frame_fallback_disables_plan_fill_clear(monkeypatch):
    coordinator = _build_preview_coordinator()
    uploaded = {}

    class FakeCanvas:
        def __init__(self):
            self._use_plan_fill_clear = True
            self.runtime_state = SimpleNamespace(_store=object())

        def set_pil_layers(self, **kwargs):
            uploaded.update(kwargs)

    canvas = FakeCanvas()
    coordinator.view.preview_label = canvas
    monkeypatch.setattr(
        "engine.cache.tab_canvas_services.reset_canvas_overlays",
        lambda _c: None,
    )

    coordinator._apply_preview_frame(
        Image.new("RGBA", (8, 8), (1, 2, 3, 255)),
        request_key=("req",),
    )

    assert canvas._use_plan_fill_clear is False
    assert uploaded.get("shader_letterbox") is False


def test_video_editor_ready_does_not_activate_from_background(monkeypatch):
    from PySide6.QtCore import Qt
    from tabs.image_compare.plugins.video_editor import plugin as plugin_module
    from tabs.image_compare.plugins.video_editor.plugin import VideoEditorPlugin

    calls: list[str] = []
    dialog = SimpleNamespace(
        isMinimized=lambda: False,
        show=lambda: calls.append("show"),
        raise_=lambda: calls.append("raise"),
        activateWindow=lambda: calls.append("activate"),
    )
    app = SimpleNamespace(
        applicationState=lambda: Qt.ApplicationState.ApplicationInactive,
        activeWindow=lambda: None,
        processEvents=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(plugin_module.QApplication, "instance", lambda *_args, **_kwargs: app)

    editor_plugin = VideoEditorPlugin()
    editor_plugin._editor_dialog = dialog
    editor_plugin._show_editor_dialog()

    assert calls == ["show"]

def test_snapshot_frame_renderer_content_fit_uses_requested_resampler(monkeypatch):
    from tabs.image_compare.services.video_snapshot_rendering import geometry as geometry_mod
    from engine.store.tiled_pixel_store import TiledPixelStore

    calls = []

    def fake_write(store, source, target_w, target_h, resample, *, block=512):
        calls.append(
            {
                "size": (target_w, target_h),
                "resample": resample,
            }
        )
        # Minimal fill so allocate store is valid.
        store.write_pil(
            (0, 0, target_w, target_h),
            Image.new("RGBA", (target_w, target_h), (1, 2, 3, 255)),
        )

    monkeypatch.setattr(geometry_mod, "write_resampled_to_store", fake_write)

    fitted = geometry_mod.fit_source_to_content(
        TiledPixelStore.from_pil(Image.new("RGBA", (8, 8), (255, 255, 255, 255))),
        (4, 4),
        resize_method="LANCZOS",
    )

    assert isinstance(fitted, TiledPixelStore)
    assert fitted.size == (4, 4)
    assert calls == [
        {
            "size": (4, 4),
            "resample": Image.Resampling.LANCZOS,
        }
    ]


def test_fit_source_to_content_accepts_tiled_pixel_store():
    from engine.store.tiled_pixel_store import TiledPixelStore
    from tabs.image_compare.services.video_snapshot_rendering.geometry import (
        fit_source_to_content,
    )

    store = TiledPixelStore.from_pil(
        Image.new("RGBA", (16, 12), (255, 0, 0, 255))
    )
    fitted = fit_source_to_content(store, (8, 6), resize_method="BILINEAR")

    assert isinstance(fitted, TiledPixelStore)
    assert fitted.size == (8, 6)


def test_prescale_pair_accepts_tiled_pixel_store():
    from shared.image_processing.prescale import prescale_pair
    from engine.store.tiled_pixel_store import TiledPixelStore

    img1 = TiledPixelStore.from_pil(Image.new("RGBA", (40, 30), (255, 0, 0, 255)))
    img2 = TiledPixelStore.from_pil(Image.new("RGBA", (40, 30), (0, 255, 0, 255)))

    out1, out2 = prescale_pair(img1, img2, 20, 15, "LANCZOS")

    assert isinstance(out1, TiledPixelStore) and isinstance(out2, TiledPixelStore)
    assert out1.size == out2.size == (20, 15)


def test_plan_builder_keeps_unpadded_tiled_sources():
    from types import SimpleNamespace

    from engine.store.tiled_pixel_store import TiledPixelStore
    from tabs.image_compare.canvas.presentation.geometry import CanvasGeometry
    from tabs.image_compare.services.snapshot_render_plan_builder import (
        SnapshotRenderPlanBuilder,
    )

    img1 = TiledPixelStore.from_pil(Image.new("RGBA", (32, 24), (255, 0, 0, 255)))
    img2 = TiledPixelStore.from_pil(Image.new("RGBA", (32, 24), (0, 255, 0, 255)))
    builder = SnapshotRenderPlanBuilder(SimpleNamespace())
    geometry = CanvasGeometry(
        image_width=32,
        image_height=24,
        canvas_width=40,
        canvas_height=28,
        padding_left=4,
        padding_top=2,
        padding_right=4,
        padding_bottom=2,
        virtual_layout=None,
    )
    scene = builder._prepare_canvas_scene_images(
        canvas_plan=geometry,
        image1=img1,
        image2=img2,
        source_image1=img1,
        source_image2=img2,
        cached_diff_image=None,
        canvas_fill_rgba=(0, 0, 0, 255),
    )
    assert scene["bg1"] is img1
    assert scene["bg2"] is img2
    assert isinstance(scene["bg1"], TiledPixelStore)
    assert scene["bg1"].size == (32, 24)


def test_geometry_with_aspect_insets_folds_letterbox_into_pads():
    from tabs.image_compare.canvas.presentation.geometry import CanvasGeometry
    from tabs.image_compare.services.video_snapshot_rendering.geometry import (
        geometry_with_aspect_insets,
    )

    base = CanvasGeometry(
        image_width=100,
        image_height=50,
        canvas_width=120,
        canvas_height=60,
        padding_left=10,
        padding_top=5,
        padding_right=10,
        padding_bottom=5,
        virtual_layout=None,
    )
    # Fitted 80x50 leaves 20px horizontal slack inside the content box.
    out = geometry_with_aspect_insets(base, (80, 50))
    assert out.image_width == 80
    assert out.image_height == 50
    assert out.canvas_width == 120
    assert out.canvas_height == 60
    assert out.padding_left == 10 + 10
    assert out.padding_right == 10 + 10
    assert out.padding_top == 5
    assert out.padding_bottom == 5


def test_resolve_scaled_content_geometry_uses_target_not_letterboxed_render():
    """Vertical-only magnifier overflow must not invent left/right pads.

    ``build_render_frame_presentation`` letterboxes the unpadded image into the
    padded target, so ``frame.render_*`` is the image box. Resolving pads from
    that size (instead of ``frame.target``) shrinks content height by span_y and
    then aspect-insets fake horizontal chrome.
    """
    from types import SimpleNamespace

    from engine.cache import NormalizedBounds, VirtualCanvasLayout
    from tabs.image_compare.services.video_snapshot_rendering.geometry import (
        geometry_with_aspect_insets,
        resolve_scaled_content_geometry,
    )
    from tabs.image_compare.canvas.presentation.geometry import CanvasGeometry
    from ui.canvas_presentation.models import CanvasTarget

    # Top-only overflow: same shape as still-export with loupe at y=0.
    bounds = NormalizedBounds(0.0, 1.0, -0.06666666666666667, 1.0)
    layout = VirtualCanvasLayout(
        canvas_bounds=bounds,
        content_bounds=NormalizedBounds.unit(),
    )
    target = CanvasTarget(width=1200, height=720)
    # Letterboxed image rect inside the taller target (what render_frame does).
    frame = SimpleNamespace(
        render_width=1200,
        render_height=675,
        target=target,
        virtual_layout=layout,
    )
    target_size, content_size, pad_left, pad_top = resolve_scaled_content_geometry(
        frame
    )
    assert target_size == (1200, 720)
    assert content_size == (1200, 675)
    assert pad_left == 0
    assert pad_top == 45
    pad_right = target_size[0] - content_size[0] - pad_left
    pad_bottom = target_size[1] - content_size[1] - pad_top
    assert (pad_right, pad_bottom) == (0, 0)

    geometry = CanvasGeometry(
        image_width=content_size[0],
        image_height=content_size[1],
        canvas_width=target_size[0],
        canvas_height=target_size[1],
        padding_left=pad_left,
        padding_top=pad_top,
        padding_right=pad_right,
        padding_bottom=pad_bottom,
        virtual_layout=layout,
    )
    # Source already matches content — no aspect insets.
    out = geometry_with_aspect_insets(geometry, content_size)
    assert (out.padding_left, out.padding_right, out.padding_top, out.padding_bottom) == (
        0,
        0,
        45,
        0,
    )


def test_resolve_scaled_content_geometry_keeps_export_target_without_virtual_layout():
    """4:3 letterbox into a 16:9 export target must keep canvas = target.

    Regression: returning render size as the canvas dropped side pads; the
    export loop then stretched 749×562 → 1000×562 and MediaInfo reported a
    fat 16:9 DAR for square-pixel 4:3 Tom & Jerry title cards.
    """
    from types import SimpleNamespace

    from tabs.image_compare.services.video_snapshot_rendering.geometry import (
        resolve_scaled_content_geometry,
    )
    from ui.canvas_presentation.models import CanvasContentLayout, CanvasTarget

    target = CanvasTarget(width=1000, height=562)
    layout = CanvasContentLayout(
        canvas_width=1000,
        canvas_height=562,
        content_x=125,
        content_y=0,
        content_width=749,
        content_height=562,
    )
    frame = SimpleNamespace(
        render_width=749,
        render_height=562,
        image_dest_x=125,
        image_dest_y=0,
        target=target,
        layout=layout,
        virtual_layout=None,
    )
    target_size, content_size, pad_left, pad_top = resolve_scaled_content_geometry(
        frame
    )
    assert target_size == (1000, 562)
    assert content_size == (749, 562)
    assert pad_left == 125
    assert pad_top == 0
    assert target_size[0] - content_size[0] - pad_left == 126


def test_video_export_contain_resize_does_not_stretch_aspect():
    from PIL import Image, ImageDraw
    import numpy as np

    from tabs.image_compare.plugins.video_editor.services.video_export.render_loop import (
        _contain_resize_rgba,
    )

    src = Image.new("RGBA", (400, 300), (0, 0, 0, 255))
    draw = ImageDraw.Draw(src)
    draw.ellipse((100, 50, 300, 250), fill=(255, 255, 255, 255))
    out = _contain_resize_rgba(src, 1000, 562, (0, 0, 0, 255))
    assert out.size == (1000, 562)
    arr = np.asarray(out)
    white = (arr[:, :, 0] > 200) & (arr[:, :, 1] > 200) & (arr[:, :, 2] > 200)
    ys, xs = np.where(white)
    bw = int(xs.max() - xs.min() + 1)
    bh = int(ys.max() - ys.min() + 1)
    assert abs(bw / bh - 1.0) < 0.05
    # Pillarbox present on the sides.
    assert float(arr[:, :80, :3].mean()) < 20.0


def test_unpadded_store_plan_apply_sets_shader_letterbox_from_clip(monkeypatch):
    """prepare→apply smoke: unpadded TiledPixelStore + overlay_clip → letterbox."""
    from engine.store.tiled_pixel_store import TiledPixelStore
    from engine.cache import get_effective_export_interpolation_method
    from tabs.image_compare.canvas.presentation.geometry import CanvasGeometry
    from tabs.image_compare.canvas.presentation import plan_applicator as applicator
    from tabs.image_compare.canvas.scene import RenderScene
    from tabs.image_compare.services.snapshot_render_plan_builder import (
        SnapshotRenderPlanBuilder,
    )
    import tabs.image_compare.services.snapshot_render_plan_builder as builder_module

    store_img = TiledPixelStore.from_pil(
        Image.new("RGBA", (32, 24), (10, 20, 30, 255))
    )
    geometry = CanvasGeometry(
        image_width=32,
        image_height=24,
        canvas_width=40,
        canvas_height=28,
        padding_left=4,
        padding_top=2,
        padding_right=4,
        padding_bottom=2,
        virtual_layout=None,
    )
    fake_store = SimpleNamespace(
        viewport=SimpleNamespace(
            view_state=SimpleNamespace(
                diff_mode="off",
                channel_view_mode="RGB",
                is_horizontal=False,
                split_position_visual=0.5,
                showing_single_image_mode=0,
            ),
            render_config=SimpleNamespace(
                interpolation_method="LANCZOS",
                zoom_interpolation_method="LANCZOS",
            ),
            geometry_state=SimpleNamespace(pixmap_width=32, pixmap_height=24),
            session_data=SimpleNamespace(
                render_cache=SimpleNamespace(cached_diff_image=None)
            ),
        ),
        runtime_cache=SimpleNamespace(overlay_clip_rect=None),
        document=SimpleNamespace(image1_path="a.png", image2_path="b.png"),
    )
    monkeypatch.setattr(
        builder_module,
        "compute_canvas_plan",
        lambda *_a, **_k: geometry,
    )
    monkeypatch.setattr(
        builder_module,
        "compute_export_stroke_scales",
        lambda *_a, **_k: (1.0, 1.0, 1.0),
    )
    monkeypatch.setattr(
        builder_module,
        "query_guides_state",
        lambda _view: SimpleNamespace(
            enabled=False,
            thickness=1,
            color=SimpleNamespace(r=255, g=255, b=255, a=255),
        ),
    )
    monkeypatch.setattr(
        builder_module,
        "build_divider_export_overlay",
        lambda *_a, **_k: {"thickness": 0},
    )
    monkeypatch.setattr(
        builder_module,
        "query_active_magnifier_divider_thickness",
        lambda _store: 0,
    )
    monkeypatch.setattr(
        builder_module,
        "build_export_render_scene",
        lambda store, *_a, **_k: RenderScene(
            diff_mode_active=False,
            diff_mode_int=0,
            zoom_interpolation_method=get_effective_export_interpolation_method(
                store.viewport
            ),
        ),
    )

    def _capture_build_canvas_plan(_store, image1, image2, **kwargs):
        return SimpleNamespace(
            canvas_w=kwargs["target_size"][0],
            canvas_h=kwargs["target_size"][1],
            image1=image1,
            image2=image2,
            source_image1=kwargs.get("source_image1", image1),
            source_image2=kwargs.get("source_image2", image2),
            source_key=kwargs.get("source_key", ()),
            display_cache_key=kwargs.get("display_cache_key"),
            render_scene=kwargs["render_scene"],
            fill_rgba=kwargs.get("fill_color"),
            image_is_padded_composite=kwargs.get("image_is_padded_composite", False),
            geometry_letterbox=kwargs.get("geometry_letterbox", False),
            preserve_zoom=False,
        )

    monkeypatch.setattr(builder_module, "build_canvas_plan", _capture_build_canvas_plan)

    plan = SnapshotRenderPlanBuilder(fake_store).build_render_plan(
        store_img,
        store_img,
        source_image1=store_img,
        source_image2=store_img,
        source_key=("smoke",),
        display_cache_key=("display",),
        canvas_geometry=geometry,
        canvas_fill_rgba=(1, 2, 3, 255),
    )

    assert plan.image_is_padded_composite is False
    assert plan.geometry_letterbox is True
    assert isinstance(plan.image1, TiledPixelStore)
    assert plan.image1.size == (32, 24)
    assert plan.canvas_w == 40 and plan.canvas_h == 28
    assert plan.render_scene.overlay_clip_rect == (4, 2, 32, 24)
    assert plan.fill_rgba == (1, 2, 3, 255)

    uploaded = {}

    def _capture_layers(img1, img2, **kwargs):
        uploaded["img1"] = img1
        uploaded["shader_letterbox"] = kwargs.get("shader_letterbox")

    canvas = SimpleNamespace(
        runtime_state=SimpleNamespace(
            _letterbox_params=[(0.0, 0.0, 1.0, 1.0), (0.0, 0.0, 1.0, 1.0)],
            _canvas_frame_letterbox=None,
            _letterbox_fill_rgba=None,
            _content_rect_px=(0, 0, 40, 28),
            _inner_content_rect_px=None,
            _store=None,
            _clip_overlays_to_content_rect=False,
        ),
        begin_update_batch=lambda: None,
        end_update_batch=lambda: None,
        reset_view=lambda: None,
        set_pil_layers=_capture_layers,
        set_render_scene=lambda *_a, **_k: None,
        width=lambda: 40,
        height=lambda: 28,
    )
    monkeypatch.setattr(
        applicator,
        "registry",
        lambda: SimpleNamespace(
            apply_feature_plan_runtime_overlays=lambda *_a, **_k: None,
            get_feature_command_by_alias=lambda *_a, **_k: None,
        ),
    )
    monkeypatch.setattr(applicator, "_setup_store_bindings", lambda *_a, **_k: None)
    monkeypatch.setattr(applicator, "_apply_overlays", lambda *_a, **_k: None)
    monkeypatch.setattr(applicator, "_resolve_clip_flag", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "ui.canvas_infra.viewport.state.get_zoom_level",
        lambda *_a, **_k: 1.0,
    )
    monkeypatch.setattr(
        "ui.canvas_infra.viewport.state.get_pan_offset_x",
        lambda *_a, **_k: 0.0,
    )
    monkeypatch.setattr(
        "ui.canvas_infra.viewport.state.get_pan_offset_y",
        lambda *_a, **_k: 0.0,
    )
    monkeypatch.setattr(
        "ui.canvas_infra.viewport.state.set_zoom_level",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "ui.canvas_infra.viewport.state.set_pan_offsets",
        lambda *_a, **_k: None,
    )

    applicator._apply_plan_full(
        canvas,
        plan,
        store=fake_store,
        clip_overlays_to_image_bounds=False,
    )

    assert uploaded["img1"] is store_img
    assert uploaded["shader_letterbox"] is True
    assert canvas.runtime_state._letterbox_params[0] == pytest.approx(
        (4 / 40, 2 / 28, 32 / 40, 24 / 28)
    )
    assert canvas.runtime_state._canvas_frame_letterbox == pytest.approx(
        (0.0, 0.0, 1.0, 1.0)
    )
    assert canvas.runtime_state._letterbox_fill_rgba == (1.0, 2.0, 3.0, 255.0)

def test_snapshot_frame_renderer_prescale_target_uses_exact_output_size():
    from tabs.image_compare.plugins.video_editor.services.video_export_models import VideoRenderRequest
    from tabs.image_compare.plugins.video_editor.services.video_snapshot_rendering import SnapshotFrameRenderer
    from engine.cache import TargetSurfaceSpec

    request = VideoRenderRequest(
        target_surface=TargetSurfaceSpec(width=1954, height=1066, fill_rgba=(0, 0, 0, 0)),
        font_path=None,
        auto_crop=False,
        fit_content=False,
        global_bounds=None,
    )

    assert SnapshotFrameRenderer._resolve_prescale_target(request) == (1954, 1066)

def test_prescale_pair_unifies_mismatched_sources_directly():
    from shared.image_processing.prescale import prescale_pair

    img1 = Image.new("RGBA", (5760, 4288), (0, 0, 0, 255))
    img2 = Image.new("RGBA", (1440, 1072), (255, 255, 255, 255))

    out1, out2 = prescale_pair(img1, img2, 1451, 1080, "LANCZOS")

    assert out1.size == (1450, 1080)
    assert out2.size == (1450, 1080)

def test_snapshot_frame_renderer_prescale_target_keeps_fit_content_base_visible():
    from tabs.image_compare.plugins.video_editor.services.video_export_models import (
        GlobalCanvasBounds,
        VideoRenderRequest,
    )
    from tabs.image_compare.plugins.video_editor.services.video_snapshot_rendering import SnapshotFrameRenderer
    from engine.cache import TargetSurfaceSpec

    request = VideoRenderRequest(
        target_surface=TargetSurfaceSpec(width=594, height=562, fill_rgba=(0, 0, 0, 0)),
        font_path=None,
        auto_crop=False,
        fit_content=True,
        global_bounds=GlobalCanvasBounds(
            pad_left=1903,
            pad_right=1903,
            pad_top=0,
            pad_bottom=0,
            base_width=5760,
            base_height=4288,
            canvas_x_min=-0.33045697316157685,
            canvas_x_max=1.3304569731615768,
            canvas_y_min=0.0,
            canvas_y_max=1.0,
        ),
    )

    target_w, target_h = SnapshotFrameRenderer._resolve_prescale_target(request)

    assert target_w >= 594
    assert target_h >= 562

def test_snapshot_frame_renderer_coerces_nearest_export_resampler(monkeypatch):
    from tabs.image_compare.plugins.video_editor.services.video_export_models import VideoRenderRequest
    from tabs.image_compare.plugins.video_editor.services.video_snapshot_rendering import SnapshotFrameRenderer
    from engine.cache import TargetSurfaceSpec

    class Renderer(SnapshotFrameRenderer):
        def _prepare_canvas_frame_core(self, snap, request, img1, img2, **kwargs):
            return SimpleNamespace(
                store=object(),
                plan=SimpleNamespace(canvas_w=8, canvas_h=8),
                output_width=8,
                output_height=8,
                image_dest_x=0,
                image_dest_y=0,
                fill_rgba=(0, 0, 0, 0),
                debug=kwargs["debug"],
            )

    calls = []

    def fake_prescale_pair(img1, img2, output_width, output_height, method_name):
        calls.append(method_name)
        return img1, img2

    monkeypatch.setattr(
        "tabs.image_compare.plugins.video_editor.services.video_snapshot_rendering.prescale_pair",
        fake_prescale_pair,
    )

    snap = SimpleNamespace(
        image1_path="a",
        image2_path="b",
        viewport_state=SimpleNamespace(
            render_config=SimpleNamespace(interpolation_method="NEAREST")
        ),
    )
    request = VideoRenderRequest(
        target_surface=TargetSurfaceSpec(width=8, height=8, fill_rgba=(0, 0, 0, 0)),
        font_path=None,
        auto_crop=False,
        fit_content=False,
        global_bounds=None,
    )
    renderer = Renderer(
        image_loader=lambda *_args, **_kwargs: Image.new("RGBA", (16, 16), (0, 0, 0, 255))
    )

    renderer.prepare_canvas_frame(snap, request)

    assert calls == ["LANCZOS"]