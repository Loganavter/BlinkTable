"""Preview tiers follow the canvas: bucketed keys + decode-at-size.

The preview is what the canvas paints for the frame(s) before the full-res
store lands, so a fixed 1024 tier upscaled to a bigger canvas IS the soft
frame the owner reported on a pair switch (2026-11-18). Previews are therefore
tiered (``PREVIEW_BUCKETS``) and decoded AT the target size — libvips
``thumbnail`` shrink-on-load / PIL ``thumbnail`` — never decoded full-frame and
downscaled (Qt ``QImageReader.setScaledSize`` semantics).

Guarded here:
* the bucket ladder (None/garbage/<=0 -> 1024, never above 4096);
* the key keeps its historic shape, so an unscaled caller is byte-identical;
* two buckets of one file are different entries (a pick must not read the
  stale tier);
* the decode honours the target and never upscales a smaller source.
"""

from __future__ import annotations

import os

from PIL import Image
from PySide6.QtGui import QImage

from shared.fs_paths import fs_path_key
from shared.image_processing.progressive_loader import load_preview_image
from tabs.image_compare.pipeline.cache import (
    PREVIEW_BUCKETS,
    PipelineCache,
    _preview_key,
    preview_bucket_for,
)


# -- bucket ladder --


def test_bucket_ladder_is_1024_2048_4096():
    assert PREVIEW_BUCKETS == (1024, 2048, 4096)


def test_bucket_for_none_and_garbage_is_1024():
    assert preview_bucket_for(None) == 1024
    assert preview_bucket_for("nonsense") == 1024
    assert preview_bucket_for("") == 1024
    assert preview_bucket_for(object()) == 1024


def test_bucket_for_non_positive_is_1024():
    assert preview_bucket_for(0) == 1024
    assert preview_bucket_for(-1) == 1024
    assert preview_bucket_for(-5000) == 1024


def test_bucket_for_small_edges_snap_up_to_the_ladder():
    assert preview_bucket_for(1) == 1024
    assert preview_bucket_for(800) == 1024
    assert preview_bucket_for(1024) == 1024
    assert preview_bucket_for(1025) == 2048
    assert preview_bucket_for(2048) == 2048
    assert preview_bucket_for(2049) == 4096
    assert preview_bucket_for(4096) == 4096


def test_bucket_for_never_above_the_largest_bucket():
    assert preview_bucket_for(5000) == 4096
    assert preview_bucket_for(10**9) == PREVIEW_BUCKETS[-1]


# -- keys --


def test_preview_key_default_is_byte_identical_to_the_historic_shape(tmp_path):
    f = tmp_path / "a.png"
    Image.new("RGBA", (16, 16), (1, 2, 3, 255)).save(f)
    st = os.stat(str(f))
    expected = (fs_path_key(str(f)), st.st_mtime_ns, st.st_size, False, 1024)
    assert _preview_key(str(f)) == expected
    assert _preview_key(str(f), None, None) == expected


def test_preview_key_carries_the_bucket_and_tiers_do_not_collide(tmp_path):
    f = tmp_path / "a.png"
    Image.new("RGBA", (16, 16), (1, 2, 3, 255)).save(f)
    base = _preview_key(str(f))
    big = _preview_key(str(f), None, None, bucket=2048)
    huge = _preview_key(str(f), None, None, bucket=4096)
    assert base[4] == 1024 and big[4] == 2048 and huge[4] == 4096
    assert base[:4] == big[:4] == huge[:4], "file identity prefix unchanged"
    assert base != big and big != huge, "two buckets are two entries"
    # Off-ladder values land on the ladder instead of minting a key per pixel.
    assert _preview_key(str(f), None, None, bucket=1500)[4] == 2048


def test_cache_keeps_tiers_apart_and_resolves_path_only_lookups(tmp_path):
    """Put at one bucket -> read at another must not serve the wrong tier."""
    f = tmp_path / "a.png"
    Image.new("RGBA", (16, 16), (1, 2, 3, 255)).save(f)
    cache = PipelineCache()
    small = QImage(1024, 683, QImage.Format_RGBA8888)
    large = QImage(2048, 1365, QImage.Format_RGBA8888)
    cache.put_preview(str(f), small)  # no bucket -> payload's own long edge
    assert cache.get_preview(str(f), bucket=1024) is small
    assert cache.get_preview(str(f), bucket=2048) is None, "no silent tier mix"
    cache.put_preview(str(f), large, bucket=2048)
    assert cache.get_preview(str(f), bucket=1024) is small
    assert cache.get_preview(str(f), bucket=2048) is large
    # Path-only readers (the picker, presenter tracking) take the best tier.
    assert cache.get_preview(str(f)) is large


# -- decode at the target size --


def _png(tmp_path, name, width, height):
    path = tmp_path / name
    Image.new("RGBA", (width, height), (10, 20, 30, 255)).save(path)
    return path


def test_load_preview_at_2048_keeps_aspect_and_hits_the_tier(tmp_path):
    path = _png(tmp_path, "big.png", 2048, 1365)
    preview = load_preview_image(str(path), target_long_edge=2048)
    assert isinstance(preview, QImage)
    assert not preview.isNull()
    assert max(preview.width(), preview.height()) == 2048
    assert preview.width() == 2048 and abs(preview.height() - 1365) <= 1
    assert abs(preview.width() / preview.height() - 2048 / 1365) < 0.01


def test_load_preview_at_1024_is_the_historic_tier(tmp_path):
    path = _png(tmp_path, "big.png", 2048, 1365)
    explicit = load_preview_image(str(path), target_long_edge=1024)
    default = load_preview_image(str(path))
    assert explicit is not None and default is not None
    assert max(explicit.width(), explicit.height()) == 1024
    assert max(default.width(), default.height()) == 1024
    assert (explicit.width(), explicit.height()) == (default.width(), default.height())


def test_load_preview_never_upscales_a_smaller_source(tmp_path):
    path = _png(tmp_path, "small.png", 300, 200)
    preview = load_preview_image(str(path), target_long_edge=2048)
    assert preview is not None
    assert max(preview.width(), preview.height()) == 300


def test_load_preview_garbage_target_falls_back_to_1024(tmp_path):
    path = _png(tmp_path, "big.png", 1600, 900)
    for bad in (0, -3, "junk"):
        preview = load_preview_image(str(path), target_long_edge=bad)  # type: ignore[arg-type]
        assert preview is not None
        assert max(preview.width(), preview.height()) == 1024
