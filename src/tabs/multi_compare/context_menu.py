from __future__ import annotations

from tabs.host_helpers import AppTextInputDialog
from sli_ui_toolkit.widgets import (
    ContextMenuAction,
    ContextMenuEntry,
    ContextMenuSeparator,
)

from plugins.image_properties.plugin import open_image_properties_dialog
from sli_ui_toolkit.i18n import get_current_language
from sli_ui_toolkit.i18n import tr as app_tr
from tabs.multi_compare.models import CompareSlot, leaves
from tabs.multi_compare.scene import actions
from ui.context_menu.models import ContextMenuRequest
from tabs.multi_compare.icons import Icon
from core.log_names import APP_LOGGER_NAME


class MultiCompareContextMenuProvider:
    def __init__(self, widget):
        self.widget = widget

    def entries_for(self, request: ContextMenuRequest) -> tuple[ContextMenuEntry, ...]:
        if request.session_type != "multi_compare":
            return ()
        if request.source_widget is not self.widget.canvas:
            return ()
        if request.target.kind != "multi_compare_slot":
            return ()
        slot = self._slot(request)
        if slot is None:
            return ()
        can_duplicate = self._has_pixels(slot)
        return (
            ContextMenuAction(
                "multi_compare.rename_slot",
                self._tr("context.rename", "Rename"),
                icon=Icon.TEXT_MANIPULATOR,
                data=slot.id,
            ),
            ContextMenuAction(
                "multi_compare.duplicate_slot",
                self._tr("context.duplicate", "Duplicate"),
                icon=Icon.ADD,
                enabled=can_duplicate,
                data=slot.id,
            ),
            ContextMenuAction(
                "multi_compare.show_slot_properties",
                self._tr("context.properties", "Properties"),
                icon=Icon.PHOTO,
                data=slot.id,
            ),
            ContextMenuAction(
                "multi_compare.crop_slot",
                self._tr("context.crop", "Crop"),
                icon=Icon.SCISSORS,
                checkable=True,
                checked=self._crop_effective(slot),
                data=slot.id,
            ),
            ContextMenuSeparator(),
            ContextMenuAction(
                "multi_compare.remove_slot",
                self._tr("context.remove", "Remove"),
                icon=Icon.DELETE,
                danger=True,
                data=slot.id,
            ),
        )

    def execute(
        self, action_id: str, request: ContextMenuRequest, data: object
    ) -> bool:
        if not action_id.startswith("multi_compare."):
            return False
        slot = self._slot(request, slot_id=data)
        if slot is None:
            return True
        if action_id == "multi_compare.rename_slot":
            self._rename_slot(slot)
            return True
        if action_id == "multi_compare.duplicate_slot":
            self._duplicate_slot(slot)
            return True
        if action_id == "multi_compare.show_slot_properties":
            self._show_properties(slot)
            return True
        if action_id == "multi_compare.crop_slot":
            self._toggle_crop(slot)
            return True
        if action_id == "multi_compare.remove_slot":
            self._remove_slot(slot)
            return True
        return False

    def _slot(
        self, request: ContextMenuRequest, slot_id: object = None
    ) -> CompareSlot | None:
        sid = slot_id if slot_id is not None else request.target.id
        try:
            sid = int(sid)  # type: ignore[arg-type]  # sid is duck-typed
        except (TypeError, ValueError):
            return None
        return next((slot for slot in self.widget.state.slots if slot.id == sid), None)

    @staticmethod
    def _cache(widget):
        canvas = getattr(widget, "canvas", None)
        return getattr(canvas, "pixel_cache", None)

    def _has_pixels(self, slot: CompareSlot) -> bool:
        """B3: single shared gate — delegates to ``slot_has_pixels``."""
        try:
            from tabs.multi_compare.pipeline.cache import slot_has_pixels

            return slot_has_pixels(self._cache(self.widget), slot)
        except Exception:
            return False

    def _pixel_source(self, slot: CompareSlot):
        try:
            from tabs.multi_compare.pipeline.cache import resolve_slot_source

            return resolve_slot_source(self._cache(self.widget), slot)
        except Exception:
            return None

    def _remove_slot(self, slot: CompareSlot) -> None:
        """Removal via the controller path so inflight decodes cancel first.

        ``controller.remove_slot`` → ``preview_decode.remove_slot_with_cancel``
        (AbortSignal-style cancel-on-remove); the widget's direct dispatch
        is the headless fallback (same end state, no cancel).
        """
        try:
            ctrl = getattr(self.widget, "_controller", None)
        except Exception:
            ctrl = None
        remove = getattr(ctrl, "remove_slot", None)
        if callable(remove):
            try:
                remove(slot.id)
                return
            except Exception:
                pass
        self.widget.remove_slot(slot.id)

    def _fail(self, message: str) -> None:
        """User-visible failure signal (toast/bus, never silent, never raising).

        Routes through the controller's single error channel
        (``CoreErrorOccurredEvent`` → warning dialog by house policy);
        falls back to a loud log when no controller/bus is reachable
        (headless fakes).
        """
        try:
            ctrl = getattr(self.widget, "_controller", None)
        except Exception:
            ctrl = None
        try:
            from tabs._shared.user_errors import emit_user_error

            emit_user_error(ctrl if ctrl is not None else self.widget, message)
            return
        except Exception:
            pass
        import logging as _logging

        _logging.getLogger(APP_LOGGER_NAME).warning("mc menu: %s", message)

    def _rename_slot(self, slot: CompareSlot) -> None:
        text, ok = AppTextInputDialog.get_text(
            self.widget,
            self._tr("context.rename", "Rename"),
            self._tr("context.name", "Name"),
            slot.label,
        )
        if ok:
            self.widget.store.dispatch(actions.rename_slot(slot.id, text.strip()))

    def _duplicate_slot(self, slot: CompareSlot) -> None:
        if not self._has_pixels(slot) or self.widget.state.root is None:
            self._fail(
                self._tr(
                    "msg.compare_nothing_to_duplicate",
                    "Nothing to duplicate — this slot has no loaded image.",
                )
            )
            return
        self.widget.begin_pending_duplicate(slot.id)

    def _global_crop_enabled(self) -> bool:
        """Global ``auto_crop_black_borders`` fallback for Auto (None) slots."""
        try:
            ctrl = getattr(self.widget, "_controller", None)
            store = getattr(ctrl, "store", None)
            settings = getattr(store, "settings", None)
            if settings is None:
                store = getattr(self.widget, "store", None)
                settings = getattr(store, "settings", None)
            return bool(getattr(settings, "auto_crop_black_borders", False))
        except Exception:
            return True

    def _crop_effective(self, slot: CompareSlot) -> bool:
        """Effective crop toggle state: explicit override, else global setting."""
        try:
            override = getattr(slot, "crop_override", None)
        except Exception:
            return self._global_crop_enabled()
        if override is None:
            return self._global_crop_enabled()
        try:
            return bool(override)
        except Exception:
            return self._global_crop_enabled()

    def _toggle_crop(self, slot: CompareSlot) -> None:
        """Flip the effective crop state into an explicit override (fail-soft).

        Mirrors IC ``_cycle_crop_override_slot``: the menu always writes an
        explicit bool (Auto is not reachable from the menu, but a stored
        None keeps following the global setting and toggles from its
        effective state). Prefers the controller path (canvas refresh
        included); the widget dispatch is the headless fallback.
        """
        try:
            nxt = not self._crop_effective(slot)
        except Exception:
            return
        if getattr(slot, "path", None) is None:
            return  # imageless slot: nothing to crop, keep Auto
        try:
            ctrl = getattr(self.widget, "_controller", None)
            setter = getattr(ctrl, "set_slot_crop_override", None)
            if callable(setter):
                setter(slot.id, nxt)
                return
        except Exception:
            pass
        try:
            self.widget.store.dispatch(actions.set_crop_override(slot.id, nxt))
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

    def _show_properties(self, slot: CompareSlot) -> None:
        ordered = [leaf.slot_id for leaf in leaves(self.widget.state.root)]
        total = len(ordered)
        try:
            position = ordered.index(slot.id) + 1
            position_text = f"{position} / {total}"
        except ValueError:
            position_text = str(slot.id)
        open_image_properties_dialog(
            path=slot.path,
            display_name=slot.label,
            image=self._pixel_source(slot),
            app_rows=(("image_properties.position", "Position", position_text),),
            language=get_current_language() or "en",
            tr_func=app_tr,
        )

    def _tr(self, key: str, default: str) -> str:
        translate = getattr(self.widget, "_translate", None)
        if callable(translate):
            return translate(key, default)
        return default