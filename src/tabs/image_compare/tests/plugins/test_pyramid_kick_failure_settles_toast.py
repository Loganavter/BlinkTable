"""A failed pyramid kick must not leave the loading toast hanging.

Owner log (2026-11-15): every kick raised
``AttributeError: '_SharedView' object has no attribute '_memmap'`` inside the
registry sweep; the seam logged "pyramid build kick failed" at debug and the
"loading full version of image" toast stayed at its first progress value
forever. The store race is fixed at its source
(``tests/render/test_pixel_store_is_open_closed.py``); this module pins the
second half: a kick that cannot start must settle the toast instead of leaving
it to wait for a progress event that can never arrive.
"""

from __future__ import annotations

from types import SimpleNamespace

from tabs.image_compare.use_cases import loading_pyramid


class _RaisingCoordinator:
    """A coordinator whose kick blows up the way the sweep did."""

    def start_build(self, store, *, slot_id=None, toast_live=True, should_abort=None):
        raise AttributeError("'_SharedView' object has no attribute '_memmap'")


def _controller(*, coordinator, finished):
    pipeline = SimpleNamespace(_inflight={})
    return SimpleNamespace(
        _pyramid_coordinator=coordinator,
        pipeline=pipeline,
        store=SimpleNamespace(
            viewport=SimpleNamespace(
                session_data=SimpleNamespace(image_state=SimpleNamespace(image1=None, image2=None))
            )
        ),
        presenter=None,
        _finish_loading_toast=finished.append,
    )


def test_a_raising_kick_reports_false_and_finishes_the_toast():
    finished: list[int] = []
    controller = _controller(coordinator=_RaisingCoordinator(), finished=finished)

    kicked = loading_pyramid.demand_fit_level(controller, object(), 1)

    assert kicked is False, "a kick that raised cannot report a started build"
    assert finished == [1], "the live toast must be settled, not left waiting forever"


def test_a_raising_kick_with_no_live_toast_touches_nothing():
    finished: list[int] = []
    controller = _controller(coordinator=_RaisingCoordinator(), finished=finished)
    # An in-flight load for the slot makes the toast non-live: nothing to settle.
    controller.pipeline._inflight = {(1, "sig"): SimpleNamespace(is_aborted=lambda: False)}

    kicked = loading_pyramid.demand_fit_level(controller, object(), 1)

    assert kicked is False
    assert finished == []
