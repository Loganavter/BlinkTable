"""Timeline thumbnails: dynamic width preserved, initial/fallback waves use
capped priority indices, and the coordinator prefers visible timeline indices.

P1 proxy-source structure contracts (no timing asserts — CI/flake
discipline): the 160x90 request resolves pyramid-coarse / cached-base
sources without decoding, falls back to the full-res path on miss, and
cover output lands exactly on 160x90.

Dogma source: docs/dev/rendering/display-image-pipeline.md (thumbnail requests
resolve through the preview tier / pyramid-coarse sources picked by
``display_image_picker``, not a fresh decode).
"""

from __future__ import annotations
from types import SimpleNamespace

from PIL import Image


_THUMB = (160, 90)


def _proxy_service(*, repo_cache, renderer):
    """Bare VideoExporterService with fakes — no GPU/tab context needed."""
    from tabs.image_compare.plugins.video_editor.services.video_export.service import (
        VideoExporterService,
    )

    service = VideoExporterService.__new__(VideoExporterService)
    service._image_repository = SimpleNamespace(_images_cache=dict(repo_cache))
    service._thumbnail_frame_renderer = renderer
    service._thumbnail_proxy_resolver = None
    service._last_thumbnail_source = None
    service._thumbnail_callback_bridge = None
    service._last_render_backend = "gpu"
    return service


def _proxy_base(width=5760, height=3240):
    return SimpleNamespace(width=width, height=height, is_open=True)


def _proxy_pyramid(*, proxy, picked=2, depth=3):
    return SimpleNamespace(
        level_count=depth,
        best_level_for_scale=lambda _scale: picked,
        level=lambda k: proxy if k == picked else (_ for _ in ()).throw(AssertionError(k)),
    )

def test_renderer_thumbnail_cover_crops_to_fixed_size():
    """Cover (not fit): any opaque render lands exactly on thumbnail_size.
    Contract changed with the transparent-margin trim (0eb33996): the old
    fit-height dynamic-width form (320x90) is gone — strip blocks are
    uniform 160x90, cover-scaled + center-cropped."""
    from tabs.image_compare.plugins.video_editor.services.thumbnails import (
        _render_thumbnail_using_renderer,
    )

    def fake_renderer(_snap, _target_w, _target_h, *, auto_crop=False):
        assert auto_crop is False
        return Image.new("RGBA", (640, 180), (255, 0, 0, 255))

    result = _render_thumbnail_using_renderer(
        snap=object(),
        thumbnail_size=(160, 90),
        auto_crop=False,
        render_scale=1.0,
        render_snapshot=fake_renderer,
    )

    assert result is not None
    assert result.size == (160, 90)

def test_finish_trims_transparent_margins_before_cover():
    """Snapshot-plan letterbox pads (transparent) must not survive into the
    strip pixmap: painted over the light strip background they read as gaps
    between blocks. Full-bleed renders pass through untouched."""
    from tabs.image_compare.plugins.video_editor.services.thumbnails import (
        _finish_thumbnail_image,
    )

    # 160x90 with transparent side margins (as dumped live: opaque content
    # only in cols 20..138) → trimmed, then cover-scaled to exactly 160x90.
    img = Image.new("RGBA", (160, 90), (0, 0, 0, 0))
    px = img.load()
    for y in range(90):
        for x in range(20, 139):
            px[x, y] = (170, 143, 107, 255)
    result = _finish_thumbnail_image(img, (160, 90))
    assert result is not None
    assert result.size == (160, 90)
    alpha = result.getdata(3)
    assert min(alpha) > 0, "no transparent margins may survive"

    # Full-bleed opaque render: byte-identical passthrough (no-op).
    full = Image.new("RGBA", (160, 90), (170, 143, 107, 255))
    assert _finish_thumbnail_image(full, (160, 90)).tobytes() == full.tobytes()

    # Fully transparent render: nothing to show.
    assert _finish_thumbnail_image(Image.new("RGBA", (160, 90), (0, 0, 0, 0)), (160, 90)) is None

def test_thumbnail_service_initial_wave_uses_priority_indices_only(monkeypatch):
    from tabs.image_compare.plugins.video_editor.services.thumbnails import ThumbnailService

    class FakeRecording:
        def evaluate_at(self, _timestamp: float):
            return object()

        def get_duration(self) -> float:
            return 10.0

    service = ThumbnailService()
    queued_indices: list[int] = []
    monkeypatch.setattr(
        service,
        "_queue_thumbnail_worker",
        lambda index, *, priority, track_finish: queued_indices.append(index),
    )

    task_id = service.generate_thumbnails(
        FakeRecording(),
        target_count=50,
        priority_indices=[8, 16, 16, 32],
        fps=60,
    )

    assert task_id > 0
    assert queued_indices == [8, 16, 32]

def test_thumbnail_service_fallback_wave_is_capped(monkeypatch):
    from tabs.image_compare.plugins.video_editor.services.thumbnails import ThumbnailService

    class FakeRecording:
        def evaluate_at(self, _timestamp: float):
            return object()

        def get_duration(self) -> float:
            return 20.0

    service = ThumbnailService()
    queued_indices: list[int] = []
    monkeypatch.setattr(
        service,
        "_queue_thumbnail_worker",
        lambda index, *, priority, track_finish: queued_indices.append(index),
    )

    service.generate_thumbnails(
        FakeRecording(),
        target_count=200,
        priority_indices=[],
        fps=60,
    )

    assert 1 <= len(queued_indices) <= 12

def test_thumbnail_coordinator_prefers_timeline_viewport_indices():
    from tabs.image_compare.plugins.video_editor.presenter_parts.thumbnails import ThumbnailCoordinator

    timeline = SimpleNamespace(
        get_visible_thumbnail_frame_indices=lambda overscan_blocks=0: [12, 24, 36],
    )
    coordinator = ThumbnailCoordinator(
        view=SimpleNamespace(timeline=timeline),
        editor_service=SimpleNamespace(
            get_current_recording=lambda: object(),
            get_fps=lambda: 60,
            get_frame_count=lambda: 100,
        ),
        playback_engine=SimpleNamespace(get_current_frame=lambda: 50),
        thumbnail_service=SimpleNamespace(),
        emit_thumbnails_updated=lambda _thumbnails: None,
    )

    assert coordinator.get_visible_frame_indices() == [12, 24, 36]


def test_timeline_resize_debounces_thumbnail_generation(monkeypatch):
    """Window/timeline resize must not queue GPU thumbnails on every pixel."""
    from PySide6.QtCore import QObject

    from tabs.image_compare.plugins.video_editor.presenter_parts.thumbnails import (
        ThumbnailCoordinator,
    )

    generate_calls: list[list[int]] = []
    thumbnail_service = SimpleNamespace(
        generate_additional_thumbnails=lambda indices, fps=None: generate_calls.append(
            list(indices)
        ),
    )
    timeline = SimpleNamespace(
        get_visible_thumbnail_frame_indices=lambda overscan_blocks=0: [4, 8, 12],
    )
    timer_parent = QObject()
    coordinator = ThumbnailCoordinator(
        view=SimpleNamespace(timeline=timeline),
        editor_service=SimpleNamespace(
            get_fps=lambda: 60,
            get_frame_count=lambda: 100,
        ),
        playback_engine=SimpleNamespace(),
        thumbnail_service=thumbnail_service,
        emit_thumbnails_updated=lambda _thumbnails: None,
        timer_parent=timer_parent,
    )
    coordinator._test_timer_parent = timer_parent
    pings: list[int] = []
    monkeypatch.setattr(
        coordinator._visible_refresh,
        "ping",
        lambda: pings.append(1),
    )

    coordinator.on_timeline_zoom_changed()
    coordinator.on_timeline_viewport_changed()
    coordinator.on_timeline_zoom_changed()

    assert pings == [1, 1, 1]
    assert generate_calls == []

    coordinator._refresh_visible_thumbnails()
    assert generate_calls == [[4, 8, 12]]


def test_thumbnail_proxy_prefers_pyramid_coarse_level(monkeypatch):
    """Structure: warm pyramid → coarse level, no full-res decode."""
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc

    proxy = object()
    base = _proxy_base()
    snap = SimpleNamespace(image1_path="a.png", image2_path="b.png")
    monkeypatch.setattr(
        svc, "pyramid_for", lambda _store: _proxy_pyramid(proxy=proxy)
    )
    calls = {}

    def fake_render_from_images(_snap, _request, image1, image2, **_kwargs):
        calls["from_images"] = (image1, image2)
        return SimpleNamespace(
            image=Image.new("RGBA", _THUMB, (10, 20, 30, 255)),
            backend="gpu",
        )

    def fake_render(_snap, _request):
        calls["render"] = True
        raise AssertionError("full-res path must not run on a pyramid hit")

    renderer = SimpleNamespace(
        render_from_images=fake_render_from_images, render=fake_render
    )
    service = _proxy_service(
        repo_cache={("a.png", False): base, ("b.png", False): base},
        renderer=renderer,
    )
    result = service.render_snapshot_thumbnail_to_pil(snap, 160, 90)

    assert calls.get("from_images") == (proxy, proxy)
    assert "render" not in calls
    assert service.last_thumbnail_source == ("pyramid", "pyramid")
    assert result is not None and result.size == (160, 90)


def test_thumbnail_proxy_falls_back_to_cached_base(monkeypatch):
    """Structure: pyramid absent/incomplete → decoded base reused, no decode."""
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc

    base = _proxy_base()
    snap = SimpleNamespace(image1_path="a.png", image2_path="b.png")
    monkeypatch.setattr(svc, "pyramid_for", lambda _store: None)
    seen = {}

    def fake_render_from_images(_snap, _request, image1, image2, **_kwargs):
        seen["images"] = (image1, image2)
        return SimpleNamespace(
            image=Image.new("RGBA", _THUMB, (40, 50, 60, 255)),
            backend="gpu",
        )

    service = _proxy_service(
        repo_cache={("a.png", False): base, ("b.png", False): base},
        renderer=SimpleNamespace(render_from_images=fake_render_from_images),
    )
    result = service.render_snapshot_thumbnail_to_pil(snap, 160, 90)

    assert seen["images"] == (base, base)
    assert service.last_thumbnail_source == ("cache", "cache")
    assert result is not None and result.size == (160, 90)


def test_thumbnail_proxy_miss_never_decodes():
    """Structure: cold caches → full-res fallback, repository never decoded."""
    base_missing = SimpleNamespace(
        _images_cache={},
        get_image=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("must not decode on the probe path")
        ),
    )
    snap = SimpleNamespace(image1_path="a.png", image2_path="b.png")
    calls = {}

    def fake_render(_snap, _request):
        calls["render"] = True
        return SimpleNamespace(
            image=Image.new("RGBA", _THUMB, (70, 80, 90, 255)),
            backend="gpu",
        )

    def fake_render_from_images(*_args, **_kwargs):
        calls["from_images"] = True
        raise AssertionError("proxy path must not run on a miss")

    from tabs.image_compare.plugins.video_editor.services.video_export.service import (
        VideoExporterService,
    )

    service = VideoExporterService.__new__(VideoExporterService)
    service._image_repository = base_missing
    service._thumbnail_frame_renderer = SimpleNamespace(
        render=fake_render, render_from_images=fake_render_from_images
    )
    service._thumbnail_proxy_resolver = None
    service._last_thumbnail_source = None
    service._thumbnail_callback_bridge = None
    service._last_render_backend = "gpu"

    result = service.render_snapshot_thumbnail_to_pil(snap, 160, 90)

    assert calls.get("render") is True
    assert "from_images" not in calls
    assert service.last_thumbnail_source == ("miss", "miss")
    assert result is not None and result.size == (160, 90)


def test_thumbnail_async_proxy_delivers_without_decode(monkeypatch):
    """Structure: async hit renders from proxy; async miss uses render_async."""
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc

    base = _proxy_base()
    snap = SimpleNamespace(image1_path="a.png", image2_path="b.png")
    monkeypatch.setattr(svc, "pyramid_for", lambda _store: None)
    delivered = []

    def fake_render_from_images(_snap, _request, _i1, _i2, **_kwargs):
        return SimpleNamespace(
            image=Image.new("RGBA", _THUMB, (1, 2, 3, 255)), backend="gpu"
        )

    def fake_render_async(_snap, _request, _cb):
        raise AssertionError("render_async must not run on a proxy hit")

    service = _proxy_service(
        repo_cache={("a.png", False): base, ("b.png", False): base},
        renderer=SimpleNamespace(
            render_from_images=fake_render_from_images,
            render_async=fake_render_async,
        ),
    )
    service.render_snapshot_thumbnail_to_pil_async(
        snap, 160, 90, delivered.append
    )

    assert len(delivered) == 1
    assert delivered[0].size == (160, 90)
    assert service.last_thumbnail_source == ("cache", "cache")


def test_thumbnail_proxy_resolver_override_wins(monkeypatch):
    """Structure: injected resolver short-circuits the built-in lookup."""
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc

    custom = object()
    snap = SimpleNamespace(image1_path="a.png", image2_path="b.png")
    monkeypatch.setattr(
        svc, "pyramid_for", lambda _store: (_ for _ in ()).throw(
            AssertionError("built-in lookup must not run when overridden")
        )
    )
    seen = {}

    def fake_render_from_images(_snap, _request, image1, image2, **_kwargs):
        seen["images"] = (image1, image2)
        return SimpleNamespace(
            image=Image.new("RGBA", _THUMB, (9, 9, 9, 255)), backend="gpu"
        )

    service = _proxy_service(
        repo_cache={},
        renderer=SimpleNamespace(render_from_images=fake_render_from_images),
    )
    service.set_thumbnail_proxy_resolver(
        lambda _path, _auto_crop, _size: (custom, "pyramid")
    )
    result = service.render_snapshot_thumbnail_to_pil(snap, 160, 90)

    assert seen["images"] == (custom, custom)
    assert service.last_thumbnail_source == ("pyramid", "pyramid")
    assert result is not None and result.size == (160, 90)


def test_cover_source_to_content_fills_thumbnail_target():
    """Structure: cover (not fit) lands exactly on the content box."""
    from tabs.image_compare.services.video_snapshot_rendering.geometry import (
        cover_source_to_content,
        is_thumbnail_target_size,
    )

    assert is_thumbnail_target_size(160, 90) is True
    assert is_thumbnail_target_size(1920, 1080) is False

    wide = Image.new("RGB", (320, 90), (200, 100, 50))
    assert cover_source_to_content(wide, (160, 90)).size == (160, 90)
    tall = Image.new("RGB", (90, 180), (50, 100, 200))
    assert cover_source_to_content(tall, (160, 90)).size == (160, 90)


def test_thumbnail_plan_covers_the_tile_canvas_not_a_fit_inset():
    """Structure: a 160x90 tile PLANS a full-bleed cover, whatever the source.

    ``_finish_thumbnail_image`` trims the plan's transparent letterbox margins
    and re-covers the PIXMAP, so the QPainter strip never showed the bars. The
    GPU band draws the PLAN directly (one tile stretched over one block cell),
    so a fit-inside plan reaches the cell as-is: a portrait source (1080x2400)
    left 40 of 160 px of content, i.e. 32 of a 128 px cell — the live "the band
    renders the wrong pixels" report (2026-11-03). The 160x90 target must
    therefore plan a cover of the WHOLE canvas, which is also what the CPU
    filmstrip composer does (``cpu_thumbnail._cover_pil(source, tile_size)``).
    """
    from engine.cache import TargetSurfaceSpec
    from engine.cache.live_snapshot import build_live_frame_snapshot
    from tabs.image_compare.plugins.video_editor.services.video_export_models import (
        VideoRenderRequest,
    )
    from tabs.image_compare.services.video_snapshot_rendering.renderer import (
        SnapshotFrameRenderer,
    )

    from core.store import Store
    from tabs.image_compare.tab import ImageCompareTab
    from tabs.registry import get_shared_tab_registry

    # ``build_live_frame_snapshot`` resolves the tab's own service against the
    # shared registry's ACTIVE session type, and this test builds a bare
    # ``Store()`` (docs/dev/tabs/isolation.md).
    ImageCompareTab().register_canvas_features()
    registry = get_shared_tab_registry()
    previous_active = registry._active_session_type
    registry._active_session_type = "image_compare"
    try:
        store = Store()
        store.create_workspace_session(session_type="image_compare", activate=True)
        # The same session shape the live canvas builds (see
        # tests/plugins/test_export_diff_support.py): the live snapshot reads
        # the session's document and viewport state.
        from core.store_viewport import SessionData
        from tabs.image_compare.state.document import DocumentModel
        from tabs.image_compare.state.models import ImageSessionState, RenderCacheState

        store.set_session_state_slot("document", DocumentModel())
        store.viewport.session_data = SessionData(
            image_state=ImageSessionState(), render_cache=RenderCacheState()
        )
        renderer = SnapshotFrameRenderer(
            image_loader=lambda *_args, **_kwargs: None
        )
        portrait = Image.new("RGBA", (90, 180), (20, 40, 60, 255))
        request = VideoRenderRequest(
            target_surface=TargetSurfaceSpec(
                width=_THUMB[0], height=_THUMB[1], fill_rgba=(0, 0, 0, 0)
            ),
            font_path=None,
            auto_crop=False,
            fit_content=False,
            global_bounds=None,
        )
        prepared = renderer.prepare_canvas_frame_from_images(
            build_live_frame_snapshot(store), request, portrait, portrait
        )
    finally:
        registry._active_session_type = previous_active

    plan = prepared.plan
    assert (int(plan.canvas_w), int(plan.canvas_h)) == _THUMB
    # The plan's pixels ARE the tile: no transparent letterbox band for a block
    # cell to stretch ahead of the frame.
    assert (int(plan.image1.width), int(plan.image1.height)) == _THUMB
    assert (int(plan.image2.width), int(plan.image2.height)) == _THUMB
    assert (prepared.image_dest_x, prepared.image_dest_y) == (0, 0)


def test_pipeline_cache_resolver_returns_pyramid_level(monkeypatch):
    """Structure: session-warmed store → same object reaches pyramid_for, L3 out."""
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc

    proxy = object()
    base = _proxy_base()
    seen = {}

    def get_pixel(path, auto_crop=None):
        seen["args"] = (path, auto_crop)
        return base

    def fake_pyramid_for(store):
        assert store is base, "resolver must return the canvas-loaded store object"
        return _proxy_pyramid(proxy=proxy)

    monkeypatch.setattr(svc, "pyramid_for", fake_pyramid_for)
    resolver = svc.make_pipeline_cache_thumbnail_resolver(
        SimpleNamespace(get_pixel=get_pixel)
    )

    image, kind = resolver("a.png", False, _THUMB)

    assert image is proxy
    assert kind == "pyramid"
    assert seen["args"] == ("a.png", False)


def test_pipeline_cache_resolver_returns_cache_when_pyramid_incomplete(monkeypatch):
    """Structure: no pyramid (or only L0 usable) → warmed base reused, no decode."""
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc

    base = _proxy_base()
    pcache = SimpleNamespace(get_pixel=lambda path, auto_crop=None: base)

    monkeypatch.setattr(svc, "pyramid_for", lambda _store: None)
    image, kind = svc.make_pipeline_cache_thumbnail_resolver(pcache)(
        "a.png", False, _THUMB
    )
    assert (image, kind) == (base, "cache")

    monkeypatch.setattr(
        svc, "pyramid_for", lambda _store: _proxy_pyramid(proxy=object(), picked=0)
    )
    image, kind = svc.make_pipeline_cache_thumbnail_resolver(pcache)(
        "a.png", False, _THUMB
    )
    assert (image, kind) == (base, "cache")


def test_pipeline_cache_resolver_none_when_store_absent():
    """Structure: store absent (or no get_pixel) → None, built-in miss path stays."""
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc

    absent = SimpleNamespace(get_pixel=lambda path, auto_crop=None: None)
    assert (
        svc.make_pipeline_cache_thumbnail_resolver(absent)("a.png", False, _THUMB)
        is None
    )
    assert (
        svc.make_pipeline_cache_thumbnail_resolver(SimpleNamespace())(
            "a.png", False, _THUMB
        )
        is None
    )
    assert (
        svc.make_pipeline_cache_thumbnail_resolver(None)("a.png", False, _THUMB)
        is None
    )


def test_presenter_pipeline_cache_resolver_guards():
    """Structure: every hop (sessions/_pipeline_cache/get_pixel) may be missing."""
    from tabs.image_compare.plugins.video_editor.presenter import (
        pipeline_cache_thumbnail_resolver_for,
    )

    assert pipeline_cache_thumbnail_resolver_for(None) is None
    assert pipeline_cache_thumbnail_resolver_for(SimpleNamespace()) is None
    assert pipeline_cache_thumbnail_resolver_for(SimpleNamespace(sessions=None)) is None
    assert (
        pipeline_cache_thumbnail_resolver_for(
            SimpleNamespace(sessions=SimpleNamespace())
        )
        is None
    )
    assert (
        pipeline_cache_thumbnail_resolver_for(
            SimpleNamespace(sessions=SimpleNamespace(_pipeline_cache=SimpleNamespace()))
        )
        is None
    )
    pcache = SimpleNamespace(get_pixel=lambda path, auto_crop=None: None)
    resolver = pipeline_cache_thumbnail_resolver_for(
        SimpleNamespace(sessions=SimpleNamespace(_pipeline_cache=pcache))
    )
    assert callable(resolver)


def test_presenter_style_injection_thumb_path_uses_pipeline_cache(monkeypatch):
    """Structure: presenter-style injection → thumb path renders from the pyramid."""
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc
    from tabs.image_compare.plugins.video_editor.presenter import (
        pipeline_cache_thumbnail_resolver_for,
    )

    proxy = object()
    base = _proxy_base()
    monkeypatch.setattr(
        svc, "pyramid_for", lambda _store: _proxy_pyramid(proxy=proxy)
    )
    pcache = SimpleNamespace(get_pixel=lambda path, auto_crop=None: base)
    main_controller = SimpleNamespace(
        sessions=SimpleNamespace(_pipeline_cache=pcache)
    )
    resolver = pipeline_cache_thumbnail_resolver_for(main_controller)
    assert callable(resolver)

    seen = {}

    def fake_render_from_images(_snap, _request, image1, image2, **_kwargs):
        seen["images"] = (image1, image2)
        return SimpleNamespace(
            image=Image.new("RGBA", _THUMB, (11, 22, 33, 255)), backend="gpu"
        )

    service = _proxy_service(
        repo_cache={},
        renderer=SimpleNamespace(render_from_images=fake_render_from_images),
    )
    service.set_thumbnail_proxy_resolver(resolver)
    result = service.render_snapshot_thumbnail_to_pil(
        SimpleNamespace(image1_path="a.png", image2_path="b.png"), 160, 90
    )

    assert seen["images"] == (proxy, proxy)
    assert service.last_thumbnail_source == ("pyramid", "pyramid")
    assert result is not None and result.size == (160, 90)


def test_video_export_image_repository_single_flight(tmp_path, monkeypatch):
    """Structure: N workers racing one cold key decode exactly once."""
    import threading

    from tabs.image_compare.plugins.video_editor.services.video_export import (
        images as images_mod,
    )

    decodes: list = []
    decode_lock = threading.Lock()

    def slow_from_path(path, auto_crop=False):
        with decode_lock:
            decodes.append(path)
        import time

        time.sleep(0.05)
        return object()

    monkeypatch.setattr(
        images_mod.TiledPixelStore, "from_path", slow_from_path
    )
    target = tmp_path / "frame.png"
    target.write_bytes(b"fake")
    repo = images_mod.VideoExportImageRepository()

    count = 8
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def worker(index):
        barrier.wait(timeout=10)
        results[index] = repo.get_image(str(target), False)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(thread.is_alive() is False for thread in threads)
    assert len(decodes) == 1
    assert all(result is results[0] and result is not None for result in results)


def test_session_state_resolver_returns_pyramid_level_for_loaded_paths(monkeypatch):
    """Structure: live session image1/image2 (same objects pyramids built for)
    resolve by loaded path; unmatched path misses."""
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc

    proxy = object()
    base1, base2 = _proxy_base(), _proxy_base()

    def peek():
        return (
            base1,
            base2,
            ["/media/ken.png"],
            ["/media/futsu.png"],
        )

    def fake_pyramid_for(store):
        assert store is base1 or store is base2
        return _proxy_pyramid(proxy=proxy)

    monkeypatch.setattr(svc, "pyramid_for", fake_pyramid_for)
    resolver = svc.make_session_state_thumbnail_resolver(peek)

    image, kind = resolver("/media/ken.png", True, _THUMB)
    assert image is proxy
    assert kind == "pyramid"

    image, kind = resolver("/media/futsu.png", True, _THUMB)
    assert image is proxy
    assert kind == "pyramid"

    assert resolver("/media/unknown.png", True, _THUMB) is None


def test_session_state_resolver_miss_never_decodes_or_raises():
    """Structure: absent state / broken peek / unmapped path → None, no raise."""
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc

    assert svc.make_session_state_thumbnail_resolver(lambda: None)(
        "a.png", False, _THUMB
    ) is None
    assert svc.make_session_state_thumbnail_resolver(None)(
        "a.png", False, _THUMB
    ) is None

    def broken():
        raise RuntimeError("boom")

    assert svc.make_session_state_thumbnail_resolver(broken)(
        "a.png", False, _THUMB
    ) is None


def test_chained_resolvers_session_first_pipeline_second():
    """Structure: session-state hit wins; pipeline cache is the fallback."""
    from tabs.image_compare.plugins.video_editor.presenter import (
        chain_thumbnail_resolvers,
    )

    calls = []

    def session_resolver(path, auto_crop=False, target_size=None):
        calls.append(("session", path))
        return None

    def cache_resolver(path, auto_crop=False, target_size=None):
        calls.append(("cache", path))
        return ("img", "cache")

    chained = chain_thumbnail_resolvers(session_resolver, cache_resolver)
    assert chained("a.png", False, _THUMB) == ("img", "cache")
    assert calls == [("session", "a.png"), ("cache", "a.png")]

    def winning(path, auto_crop=False, target_size=None):
        return ("img", "pyramid")

    chained = chain_thumbnail_resolvers(winning, cache_resolver)
    assert chained("a.png", False, _THUMB) == ("img", "pyramid")

    assert chain_thumbnail_resolvers(None, None) is None


def test_session_state_factory_peeks_document_lists_not_loaded_paths(monkeypatch):
    """Regression: loaded_image*_paths are never populated — the factory must
    match snap paths against the document image_lists (recorder's source)."""
    from types import SimpleNamespace as NS

    from tabs.image_compare.plugins.video_editor.presenter import (
        session_state_thumbnail_resolver_for,
    )
    import tabs.image_compare.plugins.video_editor.services.video_export.service as svc

    base = _proxy_base()
    istate = NS(
        image1=base,
        image2=base,
        loaded_image1_paths=[],
        loaded_image2_paths=[],
    )
    doc = NS(
        image_list1=[NS(path="/media/ken.png")],
        image_list2=[NS(path="/media/futsu.png")],
    )
    redux = NS(
        viewport=NS(session_data=NS(image_state=istate)),
        get_session_state_slot=lambda name: doc if name == "document" else None,
    )
    main_controller = NS(sessions=NS(store=redux))

    monkeypatch.setattr(
        svc, "pyramid_for", lambda _store: _proxy_pyramid(proxy="PX")
    )
    resolver = session_state_thumbnail_resolver_for(main_controller)
    assert resolver is not None

    image, kind = resolver("/media/ken.png", True, _THUMB)
    assert (image, kind) == ("PX", "pyramid")
    assert resolver("/media/other.png", True, _THUMB) is None

    assert session_state_thumbnail_resolver_for(None) is None
    assert session_state_thumbnail_resolver_for(NS(sessions=None)) is None


def test_cache_key_builder_requires_index_and_fps(tmp_path):
    """I5: the disk-cache key is unbuildable without index/fps (miss).

    A key that can be built without the index collides across indices
    sharing one snapshot (poisoning); without fps it serves another
    frame's pixels after an fps change. Both components are mandatory
    keyword-only, so a missing component fails closed to a miss.
    """
    import pytest

    from tabs.image_compare.plugins.video_editor.services.thumbnail_cache import (
        thumbnail_cache_key_for_snap,
    )

    src1 = tmp_path / "a.png"
    src2 = tmp_path / "b.png"
    Image.new("RGB", (64, 36), (200, 100, 50)).save(str(src1), "PNG")
    Image.new("RGB", (64, 36), (50, 100, 200)).save(str(src2), "PNG")
    snap = SimpleNamespace(
        timestamp=1.0,
        image1_path=str(src1),
        image2_path=str(src2),
        name1="a",
        name2="b",
        viewport_state={"frame": 3, "split": 0.5},
        settings_state={"mode": "compare"},
    )

    with pytest.raises(TypeError):
        thumbnail_cache_key_for_snap(snap, auto_crop=False)
    with pytest.raises(TypeError):
        thumbnail_cache_key_for_snap(snap, index=3, auto_crop=False)

    keyed = thumbnail_cache_key_for_snap(
        snap, index=3, fps=60, auto_crop=False, thumbnail_size=_THUMB
    )
    assert keyed is not None
    assert (
        thumbnail_cache_key_for_snap(
            snap, index=4, fps=60, auto_crop=False, thumbnail_size=_THUMB
        )
        != keyed
    )
    assert (
        thumbnail_cache_key_for_snap(
            snap, index=3, fps=30, auto_crop=False, thumbnail_size=_THUMB
        )
        != keyed
    )


def test_every_key_construction_site_passes_index_and_fps():
    """I5: AST audit — every key construction in the service passes index.

    A key built anywhere in the feed without the index component is a
    poisoning bug (two indices, one entry). This scan fails on the old
    call shape instead of letting a new call site regress it.
    """
    import ast
    from pathlib import Path

    svc_path = (
        Path(__file__).resolve().parents[4]
        / "tabs"
        / "image_compare"
        / "plugins"
        / "video_editor"
        / "services"
        / "thumbnails.py"
    )
    tree = ast.parse(svc_path.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "thumbnail_cache_key_for_snap"
    ]
    assert calls, "expected key construction sites in the thumbnail service"
    for node in calls:
        keywords = {kw.arg for kw in node.keywords}
        assert {"index", "fps"} <= keywords, (
            f"key construction at line {node.lineno} misses the I5 components"
        )
