"""Display-tier upload/geometry call surface (thin owner).

Whole-owner rule (engine-migration C3): the implementation lives in
``engine.cache.display_tier_service``; this module keeps same-signature
thin delegators that bind the tab collaborators (crop-box resolvers,
texture-upload queue, first-frame debug, Store transact) into hook
bundles. No logic lives here — consumers keep their import paths while
the single live implementation sits behind the service boundary.
"""

from engine.cache.display_tier_service import (
    DisplayTierHooks,
)
from engine.cache import display_tier_service as _service
from tabs.image_compare.first_frame_debug import ic_first_frame_debug

from .upload_queue import queue_prepared_texture_upload, queue_texture_upload
from .crop_clip import (
    box_dims,
    clip_box_for_image,
    clip_image_for_upload,
    original_size_for_path,
    resolve_slot_boxes,
    sanitize_box,
    slot_paths_for_widget,
)


def _tier_hooks() -> DisplayTierHooks:
    return tier_hooks()


def tier_hooks() -> DisplayTierHooks:
    """Shared display-tier hook bundle (also used by the plan-applicator surface).

    Single home for the tab→engine hook binding so the legacy-plan impl
    does not re-bind (or duplicate) the crop/upload/dispatch collaborators.
    """
    return DisplayTierHooks(
        debug=lambda widget, msg, *args: ic_first_frame_debug(widget, msg, *args),
        queue_upload=queue_texture_upload,
        queue_prepared_upload=queue_prepared_texture_upload,
        resolve_slot_boxes=resolve_slot_boxes,
        slot_paths_for_widget=slot_paths_for_widget,
        original_size_for_path=original_size_for_path,
        sanitize_box=sanitize_box,
        box_dims=box_dims,
        clip_box_for_image=clip_box_for_image,
        clip_image_for_upload=clip_image_for_upload,
        dispatch_store_rect=_dispatch_store_rect,
    )


def _dispatch_store_rect(store, rect_tuple: tuple[int, int, int, int]) -> bool:
    """Store transact shape for the eager-envelope rect (tab adaptation).

    Action construction stays tab-side so the engine never imports Store
    action vocabulary; mirrors the former ``_dispatch_store`` closure.
    """
    if store is None:
        return False
    try:
        dispatcher = store.get_dispatcher()
        if dispatcher is None:
            return False
        from core.state_management.geometry_actions import (
            SetImageDisplayRectAction,
            SetPixmapDimensionsAction,
        )
        from domain.types import Rect

        x, y, w, h = rect_tuple
        rect = Rect(x, y, w, h)
        store.transact(
            [
                SetPixmapDimensionsAction(width=w, height=h),
                SetImageDisplayRectAction(rect=rect),
            ],
            scope="viewport",
        )
        return True
    except Exception:
        return False


def upload_image(widget, qimage, slot_index: int):
    return _service.upload_image(widget, qimage, slot_index, hooks=_tier_hooks())


def upload_source_pil_image(widget, pil_image, slot_index: int, *, crop_box=None):
    return _service.upload_source_pil_image(
        widget, pil_image, slot_index, crop_box=crop_box, hooks=_tier_hooks()
    )


def upload_diff_source_pil_image(widget, pil_image):
    return _service.upload_diff_source_pil_image(
        widget, pil_image, hooks=_tier_hooks()
    )


def letterbox_pil(widget, img, slot_index: int = -1, *, crop_box=None):
    return _service.letterbox_pil(
        widget, img, slot_index, crop_box=crop_box, hooks=_tier_hooks()
    )


def update_letterbox_geometry(widget, img, slot_index: int = -1, *, crop_box=None):
    return _service.update_letterbox_geometry(
        widget, img, slot_index, crop_box=crop_box, hooks=_tier_hooks()
    )


def update_common_letterbox_geometry(
    widget,
    image1,
    image2,
    *,
    crop_boxes: tuple | None = None,
    envelope_dims: tuple | None = None,
):
    """Thin passthrough to the display-tier owner (see its docstring).

    ``envelope_dims`` is the plan's pair envelope: the live resize path passes
    it so a resize cannot re-fit the UNCROPPED preview when the crop boxes are
    not resolvable yet (owner report 2026-11-18 — the divider then drew against
    a different ruler than the image).
    """
    return _service.update_common_letterbox_geometry(
        widget,
        image1,
        image2,
        crop_boxes=crop_boxes,
        envelope_dims=envelope_dims,
        hooks=_tier_hooks(),
    )


def upload_pil_images(
    widget,
    pil_image1,
    pil_image2,
    source_image1=None,
    source_image2=None,
    source_key=None,
    display_cache_key=None,
    shader_letterbox: bool = False,
):
    return _service.upload_pil_images(
        widget,
        pil_image1,
        pil_image2,
        source_image1=source_image1,
        source_image2=source_image2,
        source_key=source_key,
        display_cache_key=display_cache_key,
        shader_letterbox=shader_letterbox,
        hooks=_tier_hooks(),
    )


def configure_offscreen_render(
    widget,
    *,
    stored_images,
    source_images,
    content_rect: tuple[int, int, int, int],
    shader_letterbox: bool = False,
):
    return _service.configure_offscreen_render(
        widget,
        stored_images=stored_images,
        source_images=source_images,
        content_rect=content_rect,
        shader_letterbox=shader_letterbox,
        hooks=_tier_hooks(),
    )


def get_letterbox_params(widget, slot: int = 0) -> tuple:
    return _service.get_letterbox_params(widget, slot, hooks=_tier_hooks())


def clear_diff_texture(widget):
    return _service.clear_diff_texture(widget, hooks=_tier_hooks())
