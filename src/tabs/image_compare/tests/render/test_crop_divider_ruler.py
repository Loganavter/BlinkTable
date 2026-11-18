"""The crop box must be ONE ruler for the image and for the divider.

Owner report 2026-11-18 (Image Compare): with a crop box active the canvas drew
the cropped image, but the split line, its clip, the canvas->widget projection
and the drag->split mapping all read ``runtime_state._content_rect_px`` — the
letterbox fit of the *uncropped* source. At the default split 0.5 both rects
are centred, so the seam looked right; at any other split the line sat up to
~60 px away from the painted cut ("the seam lags behind the line, as if
measured with different rulers").

Root cause: ``refresh_live_content_rect`` (the per-plan-application owner of
that rect) falls back to ``get_image_dims(plan.image1/2)`` when the plan
carries no envelope, and the installed display store reports the UNCROPPED
frame — measured live: a 312x512 crop of a 512x512 source still read
``size=[512, 512]``. The plan builder now carries the box-clipped dims as
``envelope_dims``; these tests pin the two consequences that matter:

* the content rect is the rect the image is actually painted into, and
* the divider's position lands exactly on the painted cut off-centre.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtGui import QColor, QImage

from engine.cache.display_tier_service import refresh_live_content_rect
from tabs.image_compare.canvas.render_config import (
    get_view_transformed_content_rect_widget_px,
)
from tabs.image_compare.use_cases.chrome_sync import format_resolution_text

SOURCE = (512, 512)
CROP = (0, 0, 312, 512)  # what an auto-crop of the owner's frames produced
WIDGET = (1024, 484)


@pytest.fixture(autouse=True)
def _register_image_compare_canvas_features():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
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


def _png_pair(tmp_path):
    """Two real 512x512 files — the crop box is scaled through the file probe."""
    paths = []
    for name, color in (("a.png", (200, 20, 20)), ("b.png", (20, 20, 200))):
        path = tmp_path / name
        Image.new("RGB", SOURCE, color).save(path)
        paths.append(str(path))
    return paths


def _qimage():
    img = QImage(SOURCE[0], SOURCE[1], QImage.Format.Format_RGBA8888)
    img.fill(QColor(120, 120, 120))
    return img


class _LazyFullFrame:
    """A store-shaped display tier that reports the UNCROPPED frame.

    Production truth on 2026-11-18: the plan's ``image1`` was a
    ``TiledPixelStore`` whose ``.size`` stayed ``[512, 512]`` while the crop
    box clipped the drawn content to 312x512. That mismatch is exactly what
    the rect owner used to fit.
    """

    def __init__(self, size):
        self.size = [int(size[0]), int(size[1])]
        self.is_open = True


def _store(paths):
    """Live-shaped store: a document with both paths and a QImage pair."""
    import tabs.image_compare.bootstrap_reducers  # noqa: F401
    from core.store import Store
    from tabs.image_compare.state.document import DocumentModel, ImageItem
    from tabs.image_compare.state.models import (
        ImageSessionState,
        PipelineCacheState,
        RenderCacheState,
    )
    from core.state_management.dispatcher import Dispatcher
    from core.store_viewport import SessionData

    store = Store()
    store.create_workspace_session(session_type="image_compare", activate=True)
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
    img1, img2 = _LazyFullFrame(SOURCE), _LazyFullFrame(SOURCE)
    store.viewport.session_data.image_state.image1 = img1
    store.viewport.session_data.image_state.image2 = img2
    return store


def _cropped_plan(store, boxes=(CROP, CROP)):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from tabs.image_compare.canvas.presentation.plan_builder import build_canvas_plan

    image_state = store.viewport.session_data.image_state
    return build_canvas_plan(
        store,
        image_state.image1,
        image_state.image2,
        crop_boxes=boxes,
    )


def test_the_plan_carries_the_box_clipped_envelope(tmp_path):
    """Without this the rect owner fits the UNCROPPED display store size."""
    store = _store(_png_pair(tmp_path))
    plan = _cropped_plan(store)

    assert (plan.canvas_w, plan.canvas_h) == (CROP[2], CROP[3]), (
        "the canvas basis is the cropped content "
        f"(got {plan.canvas_w}x{plan.canvas_h})"
    )
    assert plan.envelope_dims == (CROP[2], CROP[3], CROP[2], CROP[3]), (
        "the plan must hand the box-clipped dims to the letterbox owner; "
        f"got {plan.envelope_dims!r}"
    )


def test_the_content_rect_is_the_painted_rect_not_the_source_fit(tmp_path):
    store = _store(_png_pair(tmp_path))
    plan = _cropped_plan(store)

    state = SimpleNamespace(
        _store=store,
        _export_canvas_viewport=None,
        _content_rect_px=(270, 0, 484, 484),  # the uncropped square fit
        _inner_content_rect_px=None,
        _letterbox_params=[(0.0, 0.0, 1.0, 1.0), (0.0, 0.0, 1.0, 1.0)],
        _clip_overlays_to_content_rect=False,
    )
    canvas = SimpleNamespace(
        runtime_state=state,
        _store=store,
        width=lambda: WIDGET[0],
        height=lambda: WIDGET[1],
    )

    refresh_live_content_rect(canvas, state, plan)

    x, y, w, h = state._content_rect_px
    assert (w, h) != (484, 484), "the uncropped square fit is the bug's ruler"
    assert abs(w - h * (CROP[2] / CROP[3])) <= 1.5, (
        "the content rect must carry the cropped aspect "
        f"(got {w}x{h})"
    )
    assert x + w <= WIDGET[0] and y + h <= WIDGET[1]


def test_the_divider_lands_on_the_painted_cut_off_centre(tmp_path):
    """At split 0.25 the line and the cut must share one ruler.

    Before the fix the divider read ``270 + 484*0.25`` (the uncropped square
    fit) while the painted content was the cropped 312x512 → the cut sat at
    ``365 + 295*0.25``, ~48 px away, and the gap grew with the split.
    """
    store = _store(_png_pair(tmp_path))
    plan = _cropped_plan(store)

    state = SimpleNamespace(
        _store=store,
        _export_canvas_viewport=None,
        _content_rect_px=(270, 0, 484, 484),
        _inner_content_rect_px=None,
        _letterbox_params=[(0.0, 0.0, 1.0, 1.0), (0.0, 0.0, 1.0, 1.0)],
        _clip_overlays_to_content_rect=False,
    )
    widget = SimpleNamespace(
        runtime_state=state,
        _store=store,
        width=lambda: WIDGET[0],
        height=lambda: WIDGET[1],
    )
    refresh_live_content_rect(widget, state, plan)

    rect = get_view_transformed_content_rect_widget_px(widget)
    assert rect is not None
    cx, _cy, cw, _ch = rect

    # The rect the image is actually painted into (the plan's own basis).
    from ui.canvas_infra.scene.frame_geometry import resolve_canvas_content_geometry

    painted = resolve_canvas_content_geometry(
        widget_width=WIDGET[0],
        widget_height=WIDGET[1],
        image_width=CROP[2],
        image_height=CROP[3],
        virtual_layout=None,
    ).outer_rect_px
    assert painted is not None
    px, _py, pw, _ph = painted
    assert abs(cx - px) <= 1 and abs(cw - pw) <= 1, (
        "the divider must read the painted rect, not the uncropped fit "
        f"(divider {cx}+{cw}, painted {px}+{pw})"
    )

    split = 0.25
    cut_x = px + pw * split
    stale_line = 270 + 484 * split  # the uncropped ruler's answer
    assert abs(cut_x - stale_line) > 20, "the two rulers must actually differ here"
    # The divider paints at clip.x + clip.w * split.
    assert abs((cx + cw * split) - cut_x) < 0.5


@pytest.mark.parametrize(
    ("source", "effective", "expected"),
    [
        ((512, 512), None, "512x512"),
        ((512, 512), (512, 512), "512x512"),
        ((512, 512), (312, 512), "512x512 → 312x512"),
        (None, None, ""),
    ],
)
def test_the_hud_names_the_crop(source, effective, expected):
    assert format_resolution_text(source, effective) == expected
