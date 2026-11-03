"""``IMGSLI_IMAGE_COMPARE_DEBUG``-gated tracing for Image Compare DnD.

Mirrors ``tabs/image_gallery/debug.py``:
``IMGSLI_GALLERY_DEBUG`` → ``[gallery-dnd]`` / ``[gallery-debug]``.

Env vars:
    IMGSLI_IMAGE_COMPARE_DEBUG=1 — official
    IMGSLI_IC_DEBUG=1            — short alias (same flag)
    IMGSLI_IC_PREVIEW_DEBUG=1    — preview display / re-render gate ([ic-preview])
    IMGSLI_MAGNIFIER_DEBUG=1     — magnifier instances toolbar ([magnifier-debug])
    IMGSLI_IC_LOUPE_DEBUG=1      — magnifier residency/LOD trace ([ic-loupe])
    IMGSLI_IC_INVALIDATE_DEBUG=1 — attributed invalidation bus ([ic-invalidate])

Tags:
    [ic-dnd]     — drag-enter / accept / handle_drop routing
    [ic-debug]   — generic lifecycle
    [ic-loupe]   — magnifier residency/LOD trace: requestLevel answers,
                   dropped-slice reasons, texture-choice and dedup verdicts
                   (magnifier_pass, scene_update, feature_overlay_gpu,
                   interaction — records also land in FrameContext)
    [ic-preview] — preview display gate: when the canvas apply runs, and
                   every reason it is deferred/skipped instead (render_flow.py,
                   loading_pyramid.py, _session_controller.py); since
                   2026-08-29 also per-slot picker decisions
                   (pick_display_with_preview_backing), residency rekey
                   (realize_tile_plan stashing/restore) and fallback-LOD
                   atomic decision (renderer._resolve_fallback_plan) so
                   "preview shown but placeholder missing" can be correlated
                   as: tiles replaced → render state invalidated → pick
                   → fallback decision → apply/skip

All helpers emit ``WARNING`` when their explicit env flag is set (visible
without ``--debug``) and ``DEBUG`` when global ``--debug`` is on — hybrid per
gallery fix 2026-08-26. See ``docs/dev/LOGGING.md``.
"""

from __future__ import annotations

import logging

from shared.debug_flags import env_flag as _env_flag
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


def _ic_debug_enabled() -> bool:
    return _env_flag("IMGSLI_IMAGE_COMPARE_DEBUG") or _env_flag("IMGSLI_IC_DEBUG")


def _emit(prefix: str, msg: str, *args, env_flags=("IMGSLI_IMAGE_COMPARE_DEBUG", "IMGSLI_IC_DEBUG"), **kwargs) -> None:
    if any(_env_flag(flag) for flag in env_flags):
        logger.warning(prefix + " " + msg, *args, **kwargs)
    else:
        logger.debug(prefix + " " + msg, *args, **kwargs)


def ic_debug_enabled() -> bool:
    return _ic_debug_enabled()


def ic_dnd_debug_enabled() -> bool:
    return _ic_debug_enabled()


def ic_dnd_debug(msg: str, *args, **kwargs) -> None:
    if ic_dnd_debug_enabled():
        _emit("[ic-dnd]", msg, *args, **kwargs)


def ic_preview_debug_enabled() -> bool:
    return _env_flag("IMGSLI_IC_PREVIEW_DEBUG")


def ic_preview_debug(msg: str, *args, **kwargs) -> None:
    if ic_preview_debug_enabled():
        _emit(
            "[ic-preview]",
            msg,
            *args,
            env_flags=("IMGSLI_IC_PREVIEW_DEBUG",),
            **kwargs,
        )


def ic_gap_debug_enabled() -> bool:
    return _env_flag("IMGSLI_IC_GAP_DEBUG") or _env_flag("IMGSLI_IC_PREVIEW_DEBUG")


def ic_gap_debug(msg: str, *args, **kwargs) -> None:
    if ic_gap_debug_enabled():
        _emit(
            "[ic-gap]",
            msg,
            *args,
            env_flags=("IMGSLI_IC_GAP_DEBUG", "IMGSLI_IC_PREVIEW_DEBUG"),
            **kwargs,
        )


def ic_label_debug_enabled() -> bool:
    return _env_flag("IMGSLI_IC_LABEL_DEBUG")


def ic_label_debug(msg: str, *args, **kwargs) -> None:
    if ic_label_debug_enabled():
        _emit(
            "[ic-label-debug]",
            msg,
            *args,
            env_flags=("IMGSLI_IC_LABEL_DEBUG",),
            **kwargs,
        )


def ic_video_debug_enabled() -> bool:
    # FROZEN OR-semantics (host-dbgflags epic): the VIDEO_EDITOR /
    # IC_VIDEO umbrella stays — ~30 consumer files outside the epic
    # scope rely on it (render_flow, loading_pyramid, use_cases/*,
    # rhi_renderer/*). Narrow per call site (editor-zone callers use
    # this predicate as-is; thumbnail/timeline zones use their own
    # gates); the full OR-split is a deferred supervisor phase.
    return _env_flag("IMGSLI_VIDEO_EDITOR_DEBUG") or _env_flag("IMGSLI_IC_VIDEO_DEBUG")


def ic_video_debug(msg: str, *args, **kwargs) -> None:
    if ic_video_debug_enabled():
        _emit(
            "[video-editor-debug]",
            msg,
            *args,
            env_flags=("IMGSLI_VIDEO_EDITOR_DEBUG", "IMGSLI_IC_VIDEO_DEBUG"),
            **kwargs,
        )


def ic_perf_debug_enabled() -> bool:
    return _env_flag("IMGSLI_VIDEO_PERF_DEBUG") or _env_flag("IMGSLI_IC_PERF_DEBUG")


def ic_perf_debug(msg: str, *args, **kwargs) -> None:
    if ic_perf_debug_enabled():
        _emit(
            "[video-perf-debug]",
            msg,
            *args,
            env_flags=("IMGSLI_VIDEO_PERF_DEBUG", "IMGSLI_IC_PERF_DEBUG"),
            **kwargs,
        )


def ic_magnifier_debug_enabled() -> bool:
    return _env_flag("IMGSLI_MAGNIFIER_DEBUG")


def ic_magnifier_debug(msg: str, *args, **kwargs) -> None:
    if ic_magnifier_debug_enabled():
        _emit(
            "[magnifier-debug]",
            msg,
            *args,
            env_flags=("IMGSLI_MAGNIFIER_DEBUG",),
            **kwargs,
        )


def ic_loupe_debug_enabled() -> bool:
    return _env_flag("IMGSLI_IC_LOUPE_DEBUG")


def ic_loupe_debug(msg: str, *args, **kwargs) -> None:
    if ic_loupe_debug_enabled():
        _emit(
            "[ic-loupe]",
            msg,
            *args,
            env_flags=("IMGSLI_IC_LOUPE_DEBUG",),
            **kwargs,
        )


def ic_invalidate_debug_enabled() -> bool:
    return _env_flag("IMGSLI_IC_INVALIDATE_DEBUG")


def ic_invalidate_debug(msg: str, *args, **kwargs) -> None:
    if ic_invalidate_debug_enabled():
        _emit(
            "[ic-invalidate]",
            msg,
            *args,
            env_flags=("IMGSLI_IC_INVALIDATE_DEBUG",),
            **kwargs,
        )


def ic_preview_source_tier(img, preview, original, state_img) -> str:
    """Human-readable display tier for ``img`` (``[ic-preview]`` payload).

    ``pick_display_with_preview_backing`` / ``pick_display_image`` can hand
    the canvas any of: the unified full-res store (``image_state.image*``),
    the bounded preview ``QImage`` (pipeline preview tier), or the
    original ``QImage``. Identity comparison -- the tiers are distinct
    objects, never shared.
    """
    if img is None:
        return "none"
    if img is preview:
        return "preview"
    if img is original:
        return "original"
    if img is state_img:
        return "store"
    return "other"
