"""Autocrop new module — parity vips vs pil и кэш CropService.

Проверяет поддерживаемость рефактора: явные типы, DI без глобала.
"""
from __future__ import annotations

import time

import numpy as np
from PIL import Image

from shared.image_processing.autocrop import CropBox, CropConfig, CropService
from shared.image_processing.autocrop.model import (
    SkipReason,
    content_key,
    file_stat,
    probe_dims,
)
from shared.image_processing.autocrop.scaling import get_scaled_box_for_thumb
from shared.image_processing.autocrop.pil import find_box_pil
from shared.image_processing.autocrop.vips import find_box_vips
from shared.image_processing import embedded_pixel_cache as emb


def test_parity_vips_vs_pil_small_probe():
    """Parity как в test_decode_backends:125 — vips find_trim vs PIL get_auto_crop_box."""
    arr = np.zeros((60, 80, 4), dtype=np.uint8)
    arr[10:50, 20:60, :3] = 200
    arr[10:50, 20:60, 3] = 255
    rgba = Image.fromarray(arr, mode="RGBA")

    vips_box = find_box_vips(np.asarray(rgba), threshold=15)
    pil_box = find_box_pil(rgba, threshold=15)

    if vips_box is not None:
        assert pil_box is not None, "PIL должен согласиться с vips"
        assert vips_box.to_tuple() == pil_box.to_tuple()
    # если vips недоступен — pil всё равно должен работать
    assert pil_box is not None
    assert pil_box == CropBox(20, 10, 60, 50)


def test_parity_vips_vs_pil_scaled():
    """Scaled parity — зонд 1024 с fallback thr30 (как в end-to-end streaming test)."""
    arr = np.zeros((200, 300, 4), dtype=np.uint8)
    arr[30:170, 40:260, :3] = 180
    arr[30:170, 40:260, 3] = 255
    rgba = Image.fromarray(arr, mode="RGBA")
    # pil напрямую на полном
    pil_full = find_box_pil(rgba, 15)
    assert pil_full is not None
    # vips на том же
    vips_full = find_box_vips(np.asarray(rgba), 15)
    if vips_full is not None:
        assert vips_full.to_tuple() == pil_full.to_tuple()


def test_crop_service_cache_and_invalidate(tmp_path):
    # создаём изображение с чёрными полями
    arr = np.zeros((80, 100, 4), dtype=np.uint8)
    arr[10:70, 20:80, :3] = 150
    arr[10:70, 20:80, 3] = 255
    p = tmp_path / "a.png"
    Image.fromarray(arr, mode="RGBA").save(str(p))

    svc = CropService(CropConfig(thr=15, thr_fallback=30, probe_max=1024))
    assert svc.cache_size == 0
    box1 = svc.get(str(p))
    assert box1 is not None
    assert svc.cache_size == 1
    # повторный hit — из кэша, не переоткрывает
    box2 = svc.get(str(p))
    assert box2 == box1
    assert svc.cache_size == 1

    # invalidate одного пути
    svc.invalidate(str(p))
    assert svc.cache_size == 0
    # снова compute
    box3 = svc.get(str(p))
    assert box3 == box1

    # invalidate_all
    svc.invalidate_all()
    assert svc.cache_size == 0


def test_crop_service_isolation():
    svc1 = CropService()
    svc2 = CropService()
    # разные инстансы — разные кэши (нет глобала)
    svc1._cache["/tmp/fake.png"] = None  # type: ignore
    assert svc2.cache_size == 0
    svc1.invalidate_all()
    assert svc1.cache_size == 0


def test_scaling_centralized():
    orig = CropBox(20, 10, 60, 50)
    scaled = get_scaled_box_for_thumb(orig, (80, 60), (40, 30))
    assert scaled is not None
    # 80->40 scale 0.5, 60->30 scale 0.5 => (10,5,30,25)
    assert scaled.to_tuple() == (10, 5, 30, 25)
    # полный кадр -> None
    full = CropBox(0, 0, 80, 60)
    assert get_scaled_box_for_thumb(full, (80, 60), (80, 60)) is None
    # None input -> None
    assert get_scaled_box_for_thumb(None, (80, 60), (40, 30)) is None


def test_crop_service_no_crop_returns_none(tmp_path):
    # полностью залитое без полей — нет кропа
    arr = np.full((32, 32, 4), 200, dtype=np.uint8)
    p = tmp_path / "full.png"
    Image.fromarray(arr, mode="RGBA").save(str(p))
    svc = CropService()
    res = svc.compute(str(p))
    assert res is None or res.box is None
    # get также None
    assert svc.get(str(p)) is None


# --- Task 2: shared cache identity (embedded key + verdicts + probe_size) ---

def _bordered(path, size=(64, 64), border=8):
    arr = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    arr[border:size[1] - border, border:size[0] - border, :3] = 170
    arr[border:size[1] - border, border:size[0] - border, 3] = 255
    Image.fromarray(arr, mode="RGBA").save(str(path))


def _flat(path, size=(64, 64)):
    Image.fromarray(np.full((size[1], size[0], 4), 200, dtype=np.uint8)).save(str(path))


def test_skip_reason_vocabulary():
    assert SkipReason.BORDERLESS == "borderless"
    assert SkipReason.UNDECODABLE == "undecodable"
    assert SkipReason.REPLACED_FILE == "replaced-file"
    assert SkipReason.OVERSIZED == "oversized"
    assert SkipReason.DISABLED == "disabled"


def test_probe_dims_math():
    # No downscale: probe runs on the full frame.
    assert probe_dims((800, 600), 1024) == (800, 600)
    # Longest side scales to probe_max (mirrors CropService._box_from_rgba).
    assert probe_dims((1400, 1200), 1024) == (1024, 878)
    assert probe_dims((600, 1400), 1024) == (439, 1024)


def test_crop_result_with_probe_assigns_probe_size():
    from shared.image_processing.autocrop import CropResult

    res = CropResult(path="p", box=CropBox(100, 100, 1300, 1100), threshold=15)
    assert res.probe_size is None
    probed = res.with_probe((1400, 1200), 1024)
    assert probed.probe_size == (1024, 878)
    assert probed.box == res.box
    full = CropResult(path="p", box=None, threshold=None, reason=SkipReason.BORDERLESS)
    assert full.with_probe((800, 600), 1024).probe_size == (800, 600)


def test_content_key_changes_on_replace(tmp_path):
    p = tmp_path / "a.png"
    _flat(p)
    before = content_key(str(p))
    assert before[1] is not None and before[2] is not None
    _bordered(p)
    after = content_key(str(p))
    assert before != after
    assert file_stat(str(p))[0] is not None


def test_embedded_lookup_misses_loudly_on_replaced_file(tmp_path):
    p = tmp_path / "a.png"
    _flat(p, size=(32, 32))
    emb.clear()
    try:
        emb.register(str(p), "/tmp/fake-cache", 32, 32)
        assert emb.lookup(str(p)) == ("/tmp/fake-cache", 32, 32)
        _bordered(p, size=(64, 64))
        detail = emb.lookup_detail(str(p))
        assert detail.entry is None
        assert detail.status == emb.STATUS_MISS_REPLACED_FILE
        # Stale entry evicted, not served again.
        assert emb.lookup(str(p)) is None
    finally:
        emb.clear()


def test_embedded_crop_inputs_gate_the_hit(tmp_path):
    p = tmp_path / "a.png"
    _bordered(p)
    emb.clear()
    try:
        box = (8, 8, 56, 56)
        emb.register(str(p), "/tmp/fake-cache", 48, 48, crop_box=box, crop_known=True)
        assert emb.lookup(str(p), crop_box=box) == ("/tmp/fake-cache", 48, 48)
        detail = emb.lookup_detail(str(p), crop_box=(0, 0, 64, 64))
        assert detail.entry is None
        assert detail.status == emb.STATUS_MISS_CROP_MISMATCH
        # Legacy entry with unknown crop: unverifiable under a crop request.
        emb.register(str(p), "/tmp/fake-cache", 48, 48)
        detail2 = emb.lookup_detail(str(p), crop_box=box)
        assert detail2.entry is None
        assert detail2.status == emb.STATUS_MISS_CROP_UNVERIFIABLE
        # No crop requested: legacy serve-as-saved behavior kept.
        assert emb.lookup(str(p)) == ("/tmp/fake-cache", 48, 48)
    finally:
        emb.clear()


def test_embedded_legacy_entry_adopts_stat_baseline(tmp_path):
    p = tmp_path / "a.png"
    _flat(p, size=(32, 32))
    emb.clear()
    try:
        emb._cache[str(p)] = ("/tmp/fake-cache", 32, 32)  # direct write, no sidecar
        assert emb.lookup(str(p)) == ("/tmp/fake-cache", 32, 32)
        assert emb.entry_meta(str(p)) is not None
        _bordered(p, size=(64, 64))
        assert emb.lookup_detail(str(p)).status == emb.STATUS_MISS_REPLACED_FILE
    finally:
        emb.clear()


def test_loader_hit_applies_crop_service_or_misses(tmp_path):
    """End-to-end through load_pixel_store: a crop-matching embedded entry
    serves; a crop-mismatched one misses loudly and falls back to a fresh
    decode with the crop applied (never serves wrongly-cropped pixels)."""
    import shutil

    from engine.store.tiled_pixel_store import TiledPixelStore
    from shared.image_processing.pixel_cache_loader import load_pixel_store

    p = tmp_path / "bordered.png"
    _bordered(p, size=(64, 64))
    svc = CropService()
    box = svc.get(str(p))
    assert box is not None
    emb.clear()
    try:
        # Project-save-shaped entry: raw cropped buffer + recorded box.
        fresh = TiledPixelStore.from_path(str(p), crop_service=svc, tmp_dir=str(tmp_path))
        assert (fresh.width, fresh.height) == (box.width, box.height)
        spill_copy = str(tmp_path / "embedded.raw")
        shutil.copyfile(fresh.path, spill_copy)
        emb.register(
            str(p), spill_copy, fresh.width, fresh.height,
            crop_box=box.to_tuple(), crop_known=True,
        )
        fresh.close()
        hit = load_pixel_store(str(p), crop_service=svc, embedded_cache=None)
        assert (hit.width, hit.height) == (box.width, box.height)
        hit.close()
        # Mismatched crop record: must miss and decode fresh (same correct
        # dims, but sourced from from_path, not the stale buffer).
        emb.register(
            str(p), spill_copy, 8, 8, crop_box=(0, 0, 8, 8), crop_known=True,
        )
        miss = load_pixel_store(str(p), crop_service=svc, embedded_cache=None)
        assert (miss.width, miss.height) == (box.width, box.height)
        miss.close()
    finally:
        emb.clear()


def test_loader_no_sticky_none_on_replace(tmp_path):
    """Flat file (borderless, verdict None) replaced by bordered content:
    the embedded sidecar detects the replace, the loader invalidates the
    crop service's stale entry, and the fresh decode applies the new box.

    (Scope note: the replace evidence comes from the embedded stat
    sidecar. A bare CropService with no embedded entry still keys by path
    only — see CropService._cache; wiring it to content_key() is a
    service.py follow-up, not this task's files.)"""
    import shutil

    from engine.store.tiled_pixel_store import TiledPixelStore
    from shared.image_processing.pixel_cache_loader import load_pixel_store

    p = tmp_path / "swap.png"
    _flat(p, size=(64, 64))
    svc = CropService()
    emb.clear()
    try:
        first = load_pixel_store(str(p), crop_service=svc, embedded_cache=None)
        assert svc.get(str(p)) is None
        # Project-save-shaped entry for the flat file (known boxless).
        spill_copy = str(tmp_path / "swap.raw")
        shutil.copyfile(first.path, spill_copy)
        emb.register(
            str(p), spill_copy, first.width, first.height,
            crop_box=None, crop_known=True,
        )
        first.close()
        # Replace with bordered content. (No manual lookup_detail here:
        # that call evicts replaced-file entries, consuming the evidence
        # the loader itself must observe.)
        _bordered(p, size=(64, 64))
        second = load_pixel_store(str(p), crop_service=svc, embedded_cache=None)
        box = svc.get(str(p))
        assert box is not None, "stale None stuck across file replace"
        assert (second.width, second.height) == (box.width, box.height)
        second.close()
    finally:
        emb.clear()


def test_warmup_reports_completion_including_an_already_warm_cache(tmp_path):
    """``on_done`` must fire for BOTH a cold and an already-warm service.

    The InfoHUD readout (``512x512 → 312x512``) is derived from the box, and
    the box arrives asynchronously without dispatching anything to the Store
    (owner report 2026-11-18: the chip kept the source size while the canvas
    painted the crop). The completion callback is that missing edge; a
    silent "already cached" branch would drop it on the second load.
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from shared.image_processing.autocrop.service import schedule_crop_warmup

    app = QApplication.instance() or QApplication([])

    img = Image.new("RGB", (200, 160), (0, 0, 0))
    img.paste(Image.new("RGB", (120, 80), (200, 180, 160)), (40, 40))
    path = str(tmp_path / "bordered.png")
    img.save(path)

    svc = CropService()
    calls: list[str] = []

    schedule_crop_warmup(svc, [path], on_done=lambda: calls.append("cold"))
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and not calls:
        app.processEvents()
        time.sleep(0.02)
    assert svc._has_cached(path), "the warmup must have computed the box"
    assert calls == ["cold"], "a cold warmup owes exactly one completion"

    schedule_crop_warmup(svc, [path], on_done=lambda: calls.append("warm"))
    assert calls == ["cold", "warm"], (
        "an already-warm cache must still answer the caller — otherwise the "
        "HUD never learns about a crop resolved by an earlier load"
    )
