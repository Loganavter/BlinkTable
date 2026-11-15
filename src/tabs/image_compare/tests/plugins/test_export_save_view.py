"""Export "save view": the file is the region the canvas showed.

Three seams, one rect:

* the presenter publishes the visible-view snapshot into the dialog state
  (``save_view_available`` / ``view_rect`` / ``view_native_size``) from the
  tab's canvas geometry provider — never from a widget import or a global;
* ``ExportService.export_image`` crops the rendered plan to that rect before
  "crop to images" and before the background fill, and only then resizes to a
  typed resolution;
* quick save reads the same geometry through the persisted "save view"
  setting.

The rect itself is ``shared.image_processing.view_geometry``; these tests only
pin that the three seams consume it, so the numbers below are derived from the
canvas form ``screen_uv = (canvas_uv - 0.5 + pan) * zoom + 0.5`` by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MethodType, SimpleNamespace

from PIL import Image

from domain.types import Color
from tabs.image_compare.presenters.export_presenter import ExportPresenter
from tabs.image_compare.services.image_export.models import ExportSaveContext
from tabs.image_compare.services.image_export.service import ExportService
from tabs.image_compare.services.image_export.state import ExportStateCoordinator
from tabs.image_compare.services.image_export.view_snapshot import (
    resolve_view_snapshot,
)

CANVAS = (400, 300)
# zoom 2, pan (0.25, 0.125): visible uv = (left 0.0, top 0.125, 0.5 x 0.5)
# -> canvas px (0, 38, 200, 150)  [round(0.125 * 300) == 38]
ZOOM_PAN = (2.0, 0.25, 0.125)
VIEW_RECT = (0, 38, 200, 150)


# -- shared fixture: a frame whose pixels say where they came from ------------


def _quadrant_frame(width: int, height: int) -> Image.Image:
    """RGBA frame painted per quadrant, so a wrong crop shows wrong pixels."""
    frame = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    pixels = frame.load()
    for y in range(height):
        top = y < height // 2
        for x in range(width):
            left = x < width // 2
            if top and left:
                pixels[x, y] = (255, 0, 0, 255)
            elif top:
                pixels[x, y] = (0, 255, 0, 255)
            elif left:
                pixels[x, y] = (0, 0, 255, 255)
            else:
                pixels[x, y] = (255, 255, 0, 255)
    return frame


class _FakeGpuExport:
    """Renders the plan canvas as the quadrant frame — region is what matters."""

    def __init__(self, frame_factory=None):
        self.calls = 0
        self._frame_factory = frame_factory or _quadrant_frame

    def render_plan(self, plan, *, store=None, diff_image=None):
        self.calls += 1
        return self._frame_factory(int(plan.canvas_w), int(plan.canvas_h)), {}


@dataclass
class _Plan:
    """Plan stand-in with the fields the save path touches (fill replace needs
    a real dataclass: ``plan_with_canvas_fill`` uses ``dataclasses.replace``)."""

    canvas_w: int
    canvas_h: int
    output_scale: float = 1.0
    fill_rgba: tuple[int, int, int, int] | None = None
    render_scene: object = field(default=None)


def _plan(canvas=CANVAS, clip=None):
    return _Plan(
        canvas_w=canvas[0],
        canvas_h=canvas[1],
        render_scene=SimpleNamespace(
            overlay_clip_rect=clip if clip is not None else (0, 0, *canvas)
        ),
    )


def _options(tmp_path, name, **extra):
    options = {
        "output_dir": str(tmp_path),
        "file_name": name,
        "format": "PNG",
        "quality": 95,
        "fill_background": False,
        "background_color": (255, 255, 255, 255),
        "png_compress_level": 1,
        "png_optimize": False,
        "include_metadata": False,
        "comment_text": "",
        "crop_to_image": False,
    }
    options.update(extra)
    return options


def _export(tmp_path, name, *, plan=None, frame_factory=None, **extra):
    service = ExportService(font_path_absolute="")
    service.gpu_export_service = _FakeGpuExport(frame_factory)
    pair = Image.new("RGBA", (100, 100), (120, 60, 60, 255))
    service.export_image(
        store=SimpleNamespace(),
        original_image1=pair,
        original_image2=pair,
        export_options=_options(tmp_path, name, **extra),
        render_plan=plan if plan is not None else _plan(),
        render_store=None,
    )
    with Image.open(tmp_path / f"{name}.png") as written:
        return written.convert("RGBA")


# -- geometry -----------------------------------------------------------------


def test_snapshot_for_a_zoomed_panned_view():
    snapshot = resolve_view_snapshot(*CANVAS, ZOOM_PAN)

    assert snapshot is not None
    assert snapshot.rect == VIEW_RECT
    assert snapshot.size == (200, 150), "native density: 1 canvas px per output px"
    assert snapshot.cropped is True


def test_snapshot_of_an_unzoomed_view_is_not_a_crop():
    snapshot = resolve_view_snapshot(*CANVAS, (1.0, 0.0, 0.0))

    assert snapshot is not None
    assert snapshot.cropped is False, "zoom 1 cannot reveal anything new"
    assert snapshot.rect == (0, 0, 400, 300)


def test_snapshot_without_a_canvas_or_accessor_is_none():
    assert resolve_view_snapshot(0, 0, ZOOM_PAN) is None
    assert resolve_view_snapshot(*CANVAS, None) is None


# -- the save path ------------------------------------------------------------


def test_save_view_writes_the_visible_region_at_native_density(tmp_path):
    written = _export(
        tmp_path,
        "view",
        save_view=True,
        view_rect=VIEW_RECT,
        width=200,
        height=150,
    )

    assert written.size == (200, 150)
    expected = _quadrant_frame(*CANVAS).crop(
        (VIEW_RECT[0], VIEW_RECT[1], VIEW_RECT[0] + 200, VIEW_RECT[1] + 150)
    )
    assert written.tobytes() == expected.tobytes(), (
        "the file must be the canvas region the rect names, pixel for pixel"
    )


def test_save_view_without_a_rect_writes_the_full_frame(tmp_path):
    """A missing rect degrades to today's export, not to a 1x1 crop."""
    written = _export(tmp_path, "no_rect", save_view=True)

    assert written.size == CANVAS
    assert written.tobytes() == _quadrant_frame(*CANVAS).tobytes()


def test_save_view_off_is_the_unchanged_path(tmp_path):
    written = _export(tmp_path, "full")

    assert written.size == CANVAS
    assert written.tobytes() == _quadrant_frame(*CANVAS).tobytes()


def test_save_view_beats_crop_to_image(tmp_path):
    """Both options on: the view rect decides (it is in the FULL canvas)."""
    plan = _plan(clip=(20, 10, 100, 60))
    written = _export(
        tmp_path,
        "both",
        plan=plan,
        save_view=True,
        view_rect=VIEW_RECT,
        crop_to_image=True,
    )

    assert written.size == (200, 150), (
        "crop_to_image must be skipped: the view rect is expressed in the full "
        "composition and trimming to the image area too would crop a region "
        "nobody saw"
    )


def test_save_view_honours_a_typed_resolution_after_the_crop(tmp_path):
    written = _export(
        tmp_path,
        "typed",
        save_view=True,
        view_rect=VIEW_RECT,
        width=100,
        height=75,
    )

    assert written.size == (100, 75), (
        "a user-typed resolution resizes the cropped region; it must not be "
        "the frame the crop is taken from"
    )


def test_save_view_crop_runs_before_the_background_fill(tmp_path):
    """A fill reaches transparency inside the kept area only."""

    def _frame_with_a_hole(width, height):
        frame = _quadrant_frame(width, height).copy()
        # Inside the view rect (x < 200, 38 <= y < 188): a transparent block.
        for y in range(60, 80):
            for x in range(10, 30):
                frame.putpixel((x, y), (0, 0, 0, 0))
        return frame

    written = _export(
        tmp_path,
        "fill",
        frame_factory=_frame_with_a_hole,
        save_view=True,
        view_rect=VIEW_RECT,
        fill_background=True,
        background_color=(1, 2, 3, 255),
    )

    assert written.size == (200, 150)
    assert written.getpixel((10, 30)) == (1, 2, 3, 255), (
        "the fill lands under the transparent pocket of the CROPPED frame "
        "(canvas (10, 68) -> crop (10, 30))"
    )
    assert written.getpixel((0, 0)) == (255, 0, 0, 255)


def test_select_prefers_the_full_variant_for_save_view():
    full_plan = object()
    crop_plan = object()
    ctx = ExportSaveContext(
        original1_full=None,
        original2_full=None,
        image1_for_save=None,
        image2_for_save=None,
        render_plan=full_plan,
        render_store=None,
        preview_img=None,
        suggested_filename="x",
        crop_native_size=(10, 10),
        crop_plan=crop_plan,
    )

    assert ctx.select({"crop_to_image": True}).render_plan is crop_plan
    assert ctx.select({"crop_to_image": True, "save_view": True}).render_plan is full_plan
    assert ctx.select({"save_view": True}).render_plan is full_plan


# -- the dialog state the presenter publishes ---------------------------------


class _DialogStateWithView:
    """Slot-restricted stand-in for the merged ``ExportDialogState``."""

    __slots__ = (
        "virtual_canvas_active",
        "crop_to_image_available",
        "crop_native_size",
        "crop_preview_image",
        "save_view_available",
        "view_rect",
        "view_native_size",
    )


class _DialogStateWithoutView:
    """Today's dialog state: no save-view fields, so the setattr must not raise."""

    __slots__ = (
        "virtual_canvas_active",
        "crop_to_image_available",
        "crop_native_size",
        "crop_preview_image",
    )


class _Provider:
    def __init__(self, zoom_pan):
        self._zoom_pan = zoom_pan

    def get_zoom_pan(self):
        return self._zoom_pan


class _Tab:
    def __init__(self, zoom_pan):
        self._zoom_pan = zoom_pan

    def get_canvas_geometry_provider(self):
        return _Provider(self._zoom_pan)


def _presenter(*, zoom_pan=ZOOM_PAN, dialog_state=None, native_size=CANVAS):
    opened: list[dict] = []
    state = dialog_state if dialog_state is not None else _DialogStateWithView()

    class _Dialogs:
        @staticmethod
        def show_export_dialog(*args, **kwargs):
            opened.append({"args": args, "kwargs": kwargs})
            return object()

    tab = _Tab(zoom_pan) if zoom_pan is not None else None
    registry = SimpleNamespace(get_tab=lambda _t: tab)
    presenter = SimpleNamespace(
        main_window_app=SimpleNamespace(ui=SimpleNamespace(_tab_registry=registry)),
        state=SimpleNamespace(build_export_dialog_state=lambda: state),
        ui_manager=SimpleNamespace(dialogs=_Dialogs()),
        _set_export_favorite_dir=lambda _path: None,
    )
    for name in (
        "_open_export_dialog",
        "_publish_view_state",
        "_canvas_view_state",
        "_resolve_tab_contract",
    ):
        setattr(presenter, name, MethodType(getattr(ExportPresenter, name), presenter))
    presenter._open_export_dialog(
        preview_img=None,
        suggested_filename="out",
        native_size=native_size,
    )
    return state, opened


def test_dialog_state_gets_the_visible_view_snapshot():
    state, opened = _presenter()

    assert state.save_view_available is True
    assert state.view_rect == VIEW_RECT
    assert state.view_native_size == (200, 150)
    assert len(opened) == 1


def test_dialog_state_without_a_canvas_offers_no_view():
    state, _ = _presenter(zoom_pan=None)

    assert state.save_view_available is False
    assert state.view_rect is None
    assert state.view_native_size is None


def test_unzoomed_canvas_offers_no_view():
    state, _ = _presenter(zoom_pan=(1.0, 0.0, 0.0))

    assert state.save_view_available is False, "the option would be a no-op"


def test_older_dialog_state_keeps_opening():
    """A dialog state without the fields is a normal state, not a crash."""
    state, opened = _presenter(dialog_state=_DialogStateWithoutView())

    assert not hasattr(state, "save_view_available")
    assert len(opened) == 1


# -- quick save ---------------------------------------------------------------


def _store(*, save_view: bool, crop_to_image: bool = False):
    settings = SimpleNamespace(
        export_save_view=save_view,
        export_background_color=Color(255, 255, 255, 255),
        export_last_format="PNG",
        export_quality=95,
        export_fill_background=False,
        export_crop_to_image=crop_to_image,
        export_png_compress_level=9,
        export_comment_keep_default=False,
        export_comment_text="",
        export_use_default_dir=True,
        export_default_dir="/tmp/quick",
        export_favorite_dir="",
    )
    document = SimpleNamespace(
        image_list1=[],
        image_list2=[],
        current_index1=0,
        current_index2=0,
    )
    return SimpleNamespace(
        settings=settings, get_session_state_slot=lambda _slot: document
    )


def test_quick_save_carries_the_view_when_the_setting_is_on():
    coordinator = ExportStateCoordinator(
        store=_store(save_view=True),
        main_controller=None,
        main_window_app=None,
        get_view_state=lambda: ZOOM_PAN,
    )

    options = coordinator.build_quick_export_options(CANVAS)

    assert options["save_view"] is True
    assert options["view_rect"] == VIEW_RECT
    assert (options["width"], options["height"]) == (200, 150)
    assert options["crop_to_image"] is False, "save view wins over crop-to-image"


def test_quick_save_is_unchanged_when_the_setting_is_off():
    coordinator = ExportStateCoordinator(
        store=_store(save_view=False),
        main_controller=None,
        main_window_app=None,
        get_view_state=lambda: ZOOM_PAN,
    )

    options = coordinator.build_quick_export_options(CANVAS)

    assert "save_view" not in options
    assert "view_rect" not in options
    assert "width" not in options and "height" not in options


def test_quick_save_with_the_setting_on_but_no_canvas_stays_full_frame():
    coordinator = ExportStateCoordinator(
        store=_store(save_view=True),
        main_controller=None,
        main_window_app=None,
        get_view_state=None,
    )

    options = coordinator.build_quick_export_options(CANVAS)

    assert "save_view" not in options
