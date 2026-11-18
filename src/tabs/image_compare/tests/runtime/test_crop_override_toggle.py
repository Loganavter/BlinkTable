"""Per-image autocrop override (W5): tristate persist + boolean menu toggle.

Covers:
* ``ImageItem.crop_override`` round-trips through
  ``serialize_session``/``deserialize_session`` (None/True/False), and
  pre-override blobs (missing key) rebuild as None (Auto).
* The context menu exposes a single checkable ``Crop`` toggle in BOTH the
  slot menu and the list-item menu (On=True / Off=False). Stored Auto (None)
  keeps following the global ``auto_crop_black_borders`` setting and toggles
  from its effective state.
* Dispatch shape: fallback path dispatches ``SetCropOverrideAction`` with
  scope ``"document"`` (Store-native, never in-place); when the session
  controller offers ``set_crop_override_at_index`` it is preferred and no
  manual dispatch happens.

Headless-safe: no real dialogs, no widgets (``qapp`` fixture is only a
safety net for toolkit ``tr``).
"""

from __future__ import annotations

import os
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from sli_ui_toolkit.widgets import ContextMenuAction

from core.state_management.actions import SetCropOverrideAction
from tabs.contract import TabContext
from tabs.image_compare.state.document import DocumentModel, ImageItem
from tabs.image_compare.state.reducer import DocumentReducer
from tabs.image_compare.ui.context_menu import ImageCompareContextMenuProvider
from tabs.image_compare.use_cases import persistence
from ui.context_menu.models import ContextMenuRequest, ContextMenuTarget


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _Session:
    def __init__(self, session_id: str, *, document=None, viewport=None):
        self.id = session_id
        self.session_type = "image_compare"
        self.document = document
        self.viewport = viewport
        self.state_slots: dict = {}


class _RecordingDispatcher:
    """Fake dispatcher applying the real DocumentReducer (no Qt needed)."""

    def __init__(self, store):
        self._store = store
        self.calls: list = []

    def dispatch(self, action, scope: str = "viewport") -> None:
        session = self._store.get_workspace_session(self._store._active_id)
        self.calls.append((action, scope))
        if scope == "document" and session is not None:
            session.document = DocumentReducer.reduce(session.document, action)
            self._store.emit_state_change(scope)


class _Store:
    def __init__(self, sessions: dict | None = None, *, with_dispatcher: bool = False):
        self._sessions = sessions or {}
        self._active_id = next(iter(self._sessions), "s1")
        self.emitted: list = []
        self._dispatcher = _RecordingDispatcher(self) if with_dispatcher else None
        # The real object ALWAYS carries this field (``SettingsState``), so the
        # fake models it instead of leaning on the context menu's defensive
        # ``getattr(..., True)`` fallback. These tests describe a user who has
        # auto-crop ON — the profile where "Auto" reads as checked and a toggle
        # goes to Off — so the setting is stated here rather than inherited
        # from a fallback literal (changing that literal used to flip this
        # suite's premise silently).
        self.settings = SimpleNamespace(auto_crop_black_borders=True)

    def get_workspace_session(self, session_id):
        return self._sessions.get(session_id)

    def get_active_workspace_session(self):
        return self._sessions.get(self._active_id)

    def get_session_state_slot(self, slot, *, session_id=None, default=None):
        session = self._sessions.get(session_id or self._active_id)
        if session is None:
            return default
        if slot == "document":
            return session.document
        return session.state_slots.get(slot, default)

    def set_session_state_slot(self, slot, value, *, session_id=None, emit_scope=None):
        session = self._sessions.get(session_id or self._active_id)
        if session is None:
            return
        if slot == "document":
            session.document = value
        else:
            session.state_slots[slot] = value

    def ensure_session_state_slot(self, slot, *, session_id=None, factory=None):
        session = self._sessions.get(session_id or self._active_id)
        if session is None:
            return None
        if slot == "document":
            return session.document
        existing = session.state_slots.get(slot)
        if existing is None and factory is not None:
            existing = factory()
            session.state_slots[slot] = existing
        return existing

    def get_dispatcher(self):
        return self._dispatcher

    def batch_changes(self):
        return nullcontext()

    def emit_state_change(self, scope: str = "") -> None:
        self.emitted.append(scope)


class _Tab:
    session_type = "image_compare"

    def __init__(self, widget=None, active_session_id=None):
        self._widget = widget
        self._active_session_id = active_session_id


def _doc() -> DocumentModel:
    return DocumentModel(
        image_list1=[
            ImageItem(path="/a.png", display_name="a", crop_override=None),
            ImageItem(path="/b.png", display_name="b", crop_override=True),
        ],
        image_list2=[
            ImageItem(path="/c.png", display_name="c", crop_override=False),
        ],
        current_index1=0,
        current_index2=0,
    )


def _roundtrip(doc: DocumentModel) -> DocumentModel:
    from core.store_viewport import ViewportState

    store = _Store({"s1": _Session("s1", document=doc, viewport=ViewportState())})
    tab = _Tab(widget=None, active_session_id="s1")
    blob = persistence.serialize_session(tab, "s1", TabContext(store=store))
    assert blob is not None
    fresh = _Store({"s1": _Session("s1", viewport=ViewportState())})
    persistence.deserialize_session(tab, "s1", blob, TabContext(store=fresh))
    return fresh.get_workspace_session("s1").document


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def test_tristate_persist_roundtrip():
    rebuilt = _roundtrip(_doc())
    assert [it.crop_override for it in rebuilt.image_list1] == [None, True]
    assert [it.crop_override for it in rebuilt.image_list2] == [False]


def test_deserialize_legacy_missing_key_defaults_auto():
    blob = {
        "version": 3,
        "image_list1": [{"path": "/a.png", "display_name": "a", "rating": 0}],
        "image_list2": [],
        "current_index1": 0,
        "current_index2": -1,
        "camera": {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0},
    }
    from core.store_viewport import ViewportState

    store = _Store({"s1": _Session("s1", viewport=ViewportState())})
    tab = _Tab(widget=None, active_session_id="other")
    persistence.deserialize_session(tab, "s1", blob, TabContext(store=store))
    doc = store.get_workspace_session("s1").document
    assert doc.image_list1[0].crop_override is None


def test_serialize_blob_carries_tristate():
    from core.store_viewport import ViewportState

    doc = _doc()
    store = _Store({"s1": _Session("s1", document=doc, viewport=ViewportState())})
    tab = _Tab(widget=None, active_session_id="s1")
    blob = persistence.serialize_session(tab, "s1", TabContext(store=store))
    assert blob is not None
    assert [e["crop_override"] for e in blob["image_list1"]] == [None, True]
    assert [e["crop_override"] for e in blob["image_list2"]] == [False]


# --------------------------------------------------------------------------
# context menu
# --------------------------------------------------------------------------


def _provider(store, canvas=None):
    canvas = canvas if canvas is not None else SimpleNamespace(updated=[])
    if not hasattr(canvas, "update"):
        canvas.update = lambda: canvas.updated.append(True)
    provider = ImageCompareContextMenuProvider(canvas, store)
    return provider, canvas


def _slot_request(canvas, slot: int) -> ContextMenuRequest:
    return ContextMenuRequest(
        source_widget=canvas,
        global_pos=QPoint(0, 0),
        local_pos=QPoint(0, 0),
        session_type="image_compare",
        target=ContextMenuTarget(kind="image_compare_slot", id=slot),
    )


def _list_request(canvas, list_num: int, index: int) -> ContextMenuRequest:
    flyout = SimpleNamespace()
    return ContextMenuRequest(
        source_widget=flyout,
        global_pos=QPoint(0, 0),
        local_pos=QPoint(0, 0),
        session_type="image_compare",
        target=ContextMenuTarget(
            kind="image_compare_list_item",
            id=(list_num, index),
            payload={"list_num": list_num, "index": index},
        ),
    )


def test_toggle_exposed_in_both_menus_with_state_label(qapp):
    store = _Store({"s1": _Session("s1", document=_doc())})
    provider, canvas = _provider(store)

    slot_ids = [
        e.action_id
        for e in provider.entries_for(_slot_request(canvas, 1))
        if isinstance(e, ContextMenuAction)
    ]
    assert "image_compare.crop_override_slot" in slot_ids

    # Flyout-owned provider for the list menu (entries_for guards source).
    flyout = SimpleNamespace()
    provider_flyout = ImageCompareContextMenuProvider(
        SimpleNamespace(update=lambda: None), store, flyout=flyout
    )
    req = ContextMenuRequest(
        source_widget=flyout,
        global_pos=QPoint(0, 0),
        local_pos=QPoint(0, 0),
        session_type="image_compare",
        target=ContextMenuTarget(
            kind="image_compare_list_item",
            id=(1, 1),
            payload={"list_num": 1, "index": 1},
        ),
    )
    list_ids = [
        e.action_id
        for e in provider_flyout.entries_for(req)
        if isinstance(e, ContextMenuAction)
    ]
    assert "image_compare.crop_override_list_item" in list_ids

    # Single checkable toggle: plain label, checked == effective state.
    # Item (1,1) is On → checked; item (1,0) is Auto with the global default
    # on (fake store has no settings) → checked as well.
    toggles = {
        e.action_id: e
        for e in provider_flyout.entries_for(req)
        if isinstance(e, ContextMenuAction)
    }
    toggle = toggles["image_compare.crop_override_list_item"]
    assert toggle.checkable is True
    assert toggle.checked is True
    assert ":" not in toggle.text

    slot_toggle = next(
        e
        for e in provider.entries_for(_slot_request(canvas, 1))
        if isinstance(e, ContextMenuAction)
        and e.action_id == "image_compare.crop_override_slot"
    )
    assert slot_toggle.checkable is True
    assert slot_toggle.checked is True


def test_execute_toggles_boolean_via_document_dispatch(qapp):
    store = _Store({"s1": _Session("s1", document=_doc())}, with_dispatcher=True)
    provider, canvas = _provider(store)
    provider.attach_session_controller(None)
    req = _list_request(canvas, 1, 0)  # starts Auto (None), effective on

    for expected in (False, True):
        handled = provider.execute(
            "image_compare.crop_override_list_item", req, (1, 0)
        )
        assert handled is True
        action, scope = store.get_dispatcher().calls[-1]
        assert isinstance(action, SetCropOverrideAction)
        assert (action.slot, action.index, action.value) == (1, 0, expected)
        assert scope == "document"
    doc = store.get_workspace_session("s1").document
    assert doc.image_list1[0].crop_override is True
    assert "document" in store.emitted
    assert canvas.updated, "canvas must repaint after the toggle"


def test_execute_prefers_controller_over_manual_dispatch(qapp):
    store = _Store({"s1": _Session("s1", document=_doc())}, with_dispatcher=True)
    provider, canvas = _provider(store)
    ctrl = MagicMock()
    provider.attach_session_controller(ctrl)
    req = _list_request(canvas, 1, 0)

    handled = provider.execute(
        "image_compare.crop_override_list_item", req, (1, 0)
    )

    assert handled is True
    ctrl.set_crop_override_at_index.assert_called_once_with(1, 0, False)
    assert store.get_dispatcher().calls == []


def test_slot_execute_resolves_current_index(qapp):
    store = _Store({"s1": _Session("s1", document=_doc())}, with_dispatcher=True)
    provider, canvas = _provider(store)
    provider.attach_session_controller(None)

    handled = provider.execute(
        "image_compare.crop_override_slot", _slot_request(canvas, 2), 2
    )

    assert handled is True
    action, scope = store.get_dispatcher().calls[-1]
    assert isinstance(action, SetCropOverrideAction)
    # slot 2 current index is 0, whose override is Off → toggles to On.
    assert (action.slot, action.index, action.value) == (2, 0, True)
    assert scope == "document"
