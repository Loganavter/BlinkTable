# Audit-Meta: pattern=canvas-presentation size=exempt reason="texture_parts/crop_clip single box seam (W3a/W3f) — box-clipped texture/geometry helpers share one resolution path"
"""Box-clipped texture/geometry helpers for image_compare (W3a).

Pixel stores decode FULL-FRAME (W1+W2); the detected box is side metadata
owned by ``pipeline.crop_box.effective_crop_box_for_path`` — this module is
the canvas layer's single consumer of that interface (one call per slot, no
new box-resolution logic here).

GUI-thread rule: ``CropService.get`` does IO on first touch, but the load
path warms the service before decode finishes. An unwarmed path resolves to
``None`` for this frame (plus a background warmup kick) instead of blocking;
test fakes implementing only ``get`` are always queried. ``None`` box means
"identical to today": every helper below passes ``None`` through.
"""

from __future__ import annotations

from typing import Any


def sanitize_box(box: Any, size: tuple[int, int] | None) -> tuple[int, int, int, int] | None:
    """Clamp *box* to *size*, or ``None`` when there is nothing to clip.

    Returns ``None`` for a ``None`` box, a degenerate/empty intersection, or
    a box that covers the whole frame (full-frame box == no crop, keeps
    today's behavior bit-identical). Otherwise returns an
    ``(left, top, right, bottom)`` int tuple strictly inside the frame.
    """
    if box is None or size is None:
        return None
    try:
        w, h = int(size[0]), int(size[1])
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    try:
        left, top, right, bottom = (
            int(box[0]),
            int(box[1]),
            int(box[2]),
            int(box[3]),
        )
    except Exception:
        try:
            left, top, right, bottom = (
                int(box.left),
                int(box.top),
                int(box.right),
                int(box.bottom),
            )
        except Exception:
            return None
    left = max(0, min(left, w))
    top = max(0, min(top, h))
    right = max(0, min(right, w))
    bottom = max(0, min(bottom, h))
    if right <= left or bottom <= top:
        return None
    if left == 0 and top == 0 and right == w and bottom == h:
        return None
    return (left, top, right, bottom)


def box_dims(box: tuple[int, int, int, int] | None, full_size: tuple[int, int]) -> tuple[int, int]:
    """``(width, height)`` of *box*, or *full_size* when *box* is ``None``."""
    if box is None:
        try:
            return (int(full_size[0]), int(full_size[1]))
        except Exception:
            return (0, 0)
    return (int(box[2] - box[0]), int(box[3] - box[1]))


def clip_box_for_image(
    box_full: Any | None,
    *,
    full_size: tuple[int, int] | None,
    img_size: tuple[int, int] | None,
) -> tuple[int, int, int, int] | None:
    """*box_full* (full-source coords) translated into one image's coords.

    Identity when the image is full-size; uniformly scaled (rounded,
    clamped) for downscaled tiers such as previews, so a box-clipped
    preview keeps the box aspect and the envelope rect stays stable across
    the preview→store flip. ``None`` when there is nothing to clip
    (no box, unknown sizes, degenerate result) — callers then keep today's
    behavior.
    """
    if box_full is None or full_size is None or img_size is None:
        return None
    try:
        fw, fh = int(full_size[0]), int(full_size[1])
        iw, ih = int(img_size[0]), int(img_size[1])
    except Exception:
        return None
    if fw <= 0 or fh <= 0 or iw <= 0 or ih <= 0:
        return None
    clean = sanitize_box(box_full, (fw, fh))
    if clean is None:
        return None
    if iw == fw and ih == fh:
        return clean
    sx = iw / float(fw)
    sy = ih / float(fh)
    left = int(round(clean[0] * sx))
    top = int(round(clean[1] * sy))
    right = int(round(clean[2] * sx))
    bottom = int(round(clean[3] * sy))
    return sanitize_box((left, top, right, bottom), (iw, ih))


def _is_warmed(service: Any, path: str) -> bool:
    """Best-effort warmed check; ``True`` when unknown (test fakes)."""
    check = getattr(service, "_has_cached", None)
    if callable(check):
        try:
            return bool(check(path))
        except Exception:
            return True
    return True


def _kick_warmup(service: Any, path: str) -> None:
    try:
        warm = getattr(service, "warm_cache_async", None)
        if callable(warm):
            warm([path])
            return
        from shared.image_processing.autocrop.service import schedule_crop_warmup

        schedule_crop_warmup(service, [path])
    except Exception:
        pass


def resolve_box_for_path(
    path: Any,
    crop_service: Any | None,
    override: bool | None = None,
    *,
    forced_service: Any | None = None,
) -> Any | None:
    """One slot's effective box via the single-owner interface.

    ``override`` is the per-image tristate (``None`` == Auto, ``True`` ==
    On, ``False`` == Off → ``None`` without detection); forwarded to
    :func:`effective_crop_box_for_path`. ``forced_service`` is a detection-only
    spare consulted solely when ``override`` is True and ``crop_service`` is
    None (global OFF nulled the session service): Auto/Off behavior is
    untouched either way. Never raises; any failure (including an unwarmed
    service on the GUI thread, which instead gets a background warmup kick)
    yields ``None``.
    """
    if override is False:
        return None
    svc = crop_service
    if override is True and (svc is None or isinstance(svc, bool)):
        svc = forced_service
    if not path or svc is None or isinstance(svc, bool):
        return None
    try:
        path_str = str(path)
    except Exception:
        return None
    if not path_str:
        return None
    try:
        if not _is_warmed(svc, path_str):
            _kick_warmup(svc, path_str)
            return None
    except Exception:
        pass
    try:
        from tabs.image_compare.pipeline.crop_box import effective_crop_box_for_path

        return effective_crop_box_for_path(
            path_str, crop_service=svc, override=override
        )
    except Exception:
        return None


def crop_service_for_widget(widget: Any, explicit: Any | None = None) -> Any | None:
    """Best-effort session crop service for a canvas widget.

    Explicit arg wins, then widget/runtime_state stashes (tests), then the
    live session service via the session-controller backlink
    (``_pending_session_controller`` / ``_context_menu_provider._session_ctrl``
    → ``_get_crop_service()`` — the same object that getter returns while
    ``auto_crop_black_borders`` is ON, ``None`` when OFF), then the Store's
    ``pipeline`` session slot (``ImageSession.crop_service`` in tests).
    """
    if explicit is not None:
        return None if isinstance(explicit, bool) else explicit
    for holder in (widget, getattr(widget, "runtime_state", None)):
        try:
            svc = getattr(holder, "_crop_service", None)
        except Exception:
            svc = None
        if svc is not None and not isinstance(svc, bool):
            return svc
    # W3f: live session service via the session-controller backlink.
    # Production canvas never carries the service on the Store pipeline
    # slot (that slot holds the reducer-owned PipelineCacheState mirror —
    # pixel/preview/unify dicts only), so the slot lookup below stays None
    # while bordered images decode full-frame (W1): with autocrop ON the
    # canvas showed full frames WITH black borders. The canvas widget does
    # retain the sessions object: set_session_controller() stashes it as
    # _pending_session_controller until the context-menu provider is
    # installed, then set_context_menu_provider() forwards it to
    # provider._session_ctrl (transient_flyouts/coordinators wire the same
    # main_controller.sessions object). _get_crop_service() returns the
    # session's live service while auto_crop_black_borders is ON, None
    # when OFF — the OFF gate is respected exactly, no new logic here.
    # (A _live_services warmed scan was considered instead, but it is
    # session-blind and gate-blind: without path/session context it can
    # neither pick the active session's service nor honor OFF. The
    # backlink can, so no scan.)
    try:
        provider = getattr(widget, "_context_menu_provider", None)
        for ctrl in (
            getattr(widget, "_pending_session_controller", None),
            getattr(provider, "_session_ctrl", None),
        ):
            getter = getattr(ctrl, "_get_crop_service", None)
            if not callable(getter):
                continue
            try:
                svc = getter()
            except Exception:
                svc = None
            if svc is not None and not isinstance(svc, bool):
                return svc
    except Exception:
        pass
    try:
        state = getattr(widget, "runtime_state", None)
        store = getattr(state, "_store", None) or getattr(widget, "_store", None)
        if store is not None:
            getter = getattr(store, "get_session_state_slot", None)
            if callable(getter):
                try:
                    session = getter("pipeline")
                except Exception:
                    session = None
                svc = getattr(session, "crop_service", None)
                if svc is not None and not isinstance(svc, bool):
                    return svc
    except Exception:
        pass
    return None


def slot_paths_for_widget(
    widget: Any,
    source_key: Any | None = None,
) -> tuple[Any | None, Any | None]:
    """``(path1, path2)`` for box resolution, best-effort.

    Prefers the upload's own ``source_key`` (``(path1, path2, ...)``), then
    the retained ``state._source_image_ids`` (which embeds that same key),
    then the Store ``document`` slot paths.
    """
    key = source_key
    if key is None:
        try:
            state = getattr(widget, "runtime_state", None)
            ids = getattr(state, "_source_image_ids", None)
            if isinstance(ids, tuple) and ids and isinstance(ids[0], tuple):
                key = ids[0]
            else:
                key = ids
        except Exception:
            key = None
    if isinstance(key, (tuple, list)) and len(key) >= 2:
        try:
            p1, p2 = key[0], key[1]
            if isinstance(p1, str) and isinstance(p2, str):
                return (p1 or None, p2 or None)
        except Exception:
            pass
    try:
        state = getattr(widget, "runtime_state", None)
        store = getattr(state, "_store", None) or getattr(widget, "_store", None)
        if store is not None:
            getter = getattr(store, "get_session_state_slot", None)
            if callable(getter):
                doc = getter("document")
                return (
                    getattr(doc, "image1_path", None) or None,
                    getattr(doc, "image2_path", None) or None,
                )
    except Exception:
        pass
    return (None, None)


def _document_for_widget(widget: Any) -> Any | None:
    """Best-effort ``document`` session slot behind a canvas widget.

    Same store reach as :func:`slot_paths_for_widget` (``runtime_state._store``
    / ``widget._store`` → ``get_session_state_slot("document")``); ``None``
    when unreachable. Pure read — the per-image ``crop_override`` lookup
    source for :func:`resolve_slot_boxes`.
    """
    try:
        state = getattr(widget, "runtime_state", None)
        store = getattr(state, "_store", None) or getattr(widget, "_store", None)
        if store is None:
            return None
        getter = getattr(store, "get_session_state_slot", None)
        if not callable(getter):
            return None
        return getter("document")
    except Exception:
        return None


def _forced_service_for_widget(widget: Any) -> Any | None:
    """Ungated detection spare via the session-controller backlink.

    Same walk as :func:`crop_service_for_widget`'s controller branch, but
    calling ``_get_crop_service_forced`` (never None-gated by the global
    flag). Used only for explicit per-image On (True); never wired into the
    decode pipeline. Never raises.
    """
    try:
        provider = getattr(widget, "_context_menu_provider", None)
        for ctrl in (
            getattr(widget, "_pending_session_controller", None),
            getattr(provider, "_session_ctrl", None),
        ):
            getter = getattr(ctrl, "_get_crop_service_forced", None)
            if not callable(getter):
                continue
            try:
                svc = getter()
            except Exception:
                continue
            if svc is not None and not isinstance(svc, bool):
                return svc
    except Exception:
        pass
    return None


def resolve_slot_boxes(
    widget: Any,
    source_key: Any | None = None,
    crop_service: Any | None = None,
    document: Any | None = None,
) -> tuple[Any | None, Any | None]:
    """``(box1, box2)`` effective boxes for both slots; ``None`` per slot.

    Single consumer of ``effective_crop_box_for_path`` for the canvas layer
    (W3a). Per-image overrides (W5b) resolve from ``document`` (explicit arg
    wins, else the widget Store's ``document`` slot) via
    ``crop_override_for_path``: Off → ``None`` without detection, otherwise
    detection as today.
    """
    svc = crop_service_for_widget(widget, explicit=crop_service)
    path1, path2 = slot_paths_for_widget(widget, source_key=source_key)
    doc = document if document is not None else _document_for_widget(widget)
    try:
        from tabs.image_compare.state.document import crop_override_for_path

        ov1 = crop_override_for_path(doc, path1) if doc is not None else None
        ov2 = crop_override_for_path(doc, path2) if doc is not None else None
    except Exception:
        ov1, ov2 = None, None
    forced = None
    if (ov1 is True or ov2 is True) and svc is None:
        forced = _forced_service_for_widget(widget)
    return (
        resolve_box_for_path(path1, svc, override=ov1, forced_service=forced),
        resolve_box_for_path(path2, svc, override=ov2, forced_service=forced),
    )


def box_for_texture_key(
    widget: Any,
    texture_key: Any,
    *,
    source_key: Any | None = None,
    crop_service: Any | None = None,
    document: Any | None = None,
) -> Any | None:
    """Effective box for one texture role key (stored/source slots).

    ``stored_N``/``source_N`` map to slot N's path; the diff role (and
    unknown keys) have no file path, so no box. ``None``-box parity: unknown
    keys resolve to ``None`` (no clipping). ``document`` forwards to
    :func:`resolve_slot_boxes` for the per-image override lookup.
    """
    slot = slot_for_texture_key(widget, texture_key)
    if slot is None:
        return None
    boxes = resolve_slot_boxes(
        widget, source_key=source_key, crop_service=crop_service, document=document
    )
    try:
        return boxes[slot]
    except Exception:
        return None


def clip_image_for_upload(image: Any, box: tuple[int, int, int, int] | None) -> Any:
    """Crop a PIL/QImage upload source to *box* (already in image coords).

    ``None`` box returns the image unchanged. ``TiledPixelStore`` sources
    are returned unchanged — they stay lazy (``realize_tile_plan`` crops
    tiles through :class:`BoxCroppedStoreView`). Never raises: any failure
    yields the original image (today's behavior).
    """
    if image is None or box is None:
        return image
    try:
        from engine.store.tiled_pixel_store import TiledPixelStore

        if isinstance(image, TiledPixelStore):
            return image
    except Exception:
        pass
    try:
        from PySide6.QtGui import QImage

        if isinstance(image, QImage):
            left, top, right, bottom = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
            return image.copy(left, top, right - left, bottom - top)
    except Exception:
        pass
    try:
        return image.crop((int(box[0]), int(box[1]), int(box[2]), int(box[3])))
    except Exception:
        return image


class BoxCroppedStoreView:
    """Box-sized view over a full-frame ``TiledPixelStore`` (W3a).

    Tile grids register on the *box* size while pixels stay full-frame in
    the memmap: this view translates box-local rects back to full-frame
    coords for the existing sub-rect utils (``crop_apron_tile``'s generic
    ``.size``/``.crop()`` branch, ``pixel_source_size``, ``get_image_dims``),
    so per-tile crops never materialize the box. Read-only; the parent
    store is never mutated.
    """

    __slots__ = ("_parent", "_box", "_size")

    def __init__(self, parent: Any, box: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
        self._parent = parent
        self._box = (left, top, right, bottom)
        self._size = (right - left, bottom - top)

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    @property
    def parent(self) -> Any:
        return self._parent

    @property
    def crop_box(self) -> tuple[int, int, int, int]:
        return self._box

    @property
    def is_open(self) -> bool:
        try:
            return bool(self._parent.is_open)
        except Exception:
            return False

    def crop(self, box: tuple[int, int, int, int]):  # type: ignore[no-untyped-def]
        """Crop a box-local rect, translated to full-frame parent coords.

        Returns a ``QImage`` through the existing memmap sub-rect util —
        the same type ``crop_apron_tile``'s ``TiledPixelStore`` fast path
        yields, which is what tile-upload consumers expect.
        """
        left, top, right, bottom = self._box
        try:
            al, at, ar, ab = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
        except Exception:
            raise ValueError(f"BoxCroppedStoreView.crop: bad box {box!r}")
        w, h = self._size
        al = max(0, min(al, w))
        at = max(0, min(at, h))
        ar = max(0, min(ar, w))
        ab = max(0, min(ab, h))
        from engine.store.tiled_pixel_store import qimage_from_pixel_source

        return qimage_from_pixel_source(
            self._parent, (al + left, at + top, ar + left, ab + top)
        )


# Original-file dims memo: path -> (mtime, (w, h)). Header-only probe (no
# full decode), so resolving the box reference size stays cheap on
# per-frame paths (residency). Validated by mtime each call.
_original_size_memo: dict[str, tuple[float, tuple[int, int]]] = {}


def original_size_for_path(path: Any) -> tuple[int, int] | None:
    """File's ORIGINAL dims via header probe, or ``None`` on any failure."""
    if not path:
        return None
    try:
        import os

        path_str = os.fspath(path)
        try:
            mtime = os.path.getmtime(path_str)
        except OSError:
            return None
        cached = _original_size_memo.get(path_str)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        from shared.image_processing import progressive_loader as _pl

        dims = _pl.get_image_dimensions(path_str)
        if dims is None:
            return None
        result = (int(dims[0]), int(dims[1]))
        _original_size_memo[path_str] = (mtime, result)
        return result
    except Exception:
        return None


def scaled_box_for_source(
    raw_box: Any | None,
    *,
    path: Any | None,
    live_size: tuple[int, int] | None,
) -> tuple[int, int, int, int] | None:
    """*raw_box* (full-source coords) translated into a live source's coords.

    The live tier may be a downscaled preview or a unify-resampled store;
    the box scales with it via :func:`clip_box_for_image` against the
    header-probed original size. Falls back to sanitizing against the live
    size itself when the probe fails (exact whenever live == original).
    ``None`` when there is nothing to clip.
    """
    if raw_box is None or live_size is None:
        return None
    try:
        lw, lh = int(live_size[0]), int(live_size[1])
    except Exception:
        return None
    if lw <= 0 or lh <= 0:
        return None
    orig: tuple[int, int] | None = None
    try:
        orig = original_size_for_path(path)
    except Exception:
        orig = None
    if orig is None:
        return sanitize_box(raw_box, (lw, lh))
    return clip_box_for_image(raw_box, full_size=orig, img_size=(lw, lh))


def slot_for_texture_key(widget: Any, texture_key: Any) -> int | None:
    """Slot index (0/1) behind a stored/source texture role key.

    Unwraps LOD ``LevelKey`` to its base role first; diff/unknown keys
    yield ``None``.
    """
    try:
        from engine.cache.lod import LevelKey

        if isinstance(texture_key, LevelKey):
            return slot_for_texture_key(widget, texture_key.base)
    except Exception:
        pass
    try:
        texture_ids = list(getattr(widget, "texture_ids", []) or [])
        if texture_key in texture_ids:
            slot = int(texture_ids.index(texture_key))
            return slot if slot in (0, 1) else None
        source_ids = list(getattr(widget, "_source_texture_ids", []) or [])
        if texture_key in source_ids:
            slot = int(source_ids.index(texture_key))
            return slot if slot in (0, 1) else None
    except Exception:
        pass
    return None


def path_for_texture_key(
    widget: Any,
    texture_key: Any,
    *,
    source_key: Any | None = None,
) -> Any | None:
    """File path behind a texture role key, or ``None`` (diff/unknown)."""
    try:
        from engine.cache.lod import LevelKey

        if isinstance(texture_key, LevelKey):
            return path_for_texture_key(widget, texture_key.base, source_key=source_key)
    except Exception:
        pass
    slot = slot_for_texture_key(widget, texture_key)
    if slot is None:
        return None
    try:
        paths = slot_paths_for_widget(widget, source_key=source_key)
        return paths[slot]
    except Exception:
        return None
