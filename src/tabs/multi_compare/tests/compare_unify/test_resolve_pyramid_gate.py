"""MC resolve holds preview until the pixel tier's pyramid completes.

Regression for the early-replace seen in the 2026-09-30 log: full noted at
trace seq 73 while its pyramid built frames later, so the raw store replaced
the preview before any tile was resident. IC parity (pick_display_image):
pixel tier wins only when pyramid-gated ready.
"""

from __future__ import annotations

import pytest

from engine.store.tiled_pixel_store import TiledPixelStore
from tabs.multi_compare.pipeline import cache as mc_cache

class _Store(TiledPixelStore):
    """TiledPixelStore identity without a real memmap.

    ``is_open`` is declared (not inherited): ``TiledPixelStore.is_open`` reads
    the real mapping, and a double that skips ``__init__`` has none — it must
    stand for an OPEN store here, or the cache's usability gate drops it (the
    sibling ``_ClosedStore`` below is the explicit opposite).
    """

    is_open = True

    def __init__(self) -> None:
        pass


class _Pyramid:
    def __init__(self, complete: bool) -> None:
        self._complete = complete

    def is_complete(self) -> bool:
        return self._complete


class _SizedStore(TiledPixelStore):
    """Store with a fixed frame size (preview is 64x48, so tier flips show)."""

    width = 800
    height = 600
    is_open = True

    def __init__(self) -> None:
        pass


class _ClosedStore(TiledPixelStore):
    """Store the cache treats as closed (evicted out from under a frame)."""

    is_open = False

    def __init__(self) -> None:
        pass


def _preview():
    from PySide6.QtGui import QImage

    img = QImage(64, 48, QImage.Format.Format_RGBA8888)
    img.fill(0xFF808080)
    assert not img.isNull()
    return img


def _cache_with(store, preview, monkeypatch, *, pyramid=None):
    from pathlib import Path

    c = mc_cache.MultiComparePixelCache()
    path = str(Path("/tmp/mc-gate-probe.png"))
    c.put_pixel(path, store)
    if preview is not None:
        c.put_preview(path, preview)
    monkeypatch.setattr(mc_cache, "pyramid_for", lambda _s: pyramid, raising=False)
    # _pyramid_ready imports pyramid_for lazily; patch the source module too.
    import engine.store.pyramid_registry as _reg

    monkeypatch.setattr(_reg, "pyramid_for", lambda _s: pyramid, raising=False)
    return c, path


def test_incomplete_pyramid_holds_preview(monkeypatch):
    c, path = _cache_with(_Store(), _preview(), monkeypatch, pyramid=_Pyramid(False))
    assert c.resolve(path) is not None
    assert c.resolve(path) == c.get_preview(path)


def test_complete_pyramid_returns_store(monkeypatch):
    c, path = _cache_with(_Store(), _preview(), monkeypatch, pyramid=_Pyramid(True))
    assert c.resolve(path) is c.get_pixel(path)


def test_no_preview_fail_open_returns_store(monkeypatch):
    c, path = _cache_with(_Store(), None, monkeypatch, pyramid=_Pyramid(False))
    assert c.resolve(path) is c.get_pixel(path)


def test_untracked_store_holds_preview(monkeypatch):
    c, path = _cache_with(_Store(), _preview(), monkeypatch, pyramid=None)
    assert c.resolve(path) == c.get_preview(path)


def test_non_store_pixel_unchanged(monkeypatch):
    c, path = _cache_with(object(), _preview(), monkeypatch, pyramid=_Pyramid(False))
    assert c.resolve(path) is c.get_pixel(path)


def test_flip_on_complete_same_cache_instance(monkeypatch):
    """Incomplete→preview, then complete→store on the SAME cache, no re-put.

    Proves the canvas flip needs no new publishes: ``_slot_sources`` /
    ``_sync_textures`` re-resolve every frame, and the ``is not source``
    identity check re-uploads once the gate opens.
    """
    pyramid = _Pyramid(False)
    c, path = _cache_with(_Store(), _preview(), monkeypatch, pyramid=pyramid)
    first = c.resolve(path)
    assert first == c.get_preview(path)
    pyramid._complete = True
    second = c.resolve(path)
    assert second is c.get_pixel(path)
    assert second is not first


def test_source_size_follows_gated_tier(monkeypatch):
    """``source_size`` tracks the gated tier (preview 64x48, store 800x600)."""
    pyramid = _Pyramid(False)
    c, path = _cache_with(_SizedStore(), _preview(), monkeypatch, pyramid=pyramid)
    assert c.source_size(path) == (64, 48)
    pyramid._complete = True
    assert c.source_size(path) == (800, 600)


def test_evicted_store_reads_imageless(monkeypatch):
    c, path = _cache_with(_SizedStore(), _preview(), monkeypatch, pyramid=_Pyramid(True))
    assert c.resolve(path) is c.get_pixel(path)
    c.evict(path)
    assert c.resolve(path) is None
    assert c.source_size(path) is None


def test_closed_store_reads_imageless(monkeypatch):
    c, path = _cache_with(_ClosedStore(), None, monkeypatch, pyramid=_Pyramid(True))
    assert c.get_pixel(path) is None
    assert c.resolve(path) is None
    assert c.source_size(path) is None


def test_closed_store_falls_back_to_preview(monkeypatch):
    c, path = _cache_with(_ClosedStore(), _preview(), monkeypatch, pyramid=_Pyramid(True))
    assert c.resolve(path) == c.get_preview(path)


def test_tierless_path_resolves_imageless():
    c = mc_cache.MultiComparePixelCache()
    assert c.resolve(None) is None
    assert c.source_size(None) is None
