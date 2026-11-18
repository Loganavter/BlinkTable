"""Tile-native unify: resize both sides to a common canvas into ``TiledPixelStore``."""

from __future__ import annotations

import logging

from PIL import Image

from engine.store.pixel_ops.resample import write_resampled_to_store
from shared.image_processing.resample_map import get_resample
from engine.store.tiled_pixel_store import (
    TiledPixelStore,
    maybe_wrap_pixel_store,
    pixel_source_size,
    to_real_pil_copy,
)
from shared.image_processing.store_lease import StoreLease
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

# Below this size unified output uses PIL resize_images_processor (exact match).
_ACCURATE_UNIFY_MAX_PIXELS = 4096 * 4096


def is_preview_tier(source) -> bool:
    """True when ``source`` is a preview-tier QImage (never unify input).

    Single source of truth for the preview-tier predicate: the tile-native
    path must never upscale a preview (a 1024/2048/4096 long-edge QImage, the
    tab layer's preview buckets) into a full-size store, and tab-side callers
    gate on this before spawning a worker so mixed-tier pairs serve ready
    sides without an empty task.
    """
    try:
        from PySide6.QtGui import QImage
    except Exception:
        return False
    return isinstance(source, QImage)


def unify_pair(
    source1,
    source2,
    method_name: str = "LANCZOS",
    *,
    lease1: StoreLease | None = None,
    lease2: StoreLease | None = None,
    should_abort=None,
) -> tuple[TiledPixelStore | Image.Image | None, TiledPixelStore | Image.Image | None]:
    """Unify two sources to a common canvas (max width/height per side).

    ``should_abort`` is polled between output strips; a superseded unify
    (newer pair scheduled) must stop burning CPU/IO instead of finishing a
    result nobody will use. On abort partial stores are closed and
    ``(None, None)`` is returned.
    """
    if lease1 is not None and not lease1.valid:
        return None, None
    if lease2 is not None and not lease2.valid:
        return None, None

    if is_preview_tier(source1) or is_preview_tier(source2):
        # Preview tier must never enter unify: the tile-native path would
        # silently upscale a preview (1024/2048/4096 long edge) into a full-size
        # store (burning a full resample) and the result would poison the unify
        # memo as a full-res pair. The full-res arrival re-triggers unify via
        # handle_full_image_loaded -> ensure_unification; until then the
        # preview-backing display path holds the preview.
        logger.debug(
            "Unify skipped (preview-tier QImage side): %s + %s",
            type(source1).__name__,
            type(source2).__name__,
        )
        return None, None

    if source1 is None and source2 is None:
        return None, None

    resample = get_resample(method_name)

    w1, h1 = pixel_source_size(source1)
    w2, h2 = pixel_source_size(source2)
    target_w = max(w1, w2)
    target_h = max(h1, h2)
    if target_w <= 0 or target_h <= 0:
        return None, None

    if target_w * target_h <= _ACCURATE_UNIFY_MAX_PIXELS:
        from engine.store.pixel_ops.downscale import downscale_source_to_pil
        from shared.image_processing.resize import resize_images_processor

        try:
            pil1 = (
                downscale_source_to_pil(source1, (w1, h1), resample=resample)
                if source1 is not None
                else None
            )
            pil2 = (
                downscale_source_to_pil(source2, (w2, h2), resample=resample)
                if source2 is not None
                else None
            )
        except RuntimeError as exc:
            # Source closed mid-unify (race with Store swap / new unify task).
            # Treat as abort, not error — caller will retry with live store.
            logger.debug("Unify aborted (source closed): %s", exc)
            return None, None
        u1, u2 = resize_images_processor(pil1, pil2, method_name)
        return maybe_wrap_pixel_store(u1), maybe_wrap_pixel_store(u2)

    if (w1, h1) == (target_w, target_h) and (w2, h2) == (target_w, target_h):
        return source1, source2

    out1 = source1 if (w1, h1) == (target_w, target_h) else None
    out2 = source2 if (w2, h2) == (target_w, target_h) else None

    if out1 is None and source1 is not None:
        try:
            out1 = TiledPixelStore.allocate(target_w, target_h)
            if not write_resampled_to_store(
                out1, source1, target_w, target_h, resample, should_abort=should_abort
            ):
                out1.close()
                return None, None
        except (OSError, RuntimeError) as exc:
            # RuntimeError = source closed mid-unify (race) — treat as abort
            if isinstance(exc, RuntimeError) and "closed" in str(exc).lower():
                logger.debug("Unify aborted (source1 closed): %s", exc)
                if out1 is not None and out1 is not source1:
                    try:
                        out1.close()
                    except Exception:
                        pass
                return None, None
            logger.warning("Tile-native unify memmap failed for source1, falling back to PIL: %s", exc)
            try:
                pil1 = to_real_pil_copy(source1)
            except RuntimeError:
                return None, None
            pil2 = to_real_pil_copy(source2) if source2 is not None else None
            u1, u2 = resize_images_processor(pil1, pil2, method_name)
            return maybe_wrap_pixel_store(u1), maybe_wrap_pixel_store(u2)

    if out2 is None and source2 is not None:
        try:
            out2 = TiledPixelStore.allocate(target_w, target_h)
            if not write_resampled_to_store(
                out2, source2, target_w, target_h, resample, should_abort=should_abort
            ):
                assert out2 is not None
                out2.close()
                if out1 is not source1:
                    assert out1 is not None
                    out1.close()
                return None, None
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, RuntimeError) and "closed" in str(exc).lower():
                logger.debug("Unify aborted (source2 closed): %s", exc)
                if out2 is not None and out2 is not source2:
                    try:
                        out2.close()
                    except Exception:
                        pass
                if out1 is not None and out1 is not source1:
                    try:
                        out1.close()
                    except Exception:
                        pass
                return None, None
            logger.warning("Tile-native unify memmap failed for source2, falling back to PIL: %s", exc)
            try:
                pil1 = to_real_pil_copy(source1) if source1 is not None else None
            except RuntimeError:
                return None, None
            try:
                pil2 = to_real_pil_copy(source2) if source2 is not None else None
            except RuntimeError:
                return None, None
            u1, u2 = resize_images_processor(pil1, pil2, method_name)
            return maybe_wrap_pixel_store(u1), maybe_wrap_pixel_store(u2)

    return out1, out2