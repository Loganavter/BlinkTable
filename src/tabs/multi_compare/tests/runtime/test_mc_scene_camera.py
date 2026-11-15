"""Scene camera (whole-composition zoom/pan) for Multi Compare.

One camera, two consumers: the render projection and hit-testing both go
through ``scene.projection.composition_frame_geometry``, so a Ctrl+wheel tick
must move the pixels *and* the leaf rects by the same amount — otherwise the
slot under the cursor would stop being the slot that zooms.

Red-before: on the pre-change tree ``MultiCompareState`` had no scene fields,
``composition_frame_geometry`` had no camera arguments, Ctrl+wheel fell into
the content-zoom path (dispatching ``multi_compare/set_zoom`` on the slot under
the cursor) and ``letterbox_transform`` kept its own copy of the letterbox
formula, so every assertion below about scene actions / camera-bearing geometry
fails there.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt

from tabs.multi_compare.canvas import interaction as mc_interaction
from tabs.multi_compare.models import CompareSlot, LeafNode, MultiCompareState
from tabs.multi_compare.scene import actions as mc_actions
from tabs.multi_compare.scene.projection import (
    build_render_context,
    composition_frame_geometry,
    scene_camera,
    scene_pan_limit,
)
from tabs.multi_compare.scene.store import MultiCompareStore, reduce
from tabs.multi_compare.ui import chrome, hit_projection
from ui.canvas_presentation.composition import (
    CompositionPlan,
    LayerNode,
    resolve_composition,
)

CANVAS = (800, 600)
WIDGET = (400, 300)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _WheelEvent:
    def __init__(self, delta, x=100.0, y=100.0, ctrl=True):
        self._delta = delta
        self._pos = QPointF(x, y)
        self._ctrl = ctrl
        self.accepted = False
        self.ignored = False

    def angleDelta(self):
        return SimpleNamespace(y=lambda: self._delta)

    def position(self):
        return self._pos

    def modifiers(self):
        return (
            Qt.KeyboardModifier.ControlModifier
            if self._ctrl
            else Qt.KeyboardModifier.NoModifier
        )

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


class _KeyEvent:
    def __init__(self, key, *, ctrl=False):
        self._key = key
        self._ctrl = ctrl
        self.accepted = False

    def key(self):
        return self._key

    def modifiers(self):
        return (
            Qt.KeyboardModifier.ControlModifier
            if self._ctrl
            else Qt.KeyboardModifier.NoModifier
        )

    def accept(self):
        self.accepted = True


class _MouseEvent:
    def __init__(self, pos, *, button=Qt.MouseButton.NoButton, ctrl=True):
        self._pos = QPointF(*pos)
        self._button = button
        self._ctrl = ctrl
        self.accepted = False

    def position(self):
        return self._pos

    def button(self):
        return self._button

    def buttons(self):
        return self._button

    def modifiers(self):
        return (
            Qt.KeyboardModifier.ControlModifier
            if self._ctrl
            else Qt.KeyboardModifier.NoModifier
        )

    def accept(self):
        self.accepted = True


def _state(**kwargs):
    base = dict(
        root=LeafNode(slot_id=1),
        slots=[CompareSlot(id=1)],
        is_focused=False,
        zoom=1.0,
        pan_x=0.0,
        pan_y=0.0,
        scene_zoom=1.0,
        scene_pan_x=0.0,
        scene_pan_y=0.0,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _scene_widget(*, state=None, size=WIDGET, canvas=CANVAS):
    """Fake canvas: real state fields, no leaf picking needed for scene math."""
    widget = SimpleNamespace(
        state=state or _state(),
        ZOOM_MIN=1.0,
        ZOOM_MAX=50.0,
        ZOOM_STEP=1.1,
        dispatched=[],
        _active_composition=SimpleNamespace(canvas_w=canvas[0], canvas_h=canvas[1]),
        _scene_panning=False,
        _panning=False,
    )
    widget._slot_sources = lambda: {}
    widget._leaf_rects = lambda: []
    widget.width = lambda: size[0]
    widget.height = lambda: size[1]
    widget.rect = lambda: QRect(0, 0, size[0], size[1])
    widget.setCursor = lambda _cursor: None
    widget.dispatched = []

    def _do_dispatch(action):
        widget.dispatched.append(action)
        widget.state = reduce_multistate(widget.state, action)

    widget._do_dispatch = _do_dispatch
    return widget


def reduce_multistate(state, action):
    """Apply ``reduce`` to a SimpleNamespace state (fake, not the dataclass)."""
    plain = MultiCompareState(
        slots=list(state.slots),
        root=state.root,
        zoom=state.zoom,
        pan_x=state.pan_x,
        pan_y=state.pan_y,
        scene_zoom=state.scene_zoom,
        scene_pan_x=state.scene_pan_x,
        scene_pan_y=state.scene_pan_y,
    )
    new = reduce(plain, action)
    state.zoom = new.zoom
    state.pan_x = new.pan_x
    state.pan_y = new.pan_y
    state.scene_zoom = new.scene_zoom
    state.scene_pan_x = new.scene_pan_x
    state.scene_pan_y = new.scene_pan_y
    return state


def _composition(width=CANVAS[0], height=CANVAS[1]):
    return resolve_composition(
        CompositionPlan(
            root=LayerNode(layer_id=1, image=None, zoom=1.0),
            canvas_w=width,
            canvas_h=height,
        )
    )


def _viewport_a(widget, zoom):
    """Fraction of the framebuffer the composition covers (test-side mirror)."""
    fb_w = float(widget.width())
    fb_h = float(widget.height())
    canvas_w = float(widget._active_composition.canvas_w)
    canvas_h = float(widget._active_composition.canvas_h)
    fit = min(fb_w / canvas_w, fb_h / canvas_h)
    return (canvas_w * fit * zoom / fb_w, canvas_h * fit * zoom / fb_h)


# ---------------------------------------------------------------------------
# geometry: one camera for pixels and for hit-testing
# ---------------------------------------------------------------------------


def test_scene_camera_reads_identity_off_a_state_without_camera_fields():
    assert scene_camera(None) == (1.0, 0.0, 0.0)
    assert scene_camera(SimpleNamespace(zoom=3.0)) == (1.0, 0.0, 0.0)
    # Out-of-range values are repaired, not propagated: < 1 would show void and
    # a pan past the window would show the frame edge.
    assert scene_camera(SimpleNamespace(scene_zoom=0.5)) == (1.0, 0.0, 0.0)
    assert scene_camera(
        SimpleNamespace(scene_zoom=2.0, scene_pan_x=5.0, scene_pan_y=-5.0)
    ) == pytest.approx((2.0, scene_pan_limit(2.0), -scene_pan_limit(2.0)))


def test_composition_frame_geometry_folds_the_camera_into_scale_and_offset():
    canvas = (800.0, 600.0)
    frame = (400.0, 300.0)
    base_scale, base_ox, base_oy = composition_frame_geometry(
        canvas_size=canvas, frame_size=frame
    )
    scale, ox, oy = composition_frame_geometry(
        canvas_size=canvas, frame_size=frame, scene_zoom=2.0
    )
    assert scale == pytest.approx(base_scale * 2.0)
    # Zoomed about the frame center: the centering term shrinks, the pan term
    # is the camera's (canvas-uv units scaled by the composition's frame size).
    assert ox == pytest.approx((frame[0] - canvas[0] * scale) * 0.5)
    assert oy == pytest.approx((frame[1] - canvas[1] * scale) * 0.5)
    scale_pan, ox_pan, oy_pan = composition_frame_geometry(
        canvas_size=canvas, frame_size=frame, scene_zoom=2.0, scene_pan=(0.1, -0.05)
    )
    assert scale_pan == scale
    assert ox_pan == pytest.approx(ox + 0.1 * canvas[0] * scale)
    assert oy_pan == pytest.approx(oy - 0.05 * canvas[1] * scale)
    # Identity camera reproduces the plain letterbox byte-for-byte.
    assert (base_scale, base_ox, base_oy) == composition_frame_geometry(
        canvas_size=canvas, frame_size=frame, scene_zoom=1.0, scene_pan=(0.0, 0.0)
    )


@pytest.mark.parametrize(
    "camera", ((1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (2.5, 0.12, -0.07))
)
def test_hit_projection_agrees_with_build_render_context_under_the_camera(camera):
    """A canvas rect and a leaf rect land on the same framebuffer rect.

    This is the seam that used to carry two copies of the letterbox formula:
    with a scene camera folded into ``ctx.scale/offset`` only, the old
    hit-testing copy kept the identity geometry and every cursor-to-slot pick
    drifted by the zoom factor.
    """
    zoom, pan_x, pan_y = camera
    state = _state(scene_zoom=zoom, scene_pan_x=pan_x, scene_pan_y=pan_y)
    widget = _scene_widget(state=state)
    composition = _composition()

    ctx = build_render_context(
        composition=composition,
        framebuffer_size=(float(WIDGET[0]), float(WIDGET[1])),
        clip_matrix=(),
        available_slot_ids=frozenset({1}),
        widget=widget,
    )
    layout = hit_projection.canvas_layout(widget)
    assert layout is not None
    canvas_w, canvas_h, sr, ox, oy = layout
    assert canvas_w == CANVAS[0] and canvas_h == CANVAS[1]
    assert sr == pytest.approx(ctx.scale)
    assert (ox, oy) == pytest.approx(ctx.offset)

    # The single leaf covers the whole canvas: its widget-px rect must equal
    # the renderer's projected layer rect (both derived from the same camera).
    leaves = hit_projection.leaf_rects(widget)
    assert len(leaves) == 1
    _leaf, rect = leaves[0]
    layer_rect = ctx.projected_layers[0].rect_fb
    assert rect.x() == pytest.approx(layer_rect[0], abs=0.5)
    assert rect.y() == pytest.approx(layer_rect[1], abs=0.5)
    assert rect.width() == pytest.approx(layer_rect[2], abs=0.5)
    assert rect.height() == pytest.approx(layer_rect[3], abs=0.5)

    # And the whole-canvas rect projects to the same place, i.e. hit-testing
    # and rendering describe one geometry.
    whole = hit_projection.project_canvas_rect(
        QRect(0, 0, canvas_w, canvas_h), sr, ox, oy
    )
    assert whole.x() == pytest.approx(layer_rect[0], abs=0.5)
    assert whole.y() == pytest.approx(layer_rect[1], abs=0.5)


def test_export_projection_ignores_the_scene_camera():
    """The export canvas renders identity: save-view crops a native frame."""
    state = _state(scene_zoom=2.0, scene_pan_x=0.1)
    widget = _scene_widget(state=state)
    widget._export_canvas_viewport = (800, 600, 0, 0)
    ctx = build_render_context(
        composition=_composition(),
        framebuffer_size=(800.0, 600.0),
        clip_matrix=(),
        available_slot_ids=frozenset({1}),
        widget=widget,
    )
    assert ctx.scale == pytest.approx(1.0)
    assert ctx.offset == pytest.approx((0.0, 0.0))


# ---------------------------------------------------------------------------
# Ctrl+wheel: scene zoom, anchored under the cursor
# ---------------------------------------------------------------------------


def _canvas_uv_under_cursor(widget, pos, zoom, pan_x, pan_y):
    """Composition point under a widget-px cursor (the camera inverse)."""
    a_x, a_y = _viewport_a(widget, zoom)
    s_x = pos[0] / float(widget.width())
    s_y = pos[1] / float(widget.height())
    return ((s_x - 0.5) / a_x - pan_x, (s_y - 0.5) / a_y - pan_y)


def _cursor_for_canvas_uv(widget, uv, zoom, pan_x, pan_y):
    a_x, a_y = _viewport_a(widget, zoom)
    return (
        (a_x * (uv[0] + pan_x) + 0.5) * float(widget.width()),
        (a_y * (uv[1] + pan_y) + 0.5) * float(widget.height()),
    )


@pytest.mark.parametrize(
    "start_zoom,start_pan,delta,cursor",
    (
        (1.0, (0.0, 0.0), 120, (100.0, 90.0)),
        (2.5, (0.12, -0.07), 120, (320.0, 40.0)),
        (2.0, (-0.1, 0.05), -120, (60.0, 260.0)),
    ),
)
def test_ctrl_wheel_keeps_the_canvas_point_under_the_cursor(
    start_zoom, start_pan, delta, cursor
):
    state = _state(
        scene_zoom=start_zoom, scene_pan_x=start_pan[0], scene_pan_y=start_pan[1]
    )
    widget = _scene_widget(state=state)
    uv = _canvas_uv_under_cursor(widget, cursor, start_zoom, *start_pan)

    mc_interaction.handle_wheel_event(widget, _WheelEvent(delta, *cursor))

    assert len(widget.dispatched) == 1
    action = widget.dispatched[0]
    assert action.type == "multi_compare/set_scene_zoom"
    after = widget.state
    pinned = _cursor_for_canvas_uv(
        widget, uv, after.scene_zoom, after.scene_pan_x, after.scene_pan_y
    )
    assert pinned[0] == pytest.approx(cursor[0], abs=0.5)
    assert pinned[1] == pytest.approx(cursor[1], abs=0.5)


def test_ctrl_wheel_uses_the_scene_zoom_step_and_clamps_to_its_range():
    widget = _scene_widget()
    mc_interaction.handle_wheel_event(widget, _WheelEvent(240, 100.0, 100.0))
    assert len(widget.dispatched) == 1
    assert widget.dispatched[0].zoom == pytest.approx(widget.ZOOM_STEP**2)
    widget = _scene_widget()
    mc_interaction.handle_wheel_event(widget, _WheelEvent(120 * 400, 100.0, 100.0))
    assert widget.dispatched[0].zoom == pytest.approx(widget.ZOOM_MAX)


def test_ctrl_wheel_clamps_the_anchor_pan_so_no_void_is_shown():
    widget = _scene_widget()
    # Cursor in the top-left corner: zooming there walks the visible window
    # toward the composition's top-left, and the uncapped anchor would push it
    # past the composition's own edge (+ -> the window centre moves to
    # canvas_uv 0.5 - pan, i.e. up-left).
    mc_interaction.handle_wheel_event(widget, _WheelEvent(120 * 8, 0.0, 0.0))
    action = widget.dispatched[0]
    limit = scene_pan_limit(action.zoom)
    assert action.pan_x == pytest.approx(limit)
    assert action.pan_y == pytest.approx(limit)
    assert abs(action.pan_x) <= limit + 1e-12
    # The window stays inside the canvas: no void at either edge.
    assert 0.5 - limit - 0.5 / action.zoom == pytest.approx(0.0)


def test_ctrl_wheel_snaps_pan_to_origin_at_the_zoom_floor():
    state = _state(scene_zoom=1.05, scene_pan_x=0.02, scene_pan_y=-0.01)
    widget = _scene_widget(state=state)
    mc_interaction.handle_wheel_event(widget, _WheelEvent(-120, 200.0, 150.0))
    action = widget.dispatched[0]
    assert action.zoom == pytest.approx(1.0)
    assert (action.pan_x, action.pan_y) == (0.0, 0.0)
    assert widget.state.scene_zoom == pytest.approx(1.0)


def test_ctrl_wheel_swallows_invisible_ticks():
    widget = _scene_widget()  # already at the floor
    event = _WheelEvent(-120, 200.0, 150.0)
    mc_interaction.handle_wheel_event(widget, event)
    assert widget.dispatched == []
    assert event.accepted


def test_plain_wheel_still_zooms_the_slot_content():
    state = _state()
    widget = _scene_widget(state=state)
    widget._leaf_rects = lambda: [(LeafNode(1), QRect(0, 0, 400, 300))]
    event = _WheelEvent(120, 200.0, 150.0, ctrl=False)
    mc_interaction.handle_wheel_event(widget, event)
    assert len(widget.dispatched) == 1
    assert widget.dispatched[0].type == "multi_compare/set_zoom"
    assert widget.state.scene_zoom == 1.0


# ---------------------------------------------------------------------------
# Ctrl+drag: scene pan
# ---------------------------------------------------------------------------


def test_ctrl_drag_pans_the_scene_and_never_the_content(monkeypatch):
    monkeypatch.setattr(mc_interaction, "iter_active", lambda _w: ())
    state = _state(scene_zoom=2.0)
    widget = _scene_widget(state=state)

    mc_interaction.handle_mouse_press_event(
        widget, _MouseEvent((100.0, 100.0), button=Qt.MouseButton.MiddleButton)
    )
    assert widget._scene_panning is True
    assert widget._panning is False

    mc_interaction.handle_mouse_move_event(widget, _MouseEvent((140.0, 100.0)))
    assert len(widget.dispatched) == 1
    action = widget.dispatched[0]
    assert action.type == "multi_compare/set_scene_pan"
    assert not any(a.type == "multi_compare/set_pan" for a in widget.dispatched)
    # 40 px over an 800-px-wide composition at scene zoom 2 (canvas 800 in a
    # 400-px widget) == 0.05 of the composition.
    assert action.pan_x == pytest.approx(0.05)
    assert action.pan_y == pytest.approx(0.0)

    mc_interaction.handle_mouse_release_event(
        widget, _MouseEvent((140.0, 100.0), button=Qt.MouseButton.MiddleButton)
    )
    assert widget._scene_panning is False


def test_plain_middle_drag_still_pans_the_slot_content(monkeypatch):
    monkeypatch.setattr(mc_interaction, "iter_active", lambda _w: ())
    state = _state(zoom=2.0)
    widget = _scene_widget(state=state)
    widget._pan_ref_rect = QRect(0, 0, 400, 300)
    widget._pan_ref_fit = (1.0, 1.0)

    mc_interaction.handle_mouse_press_event(
        widget,
        _MouseEvent((100.0, 100.0), button=Qt.MouseButton.MiddleButton, ctrl=False),
    )
    assert widget._panning is True
    assert widget._scene_panning is False
    mc_interaction.handle_mouse_move_event(
        widget, _MouseEvent((140.0, 100.0), ctrl=False)
    )
    assert [a.type for a in widget.dispatched] == ["multi_compare/set_pan"]


# ---------------------------------------------------------------------------
# keyboard parity
# ---------------------------------------------------------------------------


def test_ctrl_plus_and_minus_zoom_the_scene_camera():
    widget = _scene_widget(state=_state(scene_zoom=2.0, scene_pan_x=0.1))
    mc_interaction.handle_key_press_event(widget, _KeyEvent(Qt.Key.Key_Plus, ctrl=True))
    action = widget.dispatched[-1]
    assert action.type == "multi_compare/set_scene_zoom"
    assert action.zoom == pytest.approx(2.2)
    assert action.pan_y == 0.0
    widget = _scene_widget(state=_state(scene_zoom=1.05, scene_pan_x=0.02))
    mc_interaction.handle_key_press_event(
        widget, _KeyEvent(Qt.Key.Key_Minus, ctrl=True)
    )
    action = widget.dispatched[-1]
    assert action.zoom == pytest.approx(1.0)
    assert (action.pan_x, action.pan_y) == (0.0, 0.0)


def test_ctrl_arrows_pan_the_scene_camera():
    widget = _scene_widget(state=_state(scene_zoom=2.0))
    mc_interaction.handle_key_press_event(
        widget, _KeyEvent(Qt.Key.Key_Right, ctrl=True)
    )
    action = widget.dispatched[-1]
    assert action.type == "multi_compare/set_scene_pan"
    assert action.pan_x == pytest.approx(0.025)  # KEY_PAN_NUDGE / zoom
    assert action.pan_y == 0.0
    # At the floor there is nothing to pan (the whole composition is in view).
    widget = _scene_widget()
    mc_interaction.handle_key_press_event(widget, _KeyEvent(Qt.Key.Key_Left, ctrl=True))
    assert widget.dispatched == []


def test_ctrl_zero_resets_and_plain_zero_still_resets():
    widget = _scene_widget(state=_state(scene_zoom=2.0, scene_pan_x=0.1))
    mc_interaction.handle_key_press_event(widget, _KeyEvent(Qt.Key.Key_0, ctrl=True))
    assert widget.dispatched[-1].type == "multi_compare/reset_view"
    assert widget.state.scene_zoom == 1.0 and widget.state.scene_pan_x == 0.0
    widget = _scene_widget(state=_state(zoom=2.0))
    mc_interaction.handle_key_press_event(widget, _KeyEvent(Qt.Key.Key_0))
    assert widget.dispatched[-1].type == "multi_compare/reset_view"


# ---------------------------------------------------------------------------
# state: reducer rules
# ---------------------------------------------------------------------------


def test_scene_actions_round_trip_and_ignore_identical_payloads():
    state = MultiCompareState()
    zoomed = reduce(state, mc_actions.set_scene_zoom(2.0, 0.1, -0.1))
    assert (zoomed.scene_zoom, zoomed.scene_pan_x, zoomed.scene_pan_y) == (
        2.0,
        0.1,
        -0.1,
    )
    assert reduce(zoomed, mc_actions.set_scene_zoom(2.0, 0.1, -0.1)) is zoomed
    panned = reduce(zoomed, mc_actions.set_scene_pan(0.05, 0.0))
    assert (panned.scene_pan_x, panned.scene_pan_y) == (0.05, 0.0)
    assert reduce(panned, mc_actions.set_scene_pan(0.05, 0.0)) is panned
    # Content camera untouched by scene actions (and vice versa).
    assert (panned.zoom, panned.pan_x, panned.pan_y) == (1.0, 0.0, 0.0)


def test_reset_view_clears_both_cameras_and_is_a_no_op_at_identity():
    state = reduce(MultiCompareState(), mc_actions.set_zoom(2.0, 0.1, 0.0))
    state = reduce(state, mc_actions.set_scene_zoom(3.0, 0.2, 0.0))
    reset = reduce(state, mc_actions.reset_view())
    assert (reset.zoom, reset.pan_x, reset.pan_y) == (1.0, 0.0, 0.0)
    assert (reset.scene_zoom, reset.scene_pan_x, reset.scene_pan_y) == (
        1.0,
        0.0,
        0.0,
    )
    identity = MultiCompareState()
    assert reduce(identity, mc_actions.reset_view()) is identity


def test_store_dispatch_carries_the_scene_camera():
    store = MultiCompareStore()
    store.dispatch(mc_actions.set_scene_zoom(2.0, 0.1, 0.0))
    assert store.state.scene_zoom == pytest.approx(2.0)
    store.dispatch(mc_actions.reset_view())
    assert store.state.scene_zoom == 1.0
    assert store.state.scene_pan_x == 0.0


# ---------------------------------------------------------------------------
# indicator
# ---------------------------------------------------------------------------


def _indicator_widget(**state_kwargs):
    updates = []
    indicator = SimpleNamespace(
        update_zoom=lambda z, px=0.0, py=0.0: updates.append((z, px, py)),
        _lang_provider=lambda: "en",
        _target_widget=None,
    )
    widget = SimpleNamespace(
        zoom_indicator=indicator,
        store=SimpleNamespace(state=_state(**state_kwargs)),
    )
    return widget, updates


def test_indicator_shows_the_scene_zoom_when_the_camera_is_off_identity():
    widget, updates = _indicator_widget(
        zoom=2.0, scene_zoom=3.0, scene_pan_x=0.1, scene_pan_y=0.0
    )
    chrome.sync_zoom_indicator(widget)
    assert updates[-1] == pytest.approx((3.0, 0.1, 0.0))


def test_indicator_falls_back_to_the_content_zoom_and_keeps_its_memo():
    widget, updates = _indicator_widget(zoom=2.0, pan_x=0.1)
    chrome.sync_zoom_indicator(widget)
    chrome.sync_zoom_indicator(widget)
    assert updates == [pytest.approx((2.0, 0.1, 0.0))]
    # A scene zoom now takes over the chip (and is not swallowed by the memo).
    widget.store.state.scene_zoom = 1.5
    chrome.sync_zoom_indicator(widget)
    assert updates[-1][0] == pytest.approx(1.5)


def test_indicator_visibility_covers_a_panned_scene_at_identity_content():
    widget, updates = _indicator_widget(scene_zoom=1.0, scene_pan_x=0.0)
    chrome.sync_zoom_indicator(widget)
    # Nothing visible: the chip is updated with the identity camera and hides
    # itself through the indicator's own visibility rule.
    assert updates[-1] == pytest.approx((1.0, 0.0, 0.0))
