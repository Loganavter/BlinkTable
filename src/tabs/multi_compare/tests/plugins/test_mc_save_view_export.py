"""Save-view export for Multi Compare ("export what the canvas shows").

The scene camera (``scene/projection.py``) decides both whether the option is
applicable and which rect it crops: at identity the option is unavailable (a
full-canvas crop would be a no-op), zoomed it writes the visible region at
native density — render the composition at its NATIVE canvas size, crop by the
snapshot rect, then resample to the requested output size only when that
differs from the crop.

Red-before: on the pre-change tree there is no ``save_view_state_fields`` /
save_view option at all, the dialog state carries no save_view_available /
view_rect / view_native_size keys, and the save path always composes at the
requested output size, so the exported PNG of a zoomed canvas was the whole
composition and every assertion here fails.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QDialog

from shared.image_processing.view_geometry import visible_view_rect, view_native_size
from tabs.multi_compare.use_cases import export as mc_export

NATIVE = (800, 600)


def _coordinate_image(width: int, height: int) -> QImage:
    """QPixmap-free QImage whose pixels encode their own coordinates.

    ``R = x % 256``, ``G = y % 256``: a crop's origin is provable from one
    pixel, which a flat fill could never show.
    """
    xs = np.arange(width, dtype=np.uint8).reshape(1, width)
    ys = np.arange(height, dtype=np.uint8).reshape(height, 1)
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[..., 0] = np.broadcast_to(xs, (height, width))
    rgba[..., 1] = np.broadcast_to(ys, (height, width))
    rgba[..., 2] = 7
    rgba[..., 3] = 255
    data = np.ascontiguousarray(rgba)
    image = QImage(data.data, width, height, width * 4, QImage.Format.Format_RGBA8888)
    return image.copy()  # own the buffer


def _state(**kwargs):
    base = dict(
        root=None,
        slots=[],
        zoom=1.0,
        pan_x=0.0,
        pan_y=0.0,
        scene_zoom=1.0,
        scene_pan_x=0.0,
        scene_pan_y=0.0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _leaf():
    from tabs.multi_compare.models import LeafNode

    return LeafNode(slot_id=1)


def _controller(*, camera, native=NATIVE, settings=None):
    composed: list[tuple[int, int]] = []

    def _compose_image(width, height, background_color=None, fill_background=False):
        composed.append((int(width), int(height)))
        return _coordinate_image(int(width), int(height))

    controller = SimpleNamespace(
        widget=SimpleNamespace(state=_state(**camera)),
        store=SimpleNamespace(settings=settings or SimpleNamespace()),
        _native_canvas_size=lambda: native,
        _compose_image=_compose_image,
        composed=composed,
    )
    return controller


def _options(**overrides):
    options = dict(
        output_dir="/tmp",
        file_name="multi_compare",
        format="PNG",
        quality=90,
        png_compress_level=6,
        png_optimize=True,
        fill_background=False,
        background_color=(0, 0, 0, 0),
        comment_text="",
        include_metadata=False,
        width=NATIVE[0],
        height=NATIVE[1],
        is_quick_save=True,
    )
    options.update(overrides)
    return options


# ---------------------------------------------------------------------------
# dialog state
# ---------------------------------------------------------------------------


def test_dialog_state_marks_save_view_unavailable_at_identity_camera():
    fields = mc_export.save_view_state_fields(_controller(camera={}))
    assert fields["save_view_available"] is False
    assert fields["view_rect"] == (0, 0, NATIVE[0], NATIVE[1])
    assert fields["view_native_size"] == NATIVE


def test_dialog_state_carries_the_visible_rect_of_the_scene_camera():
    camera = dict(scene_zoom=2.0, scene_pan_x=0.1, scene_pan_y=-0.05)
    controller = _controller(camera=camera)
    fields = mc_export.save_view_state_fields(controller)
    expected = visible_view_rect(
        NATIVE[0],
        NATIVE[1],
        camera["scene_zoom"],
        camera["scene_pan_x"],
        camera["scene_pan_y"],
    )
    assert fields["save_view_available"] is True
    assert fields["view_rect"] == expected
    assert fields["view_native_size"] == view_native_size(
        NATIVE[0],
        NATIVE[1],
        camera["scene_zoom"],
        camera["scene_pan_x"],
        camera["scene_pan_y"],
    )
    assert fields["view_native_size"] == (expected[2], expected[3])


def test_export_dialog_state_kwargs_merges_the_save_view_fields():
    controller = _controller(camera=dict(scene_zoom=2.0))
    kwargs = mc_export.export_dialog_state_kwargs(controller)
    assert kwargs["save_view_available"] is True
    assert kwargs["view_rect"] == visible_view_rect(*NATIVE, 2.0)
    assert kwargs["view_native_size"] == (NATIVE[0] // 2, NATIVE[1] // 2)
    # The historical dialog fields are still there.
    assert kwargs["virtual_canvas_active"] is True


def test_save_view_fields_fail_open_without_a_known_native_size():
    controller = _controller(camera=dict(scene_zoom=2.0), native=None)
    fields = mc_export.save_view_state_fields(controller)
    assert fields["save_view_available"] is True
    assert fields["view_rect"] is None
    assert fields["view_native_size"] is None


# ---------------------------------------------------------------------------
# composition / crop
# ---------------------------------------------------------------------------


def test_save_view_renders_native_then_crops_and_keeps_native_size():
    camera = dict(scene_zoom=2.0, scene_pan_x=0.1, scene_pan_y=-0.05)
    controller = _controller(camera=camera)
    rect = visible_view_rect(
        NATIVE[0],
        NATIVE[1],
        camera["scene_zoom"],
        camera["scene_pan_x"],
        camera["scene_pan_y"],
    )
    options = _options(
        save_view=True,
        view_rect=rect,
        width=rect[2],
        height=rect[3],
    )
    image = mc_export.compose_export_image(controller, options)

    # Composed at the native canvas, never at the crop or output size.
    assert controller.composed == [NATIVE]
    assert image.size == (rect[2], rect[3])
    # The crop really came from the view rect's origin (coordinate pixels).
    assert image.getpixel((0, 0)) == (rect[0] % 256, rect[1] % 256, 7, 255)


def test_save_view_resizes_only_when_the_output_differs_from_the_crop():
    controller = _controller(camera=dict(scene_zoom=2.0))
    rect = visible_view_rect(*NATIVE, 2.0)
    options = _options(
        save_view=True,
        view_rect=rect,
        width=200,
        height=150,
        background_color=(0, 0, 0, 0),
    )
    image = mc_export.compose_export_image(controller, options)
    assert image.size == (200, 150)
    assert controller.composed == [NATIVE]


def test_without_the_option_the_export_path_is_unchanged():
    controller = _controller(camera=dict(scene_zoom=2.0))
    options = _options(width=320, height=240)
    image = mc_export.compose_export_image(controller, options)
    assert controller.composed == [(320, 240)]
    assert image.size == (320, 240)


def test_save_view_with_a_full_canvas_rect_is_a_no_op_crop():
    controller = _controller(camera={})
    options = _options(
        save_view=True,
        view_rect=(0, 0, NATIVE[0], NATIVE[1]),
    )
    image = mc_export.compose_export_image(controller, options)
    assert controller.composed == [NATIVE]
    assert image.size == NATIVE


def test_malformed_view_rect_falls_back_to_the_full_frame_path():
    controller = _controller(camera=dict(scene_zoom=2.0))
    options = _options(save_view=True, view_rect=(0, 0, 0, 0), width=100, height=80)
    image = mc_export.compose_export_image(controller, options)
    assert controller.composed == [(100, 80)]
    assert image.size == (100, 80)


# ---------------------------------------------------------------------------
# save flows
# ---------------------------------------------------------------------------


def test_start_composite_save_writes_the_crop_at_the_requested_size():
    camera = dict(scene_zoom=2.0)
    controller = _controller(camera=camera)
    saved = []
    controller._get_save_flow = lambda: SimpleNamespace(
        start_save_worker=lambda pil, opts: saved.append((pil, opts))
    )
    rect = visible_view_rect(*NATIVE, 2.0)
    options = _options(
        save_view=True,
        view_rect=rect,
        width=250,
        height=190,
        resolution_scale=1.0,
    )
    mc_export.start_composite_save(controller, QDialog.DialogCode.Accepted, options)

    assert len(saved) == 1
    pil_image, written = saved[0]
    assert written is options
    assert controller.composed == [NATIVE]
    assert pil_image.size == (250, 190)


def test_quick_save_options_follow_the_view_when_the_setting_is_on():
    settings = SimpleNamespace(
        export_save_view=True,
        export_resolution_scale=2.0,
        export_last_format="PNG",
        export_quality=95,
        export_png_compress_level=9,
        export_png_optimize=True,
        export_fill_background=False,
        export_background_color=SimpleNamespace(r=0, g=0, b=0, a=255),
        export_default_dir="/tmp",
        export_use_default_dir=True,
        export_favorite_dir=None,
        export_comment_keep_default=False,
        export_comment_text="",
    )
    camera = dict(scene_zoom=2.0, scene_pan_x=0.1)
    controller = _controller(camera=camera, settings=settings)
    options = mc_export.build_quick_export_options(controller)

    expected_rect = visible_view_rect(
        NATIVE[0], NATIVE[1], camera["scene_zoom"], camera["scene_pan_x"], 0.0
    )
    expected_size = view_native_size(
        NATIVE[0], NATIVE[1], camera["scene_zoom"], camera["scene_pan_x"], 0.0
    )
    assert options["save_view"] is True
    assert options["view_rect"] == expected_rect
    # width/height describe the VIEW (x resolution scale), not the canvas.
    assert (options["width"], options["height"]) == (
        expected_size[0] * 2,
        expected_size[1] * 2,
    )
    assert options["width"] != NATIVE[0] * 2


def test_quick_save_options_stay_full_frame_without_the_setting_or_the_camera():
    plain = SimpleNamespace(
        export_resolution_scale=1.0,
        export_last_format="PNG",
        export_default_dir="/tmp",
        export_use_default_dir=True,
    )
    options = mc_export.build_quick_export_options(
        _controller(camera=dict(scene_zoom=2.0), settings=plain)
    )
    assert "save_view" not in options
    assert (options["width"], options["height"]) == NATIVE

    off_identity_camera = SimpleNamespace(**vars(plain))
    off_identity_camera.export_save_view = True
    options = mc_export.build_quick_export_options(
        _controller(camera={}, settings=off_identity_camera)
    )
    assert "save_view" not in options
    assert (options["width"], options["height"]) == NATIVE


def test_quick_save_writes_the_crop_end_to_end(monkeypatch):
    settings = SimpleNamespace(
        export_save_view=True,
        export_resolution_scale=1.0,
        export_last_format="PNG",
        export_default_dir="/tmp",
        export_use_default_dir=True,
    )
    camera = dict(scene_zoom=2.0, root=_leaf())
    controller = _controller(camera=camera, settings=settings)
    # The admission gates are covered by their own tests; this one pins the
    # crop/size of the write.
    monkeypatch.setattr(mc_export, "slot_ids_in_tree", lambda _root: {1})
    monkeypatch.setattr(mc_export, "has_exportable_pixels", lambda _c: True)
    saved = []
    controller._get_save_flow = lambda: SimpleNamespace(
        start_save_worker=lambda pil, opts: saved.append((pil, opts))
    )
    mc_export.on_quick_save_requested(controller)

    assert len(saved) == 1
    pil_image, options = saved[0]
    rect = visible_view_rect(*NATIVE, 2.0)
    assert controller.composed == [NATIVE]
    assert pil_image.size == (rect[2], rect[3])
    assert options["save_view"] is True


def test_pil_resample_is_lanczos():
    assert mc_export._pil_lanczos() is Image.Resampling.LANCZOS
