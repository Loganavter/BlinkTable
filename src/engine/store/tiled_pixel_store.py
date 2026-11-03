"""GEGL-style tiled pixel storage — always memmap-backed RGBA8.

Every decoded full-resolution image lives in a disk-backed memory map,
addressable by fixed-size host tiles (``AppConstants.PIXEL_TILE_SIZE``).
There is no small-image fast path at the public API level; callers use
``TiledPixelStore`` uniformly for all canvas tabs.

Full-res spill uses strip writes so peak RAM is one decode buffer plus a
strip, not a second full ``HxWx4`` copy beside PIL.
Audit-Meta: pattern=state-machine reason="one memmap lifecycle — splitting threads memmap/shape/tile_size"
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import weakref
from pathlib import Path

import numpy as np
from PIL import Image
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


from shared.image_processing.autocrop.debug import autocrop_debug as _autocrop_debug

def autocrop_debug(message: str, *args) -> None:
    """Backward-compat re-export — см. shared.image_processing.autocrop.debug."""
    _autocrop_debug(message, *args)

_spill_dir_cache: str | None = None
# Legacy: глобальный кэш удалён — единый ключ PipelineCache._pixel_key
# (path, mtime, size, has_crop, box_tuple). Переменная оставлена пустой для
# обратной совместимости импортов (autocrop_service, старые тесты) — фактический
# кэш принадлежит CropService._cache (DI) + PipelineCache LRU8. Не заполняется.
_crop_box_cache: dict[str, tuple[int, int, int, int] | None] = {}

# Held for the process's whole lifetime once acquired (module-level so it
# isn't garbage-collected, which would release the OS lock early). Whether
# *this* process is the sole live holder of the spill dir -- see
# resolve_pixel_spill_dir's docstring for why this replaced a hand-rolled
# PID-sentinel-plus-/proc scheme.
_instance_lock = None
_have_exclusive_lock = False

# Deferred-delete journal, inside the spill dir (not matched by the
# ``imgsli_tps_*.raw`` purge pattern, so it is never swept as a spill).
_PENDING_DELETE_NAME = ".pending_spill_deletes"
# Serialises in-process readers/writers of that file: several stores can
# close from different threads, and the sweep rewrites the whole list.
_pending_delete_lock = threading.Lock()


def _is_owned_spill_name(name: str) -> bool:
    """The one ownership rule: only ``imgsli_tps_*.raw`` spill files are ours.

    Shared by the purge, the immediate unlink and the deferred sweep so a
    path that is not a spill of ours can never be deleted by any of them.
    """
    return name.startswith("imgsli_tps_") and name.endswith(".raw")


def _is_owned_spill_path(path: str) -> bool:
    return _is_owned_spill_name(os.path.basename(path))


def _pending_delete_file() -> str | None:
    spill_dir = resolve_pixel_spill_dir()
    if not spill_dir:
        return None
    return os.path.join(spill_dir, _PENDING_DELETE_NAME)


def _read_pending_unlocked() -> list[str]:
    pending = _pending_delete_file()
    if pending is None:
        return []
    try:
        with open(pending, "r", encoding="utf-8") as fh:
            return [line.strip() for line in fh if line.strip()]
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.debug("[SpillDelete] Cannot read pending list %s: %s", pending, exc)
        return []


def read_pending_spill_deletes() -> list[str]:
    """Absolute paths currently queued for a deferred spill delete."""
    with _pending_delete_lock:
        return _read_pending_unlocked()


def _write_pending_unlocked(paths: list[str]) -> None:
    pending = _pending_delete_file()
    if pending is None:
        return
    try:
        if not paths:
            try:
                os.remove(pending)
            except FileNotFoundError:
                pass
            return
        with open(pending, "w", encoding="utf-8") as fh:
            fh.write("\n".join(paths) + "\n")
    except OSError as exc:
        logger.debug("[SpillDelete] Cannot write pending list %s: %s", pending, exc)


def record_pending_spill_delete(path: str) -> bool:
    """Queue an owned spill file for the next sweep; never raises.

    Used when the file cannot be unlinked yet — on Windows because a numpy
    view still holds the mapping (sharing violation), and on POSIX when we
    can prove the same view survives (unlinking a mapped file there would
    "work" and hide the leak instead of fixing it).
    """
    if not _is_owned_spill_path(path):
        return False
    target = os.path.abspath(path)
    pending = _pending_delete_file()
    if pending is None:
        logger.warning(
            "[SpillDelete] No spill dir to record %s for deletion; leaving it on disk.",
            path,
        )
        return False
    try:
        with _pending_delete_lock:
            if target not in _read_pending_unlocked():
                with open(pending, "a", encoding="utf-8") as fh:
                    fh.write(target + "\n")
        logger.debug("[SpillDelete] Deferred delete of %s (still mapped)", target)
        return True
    except OSError as exc:
        logger.warning("[SpillDelete] Could not defer delete of %s: %s", target, exc)
        return False


def sweep_pending_spill_deletes() -> int:
    """Retry queued spill deletes; returns how many paths resolved.

    Only runs while this process holds the exclusive spill-dir lock (same
    gate as :func:`purge_stale_spill_files`): another live instance may
    still be mapping the queued files. Entries whose delete fails again
    (still mapped on Windows) stay queued for the next sweep; entries that
    are gone already count as resolved; lines that are not ``imgsli_tps_*.raw``
    are dropped rather than honoured.

    Read, delete and rewrite run under one lock hold so an entry queued
    concurrently by another closing store cannot be lost by the rewrite.
    """
    if not _have_exclusive_lock:
        logger.debug(
            "[SpillDelete] Another live ImgSLI instance holds the spill dir lock, "
            "skipping deferred deletes."
        )
        return 0
    resolved = 0
    with _pending_delete_lock:
        remaining: list[str] = []
        for path in _read_pending_unlocked():
            if not _is_owned_spill_path(path):
                continue
            try:
                os.remove(path)
                resolved += 1
                logger.debug("[SpillDelete] Swept deferred spill file %s", path)
            except FileNotFoundError:
                resolved += 1
            except OSError as exc:
                logger.debug("[SpillDelete] Still locked, keeping %s: %s", path, exc)
                remaining.append(path)
        _write_pending_unlocked(remaining)
    if resolved:
        logger.info("[SpillDelete] Resolved %d deferred spill delete(s).", resolved)
    return resolved


def resolve_pixel_spill_dir() -> str | None:
    """Disk-backed directory for memmap spill files (not tmpfs).

    Also acquires an exclusive, cross-platform ``QLockFile`` on the
    directory for the rest of this process's life, recording the result in
    ``_have_exclusive_lock`` for :func:`purge_stale_spill_files` to read.

    This used to be a hand-written PID-sentinel file checked against
    ``/proc`` to decide whether a leftover lock belonged to a dead process
    -- ``/proc`` only exists on Linux, so the same scheme ported naively to
    the Windows/macOS builds this project also ships would need a second,
    platform-specific liveness check. ``QLockFile`` already solves exactly
    this (stale-lock detection via PID+hostname, or a time-based fallback
    when that isn't possible) with one implementation Qt maintains per
    platform, so there is nothing OS-specific left here at all.
    """
    global _spill_dir_cache
    if _spill_dir_cache is not None:
        return _spill_dir_cache
    try:
        from PySide6.QtCore import QStandardPaths

        cache_dir = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation
        )
        if cache_dir:
            spill_dir = os.path.join(cache_dir, "pixel_tile_store")
            os.makedirs(spill_dir, exist_ok=True)
            _spill_dir_cache = spill_dir
            _acquire_instance_lock(spill_dir)
            return spill_dir
    except Exception as exc:
        logger.debug("Failed to resolve Qt cache location for spill dir: %s", exc)
    return None


def _acquire_instance_lock(spill_dir: str) -> None:
    """Split out of :func:`resolve_pixel_spill_dir` so tests can drive the
    lock-acquisition step directly against a temp directory without also
    needing to fake ``QStandardPaths``'s real cache location.

    Non-blocking: a live sibling instance must not stall our startup.
    ``QLockFile.tryLock`` itself detects and removes a stale lock left by a
    dead process (crash, force-kill) before granting it to us, so a
    leftover lock file is never a permanent blocker the way the old
    PID-sentinel scheme could become if its dead-PID sweep never got to run
    (see ``bootstrap.py``'s ``_purge_stale_pixel_spill`` docstring for that
    race)."""
    global _instance_lock, _have_exclusive_lock
    from PySide6.QtCore import QLockFile

    lock = QLockFile(os.path.join(spill_dir, ".instance.lock"))
    _have_exclusive_lock = lock.tryLock(0)
    _instance_lock = lock  # keep alive regardless of outcome


def purge_stale_spill_files() -> int:
    """Delete leftover ``imgsli_tps_*.raw`` files from crashed/killed sessions.

    Safe to call at startup before any :class:`TiledPixelStore` is allocated
    for this process (:func:`resolve_pixel_spill_dir` must have been called
    first -- it decides, via the exclusive lock, whether purging is safe).

    Only purges when this process holds the exclusive spill-dir lock, i.e.
    no other ImgSLI instance is currently live: with the lock held, every
    ``imgsli_tps_*.raw`` file found is guaranteed to be either ours (none
    yet, since this runs before any store is allocated) or an orphan from a
    session that's provably no longer running. Returns the number of bytes
    reclaimed.
    """
    spill_dir = resolve_pixel_spill_dir()
    if not spill_dir:
        return 0

    if not _have_exclusive_lock:
        logger.debug(
            "[SpillPurge] Another live ImgSLI instance holds the spill dir lock, skipping purge."
        )
        return 0

    reclaimed = 0
    try:
        for name in os.listdir(spill_dir):
            if not _is_owned_spill_name(name):
                continue
            full = os.path.join(spill_dir, name)
            try:
                size = os.path.getsize(full)
                os.remove(full)
                reclaimed += size
                logger.debug("[SpillPurge] Removed stale spill file %s (%d bytes)", name, size)
            except OSError as exc:
                logger.debug("[SpillPurge] Could not remove %s: %s", name, exc)
    except OSError:
        pass

    # Files whose close() could not unlink them (still mapped at the time)
    # are retried here, once the lock proves no sibling instance can own
    # them. Deliberately after the raw purge: anything it already removed
    # now resolves as "gone" in the sweep.
    try:
        sweep_pending_spill_deletes()
    except Exception as exc:  # never let the startup purge fail the boot
        logger.debug("[SpillPurge] Deferred delete sweep failed: %s", exc)

    if reclaimed:
        logger.info("[SpillPurge] Reclaimed %.1f MiB from stale pixel spill files.", reclaimed / 1024 / 1024)
    return reclaimed


def _spill_dir(tmp_dir: str | None) -> str | None:
    return tmp_dir if tmp_dir is not None else resolve_pixel_spill_dir()


def _allocate_spill_memmap(
    width: int, height: int, tmp_dir: str | None = None
) -> tuple[np.memmap, str]:
    """Create an empty RGBA8 spill file and return a writable memmap + path."""
    width, height = max(1, int(width)), max(1, int(height))
    expected_size = width * height * 4
    fd, path = tempfile.mkstemp(
        prefix="imgsli_tps_",
        suffix=".raw",
        dir=_spill_dir(tmp_dir),
    )
    
    try:
        try:
            if hasattr(os, 'posix_fallocate'):
                try:
                    os.posix_fallocate(fd, 0, expected_size)
                except OSError as exc:
                    raise OSError(f"No space to allocate {expected_size} bytes for pixel store: {exc}") from exc
            else:
                os.ftruncate(fd, expected_size)
        finally:
            os.close(fd)
            
        memmap = np.memmap(path, dtype=np.uint8, mode="r+", shape=(height, width, 4))
        # Zero the mapping but do NOT msync it: ``fallocate``/``ftruncate``
        # already promise zero content, this write only covers platforms where
        # that promise is weaker, and an mmap write is visible to every reader
        # of the file through the page cache — a flush buys durability the spill
        # file does not need (it is a decode cache: a crash re-decodes). Each
        # flush costs ~1.5-3 ms regardless of size, and the strip's per-tile
        # plan path allocates several 60-90 KB stores per window (measured
        # 2026-11-04: 14 flushes = 24 of a 38 ms window build).
        memmap[:] = 0
        return memmap, path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _reopen_readonly(path: str, height: int, width: int) -> np.memmap:
    return np.memmap(path, dtype=np.uint8, mode="r", shape=(height, width, 4))


def release_spill_mapping(owner) -> bool:
    """Flush and explicitly close ``owner``'s spill mapping; drop our refs.

    ``owner`` is any object that holds the mapping in ``_memmap`` (a
    :class:`TiledPixelStore` and, through it, the shared-pixel registry's
    views and entries). The attribute is left set to ``None`` so
    ``is_open`` / ``_ensure_open`` keep working.

    Returns ``True`` when nothing else referenced the mapping: the mmap
    handle is closed and the backing file can be unlinked right away.
    Returns ``False`` when another reference survived (a numpy view, a
    registry entry, a caller-held memmap): the mapping is left intact —
    closing it there would invalidate the survivor's buffer — and the
    caller must defer the unlink.

    Why the handle needs explicit closing instead of relying on CPython
    refcounting: ``np.memmap`` hands the *same* mmap handle to every view,
    so ``self._memmap = None`` drops only one reference. On Windows an
    open handle makes ``os.remove`` raise ``PermissionError``, which the
    old ``close()`` swallowed at debug level, leaking the ``.raw`` spill
    forever (POSIX unlinks mapped files happily, which is why this went
    unnoticed). A surviving reference is detected with a weakref instead
    of a reference count, so the decision does not depend on how many
    temporaries a caller happens to hold.
    """
    model = owner.__dict__.pop("_memmap", None)
    if model is None:
        return True
    try:
        try:
            # Never bind ``model.flags`` / ``model.flush`` to a local before
            # the survivor check below: both are objects that reference the
            # array (``flagsobj.a`` is the array itself, a bound method keeps
            # its ``__self__``), so a local would defeat the check and make
            # every close look like "still mapped".
            if model.flags.writeable:
                model.flush()
        except Exception as exc:  # a read-only/closed mapping needs no flush
            logger.debug("Spill mapping flush skipped for %s: %s", owner, exc)
        handle = getattr(model, "_mmap", None)
        try:
            last_owner = weakref.ref(model)
        except TypeError:
            # Unverifiable (no weakref support): keep the file rather than
            # risk invalidating a buffer we cannot see; the sweep retries.
            logger.debug("Cannot weakref spill mapping; deferring its delete")
            return False
        del model
        if last_owner() is not None:
            return False  # a view still maps it: do not close handle under it
        if handle is not None:
            try:
                handle.close()
            except (BufferError, OSError, ValueError) as exc:
                logger.debug("Explicit spill memmap close failed: %s", exc)
                return False
        return True
    finally:
        owner._memmap = None


def _unlink_spill_file(path: str, *, exclusive: bool) -> bool:
    """Delete an owned spill file now, or queue it for the deferred sweep.

    ``exclusive`` is :func:`release_spill_mapping`'s answer. With a
    surviving view the unlink is deferred even on POSIX, where it would
    "succeed" while someone still maps the file: that hides exactly the
    leak this path exists to make visible, and Windows would refuse it
    anyway. Any other ``OSError`` (Windows sharing violation) is deferred
    too instead of being swallowed.
    """
    if not _is_owned_spill_path(path):
        return False
    if not exclusive:
        record_pending_spill_delete(path)
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.debug("Spill delete deferred for %s: %s", path, exc)
        record_pending_spill_delete(path)
        return False


def _strip_height() -> int:
    from core.constants import AppConstants

    return max(1, int(AppConstants.PIXEL_TILE_SIZE))


def _write_rgba_strips(
    memmap: np.memmap,
    source: Image.Image | np.ndarray,
    *,
    src_box: tuple[int, int, int, int] | None = None,
) -> None:
    """Copy ``source`` into ``memmap`` in horizontal strips (no full ``asarray``).

    ``src_box`` is ``(left, top, right, bottom)`` in source pixel space. When
    set, only that region is written and must match ``memmap`` shape.
    """
    strip_h = _strip_height()
    out_h, out_w, _ = memmap.shape

    if isinstance(source, np.ndarray):
        arr = np.asarray(source)
        if arr.ndim != 3 or arr.shape[2] < 3:
            raise ValueError(f"Expected HxWxC array, got shape {arr.shape}")
        src_h, src_w = int(arr.shape[0]), int(arr.shape[1])
        if src_box is None:
            left, top, right, bottom = 0, 0, src_w, src_h
        else:
            left, top, right, bottom = (int(v) for v in src_box)
        if (bottom - top, right - left) != (out_h, out_w):
            raise ValueError(
                f"src_box {(right - left)}x{(bottom - top)} != memmap {out_w}x{out_h}"
            )
        import time

        # Helper: normalize uint16 -> uint8 by scaling (>>8), not truncation
        def _to_u8(b: np.ndarray) -> np.ndarray:
            if b.dtype == np.uint8:
                return b
            if b.dtype == np.uint16:
                # 16-bit JXL etc: 0..65535 -> 0..255 via high byte (>>8), i.e.
                # 300 -> 1 not 44 (mod-256 truncation). Equivalent to //257.
                return (b >> 8).astype(np.uint8)
            # Fallback for float or other: clip and cast
            if b.dtype.kind == "f":
                return np.clip(b, 0, 255).astype(np.uint8)
            return np.asarray(b, dtype=np.uint8)

        y = 0
        while y < out_h:
            chunk = min(strip_h, out_h - y)
            src_y0 = top + y
            src_y1 = src_y0 + chunk
            band = arr[src_y0:src_y1, left:right, :4]
            if band.shape[2] == 3:
                rgba = np.empty((chunk, out_w, 4), dtype=np.uint8)
                rgba[:, :, :3] = _to_u8(band)
                rgba[:, :, 3] = 255
                memmap[y : y + chunk, :, :] = rgba
            else:
                memmap[y : y + chunk, :, :] = _to_u8(band)
            y += chunk
        return

    rgba = source if source.mode == "RGBA" else source.convert("RGBA")
    src_w, src_h = rgba.size
    if src_box is None:
        left, top, right, bottom = 0, 0, src_w, src_h
    else:
        left, top, right, bottom = (int(v) for v in src_box)
    if (bottom - top, right - left) != (out_h, out_w):
        raise ValueError(
            f"src_box {(right - left)}x{(bottom - top)} != memmap {out_w}x{out_h}"
        )
    import time
    y = 0
    while y < out_h:
        chunk = min(strip_h, out_h - y)
        src_y0 = top + y
        src_y1 = src_y0 + chunk
        band = rgba.crop((left, src_y0, right, src_y1))
        memmap[y : y + chunk, :, :] = np.asarray(band, dtype=np.uint8).reshape(
            chunk, out_w, 4
        )
        y += chunk
    # No msync here: the write is already visible to every reader through the
    # page cache, and a spill file is a cache (a crash re-decodes). See
    # ``_allocate_spill_memmap`` for the measured cost of these flushes.


def _find_trim_box_vips(
    rgb: np.ndarray, *, threshold: int = 15
) -> tuple[int, int, int, int] | None:
    """Совместимость — делегирует в shared.image_processing.autocrop.vips."""
    from shared.image_processing.autocrop.vips import find_box_vips

    box = find_box_vips(rgb, threshold=threshold)
    return box.to_tuple() if box is not None else None


def _decode_path_to_rgba(path: str | Path) -> Image.Image | np.ndarray:
    """Decode file to one RGBA surface (PIL or ndarray for JXL).

    Returns a single decode buffer — no extra ``.copy()``. Caller strip-spills
    then drops this object.
    """
    from shared.image_processing.progressive_loader import (
        JXL_SUPPORTED,
        _ensure_supported_dimensions,
    )

    path_str = os.fspath(path)
    if JXL_SUPPORTED and path_str.lower().endswith(".jxl"):
        import imagecodecs

        decoded = imagecodecs.imread(path_str)
        height, width = int(decoded.shape[0]), int(decoded.shape[1])
        _ensure_supported_dimensions(width, height, path_str)
        return decoded

    img = Image.open(path_str)
    try:
        _ensure_supported_dimensions(img.width, img.height, path_str)
        rgba = img.convert("RGBA")
        rgba.load()
        return rgba
    finally:
        img.close()


def _stream_pyvips_to_memmap(
    path_str: str,
    tmp_dir: str | None,
    crop_service=None,
    auto_crop: bool | None = None,
) -> tuple[np.memmap, str, int, int]:
    import pyvips

    img = pyvips.Image.new_from_file(path_str, access="sequential")
    # Source depth metadata for --debug diagnosis (header-only, no pixel
    # evaluation — safe on sequential access). A non-uchar format here is
    # exactly how ic-right-image-vanish hid: healthy RGB, zeroed alpha.
    _src_format = img.format
    _src_bands = img.bands
    _src_hasalpha = img.hasalpha()
    # INFO, not DEBUG: header-only attrs, zero pixel cost, and this is the
    # line that would have caught ic-right-image-vanish from any old log.
    logger.info(
        "[TileStore] stream source %s format=%s bands=%s hasalpha=%s",
        path_str,
        _src_format,
        _src_bands,
        _src_hasalpha,
    )
    if img.format != "uchar":
        if img.format == "ushort":
            # 16-bit -> 8-bit by scaling, not C truncation (cast("uchar") keeps low byte)
            img = (img / 257).cast("uchar")
        elif img.format in ("char", "short", "int"):
            # Signed or other integer: clamp then cast
            img = img.cast("uchar")
        elif img.format in ("float", "double"):
            img = (img * 255).cast("uchar")
        else:
            img = img.cast("uchar")
    if not img.hasalpha():
        # AFTER the depth cast: joining 255 into a ushort image stores
        # alpha=255/65535 (~transparent); the /257 scale then rounds it to
        # 0 and the whole store renders blank (ic-right-image-vanish).
        img = img.bandjoin(255)

    src_w = img.width
    src_h = img.height

    # Совместимость: auto_crop bool -> ephemeral CropService
    if crop_service is None and auto_crop is True:
        try:
            from shared.image_processing.autocrop import CropService

            crop_service = CropService()
        except Exception:
            crop_service = None
    # Если передали bool как crop_service (старые вызовы), нормализуем
    if isinstance(crop_service, bool):
        if crop_service:
            from shared.image_processing.autocrop import CropService

            crop_service = CropService()
        else:
            crop_service = None

    src_box = None
    if crop_service is not None:
        try:
            box = crop_service.get(path_str)
            src_box = box.to_tuple() if box is not None else None
            # fallback: если get вернул None, но прямой PIL-зонд может дать bbox
            # (parity с прошлой логикой fallback thr30) — CropService уже пробует оба thr
        except Exception as e:
            logger.debug("crop_service failed for %s: %s", path_str, e)
            src_box = None
        autocrop_debug("streaming probe box=%s (src %dx%d)", src_box, src_w, src_h)
    else:
        autocrop_debug(
            "streaming verdict=SKIP reason=disabled (no crop_service; "
            "settings auto_crop_black_borders off?) path=%s src=%dx%d",
            path_str, src_w, src_h,
        )

    out_w = src_w
    out_h = src_h
    if src_box is not None:
        left, top, right, bottom = src_box
        out_w = right - left
        out_h = bottom - top
        logger.info("Auto-crop applied via pyvips: %s (Orig: %dx%d)", src_box, src_w, src_h)
        autocrop_debug(
            "streaming verdict=APPLIED src=%dx%d out=%dx%d box=%s",
            src_w, src_h, out_w, out_h, src_box,
        )
    elif crop_service is not None:
        autocrop_debug(
            "streaming verdict=SKIP reason=probe found no box path=%s src=%dx%d",
            path_str, src_w, src_h,
        )

    memmap, spill_path = _allocate_spill_memmap(out_w, out_h, tmp_dir)

    try:
        import time
        strip_h = _strip_height()
        if src_box is not None:
            left, top, right, bottom = src_box
            img = img.crop(left, top, out_w, out_h)

        # Region.fetch (not repeated top-level crop().write_to_memory() calls)
        # is required here: each independent sink evaluation on a
        # sequential-access source restarts the reader's line cursor, so a
        # second strip pull past what the first already consumed raises
        # "out of order read". Region.fetch shares one cursor across calls.
        region = pyvips.Region.new(img)
        y = 0
        while y < out_h:
            chunk = min(strip_h, out_h - y)
            strip_bytes = region.fetch(0, y, out_w, chunk)
            arr = np.ndarray(buffer=strip_bytes, dtype=np.uint8, shape=(chunk, out_w, 4))
            memmap[y:y+chunk, :, :] = arr
            y += chunk
        memmap.flush()
        # Depth/alpha audit (debug only): catches depth-induced transparency
        # regressions without a new repro run — e.g. a 16-bit source whose
        # alpha collapsed to 0 while RGB stayed healthy. Numpy scan of the
        # already-written memmap; no extra vips evaluation (sequential
        # cursor is already consumed). Lazy %s-args: zero cost unless
        # --debug enables DEBUG.
        try:
            _alpha = np.asarray(memmap[:, :, 3])
            _rgb = np.asarray(memmap[:, :, :3])
            logger.debug(
                "[TileStore] stream depth src=%s format=%s bands=%s "
                "hasalpha=%s out=%dx%d alpha_min=%s alpha_max=%s rgb_mean=%.1f",
                path_str,
                _src_format,
                _src_bands,
                _src_hasalpha,
                out_w,
                out_h,
                int(_alpha.min()),
                int(_alpha.max()),
                float(_rgb.mean()),
            )
        except Exception as exc:
            logger.debug("[TileStore] stream depth audit skipped: %r", exc)
            pass
    except Exception:
        try:
            os.remove(spill_path)
        except OSError:
            pass
        raise

    return memmap, spill_path, out_w, out_h


class TiledPixelStore:
    """Disk-backed RGBA8 buffer with tile-addressable reads."""

    mode = "RGBA"

    def __init__(
        self,
        memmap: np.memmap,
        path: str,
        *,
        tile_size: int,
        generation: int = 0,
        owns_file: bool = True,
    ):
        self._memmap: np.memmap | None = memmap
        self._path: str | None = path
        self._owns_file: bool = bool(owns_file)
        self._tile_size = max(1, int(tile_size))
        self._generation = int(generation)
        self.info: dict = {}
        height, width, _ = memmap.shape
        self._tile_rows = (height + self._tile_size - 1) // self._tile_size
        self._tile_cols = (width + self._tile_size - 1) // self._tile_size

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def is_open(self) -> bool:
        return self._memmap is not None

    def lease(self):
        from shared.image_processing.store_lease import StoreLease

        return StoreLease.capture(self)

    @classmethod
    def allocate(cls, width: int, height: int, tmp_dir: str | None = None) -> "TiledPixelStore":
        from core.constants import AppConstants

        memmap, path = _allocate_spill_memmap(width, height, tmp_dir)
        return cls(memmap, path, tile_size=AppConstants.PIXEL_TILE_SIZE)

    def write_pil(self, box: tuple[int, int, int, int], pil_image: Image.Image) -> None:
        left, top, right, bottom = box
        memmap = self._ensure_open()
        rgba = pil_image if pil_image.mode == "RGBA" else pil_image.convert("RGBA")
        # Local write may be small; still avoid a needless full-image asarray.
        strip_h = _strip_height()
        out_h = bottom - top
        out_w = right - left
        import time
        y = 0
        while y < out_h:
            chunk = min(strip_h, out_h - y)
            band = rgba.crop((0, y, out_w, y + chunk))
            memmap[top + y : top + y + chunk, left:right, :] = np.asarray(
                band, dtype=np.uint8
            ).reshape(chunk, out_w, 4)
            y += chunk
        # No msync (see ``_allocate_spill_memmap``): page-cache coherence is
        # enough for readers, and the spill file is a cache, not a record.

    def write_array(self, box: tuple[int, int, int, int], arr: np.ndarray) -> None:
        """Write an (H, W, 4) uint8 array into ``box`` without a PIL round-trip."""
        left, top, right, bottom = box
        memmap = self._ensure_open()
        memmap[top:bottom, left:right, :] = arr

    def read_array(self, box: tuple[int, int, int, int]) -> np.ndarray:
        """Read ``box`` as an (H, W, 4) uint8 array without a PIL round-trip."""
        left, top, right, bottom = box
        memmap = self._ensure_open()
        return np.asarray(memmap[top:bottom, left:right, :], dtype=np.uint8)

    def flush(self) -> None:
        self._ensure_open().flush()

    @classmethod
    def from_pil(cls, pil_image: Image.Image, tmp_dir: str | None = None) -> "TiledPixelStore":
        from core.constants import AppConstants

        rgba = pil_image if pil_image.mode == "RGBA" else pil_image.convert("RGBA")
        width, height = rgba.size
        memmap, path = _allocate_spill_memmap(width, height, tmp_dir)
        try:
            _write_rgba_strips(memmap, rgba)
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        memmap = _reopen_readonly(path, height, width)
        return cls(memmap, path, tile_size=AppConstants.PIXEL_TILE_SIZE)

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        tmp_dir: str | None = None,
        *,
        crop_service=None,
        auto_crop: bool | None = None,
    ) -> "TiledPixelStore":
        import time
        from core.constants import AppConstants
        from shared.image_processing.progressive_loader import (
            ImageSizeLimitError,
            pyvips_can_stream,
        )

        # Нормализация DI: crop_service — предпочтительно, auto_crop — deprecated
        if crop_service is None and auto_crop is not None:
            if isinstance(auto_crop, bool) and auto_crop:
                try:
                    from shared.image_processing.autocrop import CropService

                    crop_service = CropService()
                except Exception:
                    crop_service = None
            elif not isinstance(auto_crop, bool) and auto_crop is not None:
                crop_service = auto_crop  # если передали сервис как auto_crop
        if isinstance(crop_service, bool):
            if crop_service:
                from shared.image_processing.autocrop import CropService

                crop_service = CropService()
            else:
                crop_service = None

        t0 = time.perf_counter()
        path_str = os.fspath(path)
        logger.info(f"[TileStore] from_path starting for {path_str} (crop_service={'yes' if crop_service else 'no'})")
        autocrop_debug("from_path start path=%s crop_service=%s", path_str, bool(crop_service))
        if pyvips_can_stream(path_str):
            try:
                memmap, spill_path, out_w, out_h = _stream_pyvips_to_memmap(
                    path_str, tmp_dir, crop_service=crop_service
                )
                memmap = _reopen_readonly(spill_path, out_h, out_w)
                elapsed = time.perf_counter() - t0
                logger.info(f"[TileStore] pyvips streamed {path_str} ({out_w}x{out_h}) in {elapsed:.3f}s")
                return cls(memmap, spill_path, tile_size=AppConstants.PIXEL_TILE_SIZE)
            except Exception as e:
                logger.warning("pyvips stream decode failed for %s, falling back to PIL/imagecodecs: %s", path_str, e)

        try:
            decoded = _decode_path_to_rgba(path)
        except ImageSizeLimitError:
            raise

        if isinstance(decoded, np.ndarray):
            arr = np.asarray(decoded)
            src_h, src_w = int(arr.shape[0]), int(arr.shape[1])
            if crop_service is not None:
                try:
                    box = crop_service.get(path_str)
                    src_box = box.to_tuple() if box is not None else None
                    # без fallback — единый зонд CropService (DI)
                except Exception:
                    src_box = None
            else:
                src_box = None
            if src_box is None:
                out_w, out_h = src_w, src_h
                if crop_service is None:
                    autocrop_debug(
                        "ndarray verdict=SKIP reason=disabled (no crop_service; "
                        "settings auto_crop_black_borders off?) path=%s src=%dx%d",
                        path_str, src_w, src_h,
                    )
                else:
                    autocrop_debug(
                        "ndarray verdict=SKIP reason=probe found no box path=%s src=%dx%d",
                        path_str, src_w, src_h,
                    )
            else:
                left, top, right, bottom = src_box
                out_w, out_h = right - left, bottom - top
                logger.info(
                    "Auto-crop applied: %s (Orig: %dx%d)", src_box, src_w, src_h
                )
                autocrop_debug(
                    "ndarray verdict=APPLIED box=%s src=%dx%d out=%dx%d",
                    src_box, src_w, src_h, out_w, out_h,
                )
            memmap, spill_path = _allocate_spill_memmap(out_w, out_h, tmp_dir)
            try:
                _write_rgba_strips(memmap, arr, src_box=src_box)
            except Exception:
                try:
                    os.remove(spill_path)
                except OSError:
                    pass
                raise
            del arr
            del decoded
            memmap = _reopen_readonly(spill_path, out_h, out_w)
            return cls(memmap, spill_path, tile_size=AppConstants.PIXEL_TILE_SIZE)

        rgba = decoded if decoded.mode == "RGBA" else decoded.convert("RGBA")
        if crop_service is not None:
            try:
                box = crop_service.get(path_str)
                src_box = box.to_tuple() if box is not None else None
                # без fallback — единый зонд CropService (DI)
            except Exception:
                src_box = None
        else:
            src_box = None
        if src_box is None:
            out_w, out_h = rgba.size
            if crop_service is None:
                autocrop_debug(
                    "PIL verdict=SKIP reason=disabled (no crop_service; "
                    "settings auto_crop_black_borders off?) path=%s src=%s",
                    path_str, rgba.size,
                )
            else:
                autocrop_debug(
                    "PIL verdict=SKIP reason=probe found no box path=%s src=%s",
                    path_str, rgba.size,
                )
        else:
            left, top, right, bottom = src_box
            out_w, out_h = right - left, bottom - top
            logger.info("Auto-crop applied: %s (Orig: %s)", src_box, rgba.size)
            autocrop_debug(
                "PIL verdict=APPLIED box=%s src=%s out=%dx%d",
                src_box, rgba.size, out_w, out_h,
            )

        memmap, spill_path = _allocate_spill_memmap(out_w, out_h, tmp_dir)
        try:
            _write_rgba_strips(memmap, rgba, src_box=src_box)
        except Exception:
            try:
                os.remove(spill_path)
            except OSError:
                pass
            raise
        del rgba
        memmap = _reopen_readonly(spill_path, out_h, out_w)
        return cls(memmap, spill_path, tile_size=AppConstants.PIXEL_TILE_SIZE)

    @classmethod
    def from_embedded_cache(cls, cache_path: str, width: int, height: int) -> "TiledPixelStore":
        """Wrap an already-decoded RGBA8 raw buffer extracted from a project cache.

        The buffer's dimensions already reflect whatever crop was applied
        when it was captured at save time — no auto-crop pass here.

        The store is a read-only tenant: the file is owned by the project
        extract cache (pixel_cache_registry), so :meth:`close` must not
        delete it.

        W3.1: validate dims vs MAX_SUPPORTED_IMAGE_DIMENSION and
        ``st_size >= w*h*4`` before memmap to avoid SIGBUS.
        """
        from core.constants import AppConstants
        from shared.image_processing.progressive_loader import (
            is_supported_image_dimension,
            max_supported_image_dimension,
        )

        width, height = int(width), int(height)
        if not is_supported_image_dimension(width, height):
            raise ValueError(f"Embedded cache dims {width}x{height} out of bounds (max {max_supported_image_dimension()})")
        try:
            st_size = Path(cache_path).stat().st_size
        except OSError as exc:
            raise OSError(f"Cannot stat embedded cache {cache_path}: {exc}") from exc
        expected = width * height * 4
        if st_size < expected:
            raise ValueError(f"Embedded cache file too small ({st_size} < {expected} = {width}x{height}x4)")
        memmap = _reopen_readonly(cache_path, height, width)
        return cls(memmap, cache_path, tile_size=AppConstants.PIXEL_TILE_SIZE, owns_file=False)

    @property
    def tile_size(self) -> int:
        return self._tile_size

    @property
    def tile_rows(self) -> int:
        return self._tile_rows

    @property
    def tile_cols(self) -> int:
        return self._tile_cols

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def size(self) -> tuple[int, int]:
        height, width, _ = self._ensure_open().shape
        return (width, height)

    @property
    def width(self) -> int:
        return self._ensure_open().shape[1]

    @property
    def height(self) -> int:
        return self._ensure_open().shape[0]

    def _ensure_open(self) -> np.memmap:
        if self._memmap is None:
            raise RuntimeError("TiledPixelStore is closed")
        return self._memmap

    def read_tile(self, row: int, col: int) -> Image.Image:
        """Return one host tile as a PIL RGBA image."""
        memmap = self._ensure_open()
        if row < 0 or col < 0 or row >= self._tile_rows or col >= self._tile_cols:
            raise IndexError(f"tile ({row}, {col}) out of range")
        ts = self._tile_size
        top = row * ts
        left = col * ts
        bottom = min(top + ts, self.height)
        right = min(left + ts, self.width)
        region = np.array(memmap[top:bottom, left:right, :], copy=True)
        return Image.fromarray(region, mode="RGBA")

    def crop(self, box: tuple[int, int, int, int]) -> Image.Image:
        left, top, right, bottom = box
        memmap = self._ensure_open()
        region = np.array(memmap[top:bottom, left:right, :], copy=True)
        return Image.fromarray(region, mode="RGBA")

    def materialize_full(self) -> Image.Image:
        """Return a full in-memory RGBA copy.

        Prefer :meth:`crop`, :meth:`read_tile`, or tile iteration for
        residency/export paths. This materializes every pixel and can spike
        CPU/RAM on first use — acceptable only for workers that inherently
        need the whole image (SSIM/unify/export resize).
        """
        memmap = self._ensure_open()
        return Image.fromarray(np.array(memmap), mode="RGBA")

    def as_pil_rgba(self) -> Image.Image:
        """Zero-copy read-only RGBA view over the spill mapping.

        Exists so a bounded consumer can be derived from the ONE authoritative
        decode instead of decoding the file again (gallery owner invariant I2:
        the Stage-2 proxy used to be a second full decode beside this store).
        The view SHARES the memmap and is therefore only valid while the store
        is open: copy or downscale it (``convert``/``resize``/``crop``) before
        :meth:`close`. Never mutate it — the mapping is not a scratch buffer.
        """
        return Image.fromarray(self._ensure_open())

    def close(self) -> None:
        path = self._path
        owns = getattr(self, "_owns_file", True)
        # Order matters: release (flush + explicit mmap close) must happen
        # before the unlink, and it tells us whether anything still maps the
        # file. On Windows a surviving view keeps the handle open and the
        # unlink fails; such a delete is queued instead of swallowed.
        exclusive = release_spill_mapping(self)
        self._path = None
        self._generation += 1
        if path and owns:
            _unlink_spill_file(path, exclusive=exclusive)

    def __enter__(self) -> "TiledPixelStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self):
        if getattr(self, "_path", None):
            self.close()


# Part D (2026-10-17): duck-typed pixel-source adapters moved to
# engine/store/pixel_source_adapters.py (no memmap access) — re-exported here
# so existing `from engine.store.tiled_pixel_store import ...` importers keep working.
from engine.store.pixel_source_adapters import (  # noqa: E402
    close_pixel_store,
    maybe_wrap_pixel_store,
    pixel_source_size,
    qimage_from_pixel_source,
    to_real_pil_copy,
)
