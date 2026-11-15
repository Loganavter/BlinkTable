"""A closed/racing pixel store must read as closed, never raise.

Owner log (2026-11-15, ``[FullImageLoad]`` on a DnD drop)::

    File ".../pyramid_registry.py", line 25, in sweep
        dead = [uid for uid, pyramid in _pyramids.items() if not pyramid.valid]
    File ".../pyramid_pixel_store.py", line 85, in valid
        return base.is_open and base.generation == self._base_generation
    File ".../tiled_pixel_store.py", line 747, in is_open
        return self._memmap is not None
    AttributeError: '_SharedView' object has no attribute '_memmap'

``release_spill_mapping`` removes ``_memmap`` from ``__dict__`` while it flushes
and closes the mapping, and the pyramid registry's sweep reads ``is_open`` from
another thread. The sweep runs on every ``ensure_pyramid`` call, so one hit
poisons the whole build: every kick raises, no pyramid level is ever built, and
the "loading full version of image" toast sits at its first progress value
forever (the owner's "toasts hang in building the pyramid").

Pinned here: the transient shape reads as *closed*, and ``release_spill_mapping``
leaves the attribute present so no other reader can meet the absent shape.
"""

from __future__ import annotations

from PIL import Image

from engine.store.pyramid_pixel_store import PyramidPixelStore
from engine.store.tiled_pixel_store import TiledPixelStore, release_spill_mapping


def _store(tmp_path) -> TiledPixelStore:
    image = Image.new("RGB", (32, 24), "red")
    return TiledPixelStore.from_pil(image, tmp_dir=str(tmp_path))


def test_is_open_reads_closed_when_the_attribute_is_absent(tmp_path):
    """The exact shape the sweep met mid-close: absent, not None."""
    store = _store(tmp_path)
    del store.__dict__["_memmap"]

    assert store.is_open is False


def test_release_leaves_the_closed_attribute_in_place(tmp_path):
    store = _store(tmp_path)

    release_spill_mapping(store)

    assert "_memmap" in store.__dict__, (
        "the pop must be re-published at once: a reader on another thread "
        "(the pyramid sweep) must never see the attribute absent"
    )
    assert store.is_open is False


def test_pyramid_valid_is_false_for_a_store_that_lost_its_mapping(tmp_path):
    """``PyramidPixelStore.valid`` must answer False, not raise."""
    base = _store(tmp_path)
    pyramid = PyramidPixelStore(base, tmp_dir=str(tmp_path))
    del base.__dict__["_memmap"]

    assert pyramid.valid is False
