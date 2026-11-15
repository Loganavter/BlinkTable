"""Redux-style state management for the multi-compare tab.

The tab keeps its own action namespace (``multi_compare/*``) rather than
extending the global ``ActionType`` enum used by main compare: the
multi-compare layout, slots and drag state are orthogonal to the rest of the
app.

Since the state unification (state-unification-plan.md, kept private in the
improve-imgsli-internal-docs repo), ``MultiCompareStore``
is a **facade** over the core ``Dispatcher`` and the active session's
``state_slots["multi_compare.state"]`` slot — the slot is the single source of
truth, reduced by the core ``RootReducer`` (see ``bootstrap_reducers.py``) and
covered by the core undo/redo stacks. The standalone mode (no ``core_store``)
keeps the historical local dispatch loop for tests. The pure ``reduce``
function remains the reducer for both modes.
Audit-Meta: pattern=state-machine reason="MultiCompareStore facade over Dispatcher + slot lifecycle"
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tabs.multi_compare.models import (
    CompareSlot,
    LayoutNode,
    LeafNode,
    MultiCompareDividerSettings,
    MultiCompareLabelSettings,
    MultiCompareState,
    slot_ids_in_tree,
)
from tabs.multi_compare.scene import layout_constraints, tree_ops
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


@dataclass(frozen=True)
class MultiCompareAction:
    """Base for all multi-compare actions.

    ``type`` is a stable string so logs / dev tools can show a readable label.
    Concrete subclasses add their own payload fields.
    """

    type: str


@dataclass(frozen=True)
class AddSlot(MultiCompareAction):
    path: Path
    label: str
    target_path: tuple[int, ...] | None
    side: str | None
    target_root: bool


@dataclass(frozen=True)
class NoteSlotPixels(MultiCompareAction):
    """A decoded tier landed in the session pixel cache for ``slot_id``.

    B1 replacement for the removed image-carrying slot action: pixels travel
    worker → :class:`pipeline.cache.MultiComparePixelCache`, never through
    the action/reducer — this only bumps the slot's ``revision`` (pure
    ``dataclasses.replace``) so subscribers rebuild composition / re-sync
    textures exactly as they did on the old image-carrying action. Tier is
    ``"preview"`` (bounded ``QImage``) or ``"full"`` (``TiledPixelStore``).
    """

    slot_id: int
    tier: str


@dataclass(frozen=True)
class RemoveSlot(MultiCompareAction):
    slot_id: int


@dataclass(frozen=True)
class RenameSlot(MultiCompareAction):
    slot_id: int
    label: str


@dataclass(frozen=True)
class SetCropOverride(MultiCompareAction):
    """Per-slot crop override (IC ``SetCropOverrideAction`` parity).

    ``override``: ``True``/``False`` forces crop on/off for the slot,
    ``None`` returns it to Auto (follow the global setting). Pure state —
    the reload (evict + re-decode, MC bakes crop) is orchestrated by
    ``use_cases/crop.py``, never by the reducer.
    """

    slot_id: int
    override: bool | None


@dataclass(frozen=True)
class SwapSlots(MultiCompareAction):
    slot_id_a: int
    slot_id_b: int


@dataclass(frozen=True)
class MoveSlot(MultiCompareAction):
    source_slot_id: int
    target_path: tuple[int, ...]
    target_anchor_slot_id: int
    side: str


@dataclass(frozen=True)
class SetFocus(MultiCompareAction):
    slot_id: int | None


@dataclass(frozen=True)
class SetZoom(MultiCompareAction):
    zoom: float
    pan_x: float
    pan_y: float


@dataclass(frozen=True)
class SetPan(MultiCompareAction):
    pan_x: float
    pan_y: float


@dataclass(frozen=True)
class SetSceneZoom(MultiCompareAction):
    """Whole-composition camera zoom (``MultiCompareState.scene_zoom``)."""

    zoom: float
    pan_x: float
    pan_y: float


@dataclass(frozen=True)
class SetScenePan(MultiCompareAction):
    """Whole-composition camera pan, in the zoomed viewport's units."""

    pan_x: float
    pan_y: float


@dataclass(frozen=True)
class ResetView(MultiCompareAction):
    pass


@dataclass(frozen=True)
class SetDragState(MultiCompareAction):
    active: bool
    internal: bool
    source_slot_id: int | None
    target_path: tuple[int, ...] | None
    target_side: str | None
    target_root: bool
    target_swap_slot_id: int | None


@dataclass(frozen=True)
class SetSplitWeights(MultiCompareAction):
    path: tuple[int, ...]
    weights: tuple[float, ...]
    # Natural ``(w, h)`` sizes per slot id, resolved from the session pixel
    # cache at the dispatch call site — the reducer stays pure (B1: pixels
    # are no longer on the slot). ``None``/missing = imageless (same outcome
    # as an imageless slot before B1: min-share clamp only).
    sizes: tuple[tuple[int, tuple[int, int]], ...] | None = None


@dataclass(frozen=True)
class SetLabelSettings(MultiCompareAction):
    settings: MultiCompareLabelSettings


@dataclass(frozen=True)
class SetDividerSettings(MultiCompareAction):
    settings: MultiCompareDividerSettings


@dataclass(frozen=True)
class ApplyLayoutTree(MultiCompareAction):
    """Replace the whole tree (used by tests / snapshot restore)."""

    root: LayoutNode | None


@dataclass(frozen=True)
class Clear(MultiCompareAction):
    pass


class actions:
    """Convenience factories so call sites don't pass ``type=`` everywhere."""

    @staticmethod
    def add_slot(
        path: Path,
        label: str,
        target_path: tuple[int, ...] | None = None,
        side: str | None = None,
        target_root: bool = False,
    ) -> AddSlot:
        return AddSlot(
            type="multi_compare/add_slot",
            path=path,
            label=label,
            target_path=target_path,
            side=side,
            target_root=target_root,
        )

    @staticmethod
    def note_slot_pixels(slot_id: int, tier: str) -> NoteSlotPixels:
        return NoteSlotPixels(
            type="multi_compare/note_slot_pixels",
            slot_id=slot_id,
            tier=tier,
        )

    @staticmethod
    def remove_slot(slot_id: int) -> RemoveSlot:
        return RemoveSlot(type="multi_compare/remove_slot", slot_id=slot_id)

    @staticmethod
    def rename_slot(slot_id: int, label: str) -> RenameSlot:
        return RenameSlot(
            type="multi_compare/rename_slot",
            slot_id=slot_id,
            label=label,
        )

    @staticmethod
    def set_crop_override(slot_id: int, override: bool | None) -> SetCropOverride:
        return SetCropOverride(
            type="multi_compare/set_crop_override",
            slot_id=slot_id,
            override=override,
        )

    @staticmethod
    def swap_slots(slot_id_a: int, slot_id_b: int) -> SwapSlots:
        return SwapSlots(
            type="multi_compare/swap_slots",
            slot_id_a=slot_id_a,
            slot_id_b=slot_id_b,
        )

    @staticmethod
    def move_slot(
        source_slot_id: int,
        target_path: tuple[int, ...],
        target_anchor_slot_id: int,
        side: str,
    ) -> MoveSlot:
        return MoveSlot(
            type="multi_compare/move_slot",
            source_slot_id=source_slot_id,
            target_path=target_path,
            target_anchor_slot_id=target_anchor_slot_id,
            side=side,
        )

    @staticmethod
    def set_focus(slot_id: int | None) -> SetFocus:
        return SetFocus(type="multi_compare/set_focus", slot_id=slot_id)

    @staticmethod
    def set_zoom(zoom: float, pan_x: float, pan_y: float) -> SetZoom:
        return SetZoom(
            type="multi_compare/set_zoom",
            zoom=zoom,
            pan_x=pan_x,
            pan_y=pan_y,
        )

    @staticmethod
    def set_pan(pan_x: float, pan_y: float) -> SetPan:
        return SetPan(type="multi_compare/set_pan", pan_x=pan_x, pan_y=pan_y)

    @staticmethod
    def set_scene_zoom(zoom: float, pan_x: float, pan_y: float) -> SetSceneZoom:
        return SetSceneZoom(
            type="multi_compare/set_scene_zoom",
            zoom=zoom,
            pan_x=pan_x,
            pan_y=pan_y,
        )

    @staticmethod
    def set_scene_pan(pan_x: float, pan_y: float) -> SetScenePan:
        return SetScenePan(
            type="multi_compare/set_scene_pan", pan_x=pan_x, pan_y=pan_y
        )

    @staticmethod
    def reset_view() -> ResetView:
        return ResetView(type="multi_compare/reset_view")

    @staticmethod
    def set_drag_state(
        *,
        active: bool,
        internal: bool = False,
        source_slot_id: int | None = None,
        target_path: tuple[int, ...] | None = None,
        target_side: str | None = None,
        target_root: bool = False,
        target_swap_slot_id: int | None = None,
    ) -> SetDragState:
        return SetDragState(
            type="multi_compare/set_drag_state",
            active=active,
            internal=internal,
            source_slot_id=source_slot_id,
            target_path=target_path,
            target_side=target_side,
            target_root=target_root,
            target_swap_slot_id=target_swap_slot_id,
        )

    @staticmethod
    def set_split_weights(
        path: tuple[int, ...],
        weights: tuple[float, ...] | list[float],
        sizes: dict[int, tuple[int, int]] | None = None,
    ) -> SetSplitWeights:
        return SetSplitWeights(
            type="multi_compare/set_split_weights",
            path=tuple(path),
            weights=tuple(weights),
            sizes=tuple(sorted(sizes.items())) if sizes else None,
        )

    @staticmethod
    def set_label_settings(settings: MultiCompareLabelSettings) -> SetLabelSettings:
        return SetLabelSettings(
            type="multi_compare/set_label_settings",
            settings=settings,
        )

    @staticmethod
    def set_divider_settings(
        settings: MultiCompareDividerSettings,
    ) -> SetDividerSettings:
        return SetDividerSettings(
            type="multi_compare/set_divider_settings",
            settings=settings,
        )

    @staticmethod
    def apply_layout_tree(root: LayoutNode | None) -> ApplyLayoutTree:
        return ApplyLayoutTree(type="multi_compare/apply_layout_tree", root=root)

    @staticmethod
    def clear() -> Clear:
        return Clear(type="multi_compare/clear")


def _next_slot_id(state: MultiCompareState) -> int:
    if not state.slots:
        return 0
    return max(s.id for s in state.slots) + 1


def _replace(state: MultiCompareState, **changes) -> MultiCompareState:
    """Shallow ``dataclasses.replace`` that preserves the legacy list identity
    of ``slots`` when not changed — important while the canvas still reads the
    same list reference between dispatches."""
    return dataclasses.replace(state, **changes)


def reduce(state: MultiCompareState, action: MultiCompareAction) -> MultiCompareState:
    """Pure reducer: ``state + action → new_state`` (or same instance when no-op)."""
    if isinstance(action, AddSlot):
        slot_id = _next_slot_id(state)
        slot = CompareSlot(
            id=slot_id,
            path=action.path,
            label=action.label or (action.path.stem if action.path else ""),
        )
        new_slots = list(state.slots) + [slot]
        if action.target_root or state.root is None:
            new_root: LayoutNode | None = LeafNode(slot_id)
        elif action.target_path is not None and action.side is not None:
            new_root = tree_ops.insert_beside_path(
                state.root, action.target_path, action.side, slot_id
            )
        else:

            new_root = state.root
        return _replace(state, slots=new_slots, root=new_root)

    if isinstance(action, NoteSlotPixels):
        found = False
        new_slots = []
        for slot in state.slots:
            if slot.id == action.slot_id:
                found = True
                new_slots.append(
                    dataclasses.replace(slot, revision=slot.revision + 1)
                )
            else:
                new_slots.append(slot)
        if not found:
            return state
        return _replace(state, slots=new_slots)

    if isinstance(action, RemoveSlot):
        # Nothing closable lives in state after B1 (pixels are owned by the
        # session pixel cache, snapshots hold paths only) — removal just
        # drops the path reference. The cache entry stays warm under LRU so
        # undo of this removal re-resolves instantly; eviction close can
        # never break an undo snapshot (it holds no stores).
        new_slots = [s for s in state.slots if s.id != action.slot_id]
        new_root = tree_ops.remove_leaf(state.root, action.slot_id)
        focused = (
            None if state.focused_slot_id == action.slot_id else state.focused_slot_id
        )
        return _replace(state, slots=new_slots, root=new_root, focused_slot_id=focused)

    if isinstance(action, RenameSlot):
        new_slots = [
            (
                dataclasses.replace(slot, label=action.label)
                if slot.id == action.slot_id
                else slot
            )
            for slot in state.slots
        ]
        if new_slots == state.slots:
            return state
        return _replace(state, slots=new_slots)

    if isinstance(action, SetCropOverride):
        new_slots = [
            (
                dataclasses.replace(slot, crop_override=action.override)
                if slot.id == action.slot_id
                else slot
            )
            for slot in state.slots
        ]
        if new_slots == state.slots:
            return state
        return _replace(state, slots=new_slots)

    if isinstance(action, SwapSlots):
        new_root = tree_ops.swap_slot_ids(
            state.root, action.slot_id_a, action.slot_id_b
        )
        return _replace(state, root=new_root)

    if isinstance(action, MoveSlot):
        pruned = tree_ops.remove_leaf(state.root, action.source_slot_id)
        if pruned is None:
            return _replace(state, root=LeafNode(action.source_slot_id))

        from tabs.multi_compare.models import find_path

        new_anchor_path = find_path(pruned, action.target_anchor_slot_id)
        if new_anchor_path is None:
            return _replace(state, root=pruned)
        target_depth = len(action.target_path)
        new_path = tuple(new_anchor_path[:target_depth])
        new_root = tree_ops.insert_beside_path(
            pruned, new_path, action.side, action.source_slot_id
        )
        return _replace(state, root=new_root)

    if isinstance(action, SetFocus):
        if state.focused_slot_id == action.slot_id:
            return state
        return _replace(state, focused_slot_id=action.slot_id)

    if isinstance(action, SetZoom):
        if (
            state.zoom == action.zoom
            and state.pan_x == action.pan_x
            and state.pan_y == action.pan_y
        ):
            return state
        return _replace(
            state,
            zoom=action.zoom,
            pan_x=action.pan_x,
            pan_y=action.pan_y,
        )

    if isinstance(action, SetPan):
        if state.pan_x == action.pan_x and state.pan_y == action.pan_y:
            return state
        return _replace(state, pan_x=action.pan_x, pan_y=action.pan_y)

    if isinstance(action, SetSceneZoom):
        # Same no-op guard shape as SetZoom: the wheel tick dispatches at
        # most one action, and an identical payload must not notify.
        if (
            state.scene_zoom == action.zoom
            and state.scene_pan_x == action.pan_x
            and state.scene_pan_y == action.pan_y
        ):
            return state
        return _replace(
            state,
            scene_zoom=action.zoom,
            scene_pan_x=action.pan_x,
            scene_pan_y=action.pan_y,
        )

    if isinstance(action, SetScenePan):
        if (
            state.scene_pan_x == action.pan_x
            and state.scene_pan_y == action.pan_y
        ):
            return state
        return _replace(state, scene_pan_x=action.pan_x, scene_pan_y=action.pan_y)

    if isinstance(action, ResetView):
        # One reset clears BOTH cameras: the zoom chip's reset button (and
        # ``Ctrl+0``) is the user's "put the view back" gesture, so leaving a
        # scene zoom behind would keep the chip visible after a reset.
        if (
            state.zoom == 1.0
            and state.pan_x == 0.0
            and state.pan_y == 0.0
            and state.scene_zoom == 1.0
            and state.scene_pan_x == 0.0
            and state.scene_pan_y == 0.0
        ):
            return state
        return _replace(
            state,
            zoom=1.0,
            pan_x=0.0,
            pan_y=0.0,
            scene_zoom=1.0,
            scene_pan_x=0.0,
            scene_pan_y=0.0,
        )

    if isinstance(action, SetDragState):
        # Same equality-guard shape as SetFocus/SetZoom/SetPan above: an
        # unchanged drag payload returns the same instance, so neither the
        # standalone dispatch loop (``new_state is self._state`` early-out)
        # nor the bound facade (slot-identity guard in ``_on_core_change``)
        # notifies subscribers — no composition rebuild, no render, no
        # session-slot rewrite for a no-op. dragMove fires per mouse tick
        # with usually-identical targets; without this every tick pays the
        # full dispatch→render pipeline and starves the event loop (visible
        # as a frozen DnD cursor). Mirrors image_compare's
        # ``set_drag_overlay_state`` early-return on identical payload.
        if (
            state.drag_active == action.active
            and state.drag_internal == action.internal
            and state.drag_source_slot_id == action.source_slot_id
            and state.drag_target_path == action.target_path
            and state.drag_target_side == action.target_side
            and state.drag_target_root == action.target_root
            and state.drag_target_swap_slot_id == action.target_swap_slot_id
        ):
            return state
        return _replace(
            state,
            drag_active=action.active,
            drag_internal=action.internal,
            drag_source_slot_id=action.source_slot_id,
            drag_target_path=action.target_path,
            drag_target_side=action.target_side,
            drag_target_root=action.target_root,
            drag_target_swap_slot_id=action.target_swap_slot_id,
        )

    if isinstance(action, SetSplitWeights):
        sizes = dict(action.sizes) if action.sizes else None
        weights = layout_constraints.constrain_split_weights(
            state.root,
            action.path,
            action.weights,
            state.slots,
            zoom=state.zoom,
            sizes=sizes,
        )
        new_root = tree_ops.set_split_weights(state.root, action.path, weights)
        if new_root is state.root:
            return state
        return _replace(state, root=new_root)

    if isinstance(action, SetLabelSettings):
        if state.label_settings == action.settings:
            return state
        return _replace(state, label_settings=action.settings)

    if isinstance(action, SetDividerSettings):
        if state.divider_settings == action.settings:
            return state
        return _replace(state, divider_settings=action.settings)

    if isinstance(action, ApplyLayoutTree):
        if state.root is action.root:
            return state
        valid_ids = slot_ids_in_tree(action.root) if action.root is not None else set()
        focused = state.focused_slot_id if state.focused_slot_id in valid_ids else None
        return _replace(state, root=action.root, focused_slot_id=focused)

    if isinstance(action, Clear):
        # Nothing closable lives in state after B1 (see RemoveSlot above).
        return MultiCompareState()

    logger.warning("multi_compare reducer: unhandled action %s", type(action).__name__)
    return state


class MultiCompareStore:
    """Redux-style state for the multi-compare tab.

    Two modes (same public API — ``state`` / ``dispatch`` / ``subscribe`` /
    ``replace_state``):

    - **bound** (production): a thin facade over the core ``Dispatcher`` and
      the active session's ``state_slots["multi_compare.state"]`` — the slot
      is the single source of truth. ``dispatch`` forwards to the core
      Dispatcher (scope ``"multi_compare"``), ``state`` reads the slot,
      ``subscribe`` hooks core store changes filtered to that scope,
      ``replace_state`` writes the slot directly (session restore is not a
      user action, so it bypasses the undo stack). See
      ``state-unification-plan.md`` (kept private in the
      improve-imgsli-internal-docs repo, mirrored path).
    - **standalone** (tests): owns a private ``MultiCompareState`` and runs
      the pure ``reduce`` locally, notifying subscribers with
      ``(action, new_state)`` — the historical pre-facade behavior.

    Subscribers are always called *after* a state change with
    ``(action, new_state)``.
    """

    _SLOT = "multi_compare.state"

    def __init__(
        self,
        initial: MultiCompareState | None = None,
        *,
        core_store=None,
    ):
        self._core_store = core_store
        self._subscribers: list[
            Callable[[MultiCompareAction, MultiCompareState], None]
        ] = []
        self._last_action: MultiCompareAction = MultiCompareAction(
            type="multi_compare/replace_state"
        )
        self._dispatching = False
        self._last_notified_slot: MultiCompareState | None = None
        if core_store is None:
            self._state: MultiCompareState = initial or MultiCompareState()
        else:
            self._state = None  # never holds state in bound mode
            self._bound_change_cb = self._on_core_change
            if hasattr(core_store, "on_change"):
                core_store.on_change(self._bound_change_cb)

    # --- bound-mode plumbing ---------------------------------------------

    def _active_session(self):
        try:
            return self._core_store.get_active_workspace_session()
        except Exception:
            return None

    def _active_slot_value(self) -> MultiCompareState | None:
        """The active session's real MC slot object (``None`` when the active
        session is not a multi_compare session — do not synthesize a default
        here, identity checks must see the true value)."""
        session = self._active_session()
        if session is None:
            return None
        value = session.state_slots.get(self._SLOT)
        if isinstance(value, MultiCompareState):
            return value
        return None

    def _read_slot(self) -> MultiCompareState:
        value = self._active_slot_value()
        if value is not None:
            return value
        return MultiCompareState()

    def _on_core_change(self, scope: str) -> None:
        # React to MC dispatches ("multi_compare") and to undo/redo, which the
        # core Dispatcher emits as "viewport" (undo must repaint the canvas
        # with the restored slot). Guarded by slot-object identity so IC /
        # unrelated core changes (active session without an MC slot, or an
        # unchanged slot) never notify.
        if scope not in ("multi_compare", "viewport"):
            return
        if not self._subscribers:
            return
        value = self._active_slot_value()
        if value is None or value is self._last_notified_slot:
            return
        self._last_notified_slot = value
        action = (
            self._last_action
            if self._dispatching
            else MultiCompareAction(type="multi_compare/replace_state")
        )
        self._dispatching = False
        for sub in list(self._subscribers):
            try:
                sub(action, value)
            except Exception:
                logger.exception(
                    "multi_compare subscriber raised on core state change",
                )

    # --- public API ------------------------------------------------------

    @property
    def state(self) -> MultiCompareState:
        if self._core_store is not None:
            return self._read_slot()
        return self._state

    def replace_state(self, state: MultiCompareState) -> None:
        """Swap the entire scene state in one shot (e.g. session restore).

        Notifies subscribers with a synthetic ``multi_compare/replace_state``
        action so listeners (canvas, etc.) re-sync without going through
        per-slot reducers. In bound mode this writes the session slot directly
        (a restore is not a user action and must not enter the undo stack).
        """
        if self._core_store is None:
            self._state = state
        else:
            session = self._active_session()
            # Phase 6C: store slot API instead of direct state_slots write (STORE.md).
            # Restore is not a user action (bypass undo); facade notifies directly.
            # Use viewport scope so bound facade's _on_core_change sees the slot change
            # but is suppressed via _last_notified identity (no double notify).
            self._last_notified_slot = state
            if session is not None:
                self._core_store.set_session_state_slot(
                    self._SLOT, state, emit_scope="viewport"
                )
        synthetic = MultiCompareAction(type="multi_compare/replace_state")
        self._last_action = synthetic
        for sub in list(self._subscribers):
            try:
                sub(synthetic, state)
            except Exception:
                logger.exception(
                    "multi_compare subscriber raised on replace_state",
                )

    def dispatch(self, action: MultiCompareAction) -> MultiCompareState:
        if self._core_store is None:
            try:
                new_state = reduce(self._state, action)
            except Exception:
                logger.exception(
                    "multi_compare dispatch failed: %s", getattr(action, "type", action)
                )
                return self._state
            if new_state is self._state:
                return self._state
            self._state = new_state
            self._last_action = action
            for sub in list(self._subscribers):
                try:
                    sub(action, new_state)
                except Exception:
                    logger.exception(
                        "multi_compare subscriber raised on %s",
                        getattr(action, "type", action),
                    )
            return new_state

        # bound mode: forward to the core Dispatcher (slot is authoritative).
        # ``_dispatching`` is observed by ``_on_core_change`` (which runs
        # synchronously inside the forward) so subscribers receive the real
        # action rather than a synthetic one; undo/redo never set it, so they
        # deliver the synthetic ``replace_state`` action (no QSettings save).
        self._last_action = action
        self._dispatching = True
        dispatcher = getattr(self._core_store, "get_dispatcher", lambda: None)()
        if dispatcher is not None:
            dispatcher.dispatch(action, scope="multi_compare")
        self._dispatching = False
        return self._read_slot()

    def transact(self, actions_list: list[MultiCompareAction]) -> MultiCompareState:
        """Coalesce N actions into one dispatch/emit (A3, IC-parity).

        Additive batch API — the pure ``reduce`` above and every existing
        action are untouched. N-file add (DnD/chrome/carry/dialog) plans N
        ``AddSlot`` actions against a scratch state and commits them here,
        so one user-visible multi-add pays one dispatch → one
        ``emit_state_change("multi_compare")`` instead of N (IC precedent:
        ``image_compare.use_cases.slot.load_images_from_paths`` single
        transact of ``Append + SetCurrentIndex``,
        ``core.state_management.transaction.TransactionAction``).

        - **standalone** (tests): the ``reduce`` chain runs over a local
          copy; subscribers are notified once with the *last* inner action
          (keeps the ``persistence`` divider/label guards and canvas
          behavior identical to N sequential dispatches).
        - **bound** (production): a single ``core_store.transact([...],
          scope="multi_compare")`` → one core ``Dispatcher.dispatch`` of
          the ``TRANSACTION`` wrapper → one emit. Facade subscribers
          observe the last inner action rather than the core wrapper, so
          the action vocabulary they filter on is unchanged. Falls back to
          the single ``dispatch`` path for one action and to sequential
          dispatch when the core store has neither ``transact`` nor a
          dispatcher.

        Undo note: the outer ``TRANSACTION`` type is outside
        ``Dispatcher._UNDOABLE_TYPES`` (same as every IC transact), so a
        batched multi-add commits atomically outside undo while single-add
        ``dispatch`` stays undoable. ``MoveSlot`` and the reducer are
        untouched by this method.
        """
        pending = list(actions_list or [])
        if not pending:
            return self.state
        if self._core_store is None:
            current = self._state
            try:
                for sub in pending:
                    current = reduce(current, sub)
            except Exception:
                logger.exception(
                    "multi_compare transact failed (%d actions)", len(pending)
                )
                return self._state
            if current is self._state:
                return self._state
            self._state = current
            self._last_action = pending[-1]
            for sub_cb in list(self._subscribers):
                try:
                    sub_cb(pending[-1], current)
                except Exception:
                    logger.exception(
                        "multi_compare subscriber raised on transact",
                    )
            return current

        # bound mode: one core transaction → one dispatch → one emit.
        if len(pending) == 1:
            return self.dispatch(pending[0])
        self._last_action = pending[-1]
        self._dispatching = True
        try:
            transact = getattr(self._core_store, "transact", None)
            if callable(transact):
                transact(pending, scope="multi_compare")
            else:
                dispatcher = getattr(self._core_store, "get_dispatcher", lambda: None)()
                if dispatcher is not None:
                    from core.state_management.transaction import (
                        TransactionAction,
                    )

                    dispatcher.dispatch(
                        TransactionAction(pending), scope="multi_compare"
                    )
                else:
                    # LOUD fallback: no batch API and no dispatcher — the
                    # actions would vanish silently. Error-log (not debug)
                    # and funnel through the standard single-action path so
                    # the failure is observable and behavior matches the
                    # sequential fallback in use_cases.loading.
                    logger.error(
                        "multi_compare transact dropped (%d actions): "
                        "core store has neither transact nor dispatcher",
                        len(pending),
                    )
                    self._dispatching = False
                    for sub in pending:
                        self.dispatch(sub)
                    return self._read_slot()
        except Exception:
            logger.exception(
                "multi_compare transact failed (%d actions)", len(pending)
            )
        finally:
            self._dispatching = False
        return self._read_slot()

    def subscribe(
        self,
        callback: Callable[[MultiCompareAction, MultiCompareState], None],
    ) -> Callable[[], None]:
        if callback not in self._subscribers:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe