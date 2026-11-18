"""Controller for multi-compare tab — load/save with container-tree layout.

Owns wiring (widget signals, settings/ui-mode subscriptions) and instance
state; the actual loading/pyramid/toast and export/save logic live in
``use_cases/loading.py`` and ``use_cases/export.py`` (mirrors image_compare's
``_session_controller.py`` + ``use_cases/`` split) — this class stays a thin
set of delegators plus the state those modules read and write.
Audit-Meta: pattern=thin-owner reason="B1 owns the session pixel cache + lazy-restore demand fill here; loading/pyramid/toast stay in use_cases (further split tracked post-B1, not across B2)"
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFileDialog

from shared.image_extensions import ACCEPTED_IMAGE_EXTENSIONS as _IMAGE_EXTENSIONS
from tabs.multi_compare.scene import actions as mc_actions
from tabs.multi_compare.services.gpu_export import MultiCompareGpuExporter
from tabs.multi_compare.services.save_flow import MultiCompareSaveFlowCoordinator
from tabs.multi_compare.use_cases import export as export_use_cases
from tabs.multi_compare.use_cases import loading as loading_use_cases
from tabs.multi_compare.use_cases import preview_decode as preview_decode_use_cases
from tabs.multi_compare.widget import MultiCompareWidget
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


class MultiCompareController:
    """Manages image loading and save composition for the multi-compare widget."""

    SAVE_OUTPUT_W = 1920
    SAVE_OUTPUT_H = 1080
    PREVIEW_MAX_EDGE = 1024

    # Progress checkpoints re-exported from shared (single source).
    @property
    def _DECODE_DONE_PROGRESS(self) -> int:  # type: ignore[override]
        from tabs._shared.loading_toast import DECODE_DONE_PROGRESS as _V

        return _V

    @property
    def _PYRAMID_START_PROGRESS(self) -> int:  # type: ignore[override]
        from tabs._shared.loading_toast import PYRAMID_START_PROGRESS as _V

        return _V

    def __init__(
        self,
        widget: MultiCompareWidget,
        store: Any = None,
        *,
        translate=None,
        open_export_dialog=None,
        context=None,
    ):
        self.widget = widget
        self.store = store
        self.translate = translate or (lambda _key, default=None: default or _key)
        # Host service (``open_image_export_dialog``). Returns at once with the
        # live dialog — it is non-modal, so callers must pass
        # ``on_finished=`` and continue the export there (see
        # ``use_cases/export.on_save_requested``).
        self.open_export_dialog = open_export_dialog
        self.context = context
        # B1: the session pixel cache lives here (tab-page lifetime, shared
        # across this page's MC sessions — keys are content-addressed).
        # Redux state holds paths only; every tier arrival does
        # cache.put_* + NoteSlotPixels dispatch (STORE replace-only).
        from tabs.multi_compare.pipeline.cache import MultiComparePixelCache

        self.pixel_cache = MultiComparePixelCache()
        try:
            canvas = getattr(widget, "canvas", None)
            if canvas is not None:
                canvas.pixel_cache = self.pixel_cache
        except Exception:
            pass
        # Backref for use_cases that only receive the widget (duplicate
        # finalize demand-fill kick). Cycles are GC-safe; fakes without a
        # controller read None via getattr.
        try:
            widget._controller = self
        except Exception:
            pass
        self._gpu_exporter: MultiCompareGpuExporter | None = None
        self._save_flow: MultiCompareSaveFlowCoordinator | None = None
        self._last_applied_ui_mode: str | None = None
        # B2/B3 shared coordinators (state-owning collaborators)
        from tabs._shared.loading_toast import LoadingToastCoordinator
        from engine.pyramid.coordinator import PyramidBuildCoordinator

        def _mc_get_toast_manager():
            main_window = getattr(self.context, "main_window", None) if self.context else None
            return getattr(main_window, "toast_manager", None) if main_window else None

        self._loading_toast_coordinator = LoadingToastCoordinator(
            get_toast_manager=_mc_get_toast_manager,
            translate=self.translate,
        )
        self._loading_toasts: dict[int, int] = self._loading_toast_coordinator._loading_toasts  # type: ignore[attr-defined]

        def _mc_request_update():
            canvas = getattr(self.widget, "canvas", None)
            if canvas is not None:
                try:
                    canvas.request_view_update()
                except Exception:
                    pass

        self._pyramid_coordinator = PyramidBuildCoordinator(
            get_thread_pool=lambda: getattr(self.context, "thread_pool", None) if self.context else None,
            toast_coordinator=self._loading_toast_coordinator,
            request_view_update=_mc_request_update,
        )
        self._pyramid_builds: set[int] = self._pyramid_coordinator._pyramid_builds
        self._pyramid_toast_slot: dict[int, int] = self._pyramid_coordinator._pyramid_toast_slot

        # DI CropService (как в image_compare._session_controller._get_crop_service)
        try:
            from shared.image_processing.autocrop import CropService

            self._crop_service = CropService()
        except Exception:
            self._crop_service = None  # type: ignore[attr-defined]

        self.widget.images_dropped.connect(self._on_images_dropped)
        self.widget.add_requested.connect(self._on_add_requested)
        self.widget.save_requested.connect(self._on_save_requested)
        self.widget.quick_save_requested.connect(self._on_quick_save_requested)
        self.widget.settings_requested.connect(self._on_settings_requested)
        self.widget.help_requested.connect(self._on_help_requested)
        self.widget.divider_color_picker_requested.connect(
            self._on_divider_color_picker_requested
        )
        self._apply_ui_mode_to_toolbar()
        self._subscribe_to_ui_mode_changes()
        self._subscribe_to_settings_store_changes()

    def _get_crop_service(self):
        """Вернуть CropService если autocrop включён, иначе None (зеркало IC)."""
        try:
            should_crop = getattr(self.store.settings, "auto_crop_black_borders", False) if self.store else True
        except Exception:
            should_crop = True
        if not should_crop:
            return None
        return getattr(self, "_crop_service", None)

    def _call_service(self, name: str) -> None:
        if self.context is None:
            return
        try:
            self.context.call_service(name)
        except Exception:
            logger.exception("Tab service %s failed", name)

    def _on_settings_requested(self) -> None:
        self._call_service("show_settings_dialog")

    def _apply_ui_mode_to_toolbar(self) -> None:
        settings = getattr(self.store, "settings", None) if self.store else None
        mode = getattr(settings, "ui_mode", "beginner") or "beginner"
        self._apply_ui_mode(mode, source="initial-or-store-read")

    def _subscribe_to_ui_mode_changes(self) -> None:
        """React to global ui_mode switches the same way image-compare's LayoutPlugin does."""
        event_bus = getattr(self.context, "event_bus", None) if self.context else None
        if event_bus is None:
            return
        try:
            from plugins.settings.events import SettingsUIModeChangedEvent
        except Exception:
            return
        event_bus.subscribe(
            SettingsUIModeChangedEvent,
            self._on_ui_mode_changed_event,
        )

    def _on_ui_mode_changed_event(self, event) -> None:
        mode = getattr(event, "ui_mode", "beginner") or "beginner"
        self._apply_ui_mode(mode, source="event")

    def _subscribe_to_settings_store_changes(self) -> None:
        if self.store is None or not hasattr(self.store, "on_change"):
            return
        self.store.on_change(self._on_store_changed)

    def _on_store_changed(self, scope: str) -> None:
        settings = getattr(self.store, "settings", None) if self.store else None
        mode = getattr(settings, "ui_mode", "beginner") or "beginner"
        mode_changed = mode != self._last_applied_ui_mode
        if scope != "settings" and not mode_changed:
            return
        self._apply_ui_mode(mode, source=f"store:{scope}")

    def _apply_ui_mode(self, mode: str, *, source: str) -> None:
        toolbar = getattr(self.widget, "toolbar", None)
        if toolbar is None or not hasattr(toolbar, "apply_ui_mode"):
            return
        toolbar.apply_ui_mode(mode)
        self.widget.sync_divider_toolbar()
        self._last_applied_ui_mode = mode
        # Hidden toolbar slots must drop Find Action rows and shortcuts.
        self._resync_action_shortcuts_after_mode_change()

    def _resync_action_shortcuts_after_mode_change(self) -> None:
        try:
            from ui.actions.binder import resync_action_shortcuts

            window = self.widget.window() if self.widget is not None else None
            resync_action_shortcuts(window, active_tab="multi_compare")
        except Exception:
            logger.debug(
                "[mc-actions] shortcut resync after ui_mode failed",
                exc_info=True,
            )

    def _on_divider_color_picker_requested(self) -> None:
        existing = getattr(self, "_divider_color_dialog", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            return

        from ui.widgets.color import ColorPickerDialog

        current = QColor(*self.widget.state.divider_settings.color_rgba)
        dialog = ColorPickerDialog(
            current,
            self.widget.window(),
            title=self.translate("ui.choose_divider_line_color", "Choose divider color"),
            show_alpha=True,
        )
        dialog.setModal(False)

        def on_color_selected(color):
            if color.isValid():
                self.widget.apply_divider_color(color)

        def on_finished(_result):
            self._divider_color_dialog = None

        dialog.colorSelected.connect(on_color_selected)
        dialog.finished.connect(on_finished)
        self._divider_color_dialog = dialog
        dialog.show()

    def _on_help_requested(self) -> None:
        self._call_service("show_help_dialog")

    def _on_quick_save_requested(self) -> None:
        export_use_cases.on_quick_save_requested(self)

    def _get_save_flow(self) -> MultiCompareSaveFlowCoordinator:
        return export_use_cases.get_save_flow(self)

    def shutdown(self) -> None:
        if self._save_flow is not None:
            self._save_flow._flow.cancel_all_exports()
        if self._gpu_exporter is not None:
            self._gpu_exporter.shutdown()
            self._gpu_exporter = None

    def load_images(self, paths: list[Path]) -> int:
        """Toolbar "Add images" dialog entry — thin delegate (P8).

        Returns slots created synchronously; dialog confirm with ≥1
        valid file always yields a slot + preview/error-toast, never a
        silent 0 slots (see ``use_cases.dialog_add.load_images``).
        """
        from tabs.multi_compare.use_cases import dialog_add as dialog_add_use_cases

        return dialog_add_use_cases.load_images(self, paths)

    def load_external_paths(self, paths, side=None) -> int:
        """Direct-load for chrome/carry/paste drops (P3A) — thin delegate.

        ``side`` is the placement axis a caller may name instead of a canvas
        position (the control surface's ``open_folder(side=...)``); ``None``
        keeps auto placement.
        """
        return loading_use_cases.load_external_paths(self, paths, side=side)

    def begin_paste_placement(self, paths: list[Path]) -> None:
        """Paste entry — direct-loads like IC (P3A), no pending-paste arming.

        Previously armed ``widget.begin_pending_paste`` (highlight + click
        to place, ``Esc`` to cancel); now auto-places through the P2 async
        path (``load_external_paths``) so clipboard/carry content appears
        with a loading toast immediately.
        """
        self.load_external_paths(paths)

    def rehydrate_slots(self, state) -> bool:
        """Lazy rehydrate — paths only, zero sync decodes (B1, IC parity).

        ``deserialize_session`` already restored the path-only slots; this
        only kicks async demand fill for the *visible* (active-session)
        slots that have no cached tier yet. Project reopen therefore calls
        ``load_pixel_store`` 0 times — history stays path-only until demand
        (same posture as image_compare's lazy ``rehydrate_session``).

        Returns True when at least one fill was kicked (callers refresh the
        widget unconditionally — ordering, not payload: the old
        ``if not changed: return`` gate skipped the refresh exactly when
        nothing was decoded, leaving restored sessions blank after tab
        switch — P6).

        Dormant sessions (``state`` is not the visible one) record paths
        only — their fill is kicked on activation
        (``ensure_visible_slots_loading`` with no args), so a project load
        never shows another session's toasts on the active grid.
        """
        try:
            visible = self.widget.state
        except Exception:
            visible = None
        if state is not None and visible is not None and state is not visible:
            return False
        return self.ensure_visible_slots_loading(state)

    def ensure_visible_slots_loading(self, state=None) -> bool:
        """Kick async preview fill for imageless slots with files on disk.

        Activation/restore entry (P6 ordering fix): fill is tied to the
        session becoming visible, not to load-time conditionals. Slots
        whose file is gone are left imageless (no toast, no removal — a
        reopened project must keep its layout); slots already cached or
        already inflight are skipped. Restore fills pass
        ``keep_slot_on_error`` so a corrupt-at-restore file surfaces an
        error toast instead of deleting the restored slot.
        """
        from pathlib import Path as _Path

        from tabs.multi_compare.pipeline.cache import resolve_slot_source
        from tabs.multi_compare.use_cases import preview_decode as _preview

        try:
            live = self.widget.state if state is None else state
            slots = list(live.slots)
        except Exception:
            return False
        kicked = False
        for slot in slots:
            path = slot.path if isinstance(slot.path, _Path) else None
            if slot.path is not None and not isinstance(slot.path, _Path):
                try:
                    path = _Path(slot.path)
                except Exception:
                    path = None
            if path is None:
                continue
            try:
                if resolve_slot_source(self.pixel_cache, slot) is not None:
                    continue
            except Exception:
                pass
            try:
                if not path.is_file():
                    continue
            except Exception:
                continue
            try:
                if _preview.has_pending_load(self, slot.id, path):
                    continue
            except Exception:
                pass
            try:
                self._show_loading_toast(slot.id)
                _preview.load_preview_async(
                    self, path, slot.id, keep_slot_on_error=True
                )
                kicked = True
            except Exception:
                logger.exception(
                    "mc: demand fill kick failed for slot %s", slot.id
                )
        return kicked

    def clear(self) -> None:
        self.widget.store.dispatch(mc_actions.clear())

    def remove_slot(self, slot_id: int) -> None:
        """Remove a slot, aborting its inflight preview/full decodes first (A2).

        Thin delegate over ``preview_decode.remove_slot_with_cancel`` —
        the AbortSignal-style cancel-on-remove entry point (the widget's
        direct ``placement.remove_slot`` path stays safe via stale guards).
        """
        preview_decode_use_cases.remove_slot_with_cancel(self, slot_id)

    def set_slot_crop_override(self, slot_id: int, value: bool | None) -> None:
        """Flip a slot's parametric crop override (IC context-crop parity).

        Pure param write: look the slot up in widget state, dispatch
        ``SetCropOverride`` via the widget store, request a canvas view
        update — no evict, no reload, no decode. Fail-soft, never raises.
        """
        try:
            slots = list(getattr(getattr(self.widget, "state", None), "slots", None) or [])
        except Exception:
            return
        slot = next((s for s in slots if getattr(s, "id", None) == slot_id), None)
        if slot is None:
            return
        try:
            self.widget.store.dispatch(mc_actions.set_crop_override(slot_id, value))
        except Exception:
            return
        try:
            canvas = getattr(self.widget, "canvas", None)
            if canvas is not None:
                update = getattr(canvas, "request_view_update", None)
                if callable(update):
                    update()
                else:
                    canvas.update()
        except Exception:
            pass

    def cancel_slot_loads(self, slot_id: int) -> None:
        """Abort inflight decodes for ``slot_id`` without removing the slot."""
        preview_decode_use_cases.cancel_slot_loads(self, slot_id)

    def _on_images_dropped(self, paths: list, target, side) -> None:
        # P7: kept (not collapsed into drop_event's defer) on purpose. The
        # canvas dropEvent already returns past accept before emitting, but
        # other emitters (tab.handle_drop's no-controller fallback,
        # finalize_pending_paste) can fire inside a window-accept window, so
        # this singleShot(0) is the load-past-finish guarantee for ALL of
        # them. Cost is one extra event-loop hop (~0ms), not cursor-relevant:
        # decoding already happens in GenericWorkers (P2), and the pre-accept
        # sync segment (resolve/dispatch/stat) moved to _finish_external_drop.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(
            0,
            lambda p=list(paths), t=target, s=side: loading_use_cases.on_images_dropped(
                self, p, t, s
            ),
        )

    def _on_add_requested(self) -> None:
        start_dir = export_use_cases.default_dir(self)
        from shared.image_extensions import build_image_dialog_filter

        filters = build_image_dialog_filter()
        paths, _ = QFileDialog.getOpenFileNames(
            self.widget, "Add images to compare", start_dir, filters
        )
        if paths:
            self.load_images([Path(p) for p in paths])

    def _on_save_requested(self) -> None:
        export_use_cases.on_save_requested(self)

    def _read_image(self, path: Path, *, slot_id: int | None = None, start_pyramid: bool = True):
        return loading_use_cases.read_image(
            self, path, slot_id=slot_id, start_pyramid=start_pyramid
        )

    def _start_pyramid_build(self, store, *, slot_id: int | None = None) -> None:
        # Coordinator owns thread_pool + toast routing.
        # MC abort predicate: not pyramid.valid (capture resolved pyramid).
        from engine.store.pyramid_registry import ensure_pyramid as _ensure

        _pyr = _ensure(store)

        def _should_abort(_p=_pyr):
            return not getattr(_p, "valid", True) if _p is not None else False

        # If pyramid is None, coordinator will handle finishing toast itself.
        self._pyramid_coordinator.start_build(store, slot_id=slot_id, should_abort=_should_abort)

    def _on_pyramid_level_ready(self, payload=None) -> None:
        self._pyramid_coordinator.on_level_ready(payload)

    def _show_loading_toast(self, slot_id: int) -> None:
        self._loading_toast_coordinator.show(slot_id)

    def _mark_full_res_ready(self, slot_id: int) -> None:
        self._loading_toast_coordinator.mark_full_res_ready(slot_id)

    def _finish_loading_toast(self, slot_id: int) -> None:
        self._loading_toast_coordinator.finish(slot_id)

    def _dismiss_loading_toast(self, slot_id: int) -> None:
        self._loading_toast_coordinator.dismiss(slot_id)

    def report_user_error(self, message: str) -> None:
        """User-visible failure signal for menu/dnd paths (P2 hygiene).

        Single channel for duplicate-full, carry-miss, and queue-overflow
        summaries: ``CoreErrorOccurredEvent`` on the tab bus (→ warning
        dialog by house policy, same as MC load errors). Falls back to a
        loud log when no bus is reachable — never silent, never raising.
        """
        try:
            event_bus = loading_use_cases._resolve_mc_event_bus(self)
        except Exception:
            event_bus = None
        if event_bus is None:
            logger.warning("mc: %s", message)
            return
        try:
            from core.events import CoreErrorOccurredEvent

            event_bus.emit(CoreErrorOccurredEvent(message))
        except Exception:
            logger.exception("mc: failed to emit user error: %s", message)

    def _load_full_resolution_async(
        self, path: Path, slot_id: int, *, keep_slot_on_error: bool = False
    ) -> None:
        loading_use_cases.load_full_resolution_async(
            self, path, slot_id, keep_slot_on_error=keep_slot_on_error
        )

    def _on_full_resolution_error(
        self, path: Path, slot_id: int, err, *, keep_slot_on_error: bool = False
    ) -> None:
        loading_use_cases.on_full_resolution_error(
            self, path, slot_id, err, keep_slot_on_error=keep_slot_on_error
        )

    def _apply_full_resolution(
        self, slot_id: int, path: Path, store, *, keep_slot_on_error: bool = False
    ) -> None:
        loading_use_cases.apply_full_resolution(
            self, slot_id, path, store, keep_slot_on_error=keep_slot_on_error
        )

    def _load_single_auto(self, path: Path) -> int | None:
        from tabs.multi_compare.use_cases import dialog_add as dialog_add_use_cases

        return dialog_add_use_cases.load_single_auto(self, path)

    def _native_canvas_size(self) -> tuple[int, int] | None:
        return export_use_cases.native_canvas_size(self)

    def _compose_image(
        self,
        w: int | None = None,
        h: int | None = None,
        *,
        background_color: QColor | None = None,
        fill_background: bool = False,
    ):
        return export_use_cases.compose_image(
            self, w, h, background_color=background_color, fill_background=fill_background
        )