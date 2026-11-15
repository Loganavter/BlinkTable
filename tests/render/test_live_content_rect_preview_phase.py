"""``refresh_live_content_rect`` must not keep the previous pair's rect.

Owner report 2026-11-15 (Image Compare): after dropping a pair and dragging the
split divider while one side's pyramid is still building, the canvas keeps a
vertical seam at the *previous* pair's content edge (and the magnifier's
overlap outline flaps in that zone). Both read
``runtime_state._content_rect_px`` / ``_inner_content_rect_px``.

The refresh below is the per-plan-application owner of that rect. It used to
return early whenever no store was attached to the canvas — which is exactly
the state of a preview-tier frame while the pyramid builds — so the rect kept
the outgoing pair's aspect and every canvas-px consumer (divider clip and bar
position, magnifier geometry, letterbox fill) drew against it.

The plan already carries the pair envelope in SOURCE space
(``CanvasRenderPlan.envelope_dims``), i.e. the same truth the store-backed
branch fits; a preview frame must use it instead of leaving the rect stale.
"""

from __future__ import annotations

from types import SimpleNamespace

from engine.cache.display_tier_service import refresh_live_content_rect


def _state(rect):
    return SimpleNamespace(
        _store=None,
        _export_canvas_viewport=None,
        _content_rect_px=rect,
        _inner_content_rect_px=None,
        _letterbox_params=[(0.0, 0.0, 1.0, 1.0), (0.0, 0.0, 1.0, 1.0)],
        _clip_overlays_to_content_rect=False,
    )


def _canvas(state, w=1024, h=450):
    return SimpleNamespace(
        runtime_state=state,
        _store=None,
        width=lambda: w,
        height=lambda: h,
    )


def _plan(envelope):
    return SimpleNamespace(
        image_is_padded_composite=False,
        geometry_letterbox=False,
        envelope_dims=envelope,
        canvas_w=envelope[0] if envelope else 0,
        canvas_h=envelope[1] if envelope else 0,
        image1=None,
        image2=None,
    )


def test_preview_frame_without_a_store_still_refreshes_the_content_rect():
    """No store (preview tier) is not a reason to keep the outgoing rect."""
    # 16:9 rect left over from the previous pair (envelope 3840x2160 in a
    # 1024x450 canvas -> 800x450 centred at x=112).
    stale = (112, 0, 800, 450)
    state = _state(stale)
    canvas = _canvas(state)

    # New pair: both sides 1000x1000 -> a 450x450 square centred at x=287.
    refresh_live_content_rect(canvas, state, _plan((1000, 1000, 1000, 1000)))

    assert state._content_rect_px == (287, 0, 450, 450), (
        "a preview-tier frame must fit the plan's SOURCE envelope, not keep the "
        f"previous pair's rect (got {state._content_rect_px!r})"
    )


def test_plan_without_an_envelope_keeps_the_previous_rect():
    """No envelope and no store: nothing to fit, keep today's behaviour."""
    stale = (112, 0, 800, 450)
    state = _state(stale)
    canvas = _canvas(state)

    refresh_live_content_rect(canvas, state, _plan(None))

    assert state._content_rect_px == stale
