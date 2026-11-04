"""Strip latency gate (phase 4): the owner's strip numbers as a pass/fail run.

The video editor's filmstrip is one GPU pass (``build_window`` +
``render_window``, wired through the toolkit band). Its acceptance numbers,
recorded in ``plan-video-strip-atlas-2026-10-30``, are:

* a full strip fill in **<= 50 ms**;
* any interaction (scroll, zoom, resize) **< 10 ms** for the whole redraw;
* no debounce anywhere, including widget resize — the contract half is
  ``tests/contracts/test_strip_no_debounce.py``.

This devtool is the measurement half. It drives the SAME seam the band drives
(``_band_window``: the timeline's tile from ``strip_recording.strip_tile_size``,
the exporter's proxy pair, the band's ``render_request``, the host's
``StripRecordingAdapter`` and the host's ONE ``TileTextureService``) and times
the stages separately, so a red verdict names what to fix instead of "the strip
is slow".

What one case measures:

* ``build_ms`` — ``build_window``: per-block frame state + plan + array-layer
  allocation + per-tile uniform/instance packing. CPU only.
* ``render_ms`` — one frame through the grab-only window widget, i.e.
  ``render_window``'s single pass (the window's first-frame upload + one
  ``beginPass``/``endPass`` + the present).
* presentation — a framebuffer grab taken OUTSIDE the budget and printed as
  ``grab`` in the detail lines. It proves a frame really landed (a null grab
  fails the run, it is never a silent pass), but a readback is not what the
  editor spends per tick; charging it to the interaction budget would turn the
  gate into a disk test.
* ``total_ms`` — build + render, compared with the case's budget.

Interactions are timed as a tick: a scroll (new first block, same zoom) and a
zoom (new tile size), each building the newest window and rendering it, with
the latest-wins rule exercised — an older window a newer one superseded is
dropped and never applied, and its build is reported, not counted.

Flags: ``--blocks 8,17,32``, ``--fill-budget-ms 50``,
``--interaction-budget-ms 10``, ``--repeat N`` (median of N),
``--scroll-blocks N``, ``--zoom-tile WxH``, ``--cpu-only`` (planning only),
``-q``.

Exit codes: ``0`` every measured case within budget, ``1`` a budget exceeded
(or a render failure on a live surface), ``2`` no display — a fabricated pass
is never reported.

``--cpu-only`` runs the planning half with no RHI at all (it pins
``QT_QPA_PLATFORM=offscreen`` when the caller set none, so a headless shell
cannot reach a real display) and issues **no budget verdict**: ``render_ms`` is
``n/a`` and the verdict column reads ``build-only``. Only the GPU run on a
leased display is the gate — a CPU number cannot answer the interaction budget.

Determinism: every case is a steady-state measurement — one throwaway window
per case absorbs one-time costs (RHI/pass initialization, source decode, the
proxy level for that tile), which are printed once as ``cold`` — and each
number is the median of ``--repeat`` runs. That is comparable between two runs
on the same host, not across GPUs or backends.

Measured status (2026-11-04, this host, leased display for the GPU column):

* the per-tile spill ``msync`` was 14 calls / ~24 ms of a 38 ms window build;
  dropping the durability-only flushes took a fill from 34/37/42 ms to
  3/7/12 ms at 8/17/32 blocks;
* a planned tile is reused across windows (``StripTileCache``) and the plan
  builder outlives a window, so a scroll tick plans only the newly exposed
  block: build 0.3/0.6/1.9 ms, and the tick's cost was then the pass itself
  (upload + draw 5.2/17.4/32.4 ms at 8/17/32 against 0.8/1.3/2.4 ms for a
  draw-only frame of the same window);
* the array layers are reused the same way (the band's one
  ``TileTextureService``, content-addressed tile keys, a per-pass upload skip),
  so a scroll tick uploads only the block that newly entered the range — the
  rig keeps one allocator across ticks for exactly that reason;
* a zoom changes the tile size, so every tile is re-planned (~12 ms at 8
  blocks): the interaction budget is a scroll/resize number, a geometry change
  costs a planning pass.
"""

# Audit-Meta: pattern=devtool-pipeline reason="one latency gate: the band's own build seam, the per-case warm-up/median measurement, the latest-wins proof and the budget table are one measurement — splitting the rig from the table would let them disagree about what was timed"
from __future__ import annotations

import argparse
import gc
import os
import shutil

import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent.parent
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from devtools import bench_strip_batch as batch  # noqa: E402

#: The band's visible window sizes: a 1200 px strip shows ~8-11 blocks, a 2K one
#: ~17, a 4K one ~32 (``strip_recording.MAX_BLOCKS`` caps a window at 64).
DEFAULT_BLOCKS = (8, 17, 32)
DEFAULT_FILL_BUDGET_MS = 50.0
DEFAULT_INTERACTION_BUDGET_MS = 10.0
#: A zoom tick's tile: the production strip tile is pinned to the filmstrip
#: thumbnail (160x90), so this case widens the tile to prove the plan + pass
#: path on a second geometry (still one texture-array layer, see ``LAYER_PX``).
DEFAULT_ZOOM_TILE = (224, 126)
DEFAULT_REPEAT = 3
FPS = batch.FPS


# ---------------------------------------------------------------------------
# Cases and results
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CaseResult:
    """One measured case: the stages, the budget and the verdict."""

    name: str
    kind: str  # "fill" | "interaction"
    budget_ms: float
    build_ms: float
    render_ms: float | None = None
    present_ms: float | None = None
    detail: str = ""
    gpu: bool = True

    @property
    def total_ms(self) -> float:
        return self.build_ms + float(self.render_ms or 0.0)

    @property
    def verdict(self) -> str:
        if not self.gpu:
            return "build-only"
        if self.kind == "fill":
            # The owner's numbers: <= 50 ms fill, < 10 ms interaction.
            return "pass" if self.total_ms <= self.budget_ms else "FAIL"
        return "pass" if self.total_ms < self.budget_ms else "FAIL"

    @property
    def failed(self) -> bool:
        return self.verdict == "FAIL"


@dataclass(slots=True)
class _Rig:
    """The band's construction seam: one store, one exporter, one tile."""

    store: object
    exporter: object
    image1: Path
    image2: Path
    tile: tuple[int, int]
    cache: object
    service: object = None
    recording: object = None

    def build(self, count: int, *, first_block: int = 0, tile=None):
        tile_w, tile_h = self.tile if tile is None else tile
        # ``cols = count``: the band is ONE row (the timeline's grid), so tile i
        # of the window lands on block i of the strip. The cache, the array
        # allocator and the recording are the HOST's, kept across ticks: a scroll
        # tick reuses the planned tiles AND the array layers of the blocks that
        # stayed visible (StripBandHost does the same), and a fresh adapter or
        # allocator per tick would hide that reuse.
        if self.recording is None:
            # ``varied``: every block carries its OWN frame, as a real
            # recording does. With one shared pair per block the planner's
            # single-slot image-prep cache would answer for the next tile and
            # the table would report a strip no recording produces.
            self.recording = batch.band_recording(
                self.store, self.image1, self.image2, self.exporter, varied=True
            )
        if self.service is None:
            self.service = _tile_service()
        return _band_window(
            self.recording,
            count=int(count),
            first_block=int(first_block),
            tile=(int(tile_w), int(tile_h)),
            tile_cache=self.cache,
            tile_service=self.service,
        )

    def clear_cache(self) -> None:
        """Drop planned tiles: a fill/zoom case measures a cold planning pass."""
        self.cache.clear()
        self.recording = None
        # A cold fill is also a cold allocator: no layer of a previous case may
        # stand in for the upload this case is meant to pay for.
        self.service = None


def _timed(fn):
    started = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - started) * 1000.0


# ---------------------------------------------------------------------------
# The target: a grab-only band window widget, driven like the band drives it
# ---------------------------------------------------------------------------


class _Surface:
    """One band window widget: the pass a tick records, plus a grab proof."""

    def __init__(self, window) -> None:
        from engine.gpu.offscreen_canvas import (
            resize_and_show_offscreen_widget,
            shutdown_offscreen_widget,
        )

        self._shutdown = shutdown_offscreen_widget
        self.widget = batch.make_window_widget(window)
        resize_and_show_offscreen_widget(
            self.widget, (int(window.target_w), int(window.target_h))
        )
        batch.require_rhi(self.widget, "strip latency gate")

    def render(self, window) -> tuple[float, float]:
        """Draw ``window``; returns ``(frame_ms, grab_ms)``.

        ``frame_ms`` is the tick's GPU path: ``render_window``'s one pass, its
        upload on the window's first frame, and Qt's present. ``grab_ms`` is
        the readback that proves something reached the target.
        """
        from engine.gpu.offscreen_canvas import render_widget_frame

        self.widget.set_window(window)
        _, frame_ms = _timed(lambda: render_widget_frame(self.widget))
        error = self.widget.last_error
        if error:
            raise RuntimeError(error)
        grab, grab_ms = _timed(self.widget.grabFramebuffer)
        if grab.isNull() or grab.size().isEmpty():
            raise RuntimeError(
                "presentation produced a null framebuffer: the window's pass "
                "never reached the target"
            )
        return frame_ms, grab_ms

    def close(self) -> None:
        try:
            self._shutdown(self.widget)
        except Exception:
            pass


def _warm(rig: _Rig, count: int, *, tile=None, gpu: bool) -> tuple:
    """One throwaway window (and frame): ``(surface, cold_ms)``.

    The one-time costs — source decode, the exporter's proxy level for this
    tile size, the plan builder's first-call caches, and (on a display) the
    pass's samplers/array/buffers/SRBs plus the first upload — must not land in
    a measured run. They are real, so they are reported as ``cold``.
    """
    window, cold_ms = _timed(lambda: rig.build(count, tile=tile))
    if not gpu:
        return None, cold_ms
    surface = _Surface(window)
    surface.render(window)
    return surface, cold_ms


# ---------------------------------------------------------------------------
# Case runners
# ---------------------------------------------------------------------------


def _detail(
    builds, frames, grabs, *, cold: str, extra: str = "", cold_label: str = "cold"
) -> str:
    parts = [
        "build min/med/max "
        f"{min(builds):.1f}/{statistics.median(builds):.1f}/{max(builds):.1f} ms"
    ]
    if frames:
        parts.append(f"frame med {statistics.median(frames):.2f} ms")
    if grabs:
        parts.append(
            f"grab med {statistics.median(grabs):.2f} ms (outside budget)"
        )
    if extra:
        parts.append(extra)
    if cold:
        parts.append(f"{cold_label} {cold}")
    return "; ".join(parts)


def _fill_case(rig: _Rig, count: int, *, args) -> CaseResult:
    """Full strip fill: build the window, draw it once, compare with 50 ms."""
    gpu = not args.cpu_only
    surface, cold_ms = _warm(rig, count, gpu=gpu)
    builds, frames, grabs = [], [], []
    try:
        for _ in range(max(1, int(args.repeat))):
            rig.clear_cache()
            window, build_ms = _timed(lambda: rig.build(count))
            builds.append(build_ms)
            if surface is not None:
                frame_ms, grab_ms = surface.render(window)
                frames.append(frame_ms)
                grabs.append(grab_ms)
    finally:
        if surface is not None:
            surface.close()
    return CaseResult(
        name=f"fill-{count}",
        kind="fill",
        budget_ms=float(args.fill_budget_ms),
        build_ms=statistics.median(builds),
        render_ms=statistics.median(frames) if frames else None,
        present_ms=statistics.median(grabs) if grabs else None,
        detail=_detail(
            builds,
            frames,
            grabs,
            cold=f"{cold_ms:.1f} ms",
            cold_label=(
                "cold (init + first upload)" if gpu else "cold (decode + plan caches)"
            ),
        ),
        gpu=gpu,
    )


def _latest_wins(stale, newest) -> None:
    """Assert the superseded window can never be applied.

    ``render_window`` refuses it before any GPU work; a gate that measured a
    stale window would be reporting a frame the user never sees.
    """
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripWindowCancelled,
        is_current,
        render_window,
    )

    if is_current(stale):
        raise RuntimeError(
            "latest-wins broken: a superseded window is still the current "
            "one, so this tick would measure a frame the band never draws"
        )
    try:
        render_window(None, stale, rhi=None, command_buffer=None)
    except StripWindowCancelled:
        return
    raise RuntimeError(
        "latest-wins broken: render_window accepted a superseded window"
    )


def _interaction_case(rig: _Rig, count: int, *, kind: str, args) -> CaseResult:
    """A scroll/zoom tick: the newest window built and drawn, stale one dropped."""
    gpu = not args.cpu_only
    tile = tuple(args.zoom_tile) if kind == "zoom" else None
    # One throwaway window for the seam this case measures: the plan builder's
    # caches, the exporter's proxy level for this tile size, and (on a display)
    # this target's pass resources plus its first upload.
    surface, cold_ms = _warm(rig, count, tile=tile, gpu=gpu)
    builds, frames, grabs, dropped = [], [], [], []
    checked = False
    try:
        for index in range(max(1, int(args.repeat))):
            if kind == "scroll":
                # Two ticks in one frame: the scroll moved by ``scroll_blocks``
                # twice, and only the newest window is built + drawn.
                step = max(1, int(args.scroll_blocks))
                stale, stale_ms = _timed(
                    lambda: rig.build(count, first_block=step * (2 * index + 1))
                )
                window, build_ms = _timed(
                    lambda: rig.build(count, first_block=step * (2 * index + 2))
                )
            else:
                rig.clear_cache()
                stale, stale_ms = _timed(lambda: rig.build(count))
                window, build_ms = _timed(
                    lambda: rig.build(count, tile=tile)
                )
            dropped.append(stale_ms)
            if not checked:
                _latest_wins(stale, window)
                checked = True
            builds.append(build_ms)
            if surface is not None:
                frame_ms, grab_ms = surface.render(window)
                frames.append(frame_ms)
                grabs.append(grab_ms)
    finally:
        if surface is not None:
            surface.close()
    name = f"interaction-{kind}-{count}"
    return CaseResult(
        name=name,
        kind="interaction",
        budget_ms=float(args.interaction_budget_ms),
        build_ms=statistics.median(builds),
        render_ms=statistics.median(frames) if frames else None,
        present_ms=statistics.median(grabs) if grabs else None,
        detail=_detail(
            builds,
            frames,
            grabs,
            extra=(
                "superseded build med "
                f"{statistics.median(dropped):.1f} ms (dropped, not applied)"
            ),
            cold=f"{cold_ms:.1f} ms",
            cold_label=(
                "cold (init + first upload)" if gpu else "cold (decode + plan caches)"
            ),
        ),
        gpu=gpu,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_header(rig: _Rig, args) -> None:
    platform = os.environ.get("QT_QPA_PLATFORM", "(unset)")
    display = os.environ.get("DISPLAY", "(none)")
    mode = "cpu-only, no RHI" if args.cpu_only else f"platform {platform}"
    print(
        f"strip latency gate: tile {rig.tile[0]}x{rig.tile[1]} (filmstrip "
        f"thumbnail), fps {FPS}, blocks {list(args.blocks)}, "
        f"repeat {args.repeat} (median)"
    )
    print(
        f"  display {display} ({mode}); "
        f"zoom tile {args.zoom_tile[0]}x{args.zoom_tile[1]}; "
        f"scroll step {args.scroll_blocks} block(s)"
    )


def _print_table(results, *, gpu: bool, quiet: bool) -> None:
    if quiet:
        return
    print()
    print(
        f"{'case':<22} | {'build_ms':>8} | {'render_ms':>9} | "
        f"{'total_ms':>8} | {'budget_ms':>9} | verdict"
    )
    print("-" * 78)
    for result in results:
        render = "n/a" if result.render_ms is None else f"{result.render_ms:.2f}"
        print(
            f"{result.name:<22} | {result.build_ms:>8.2f} | {render:>9} | "
            f"{result.total_ms:>8.2f} | {result.budget_ms:>9.1f} | "
            f"{result.verdict}"
        )
    print()
    for result in results:
        if result.detail:
            print(f"  {result.name}: {result.detail}")


def _report(results, *, gpu: bool, args) -> int:
    failures = [result for result in results if result.failed]
    if not gpu:
        print()
        print(
            "build-only run: no RHI, so render_ms is n/a and no budget verdict "
            "is issued. The gate is the GPU run on a leased display "
            "(fill <= "
            f"{args.fill_budget_ms:g} ms, interaction < "
            f"{args.interaction_budget_ms:g} ms)."
        )
        return 0
    if failures:
        print()
        for result in failures:
            print(
                f"OVER BUDGET: {result.name} {result.total_ms:.2f} ms > "
                f"{result.budget_ms:g} ms"
            )
        return 1
    print()
    print(
        "all cases within budget: fill <= "
        f"{args.fill_budget_ms:g} ms, interaction < "
        f"{args.interaction_budget_ms:g} ms"
    )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_counts(text: str) -> tuple[int, ...]:
    counts = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            raise argparse.ArgumentTypeError(
                "--blocks must look like 8,17,32"
            ) from None
        if value <= 0:
            raise argparse.ArgumentTypeError("--blocks must be positive")
        counts.append(value)
    if not counts:
        raise argparse.ArgumentTypeError("--blocks needs at least one count")
    return tuple(counts)


def _parse_tile(text: str) -> tuple[int, int]:
    parts = str(text).replace("x", "X").split("X")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--zoom-tile must look like 224x126")
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--zoom-tile must look like 224x126"
        ) from None
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("--zoom-tile must be positive")
    return width, height


def _tile_cache():
    """The host's plan cache (``StripTileCache``), one per rig."""
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripTileCache,
    )

    return StripTileCache()


def _tile_service():
    """The host's one array allocator (``StripBandHost.array_service``)."""
    from engine.cache.tile_texture_service import TileTextureService
    from tabs.image_compare.plugins.video_editor.presenter_parts.strip_band import (
        STRIP_ARRAY_CAPACITY,
    )

    return TileTextureService(max_array_size=STRIP_ARRAY_CAPACITY)


def _band_window(recording, *, count, first_block, tile, tile_cache, tile_service):
    """``build_window`` driven with the host's own pieces and ONE allocator.

    The same seam ``bench_strip_batch.build_band_window`` uses — the host's
    ``render_request``, the host's ``StripRecordingAdapter`` over the exporter's
    proxy pair, and the layout the timeline would hand the renderer (synthetic
    here, one row of ``count`` blocks: the bench has no timeline) — plus the one
    thing that wrapper cannot carry: the rig's ``TileTextureService``. A band
    owns exactly one for its whole life, so a scroll tick keeps the array layers
    of the blocks that stayed visible; with a fresh allocator per window the
    gate would measure the full re-upload this tick must no longer pay.
    """
    from tabs.image_compare.plugins.video_editor.presenter_parts.strip_band import (
        render_request as band_render_request,
    )
    from tabs.image_compare.services.video_snapshot_rendering.strip_window import (
        StripBlockLayout,
        build_window,
    )

    tile_w, tile_h = (int(value) for value in tile)
    layout = StripBlockLayout(
        tile_size=(tile_w, tile_h),
        fps=FPS,
        block_count=int(count),
        cols=int(count),
    )
    recording.layout = layout
    recording.fps = float(layout.fps)
    recording.block_count = int(layout.block_count)
    return build_window(
        recording,
        int(first_block),
        int(count),
        tile_w,
        tile_h,
        request=band_render_request(tile_w, tile_h),
        layout=layout,
        tile_cache=tile_cache,
        tile_service=tile_service,
    )


def _strip_tile() -> tuple[int, int]:
    """The band's own tile geometry (``strip_recording`` owns the number)."""
    from tabs.image_compare.plugins.video_editor.services.strip_recording import (
        strip_tile_size,
    )

    return tuple(int(value) for value in strip_tile_size())


def _self_test() -> int:
    """The verdict arithmetic, offscreen: budgets must be able to fail.

    A gate whose red path is untested is a gate nobody has seen fail. This
    checks the two budget rules (``<=`` for a fill, ``<`` for an interaction)
    and that an inflated/lowered budget flips the same measurement — the
    supervisor's "prove the gate fails" step, without a display.
    """
    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        if not condition:
            failures.append(name)

    fill = CaseResult("fill-8", "fill", 50.0, 30.0, 10.0)
    check("fill at the budget passes (<=)", fill.verdict == "pass")
    check(
        "fill over the budget fails",
        CaseResult("fill-8", "fill", 50.0, 41.0, 10.0).verdict == "FAIL",
    )
    interaction = CaseResult("scroll", "interaction", 10.0, 6.0, 3.0)
    check("interaction under the budget passes", interaction.verdict == "pass")
    check(
        "interaction at exactly the budget fails (<)",
        CaseResult("scroll", "interaction", 10.0, 7.0, 3.0).verdict == "FAIL",
    )
    check(
        "a lowered budget flips a passing case",
        CaseResult("scroll", "interaction", 5.0, 6.0, 3.0).verdict == "FAIL",
    )
    build_only = CaseResult("fill-8", "fill", 50.0, 30.0, None, gpu=False)
    check("build-only verdict", build_only.verdict == "build-only")
    check("build-only never fails", not build_only.failed)
    check("blocks parser", _parse_counts("8,17,32") == (8, 17, 32))
    check("zoom tile parser", _parse_tile("224x126") == (224, 126))
    for name in failures:
        print(f"SELF-TEST FAILED: {name}", file=sys.stderr)
    if failures:
        return 1
    print("self-test: verdict arithmetic, budget overrides and parsers ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Strip latency gate: 50 ms fill / 10 ms interaction on the video "
            "editor's one-pass filmstrip."
        )
    )
    parser.add_argument(
        "--blocks",
        type=_parse_counts,
        default=DEFAULT_BLOCKS,
        help="comma-separated visible block counts (default 8,17,32)",
    )
    parser.add_argument(
        "--fill-budget-ms",
        type=float,
        default=DEFAULT_FILL_BUDGET_MS,
        help="budget for a full strip fill (default 50)",
    )
    parser.add_argument(
        "--interaction-budget-ms",
        type=float,
        default=DEFAULT_INTERACTION_BUDGET_MS,
        help="budget for one scroll/zoom tick (default 10)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=DEFAULT_REPEAT,
        help="runs per case; the median is reported (default 3)",
    )
    parser.add_argument(
        "--scroll-blocks",
        type=int,
        default=1,
        help="blocks a scroll tick moves (default 1)",
    )
    parser.add_argument(
        "--zoom-tile",
        type=_parse_tile,
        default=DEFAULT_ZOOM_TILE,
        help="tile size a zoom tick switches to (default 224x126)",
    )
    parser.add_argument(
        "--cpu-only",
        action="store_true",
        help="planning only: no RHI, no budget verdict (build-only table)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="check the verdict arithmetic and parsers (no display, no GPU)",
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    if max(args.zoom_tile) > batch.LAYER_PX:
        # One tile is uploaded 1:1 into one texture-array layer.
        print(
            f"--zoom-tile {args.zoom_tile[0]}x{args.zoom_tile[1]} exceeds the "
            f"{batch.LAYER_PX}px array layer (one tile is uploaded 1:1 into "
            "one layer)",
            file=sys.stderr,
        )
        return 1
    if args.cpu_only and not os.environ.get("QT_QPA_PLATFORM"):
        # Planning needs no surface at all, and a headless shell must not open
        # a connection to a real display just to time a CPU function.
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    gpu = not args.cpu_only
    workdir = Path(tempfile.mkdtemp(prefix="bench-strip-latency-"))
    try:
        batch.qt_app()
        batch.activate_image_compare_tab()
        if gpu:
            try:
                batch.preflight_rhi()
            except batch.NoDisplayError as exc:
                print(f"no display: {exc}", file=sys.stderr)
                print(
                    "this gate measures the strip's GPU pass: run it on a "
                    "private display (a live-lease) — offscreen QRhi has no "
                    "framebuffer, so a pass here would be fabricated. Use "
                    "--cpu-only for the planning half.",
                    file=sys.stderr,
                )
                return 2
        image1, image2 = batch.make_sources(workdir)
        rig = _Rig(
            store=batch.build_store(),
            exporter=batch.BandExporter(Path(image1), Path(image2)),
            image1=Path(image1),
            image2=Path(image2),
            tile=_strip_tile(),
            cache=_tile_cache(),
        )
        if not args.quiet:
            _print_header(rig, args)
        results: list[CaseResult] = []
        for count in args.blocks:
            results.append(_fill_case(rig, int(count), args=args))
        for count in args.blocks:
            results.append(
                _interaction_case(rig, int(count), kind="scroll", args=args)
            )
        results.append(
            _interaction_case(rig, int(args.blocks[0]), kind="zoom", args=args)
        )
        _print_table(results, gpu=gpu, quiet=args.quiet)
        return _report(results, gpu=gpu, args=args)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        # The plans carry spill-backed stores whose ``__del__`` unlinks a
        # memmap: letting them be collected during interpreter shutdown runs
        # that in a torn-down module namespace (a NameError traceback on stderr
        # that reads like a crash). Drop the module's live window, close the
        # process-wide pyramid registry and collect here, while the module
        # globals are alive.
        try:
            from tabs.image_compare.services.video_snapshot_rendering import (
                strip_window,
            )

            window = strip_window.live_window()
            if window is not None:
                strip_window.cancel(window, "latency gate finished")
            from engine.store import pyramid_registry

            pyramid_registry.clear()
        except Exception:
            pass
        gc.collect()


if __name__ == "__main__":
    sys.exit(main())
