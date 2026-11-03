"""The video strip has ONE renderer: the GPU band. The QPainter pipeline is gone.

Strip atlas phase 3 (``plan-video-strip-atlas-2026-10-30``) deleted the
per-frame QPainter thumbnail pipeline, so the filmstrip row has a single
producer — ``strip_window`` / ``strip_pass`` driven by
``presenter_parts.strip_band.StripBandHost``:

* no per-frame thumbnail service (queue, generation token, pending set,
  per-index disk cache, async GPU bridge);
* no thumbnail coordinator (settle gate, waves, membership filter, idle fill);
* no CPU tile composer and no ``set_cpu_thumbnail_composer`` /
  ``thumbnail_needs_gpu`` seam;
* no second offscreen export widget kept only for 160x90 tiles.

Two renderers is how the strip drifted before: the band and the QPainter tiles
could disagree about what a cell shows, and every fix had to land twice. The
module existence checks are the fail-closed half (a restored file fails here
even if nothing imports it); the symbol scans catch a renamed module that
still carries the old seams.

The positive half pins that the surviving renderer is still there and still
single: the band modules exist, and the exporter still exposes the proxy seam
the band resolves its pixels through (a strip tile must never decode on a
scroll tick).

Dogma source: ``plan-video-strip-atlas-2026-10-30`` phase 3; rendering docs at
``docs/dev/rendering/`` (snapshot replay path).
"""

from __future__ import annotations

import re

from ._framework import ROOT, SRC, iter_py, read, rel

VIDEO_EDITOR = SRC / "tabs" / "image_compare" / "plugins" / "video_editor"
SNAPSHOT_RENDERING = (
    SRC / "tabs" / "image_compare" / "services" / "video_snapshot_rendering"
)

#: Files the retirement deleted. Restoring one — even unused — reintroduces
#: the second renderer, so existence alone fails the contract.
DELETED_MODULES = (
    "src/tabs/image_compare/plugins/video_editor/services/thumbnails.py",
    "src/tabs/image_compare/plugins/video_editor/services/thumbnail_cache.py",
    "src/tabs/image_compare/plugins/video_editor/presenter_parts/thumbnails.py",
    "src/tabs/image_compare/services/video_snapshot_rendering/cpu_thumbnail.py",
)

#: Symbols of the deleted pipeline. Scanned only in the two directories that
#: owned it — ``thumbnail_cache`` is also the name of the image gallery's own,
#: unrelated persistent cache (``src/tabs/image_gallery/services/``), and the
#: gallery keeps it.
FORBIDDEN_SYMBOLS = (
    "ThumbnailService",
    "ThumbnailCoordinator",
    "ThumbnailDiskCache",
    "thumbnail_cache_key_for_snap",
    "cpu_thumbnail",
    "compose_thumbnail_cpu",
    "thumbnail_needs_gpu",
    "set_cpu_thumbnail_composer",
    "ic_thumbnail_debug",
    "thumbnailsUpdated",
    "thumbnailReady",
    "thumbnailsGenerated",
    "generationFinished",
    "thumbnail_service",
    "thumbnail_coordinator",
    "_thumbnail_gpu_export_service",
    "_thumbnail_frame_renderer",
    "render_snapshot_thumbnail_to_pil",
    "set_thumbnails",
    "add_thumbnail",
    "clear_thumbnails",
)

#: The one renderer that must remain, as (module path, required symbols).
SURVIVING_RENDERER = (
    (
        "src/tabs/image_compare/plugins/video_editor/presenter_parts/strip_band.py",
        ("StripBandHost", "render_request"),
    ),
    (
        "src/tabs/image_compare/services/video_snapshot_rendering/strip_window.py",
        ("build_window", "render_window"),
    ),
    (
        "src/tabs/image_compare/services/video_snapshot_rendering/strip_pass.py",
        ("render_strip_window", "release_window_resources"),
    ),
    (
        "src/tabs/image_compare/plugins/video_editor/services/strip_recording.py",
        ("StripRecordingAdapter", "proxy_pair_for", "timeline_strip_layout"),
    ),
)

#: The exporter proxy seam the band's ``proxy_pair_for`` resolves pixels from.
PROXY_SEAM = "src/tabs/image_compare/plugins/video_editor/services/video_export/service.py"


def _scanned_files():
    """App modules of the two directories that owned the deleted pipeline."""
    return list(iter_py(VIDEO_EDITOR)) + list(iter_py(SNAPSHOT_RENDERING))


def test_deleted_pipeline_modules_are_absent():
    present = [path for path in DELETED_MODULES if (ROOT / path).exists()]
    assert present == [], (
        "the per-frame QPainter thumbnail pipeline is deleted (strip atlas "
        f"phase 3); these files are back: {present}"
    )


def test_no_deleted_pipeline_symbol_in_the_editor_or_snapshot_rendering():
    hits = []
    for path in _scanned_files():
        text = read(path)
        for symbol in FORBIDDEN_SYMBOLS:
            for match in re.finditer(rf"\b{re.escape(symbol)}\b", text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{rel(path)}:{line}: {symbol}")
    assert hits == [], (
        "the strip has ONE renderer (the GPU band). Deleted per-frame "
        "thumbnail pipeline symbols reappeared:\n" + "\n".join(hits)
    )


def test_the_gpu_band_renderer_is_still_wired():
    missing = []
    for path, symbols in SURVIVING_RENDERER:
        full = ROOT / path
        if not full.exists():
            missing.append(f"{path} (file missing)")
            continue
        text = read(full)
        for symbol in symbols:
            if not re.search(rf"\b{re.escape(symbol)}\b", text):
                missing.append(f"{path}: {symbol}")
    assert missing == [], (
        "the surviving strip renderer lost part of itself — the band, its "
        f"window/pass or its recording adapter: {missing}"
    )


def test_the_band_and_the_exporter_share_one_proxy_seam():
    """A strip tile resolves pixels through the exporter, never by decoding."""
    service = read(ROOT / PROXY_SEAM)
    assert "_resolve_thumbnail_images" in service, (
        "the exporter proxy seam (``_resolve_thumbnail_images``) is what "
        "``strip_recording.proxy_pair_for`` reads; without it every band tile "
        "is a placeholder"
    )
    adapter = read(
        ROOT
        / "src/tabs/image_compare/plugins/video_editor/services/strip_recording.py"
    )
    assert "_resolve_thumbnail_images" in adapter, (
        "proxy_pair_for must keep driving the exporter's own seam instead of "
        "growing a second pixel source"
    )


def test_the_presenter_owns_exactly_one_strip_renderer():
    """The presenter attaches the band and hands it the editor; nothing else."""
    presenter = read(
        ROOT / "src/tabs/image_compare/plugins/video_editor/presenter.py"
    )
    assert "StripBandHost(" in presenter, "the presenter must attach the band"
    assert presenter.count("strip_band_host") >= 2, (
        "attach + teardown both go through the one host"
    )
    # A second strip producer would have to be constructed here.
    assert "SettleGate" not in presenter, (
        "the strip path has no settle gate (the band builds synchronously per "
        "view change); a gate here means a per-tile debounce came back"
    )


def test_toolkit_timeline_thumbnail_api_has_no_app_caller():
    """The toolkit still exposes its QPainter thumbnail API — unused here."""
    callers = []
    for path in iter_py(VIDEO_EDITOR):
        text = read(path)
        for name in ("set_thumbnails(", "add_thumbnail(", "clear_thumbnails("):
            if name in text:
                callers.append(f"{rel(path)}: {name}")
    assert callers == [], (
        "the app renders the strip through the GPU band; the toolkit timeline's "
        f"thumbnail API has no caller left: {callers}"
    )
