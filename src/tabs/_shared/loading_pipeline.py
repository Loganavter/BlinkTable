"""Shared loading seam for IC and MC (W0, boxless).

Single construction site for the file-identity key, the sync demand-fill
primitives, and the preview-first async staging shape both tabs previously
duplicated (``tabs.image_compare.pipeline.image_load_service`` single-flight
+ ``tabs.multi_compare.use_cases.preview_decode`` bounded full-FIFO second
stage). Thin-owner style per ``docs/dev/CODE_PATTERNS.md``: this module owns
no Store/EventBus/dispatch — dispatch stays in callers via the
``on_ready``/``on_error`` hooks; decode delegates to the shared backends
(never re-implemented here):

* preview via ``shared.image_processing.progressive_loader.load_preview_image``
* full-res via ``shared.image_processing.pixel_cache_loader.load_pixel_store``
* cancellation via ``tabs.image_compare.pipeline.abort.AbortSignal``
* pyramid via the ``engine.pyramid.coordinator`` coordinator (hook-injected)
* picking via ``engine.cache.display_image_picker.pick_display_image``

All decode is fail-open (sync helpers return ``None``, async errors fan out
to ``on_error`` and never raise). Nothing imports this module yet (W1/W2/W3
own the adapters), so there is no behavior change elsewhere.
Audit-Meta: pattern=thin-owner reason="W0 shared loading seam: key + sync fills + preview-first single-flight + bounded full FIFO + pyramid hook"
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from engine.cache.display_image_picker import pick_display_image
from tabs.image_compare.pipeline.abort import AbortSignal

from tabs._shared.overflow_summary import summarize_overflow
from shared.fs_paths import fs_path_key
from core.log_names import APP_LOGGER_NAME

if TYPE_CHECKING:
    from PySide6.QtGui import QImage

    from engine.store.tiled_pixel_store import TiledPixelStore

logger = logging.getLogger(APP_LOGGER_NAME)

#: Bound for the full-res second stage: at most this many full decodes in
#: flight; the rest wait in a FIFO queue. Preview workers stay unbounded
#: (fast, ~1024px) — only the heavy full decode is throttled
#: (``multi_compare.use_cases.preview_decode`` parity).
_FULL_MAX_CONCURRENT = 2

#: Cap on the full-stage FIFO backlog. Entries past this are shed
#: oldest-first with one summary (never silent): a slot whose full-res entry
#: was shed keeps its fast preview tier and still converges via demand fill.
_FULL_QUEUE_MAX = 64

__all__ = [
    "cache_key_for_path",
    "get_or_load_preview",
    "get_or_load_pixel",
    "ensure_async",
    "start_pyramid_build",
    "pick_display_image",
]


def _same_fs_path(a: Path | str, b: Path | str) -> bool:
    """Path equality for slot-ownership guards (single impl).

    Heals textual variants of the same file (``/x/./f.png`` vs ``/x/f.png``,
    ``str`` vs ``Path``) and the platform's own case-folding (see
    :func:`shared.fs_paths.fs_path_key`); never equates distinct files on a
    case-sensitive filesystem.
    """
    try:
        return fs_path_key(a) == fs_path_key(b)
    except Exception:
        return a == b  # type: ignore[return-value]


def cache_key_for_path(path: Path | str) -> tuple[str, int, int]:
    """Content key ``(fs_path_key, mtime_ns, size)`` (boxless by construction).

    ``mtime+size`` keeps an overwritten-in-place file from colliding with its
    own stale decode; :func:`shared.fs_paths.fs_path_key` (``normpath`` +
    ``normcase``) heals textual variants of the same file *and* the two
    spellings the platform folds together (``C:\\A\\x.png`` /
    ``c:\\a\\x.png``), so one file can never hold two cache entries.

    Key schema: this module is the single construction site, so unmigrated
    code cannot mint an old-format (``normpath``-only) key; a key left over
    from before the ``normcase`` step is a *different* string wherever
    ``normcase`` is not the identity, i.e. exactly where the old key could
    have aliased a second spelling (see
    ``tests/runtime/test_path_key_normalisation.py``). Nothing here is
    persisted — both tab caches and the reducers' tiers are process-local —
    so there is no on-disk entry set to migrate.

    The key intentionally carries no crop/box component (W1+W2
    non-destructive crop: decode is full-frame, CropService is detection
    only) so every reader of the same file converges on one entry.
    """
    try:
        norm = fs_path_key(path)
    except Exception:
        return (str(path), 0, 0)
    try:
        st = os.stat(norm)
        return (
            norm,
            int(getattr(st, "st_mtime_ns", 0) or 0),
            int(getattr(st, "st_size", 0) or 0),
        )
    except OSError:
        return (norm, 0, 0)


def _is_usable_preview(qimage: Any) -> bool:
    try:
        return qimage is not None and not qimage.isNull()
    except Exception:
        return False


def _is_usable_pixel(store: Any) -> bool:
    is_open = getattr(store, "is_open", None)
    try:
        if callable(is_open):
            return bool(is_open())
        if is_open is not None:
            return bool(is_open)
    except Exception:
        return False
    return store is not None


def _preview_decode(path: Path | str, target_long_edge: int | None):
    """``load_preview_image`` at the requested tier (1024 when ``None``)."""
    from shared.image_processing.progressive_loader import load_preview_image

    if target_long_edge is None:
        return load_preview_image(str(path))
    return load_preview_image(str(path), target_long_edge=target_long_edge)


def _preview_bucket(target_long_edge: int | None) -> int | None:
    """Ladder bucket for a requested long edge (``None`` stays the default tier)."""
    if target_long_edge is None:
        return None
    try:
        from tabs.image_compare.pipeline.cache import preview_bucket_for

        return preview_bucket_for(target_long_edge)
    except Exception:
        return None


def _peek_cached_preview(cache: Any, path: Path | str, bucket: int | None):
    """Cached preview at ``bucket`` (or the best tier when ``None``); never raises.

    Duck-types the cache: an object whose ``get_preview`` predates the bucket
    keyword keeps working through the positional fallback.
    """
    if cache is None:
        return None
    try:
        if bucket is None:
            return cache.get_preview(str(path))
        return cache.get_preview(str(path), bucket=bucket)
    except TypeError:
        try:
            return cache.get_preview(str(path))
        except Exception:
            return None
    except Exception:
        return None


def get_or_load_preview(
    cache: Any, path: Path | str, *, target_long_edge: int | None = None
) -> QImage | None:
    """Sync preview demand fill: cached tier, else bounded decode.

    Delegates decode to ``load_preview_image`` (never re-implemented here).
    ``target_long_edge`` selects the preview tier (``pipeline.cache.
    PREVIEW_BUCKETS``, normally read off the live canvas); the decode happens
    AT that size and the entry is filed under the same bucket. ``None`` keeps
    the historic 1024 tier and every existing caller unchanged.
    Fail-open: any decode/cache failure returns ``None`` (never raises).
    """
    bucket = _preview_bucket(target_long_edge)
    if cache is not None:
        hit = _peek_cached_preview(cache, path, bucket)
        if _is_usable_preview(hit):
            return hit
    try:
        preview = _preview_decode(path, bucket)
    except Exception:
        logger.debug("shared loading seam: preview decode failed for %s", path)
        return None
    if not _is_usable_preview(preview):
        return None
    if cache is not None:
        try:
            if bucket is None:
                cache.put_preview(str(path), preview)
            else:
                try:
                    cache.put_preview(str(path), preview, bucket=bucket)
                except TypeError:
                    cache.put_preview(str(path), preview)
        except Exception:
            pass
    return preview


def get_or_load_pixel(
    cache: Any, path: Path | str, *, crop_service: Any = None
) -> TiledPixelStore | None:
    """Sync full-res demand fill: cached tier, else ``load_pixel_store``.

    Delegates decode to ``pixel_cache_loader.load_pixel_store`` (never
    re-implemented here). Fail-open: any decode/cache failure returns
    ``None`` (never raises).
    """
    if cache is not None:
        try:
            hit = cache.get_pixel(str(path))
        except Exception:
            hit = None
        if _is_usable_pixel(hit):
            return hit
    try:
        from shared.image_processing.pixel_cache_loader import load_pixel_store

        store = load_pixel_store(str(path), crop_service=crop_service)
    except Exception:
        logger.debug("shared loading seam: pixel decode failed for %s", path)
        return None
    if not _is_usable_pixel(store):
        return None
    if cache is not None:
        try:
            cache.put_pixel(str(path), store)
        except Exception:
            pass
    return store


# --- async staging (preview-first single-flight + bounded full FIFO) ---

# key -> {"signal": AbortSignal, "waiters": [(slot_id, path, on_ready, on_error)]}
_preview_inflight: dict[tuple, dict] = {}
# slot_id -> {"signal": AbortSignal, "path": ..., "on_ready": ..., "on_error": ...}
_full_active: dict[int, dict] = {}
# [(slot_id, path, on_ready, on_error)] waiting for a full-stage bound slot
_full_queue: list[tuple] = []


def _global_thread_pool() -> Any | None:
    try:
        from PySide6.QtCore import QThreadPool

        pool = QThreadPool.globalInstance()
        if pool is not None:
            return pool
    except Exception:
        pass
    return None


def _deliver_full_result(slot_id: int, path: Any, store: Any, sig_ref: AbortSignal) -> None:
    cur = _full_active.get(slot_id)
    if cur is not None and cur.get("signal") is not sig_ref:
        # Superseded: a newer generation owns this slot — stay silent.
        _pump_full_stage()
        return
    _full_active.pop(slot_id, None)
    entry = cur or {}
    on_ready = entry.get("on_ready")
    if store is None:
        on_error = entry.get("on_error")
        if callable(on_error):
            try:
                on_error(slot_id, path, RuntimeError("full decode returned no store"))
            except Exception:
                pass
    elif callable(on_ready):
        try:
            on_ready(slot_id, path, store)
        except Exception:
            logger.exception("shared loading seam: on_ready failed for slot %s", slot_id)
    _pump_full_stage()


def _deliver_full_error(slot_id: int, path: Any, err: Any, sig_ref: AbortSignal) -> None:
    cur = _full_active.get(slot_id)
    if cur is not None and cur.get("signal") is not sig_ref:
        _pump_full_stage()
        return
    _full_active.pop(slot_id, None)
    on_error = (cur or {}).get("on_error")
    if callable(on_error):
        try:
            on_error(slot_id, path, err)
        except Exception:
            pass
    _pump_full_stage()


def _cap_full_queue() -> None:
    overflow = len(_full_queue) - _FULL_QUEUE_MAX
    if overflow <= 0:
        return
    dropped = _full_queue[:overflow]
    del _full_queue[:overflow]
    try:
        # loading_pipeline's original log shows only the bare name
        # list (no ``(+N more)`` tail): keep extra=False for byte parity.
        names = summarize_overflow(dropped, limit=5, extra=False)
    except Exception:
        names = ""
    logger.warning(
        "shared loading seam: full-res queue cap (%d): shed %d oldest — %s",
        _FULL_QUEUE_MAX,
        len(dropped),
        names,
    )


def _pump_full_stage() -> None:
    """Start queued full-res workers while under the concurrency bound."""
    pool = _global_thread_pool()
    if pool is None:
        # Headless/tests without a pool: drain inline (sync shape).
        while _full_queue:
            sid, p, on_ready, on_error = _full_queue.pop(0)
            try:
                from shared.image_processing.pixel_cache_loader import load_pixel_store

                store = load_pixel_store(str(p))
            except Exception as err:
                if callable(on_error):
                    try:
                        on_error(sid, p, err)
                    except Exception:
                        pass
                continue
            if callable(on_ready):
                try:
                    on_ready(sid, p, store)
                except Exception:
                    logger.exception("shared loading seam: on_ready failed for slot %s", sid)
        return

    from sli_ui_toolkit.workers import GenericWorker

    while _full_queue and len(_full_active) < _FULL_MAX_CONCURRENT:
        sid, p, on_ready, on_error = _full_queue.pop(0)
        if sid in _full_active:
            continue  # identical decode already in flight for this slot
        sig = AbortSignal()
        _full_active[sid] = {"signal": sig, "path": p, "on_ready": on_ready, "on_error": on_error}

        def load_full_task(path_str: str, sig_ref: AbortSignal = sig):
            try:
                if sig_ref.is_aborted():
                    return None
            except Exception:
                pass
            from shared.image_processing.pixel_cache_loader import load_pixel_store

            store = load_pixel_store(path_str)
            try:
                if sig_ref.is_aborted():
                    return None
            except Exception:
                pass
            return store

        worker = GenericWorker(load_full_task, str(p))
        worker.signals.result.connect(
            lambda store, sid=sid, p=p, sig_ref=sig: _deliver_full_result(sid, p, store, sig_ref)
        )
        worker.signals.error.connect(
            lambda err, sid=sid, p=p, sig_ref=sig: _deliver_full_error(sid, p, err, sig_ref)
        )

        def _finished(sid: int = sid, sig_ref: AbortSignal = sig):
            try:
                cur = _full_active.get(sid)
                if cur is not None and cur.get("signal") is sig_ref:
                    _full_active.pop(sid, None)
            except Exception:
                pass
            _pump_full_stage()

        try:
            worker.signals.finished.connect(_finished)
        except Exception:
            pass
        try:
            pool.start(worker)
        except Exception:
            logger.exception("shared loading seam: failed to start full-res worker for %s", p)
            try:
                _full_active.pop(sid, None)
            except Exception:
                pass
            _deliver_full_error(sid, p, RuntimeError("full-res worker failed to start"), sig)


def queue_full_resolution(
    path: Path | str,
    slot_id: int,
    on_ready: Callable | None = None,
    on_error: Callable | None = None,
) -> None:
    """Enqueue the bounded full-res second stage for ``(path, slot_id)``."""
    try:
        _full_queue[:] = [e for e in _full_queue if e[0] != slot_id]
        _full_queue.append((slot_id, path, on_ready, on_error))
    except Exception:
        logger.exception("shared loading seam: failed to queue full-res load for %s", path)
        return
    _cap_full_queue()
    _pump_full_stage()


def _deliver_preview_result(key: tuple, preview: Any) -> None:
    ent = _preview_inflight.pop(key, None)
    if ent is None:
        return
    try:
        aborted = ent["signal"].is_aborted()
    except Exception:
        aborted = False
    waiters = list(ent.get("waiters", ()))
    if aborted:
        return  # orphan delivery: cancelled — stay silent
    if not _is_usable_preview(preview):
        # Preview-miss: each waiter goes through the bounded full stage.
        for sid, p, on_ready, on_error in waiters:
            try:
                queue_full_resolution(p, sid, on_ready, on_error)
            except Exception:
                logger.exception("shared loading seam: failed to kick full-res stage for %s", p)
        return
    for sid, p, on_ready, on_error in waiters:
        if callable(on_ready):
            try:
                on_ready(sid, p, preview)
            except Exception:
                logger.exception("shared loading seam: on_ready failed for slot %s", sid)
        try:
            queue_full_resolution(p, sid, on_ready, on_error)
        except Exception:
            logger.exception("shared loading seam: failed to kick full-res stage for %s", p)


def _deliver_preview_error(key: tuple, err: Any) -> None:
    ent = _preview_inflight.pop(key, None)
    if ent is None:
        return
    try:
        aborted = ent["signal"].is_aborted()
    except Exception:
        aborted = False
    waiters = list(ent.get("waiters", ()))
    if aborted:
        return
    for sid, p, on_ready, on_error in waiters:
        if callable(on_error):
            try:
                on_error(sid, p, err)
            except Exception:
                pass
        else:
            # Never silent: no error hook means fall through to the full
            # stage rather than dropping the waiter.
            try:
                queue_full_resolution(p, sid, on_ready, on_error)
            except Exception:
                pass


def ensure_async(
    cache: Any,
    path: Path | str,
    slot_id: int,
    on_ready: Callable | None = None,
    on_error: Callable | None = None,
    *,
    target_long_edge: int | None = None,
) -> AbortSignal | None:
    """Preview-first async load with single-flight on the boxless key.

    Fast path: a cached tier (pixel, then preview) delivers synchronously
    via ``on_ready`` and returns ``None`` (nothing in flight). Otherwise the
    caller joins the single-flight preview worker for
    :func:`cache_key_for_path`; its result fans out to all waiters and each
    waiter continues into the bounded full-res FIFO second stage. Dispatch
    stays in callers — this seam only invokes the hooks, never the Store.

    ``target_long_edge`` is the preview tier (``pipeline.cache.
    PREVIEW_BUCKETS``, read off the live canvas by the caller): the preview
    worker decodes AT that long edge and files the result under the same
    bucket, so the tier the canvas needs is the tier the cache holds. ``None``
    keeps the historic 1024 tier and every existing caller unchanged.

    Returns the in-flight :class:`AbortSignal` for the key (``None`` on
    sync cache-hit delivery). Fail-open: hook exceptions are logged, never
    raised.
    """
    bucket = _preview_bucket(target_long_edge)
    # Fast path: cached tiers deliver synchronously (pixel first, preview).
    if cache is not None:
        for getter in ("get_pixel", "get_preview"):
            try:
                if getter == "get_preview" and bucket is not None:
                    hit = _peek_cached_preview(cache, path, bucket)
                else:
                    hit = getattr(cache, getter)(str(path))
            except Exception:
                hit = None
            usable = _is_usable_pixel(hit) if getter == "get_pixel" else _is_usable_preview(hit)
            if usable:
                if callable(on_ready):
                    try:
                        on_ready(slot_id, path, hit)
                    except Exception:
                        logger.exception(
                            "shared loading seam: on_ready failed for slot %s", slot_id
                        )
                return None

    key = cache_key_for_path(path)
    ent = _preview_inflight.get(key)
    if ent is not None:
        try:
            aborted = ent["signal"].is_aborted()
        except Exception:
            aborted = True
        if not aborted:
            ent["waiters"].append((slot_id, path, on_ready, on_error))
            logger.debug(
                "[shared-loading-seam] dedup %s key=%s waiters=%d",
                path,
                key,
                len(ent["waiters"]),
            )
            return ent["signal"]
        _preview_inflight.pop(key, None)

    sig = AbortSignal()
    _preview_inflight[key] = {"signal": sig, "waiters": [(slot_id, path, on_ready, on_error)]}

    pool = _global_thread_pool()
    if pool is None:
        # Headless/tests without a pool: sync preview, then full inline.
        _preview_inflight.pop(key, None)
        preview = get_or_load_preview(cache, path, target_long_edge=target_long_edge)
        if _is_usable_preview(preview):
            if callable(on_ready):
                try:
                    on_ready(slot_id, path, preview)
                except Exception:
                    logger.exception("shared loading seam: on_ready failed for slot %s", slot_id)
            queue_full_resolution(path, slot_id, on_ready, on_error)
        else:
            queue_full_resolution(path, slot_id, on_ready, on_error)
        return sig

    from sli_ui_toolkit.workers import GenericWorker

    def preview_task(path_str: str, sig_ref: AbortSignal = sig):
        # GenericWorker emits ``result`` only for non-None returns, so this
        # task ALWAYS returns a ``(preview_or_None,)`` tuple — a bare None
        # (miss/abort) would be swallowed and waiters would never fan out.
        try:
            if sig_ref.is_aborted():
                return (None,)
        except Exception:
            pass
        try:
            preview = _preview_decode(path_str, bucket)
        except Exception:
            logger.debug("shared loading seam: preview worker failed for %s", path_str)
            return (None,)
        try:
            if sig_ref.is_aborted():
                return (None,)
        except Exception:
            pass
        if _is_usable_preview(preview):
            try:
                if cache is not None:
                    try:
                        if bucket is None:
                            cache.put_preview(path_str, preview)
                        else:
                            cache.put_preview(path_str, preview, bucket=bucket)
                    except TypeError:
                        cache.put_preview(path_str, None, preview)
            except Exception:
                pass
            return (preview,)
        return (None,)

    worker = GenericWorker(preview_task, str(path))
    worker.signals.result.connect(lambda res: _deliver_preview_result(key, res[0]))
    worker.signals.error.connect(lambda err: _deliver_preview_error(key, err))

    def _cleanup(key: tuple = key, sig_ref: AbortSignal = sig):
        try:
            cur = _preview_inflight.get(key)
            if cur is not None and cur.get("signal") is sig_ref:
                _preview_inflight.pop(key, None)
        except Exception:
            pass

    try:
        worker.signals.finished.connect(_cleanup)
    except Exception:
        pass
    try:
        pool.start(worker)
    except Exception:
        logger.exception("shared loading seam: failed to start preview worker for %s", path)
        _preview_inflight.pop(key, None)
        _deliver_preview_error(key, RuntimeError("preview worker failed to start"))
    return sig


def start_pyramid_build(store: Any, slot_id: int, toast_hook: Any = None) -> None:
    """Kick a pyramid build for ``store`` via the shared coordinator hook.

    ``toast_hook`` is either a ``PyramidBuildCoordinator`` (its
    ``start_build`` is used — the ``engine.pyramid.coordinator`` delegate, never
    re-implemented here) or a plain ``(store, slot_id)`` callable. ``None``
    is a fail-open no-op (callers without a coordinator keep today's
    behavior of no pyramid). Never raises.
    """
    if store is None or toast_hook is None:
        return
    try:
        start = getattr(toast_hook, "start_build", None)
        if callable(start):
            try:
                start(store, slot_id=slot_id)
            except TypeError:
                start(store, slot_id)
            return
        if callable(toast_hook):
            toast_hook(store, slot_id)
    except Exception:
        logger.debug("shared loading seam: pyramid build kick failed", exc_info=True)
