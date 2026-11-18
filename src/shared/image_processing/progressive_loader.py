import logging
import os
from typing import TYPE_CHECKING

from PIL import Image

from core.constants import AppConstants
from core.log_names import APP_LOGGER_NAME

if TYPE_CHECKING:
    from PySide6.QtGui import QImage

logger = logging.getLogger(APP_LOGGER_NAME)

try:
    import imagecodecs

    JXL_SUPPORTED = True
    logger.info("JXL Support: imagecodecs detected. JXL support enabled.")
except ImportError:
    JXL_SUPPORTED = False
    logger.warning("JXL Support: imagecodecs NOT detected. JXL support disabled.")
except Exception as e:
    JXL_SUPPORTED = False
    logger.error(f"JXL Support: Error during initialization: {e}")

try:
    import pyvips  # type: ignore[import-untyped]  # pyvips has no stubs

    PYVIPS_SUPPORTED = True
    logger.info("pyvips detected. True ROI streaming enabled.")
except ImportError:
    PYVIPS_SUPPORTED = False
    logger.debug("pyvips NOT detected. True ROI streaming disabled.")
except Exception as e:
    PYVIPS_SUPPORTED = False
    logger.error(f"pyvips Support: Error during initialization: {e}")

class ImageSizeLimitError(ValueError):
    pass


# JXL header-only dimensions live in ``jxl_header`` (split per the file-size
# policy: the codestream SizeHeader parse is a cohesive unit of its own).
# Re-exported here so existing ``progressive_loader.read_jxl_header_dimensions``
# callers keep working.
from shared.image_processing.jxl_header import read_jxl_header_dimensions


def _ensure_supported_dimensions(width: int, height: int, image_path: str, ignore_limit: bool = False) -> None:
    if ignore_limit:
        return
    max_dim = max_supported_image_dimension()
    if max(int(width), int(height)) > max_dim:
        raise ImageSizeLimitError(
            f"Image is too large for the installed decoder: {width}x{height} "
            f"exceeds the {max_dim}px-per-side limit. Streaming decode "
            f"(libvips) is unavailable for this file."
        )


_vips_suffixes_cache: frozenset[str] | None = None


def _vips_suffixes() -> frozenset[str]:
    """Lowercased ``pyvips.get_suffixes()`` (e.g. ``.jxl``, ``.jpg``), cached.

    ``get_suffixes()`` reflects the *installed* libvips build's loaders —
    JXL/HEIF/AVIF are optional codecs that some builds lack, so capability
    must be probed per format, not assumed from ``PYVIPS_SUPPORTED`` alone.
    """
    global _vips_suffixes_cache
    if _vips_suffixes_cache is None:
        try:
            import pyvips

            _vips_suffixes_cache = frozenset(
                s.lower() for s in pyvips.get_suffixes()
            )
        except Exception:
            _vips_suffixes_cache = frozenset()
    return _vips_suffixes_cache


def pyvips_can_stream(path: str | os.PathLike) -> bool:
    """Whether pyvips can stream-decode ``path``'s format with this libvips.

    Unlike the module-wide ``PYVIPS_SUPPORTED`` flag, this checks the
    *file's own* extension against what the installed libvips build actually
    has loaders for. The streaming path bypasses
    ``MAX_SUPPORTED_IMAGE_DIMENSION``, so probes must only skip the bound
    for files that will actually stream — not for files that will fall back
    to the PIL/imagecodecs full-frame backend (which must keep enforcing it).
    """
    if not PYVIPS_SUPPORTED:
        return False
    ext = os.path.splitext(os.fspath(path))[1].lower()
    return bool(ext) and ext in _vips_suffixes()


def max_supported_image_dimension() -> int:
    """Single read of the decode-backend dimension bound.

    One home for the ``65536`` literal: every consumer goes through this
    (or the predicates below) instead of re-stating
    ``AppConstants.MAX_SUPPORTED_IMAGE_DIMENSION`` inline.
    """
    return int(getattr(AppConstants, "MAX_SUPPORTED_IMAGE_DIMENSION", 65536))


def needs_bounded_decode(image_path: str | os.PathLike) -> bool:
    """Whether ``image_path`` decodes via the PIL/imagecodecs full-frame path.

    ``False`` means libvips can stream the file's own format with the
    installed build (:func:`pyvips_can_stream`) — the streaming path spills
    strips straight to the memmap with no full-frame decode buffer, so no
    dimension bound applies. ``True`` means the bounded full-frame backend
    will run (including the stream-failure fallback in
    ``TiledPixelStore.from_path``, which must still enforce the bound).
    """
    return not pyvips_can_stream(image_path)


def is_supported_image_dimension(
    width: int, height: int, image_path: str | os.PathLike | None = None
) -> bool:
    """One home for the image-dimension rule (cross-cutting policy owner).

    Returns ``True`` when a ``width`` x ``height`` image may be handled:
    positive dims within :func:`max_supported_image_dimension` — unless
    ``image_path`` is given and streams (:func:`needs_bounded_decode` is
    ``False``), in which case the streaming exception applies and only
    positivity is required. Raw-buffer sites (embedded pixel caches, which
    memmap with no decode buffer) pass no path, so the bound applies.
    """
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return False
    if w <= 0 or h <= 0:
        return False
    if image_path is not None and not needs_bounded_decode(image_path):
        return True
    return max(w, h) <= max_supported_image_dimension()


def should_use_progressive_load(
    file_path: str, file_size_bytes: int | None = None
) -> bool:
    if not file_path:
        return False
    # Always use progressive preview: even small images (e.g. 764x576) go
    # via QImage preview first, then tiled full-res. Threshold forced to
    # 0 / always-True. Keep decode-backend bound check so oversized
    # non-streamable files still raise early.
    try:
        if str(file_path).lower().endswith(".jxl"):
            dims = read_jxl_header_dimensions(file_path)
            if dims is not None:
                _ensure_supported_dimensions(dims[0], dims[1], file_path, ignore_limit=pyvips_can_stream(file_path))
                return True
        with Image.open(file_path) as img:
            width, height = img.size
            _ensure_supported_dimensions(width, height, file_path, ignore_limit=pyvips_can_stream(file_path))
    except ImageSizeLimitError:
        raise
    except Exception:
        pass
    return True


def _preview_cap(target_long_edge: int | None) -> int:
    """Long edge the preview tier is decoded at (1024 unless a target says otherwise).

    The tier ladder is the tab layer's (``PREVIEW_BUCKETS`` in the image-compare
    pipeline cache); ``None``/garbage/``<= 0`` keep the historic 1024 so every
    existing caller decodes and keys exactly as before.
    """
    if target_long_edge is None:
        return 1024
    try:
        edge = int(target_long_edge)
    except (TypeError, ValueError):
        return 1024
    return edge if edge > 0 else 1024


def load_preview_image(
    image_path: str,
    crop_service=None,
    auto_crop: bool | None = None,
    *,
    target_long_edge: int | None = None,
) -> "QImage | None":
    """Load a bounded progressive preview for display-tier use only.

    Returns ``QImage`` RGBA8888 capped at ``target_long_edge`` px on the long
    edge (default 1024, the historic tier). Callers store the result in
    ``document.preview_image*`` — never wrap with ``TiledPixelStore``
    (full-res tier owns memmap storage).

    Streaming: ``pyvips.thumbnail`` when libvips can stream the format, else
    PIL ``thumbnail`` (no QImageReader — deleted per plan_image_pipeline.md
    Phase 4). Both paths decode AT the target size (shrink-on-load /
    ``thumbnail``) instead of decoding full-frame and downscaling, and neither
    upscales a source smaller than the target. ``target_long_edge=None`` keeps
    the byte-identical 1024 behaviour every existing caller relies on.

    crop_service — DI CropService (предпочтительно), auto_crop — deprecated bool.
    """
    cap = _preview_cap(target_long_edge)
    # Нормализация deprecated auto_crop → crop_service
    if crop_service is None and auto_crop is not None:
        if isinstance(auto_crop, bool) and auto_crop:
            try:
                from shared.image_processing.autocrop import CropService

                crop_service = CropService()
            except Exception:
                crop_service = None
        elif not isinstance(auto_crop, bool) and auto_crop is not None:
            crop_service = auto_crop
    if isinstance(crop_service, bool):
        if crop_service:
            from shared.image_processing.autocrop import CropService

            crop_service = CropService()
        else:
            crop_service = None

    try:
        if pyvips_can_stream(image_path):
            return _load_preview_vips(image_path, crop_service=crop_service, target_long_edge=cap)

        if JXL_SUPPORTED and image_path.lower().endswith(".jxl"):
            logger.info(f"Loading JXL preview for: {image_path}")
            # Header-first bound: the dims check must precede the
            # full-frame ``imread`` (which would OOM on an oversized file
            # before any post-decode check could fire). The post-decode
            # check stays as a consistency backstop.
            header_dims = read_jxl_header_dimensions(image_path)
            if header_dims is not None:
                _ensure_supported_dimensions(header_dims[0], header_dims[1], image_path)
            decoded = imagecodecs.imread(image_path)
            img = Image.fromarray(decoded)

            original_width, original_height = img.size
            _ensure_supported_dimensions(original_width, original_height, image_path)
            max_preview_size = cap
            scale = min(
                max_preview_size / original_width, max_preview_size / original_height
            )

            if scale < 1.0:
                new_width = int(original_width * scale)
                new_height = int(original_height * scale)
                img = img.resize((new_width, new_height), Image.Resampling.BILINEAR)

            preview = img.convert("RGBA")
            if crop_service is not None:
                from shared.image_processing.autocrop.scaling import get_scaled_box_for_thumb as _get_scaled

                box = crop_service.get(image_path)
                orig_box = box.to_tuple() if box is not None else None
                scaled = _get_scaled(orig_box, (original_width, original_height), preview.size)
                if scaled is not None:
                    preview = preview.crop(scaled.to_tuple())
                # если box is None — без кропа (единый зонд CropService, без fallback)

            from engine.store.tiled_pixel_store import qimage_from_pixel_source
            return qimage_from_pixel_source(preview)

        # PIL thumbnail fallback — single path for all non-streaming formats.
        # Replaces QImageReader 215–253 + AllocationLimit (deleted, no size bound bypass).
        with Image.open(image_path) as img:
            original_width, original_height = img.size
            _ensure_supported_dimensions(original_width, original_height, image_path)
            max_preview_size = cap
            scale = min(
                max_preview_size / original_width, max_preview_size / original_height
            )

            if scale >= 1.0:
                # Smaller than the tier: keep it as-is (never upscale a preview).
                preview = img.copy().convert("RGBA")
                new_width, new_height = preview.size
            else:
                new_width = int(original_width * scale)
                new_height = int(original_height * scale)
                preview = img.copy()
                preview.thumbnail((new_width, new_height), Image.Resampling.BILINEAR)
                preview = preview.convert("RGBA")
                new_width, new_height = preview.size

            if crop_service is not None:
                from shared.image_processing.autocrop.scaling import get_scaled_box_for_thumb as _get_scaled2

                box = crop_service.get(image_path)
                orig_box = box.to_tuple() if box is not None else None
                scaled = _get_scaled2(orig_box, (original_width, original_height), (new_width, new_height))
                if scaled is not None:
                    preview = preview.crop(scaled.to_tuple())
                # если box is None — без кропа (единый зонд CropService)
            from engine.store.tiled_pixel_store import qimage_from_pixel_source
            return qimage_from_pixel_source(preview)

    except ImageSizeLimitError:
        raise
    except Exception as e:
        logger.error(f"Failed to load preview image {image_path}: {e}")
        return None


def _load_preview_vips(
    image_path: str,
    crop_service=None,
    auto_crop: bool | None = None,
    *,
    target_long_edge: int | None = None,
) -> "QImage | None":
    """Streaming preview via ``pyvips.thumbnail`` (no full-frame decode)."""
    cap = _preview_cap(target_long_edge)
    if crop_service is None and auto_crop is not None:
        if isinstance(auto_crop, bool) and auto_crop:
            try:
                from shared.image_processing.autocrop import CropService

                crop_service = CropService()
            except Exception:
                crop_service = None
        elif not isinstance(auto_crop, bool) and auto_crop is not None:
            crop_service = auto_crop
    if isinstance(crop_service, bool):
        if crop_service:
            from shared.image_processing.autocrop import CropService

            crop_service = CropService()
        else:
            crop_service = None
    try:
        import numpy as np
        from PySide6.QtGui import QImage
        from engine.store.tiled_pixel_store import qimage_from_pixel_source
        from shared.image_processing.bounded_frame import vips_thumbnail_array

        arr = vips_thumbnail_array(image_path, cap)
        if arr.shape[2] != 4:
            rgb = arr[:, :, :3] if arr.shape[2] >= 3 else arr
            arr = np.empty((arr.shape[0], arr.shape[1], 4), dtype=np.uint8)
            arr[:, :, :3] = rgb
            arr[:, :, 3] = 255
        if crop_service is not None:
            try:
                from shared.image_processing.autocrop.scaling import get_scaled_box_for_thumb as _get_scaled

                box = crop_service.get(image_path)
                orig_box = box.to_tuple() if box is not None else None
                if orig_box is not None:
                    from PIL import Image as _PILImage

                    with _PILImage.open(image_path) as _im:
                        orig_w, orig_h = _im.size
                    scaled = _get_scaled(orig_box, (orig_w, orig_h), (arr.shape[1], arr.shape[0]))
                    if scaled is not None:
                        l, t, r, b = scaled.to_tuple()
                        arr = arr[t:b, l:r]
                # если box is None — без кропа (единый зонд CropService)
            except Exception:
                pass
        return qimage_from_pixel_source(arr)
    except ImageSizeLimitError:
        raise
    except Exception as e:
        logger.error(f"Failed to load pyvips preview for {image_path}: {e}")
        return None


def get_image_dimensions(image_path: str) -> tuple[int, int] | None:
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            _ensure_supported_dimensions(width, height, image_path, ignore_limit=pyvips_can_stream(image_path))
            return (width, height)
    except ImageSizeLimitError:
        raise
    except Exception:
        pass
    if str(image_path).lower().endswith(".jxl"):
        # The JXL branch is NOT gated on ``JXL_SUPPORTED``: the codestream
        # SizeHeader is a header parse (``jxl_header``), not a decode, so a
        # build without imagecodecs still knows the dimensions. ``JXL_SUPPORTED``
        # only gates the full-decode last resort below — and gating the header
        # on it made every size-provider report None on exactly the machines
        # where the answer was free (2026-10-28, gallery cards / properties).
        try:
            header_dims = read_jxl_header_dimensions(image_path)
            if header_dims is not None:
                _ensure_supported_dimensions(header_dims[0], header_dims[1], image_path, ignore_limit=pyvips_can_stream(image_path))
                return (header_dims[0], header_dims[1])
        except ImageSizeLimitError:
            raise
        except Exception as e:
            logger.error(f"Failed to read JXL header dimensions {image_path}: {e}")
        if JXL_SUPPORTED:
            try:
                decoded = imagecodecs.imread(image_path)
                height, width = decoded.shape[:2]
                _ensure_supported_dimensions(width, height, image_path, ignore_limit=pyvips_can_stream(image_path))
                return (width, height)
            except Exception as e:
                logger.error(f"Failed to read JXL dimensions {image_path}: {e}")
    return None

def get_image_format_info(image_path: str) -> tuple[str, bool, bool]:
    try:
        with Image.open(image_path) as img:
            format_name = img.format or "UNKNOWN"
            is_progressive = hasattr(img, "_get_loader") and "progressive" in str(
                img.info.get("progressive", 0)
            )

            return format_name, is_progressive, False
    except Exception:
        if image_path.lower().endswith(".jxl"):
            return "JXL", False, False
        return "UNKNOWN", False, False

class ProgressiveImageLoader:
    """Thin wrapper delegating to PipelineCache (single source of truth).

    Preview tier lives in PipelineCache with key
    (path,mtime,size,has_crop,box,bucket) — ``bucket`` being the long edge
    (1024/2048, see ``pipeline/cache.PREVIEW_BUCKETS``); full tier via
    PipelineCache pixel LRU. Legacy caches removed.
    New code should use PipelineCache directly.
    """

    _FULL_CACHE_MAX = 8  # compat constant, limits owned by PipelineCache now

    def __init__(self, cache=None):
        # Injected PipelineCache allowed (tab layer injects), but shared layer
        # never imports tabs.* directly (isolation contract). None → no caching,
        # direct load (caller should use PipelineCache directly for memoised path).
        self._cache = cache

    def get_preview(
        self, image_path: str, force_reload: bool = False, crop_service=None
    ) -> "QImage | None":
        # If a PipelineCache was injected, delegate (covers preview tier caching)
        if self._cache is not None:
            if not force_reload:
                try:
                    cached = self._cache.get_preview(image_path, crop_service=crop_service)
                    if cached is not None:
                        return cached
                except Exception:
                    pass
            try:
                return self._cache.get_or_load_preview(image_path, crop_service=crop_service)
            except Exception:
                pass
        return load_preview_image(image_path, crop_service=crop_service)