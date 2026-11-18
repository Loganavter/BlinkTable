"""Crop-box remap for the magnifier (non-destructive crop, W3b).

Pixel stores are FULL-FRAME (see ``tabs.image_compare.pipeline.crop_box``):
crop is never baked at decode. The magnifier therefore maps its capture over
the detected CROP BOX, not the store:

- pixel-box path (``geometry.drawing_coords``): the capture center offsets by
  ``box_origin`` and sizes scale with the box (``eff_rel`` maps over box size);
- live UV path (``geometry.layout_plan``): the capture ``uv_rect`` remaps
  box→full-image UV (``box_origin/full + rel*box/full``).

Box resolution goes ONLY through ``effective_crop_box_for_path`` (single
owner, ``tabs.image_compare.pipeline.crop_box``) — never ``CropService.get``
directly, and ``override`` stays untouched (reserved for a later wave). On
top of that, render-side lookups only consult services whose box for *path*
is already warmed: the load path warms detection off the GUI thread
(``schedule_crop_warmup`` / worker-side ``svc.get``), so a lookup here is a
dict hit and can never block the GUI on IO. No warmed service (crop OFF,
cold cache, missing paths) → ``None`` → callers fall back to today's
full-store math, bit-identical.
"""

from __future__ import annotations


def _as_box_tuple(box) -> tuple[int, int, int, int] | None:
    """Normalize a CropBox / plain tuple to ``(left, top, right, bottom)``."""
    if box is None:
        return None
    try:
        left, top, right, bottom = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def valid_full_size(full_size) -> tuple[int, int] | None:
    """Normalize a ``(width, height)`` pair; ``None`` when unusable."""
    if full_size is None:
        return None
    try:
        width, height = int(full_size[0]), int(full_size[1])
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return (width, height)


def valid_box_for_full(
    box, full_w: int, full_h: int
) -> tuple[int, int, int, int] | None:
    """Box tuple clipped to the usable domain, or ``None`` (legacy math).

    A box outside the full-store bounds (or degenerate) is treated as absent
    so callers fall back to today's full-store behavior instead of sampling
    garbage.
    """
    box_t = _as_box_tuple(box)
    if box_t is None:
        return None
    try:
        full_w, full_h = int(full_w), int(full_h)
    except Exception:
        return None
    if full_w <= 0 or full_h <= 0:
        return None
    left, top, right, bottom = box_t
    if left < 0 or top < 0 or right > full_w or bottom > full_h:
        return None
    return box_t


def _candidate_crop_services() -> list:
    """Live CropService instances (no IO, no allocation)."""
    try:
        from shared.image_processing.autocrop.service import _live_services

        return list(_live_services)
    except Exception:
        return []


def resolve_box_for_path(path: str | None) -> tuple[int, int, int, int] | None:
    """Warmed effective box for *path* in full-source coords, or ``None``.

    Only consults services that already have *path* cached (warmed off the
    GUI thread at load); a cold service is skipped, never computed here.
    """
    if not path:
        return None
    try:
        from tabs.image_compare.pipeline.crop_box import effective_crop_box_for_path
    except Exception:
        return None
    path_str = str(path)
    for svc in _candidate_crop_services():
        try:
            has_cached = svc._has_cached(path_str)
        except Exception:
            continue
        if not has_cached:
            continue
        try:
            box = effective_crop_box_for_path(path_str, crop_service=svc)
        except Exception:
            continue
        box_t = _as_box_tuple(box)
        if box_t is not None:
            return box_t
    return None


def resolve_crop_boxes_for_store(
    store,
) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]:
    """Effective boxes for the store's ``(image1, image2)`` slots.

    Crop OFF (``auto_crop_black_borders``) → ``(None, None)``. Any failure →
    ``None`` per side (callers keep today's math).
    """
    try:
        doc = store.get_session_state_slot("document")
    except Exception:
        return (None, None)
    if doc is None:
        return (None, None)
    try:
        global_off = not bool(
            getattr(
                getattr(store, "settings", None), "auto_crop_black_borders", False
            )
        )
    except Exception:
        global_off = False
    try:
        path1 = getattr(doc, "image1_path", None)
    except Exception:
        path1 = None
    try:
        path2 = getattr(doc, "image2_path", None)
    except Exception:
        path2 = None
    try:
        from tabs.image_compare.state.document import crop_override_for_path

        ov1 = crop_override_for_path(doc, path1)
        ov2 = crop_override_for_path(doc, path2)
    except Exception:
        ov1, ov2 = None, None
    # Explicit Off never crops; Auto with the global flag OFF keeps today's
    # (None, None) — only an explicit On bypasses the global gate (its box
    # comes from the forced spare warmed by the display path, which
    # _candidate_crop_services picks up via the CropService registry).
    try:
        box1 = None if ov1 is False or (ov1 is None and global_off) else resolve_box_for_path(path1)
    except Exception:
        box1 = None
    try:
        box2 = None if ov2 is False or (ov2 is None and global_off) else resolve_box_for_path(path2)
    except Exception:
        box2 = None
    return (box1, box2)


def capture_window_px(
    *,
    eff_rel_x: float,
    eff_rel_y: float,
    frac_w: float,
    frac_h: float,
    full_w: int,
    full_h: int,
    box: tuple[int, int, int, int] | None = None,
) -> tuple[float, float, int, int, int, int, int, int]:
    """Capture window in full-store px: ``(cx, cy, w, h, left, top, r, b)``.

    ``box=None`` reproduces today's full-store math exactly (center =
    ``eff_rel * full``, size = ``frac * full``, even-adjusted). With a box,
    the center offsets by ``box_origin`` and sizes scale with the box
    (``eff_rel`` maps over box size).
    """
    if box is not None:
        box_w = box[2] - box[0]
        box_h = box[3] - box[1]
        cx = box[0] + float(eff_rel_x) * box_w
        cy = box[1] + float(eff_rel_y) * box_h
        w = int(round(float(frac_w) * box_w))
        h = int(round(float(frac_h) * box_h))
    else:
        cx = float(eff_rel_x) * full_w
        cy = float(eff_rel_y) * full_h
        w = int(round(float(frac_w) * full_w))
        h = int(round(float(frac_h) * full_h))
    if w % 2 != 0:
        w += 1
    if h % 2 != 0:
        h += 1
    left = int(round(cx - w / 2.0))
    top = int(round(cy - h / 2.0))
    return (cx, cy, w, h, left, top, left + w, top + h)


def box_clamp_domain(
    box: tuple[int, int, int, int] | None, full_w: int, full_h: int
) -> tuple[int, int, int, int]:
    """``(lo_x, lo_y, hi_x, hi_y)`` a capture window must stay inside."""
    if box is not None:
        return (box[0], box[1], box[2], box[3])
    return (0, 0, int(full_w), int(full_h))


def remap_uv_rect(
    cap_x: float,
    cap_y: float,
    uv_half_w: float,
    uv_half_h: float,
    box: tuple[int, int, int, int] | None,
    full_w: int,
    full_h: int,
) -> tuple[float, float, float, float] | None:
    """Capture ``uv_rect`` remapped box→full-image UV, or ``None``.

    ``None`` (no/invalid box) means the caller keeps today's formula
    (``cap ± uv_half``), which is exactly what this returns when the box is
    the full frame.
    """
    if box is None:
        return None
    try:
        full_w, full_h = int(full_w), int(full_h)
    except Exception:
        return None
    if full_w <= 0 or full_h <= 0:
        return None
    box_w = box[2] - box[0]
    box_h = box[3] - box[1]
    if box_w <= 0 or box_h <= 0:
        return None
    return (
        (box[0] + (float(cap_x) - float(uv_half_w)) * box_w) / full_w,
        (box[1] + (float(cap_y) - float(uv_half_h)) * box_h) / full_h,
        (box[0] + (float(cap_x) + float(uv_half_w)) * box_w) / full_w,
        (box[1] + (float(cap_y) + float(uv_half_h)) * box_h) / full_h,
    )


def image_full_size(img) -> tuple[int, int] | None:
    """Full-frame ``(width, height)`` of a pixel/preview image, or ``None``.

    Accepts ``TiledPixelStore``/PIL (``.size``) and ``QImage``
    (``width()``/``height()``); anything else → ``None`` (legacy fallback).
    """
    if img is None:
        return None
    try:
        size = getattr(img, "size", None)
        if size is not None and not callable(size):
            return valid_full_size((int(size[0]), int(size[1])))
    except Exception:
        pass
    try:
        width = img.width()
        height = img.height()
        return valid_full_size((int(width), int(height)))
    except Exception:
        return None
