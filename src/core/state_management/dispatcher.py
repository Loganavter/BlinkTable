import logging
import threading
import time
from typing import Callable, List

# Audit-Meta: pattern=state-machine reason="single dispatch machine — optimistic-concurrency swap + session-targeted write-back + per-session history binding share one lock discipline, must stay one file (reducers.py precedent)"
from .actions import Action
from .reducers import RootReducer
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

_ACTION_HISTORY_SLOT = "action_history"
_UNDO_STACK_SLOT = "undo_stack"
_REDO_STACK_SLOT = "redo_stack"

# Action types a user would expect Ctrl+Z to revert. Deliberately excludes:
# - global settings (theme/language/UI/font/export dirs — store-global, not
#   per-session);
# - transient interaction/visual state (drag scrubbers, pressed keys,
#   dragging flags, geometry derived from widget resize);
# - caches/metrics/in-progress flags (cached diff, PSNR/SSIM values,
#   unification-in-progress, cache invalidation);
# - pixel-bearing document actions (SET_ORIGINAL_IMAGE / SET_FULL_RES_IMAGE /
#   SET_PREVIEW_IMAGE / CLEAR_IMAGE_SLOT_DATA): loading.py closes the replaced
#   TiledPixelStore, so a reference-snapshot of the old document would hold a
#   closed store. Undo of image *browsing* is covered by SET_CURRENT_INDEX
#   (the host tab re-loads the entry on the "document" scope emit that
#   undo/redo produces — path+reload, since the snapshot's pixels may
#   reference the closed store).
# - TRANSACTION batches: a transaction commits atomically *outside* undo
#   (consumers such as the multi_compare scene facade document this: batched
#   multi-add is one atomic commit, single-add dispatch stays undoable).
#   Transactions routinely wrap pixel-bearing inner actions (e.g.
#   set_current_image → SET_FULL_RES_IMAGE), which must never be snapshotted
#   for the closed-store reason above. Emit accounting: one TRANSACTION
#   dispatch records one action-history entry and produces exactly one
#   ``emit_state_change`` for the dispatch scope (commit), never one per
#   inner action. Gesture coalescing only applies to consecutive same-type
#   entries already on the undo stack, so a burst of transactions never
#   merges — each commit stays a separately accountable unit.
_UNDOABLE_TYPES = frozenset(
    {
        "SET_SPLIT_POSITION",
        "TOGGLE_ORIENTATION",
        "SET_MOVEMENT_SPEED",
        "SET_DIFF_MODE",
        "SET_CHANNEL_VIEW_MODE",
        "SET_SHOWING_SINGLE_IMAGE_MODE",
        "SET_ZOOM_INTERPOLATION_METHOD",
        "SET_INTERPOLATION_METHOD",
        "SET_MOVEMENT_INTERPOLATION_METHOD",
        "SET_INCLUDE_FILE_NAMES_IN_SAVED",
        "SET_FONT_SIZE_PERCENT",
        "SET_FONT_WEIGHT",
        "SET_TEXT_ALPHA_PERCENT",
        "SET_FILE_NAME_COLOR",
        "SET_FILE_NAME_BG_COLOR",
        "SET_DRAW_TEXT_BACKGROUND",
        "SET_TEXT_PLACEMENT_MODE",
        "SET_MAX_NAME_LENGTH",
        "SET_CURRENT_INDEX",
        "SET_DIVIDER_VISIBLE",
        "SET_DIVIDER_COLOR",
        "SET_DIVIDER_THICKNESS",
        "SET_GUIDES_ENABLED",
        "SET_GUIDES_THICKNESS",
        "SET_GUIDES_COLOR",
        "SET_GUIDES_SMOOTHING_ENABLED",
        "SET_GUIDES_SMOOTHING_INTERPOLATION_METHOD",
        "SET_MAGNIFIER_SIZE_RELATIVE",
        "TOGGLE_MAGNIFIER",
        "SET_MAGNIFIER_VISIBILITY",
        "TOGGLE_MAGNIFIER_ORIENTATION",
        "TOGGLE_FREEZE_MAGNIFIER",
        "SET_MAGNIFIER_POSITION",
        "SET_MAGNIFIER_INTERNAL_SPLIT",
        "UPDATE_MAGNIFIER_COMBINED_STATE",
        "SET_MAGNIFIER_OFFSET_RELATIVE",
        "SET_MAGNIFIER_SPACING_RELATIVE",
        "SET_OPTIMIZE_MAGNIFIER_MOVEMENT",
        "SET_MAGNIFIER_LASER_ENABLED",
        "SET_MAGNIFIER_MOVEMENT_INTERPOLATION_METHOD",
        "SET_MAGNIFIER_SCREEN_CENTER",
        "SET_MAGNIFIER_SCREEN_SIZE",
        "SET_CAPTURE_SIZE_RELATIVE",
        "SET_CAPTURE_VISIBLE",
        "SET_CAPTURE_COLOR",
        # multi_compare session state (slot "multi_compare.state"). Slots
        # are path-only (pixels live in the tab's session cache, never in
        # state), so a reference-snapshot undo of add/remove restores paths
        # that re-resolve against the still-warm cache.
        "multi_compare/add_slot",
        "multi_compare/remove_slot",
        "multi_compare/rename_slot",
        "multi_compare/swap_slots",
        "multi_compare/move_slot",
        "multi_compare/set_split_weights",
        "multi_compare/set_label_settings",
        "multi_compare/set_divider_settings",
        "multi_compare/set_zoom",
        "multi_compare/set_pan",
        "multi_compare/reset_view",
        "multi_compare/apply_layout_tree",
        # gallery session slot ("gallery"). Membership + triage ops only —
        # progress actions (GALLERY_THUMBNAILS_READY) and view state
        # (GALLERY_SELECTION_SET, GALLERY_FILTER_SET) stay out, mirroring
        # the multi_compare focus/pixel-note exclusion above. Slots are
        # path-only (pixmaps live in the tab's thumbnail cache, never in
        # state), so a reference-snapshot undo restores paths that
        # re-resolve against the still-warm cache.
        "GALLERY_POOL_OPENED",
        "GALLERY_PATHS_APPENDED",
        "GALLERY_POOL_REPLACED",
        "GALLERY_ITEMS_REMOVED",
        "GALLERY_TRIAGE_FINISHED",
    }
)

# Continuous-gesture action types: a single drag/scroll dispatches many of
# these. Coalesce consecutive same-type dispatches so one undo step returns
# to the pre-gesture state instead of stepping through every intermediate.
_COALESCE_TYPES = frozenset(
    {
        "SET_SPLIT_POSITION",
        "SET_MAGNIFIER_POSITION",
        "SET_MAGNIFIER_OFFSET_RELATIVE",
        "SET_MAGNIFIER_SPACING_RELATIVE",
        "SET_CAPTURE_SIZE_RELATIVE",
        "SET_MOVEMENT_SPEED",
        "multi_compare/set_zoom",
        "multi_compare/set_pan",
        "multi_compare/set_split_weights",
    }
)

# Non-continuous action types are grouped the same way (one undo step for a
# burst) when the *same type* is dispatched again within this window — a
# quick click-click on one control is one user intent, two clicks ten seconds
# apart are two. Matches the platform double-click interval convention.
_RAPID_ACTION_GROUP_MS = 400


def _targeted_slot_get(store, slot_name: str, session_id: str | None):
    """Session-addressed slot read for dispatch snapshot/verify.

    ``None`` keeps the exact historical call shape (active session) so
    legacy fake stores without the kwarg keep working.
    """
    if session_id is None:
        return store.get_session_state_slot(slot_name)
    return store.get_session_state_slot(slot_name, session_id=session_id)


def _history_lists_for(session) -> tuple[list, list, list]:
    """History stacks living in ``session``'s slots (get-or-create).

    Mirrors ``bind_history_for_session``'s ``_bind`` without moving the
    dispatcher's standing binding — a session-targeted dispatch records
    into the target's stacks while the active tab stays bound to its own.
    """
    def _get(slot: str) -> list:
        stack = session.state_slots.get(slot)
        if stack is None:
            stack = []
            session.state_slots[slot] = stack
        return stack

    return (
        _get(_ACTION_HISTORY_SLOT),
        _get(_UNDO_STACK_SLOT),
        _get(_REDO_STACK_SLOT),
    )


class Dispatcher:
    """Central Redux-style dispatcher.

    Thread-safety: ``dispatch`` prepares ``new_viewport``/``new_slots`` copies
    outside ``_lock`` (includes any ``ViewportState`` cloning / ``deepcopy`` of
    ``canvas_widget_state``). Under ``_lock`` it only does ``is``-comparison
    and an atomic swap of the already-prepared objects plus history
    bookkeeping. Subscriber notification and ``emit_state_change`` happen
    **outside** the lock (snapshot subscribers while locked, notify after),
    so a store ``on_change`` subscriber may ``dispatch`` synchronously
    without deadlock. Legacy call sites still use
    ``QTimer.singleShot(0, ...)`` deferral (e.g. ``menu_controller.py:161``)
    — now optional, not required.
    """

    def __init__(self, store):
        self._store = store
        self._reducer = RootReducer()
        self._lock = threading.Lock()
        self._subscribers: List[Callable[[Action], None]] = []
        self._action_history: List[Action] = []
        self._undo_stack: List[tuple] = []
        self._redo_stack: List[tuple] = []
        self._bound_session_id: str | None = None
        self._max_history_size = 100
        # Monotonic timestamp of the last undoable dispatch per action type —
        # drives the rapid-same-type grouping window.
        self._last_dispatch_ts: dict[str, float] = {}
        self._bind_history_to_active_session()

    def bind_history_for_session(self, session_id: str) -> None:
        """Point undo history (action list + undo/redo stacks) at ``session_id``'s slots."""
        with self._lock:
            if self._bound_session_id == session_id:
                return
            store = self._store
            get_session = getattr(store, "get_workspace_session", None)
            if not callable(get_session):
                return
            session = get_session(session_id)
            if session is None:
                return

            def _bind(slot: str):
                stack = session.state_slots.get(slot)
                if stack is None:
                    stack = []
                    session.state_slots[slot] = stack
                return stack

            self._action_history = _bind(_ACTION_HISTORY_SLOT)
            self._undo_stack = _bind(_UNDO_STACK_SLOT)
            self._redo_stack = _bind(_REDO_STACK_SLOT)
            self._bound_session_id = session_id

    def _bind_history_to_active_session(self) -> None:
        get_active = getattr(self._store, "get_active_workspace_session", None)
        if not callable(get_active):
            return
        try:
            active = get_active()
        except Exception:
            return
        if active is not None:
            self.bind_history_for_session(active.id)

    @property
    def store(self):
        return self._store

    def dispatch(self, action: Action, scope: str = "viewport", session_id: str | None = None) -> None:
        # Session targeting (Wave CORE): ``session_id=None`` (default) is
        # EXACTLY today's active-session behavior — every existing caller
        # keeps working unchanged. An explicit id addresses that session's
        # slots for snapshot → reduce → write-back, binds this dispatch's
        # history bookkeeping to that session's stacks (the dispatcher's
        # standing binding is left untouched, so the active tab's undo is
        # never unbound by a background dispatch), and leaves emit scope
        # unchanged (subscribers still filter; gallery handlers session-gate).
        # Viewport/settings stay active-scoped in both paths. Unknown ids
        # fail open to the active path. The optimistic-concurrency check
        # covers the targeted session consistently (snapshot vs write-back
        # on the same session), so a concurrent mutation of the target
        # retries instead of mis-writing.
        # Long-term Store lock fix (plan_image_compare_dnd_tiles.md Phase 3+):
        # Prepare new_viewport/new_slots copies outside the lock (includes any
        # ViewState/ViewportState cloning / dict copies / deepcopy of
        # canvas_widget_state). Under the lock only do is-comparison and an
        # atomic swap of the already-prepared objects plus history bookkeeping.
        # Subscriber snapshot is taken under lock, notification and
        # emit_state_change stay outside (reentrant-safe, dispatcher.py:280).
        from .slot_reducers import iter_state_slot_reducers

        slot_reducers = tuple(iter_state_slot_reducers())

        # Optimistic concurrency: prepare outside, verify identity under lock,
        # retry if another dispatch mutated the store since our snapshot.
        # In the common single-threaded Qt case the first attempt succeeds and
        # the critical section is only a handful of is checks + pointer swaps.
        subscribers: list = []
        emit_scope: str | None = None

        # Resolve the explicit target once (stable object identity across
        # optimistic retries — immune to a mid-dispatch tab switch).
        # None stays None: the default active path below is untouched.
        target_session = None
        target_id: str | None = None
        if session_id is not None:
            get_one = getattr(self._store, "get_workspace_session", None)
            if callable(get_one):
                try:
                    target_session = get_one(session_id)
                except Exception:
                    target_session = None
            if target_session is not None:
                target_id = session_id
            # Unknown id: fail open — target stays None and every
            # ``target_id``-gated call below takes the active shape.

        for _attempt in range(5):
            # Snapshot references without holding the lock (atomic GIL reads).
            # No deepcopy here — just identity capture for later is comparison.
            old_viewport = self._store.viewport
            old_settings = self._store.settings
            old_slots = {
                name: _targeted_slot_get(self._store, name, target_id)
                for name, _ in slot_reducers
            }

            # Heavy work outside lock: reducers may clone ViewportState /
            # deepcopy canvas_widget_state (now outside the critical section).
            try:
                # Default path keeps the historical 2-arg call shape so
                # reducer doubles without the kwarg keep working.
                if target_id is None:
                    new_store = self._reducer.reduce(self._store, action)
                else:
                    new_store = self._reducer.reduce(
                        self._store, action, session_id=target_id
                    )
            except Exception as e:
                logger.error(
                    f"Error dispatching action {action.type}: {e}", exc_info=True
                )
                raise

            if new_store is self._store:
                return

            new_viewport = new_store.viewport
            new_settings = new_store.settings
            new_slots = {
                name: new_store.get_session_state_slot(name)
                for name, _ in slot_reducers
            }

            # Critical section: only is-comparison + atomic swap + history.
            need_retry = False
            with self._lock:
                # Detect concurrent mutation since snapshot (optimistic check).
                if self._store.viewport is not old_viewport or self._store.settings is not old_settings:
                    need_retry = True
                else:
                    for name, _ in slot_reducers:
                        if _targeted_slot_get(self._store, name, target_id) is not old_slots[name]:
                            need_retry = True
                            break
                if need_retry:
                    continue

                # is-based no-op detection (no deepcopy, just identity).
                viewport_changed = new_viewport is not old_viewport
                settings_changed = new_settings is not old_settings
                slot_changed = False
                for name, _ in slot_reducers:
                    if new_slots[name] is not old_slots[name]:
                        slot_changed = True
                        break
                if not viewport_changed and not settings_changed and not slot_changed:
                    return

                # Atomic swap of the already-prepared copies.
                self._store.viewport = new_viewport
                self._store.settings = new_settings

                # The owning workspace session holds the state slots and
                # ``viewport``. Reducers return fresh instances, so the
                # session's references must be re-pointed here — otherwise
                # switching back to this session restores the pre-action
                # state (lost image lists, lost view state, etc).
                # Session-targeted dispatch re-points the TARGET session
                # (pre-resolved above); the default path re-resolves the
                # active session exactly as before.
                owner_session = target_session
                if owner_session is None:
                    get_active = getattr(self._store, "get_active_workspace_session", None)
                    if callable(get_active):
                        try:
                            owner_session = get_active()
                        except Exception:
                            owner_session = None

                for name, _ in slot_reducers:
                    new_value = new_slots[name]
                    if new_value is not old_slots.get(name):
                        if target_id is None:
                            self._store.set_session_state_slot(
                                name, new_value, emit_scope=""
                            )
                        else:
                            self._store.set_session_state_slot(
                                name, new_value, session_id=target_id, emit_scope=""
                            )
                        if owner_session is not None:
                            owner_session.state_slots[name] = new_value
                # Viewport stays active-scoped: the default path re-points
                # the active session exactly as before; an explicit target
                # never receives the active-derived viewport object (that
                # would alias two sessions onto one ViewportState).
                if target_id is None:
                    if owner_session is not None:
                        owner_session.viewport = new_viewport

                # History binds the TARGET session for explicit dispatches
                # (same stacks the active path would use), never the
                # currently-bound foreign session's stacks. The standing
                # binding is left untouched — a background dispatch must not
                # unbind the active tab's undo.
                if target_session is not None:
                    hist_list, undo_list, redo_list = _history_lists_for(target_session)
                else:
                    hist_list, undo_list, redo_list = (
                        self._action_history,
                        self._undo_stack,
                        self._redo_stack,
                    )
                hist_list.append(action)
                if len(hist_list) > self._max_history_size:
                    hist_list.pop(0)

                if action.type in _UNDOABLE_TYPES:
                    after = (new_viewport, new_slots)
                    before = (old_viewport, old_slots)
                    now = time.monotonic()
                    last_ts = self._last_dispatch_ts.get(action.type)
                    self._last_dispatch_ts[action.type] = now
                    rapid_burst = (
                        last_ts is not None
                        and (now - last_ts) * 1000.0 < _RAPID_ACTION_GROUP_MS
                    )
                    if (
                        not redo_list
                        and undo_list
                        and undo_list[-1][0] == action.type
                        and (
                            action.type in _COALESCE_TYPES or rapid_burst
                        )
                    ):
                        # Continuous gesture or a rapid same-type burst:
                        # move the undo "after" forward so one undo step
                        # returns to the pre-gesture/pre-burst state.
                        undo_list[-1] = (
                            action.type,
                            undo_list[-1][1],
                            after,
                        )
                    else:
                        undo_list.append((action.type, before, after))
                    if len(undo_list) > self._max_history_size:
                        undo_list.pop(0)
                    redo_list.clear()

                subscribers = list(self._subscribers)
                emit_scope = scope

            # Outside lock: notify subscribers and emit. Re-entrant dispatch is now safe.
            if emit_scope is not None:
                for subscriber in subscribers:
                    try:
                        subscriber(action)
                    except Exception as e:
                        logger.error(f"Error in dispatcher subscriber: {e}", exc_info=True)
                try:
                    self._store.emit_state_change(emit_scope)
                except Exception as e:
                    logger.error(f"Error emitting state change {emit_scope}: {e}", exc_info=True)
            return
        # Optimistic retries exhausted — extremely rare; log loudly instead
        # of silently dropping the update (a dropped dispatch with zero log
        # lines leaves Store and UI diverged invisibly).
        logger.error(
            "Dispatcher.dispatch exhausted 5 optimistic retries for action "
            "%r (scope=%r) — update dropped, store left at pre-dispatch state",
            getattr(action, "type", action),
            scope,
        )
        return

    def subscribe(self, callback: Callable[[Action], None]) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Action], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def get_action_history(self) -> List[Action]:
        with self._lock:
            return self._action_history.copy()

    def clear_history(self) -> None:
        with self._lock:
            self._action_history.clear()
            self._undo_stack.clear()
            self._redo_stack.clear()
            self._last_dispatch_ts.clear()

    # --- Undo / redo -----------------------------------------------------

    def _busy_loading(self) -> bool:
        """Undo is blocked while an image is loading/unifying (matches the
        video editor's policy of disabling undo during a load)."""
        try:
            render_cache = self._store.viewport.session_data.render_cache
            return bool(getattr(render_cache, "unification_in_progress", False))
        except Exception:
            return False

    def can_undo(self) -> bool:
        with self._lock:
            return bool(self._undo_stack) and not self._busy_loading()

    def can_redo(self) -> bool:
        with self._lock:
            return bool(self._redo_stack) and not self._busy_loading()

    def undo(self) -> None:
        # Lock discipline mirrors dispatch: mutate + snapshot under the lock,
        # emit outside it. Emitting inside the lock deadlocks a re-entrant
        # on_change subscriber that dispatches (Lock is not re-entrant) and
        # lets an emit exception corrupt the stacks mid-restore.
        scopes: list[str] | None = None
        with self._lock:
            if self._busy_loading() or not self._undo_stack:
                return
            entry = self._undo_stack.pop()
            self._redo_stack.append(entry)
            _type, before, _after = entry
            viewport, slots = before
            self._restore_session_state(viewport, slots)
            # The restored snapshot covers every registered slot — notify the
            # consumers that those scopes changed too (tabs use e.g. the
            # "document" scope to re-sync state that the reference snapshot
            # cannot carry, like a closed pixel store).
            scopes = ["viewport", *slots]
        for scope in scopes:
            try:
                self._store.emit_state_change(scope)
            except Exception as e:
                logger.error(f"Error emitting state change {scope}: {e}", exc_info=True)

    def redo(self) -> None:
        scopes: list[str] | None = None
        with self._lock:
            if self._busy_loading() or not self._redo_stack:
                return
            entry = self._redo_stack.pop()
            self._undo_stack.append(entry)
            _type, _before, after = entry
            viewport, slots = after
            self._restore_session_state(viewport, slots)
            scopes = ["viewport", *slots]
        for scope in scopes:
            try:
                self._store.emit_state_change(scope)
            except Exception as e:
                logger.error(f"Error emitting state change {scope}: {e}", exc_info=True)

    def _restore_session_state(self, viewport, slots) -> None:
        """Re-point the active session at a snapshot (mirrors dispatch's
        write-back, minus a reducer run and minus history recording).

        ``slots`` maps every registered slot-reducer name to its snapshot
        value; each is written back and re-pointed on the active session.
        """
        self._store.viewport = viewport
        active_session = None
        get_active = getattr(self._store, "get_active_workspace_session", None)
        if callable(get_active):
            try:
                active_session = get_active()
            except Exception:
                active_session = None
        for name, value in slots.items():
            self._store.set_session_state_slot(name, value, emit_scope="")
            if active_session is not None:
                active_session.state_slots[name] = value
        if active_session is not None:
            active_session.viewport = viewport