"""``get_canvas_scene`` — the canvas scene read verb (S1 of the scene API plan).

Every case is built from **real widgets and real builders**, never from a fake
scene object: the Image Compare canvas is a real ``CanvasWidget`` whose scene
graph is produced by the real ``build_magnifier_objects`` / ``build_magnifier_layout``,
and the Multi Compare canvas is a real ``MultiCompareCanvasWidget`` whose
composition is produced by the app's own ``_rebuild_composition`` (which calls
``build_composition_plan`` + ``resolve_composition``).

Falsification (proven RED on the pre-change tree; see the run log in
``DONE.md``): the verb, the tool and ``scene_read.py`` do not exist there, so
every test below fails at the first ``get_canvas_scene`` call — nothing here
restates an implementation that was already present.

Contract points covered:

* R1 — two loupe truths: every instance is listed (hidden ones flagged) and a
  hidden loupe reports **no** geometry, with the source of every field named;
* R2 — the read leaves the session byte-identical (populated *and* pristine);
* R3 — Multi Compare's transform equals the projection's own output, and the
  pane set is reported twice: painted composition layers + hit-test-only leaves
  with their exclusion reason;
* R4 — every geometry field carries its coordinate space;
* R5 — two reads of one scene are byte-identical;
* R6 — a tab without a canvas answers honestly;
* R7 — roles are per circle, and no per-loupe zoom field is invented.

Runs offscreen only (the supervisor runs the live proof).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
import dataclasses
from types import SimpleNamespace

import pytest

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

from core.control import scene_read
from devtools.canvas_scene import (
    SPACE_CANVAS_PX,
    SPACE_WIDGET_PX,
    SPACE_COMPOSITION_PX,
    SPACE_RELATIVE,
)
from core.control.handlers import ControlHandlers
from core.store import Store
from ui.actions.registry import ActionRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = REPO_ROOT / "mcp-server"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from app import catalog  # noqa: E402
from app.backend import AppBackend, ToolError  # noqa: E402
from app.live import LiveBackend  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# fixtures: real canvases, real builders
# ---------------------------------------------------------------------------


def _ic_fixture(qapp, *, with_plan: bool = False):
    """A real Image Compare canvas + store with two loupes (one hidden)."""
    from domain.types import Rect
    from tabs.image_compare.canvas.features.magnifier.geometry.layout_plan import (
        build_magnifier_layout,
    )
    from tabs.image_compare.canvas.features.magnifier.scene.build import (
        build_magnifier_objects,
    )
    from tabs.image_compare.canvas.features.magnifier.state.store import (
        add_magnifier_model,
        set_active_magnifier_id,
        update_magnifier_model,
    )
    from tabs.image_compare.canvas.scene import build_render_scene
    from tabs.image_compare.canvas.widget import CanvasWidget
    from tabs.image_compare.tab import ImageCompareTab
    from ui.canvas_infra.scene.context import CanvasSceneBuildContext
    from ui.canvas_infra.scene.models import CanvasSceneGraph
    from ui.canvas_presentation.plan import CanvasRenderPlan

    ImageCompareTab().register_canvas_features()

    store = Store()
    viewport = store.viewport
    viewport.geometry_state.pixmap_width = 640
    viewport.geometry_state.pixmap_height = 400
    view_state = viewport.view_state

    add_magnifier_model(view_state, viewport.render_config, "loupe-a")
    add_magnifier_model(view_state, viewport.render_config, "loupe-b")
    update_magnifier_model(view_state, viewport.render_config, "loupe-b", visible=False)
    set_active_magnifier_id(view_state, "loupe-a")

    canvas = CanvasWidget()
    canvas.resize(800, 600)
    canvas.runtime_state._store = store

    context = CanvasSceneBuildContext(
        store=store,
        image_label=canvas,
        bounds=Rect(0, 0, 640, 400),
        label_width=800,
        label_height=600,
        pix_w=640,
        pix_h=400,
    )
    graph = CanvasSceneGraph(
        bounds=Rect(0, 0, 640, 400),
        objects=tuple(build_magnifier_objects(context)),
        active_object_id="loupe-a",
    )
    # Exactly what ``workers/scene_update.py:53-55`` parks on the label after a
    # rebuild — the read consumes that cache, it does not rebuild the scene.
    canvas.runtime_state._canvas_scene_graph = graph

    if with_plan:
        layout = build_magnifier_layout(
            viewport,
            width=640,
            height=400,
            canvas_width=640,
            canvas_height=400,
            divider_thickness_px=3,
        )
        canvas._active_render_plan = CanvasRenderPlan(
            image1=None,
            image2=None,
            source_image1=None,
            source_image2=None,
            source_key=(),
            canvas_w=640,
            canvas_h=400,
            render_scene=build_render_scene(store, apply_channel_mode_in_shader=True),
            overlay_layout=layout,
            capture_visible=False,
            capture_color=None,
            guides_enabled=False,
            guides_color=None,
            guides_thickness=0,
        )

    page = QWidget()
    canvas.setParent(page)
    return SimpleNamespace(store=store, canvas=canvas, page=page, view_state=view_state)


def _mc_fixture(qapp, *, focused_slot_id: int | None = None):
    """A real Multi Compare canvas whose composition the app itself resolved.

    Slot 3 is imageless (no cached tier) and slot 1/2 carry real preview
    images, so the composition builder's own imageless-leaf policy drops 3 —
    that is the second pane set R3 demands be reported, not silently ignored.
    """
    from tabs.multi_compare.models import (
        CompareSlot,
        LeafNode,
        MultiCompareState,
        SplitNode,
    )
    from tabs.multi_compare.pipeline.cache import MultiComparePixelCache
    from tabs.multi_compare.ui.canvas_widget import MultiCompareCanvasWidget

    canvas = MultiCompareCanvasWidget()
    canvas.resize(900, 500)
    canvas.state = MultiCompareState(
        slots=[
            CompareSlot(id=1, path=Path("/fixtures/left.png"), label="left"),
            CompareSlot(id=2, path=Path("/fixtures/right.png"), label="right"),
            CompareSlot(id=3, path=Path("/fixtures/ghost.png"), label="ghost"),
        ],
        root=SplitNode(
            direction="h",
            children=[
                LeafNode(1),
                SplitNode(
                    direction="v", children=[LeafNode(2), LeafNode(3)], weights=[0.5, 0.5]
                ),
            ],
            weights=[0.6, 0.4],
        ),
        focused_slot_id=focused_slot_id,
    )
    cache = MultiComparePixelCache()
    for fixture in ("/fixtures/left.png", "/fixtures/right.png"):
        image = QImage(64, 48, QImage.Format.Format_RGB32)
        image.fill(QColor("#3366cc"))
        cache.put_preview(fixture, image)
    canvas.pixel_cache = cache
    canvas._rebuild_composition()
    assert canvas._active_composition is not None, "fixture must resolve a composition"

    page = QWidget()
    canvas.setParent(page)
    return SimpleNamespace(store=None, canvas=canvas, page=page)


def _handlers(tmp_path, *, page, kinds=("image_compare", "multi_compare", "image_gallery")):
    """ControlHandlers over a real page, with tab discovery pinned to it."""

    def _tab(canvas_owner: bool):
        class _Contract:
            def get_canvas_geometry_provider(self):
                return SimpleNamespace() if canvas_owner else None

        return _Contract()

    tabs = [(kind, _tab(kind != "image_gallery")) for kind in kinds]
    handlers = ControlHandlers(
        window=SimpleNamespace(store=None),
        app=None,
        artifacts_dir=tmp_path / "artifacts",
        registry_provider=lambda: ActionRegistry(),
    )
    handlers._tabs = lambda: tabs  # type: ignore[assignment]
    return handlers


def _read(handlers, monkeypatch, *, page, tab: str) -> dict:
    monkeypatch.setattr(
        scene_read, "page_widget", lambda _handlers, _kind: page
    )
    return handlers.handle("get_canvas_scene", {"tab": tab})


def _fingerprint(canvas, store) -> str:
    """Canonical session snapshot: state, zoom state, scene graph, widget attrs."""
    view = None
    workspace = None
    if store is not None:
        viewport = getattr(store, "viewport", None)
        view = getattr(viewport, "view_state", None)
        workspace = getattr(store, "workspace", None)
    canvas_widget_state = getattr(view, "canvas_widget_state", None) or {}
    runtime_state = getattr(canvas, "runtime_state", None)
    return json.dumps(
        {
            "canvas_widget_state": {
                str(key): repr(value)
                for key, value in sorted(canvas_widget_state.items(), key=lambda kv: str(kv[0]))
            },
            "zoom_state": repr(getattr(runtime_state, "_zoom_viewport_state", None)),
            "scene_graph": repr(getattr(runtime_state, "_canvas_scene_graph", None)),
            "content_sr": repr(getattr(runtime_state, "_content_sr", None)),
            "widget_attrs": sorted(getattr(canvas, "__dict__", {})),
            "mc_state": repr(getattr(canvas, "state", None)),
            "view_state_attrs": sorted(getattr(view, "__dict__", {})),
            "sessions": [session.id for session in (getattr(workspace, "sessions", None) or [])],
            "active_session": getattr(workspace, "active_session_id", None),
        },
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# (a) every magnifier instance, hidden ones flagged — R1/R6/R7
# ---------------------------------------------------------------------------


def test_ic_scene_lists_every_magnifier_instance_including_hidden(qapp, tmp_path, monkeypatch):
    fixture = _ic_fixture(qapp)
    handlers = _handlers(tmp_path, page=fixture.page)

    scene = _read(handlers, monkeypatch, page=fixture.page, tab="image_compare")

    assert scene["scene_source"] == "ic_scene"
    assert scene["has_canvas"] is True
    assert scene["space"] == SPACE_CANVAS_PX
    assert [row["id"] for row in scene["magnifiers"]] == ["loupe-a", "loupe-b"]

    visible, hidden = scene["magnifiers"]
    assert visible["visible"] is True and visible["active"] is True
    assert hidden["visible"] is False, "a hidden instance must be reported, not dropped"

    # The visible one carries real scene-graph geometry...
    assert visible["geometry_source"] == "scene_graph"
    # Scene-graph geometry is authored in WIDGET px, not canvas px: the same
    # quantity read 24.2 here vs 115 in render_layout until 2026-10-27.
    assert visible["space"] == SPACE_WIDGET_PX
    assert visible["capture"]["space"] == SPACE_WIDGET_PX
    assert "projection.scale" in visible["capture"]["space_note"]
    assert visible["capture"] is not None
    assert {circle["role"] for circle in visible["circles"]} == {"left", "right"}

    # ...the hidden one must NOT borrow any (R1).
    assert hidden["geometry_source"] is None
    assert hidden["circles"] == []
    assert hidden["capture"] is None
    assert "hidden" in hidden["geometry_unavailable_reason"]

    # R7: roles are per circle, and no per-instance zoom is invented.
    for row in scene["magnifiers"]:
        assert "zoom" not in row, "there is no per-loupe zoom field to report"
        for circle in row["circles"]:
            assert circle["role"] in {"diff", "combined", "left", "right", "center"}
            assert circle["space"] == SPACE_WIDGET_PX

    # R4: every geometry block names its space.
    assert scene["canvas"]["space"] == SPACE_CANVAS_PX
    assert {pane["space"] for pane in scene["panes"]} == {SPACE_CANVAS_PX}
    assert [pane["slot"] for pane in scene["panes"]] == [1, 2]
    assert scene["dividers"][0]["space"] == SPACE_CANVAS_PX
    assert scene["dividers"][0]["thickness_space"] == "widget_px", (
        "the divider width setting is a widget-px value, not canvas px"
    )
    assert scene["transform"]["camera"]["space"] == SPACE_RELATIVE


def test_ic_two_loupe_truths_are_declared_with_sources(qapp, tmp_path, monkeypatch):
    """R1: the painted overlay (visible only) and the scene graph (all) disagree."""
    fixture = _ic_fixture(qapp, with_plan=True)
    handlers = _handlers(tmp_path, page=fixture.page)

    scene = _read(handlers, monkeypatch, page=fixture.page, tab="image_compare")

    render_layout = scene["render_layout"]
    assert render_layout["available"] is True
    assert render_layout["source"] == "ic_overlay_layout"
    assert render_layout["space"] == SPACE_CANVAS_PX
    # Only the visible loupe's two circles are painted...
    assert render_layout["slot_count"] == 2
    # ...while the scene graph keeps both instances, hidden one flagged.
    assert len(scene["magnifiers"]) == 2
    assert scene["sources"]["magnifier_geometry"] == "scene_graph (visible instances)"
    assert scene["sources"]["render_layout"] == "ic_overlay_layout"
    # The aggregate cannot be mapped back per instance, and says so.
    assert render_layout["per_instance_mapping"] == "unavailable"


# ---------------------------------------------------------------------------
# (b) the read must not mutate the session — R2
# ---------------------------------------------------------------------------


def test_ic_scene_read_leaves_the_session_unchanged(qapp, tmp_path, monkeypatch):
    fixture = _ic_fixture(qapp, with_plan=True)
    handlers = _handlers(tmp_path, page=fixture.page)

    before = _fingerprint(fixture.canvas, fixture.store)
    _read(handlers, monkeypatch, page=fixture.page, tab="image_compare")
    after = _fingerprint(fixture.canvas, fixture.store)

    assert after == before, "get_canvas_scene mutated the session"


def test_ic_scene_read_does_not_materialize_lazy_state(qapp, tmp_path, monkeypatch):
    """A pristine session stays pristine: no lazy magnifier/divider/zoom state."""
    from tabs.image_compare.canvas.widget import CanvasWidget

    store = Store()
    store.viewport.geometry_state.pixmap_width = 640
    store.viewport.geometry_state.pixmap_height = 400
    canvas = CanvasWidget()
    canvas.resize(800, 600)
    canvas.runtime_state._store = store
    # Drop the state ``init_widget_state`` materialized, so a creating read is
    # observable: ``ensure_zoom_viewport_state`` would put all of it back.
    del canvas.runtime_state._zoom_viewport_state
    for attribute in ("zoom_level", "pan_offset_x", "pan_offset_y", "display_split_position"):
        if hasattr(canvas, attribute):
            delattr(canvas, attribute)
    assert getattr(store.viewport.view_state, "canvas_widget_state", None) in (None, {})

    page = QWidget()
    canvas.setParent(page)
    handlers = _handlers(tmp_path, page=page)

    before = _fingerprint(canvas, store)
    scene = _read(handlers, monkeypatch, page=page, tab="image_compare")
    after = _fingerprint(canvas, store)

    assert after == before, "the read created lazy state (R2)"
    assert not hasattr(canvas.runtime_state, "_zoom_viewport_state")
    assert getattr(store.viewport.view_state, "canvas_widget_state", None) in (None, {})
    assert scene["magnifiers"] == [], "no instances exist, so none may be invented"
    assert scene["transform"]["camera"]["state_materialized"] is False
    assert scene["dividers"][0]["state_materialized"] is False
    assert scene["sources"]["transform"] == "widget_zoom_attributes_or_defaults"


def test_mc_scene_read_leaves_the_state_unchanged(qapp, tmp_path, monkeypatch):
    fixture = _mc_fixture(qapp)
    handlers = _handlers(tmp_path, page=fixture.page)

    before = _fingerprint(fixture.canvas, fixture.store)
    _read(handlers, monkeypatch, page=fixture.page, tab="multi_compare")
    after = _fingerprint(fixture.canvas, fixture.store)

    assert after == before


# ---------------------------------------------------------------------------
# (c) determinism — R5
# ---------------------------------------------------------------------------


def test_scene_json_is_byte_identical_across_two_reads(qapp, tmp_path, monkeypatch):
    ic = _ic_fixture(qapp, with_plan=True)
    mc = _mc_fixture(qapp)

    for tab, page in (("image_compare", ic.page), ("multi_compare", mc.page)):
        handlers = _handlers(tmp_path, page=page)
        monkeypatch.setattr(scene_read, "page_widget", lambda _h, _k, _p=page: _p)
        first = scene_read.canonical_json(handlers.handle("get_canvas_scene", {"tab": tab}))
        second = scene_read.canonical_json(handlers.handle("get_canvas_scene", {"tab": tab}))
        assert first == second, f"{tab}: the same scene must serialize identically"
        # Sorted keys, no float drift on a round trip.
        assert first == scene_read.canonical_json(json.loads(first))


# ---------------------------------------------------------------------------
# (d) Multi Compare reuses the projection — R3/R4
# ---------------------------------------------------------------------------


def test_mc_transform_is_the_projection_output(qapp, tmp_path, monkeypatch):
    fixture = _mc_fixture(qapp)
    canvas = fixture.canvas
    handlers = _handlers(tmp_path, page=fixture.page)

    scene = _read(handlers, monkeypatch, page=fixture.page, tab="multi_compare")

    # The projection is called with exactly what the read reports.
    from tabs.multi_compare.scene.projection import build_render_context

    composition = canvas._active_composition
    framebuffer = scene["transform"]["framebuffer"]
    assert framebuffer == [
        canvas.width() * canvas.devicePixelRatioF(),
        canvas.height() * canvas.devicePixelRatioF(),
    ]
    expected = build_render_context(
        composition=composition,
        framebuffer_size=(framebuffer[0], framebuffer[1]),
        clip_matrix=(),
        available_slot_ids={layer.layer_id for layer in composition.layers},
        widget=canvas,
    )
    projection = scene["transform"]["projection"]
    assert projection["scale"] == expected.scale
    assert tuple(projection["offset"]) == tuple(expected.offset)
    assert projection["source"] == (
        "tabs.multi_compare.scene.projection.build_render_context"
    )
    assert scene["scene_source"] == "mc_projection"
    assert scene["space"] == SPACE_COMPOSITION_PX
    assert scene["canvas"]["space"] == SPACE_COMPOSITION_PX


def test_mc_reports_both_pane_sets_with_reasons(qapp, tmp_path, monkeypatch):
    """R3: painted composition panes vs hit-test-only leaves, each named."""
    fixture = _mc_fixture(qapp)
    handlers = _handlers(tmp_path, page=fixture.page)

    scene = _read(handlers, monkeypatch, page=fixture.page, tab="multi_compare")

    boxes = {pane["slot"]: pane for pane in scene["panes"]}
    assert set(boxes) == {1, 2, 3}
    assert boxes[1]["in_composition"] is True and boxes[1]["painted"] is True
    assert boxes[2]["in_composition"] is True
    assert boxes[3]["in_composition"] is False
    assert boxes[3]["painted"] is False
    assert boxes[3]["reason"].startswith("imageless")
    assert boxes[3]["rect_source"].startswith("layout_tree")
    assert boxes[3]["space"] == SPACE_COMPOSITION_PX
    assert scene["dividers"], "the split between slot 1 and slot 2 must be reported"
    assert all(d["space"] == SPACE_COMPOSITION_PX for d in scene["dividers"])
    assert all(d["source"] == "mc_resolved_composition.gaps" for d in scene["dividers"])
    assert any(
        entry["field"] == "panes[painted]" for entry in scene["unavailable"]
    ), "an excluded pane must be named in unavailable"


def test_mc_focus_mode_names_the_collapsed_panes(qapp, tmp_path, monkeypatch):
    fixture = _mc_fixture(qapp, focused_slot_id=2)
    handlers = _handlers(tmp_path, page=fixture.page)

    scene = _read(handlers, monkeypatch, page=fixture.page, tab="multi_compare")

    painted = {pane["slot"] for pane in scene["panes"] if pane["in_composition"]}
    collapsed = {
        pane["slot"]: pane["reason"]
        for pane in scene["panes"]
        if not pane["in_composition"]
    }
    assert painted == {2}
    assert set(collapsed) == {1, 3}
    assert all(reason.startswith("focus_collapsed") for reason in collapsed.values())
    assert scene["focus"] == {"focused": True, "slot_id": 2, "source": "multi_compare_state"}


# ---------------------------------------------------------------------------
# (e) a tab without a canvas answers honestly — R6
# ---------------------------------------------------------------------------


def test_tab_without_a_canvas_answers_honestly(qapp, tmp_path, monkeypatch):
    page = QWidget()
    handlers = _handlers(tmp_path, page=page)

    scene = _read(handlers, monkeypatch, page=page, tab="image_gallery")

    assert scene["has_canvas"] is False
    assert scene["scene_source"] is None
    assert scene["tab_has_canvas_concept"] is False
    assert "no canvas" in scene["reason"]
    assert scene["panes"] == [] and scene["magnifiers"] == []
    assert scene["canvas"] is None and scene["transform"] is None
    assert scene["unavailable"] == [{"field": "scene", "reason": scene["reason"]}]


def test_canvas_tab_whose_page_is_not_open_says_so(qapp, tmp_path, monkeypatch):
    handlers = _handlers(tmp_path, page=None)

    scene = handlers.handle("get_canvas_scene", {"tab": "image_compare"})

    assert scene["has_canvas"] is False
    assert scene["tab_has_canvas_concept"] is True
    assert "not open" in scene["reason"]


def test_unknown_tab_is_a_method_error(qapp, tmp_path):
    handlers = _handlers(tmp_path, page=None)

    with pytest.raises(Exception) as excinfo:
        handlers.handle("get_canvas_scene", {"tab": "no_such_tab"})

    assert "unknown tab" in str(excinfo.value)
    assert "image_compare" in (excinfo.value.hint or "")


# ---------------------------------------------------------------------------
# MCP surface — R8
# ---------------------------------------------------------------------------


def test_mcp_tool_is_one_read_only_live_tool(qapp, tmp_path):
    tools = {tool["name"]: tool for tool in catalog.TOOLS}
    tool = tools["get_canvas_scene"]

    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["inputSchema"]["additionalProperties"] is False
    assert tool["inputSchema"]["properties"]["tab"]["type"] == "string"
    assert tool["handler"] == "get_canvas_scene"
    assert 200 <= len(tool["description"]) <= 400
    assert "source" in tool["outputSchema"]["properties"]

    backend = AppBackend(
        REPO_ROOT,
        artifacts_dir=tmp_path / "artifacts",
        data_dir=tmp_path / "data",
        snapshot_runner=lambda tab: {},
        launcher=lambda args: 1,
        settings_writer=lambda key, value: None,
    )
    with pytest.raises(ToolError) as excinfo:
        backend.get_canvas_scene(tab="image_compare")
    assert "no live instance" in str(excinfo.value)

    # The wire payload keeps the scene's own provenance and adds the transport one.
    class _ScriptedClient:
        def __init__(self) -> None:
            self.requests: list[tuple[str, dict]] = []

        def alive(self) -> bool:
            return True

        def call(self, method, params=None, *, retry_on_timeout=False):
            self.requests.append((method, dict(params or {})))
            return {
                "schema": scene_read.SCENE_SCHEMA,
                "tab": "image_compare",
                "scene_source": "ic_scene",
                "has_canvas": True,
                "panes": [],
                "dividers": [],
                "magnifiers": [],
                "unavailable": [],
            }

        def close(self) -> None:
            return None

    client = _ScriptedClient()
    payload = LiveBackend().get_canvas_scene(client=client, tab="image_compare")
    assert client.requests == [("get_canvas_scene", {"tab": "image_compare"})]
    assert payload["source"] == "live"
    assert payload["scene_source"] == "ic_scene"
    assert "get_canvas_scene" in LiveBackend.READ_METHODS


def test_mc_scene_reports_the_scene_camera_not_the_content_camera(qapp, tmp_path, monkeypatch):
    """The transform-level camera must be the one that scales the projection.

    Measured live 2026-11-15: with the scene camera at 1.33 the dump reported
    ``camera.zoom == 1.0`` (the content camera) while ``projection.scale`` was
    visibly scaled — a read that contradicts the pixels it describes.
    """
    fixture = _mc_fixture(qapp)
    canvas = fixture.canvas
    canvas.state = dataclasses.replace(
        canvas.state, scene_zoom=1.33, scene_pan_x=0.1, scene_pan_y=0.0
    )
    handlers = _handlers(tmp_path, page=fixture.page)
    scene = _read(handlers, monkeypatch, page=fixture.page, tab="multi_compare")

    camera = scene["transform"]["camera"]
    assert camera["zoom"] == pytest.approx(1.33)
    assert camera["pan"] == pytest.approx([0.1, 0.0])
    content = scene["transform"]["content_camera"]
    assert content["zoom"] == pytest.approx(1.0)
