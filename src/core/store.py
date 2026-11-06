import logging
from contextlib import contextmanager
from typing import Any, Callable, List, Optional

from domain.workspace import WorkspaceState
from core.store_settings import SettingsState, WorkerStoreSnapshot
from core.store_operations import StoreOperationsMixin
from core.store_runtime_cache import ViewportRuntimeCache
from core.store_viewport import (
    GeometryState,
    InteractionState,
    RenderConfig,
    SessionData,
    ViewState,
    ViewportState,
)
from core.store_workspace import WorkspaceStoreMixin
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)
INITIAL_WORKSPACE_SESSION_TYPE = "session_picker"

__all__ = [
    "GeometryState",
    "InteractionState",
    "RenderConfig",
    "SessionData",
    "SettingsState",
    "Store",
    "ViewState",
    "ViewportState",
    "WorkerStoreSnapshot",
]

class Store(WorkspaceStoreMixin, StoreOperationsMixin):
    """Root Store — viewport / document / settings holder.

    Long-term Store lock fix (plan_image_compare_dnd_tiles.md): Dispatcher
    prepares new_viewport / new_slots copies (including ViewState
    ViewportState cloning and per-value copies of canvas_widget_state)
    *outside* Dispatcher._lock. The critical section only does ``is``
    identity comparison and an atomic pointer swap plus history. This keeps
    deepcopy / dict-copy work off the lock and preserves the reentrant-safe
    emit outside the lock (dispatcher.py:280).
    """

    def __init__(self):
        self._change_callbacks: List[Callable[[str], None]] = []
        self.state_changed = None
        self.workspace = WorkspaceState()
        self._pre_session_document: Optional[Any] = None
        self._pre_session_viewport: Optional[ViewportState] = ViewportState()
        self.settings = SettingsState()
        self.runtime_cache = ViewportRuntimeCache()
        self.recorder = None
        self._dispatcher = None
        self._change_batch_depth = 0
        self._change_batch_scopes: list[str] = []
        self.create_workspace_session(
            session_type=INITIAL_WORKSPACE_SESSION_TYPE,
            activate=True,
        )

    @property
    def document(self) -> Any:
        session = self.get_active_workspace_session()
        if session is None:
            return self._pre_session_document
        return session.document

    @document.setter
    def document(self, value: Any) -> None:
        session = self.get_active_workspace_session()
        if session is None:
            self._pre_session_document = value
        else:
            session.document = value

    @property
    def viewport(self) -> ViewportState:
        session = self.get_active_workspace_session()
        if session is None:
            return self._pre_session_viewport
        return session.viewport

    @viewport.setter
    def viewport(self, value: ViewportState) -> None:
        session = self.get_active_workspace_session()
        if session is None:
            self._pre_session_viewport = value
        else:
            session.viewport = value

    def on_change(self, callback: Callable[[str], None]) -> None:
        self._change_callbacks.append(callback)

    def set_dispatcher(self, dispatcher):
        self._dispatcher = dispatcher

    def get_dispatcher(self):
        return self._dispatcher

    def emit_state_change(self, scope: str = "viewport"):
        if self._change_batch_depth > 0:
            if scope not in self._change_batch_scopes:
                self._change_batch_scopes.append(scope)
            return
        for cb in self._change_callbacks:
            cb(scope)

    @contextmanager
    def batch_changes(self):
        """Coalesce change emissions inside the block into a single flush at exit.

        Several operations are semantically one user-visible transition but
        internally mutate the store in steps (e.g. ``create_workspace_session``
        + ``close_workspace_session`` when the session picker is replaced by a
        new session). Emitting per step makes the workspace UI sync to each
        intermediate state — and the adaptive tab strip legitimately holds
        *two* tabs between those steps, so an intermediate frame shows two tabs
        before the replacement lands. Batching defers every emission until the
        block ends, so listeners only ever see the coherent final state.
        Reads inside the block still see the mutated store immediately; only
        the change notifications are deferred. Scopes are flushed once each, in
        first-emitted order.

        Flush-on-success only: if the block raises, the queued scopes are
        discarded (never flushed) and the exception propagates — emitting a
        "coherent final state" that was never reached would sync listeners
        to a half-applied transition.
        """
        self._change_batch_depth += 1
        try:
            yield
        except BaseException:
            self._change_batch_depth -= 1
            if self._change_batch_depth == 0:
                self._change_batch_scopes = []
            raise
        else:
            self._change_batch_depth -= 1
            if self._change_batch_depth == 0:
                scopes = list(self._change_batch_scopes)
                self._change_batch_scopes = []
                for scope in scopes:
                    self.emit_state_change(scope)

    def transact(self, actions: list, scope: str = "document", session_id: str | None = None) -> None:
        """Single-dispatch transaction (plan_image_pipeline.md Phase 5).

        Coalesces N actions (e.g. SetFullResImage + SetImagePath + SetPreview)
        into one ``Dispatcher.dispatch(TransactionAction)`` → one
        ``RootReducer.reduce`` → one ``emit_state_change(scope)``. The reducer
        work (new_viewport / new_slots copies) is prepared outside
        Dispatcher._lock; the lock only does ``is`` comparison + atomic swap.

        ``session_id`` threads through to ``Dispatcher.dispatch`` (Wave CORE
        session targeting): None = active-session behavior, explicit id =
        that session's slots. Falls back to sequential dispatch for fake
        stores without dispatcher.
        """
        dispatcher = self.get_dispatcher()
        if dispatcher is None:
            # Fail LOUD, never silent-drop: without a dispatcher there is no
            # reducer run, no history, and no emit — swallowing the actions
            # would leave Store and UI diverged with zero log lines. Fake
            # stores in tests must wire a dispatcher (real or double) before
            # calling transact.
            logger.error(
                "Store.transact dropped %d action(s): no dispatcher wired "
                "(scope=%r). Wire set_dispatcher() before transact.",
                len(actions),
                scope,
            )
            raise RuntimeError(
                f"Store.transact requires a dispatcher (got None); "
                f"refusing to silently drop {len(actions)} action(s) "
                f"(scope={scope!r})"
            )
        # fast path: single action → direct dispatch (avoid wrapping)
        if len(actions) == 1:
            if session_id is None:
                dispatcher.dispatch(actions[0], scope=scope)
            else:
                dispatcher.dispatch(actions[0], scope=scope, session_id=session_id)
            return
        try:
            from core.state_management.transaction import TransactionAction

            if session_id is None:
                dispatcher.dispatch(TransactionAction(actions), scope=scope)
            else:
                dispatcher.dispatch(
                    TransactionAction(actions), scope=scope, session_id=session_id
                )
        except Exception:
            # fallback: sequential — each failure is logged loudly, never
            # skipped in silence.
            logger.exception(
                "Store.transact TransactionAction failed; falling back to "
                "sequential dispatch of %d action(s) (scope=%r)",
                len(actions),
                scope,
            )
            for a in actions:
                try:
                    if session_id is None:
                        dispatcher.dispatch(a, scope=scope)
                    else:
                        dispatcher.dispatch(a, scope=scope, session_id=session_id)
                except Exception:
                    logger.exception(
                        "Store.transact sequential fallback failed for "
                        "action %r (scope=%r)",
                        getattr(a, "type", a),
                        scope,
                    )
                    raise

    def publish(self, scope: str) -> None:
        """Best-effort publish for LOD / non-document scopes (phase 4: lod_available).

        Best-effort means delivery is still attempted — emit errors are
        logged loudly (never swallowed) so a broken subscriber is visible.
        """
        try:
            self.emit_state_change(scope)
        except Exception:
            logger.exception("Store.publish failed for scope %r", scope)

    def emit_viewport_change(self, subdomain: str | None = None) -> None:
        scope = "viewport"
        if subdomain:
            scope = f"viewport.{subdomain}"
        self.emit_state_change(scope)

    def build_worker_snapshot(self, viewport: ViewportState, document: Any):
        return WorkerStoreSnapshot(
            viewport, self.settings.freeze_for_export(), document
        )