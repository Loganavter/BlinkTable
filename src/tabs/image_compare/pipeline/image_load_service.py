"""ImageLoadService — single-flight loader (phase 3 bucket D).

Single-flight via ``path+mtime → AbortSignal`` (ключ всегда boxless, см.
``pipeline/cache._pixel_key``: W1+W2 non-destructive crop — декод
full-frame, сервис только детекция). Заменяет:

* ``slot._inflight[(slot,path)]`` (slot.py:475)
* ``image_decode._inflight[(slot,path,"full")]`` (image_decode.py:272)
* ``unify (p1,p2)`` (src/tabs/image_compare/use_cases/unify.py:270)
* ``pyramid single-flight`` (session.py / pyramid.py)

Key ``(normpath, mtime_ns, size, False)`` (file-identity prefix owned by
``tabs._shared.loading_pipeline.cache_key_for_path``). Preview-first async
staging is delegated to the shared seam (``ensure_async``); this service
keeps the tab-local surface (``key_for``/``try_acquire``/``release``/
``is_loading``/``ensure_async``) and all dispatch (transacts in hooks).
GUI не блокируется: декод идёт в воркерах сима, detection-прогрев
CropService покрывает schedule_crop_warmup в slot.load_images_from_paths.
"""

from __future__ import annotations

import os
import logging
from typing import Any, Callable

from tabs.image_compare.pipeline.abort import AbortSignal
from tabs.image_compare.debug import ic_preview_debug
from core.log_names import APP_LOGGER_NAME
# W1 adapter: single-flight key + preview-first async staging owned by the
# shared seam; dispatch (transacts) stays tab-local in the hooks below.
# NOTE: seam imports are function-level — the seam imports
# tabs.image_compare.pipeline.abort, which runs this package's __init__
# first, so a top-level seam import here would be circular.

logger = logging.getLogger(APP_LOGGER_NAME)


def _key_for_path(path: str, crop_service=None, **_kw) -> tuple:
    """Ключ всегда boxless: (normpath,mtime,size,False). Сервис игнорируется.

    W1+W2: single-flight dedup обязан сходиться для всех читателей
    (``ensure_async`` кладёт с None, ``bg_dirty.is_loading`` спрашивает с
    живым сервисом) — поэтому has_crop всегда False, как раньше при
    выключенном кропе. Старые True-ключи миссуют и не коллизируют.

    W1 adapter: file-identity prefix delegated to the shared seam
    (``tabs._shared.loading_pipeline.cache_key_for_path``); the boxless
    suffix stays tab-local so stored shapes never change.
    """
    from tabs._shared.loading_pipeline import cache_key_for_path as _seam_key_for_path

    norm, mtime, size = _seam_key_for_path(path)
    return (norm, int(mtime), int(size), False)


def _unify_key(path1: str, path2: str, method: str) -> tuple:
    """Unify single-flight key — path+method (store also keyed by uid later)."""
    return (os.path.normpath(str(path1)), os.path.normpath(str(path2)), str(method))


def _pipeline_inflight(controller: Any) -> dict | None:
    """Return controller.pipeline._inflight (may alias the service dict)."""
    try:
        pl = getattr(controller, "pipeline", None)
        d = getattr(pl, "_inflight", None)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


class ImageLoadService:
    """Single-flight loader with path+mtime+box dedup and progressive inside."""

    def __init__(
        self,
        cache: Any | None = None,
        get_crop_service: Callable[[], Any | None] | None = None,
        get_store: Callable[[], Any | None] | None = None,
        get_thread_pool: Callable[[], Any | None] | None = None,
    ) -> None:
        self._cache = cache
        self._get_crop_service = get_crop_service
        self._get_store = get_store
        self._get_thread_pool = get_thread_pool
        self._inflight: dict[tuple, AbortSignal] = {}

    def key_for(self, path: str, crop_service: Any | None = None) -> tuple:
        # W1+W2: single-flight ключи boxless — сессионный detection-сервис
        # здесь не резолвится (и никогда не дёргается crop_service.get:
        # off-GUI правило). Аргумент принят для совместимости и игнорируется.
        return _key_for_path(path, crop_service)

    def unify_key_for(self, path1: str, path2: str, method: str) -> tuple:
        return _unify_key(path1, path2, method)

    def is_loading(self, path: str, crop_service: Any | None = None) -> bool:
        key = self.key_for(path, crop_service)
        sig = self._inflight.get(key)
        if sig is None:
            return False
        try:
            return not sig.is_aborted()
        except Exception:
            return True

    def try_acquire(self, key: tuple) -> AbortSignal | None:
        existing = self._inflight.get(key)
        if existing is not None:
            try:
                if not existing.is_aborted():
                    return None
            except Exception:
                return None
            self._inflight.pop(key, None)
        sig = AbortSignal()
        self._inflight[key] = sig
        return sig

    def release(self, key: tuple, signal: AbortSignal | None) -> None:
        try:
            cur = self._inflight.get(key)
            if signal is None or cur is signal:
                self._inflight.pop(key, None)
        except Exception:
            pass

    def try_acquire_load(self, path: str, crop_service: Any | None = None) -> tuple[tuple, AbortSignal] | None:
        key = self.key_for(path, crop_service)
        sig = self.try_acquire(key)
        if sig is None:
            return None
        return (key, sig)

    def ensure_async(
        self,
        path: str,
        slot: int,
        index_in_list: int,
        controller: Any,
    ) -> AbortSignal | None:
        # W1 adapter: preview-first single-flight staging delegated to the
        # shared seam (tabs._shared.loading_pipeline.ensure_async). Dispatch
        # stays tab-local: the seam only invokes _on_ready/_on_error below,
        # which transact/forward exactly like the previous worker body did
        # (same toasts, same ordering, same abort semantics). The sync
        # cache-hit fast path stays here so a hit still transacts inline and
        # returns None (nothing in flight), as before.
        # W1+W2: декод и ключи — всегда boxless (crop_service=None, no-bake).
        # Сессионный сервис — только детекция; его прогрев в фоне покрывает
        # schedule_crop_warmup в slot.load_images_from_paths, дубль прогрева
        # внутри воркера упразднён вместе с собственным GenericWorker.
        cache = self._cache
        try:
            sess = None
            if hasattr(controller, "_get_image_session"):
                try:
                    sess = controller._get_image_session()
                    if sess is not None and hasattr(sess, "cache"):
                        cache = sess.cache
                except Exception:
                    pass
            if cache is None:
                cache = getattr(getattr(controller, "pipeline", None), "cache", None)
        except Exception:
            pass
        key = self.key_for(path, None)
        existing = self._inflight.get(key)
        if existing is not None:
            try:
                if not existing.is_aborted():
                    ic_preview_debug("ImageLoadService dedup path=%s key=%s", path, key)
                    return existing
            except Exception:
                return existing
            self._inflight.pop(key, None)
        try:
            hit = None
            if cache is not None:
                try:
                    hit = cache.get_pixel(path, None)
                except Exception:
                    hit = None
                if hit is None:
                    try:
                        hit = cache.get_preview(path, None)  # type: ignore
                    except Exception:
                        hit = None
            if hit is not None:
                try:
                    from core.state_management.actions import InvalidateGeometryCacheAction, SetImageSessionImageAction
                    store = getattr(controller, "store", None)
                    if store is None and self._get_store is not None:
                        try:
                            store = self._get_store()
                        except Exception:
                            store = None
                    if store is not None and hasattr(store, "transact"):
                        store.transact(
                            [SetImageSessionImageAction(slot=int(slot), image=hit), InvalidateGeometryCacheAction()],
                            scope="viewport",
                        )
                        ic_preview_debug("ImageLoadService cache-hit transact slot=%s path=%s", slot, path)
                except Exception as e:
                    ic_preview_debug("ImageLoadService cache-hit transact failed %s", e)
                return None
        except Exception:
            pass
        ic_preview_debug("ImageLoadService start slot=%s path=%s key=%s", slot, path, key)

        # Tab-local pool wiring decides sync-vs-async (legacy parity): a
        # controller without a thread pool (fakes, pool-less contexts) takes
        # the inline path — fresh signal, nothing stored, no delivery. The
        # seam would otherwise use the global pool and start a real worker.
        pool = None
        try:
            if self._get_thread_pool is not None:
                pool = self._get_thread_pool()
            if pool is None:
                pool = getattr(controller, "thread_pool", None)
        except Exception:
            try:
                pool = getattr(controller, "thread_pool", None)
            except Exception:
                pool = None
        if pool is None:
            ic_preview_debug("ImageLoadService no thread_pool, inline")
            self._inflight.pop(key, None)
            return AbortSignal()

        cell: dict[str, Any] = {}
        delivered: list[str] = []

        def _pop_alias() -> None:
            # Pipeline alias (slot, path) set by the slot.py service path in
            # the shared _inflight dict. Pop only if the entry is still ours
            # (a newer load may have reused the key).
            try:
                alias = (int(slot), str(path))
            except Exception:
                return
            dicts = [self._inflight]
            pl_d = _pipeline_inflight(controller)
            if pl_d is not None and pl_d is not self._inflight:
                dicts.append(pl_d)
            for d in dicts:
                try:
                    if d.get(alias) is cell.get("sig"):
                        d.pop(alias, None)
                except Exception:
                    pass

        def _pop_service_key() -> None:
            try:
                cur = self._inflight.get(key)
                if cur is None or cur is cell.get("sig"):
                    self._inflight.pop(key, None)
            except Exception:
                pass

        def _on_ready(slot_id: Any, ready_path: Any, result: Any) -> None:
            delivered.append("ready")
            _pop_service_key()
            _pop_alias()
            try:
                from PySide6.QtGui import QImage as _QImage
            except Exception:
                _QImage = ()  # type: ignore[assignment]
            try:
                is_preview = isinstance(result, _QImage)
            except Exception:
                is_preview = False
            try:
                from tabs.image_compare.use_cases import image_decode as _decode
            except Exception:
                _decode = None  # type: ignore[assignment]
            try:
                if is_preview:
                    if result is not None and _decode is not None:
                        # Seam owns the full-res second stage — suppress the
                        # legacy explicit full kick (chain_full=False).
                        _decode.on_image_loaded(
                            controller,
                            (result, ready_path, int(slot_id), int(index_in_list), True),
                            chain_full=False,
                        )
                    elif _decode is not None:
                        _decode.on_image_loaded(
                            controller,
                            (None, ready_path, int(slot_id), int(index_in_list), False),
                        )
                elif result is not None:
                    try:
                        if cache is not None:
                            cache.put_pixel(ready_path, None, result)
                    except Exception:
                        pass
                    controller._on_full_image_loaded(
                        result, ready_path, int(slot_id), int(index_in_list)
                    )
                elif _decode is not None:
                    _decode.on_image_loaded(
                        controller,
                        (None, ready_path, int(slot_id), int(index_in_list), False),
                    )
            except Exception as e:
                ic_preview_debug("ImageLoadService _on_ready handler failed %s", e)
                logger.exception("ImageLoadService result handler failed")

        def _on_error(slot_id: Any, err_path: Any, err: Any) -> None:
            delivered.append("error")
            _pop_service_key()
            _pop_alias()
            # Fail-open like the legacy (None, ...) decode-miss tuple: the
            # pop/resync path in on_image_loaded owns user-visible handling.
            try:
                from tabs.image_compare.use_cases import image_decode as _decode
            except Exception:
                _decode = None  # type: ignore[assignment]
            try:
                if _decode is not None:
                    _decode.on_image_loaded(
                        controller,
                        (None, err_path, int(slot_id), int(index_in_list), False),
                    )
            except Exception as e:
                ic_preview_debug("ImageLoadService _on_error handler failed %s", e)

        from tabs._shared.loading_pipeline import ensure_async as _seam_ensure_async

        # Preview tier follows the live canvas (PREVIEW_BUCKETS); the seam
        # decodes AT that size and files it under the same bucket. Computed here
        # (GUI thread, the caller of the seam) so the preview worker itself only
        # ever sees an int. 1024 when there is no canvas.
        try:
            from tabs.image_compare.use_cases.image_decode import (
                canvas_preview_bucket as _canvas_preview_bucket,
            )

            target_edge = _canvas_preview_bucket(controller)
        except Exception:
            target_edge = None
        ic_preview_debug(
            "ImageLoadService preview tier slot=%s path=%s edge=%s", slot, path, target_edge
        )

        try:
            sig = _seam_ensure_async(
                cache,
                path,
                int(slot),
                _on_ready,
                _on_error,
                target_long_edge=target_edge,
            )
        except Exception as e:
            ic_preview_debug("ImageLoadService seam ensure_async failed %s", e)
            return None
        if sig is None:
            # Sync cache-hit delivery inside the seam (pre-check race): hooks
            # already ran; nothing in flight.
            return None
        cell["sig"] = sig
        if delivered:
            # Headless/tests inline shape: the seam delivered synchronously
            # but still returned a signal. Mirror the legacy no-pool path:
            # no service-key alias, caller still gets the signal.
            self._inflight.pop(key, None)
            return sig
        self._inflight[key] = sig
        return sig

    @property
    def inflight(self) -> dict[tuple, AbortSignal]:
        return self._inflight
