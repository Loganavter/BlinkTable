"""Display-tier upload/geometry + legacy-plan application service.

Single engine home for the two fixed-pair canvas owners moved out of the
image-compare tab in the engine-migration C3 wave
(``tabs/image_compare/canvas/texture_parts/base_images.py`` and
``tabs/image_compare/canvas/presentation/plan_applicator.py``):

- display-tier/envelope service: display-role texture uploads
  (``upload_image`` / ``upload_pil_images`` / source/diff variants),
  letterbox geometry (per-image and eager-max-envelope common geometry),
  offscreen/export upload configuration;
- legacy-plan impl: ``CanvasRenderPlan`` application
  (full vs scene-only paths, padded-framebuffer letterbox nesting, store
  geometry sync, runtime overlays).

Audit-Meta: pattern=state-machine size=exempt reason="display-tier upload/geometry + legacy-plan apply share one hook boundary and canvas-state commit lifecycle — splitting threads commits across modules"

Behavior-identical port: every function body matches the tab original
statement-for-statement. The only change is the call boundary — everything
the tab owned that the engine seal forbids (feature registry, render-scene
builder, crop-box resolvers, texture-upload queue, first-frame debug, Store
action construction) arrives via the ``DisplayTierHooks`` /
``LegacyPlanHooks`` bundles, which the tab call surface binds. Canvas and
runtime-state objects stay duck-typed; the engine never names a tab.

Next phases (D multi-compare waves, E passes merge) consume this surface
with their own hook bundles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from PIL import Image as PilImage

from engine.cache.image_identity import image_uid
from engine.cache.render_debug import rhi_render_debug
from engine.cache.tile_debug import log_tile_event, tile_dump_enabled
from engine.cache.unified_envelope import (
    eager_envelope_rect,
    normalize_envelope_dims,
)
from engine.store.pixel_ops.downscale import downscale_source_to_pil
from engine.store.tiled_pixel_store import TiledPixelStore
from shared.image_processing.image_dims import get_image_dims
from ui.canvas_infra.scene.frame_geometry import resolve_canvas_content_geometry
from ui.canvas_infra.viewport.focus import (
    capture_letterbox_focus,
    restore_letterbox_focus,
)
from ui.canvas_presentation.plan import plan_owns_overlay_state

if TYPE_CHECKING:  # pragma: no cover - annotation only, no runtime import
    from PySide6.QtGui import QImage

    from ui.canvas_presentation.plan import CanvasRenderPlan


# ---------------------------------------------------------------------------
# Hook bundles (tab adaptation lives at the call surface, not here)
# ---------------------------------------------------------------------------


@dataclass
class DisplayTierHooks:
    """Tab-bound collaborators for the display-tier/envelope service.

    Every field is a plain callable mirroring the tab helper it replaces,
    so the tab surface binds by reference with no adaptation logic:

    - ``debug(widget, msg, *args)`` — first-frame debug probe.
    - ``queue_upload(widget, pil_image, texture_id, slot_index=...)``.
    - ``queue_prepared_upload(widget, texture_id, qimage, slot_index)``.
    - ``resolve_slot_boxes(widget, source_key=...)`` — effective crop boxes.
    - ``slot_paths_for_widget(widget, source_key=...)`` — header-probe paths.
    - ``original_size_for_path(path)`` — probed original dims or ``None``.
    - ``sanitize_box`` / ``box_dims`` / ``clip_box_for_image`` /
      ``clip_image_for_upload`` — crop-box utilities.
    - ``dispatch_store_rect(store, rect_tuple)`` — Store transact shape for
      the eager-envelope rect; ``False`` when there is no store.
    """

    debug: Callable[..., None]
    queue_upload: Callable[..., None]
    queue_prepared_upload: Callable[..., None]
    resolve_slot_boxes: Callable[..., Any]
    slot_paths_for_widget: Callable[..., Any]
    original_size_for_path: Callable[..., Any]
    sanitize_box: Callable[..., Any]
    box_dims: Callable[..., Any]
    clip_box_for_image: Callable[..., Any]
    clip_image_for_upload: Callable[..., Any]
    dispatch_store_rect: Callable[..., bool]


@dataclass
class LegacyPlanSteps:
    """Overridable full/scene-only orchestration steps.

    Canonical engine implementations are the default; the tab surface binds
    these to its own module globals so tab-level monkeypatching keeps
    intercepting the steps (matched by the preview-contract tests).
    """

    resolve_clip_flag: Callable[..., bool]
    setup_store_bindings: Callable[..., None]
    apply_overlays: Callable[..., None]


@dataclass
class LegacyPlanHooks:
    """Tab-bound collaborators for the legacy-plan impl.

    - ``tier`` — display-tier hooks for the scene-only geometry refresh.
    - ``apply_feature_plan_runtime_overlays(canvas, plan)``.
    - ``apply_feature_live_runtime_overlays(store, canvas)``.
    - ``get_feature_command(alias)`` — feature command or ``None``.
    - ``build_render_scene(store, *, apply_channel_mode_in_shader,
      clip_overlays_to_image_bounds)``.
    - ``make_geometry_actions(cw, ch, cx, cy)`` — constructs (not dispatches)
      the Store geometry actions; the engine dispatches them.
    - ``commit_geometry_attrs(geometry_state, cw, ch, cx, cy)`` —
      dispatch-failure fallback (``setattr`` ×3, stays tab-side).
    - ``steps`` — orchestration steps, defaulting to the canonical engine
      implementations (``field`` default ``None`` → resolved per call).
    """

    debug: Callable[..., None]
    tier: DisplayTierHooks
    apply_feature_plan_runtime_overlays: Callable[..., None]
    apply_feature_live_runtime_overlays: Callable[..., None]
    get_feature_command: Callable[..., Any]
    build_render_scene: Callable[..., Any]
    make_geometry_actions: Callable[..., list]
    commit_geometry_attrs: Callable[..., None]
    steps: LegacyPlanSteps | None = field(default=None)


# ---------------------------------------------------------------------------
# Display-tier/envelope service (from texture_parts/base_images)
# ---------------------------------------------------------------------------


def canvas_dims(widget) -> tuple[int, int]:
    """Logical canvas size for letterbox/aspect-fit math.

    During tiled export the real widget is resized to a tile's pixel
    footprint, but aspect-fit geometry must still be computed against the
    full export canvas so every tile shares one coordinate system. See
    ``render_context.build_render_runtime_context`` for the counterpart.
    """
    export_canvas_viewport = getattr(
        widget.runtime_state, "_export_canvas_viewport", None
    )
    if export_canvas_viewport is not None:
        canvas_width, canvas_height, _offset_x, _offset_y = export_canvas_viewport
        return int(canvas_width), int(canvas_height)
    return widget.width(), widget.height()


def upload_image(widget, qimage: QImage, slot_index: int, *, hooks: DisplayTierHooks):
    state = widget.runtime_state

    if slot_index not in (0, 1) or qimage.isNull():
        hooks.debug(
            widget, "upload_image slot=%s SKIPPED null=%s", slot_index, qimage.isNull()
        )
        return

    hooks.debug(
        widget,
        "upload_image slot=%s size=%sx%s",
        slot_index,
        qimage.width(),
        qimage.height(),
    )
    hooks.queue_prepared_upload(
        widget, widget.texture_ids[slot_index], qimage, slot_index
    )
    widget.update()


def upload_source_pil_image(
    widget, pil_image, slot_index: int, *, crop_box=None, hooks: DisplayTierHooks
):
    source_texture_ids = widget._source_texture_ids
    if slot_index not in (0, 1) or pil_image is None:
        return

    texture_id = (
        source_texture_ids[slot_index] if slot_index < len(source_texture_ids) else 0
    )
    if not texture_id:
        return

    if isinstance(pil_image, TiledPixelStore):
        # Full-res pixels live in the memmap store; realize_tile_plan() crops
        # tiles directly from state._source_pil_images without materializing a
        # whole-image QImage here.
        return

    if crop_box is None:
        # Auto-resolve (W3a): sanitize this slot's effective box against the
        # image's own dims. Already-clipped tiers (box-clipped previews from
        # upload_pil_images) sanitize to full -> None -> no double clip.
        try:
            _raw = hooks.resolve_slot_boxes(widget)[slot_index]
            crop_box = (
                hooks.sanitize_box(_raw, get_image_dims(pil_image))
                if _raw is not None
                else None
            )
        except Exception:
            crop_box = None
    pil_image = hooks.clip_image_for_upload(pil_image, crop_box)

    hooks.queue_upload(widget, pil_image, texture_id)


def upload_diff_source_pil_image(widget, pil_image, *, hooks: DisplayTierHooks):
    state = widget.runtime_state
    if pil_image is None or not widget._diff_source_texture_id:
        state._diff_source_pil_image = None
        state._diff_source_image_id = 0
        state._diff_source_ready = False
        cache = getattr(state, "_texture_upload_cache", None)
        if cache is not None:
            cache.pop(widget._diff_source_texture_id, None)
        return

    image_id = image_uid(pil_image)
    if state._diff_source_ready and state._diff_source_image_id == image_id:
        return

    hooks.queue_upload(widget, pil_image, widget._diff_source_texture_id)
    state._diff_source_pil_image = pil_image
    state._diff_source_image_id = image_id
    state._diff_source_ready = True


def letterbox_pil(
    widget,
    img: PilImage.Image,
    slot_index: int = -1,
    *,
    crop_box=None,
    hooks: DisplayTierHooks,
) -> PilImage.Image:
    state = widget.runtime_state
    # Rare fallback: the "stored" (display) role normally resolves to the
    # small display-cache image, never the raw unify result -- but a cache
    # invalidation can momentarily leave only the TiledPixelStore behind
    # (see plan_builder.build_live_store_presentation). Never materialize
    # the store via to_pil() here (multi-GB on a 20k source); downscale
    # tile-native to the display size instead.
    is_store = isinstance(img, TiledPixelStore)
    cw, ch = canvas_dims(widget)
    if cw <= 0 or ch <= 0:
        if slot_index >= 0:
            state._letterbox_params[slot_index] = (0.0, 0.0, 1.0, 1.0)
        if is_store:
            return downscale_source_to_pil(img, img.size).convert("RGBA")
        return img.convert("RGBA")

    w, h = get_image_dims(img)
    # W3a: geometry fits the box-clipped content (crop_box already in this
    # image's coords; None -> today's full-frame behavior).
    box = hooks.sanitize_box(crop_box, (w, h)) if crop_box is not None else None
    if box is not None:
        w, h = hooks.box_dims(box, (w, h))
    geometry = resolve_canvas_content_geometry(
        widget_width=cw,
        widget_height=ch,
        image_width=w,
        image_height=h,
        virtual_layout=None,
    )
    inner = geometry.inner_rect_px or (0, 0, cw, ch)
    offset_x, offset_y, nw, nh = inner
    if slot_index >= 0:

        state._letterbox_params[slot_index] = (
            offset_x / float(cw),
            offset_y / float(ch),
            nw / float(cw),
            nh / float(ch),
        )
        if slot_index == 0:
            state._content_rect_px = geometry.outer_rect_px or (0, 0, cw, ch)
            state._inner_content_rect_px = inner
            state._clip_overlays_to_content_rect = False
    if is_store:
        if box is not None:
            # Rare fallback (see note above): materialize the box only via
            # the existing sub-rect util, never the full frame.
            base = img.crop(box)
            scaled = base.convert("RGBA").resize((nw, nh), PilImage.Resampling.BILINEAR)
        else:
            scaled = downscale_source_to_pil(
                img, (nw, nh), resample=PilImage.Resampling.BILINEAR
            )
    else:
        base = img.crop(box) if box is not None else img
        scaled = base.convert("RGBA").resize((nw, nh), PilImage.Resampling.BILINEAR)
    result = PilImage.new("RGBA", (cw, ch), (0, 0, 0, 0))
    result.paste(scaled, (offset_x, offset_y))
    return result


# helper removed — use shared helper (B6)


def update_letterbox_geometry(
    widget,
    img: PilImage.Image | None,
    slot_index: int = -1,
    *,
    crop_box=None,
    hooks: DisplayTierHooks,
):
    state = widget.runtime_state
    cw, ch = canvas_dims(widget)
    w, h = get_image_dims(img)
    box = hooks.sanitize_box(crop_box, (w, h)) if crop_box is not None else None
    if box is not None:
        w, h = hooks.box_dims(box, (w, h))
    if img is None or cw <= 0 or ch <= 0 or w <= 0 or h <= 0:
        if slot_index >= 0:
            state._letterbox_params[slot_index] = (0.0, 0.0, 1.0, 1.0)
            if slot_index == 0:
                state._content_rect_px = (0, 0, max(1, cw), max(1, ch))
                state._inner_content_rect_px = state._content_rect_px
                state._clip_overlays_to_content_rect = False
        return

    geometry = resolve_canvas_content_geometry(
        widget_width=cw,
        widget_height=ch,
        image_width=w,
        image_height=h,
        virtual_layout=None,
    )
    inner = geometry.inner_rect_px
    outer = geometry.outer_rect_px
    if inner is None or outer is None:
        return
    offset_x, offset_y, nw, nh = inner
    if slot_index >= 0:

        state._letterbox_params[slot_index] = (
            offset_x / float(cw),
            offset_y / float(ch),
            nw / float(cw),
            nh / float(ch),
        )
        if slot_index == 0:
            state._content_rect_px = outer
            state._inner_content_rect_px = inner
            state._clip_overlays_to_content_rect = False


def update_common_letterbox_geometry(
    widget,
    image1: PilImage.Image | None,
    image2: PilImage.Image | None,
    *,
    crop_boxes: tuple | None = None,
    envelope_dims: tuple[int, int, int, int] | None = None,
    hooks: DisplayTierHooks,
) -> None:
    """Keep both comparison sides in one canvas coordinate system.

    Eager max envelope (single owner): when both sides have sizes, compute
    pw,ph = max(w1,w2), max(h1,h2) once and derive a single fitted rect via
    shared helper ``eager_envelope_rect`` (one
    ``resolve_canvas_content_geometry(cw,ch,pw,ph)`` call). Both letterbox
    slots receive the same ux/cw,uy/ch,uw/cw,uh/ch, dispatched via
    store.transact. No hold, no more_pending, no Store predicted field.
    Fallback to per-image letterbox only when one side has no size.

    ``envelope_dims`` — the plan's SOURCE-space envelope ``(w1,h1,w2,h2)``
    (``CanvasRenderPlan.envelope_dims``): the dims to fit INSTEAD of this
    tier's own ``get_image_dims(image1/2)``. Fitting the display dims makes the
    drawn letterbox disagree with the canvas basis of a live plan on preview
    frames (each preview is normalised to a 1024 long edge, so a preview pair's
    envelope has a different aspect than the source pair's; owner report
    2026-10-27). ``None`` keeps today's display-dim fit bit-identically, and the
    kwarg is ignored whenever a crop box is active — there the box-clipped
    display dims are the truth.

    W3a: ``crop_boxes`` are per-display-image boxes in each image's own
    coords (as computed by ``upload_pil_images`` via ``clip_box_for_image``,
    already scaled for preview tiers). ``None`` (default) auto-resolves the
    raw effective boxes and sanitizes them against the display dims — which
    is exact for full-frame stores and a no-op for already-clipped tiers.
    Envelope rects and dispatched pixmap dims become box-clipped; a
    ``None`` box keeps today's behavior identically.
    """
    state = widget.runtime_state
    cw, ch = canvas_dims(widget)
    while len(state._letterbox_params) < 2:
        state._letterbox_params.append((0.0, 0.0, 1.0, 1.0))
    if cw <= 0 or ch <= 0:
        update_letterbox_geometry(widget, image1, slot_index=0, hooks=hooks)
        update_letterbox_geometry(widget, image2, slot_index=1, hooks=hooks)
        return

    w1, h1 = get_image_dims(image1) if image1 is not None else (0, 0)
    w2, h2 = get_image_dims(image2) if image2 is not None else (0, 0)
    if crop_boxes is None:
        try:
            crop_boxes = hooks.resolve_slot_boxes(widget)
        except Exception:
            crop_boxes = (None, None)
    try:
        _cb1 = crop_boxes[0] if crop_boxes is not None else None
        _cb2 = crop_boxes[1] if crop_boxes is not None else None
    except Exception:
        _cb1 = _cb2 = None
    _box1 = hooks.sanitize_box(_cb1, (w1, h1)) if _cb1 is not None else None
    _box2 = hooks.sanitize_box(_cb2, (w2, h2)) if _cb2 is not None else None
    _plan_envelope = (
        normalize_envelope_dims(envelope_dims) if _box1 is None and _box2 is None else None
    )
    if _plan_envelope is not None:
        w1, h1, w2, h2 = _plan_envelope
    if _box1 is not None:
        w1, h1 = hooks.box_dims(_box1, (w1, h1))
    if _box2 is not None:
        w2, h2 = hooks.box_dims(_box2, (w2, h2))
    have1 = image1 is not None and w1 > 0 and h1 > 0
    have2 = image2 is not None and w2 > 0 and h2 > 0
    if not have1 and not have2:
        state._letterbox_params[0] = (0.0, 0.0, 1.0, 1.0)
        state._letterbox_params[1] = (0.0, 0.0, 1.0, 1.0)
        state._content_rect_px = (0, 0, max(1, cw), max(1, ch))
        state._inner_content_rect_px = state._content_rect_px
        state._clip_overlays_to_content_rect = False
        return

    def _dispatch_store(rect_tuple: tuple[int, int, int, int]) -> bool:
        store = getattr(state, "_store", None) or getattr(widget, "_store", None)
        return hooks.dispatch_store_rect(store, rect_tuple)

    if have1 and have2:
        letterbox, candidate_rect = eager_envelope_rect(cw, ch, [(w1, h1), (w2, h2)])
        state._letterbox_params[0] = letterbox
        state._letterbox_params[1] = letterbox
        state._content_rect_px = candidate_rect
        state._inner_content_rect_px = candidate_rect
        state._clip_overlays_to_content_rect = False
        _dispatch_store(candidate_rect)
        return

    update_letterbox_geometry(widget, image1, slot_index=0, crop_box=_box1, hooks=hooks)
    update_letterbox_geometry(widget, image2, slot_index=1, crop_box=_box2, hooks=hooks)
    inner = getattr(state, "_inner_content_rect_px", None) or getattr(
        state, "_content_rect_px", None
    )
    if inner is not None:
        _dispatch_store(inner)


def upload_pil_images(
    widget,
    pil_image1,
    pil_image2,
    source_image1=None,
    source_image2=None,
    source_key=None,
    display_cache_key=None,
    shader_letterbox: bool = False,
    *,
    hooks: DisplayTierHooks,
):
    state = widget.runtime_state
    # W3a: effective boxes in full-source coords (None per slot -> today's
    # behavior identically). Resolved once here; every consumer below works
    # with per-image boxes scaled via clip_box_for_image.
    try:
        _raw_boxes = hooks.resolve_slot_boxes(widget, source_key=source_key)
    except Exception:
        _raw_boxes = (None, None)
    try:
        _raw1, _raw2 = _raw_boxes[0], _raw_boxes[1]
    except Exception:
        _raw1 = _raw2 = None

    _d1 = get_image_dims(pil_image1)
    _d2 = get_image_dims(pil_image2)
    _s1 = get_image_dims(source_image1)
    _s2 = get_image_dims(source_image2)
    # Reference sizes: the header-probed originals when available, else the
    # largest live tier per slot (exact whenever live == original, which
    # covers non-unified stores and full-size sources).
    _p1 = _p2 = None
    try:
        _paths = hooks.slot_paths_for_widget(widget, source_key=source_key)
        _p1, _p2 = _paths[0], _paths[1]
    except Exception:
        _p1 = _p2 = None

    def _full_size(display_dims, source_dims, path):
        try:
            _o = hooks.original_size_for_path(path)
            if _o is not None and _o[0] > 0 and _o[1] > 0:
                return _o
        except Exception:
            pass
        _best = (0, 0)
        for _cand in (display_dims, source_dims):
            try:
                if _cand is not None and int(_cand[0]) * int(_cand[1]) > _best[0] * _best[1]:
                    _best = (int(_cand[0]), int(_cand[1]))
            except Exception:
                continue
        return _best

    _f1 = _full_size(_d1, _s1, _p1)
    _f2 = _full_size(_d2, _s2, _p2)
    _box1 = hooks.clip_box_for_image(_raw1, full_size=_f1, img_size=_d1)
    _box2 = hooks.clip_box_for_image(_raw2, full_size=_f2, img_size=_d2)
    _sbox1 = hooks.clip_box_for_image(_raw1, full_size=_f1, img_size=_s1)
    _sbox2 = hooks.clip_box_for_image(_raw2, full_size=_f2, img_size=_s2)
    # The SOURCE-space envelope the plan sized its canvas basis from
    # (``CanvasRenderPlan.envelope_dims``) — the plan builder derives it from
    # these same ``source_image1/2`` objects, so this is the same number, not a
    # second guess. Passed to the letterbox owner so the drawn rect cannot
    # follow whichever display tier is installed (a preview pair's envelope has
    # a different aspect than the source pair's). ``None`` whenever a crop box
    # is in play: the box-clipped display dims are the truth there, and that
    # path stays bit-identical.
    _envelope_dims = None
    if (
        _raw1 is None
        and _raw2 is None
        and source_image1 is not None
        and source_image2 is not None
        and _s1[0] > 0
        and _s1[1] > 0
        and _s2[0] > 0
        and _s2[1] > 0
    ):
        _envelope_dims = (int(_s1[0]), int(_s1[1]), int(_s2[0]), int(_s2[1]))
    # Box tuples join the change signatures so a late box arrival (cold
    # cache -> warmed) re-drives geometry + uploads like any content swap.
    _sig_boxes = (
        _raw1.to_tuple() if hasattr(_raw1, "to_tuple") else _raw1,
        _raw2.to_tuple() if hasattr(_raw2, "to_tuple") else _raw2,
    )
    stored_ids = (
        display_cache_key
        if display_cache_key is not None
        else (
            image_uid(pil_image1),
            image_uid(pil_image2),
            pil_image1.size if pil_image1 is not None else None,
            pil_image2.size if pil_image2 is not None else None,
            bool(shader_letterbox),
            _sig_boxes,
        )
    )
    stored_changed = stored_ids != state._stored_image_ids
    if stored_changed:
        if tile_dump_enabled():
            log_tile_event(
                "stored_changed",
                old_ids=str(state._stored_image_ids),
                new_ids=str(stored_ids),
            )
    hooks.debug(
        widget,
        "upload_pil_images stored_changed=%s stored=[%s,%s] source=[%s,%s] source_key=%s",
        stored_changed,
        pil_image1 is not None,
        pil_image2 is not None,
        source_image1 is not None,
        source_image2 is not None,
        source_key,
    )
    state._stored_pil_images = [pil_image1, pil_image2]
    state._stored_image_ids = stored_ids
    state._shader_letterbox_mode = bool(shader_letterbox)
    if __debug__ and not shader_letterbox:
        for slot_img in (pil_image1, pil_image2):
            if slot_img is not None and isinstance(slot_img, TiledPixelStore):
                raise AssertionError(
                    "stored display role must be PIL.Image, not TiledPixelStore"
                )
    has_explicit_source = source_image1 is not None and source_image2 is not None
    if has_explicit_source:
        explicit_source_sig = (
            image_uid(source_image1),
            image_uid(source_image2),
            source_image1.size if source_image1 is not None else None,
            source_image2.size if source_image2 is not None else None,
            _sig_boxes,
        )
        source_ids = (
            (source_key, explicit_source_sig)
            if source_key is not None
            else explicit_source_sig
        )
        source_changed = source_ids != state._source_image_ids
        state._source_pil_images = [source_image1, source_image2]
        state._source_image_ids = source_ids
        if source_changed:
            state._source_images_ready = False
    else:
        source_changed = state._source_image_ids is not None
        state._source_pil_images = [None, None]
        state._source_image_ids = None
        state._source_images_ready = False
        cache = getattr(state, "_texture_upload_cache", None)
        if cache is not None:
            for texture_id in widget._source_texture_ids:
                cache.pop(texture_id, None)
    # Single geometry update — previously called twice (once per slot) with identical args
    if stored_changed and state._shader_letterbox_mode and (pil_image1 or pil_image2):
        update_common_letterbox_geometry(
            widget,
            pil_image1,
            pil_image2,
            crop_boxes=(_box1, _box2),
            envelope_dims=_envelope_dims,
            hooks=hooks,
        )
    if pil_image1 and stored_changed:
        if state._shader_letterbox_mode:
            # docs/dev/rendering/tile-rendering-system.md Phase 3: skip the
            # whole-image upload for lazy sources -- see the matching
            # comment in upload_source_pil_image; realize_tile_plan()
            # crops tiles from state._stored_pil_images (set below)
            # directly, so this texture would just be wasted main-thread
            # work materializing the whole memmap.
            if isinstance(pil_image1, TiledPixelStore):
                state._images_uploaded[0] = True
            else:
                hooks.queue_upload(
                    widget,
                    hooks.clip_image_for_upload(pil_image1, _box1),
                    widget.texture_ids[0],
                    slot_index=0,
                )
        else:
            lb1 = letterbox_pil(widget, pil_image1, slot_index=0, crop_box=_box1, hooks=hooks)
            hooks.queue_upload(widget, lb1, widget.texture_ids[0], slot_index=0)
    if pil_image2 and stored_changed:
        if state._shader_letterbox_mode:
            pass  # geometry already updated above
            if isinstance(pil_image2, TiledPixelStore):
                state._images_uploaded[1] = True
            else:
                hooks.queue_upload(
                    widget,
                    hooks.clip_image_for_upload(pil_image2, _box2),
                    widget.texture_ids[1],
                    slot_index=1,
                )
        else:
            lb2 = letterbox_pil(widget, pil_image2, slot_index=1, crop_box=_box2, hooks=hooks)
            hooks.queue_upload(widget, lb2, widget.texture_ids[1], slot_index=1)
    if has_explicit_source:
        try:
            if source_changed or not state._source_images_ready:
                if source_image1 is not None:
                    upload_source_pil_image(widget, source_image1, 0, crop_box=_sbox1, hooks=hooks)
                if source_image2 is not None:
                    upload_source_pil_image(widget, source_image2, 1, crop_box=_sbox2, hooks=hooks)
                state._source_images_ready = True
        except Exception:
            state._source_images_ready = False
            if hasattr(widget, "_schedule_source_preload"):
                widget._schedule_source_preload()
    widget.update()


def configure_offscreen_render(
    widget,
    *,
    stored_images,
    source_images,
    content_rect: tuple[int, int, int, int],
    shader_letterbox: bool = False,
    hooks: DisplayTierHooks,
):
    state = widget.runtime_state
    prev_stored_images = tuple(state._stored_pil_images)
    prev_source_images = tuple(state._source_pil_images)
    prev_content_rect = state._content_rect_px
    prev_shader_letterbox = state._shader_letterbox_mode
    images_unchanged = (
        prev_stored_images[0] is stored_images[0]
        and prev_stored_images[1] is stored_images[1]
        and prev_source_images[0] is source_images[0]
        and prev_source_images[1] is source_images[1]
    )
    state._store = None
    state._shader_letterbox_mode = bool(shader_letterbox)
    state._letterbox_params[0] = (0.0, 0.0, 1.0, 1.0)
    state._letterbox_params[1] = (0.0, 0.0, 1.0, 1.0)
    state._content_rect_px = content_rect
    state._inner_content_rect_px = None
    state._clip_overlays_to_content_rect = False
    state._stored_pil_images = [stored_images[0], stored_images[1]]
    state._source_pil_images = [source_images[0], source_images[1]]
    state._source_images_ready = bool(
        source_images[0] is not None and source_images[1] is not None
    )

    if (
        images_unchanged
        and prev_content_rect == content_rect
        and prev_shader_letterbox == bool(shader_letterbox)
    ):
        return

    hooks.queue_upload(widget, stored_images[0], widget.texture_ids[0], slot_index=0)
    hooks.queue_upload(widget, stored_images[1], widget.texture_ids[1], slot_index=1)

    if source_images[0] is not None:
        hooks.queue_upload(widget, source_images[0], widget._source_texture_ids[0])
    if source_images[1] is not None:
        hooks.queue_upload(widget, source_images[1], widget._source_texture_ids[1])


def get_letterbox_params(widget, slot: int = 0, *, hooks: DisplayTierHooks) -> tuple:
    state = widget.runtime_state
    if slot < len(state._letterbox_params) and state._letterbox_params[slot]:
        return state._letterbox_params[slot]
    img = (
        state._stored_pil_images[slot] if slot < len(state._stored_pil_images) else None
    )
    w, h = canvas_dims(widget)
    if img and w > 0 and h > 0:
        iw, ih = get_image_dims(img)
        # W3a: fall back to box-clipped dims so the derived letterbox matches
        # the envelope geometry (None box -> unchanged).
        try:
            _raw = hooks.resolve_slot_boxes(widget)[slot] if slot in (0, 1) else None
            _b = hooks.sanitize_box(_raw, (iw, ih)) if _raw is not None else None
            if _b is not None:
                iw, ih = hooks.box_dims(_b, (iw, ih))
        except Exception:
            pass
        if iw > 0 and ih > 0:
            ratio = min(w / iw, h / ih)
            nw = max(1, int(iw * ratio))
            nh = max(1, int(ih * ratio))
        return (
            (w - nw) / (2.0 * w),
            (h - nh) / (2.0 * h),
            nw / float(w),
            nh / float(h),
        )
    return (0.0, 0.0, 1.0, 1.0)


def clear_diff_texture(widget, *, hooks: DisplayTierHooks):
    state = widget.runtime_state
    state._diff_source_pil_image = None
    state._diff_source_image_id = 0
    state._diff_source_ready = False
    cache = getattr(state, "_texture_upload_cache", None)
    if cache is not None:
        cache.pop(widget._diff_source_texture_id, None)


# ---------------------------------------------------------------------------
# Legacy-plan impl (from presentation/plan_applicator) -- single home for apply_plan_full/apply_plan_scene_only (R-d pin: tests/contracts/test_plan_application_boundary.py)
# ---------------------------------------------------------------------------


def plan_owns_padded_framebuffer(plan) -> bool:
    """True when ``canvas_w/h`` is the padded frame and clip/letterbox apply.

    Live main-window plans may carry ``overlay_clip_rect`` for uncrop without
    owning letterbox (images still fit by raw size). Export/video snapshot
    plans set ``geometry_letterbox`` for that ownership instead of baking
    pads into pixels (``image_is_padded_composite``).
    """
    return bool(
        getattr(plan, "image_is_padded_composite", False)
        or getattr(plan, "geometry_letterbox", False)
    )


def refresh_live_content_rect(canvas, state, plan) -> None:
    """``state._content_rect_px`` is otherwise only refreshed by
    ``letterbox_pil``/``update_letterbox_geometry`` on image upload, which
    leaves it stale after a resize until the next image upload. Re-derive it
    every call for the live, store-backed canvas.

    Live (default): fit the raw pair *envelope* — ``max(w1,w2) x max(h1,h2)``,
    the rect ``update_common_letterbox_geometry`` already fits both letterbox
    slots to. Export/video with ``geometry_letterbox`` / padded composite: fit
    the full ``canvas_w/h`` frame so ``overlay_clip_rect`` can recover the
    content box.

    Padded plans must refresh even when ``state._store`` is unset: video
    preview binds the store on ``canvas._store``, and ``set_pil_layers``
    clears ``state._store``. Skipping here leaves ``_content_rect_px`` as the
    raw-image letterbox; ``_compute_inner_content_rect`` then scales
    ``overlay_clip_rect`` against the wrong base, so the divider detaches
    from the images and stretches into the uncrop pad.
    """
    owns_padded = plan_owns_padded_framebuffer(plan)
    # Full logical canvas, not the render target: during tiled export the
    # widget is resized to one tile's footprint, so fitting against
    # ``canvas.width()`` letterboxed the canvas *into each tile* (content
    # rect at half scale, ``_content_sr`` = 0.5, shader letterbox
    # letterboxing the base image into the middle of every tile). See
    # ``canvas_dims``.
    widget_width, widget_height = canvas_dims(canvas)
    if widget_width <= 0 or widget_height <= 0:
        return
    if not owns_padded:
        store = getattr(state, "_store", None) or getattr(canvas, "_store", None)
        if store is None:
            return
        # Pair envelope, not slot 1 alone: the same rule
        # ``update_common_letterbox_geometry`` fits both letterbox slots with,
        # through the same helper. A slot-1-only fit is not pair-invariant —
        # after a swap inside the pair the installed tiers differ per side
        # (preview/preview while the order-sensitive unify cache misses, then
        # the unified pair), so the content rect followed slot 1's aspect and
        # every canvas-px → widget-px conversion (loupe radius/spacing/border,
        # divider/guide thickness) jumped with the swap and again when unify
        # landed (owner report 2026-10-27).
        #
        # ``plan.envelope_dims`` is that envelope in SOURCE space, carried by
        # the plan builder. Preferring it over the installed display tier is
        # what keeps the drawn rect (and therefore the loupe glass) identical
        # on a preview frame and on the unified frame; ``None`` keeps today's
        # display-dim fit bit-identically.
        _plan_envelope = normalize_envelope_dims(getattr(plan, "envelope_dims", None))
        if _plan_envelope is not None:
            sizes = [
                (_plan_envelope[0], _plan_envelope[1]),
                (_plan_envelope[2], _plan_envelope[3]),
            ]
        else:
            sizes = [
                get_image_dims(getattr(plan, "image1", None)),
                get_image_dims(getattr(plan, "image2", None)),
            ]
        _envelope_letterbox, content_rect = eager_envelope_rect(
            int(widget_width),
            int(widget_height),
            sizes,
        )
        if content_rect[2] <= 0 or content_rect[3] <= 0:
            return
        state._content_rect_px = content_rect
        rhi_render_debug(
            "content_rect widget=%dx%d envelope -> %r",
            widget_width,
            widget_height,
            content_rect,
        )
        return

    fit_width, fit_height = plan.canvas_w, plan.canvas_h
    if fit_width <= 0 or fit_height <= 0:
        return
    geometry = resolve_canvas_content_geometry(
        widget_width=widget_width,
        widget_height=widget_height,
        image_width=fit_width,
        image_height=fit_height,
        virtual_layout=None,
    )
    if geometry.outer_rect_px is not None:
        state._content_rect_px = geometry.outer_rect_px
    rhi_render_debug(
        "content_rect widget=%dx%d fit=%dx%d owns_padded=%s -> outer=%r",
        widget_width,
        widget_height,
        fit_width,
        fit_height,
        owns_padded,
        geometry.outer_rect_px,
    )


def compute_content_scale(canvas, plan: CanvasRenderPlan) -> float:
    """
    Return the letterbox scale factor sr = min(dw/canvas_w, dh/canvas_h).

    This is the single coefficient that converts canvas-px -> widget-px for
    isotropic attributes (border_width, divider_thickness, safe_gap).
    Returns 1.0 when geometry is not yet available (safe for export paths).
    """
    content_rect = canvas.runtime_state._content_rect_px
    if not content_rect or plan.canvas_w <= 0 or plan.canvas_h <= 0:
        return 1.0
    _, _, dw, dh = content_rect
    return min(dw / plan.canvas_w, dh / plan.canvas_h)


def compute_inner_content_rect(state, plan):
    """
    Compute the inner image rect in widget-px and the corresponding split position.

    The base image pair is never padding-aware (see
    ``refresh_live_content_rect``/``update_letterbox_geometry``): whenever
    the plan does not own a padded framebuffer, ``inner == outer ==
    _content_rect_px`` — live ``overlay_clip_rect`` must not shrink letterbox.

    When ``image_is_padded_composite`` or ``geometry_letterbox`` is set, honor
    ``overlay_clip_rect`` to narrow back down to the real image region.
    """
    content_rect = state._content_rect_px
    if not content_rect:
        return None, None

    if plan.canvas_w <= 0 or plan.canvas_h <= 0 or not plan_owns_padded_framebuffer(plan):
        ox, oy, dw, dh = content_rect
        return (
            (int(round(ox)), int(round(oy)), max(1, int(round(dw))), max(1, int(round(dh)))),
            None,
        )

    ox, oy, dw, dh = content_rect
    sx = dw / plan.canvas_w
    sy = dh / plan.canvas_h

    clip_rect = getattr(plan.render_scene, "overlay_clip_rect", None)
    if clip_rect is None:
        inner = (
            int(round(ox)),
            int(round(oy)),
            max(1, int(round(dw))),
            max(1, int(round(dh))),
        )
        return inner, None

    clip_x, clip_y, clip_w, clip_h = clip_rect
    inner = (
        int(round(ox + clip_x * sx)),
        int(round(oy + clip_y * sy)),
        max(1, int(round(clip_w * sx))),
        max(1, int(round(clip_h * sy))),
    )

    raw_split = float(getattr(plan.render_scene, "split_position_visual", 0.5))
    inner_split = max(0.0, min(1.0, raw_split))
    return inner, inner_split


def apply_plan_letterbox_from_clip(canvas, plan) -> None:
    """Place unpadded export/video images via shader letterbox.

    ``overlay_clip_rect`` is in canvas-px. The preview/export widget may be a
    different size than ``canvas_w/h``, so first fit the padded canvas frame
    into the canvas, then nest the clip inside that frame. Applying clip/canvas
    fractions directly as widget UV stretches the image to the pane (wrong for
    video preview).

    Tiled export: the render target is one tile of the full canvas, so the fit
    runs against the *canvas* (``canvas_dims``) while the emitted UVs stay
    render-target-normalized against the real widget size — the tile origin
    (``_export_canvas_viewport[2:4]``) is subtracted from the frame origin
    first. Fitting against the tile instead letterboxed the whole canvas into
    every tile (base image squeezed into the middle of the tile on a padded
    export, owner report 2026-10-28).

    Pad fill is painted by the base shader inside the canvas frame only
    (``_canvas_frame_letterbox`` + ``_letterbox_fill_rgba``); widget chrome
    outside the frame stays the theme clear color.
    """
    state = canvas.runtime_state
    state._canvas_frame_letterbox = None
    state._letterbox_fill_rgba = None

    if not bool(getattr(plan, "geometry_letterbox", False)):
        return
    if bool(getattr(plan, "image_is_padded_composite", False)):
        # Baked canvas-sized images already match the framebuffer 1:1.
        return
    clip = getattr(getattr(plan, "render_scene", None), "overlay_clip_rect", None)
    cw = int(getattr(plan, "canvas_w", 0) or 0)
    ch = int(getattr(plan, "canvas_h", 0) or 0)
    if clip is None or cw <= 0 or ch <= 0:
        return

    widget_w = int(canvas.width()) if callable(getattr(canvas, "width", None)) else 0
    widget_h = int(canvas.height()) if callable(getattr(canvas, "height", None)) else 0
    if widget_w <= 0 or widget_h <= 0:
        return
    canvas_width, canvas_height = canvas_dims(canvas)

    frame = resolve_canvas_content_geometry(
        widget_width=canvas_width,
        widget_height=canvas_height,
        image_width=cw,
        image_height=ch,
        virtual_layout=None,
    )
    inner = frame.inner_rect_px
    if inner is None:
        return
    ox, oy, fw, fh = inner
    if fw <= 0 or fh <= 0:
        return

    viewport = getattr(state, "_export_canvas_viewport", None)
    if viewport is not None:
        # Render-target origin in canvas px (tile top-left); 0 outside export.
        offset_x = float(viewport[2])
        offset_y = float(viewport[3])
    else:
        offset_x = 0.0
        offset_y = 0.0
    ox -= offset_x
    oy -= offset_y

    cx, cy, cww, chh = clip
    sx = float(fw) / float(cw)
    sy = float(fh) / float(ch)
    params = (
        (float(ox) + float(cx) * sx) / float(widget_w),
        (float(oy) + float(cy) * sy) / float(widget_h),
        (float(cww) * sx) / float(widget_w),
        (float(chh) * sy) / float(widget_h),
    )
    while len(state._letterbox_params) < 2:
        state._letterbox_params.append((0.0, 0.0, 1.0, 1.0))
    state._letterbox_params[0] = params
    state._letterbox_params[1] = params
    state._canvas_frame_letterbox = (
        float(ox) / float(widget_w),
        float(oy) / float(widget_h),
        float(fw) / float(widget_w),
        float(fh) / float(widget_h),
    )
    fill = getattr(plan, "fill_rgba", None)
    if fill is not None and len(fill) >= 4:
        state._letterbox_fill_rgba = (
            float(fill[0]),
            float(fill[1]),
            float(fill[2]),
            float(fill[3]),
        )


def apply_plan_runtime_overlays(canvas, plan: CanvasRenderPlan, *, hooks: LegacyPlanHooks) -> None:
    """
    Apply image-compare feature-owned plan/runtime overlays after letterbox geometry is known.
    """
    state = canvas.runtime_state

    # Widget resize (video preview splitter, window drag) runs
    # ``update_common_letterbox_geometry`` first, which fits the *raw* image.
    # Uncrop plans must re-nest that letterbox inside the padded canvas frame
    # before content/inner rects and DividerPass read them — otherwise images
    # and the spit line diverge after every preview pane resize.
    if plan_owns_padded_framebuffer(plan):
        apply_plan_letterbox_from_clip(canvas, plan)

    refresh_live_content_rect(canvas, state, plan)

    inner_rect, inner_split = compute_inner_content_rect(state, plan)
    state._inner_content_rect_px = inner_rect
    state._inner_split_position = inner_split
    state._content_sr = compute_content_scale(canvas, plan)

    state._clip_overlays_to_content_rect = bool(
        getattr(canvas, "_clip_overlays_to_content_rect", False)
    )

    hooks.apply_feature_plan_runtime_overlays(canvas, plan)


def _rect_xywh_matches(rect, cx: int, cy: int, cw: int, ch: int) -> bool:
    """Attribute-wise ``Rect(x, y, w, h)`` equality without importing it.

    The tab's geometry state carries ``Rect`` (a frozen dataclass); comparing
    component-wise keeps this module free of domain imports while matching
    dataclass equality exactly (``x``/``y``/``w``/``h`` are its only fields).
    """
    try:
        return (
            getattr(rect, "x", None) == cx
            and getattr(rect, "y", None) == cy
            and getattr(rect, "w", None) == cw
            and getattr(rect, "h", None) == ch
        )
    except Exception:
        return False


def sync_geometry_state(canvas, store, *, hooks: LegacyPlanHooks) -> None:
    """Update store.viewport.geometry_state from the canvas content rect."""
    state = canvas.runtime_state
    rect = getattr(state, "_inner_content_rect_px", None) or state._content_rect_px
    if not rect:
        return
    cx, cy, cw, ch = rect
    vp = getattr(store, "viewport", None)
    if vp is not None and cw > 0 and ch > 0:
        # Guard: only dispatch when geometry actually changed — otherwise
        # every update_comparison_if_needed() would emit a viewport change,
        # re-arm the fps timer and spam [ic-preview] document state logs.
        try:
            gs = getattr(vp, "geometry_state", None)
            if gs is not None:
                cur_w = getattr(gs, "pixmap_width", None)
                cur_h = getattr(gs, "pixmap_height", None)
                cur_rect = getattr(gs, "image_display_rect_on_label", None)
                if cur_w == cw and cur_h == ch and _rect_xywh_matches(cur_rect, cx, cy, cw, ch):
                    return
        except Exception:
            pass
        dispatcher = getattr(store, "get_dispatcher", lambda: None)()
        if dispatcher is not None:
            try:
                actions = hooks.make_geometry_actions(cw, ch, cx, cy)
                batch = getattr(store, "batch_changes", None)
                if callable(batch):
                    with store.batch_changes():
                        for action in actions:
                            dispatcher.dispatch(action, scope="viewport")
                else:
                    for action in actions:
                        dispatcher.dispatch(action, scope="viewport")
                return
            except Exception:
                pass
        try:
            geometry_state = getattr(vp, "geometry_state", None)
            if geometry_state is None:
                geometry_state = getattr(getattr(store, "viewport", None), "geometry_state", None)
            if geometry_state is not None:
                hooks.commit_geometry_attrs(geometry_state, cw, ch, cx, cy)
        except Exception:
            pass


def sync_split_position(store, canvas, split_position: float, *, hooks: LegacyPlanHooks) -> None:
    command = hooks.get_feature_command("splitter.sync_split_position")
    if command is not None:
        command(type("CanvasActions", (), {"store": store})(), split_position)
    canvas.set_render_scene(
        hooks.build_render_scene(
            store,
            apply_channel_mode_in_shader=getattr(canvas, "_apply_channel_mode_in_shader", True),
            clip_overlays_to_image_bounds=getattr(canvas, "_clip_overlays_to_content_rect", False),
        )
    )


def resolve_clip_flag(store, clip_overlays_to_image_bounds: bool, plan) -> bool:
    if store is not None:
        return clip_overlays_to_image_bounds
    return False


def setup_store_bindings(canvas, plan, *, store, clip_flag: bool, hooks: LegacyPlanHooks) -> None:
    state = canvas.runtime_state

    if store is not None:
        canvas._store = store
        state._store = store
    else:
        canvas._store = None
        state._store = None

    canvas._active_render_plan = plan
    canvas._clip_overlays_to_content_rect = clip_flag
    canvas.set_render_scene(plan.render_scene)
    try:
        hooks.debug(
            canvas,
            "active_render_plan SET scene=%s store=%s",
            type(plan.render_scene).__name__ if plan.render_scene is not None else None,
            store is not None,
        )
    except Exception:
        pass

    if store is not None:
        canvas.set_split_position_sync(
            lambda split: sync_split_position(store, canvas, split, hooks=hooks)
        )
        canvas.set_apply_channel_mode_in_shader(True)
    else:
        state._apply_channel_mode_in_shader = True


def rebind_plan_store(canvas, store) -> None:
    """``set_pil_layers`` clears ``state._store``; restore after texture upload."""
    if store is None:
        return
    canvas._store = store
    canvas.runtime_state._store = store


def apply_overlays(canvas, plan, *, store, hooks: LegacyPlanHooks) -> None:
    # ``plan.overlay_layout is not None`` ⇒ the plan owns the overlay state
    # (a recorded/snapshot plan — export, video preview — carried through
    # as-is). Apply its own guide/capture params and skip the live recompute:
    # ``apply_feature_live_runtime_overlays`` re-derives the overlay from the
    # LIVE store and replaces it — or, when it cannot build a layout, nulls
    # it. The store still gets bound (``sync_geometry_state``) because
    # interaction needs it. A plan WITHOUT an overlay layout keeps today's
    # live path exactly (see ``plan_owns_overlay_state``).
    plan_owns_overlay = plan_owns_overlay_state(plan)
    if store is None or plan_owns_overlay:
        canvas.set_guides_params(
            plan.guides_enabled,
            plan.guides_color,
            plan.guides_thickness,
        )
        canvas.set_capture_color(plan.capture_color)

    apply_plan_runtime_overlays(canvas, plan, hooks=hooks)

    if store is not None:
        sync_geometry_state(canvas, store, hooks=hooks)
        if not plan_owns_overlay:
            hooks.apply_feature_live_runtime_overlays(store, canvas)


def textures_are_current(canvas, plan: CanvasRenderPlan) -> bool:
    if plan.display_cache_key is None:
        return False
    state = canvas.runtime_state
    if plan.display_cache_key != state._stored_image_ids:
        return False
    stored = state._stored_pil_images
    return bool(stored and stored[0] is not None)


def _canonical_steps(hooks: LegacyPlanHooks) -> LegacyPlanSteps:
    if hooks.steps is not None:
        return hooks.steps
    return LegacyPlanSteps(
        resolve_clip_flag=lambda *a: resolve_clip_flag(*a),
        setup_store_bindings=lambda *a, **k: setup_store_bindings(*a, hooks=hooks, **k),
        apply_overlays=lambda *a, **k: apply_overlays(*a, hooks=hooks, **k),
    )


def apply_canvas_render_plan(
    canvas,
    plan: CanvasRenderPlan,
    *,
    store=None,
    clip_overlays_to_image_bounds: bool = False,
    hooks: LegacyPlanHooks,
    steps: LegacyPlanSteps | None = None,
) -> None:
    canvas._active_composition = None
    steps = steps or _canonical_steps(hooks)
    if textures_are_current(canvas, plan):
        apply_plan_scene_only(
            canvas,
            plan,
            store=store,
            clip_overlays_to_image_bounds=clip_overlays_to_image_bounds,
            hooks=hooks,
            steps=steps,
        )
    else:
        apply_plan_full(
            canvas,
            plan,
            store=store,
            clip_overlays_to_image_bounds=clip_overlays_to_image_bounds,
            hooks=hooks,
            steps=steps,
        )


def apply_plan_full(
    canvas,
    plan: CanvasRenderPlan,
    *,
    store,
    clip_overlays_to_image_bounds: bool,
    hooks: LegacyPlanHooks,
    steps: LegacyPlanSteps | None = None,
) -> None:
    """Full path: uploads textures, resets view, configures everything."""
    from ui.canvas_infra.viewport.state import (
        set_pan_offsets,
        set_zoom_level,
    )

    steps = steps or _canonical_steps(hooks)
    if hasattr(canvas, "begin_update_batch"):
        canvas.begin_update_batch()
    try:
        clip_flag = steps.resolve_clip_flag(store, clip_overlays_to_image_bounds, plan)
        steps.setup_store_bindings(canvas, plan, store=store, clip_flag=clip_flag)

        # preserve_zoom: leave the viewport completely untouched. The old
        # reset_view() + restore + zoomChanged re-emit dance produced
        # transient zoom=1/pan=0 states that listeners observed mid-apply,
        # visibly throwing the camera on texture swaps (preview→store flip,
        # docs/dev/rendering/display-image-pipeline.md).
        if plan.preserve_zoom:
            letterbox_focus = capture_letterbox_focus(canvas)
        else:
            canvas.reset_view()
            set_zoom_level(canvas, 1.0)
            set_pan_offsets(canvas, 0.0, 0.0)

        canvas.set_pil_layers(
            plan.image1,
            plan.image2,
            source_image1=plan.source_image1,
            source_image2=plan.source_image2,
            source_key=plan.source_key,
            display_cache_key=plan.display_cache_key,
            shader_letterbox=True,
        )
        rebind_plan_store(canvas, store)
        apply_plan_letterbox_from_clip(canvas, plan)
        if plan.preserve_zoom:
            restore_letterbox_focus(canvas, letterbox_focus)

        steps.apply_overlays(canvas, plan, store=store)

    finally:
        if hasattr(canvas, "end_update_batch"):
            canvas.end_update_batch()


def geometry_needs_bootstrap(state) -> bool:
    """True until some owner has written this frame's content rect.

    The per-frame geometry pass is the single writer of the letterbox. This
    scene-only path used to re-derive it from ``_stored_pil_images`` (the
    *preview* images), which disagrees with the drawn tier whenever a crop box
    is active: the owner writes the cropped aspect (e.g. 1440x1056), the
    scene-only pass then overwrites it with the preview's (1024x768), and the
    image visibly changes aspect. Measured on the D3D11 host 2026-10-24
    (plan-comparison-letterbox): letterbox alternated 0.837 <-> 0.819 in one
    canvas with identical label size and DPR 1.0. Keep the bootstrap (flows
    where this pass is the only writer) and stop the overwrite.
    """
    rect = getattr(state, "_content_rect_px", None)
    if not rect:
        return True
    try:
        return tuple(int(value) for value in rect) == (0, 0, 0, 0)
    except (TypeError, ValueError):
        return True


def apply_plan_scene_only(
    canvas,
    plan: CanvasRenderPlan,
    *,
    store,
    clip_overlays_to_image_bounds: bool,
    hooks: LegacyPlanHooks,
    steps: LegacyPlanSteps | None = None,
) -> None:
    """
    Lightweight scene-only update: textures are already current.
    """
    from ui.canvas_infra.viewport.state import (
        get_zoom_level,
        set_pan_offsets,
        set_zoom_level,
    )

    steps = steps or _canonical_steps(hooks)
    if hasattr(canvas, "begin_update_batch"):
        canvas.begin_update_batch()
    try:
        clip_flag = steps.resolve_clip_flag(store, clip_overlays_to_image_bounds, plan)
        steps.setup_store_bindings(canvas, plan, store=store, clip_flag=clip_flag)

        if not plan.preserve_zoom:
            if abs(get_zoom_level(canvas) - 1.0) > 1e-6:
                set_zoom_level(canvas, 1.0)
            set_pan_offsets(canvas, 0.0, 0.0)

        state = canvas.runtime_state
        img0 = state._stored_pil_images[0] if state._stored_pil_images else None
        if (
            img0 is not None
            and state._shader_letterbox_mode
            and geometry_needs_bootstrap(state)
        ):
            letterbox_focus = capture_letterbox_focus(canvas) if plan.preserve_zoom else None
            img1 = (
                state._stored_pil_images[1]
                if len(state._stored_pil_images) > 1
                else None
            )
            update_common_letterbox_geometry(
                canvas,
                img0,
                img1,
                envelope_dims=getattr(plan, "envelope_dims", None),
                hooks=hooks.tier,
            )
            restore_letterbox_focus(canvas, letterbox_focus)
        rebind_plan_store(canvas, store)
        apply_plan_letterbox_from_clip(canvas, plan)

        steps.apply_overlays(canvas, plan, store=store)

    finally:
        if hasattr(canvas, "end_update_batch"):
            canvas.end_update_batch()
