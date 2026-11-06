"""``ViewState.clone()`` must freeze ``canvas_widget_state`` content.

A recorded video frame holds ``store.viewport.freeze_for_export()`` — a
``ViewportState.clone()`` whose ``ViewState.clone()`` used to shallow-copy
``canvas_widget_state``, sharing every feature-state object with the live
tree. Moving the loupe or toggling the
diff after a frame was recorded then mutated that frame's snapshot in
place, so the timeline's atlas could show a frame that never existed.

These tests rebuild the live shape (a magnifier state with a non-empty
``models`` mapping — the state the recording path actually carries) and
mutate the ORIGINAL after cloning. They fail on a shallow copy because
the clone aliases the same nested objects.

The fail-closed test at the bottom is deliberate: if a value resists
copying, ``clone()`` must raise, never silently publish a shared reference.
"""

from __future__ import annotations

from collections import OrderedDict

import pytest

from core.store_viewport import ViewState, ViewportState
from domain.types import Point

from tabs.image_compare.canvas.features.magnifier.state.feature_state import (
    MagnifierWidgetState,
)
from tabs.image_compare.canvas.features.magnifier.state.models import MagnifierModel


def _live_magnifier_state() -> MagnifierWidgetState:
    return MagnifierWidgetState(
        enabled=True,
        active_id="default",
        models=OrderedDict([("default", MagnifierModel(id="default"))]),
    )


def test_clone_does_not_share_magnifier_state_with_the_source():
    live = ViewState(canvas_widget_state={"magnifier": _live_magnifier_state()})
    frozen = live.clone()

    assert frozen is not live
    assert frozen.canvas_widget_state is not live.canvas_widget_state
    assert frozen.canvas_widget_state["magnifier"] is not live.canvas_widget_state[
        "magnifier"
    ]


def test_mutating_the_source_magnifier_after_clone_leaves_the_clone_untouched():
    live = ViewState(canvas_widget_state={"magnifier": _live_magnifier_state()})
    frozen = live.clone()

    live_magnifier = live.canvas_widget_state["magnifier"]
    live_magnifier.enabled = False
    live_magnifier.active_id = None
    live_magnifier.models["default"].size_relative = 0.95
    live_magnifier.models["default"].visible = False
    live_magnifier.models["second"] = MagnifierModel(id="second")

    frozen_magnifier = frozen.canvas_widget_state["magnifier"]
    assert frozen_magnifier.enabled is True
    assert frozen_magnifier.active_id == "default"
    assert list(frozen_magnifier.models) == ["default"]
    assert frozen_magnifier.models["default"].size_relative == pytest.approx(0.2)
    assert frozen_magnifier.models["default"].visible is True


def test_mutating_the_clone_leaves_the_live_source_untouched():
    live = ViewState(canvas_widget_state={"magnifier": _live_magnifier_state()})
    frozen = live.clone()

    frozen.canvas_widget_state["magnifier"].models.clear()
    frozen.canvas_widget_state["magnifier"].enabled = False

    live_magnifier = live.canvas_widget_state["magnifier"]
    assert live_magnifier.enabled is True
    assert list(live_magnifier.models) == ["default"]


def test_freeze_for_export_freezes_canvas_widget_state():
    """The recorder stores ``store.viewport.freeze_for_export()`` per frame."""
    live_viewport = ViewportState(
        view_state=ViewState(canvas_widget_state={"magnifier": _live_magnifier_state()})
    )
    frame_snapshot = live_viewport.freeze_for_export()

    live_magnifier = live_viewport.view_state.canvas_widget_state["magnifier"]
    live_magnifier.models["default"].offset_relative = Point(0.5, 0.25)
    live_magnifier.models["default"].divider_visible = False

    frame_model = frame_snapshot.view_state.canvas_widget_state["magnifier"].models[
        "default"
    ]
    assert frame_model.offset_relative.x == pytest.approx(0.0)
    assert frame_model.divider_visible is True


def test_clone_of_plain_immutable_values_still_copies_and_compares_equal():
    live = ViewState(
        split_position=0.25,
        diff_mode="highlight",
        canvas_widget_state={"flag": True, "count": 3, "label": "named"},
    )
    frozen = live.clone()

    assert frozen is not live
    assert frozen == live
    assert frozen.canvas_widget_state == live.canvas_widget_state
    assert frozen.canvas_widget_state is not live.canvas_widget_state


def test_clone_fails_closed_on_an_uncopyable_feature_state():
    class Uncopyable:
        def __deepcopy__(self, memo):
            raise TypeError("cannot copy")

    live = ViewState(canvas_widget_state={"broken": Uncopyable()})

    with pytest.raises(TypeError):
        live.clone()
