"""W3c regression: metric/diff/export/video inputs honor the crop box.

With pixel stores now FULL-FRAME, comparison paths must operate on the crop
window (black borders excluded), sourced from full-frame stores:

* ``crop_pair_to_boxes`` / ``resolve_crop_boxes_for_paths`` (analysis_pair)
  — None-box parity, window clipping, preview-tier guard;
* metrics worker — PSNR over the box window equals PSNR over manually
  cropped PILs, differs from full-frame, None-box identical to today;
* cached diff — highlight over the box window sizes to the box;
* export save context — unified inputs + native size derive from
  box-clipped sizes, None-box identical to today;
* video ``resolve_images`` — loader outputs cropped to the box window.

Resolution goes only through ``effective_crop_box_for_path`` (single owner
in ``pipeline/crop_box.py``); per-image ``override`` steers it (Off skips
detection, Auto/On detect — see ``test_crop_override_wiring.py``).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("skimage")  # diff/SSIM needs scikit-image; app degrades without it

from PIL import Image

from shared.image_processing.autocrop.model import CropBox
from engine.store.pixel_ops.unify import unify_pair
from engine.store.tiled_pixel_store import TiledPixelStore
from tabs.image_compare.pipeline.crop_box import effective_crop_box_for_path
from tabs.image_compare.services.analysis.analysis_pair import (
    crop_pair_to_boxes,
    crop_source_to_box,
    resolve_crop_boxes_for_paths,
)
from tabs.image_compare.services.analysis.background_layers import (
    build_cached_diff_image,
)
from tabs.image_compare.services.analysis.cached_diff import CachedDiffService
from tabs.image_compare.services.analysis.metrics import MetricsService

BOX = CropBox(40, 40, 160, 120)  # 120x80 content window in a 200x160 frame
FULL = (200, 160)
WINDOW = (120, 80)


class _FakeCropService:
    """Detection stand-in: fixed box for known paths, None otherwise."""

    def __init__(self, paths=()):
        self._paths = {os.path.normpath(p) for p in paths}
        self.calls = 0

    def get(self, path):
        self.calls += 1
        if os.path.normpath(str(path)) in self._paths:
            return BOX
        return None


def _bordered_frame(path, center):
    canvas = Image.new("RGBA", FULL, (0, 0, 0, 255))
    canvas.paste(Image.new("RGBA", WINDOW, center + (255,)), (40, 40))
    canvas.save(path)
    return str(path)


def _frame_a(tmp_path):
    return _bordered_frame(tmp_path / "a.png", (200, 180, 160))


def _frame_b(tmp_path):
    return _bordered_frame(tmp_path / "b.png", (120, 100, 220))


# --------------------------------------------------------------------------
# resolution helper
# --------------------------------------------------------------------------


def test_resolve_none_getter_is_none_none(tmp_path):
    p = _frame_a(tmp_path)
    assert resolve_crop_boxes_for_paths(p, p, None) == (None, None)
    assert resolve_crop_boxes_for_paths(p, p, lambda: None) == (None, None)


def test_resolve_uses_only_effective_helper(tmp_path):
    p = _frame_a(tmp_path)
    svc = _FakeCropService([p])
    box1, box2 = resolve_crop_boxes_for_paths(p, str(tmp_path / "missing.png"), lambda: svc)
    assert box1 == BOX
    assert box2 is None
    assert svc.calls == 2


def test_resolve_survives_failing_service(tmp_path):
    p = _frame_a(tmp_path)

    def _boom():
        raise RuntimeError("no service")

    assert resolve_crop_boxes_for_paths(p, p, _boom) == (None, None)
    assert resolve_crop_boxes_for_paths(p, p, object()) == (None, None)


def test_override_false_skips_detection(tmp_path):
    p = _frame_a(tmp_path)
    svc = _FakeCropService([p])
    assert (
        effective_crop_box_for_path(p, crop_service=svc, override=False)
        is None
    )
    assert svc.calls == 0  # Off never consults detection


def test_real_service_detects_fixture_box(tmp_path):
    """End-to-end: real CropService finds the window the helpers then apply."""
    from tabs.image_compare.pipeline.session import ImageSession

    p = _frame_a(tmp_path)
    sess = ImageSession(session_id="box-aware-real")
    try:
        box = effective_crop_box_for_path(p, crop_service=sess.crop_service)
        assert box is not None
        store = sess.cache.get_or_load(p)
        try:
            assert store.size == FULL  # decode stays full-frame
            view = crop_source_to_box(store, box)
            assert view.size == (box.width, box.height)
        finally:
            store.close()
    finally:
        try:
            sess.cache.clear()
        except Exception:
            pass


# --------------------------------------------------------------------------
# pair cropping
# --------------------------------------------------------------------------


def test_crop_pair_none_parity_returns_inputs_untouched(tmp_path):
    p = _frame_a(tmp_path)
    store = TiledPixelStore.from_pil(Image.open(p).convert("RGBA"))
    try:
        out1, out2 = crop_pair_to_boxes(store, store, None, None)
        assert out1 is store and out2 is store
    finally:
        store.close()


def test_crop_pair_clips_to_window(tmp_path):
    p = _frame_a(tmp_path)
    full = Image.open(p).convert("RGBA")
    store = TiledPixelStore.from_pil(full)
    try:
        view1, view2 = crop_pair_to_boxes(store, full, BOX, BOX)
        assert view1.size == WINDOW
        assert view2.size == WINDOW
        assert list(view1.getdata()) == list(full.crop(BOX.to_tuple()).getdata())
        # full-frame store itself is untouched (non-destructive)
        assert store.size == FULL
    finally:
        store.close()


def test_crop_skips_preview_sized_source():
    small = Image.new("RGBA", (100, 80), (9, 9, 9, 255))
    assert crop_source_to_box(small, BOX) is small


def test_crop_full_box_is_noop():
    full = Image.new("RGBA", FULL, (9, 9, 9, 255))
    assert crop_source_to_box(full, CropBox(0, 0, *FULL)) is full
    assert crop_source_to_box(None, BOX) is None


# --------------------------------------------------------------------------
# metrics over the box window
# --------------------------------------------------------------------------


def _stores(tmp_path):
    a = TiledPixelStore.from_pil(Image.open(_frame_a(tmp_path)).convert("RGBA"))
    b = TiledPixelStore.from_pil(Image.open(_frame_b(tmp_path)).convert("RGBA"))
    return a, b


def test_metrics_box_window_matches_manual_crop(tmp_path):
    svc = MetricsService(None, None)
    s1, s2 = _stores(tmp_path)
    try:
        boxed = svc.metrics_worker_task(s1, s2, True, False, None, None, BOX, BOX)
        manual1 = Image.open(_frame_a(tmp_path)).convert("RGBA").crop(BOX.to_tuple())
        manual2 = Image.open(_frame_b(tmp_path)).convert("RGBA").crop(BOX.to_tuple())
        manual = svc.metrics_worker_task(manual1, manual2, True, False, None, None)
        full = svc.metrics_worker_task(s1, s2, True, False, None, None)
        assert boxed is not None and manual is not None and full is not None
        assert boxed[0] == manual[0]
        # identical black borders inflate full-frame PSNR — the window differs
        assert boxed[0] != full[0]
    finally:
        s1.close()
        s2.close()


def test_metrics_none_box_parity(tmp_path):
    svc = MetricsService(None, None)
    s1, s2 = _stores(tmp_path)
    try:
        without = svc.metrics_worker_task(s1, s2, True, False, None, None)
        with_none = svc.metrics_worker_task(
            s1, s2, True, False, None, None, None, None
        )
        assert without == with_none
    finally:
        s1.close()
        s2.close()


def test_metrics_async_resolves_boxes_from_document(tmp_path):
    """calculate_metrics_async passes resolved boxes into the worker task."""
    import tabs.image_compare.bootstrap_reducers  # noqa: F401

    from core.state_management.dispatcher import Dispatcher
    from core.store import Store
    from core.store_viewport import SessionData
    from tabs.image_compare.state.document import DocumentModel
    from tabs.image_compare.state.models import ImageSessionState, RenderCacheState

    pa = _frame_a(tmp_path)
    pb = _frame_b(tmp_path)
    s1, s2 = _stores(tmp_path)
    try:
        from tabs.image_compare.state.document import DocumentModel, ImageItem

        doc = DocumentModel(
            image_list1=[ImageItem(path=pa, display_name="a")],
            image_list2=[ImageItem(path=pb, display_name="b")],
            current_index1=0,
            current_index2=0,
        )
        store = Store()
        store.create_workspace_session(session_type="image_compare", activate=True)
        store.set_session_state_slot("document", doc)
        store.viewport.session_data = SessionData(
            image_state=ImageSessionState(image1=s1, image2=s2),
            render_cache=RenderCacheState(),
        )
        store.state_changed = type(
            "Sig", (), {"emit": staticmethod(lambda *_a, **_k: None)}
        )()
        store.set_dispatcher(Dispatcher(store))

        seen = {}

        import tabs.image_compare.services.analysis.metrics as metrics_mod

        real_worker = metrics_mod.GenericWorker

        class _RecordingWorker:
            def __init__(self, fn, *args, **kwargs):
                seen["fn"] = fn
                seen["args"] = args
                seen["kwargs"] = kwargs
                self.signals = SimpleNamespace(
                    result=SimpleNamespace(connect=lambda *_a, **_k: None),
                    error=SimpleNamespace(connect=lambda *_a, **_k: None),
                )

        class _Pool:
            def start(self, worker, *args, **kwargs):
                seen["started"] = True

        runtime = SimpleNamespace(
            thread_pool=_Pool(),
            get_toast_manager=lambda: None,
            ui_updates=SimpleNamespace(emit=lambda *_a, **_k: None),
        )

        svc = MetricsService(
            store, runtime, get_crop_service=lambda: _FakeCropService([pa, pb])
        )
        metrics_mod.GenericWorker = _RecordingWorker
        try:
            svc.calculate_metrics_async(True, False)
        finally:
            metrics_mod.GenericWorker = real_worker
        assert seen.get("started") is True
        # trailing worker args are (..., cap1, cap2, box1, box2)
        assert seen["args"][-2] == BOX
        assert seen["args"][-1] == BOX
    finally:
        s1.close()
        s2.close()


# --------------------------------------------------------------------------
# cached diff over the box window
# --------------------------------------------------------------------------


def test_diff_box_window_sizes_to_box(tmp_path):
    s1, s2 = _stores(tmp_path)
    try:
        boxed = build_cached_diff_image(
            s1, s2, "highlight", "RGB", box1=BOX, box2=BOX
        )
        full = build_cached_diff_image(s1, s2, "highlight", "RGB")
        assert boxed is not None and full is not None
        assert boxed.size == WINDOW
        assert full.size == FULL
    finally:
        s1.close()
        s2.close()


def test_diff_task_forwards_boxes(tmp_path):
    s1, s2 = _stores(tmp_path)
    try:
        result = CachedDiffService._generate_diff_map_task(
            s1, s2, "highlight", "RGB", False, None, None, BOX, BOX
        )
        assert result is not None
        assert result.size == WINDOW
    finally:
        s1.close()
        s2.close()


def test_diff_none_box_parity(tmp_path):
    s1, s2 = _stores(tmp_path)
    try:
        without = build_cached_diff_image(s1, s2, "grayscale", "RGB")
        with_none = build_cached_diff_image(
            s1, s2, "grayscale", "RGB", box1=None, box2=None
        )
        assert without is not None and with_none is not None
        assert without.size == with_none.size == FULL
        assert list(without.getdata()) == list(with_none.getdata())
    finally:
        s1.close()
        s2.close()


# --------------------------------------------------------------------------
# export save context over the box window
# --------------------------------------------------------------------------


def _export_store(doc, img1, img2):
    import tabs.image_compare.bootstrap_reducers  # noqa: F401

    from core.state_management.dispatcher import Dispatcher
    from core.store import Store
    from core.store_viewport import SessionData
    from tabs.image_compare.state.models import ImageSessionState, RenderCacheState

    store = Store()
    store.create_workspace_session(session_type="image_compare", activate=True)
    store.set_session_state_slot("document", doc)
    store.viewport.session_data = SessionData(
        image_state=ImageSessionState(image1=img1, image2=img2),
        render_cache=RenderCacheState(),
    )
    store.viewport.view_state.diff_mode = "off"
    store.viewport.view_state.channel_view_mode = "RGB"
    store.state_changed = type(
        "Sig", (), {"emit": staticmethod(lambda *_a, **_k: None)}
    )()
    store.set_dispatcher(Dispatcher(store))
    return store


def test_export_save_context_unifies_box_windows(tmp_path):
    from tabs.image_compare.services.image_export.context_builder import (
        ExportContextBuilder,
    )
    from tabs.image_compare.state.document import DocumentModel, ImageItem

    pa = _frame_a(tmp_path)
    pb = _frame_b(tmp_path)
    s1, s2 = _stores(tmp_path)
    try:
        doc = DocumentModel(
            image_list1=[ImageItem(path=pa, display_name="a")],
            image_list2=[ImageItem(path=pb, display_name="b")],
            current_index1=0,
            current_index2=0,
        )
        store = _export_store(doc, s1, s2)
        state = SimpleNamespace(
            build_export_dialog_state=lambda: SimpleNamespace(
                background_color=None, fill_background=False
            ),
            build_suggested_export_filename=lambda: "export",
        )
        builder = ExportContextBuilder(
            store,
            gpu_export_service=None,
            state_coordinator=state,
            get_crop_service=lambda: _FakeCropService([pa, pb]),
        )
        ctx = builder.build_save_context(include_preview=False)
        # The default variant is the whole frame (today's export); the box
        # windows live in the "crop to images" variant, which is what the option
        # renders (and what the dialog previews).
        assert ctx.image1_for_save.size == FULL
        assert ctx.has_crop_variant()
        assert ctx.crop_image1_for_save.size == WINDOW
        assert ctx.crop_image2_for_save.size == WINDOW
        assert ctx.crop_native_size == WINDOW
        selected = ctx.select({"crop_to_image": True})
        assert selected.image1_for_save.size == WINDOW
        assert selected.image2_for_save.size == WINDOW
        assert selected.render_plan is ctx.crop_plan
        assert (selected.native_width, selected.native_height) == WINDOW

        plain = ExportContextBuilder(
            store, gpu_export_service=None, state_coordinator=state
        )
        full_ctx = plain.build_save_context(include_preview=False)
        assert full_ctx.image1_for_save.size == FULL
        assert full_ctx.native_width == FULL[0]
    finally:
        s1.close()
        s2.close()


def test_unify_inputs_are_cropped_views(tmp_path):
    """The exact composition export uses: crop windows, then unify."""
    s1, s2 = _stores(tmp_path)
    try:
        v1, v2 = crop_pair_to_boxes(s1, s2, BOX, BOX)
        u1, u2 = unify_pair(v1, v2, "LANCZOS")
        assert u1.size == u2.size == WINDOW
    finally:
        s1.close()
        s2.close()


# --------------------------------------------------------------------------
# video snapshot loader outputs over the box window
# --------------------------------------------------------------------------


def test_video_resolve_images_crops_to_box(tmp_path):
    from tabs.image_compare.services.video_snapshot_rendering.images import (
        resolve_images,
    )

    pa = _frame_a(tmp_path)
    pb = _frame_b(tmp_path)
    full_a = Image.open(pa).convert("RGBA")
    full_b = Image.open(pb).convert("RGBA")
    snap = SimpleNamespace(image1_path=pa, image2_path=pb)
    request = SimpleNamespace(
        auto_crop=False,
        target_surface=SimpleNamespace(width=FULL[0], height=FULL[1]),
    )
    loader = lambda path, _auto: Image.open(path).convert("RGBA")  # noqa: E731

    cropped1, cropped2 = resolve_images(
        loader, snap, request, get_crop_service=lambda: _FakeCropService([pa, pb])
    )
    assert cropped1.size == WINDOW
    assert cropped2.size == WINDOW
    assert list(cropped1.getdata()) == list(full_a.crop(BOX.to_tuple()).getdata())
    assert list(cropped2.getdata()) == list(full_b.crop(BOX.to_tuple()).getdata())

    # export still/video paths (auto_crop=False, no getter) stay identical
    plain1, plain2 = resolve_images(loader, snap, request)
    assert plain1.size == FULL
    assert plain2.size == FULL
