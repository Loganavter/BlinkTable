"""Env-gated watchdog for the canvas's two geometry rulers.

The canvas paints from one basis (``plan.canvas_w``/``plan.canvas_h`` — the
crop-aware content box plus feature padding) while every canvas-px consumer
(divider bar and clip, zoom/pan projection, drag→split mapping) reads
``runtime_state._content_rect_px``, which ``refresh_live_content_rect`` fits
from that same basis. Two rulers, refreshed independently, that must agree.

They did not, on 2026-11-18: a 312x512 crop of a 512x512 source was painted
against the uncropped 484x484 square fit. At split 0.5 both rects are centred
so the seam looked right; at 0.25 the line sat ~60 px off the painted cut, and
the check that found it was pure arithmetic done by hand. This module puts that
arithmetic in the app behind ``IMGSLI_GEOMETRY_ASSERT`` so the next divergence
names itself in the log.

Env vars (both spellings go through ``shared.debug_flags.env_flag``, whose alias
layer reads ``BLINKTABLE_`` first and falls back to the legacy ``IMGSLI_``):

    BLINKTABLE_GEOMETRY_ASSERT=1  — enable
    IMGSLI_GEOMETRY_ASSERT=1      — same flag, historic spelling

Tag: ``[ic-geometry-assert]``. WARNING level (visible without ``--debug``),
at most one line per :data:`THROTTLE_SECONDS`, and each distinct mismatch once
per process — a diverging frame repainted every frame must not flood the log.

Diagnostic only: nothing here writes state or changes a returned rect, and every
call site is expected to guard the call.
"""

from __future__ import annotations

import logging
import time

from core.log_names import APP_LOGGER_NAME
from shared.debug_flags import env_flag as _env_flag

__all__ = ["TAG", "THROTTLE_SECONDS", "TOLERANCE_PX", "check", "enabled", "report"]

TAG = "[ic-geometry-assert]"

#: Slack for integer-rounded letterbox fits (both rects are int-quantised
#: somewhere upstream), and for the aspect check's implied width.
TOLERANCE_PX = 1.0

#: Minimum spacing between emitted lines, so a mismatch seen on every frame
#: cannot turn into a log flood at frame rate.
THROTTLE_SECONDS = 2.0

_logger = logging.getLogger(APP_LOGGER_NAME)

# Process-wide on purpose: the watchdog can be reached from several canvas
# widgets (pages, export, video preview), and a mismatch is one fact about the
# frame rather than an event per reporter.
_seen: set[str] = set()
_last_emit = 0.0


def enabled() -> bool:
    """True when ``*_GEOMETRY_ASSERT`` is set to a truthy value.

    ``env_flag`` is permissive (``1``/``true``/``yes``/``on`` → on,
    ``""``/``0``/``false``/``no``/``off`` → off) and resolves the
    ``BLINKTABLE_`` prefix ahead of the legacy ``IMGSLI_`` one.
    """
    return _env_flag("IMGSLI_GEOMETRY_ASSERT")


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rect_as_floats(content_rect) -> tuple[float, float, float, float] | None:
    if content_rect is None:
        return None
    try:
        values = [float(v) for v in content_rect]
    except (TypeError, ValueError):
        return None
    if len(values) != 4:
        return None
    return (values[0], values[1], values[2], values[3])


def _fmt(value: float) -> str:
    """Compact number for log lines: ``484`` rather than ``484.0``."""
    return f"{value:g}"


def check(
    canvas_w,
    canvas_h,
    widget_w,
    widget_h,
    content_rect,
    divider_px=None,
) -> list[str]:
    """Arithmetic between the canvas basis and the content rect (pure).

    Returns one short, self-contained message per broken invariant — numbers
    included, so a log line can be acted on without opening the source. An
    empty list means the two rulers agree.

    Invariants (all with :data:`TOLERANCE_PX` slack):

    * the rect's aspect matches the canvas basis, compared on the width implied
      by the rect's height — the crop-basis divergence of 2026-11-18;
    * the rect sits inside the widget — a letterbox fit that does not is fitted
      against something other than the widget it paints into;
    * when ``divider_px`` is given, that position falls inside the rect's span
      on at least one axis (a line outside the picture is the detached-divider
      symptom). The axis follows the split orientation, which this function
      deliberately does not know, so either span accepts the position.

    Never raises: unreadable input comes back as a message instead, because a
    call site guards the call and a watchdog that throws is worse than a
    watchdog that is silent.
    """
    rect = _rect_as_floats(content_rect)
    if rect is None:
        return [f"content rect is not four numbers: {content_rect!r}"]
    x, y, w, h = rect
    problems: list[str] = []

    basis_w = _as_float(canvas_w)
    basis_h = _as_float(canvas_h)
    if basis_w and basis_h and basis_w > 0 and basis_h > 0 and w > 0 and h > 0:
        expected_w = h * basis_w / basis_h
        if abs(w - expected_w) > TOLERANCE_PX:
            problems.append(
                f"content rect {_fmt(w)}x{_fmt(h)} disagrees with the canvas "
                f"basis {_fmt(basis_w)}x{_fmt(basis_h)} "
                f"(expected width {expected_w:.1f}px for height {_fmt(h)})"
            )

    host_w = _as_float(widget_w)
    host_h = _as_float(widget_h)
    if host_w is not None and host_w > 0:
        if x < -TOLERANCE_PX:
            problems.append(
                f"content rect starts left of the widget "
                f"(x={_fmt(x)}, widget width {_fmt(host_w)})"
            )
        elif x + w > host_w + TOLERANCE_PX:
            problems.append(
                f"content rect overflows the widget on the right "
                f"(x+w={_fmt(x + w)}, widget width {_fmt(host_w)})"
            )
    if host_h is not None and host_h > 0:
        if y < -TOLERANCE_PX:
            problems.append(
                f"content rect starts above the widget "
                f"(y={_fmt(y)}, widget height {_fmt(host_h)})"
            )
        elif y + h > host_h + TOLERANCE_PX:
            problems.append(
                f"content rect overflows the widget at the bottom "
                f"(y+h={_fmt(y + h)}, widget height {_fmt(host_h)})"
            )

    divider = _as_float(divider_px) if divider_px is not None else None
    if divider is not None:
        inside_x = (x - TOLERANCE_PX) <= divider <= (x + w + TOLERANCE_PX)
        inside_y = (y - TOLERANCE_PX) <= divider <= (y + h + TOLERANCE_PX)
        if not (inside_x or inside_y):
            problems.append(
                f"divider at {_fmt(divider)}px falls outside the content rect "
                f"(x {_fmt(x)}..{_fmt(x + w)}, y {_fmt(y)}..{_fmt(y + h)})"
            )

    return problems


def report(
    *,
    canvas_w,
    canvas_h,
    widget_w,
    widget_h,
    content_rect,
    divider_px=None,
    logger=None,
) -> None:
    """Log the broken invariants of one frame, throttled and de-duplicated.

    No-op unless :func:`enabled` — with the flag off the live path pays one env
    lookup and nothing else. Never raises and never returns a value: the
    caller's rect is not this function's business.
    """
    if not enabled():
        return
    target = logger if logger is not None else _logger
    global _last_emit
    try:
        problems = check(
            canvas_w,
            canvas_h,
            widget_w,
            widget_h,
            content_rect,
            divider_px,
        )
        for message in problems:
            if message in _seen:
                continue
            now = time.monotonic()
            if now - _last_emit < THROTTLE_SECONDS:
                # Not marked seen: a throttled mismatch is retried on a later
                # frame instead of being dropped for the process lifetime.
                continue
            _last_emit = now
            _seen.add(message)
            target.warning("%s %s", TAG, message)
    except Exception:
        return
