"""Single throttled debug channel for video timeline.

Replaces ~40 ad-hoc video debug / thumbnail debug / DBG calls with
one throttled logger ``BlinkTable.video_timeline`` + Tracer.

Timeline-zone gate is :func:`timeline_debug_enabled` below
(``IMGSLI_TIMELINE_DEBUG``-only, permissive ``env_flag`` parsing per
LOGGING.md:64). Throttle 700 ms per kind so resize/hover spam does not
flood log.txt. Payload goes to Tracer when IMGSLI_TRACE=1.

Zone note (host-dbgflags W5): the former ``IMGSLI_VIDEO_EDITOR_DEBUG``
umbrella disjunct is dropped — timeline sites gate on TIMELINE-only
while editor-zone sites keep ``ic_video_debug_enabled``. Migration:
``IMGSLI_TIMELINE_DEBUG=1`` for the timeline channel.

Contract: rg for legacy debug markers outside this file and tracing is 0.
"""

from __future__ import annotations

import logging
import time

from shared.debug_flags import env_flag
from core.log_names import APP_LOGGER_NAME

_thlog = logging.getLogger(f"{APP_LOGGER_NAME}.video_timeline")
_last_emit_ms: dict[str, float] = {}
_INTERVAL_MS = 700.0


def timeline_debug_enabled() -> bool:
    """Timeline-zone gate: layout/scroll-area diagnostics.

    Canonical gate for timeline-zone call sites (e.g. the dialog
    shell's ``create_timeline_scroll_area`` log). NARROW semantics
    (host-dbgflags W5): ``IMGSLI_TIMELINE_DEBUG``-only — the former
    ``IMGSLI_VIDEO_EDITOR_DEBUG`` umbrella disjunct now belongs to the
    editor zone (``ic_video_debug_enabled``). Predicate set == emit set:
    this module emits via ``_thlog`` unconditionally at call sites that
    already gated on this predicate.
    """
    return env_flag("IMGSLI_TIMELINE_DEBUG")


def _enabled() -> bool:
    # Compat alias: no live importers (verified 2026-09-20 — only
    # dialog/shell.py imports from this module, and it uses
    # timeline_debug_enabled). Kept with NARROW semantics so any
    # out-of-tree caller keeps working without the umbrella.
    return timeline_debug_enabled()


def _should_emit(kind: str) -> bool:
    now_ms = time.monotonic() * 1000.0
    last = _last_emit_ms.get(kind, 0.0)
    if now_ms - last < _INTERVAL_MS:
        return False
    _last_emit_ms[kind] = now_ms
    return True
