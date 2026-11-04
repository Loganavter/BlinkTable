import logging

from PySide6.QtCore import Qt

from core.constants import AppConstants
from resources.translations import tr
from shared.video_policy import FpsPolicy, clamp_fps
from ui.canvas_infra.scene.registry import get_canvas_registry
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

class DialogManager:
    def __init__(self, host):
        self.host = host
        # Non-modal export dialogs stay referenced here until their `finished`
        # handler runs; a QDialog parented to the shell survives in C++, but
        # the Python wrapper must not depend on that for an async close.
        self._open_export_dialogs: list = []

    @property
    def settings_dialog(self):
        return self.host._settings_dialog

    def _resolve_active_tab(self) -> str | None:
        store = getattr(self.host, "store", None)
        if store is None:
            return None
        try:
            session = store.get_active_workspace_session()
        except Exception:
            return None
        return getattr(session, "session_type", None) if session is not None else None

    def _query_metrics_settings(self) -> tuple[bool, bool]:
        from tabs.registry import TabRegistry

        registry = TabRegistry()
        registry.discover()
        result = registry.create_service(
            "settings_metrics_query", self.host.store
        )
        if result is None:
            return False, False
        return result

    def _query_overlay(self, capability_id: str, default=None):
        command = get_canvas_registry(self._resolve_active_tab()).get_feature_command_by_alias(
            capability_id
        )
        if command is None:
            return default
        result = command(self.host.store)
        return default if result is None else result

    def show_help_dialog(
        self,
        *,
        page: str | None = None,
        anchor: str | None = None,
        video_url: str | None = None,
        learn_more_url: str | None = None,
    ):
        if self.host.main_controller is None:
            logger.warning("UIManager.show_help_dialog: plugin coordinator is unavailable")
            return

        try:
            self.host.main_controller.execute_plugin_command(
                "help",
                "show_dialog",
                parent=self.host.parent_widget,
                language=self.host.store.settings.current_language,
                page=page,
                anchor=anchor,
                video_url=video_url,
                learn_more_url=learn_more_url,
            )
        except Exception as e:
            logger.error("UIManager.show_help_dialog failed: %s", e)

    def _ensure_settings_dialog(self):
        """Create the Settings dialog if needed. Does not show it."""
        from plugins.settings.application_service import SettingsApplicationService
        from plugins.settings.dialog import SettingsDialog

        if self.host._settings_dialog is not None:
            return self.host._settings_dialog

        _get_guides_state = get_canvas_registry(
            self._resolve_active_tab()
        ).get_feature_command_by_alias("guides.widget_state")
        guides_state = (
            _get_guides_state(self.host.store.viewport.view_state)
            if _get_guides_state is not None
            else type("_Fallback", (), {"smoothing_enabled": False})()
        )
        behavior_settings = self._query_overlay("overlay.behavior_settings", {}) or {}
        auto_calculate_psnr, auto_calculate_ssim = self._query_metrics_settings()

        if self.host._settings_application_service is None:
            self.host._settings_application_service = SettingsApplicationService(
                self.host.store,
                self.host.main_controller,
                self.host.event_bus,
                self.host,
            )
        self.host._settings_dialog = SettingsDialog(
            current_language=self.host.store.settings.current_language,
            current_theme=self.host.store.settings.theme,
            current_max_length=self.host.store.viewport.render_config.max_name_length,
            min_limit=AppConstants.MIN_NAME_LENGTH_LIMIT,
            max_limit=AppConstants.MAX_NAME_LENGTH_LIMIT,
            debug_mode_enabled=self.host.store.settings.debug_mode_enabled,
            system_notifications_enabled=getattr(
                self.host.store.settings, "system_notifications_enabled", True
            ),
            parent=self.host.parent_widget,
            tr_func=tr,
            current_ui_font_mode=getattr(
                self.host.store.settings, "ui_font_mode", "builtin"
            ),
            current_ui_font_family=getattr(
                self.host.store.settings, "ui_font_family", ""
            ),
            current_ui_scale_factor=getattr(
                self.host.store.settings, "ui_scale_factor", 1.0
            ),
            current_ui_mode=getattr(self.host.store.settings, "ui_mode", "beginner"),
            optimize_magnifier_movement=self.host.store.viewport.view_state.optimize_interactive_movement,
            movement_interpolation_method=self.host.store.viewport.render_config.interactive_movement_interpolation_method,
            optimize_laser_smoothing=guides_state.smoothing_enabled,
            interpolation_method=self.host.store.viewport.render_config.interpolation_method,
            zoom_interpolation_method=self.host.store.viewport.render_config.zoom_interpolation_method,
            magnifier_intersection_highlight_enabled=bool(
                behavior_settings.get("intersection_highlight_enabled", False)
            ),
            magnifier_auto_color_new_instances=bool(
                behavior_settings.get("auto_color_new_instances", False)
            ),
            auto_calculate_psnr=auto_calculate_psnr,
            auto_calculate_ssim=auto_calculate_ssim,
            auto_crop_black_borders=getattr(
                self.host.store.settings, "auto_crop_black_borders", True
            ),
            current_video_fps=clamp_fps(
                getattr(
                    self.host.store.settings, "video_recording_fps", FpsPolicy.DEFAULT
                )
            ),
            rhi_backend=getattr(self.host.store.settings, "rhi_backend", "default"),
            store=self.host.store,
            active_tab=self._resolve_active_tab(),
        )
        self.host._settings_dialog.setAttribute(
            Qt.WidgetAttribute.WA_DeleteOnClose, True
        )
        self.host._settings_dialog.settings_confirmed.connect(
            self._on_settings_confirmed
        )
        self.host._settings_dialog.destroyed.connect(
            lambda: setattr(self.host, "_settings_dialog", None)
        )
        return self.host._settings_dialog

    def _on_settings_confirmed(self) -> None:
        """Apply confirmed settings, then re-pin the QWindow minimum.

        Applies can toggle row visibility (format-dependent rows, search
        state), which changes ``minimumSize()`` while the platform window
        keeps the stale value — re-pin so CSD resizes cannot crush the form.
        """
        service = self.host._settings_application_service
        dialog = self.host._settings_dialog
        if service is None or dialog is None:
            return
        service.apply(dialog.get_settings())
        self._pin_dialog_minimum(dialog)

    @staticmethod
    def _pin_dialog_minimum(dialog) -> None:
        try:
            handle = dialog.windowHandle()
        except RuntimeError:
            return
        if handle is not None:
            handle.setMinimumSize(dialog.minimumSize())

    def show_settings_dialog(self, *, section_id: str | None = None):
        import traceback

        _caller = "".join(traceback.format_stack()[-4:-2])
        dialog = self._ensure_settings_dialog()
        logger.debug(
            "UIManager.show_settings_dialog showing dialog object=%s visible=%s caller=%s",
            hex(id(dialog)) if dialog else "None",
            dialog.isVisible() if dialog else False,
            _caller.strip(),
        )
        if dialog is None:
            return
        sync = getattr(dialog, "sync_from_store", None)
        if callable(sync):
            sync()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        # The dialog itself moves widget focus to the search field
        # synchronously in showEvent (no deferred activation ticks) — see
        # SettingsDialog.showEvent / focus_initial_widget.
        if section_id:
            dialog.select_section(section_id)

    def apply_settings_member(
        self, section_id: str, group_key: str, member_key: str
    ) -> bool:
        """Activate a Settings control and apply without showing the dialog.

        Find Action Enter on a member row: mutate the (possibly hidden) dialog
        chrome, then push ``get_settings()`` through the application service.
        Reveal still uses ``show_settings_dialog``.
        """
        from plugins.settings.member_resolve import activate_member_in_dialog

        dialog = self._ensure_settings_dialog()
        if dialog is None:
            return False
        was_visible = dialog.isVisible()
        if section_id:
            dialog.select_section(section_id)
        # Re-seed the dialog from the store before mutating the target
        # member: this path applies ``get_settings()`` of the *whole*,
        # possibly hidden, long-lived dialog, and stale widgets would
        # silently overwrite good store values with their widget defaults
        # (observed reset family: ui_mode -> beginner, ui_scale -> widget
        # default, rhi_backend -> default; see SettingsDialog.sync_from_store).
        sync = getattr(dialog, "sync_from_store", None)
        if callable(sync):
            sync()
        activated = activate_member_in_dialog(dialog, group_key, member_key)
        service = self.host._settings_application_service
        if activated and service is not None:
            try:
                service.apply(dialog.get_settings())
                self._pin_dialog_minimum(dialog)
            except Exception:
                logger.exception(
                    "apply_settings_member failed section=%s group=%s member=%s",
                    section_id,
                    group_key,
                    member_key,
                )
                activated = False
        if not was_visible and dialog.isVisible():
            dialog.hide()
        return activated

    def show_export_dialog(
        self,
        dialog_state,
        preview_image: object | None,
        suggested_filename: str = "",
        on_set_favorite_dir=None,
        native_size: tuple[int, int] | None = None,
        on_finished=None,
    ):
        """Open the export dialog **non-modally** and return it.

        ``on_finished(code, options)`` — when given — is called exactly once,
        from the dialog's ``finished`` signal: ``code`` is a
        ``QDialog.DialogCode`` and ``options`` is the dialog's export-options
        snapshot (``{}`` unless ``code == Accepted``). Callers must continue
        the export *inside* that callback; the dialog is ``deleteLater()``-ed
        right after it, so the returned object is only good for wiring.

        ``exec()`` used to live here, and that was the bug (owner, 2026-10-28):
        a modal transient is an *attached dialog* for mutter, whose move grab
        targets the transient's parent — dragging the export dialog dragged the
        main window. Non-modal keeps the shell interactive and the drag local
        (same shape as the video editor dialog, ``setModal(False)`` +
        ``show()``).
        """
        from plugins.export.dialog import ExportDialog

        dialog = ExportDialog(
            dialog_state=dialog_state,
            # Parented to the shell so the window stacks, centers, and
            # activates with the main window (unparented export dialogs can
            # open behind the shell on some compositors).
            parent=self.host.parent_widget,
            tr_func=tr,
            preview_image=preview_image,
            suggested_filename=suggested_filename,
            on_set_favorite_dir=on_set_favorite_dir,
            native_size=native_size,
        )
        if on_finished is not None:
            # The handler below is also what drops this reference, so the list
            # only ever holds dialogs that are still waiting to report back.
            self._bind_export_dialog_finished(dialog, on_finished)
            self._open_export_dialogs.append(dialog)

        # ``show()``, not ``open()``: QDialog::open() force-sets
        # ``Qt::WindowModal`` before showing (Qt source, ``QDialog::open``), which
        # is exactly the window modality that makes mutter treat this dialog as
        # an attached dialog and grab the parent on a move. ``show()`` keeps the
        # ``setModal(False)``/``NonModal`` declared in the dialog itself. The
        # ``finished`` signal is emitted by ``accept()``/``reject()`` either way,
        # so the continuation is unaffected.
        dialog.show()
        return dialog

    def _bind_export_dialog_finished(self, dialog, on_finished) -> None:
        """Route one ``finished`` emission to *on_finished* and drop the dialog.

        Held as a closure rather than a slot so the options snapshot is taken
        before ``deleteLater()`` and the callback cannot fire twice (a dialog
        can emit ``finished`` once per accept/reject, and a stray second one
        after cleanup must not re-run the export).
        """
        from PySide6.QtWidgets import QDialog

        fired = [False]

        def _handle(code) -> None:
            if fired[0]:
                return
            fired[0] = True
            try:
                result = QDialog.DialogCode(int(code))
            except (TypeError, ValueError):
                result = int(code)
            options = (
                dialog.get_export_options()
                if result == QDialog.DialogCode.Accepted
                else {}
            )
            try:
                on_finished(result, options)
            except Exception:
                logger.exception("show_export_dialog: export continuation failed")
            finally:
                try:
                    self._open_export_dialogs.remove(dialog)
                except ValueError:
                    pass
                dialog.deleteLater()

        dialog.finished.connect(_handle)
