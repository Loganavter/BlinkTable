import logging

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QDialog

from tabs.host_helpers import MessageKind

from core.store import Store
from plugins.export.services.gpu_export import GpuExportService
from core.log_names import APP_LOGGER_NAME
from sli_ui_toolkit.i18n import tr

logger = logging.getLogger(APP_LOGGER_NAME)

class ExportPresenter(QObject):
    """Still-image export glue for image_compare.

    Built as a tab-owned service (``TabRegistry.create_startup_service``,
    see ``tabs/image_compare/service_factory.py``'s ``"export_presenter"``
    id) rather than imported directly by the host composer — still-image
    export is entirely image_compare's own concept (its own context
    builder, save-flow, dialog trigger); nothing here is shared with other
    tabs, unlike a true host capability.
    """

    def __init__(
        self,
        store: Store,
        main_controller,
        ui_manager,
        main_window_app,
        font_path_absolute,
        resource_manager=None,
        parent=None,
    ):
        super().__init__(parent)
        self.store = store
        self.main_controller = main_controller
        self.ui_manager = ui_manager
        self.main_window_app = main_window_app
        self.font_path_absolute = font_path_absolute

        from tabs.image_compare.services.image_export import ExportService
        from tabs.image_compare.services.image_export.context_builder import (
            ExportContextBuilder,
        )
        from tabs.image_compare.services.image_export.save_flow import (
            ExportSaveFlowCoordinator,
        )
        from tabs.image_compare.services.export_state import ExportStateCoordinator

        self.gpu_export_service = GpuExportService(
            parent=main_window_app,
            resource_manager=resource_manager,
        )
        self.export_service = ExportService(
            font_path_absolute=font_path_absolute,
            gpu_export_service=self.gpu_export_service,
        )
        self.state = ExportStateCoordinator(
            store=store,
            main_controller=main_controller,
            main_window_app=main_window_app,
            # Late-bound for the same reason as ``_get_crop_service`` below:
            # quick save needs the canvas zoom/pan, and the tab contract (its
            # canvas geometry provider) does not exist yet at build time.
            get_view_state=self._canvas_view_state,
        )
        # The export context needs the session crop service for its cropped
        # variant ("crop to images" = what the live canvas shows). Late-bound on
        # purpose: the presenters are built before every collaborator exists,
        # and `resolve_session_controller` resolves the tab session through
        # `main_controller.sessions` at call time (single choke point, never
        # raises).
        def _get_crop_service():
            from tabs.image_compare.pipeline.session import resolve_session_controller

            ctrl = resolve_session_controller(self)
            if ctrl is None:
                return None
            try:
                return ctrl._get_crop_service()
            except Exception:
                return None

        self.context_builder = ExportContextBuilder(
            store=store,
            gpu_export_service=self.gpu_export_service,
            state_coordinator=self.state,
            get_crop_service=_get_crop_service,
        )
        self.save_flow = ExportSaveFlowCoordinator(
            store=store,
            main_window_app=main_window_app,
            ui_manager=ui_manager,
            tr_func=self._tr,
            state_coordinator=self.state,
            export_service=self.export_service,
        )

    def _tr(self, text):
        return tr(text, self.store.settings.current_language)

    def shutdown(self) -> None:
        try:
            self.cancel_all_exports()
        except Exception:
            logger.exception("Failed to cancel exports during shutdown")
        try:
            self.gpu_export_service.shutdown()
        except Exception:
            logger.exception("Failed to shutdown GPU export service")

    def sync_recording_controls(
        self, is_recording: bool, is_paused: bool = False, pause_enabled: bool = False
    ):
        ui = getattr(self.main_window_app, "ui", None)
        if ui is None:
            return

        if hasattr(ui, "btn_record"):
            ui.btn_record.blockSignals(True)
            ui.btn_record.setChecked(is_recording)
            ui.btn_record.blockSignals(False)

        if hasattr(ui, "btn_pause"):
            ui.btn_pause.setEnabled(pause_enabled)
            ui.btn_pause.setChecked(is_paused)

    def open_video_editor(self, snapshots, export_controller, video_editor_plugin):
        # Editor-zone gate: canonical predicate (umbrella retained; frozen).
        from tabs.image_compare.debug import ic_video_debug_enabled as _video_dbg_enabled
        _dbg = _video_dbg_enabled()
        if _dbg:
            logger.warning("[video-editor-debug] ExportPresenter.open_video_editor snapshots=%s plugin=%s", bool(snapshots), video_editor_plugin)
        else:
            logger.debug("[video-editor-debug] ExportPresenter.open_video_editor snapshots=%s", bool(snapshots))
        if not snapshots or export_controller is None or video_editor_plugin is None:
            if _dbg:
                logger.warning("[video-editor-debug] ExportPresenter unavailable snapshots=%s controller=%s plugin=%s", bool(snapshots), export_controller, video_editor_plugin)
            self.ui_manager.messages.show_non_modal_message(
                kind=MessageKind.WARNING,
                title=self._tr("image_compare.common.warning"),
                text=self._tr("common.error"),
            )
            return

        try:
            if _dbg:
                logger.warning("[video-editor-debug] ExportPresenter -> video_editor_plugin.open_editor")
            video_editor_plugin.open_editor(
                snapshots, export_controller, self.main_window_app
            )
        except Exception as exc:
            logger.exception("Failed to open video editor: %s", exc)
            self.ui_manager.messages.show_non_modal_message(
                kind=MessageKind.CRITICAL,
                title=self._tr("common.error"),
                text=f"Failed to open video editor: {exc}",
            )

    def _set_export_favorite_dir(self, path: str) -> None:
        self.state.set_export_favorite_dir(path)

    def save_result(self):
        if not self.context_builder.has_images():
            self.save_flow.show_missing_images_warning()
            return

        try:
            save_ctx = self.context_builder.build_save_context(include_preview=True)
            self._open_export_dialog(
                save_ctx.preview_img,
                save_ctx.suggested_filename,
                native_size=(save_ctx.native_width, save_ctx.native_height),
                virtual_canvas_active=bool(save_ctx.virtual_canvas_active),
                crop_native_size=getattr(save_ctx, "crop_native_size", None),
                crop_preview_image=getattr(save_ctx, "crop_preview_img", None),
                on_finished=lambda code, options: self._start_save_after_dialog(
                    save_ctx, code, options
                ),
            )

        except Exception as e:
            logger.error(f"Error during save preparation: {e}", exc_info=True)
            self.ui_manager.messages.show_non_modal_message(
                kind=MessageKind.CRITICAL,
                title=self._tr("common.error"),
                text=f"{self._tr('image_compare.msg.failed_to_save_image')}: {str(e)}",
            )

    def _start_save_after_dialog(self, save_ctx, result_code, export_opts) -> None:
        """Continuation of :meth:`save_result` once the dialog reports back.

        The export dialog is non-modal (it used to be ``exec()``-ed), so the
        export starts here, from its ``finished`` handler, with exactly the
        options the user accepted. ``code != Accepted`` means cancel: nothing
        is written.
        """
        if int(result_code) != int(QDialog.DialogCode.Accepted):
            return

        try:
            if not self.save_flow.validate_export_options(export_opts):
                return

            self.save_flow.start_save_worker(
                save_ctx=save_ctx,
                export_opts=export_opts,
            )
            self.state.persist_export_settings(export_opts)

        except Exception as e:
            logger.error(f"Error during save preparation: {e}", exc_info=True)
            self.ui_manager.messages.show_non_modal_message(
                kind=MessageKind.CRITICAL,
                title=self._tr("common.error"),
                text=f"{self._tr('image_compare.msg.failed_to_save_image')}: {str(e)}",
            )

    def _open_export_dialog(
        self,
        preview_img,
        suggested_filename: str,
        native_size=None,
        *,
        virtual_canvas_active: bool = True,
        crop_native_size=None,
        crop_preview_image=None,
        on_finished=None,
    ):
        dialog_state = self.state.build_export_dialog_state()
        dialog_state.virtual_canvas_active = bool(virtual_canvas_active)
        # Image Compare is the tab that owns a cropped variant of the export.
        dialog_state.crop_to_image_available = True
        dialog_state.crop_native_size = crop_native_size
        dialog_state.crop_preview_image = crop_preview_image
        # "Save view" snapshot — the canvas size here is the export plan's
        # canvas (what the dialog calls native), the zoom/pan come from the
        # live canvas. Snapshot only: the dialog is non-modal, so the save
        # path crops to this rect and never re-reads the live zoom.
        self._publish_view_state(dialog_state, native_size)
        return self.ui_manager.dialogs.show_export_dialog(
            dialog_state,
            preview_img,
            suggested_filename,
            self._set_export_favorite_dir,
            native_size,
            on_finished=on_finished,
        )

    def _resolve_tab_contract(self):
        """This tab's ``TabContract`` instance, late-bound; ``None`` if absent.

        Resolved through the window's tab registry at call time, exactly like
        ``_get_crop_service`` resolves the session: presenters are built before
        the registry has tabs, and no widget class is ever imported here.
        """
        main_window_app = getattr(self, "main_window_app", None)
        ui = getattr(main_window_app, "ui", None)
        registry = getattr(ui, "_tab_registry", None) if ui is not None else None
        if registry is None:
            registry = getattr(main_window_app, "tab_registry", None)
        get_tab = getattr(registry, "get_tab", None)
        if get_tab is None:
            return None
        try:
            return get_tab("image_compare")
        except Exception:
            return None

    def _canvas_view_state(self):
        """Live ``(zoom, pan_x, pan_y)`` of this tab's canvas, or ``None``.

        Goes through the sanctioned ``CanvasGeometryProvider`` accessor (see
        ``ui/canvas_infra/viewport/contract.py``) so the zoom/pan reading stays
        one implementation shared with the rest of the host.
        """
        tab = self._resolve_tab_contract()
        get_provider = getattr(tab, "get_canvas_geometry_provider", None)
        if get_provider is None:
            return None
        try:
            provider = get_provider()
            zoom_pan = provider.get_zoom_pan() if provider is not None else None
        except Exception:
            return None
        if not zoom_pan:
            return None
        return tuple(zoom_pan)

    def _publish_view_state(self, dialog_state, native_size) -> None:
        """Fill the dialog's save-view fields with the visible-view snapshot.

        Fail-open in both directions: no canvas/label or no usable canvas size
        means the option is unavailable, and a dialog state without the fields
        (older dialog) keeps opening — the loop just stops.
        """
        snapshot = None
        try:
            canvas_w = int(native_size[0]) if native_size else 0
            canvas_h = int(native_size[1]) if native_size else 0
        except (TypeError, ValueError):
            canvas_w = canvas_h = 0
        if canvas_w > 0 and canvas_h > 0:
            from tabs.image_compare.services.image_export.view_snapshot import (
                resolve_view_snapshot,
            )

            snapshot = resolve_view_snapshot(
                canvas_w, canvas_h, self._canvas_view_state()
            )
        values = (
            (
                "save_view_available",
                bool(snapshot is not None and snapshot.cropped),
            ),
            # Keep ``view_native_size == (view_rect[2], view_rect[3])``: both
            # are published or neither is.
            ("view_rect", snapshot.rect if snapshot is not None else None),
            (
                "view_native_size",
                snapshot.size if snapshot is not None else None,
            ),
        )
        for name, value in values:
            try:
                setattr(dialog_state, name, value)
            except AttributeError:
                return

    def quick_save(self):
        if not self.context_builder.has_images():
            self.save_flow.show_missing_images_warning()
            return

        try:
            save_ctx = self.context_builder.build_save_context(include_preview=False)
            # The canvas size is the export plan's canvas — the space the
            # save-view rect is expressed in (quick save has no dialog to
            # snapshot it for us).
            export_opts = self.state.build_quick_export_options(
                (save_ctx.native_width, save_ctx.native_height)
            )
            if not save_ctx.virtual_canvas_active:
                export_opts["fill_background"] = False

            self.save_flow.start_save_worker(
                save_ctx=save_ctx,
                export_opts=export_opts,
            )

        except Exception as e:
            logger.error(f"Error during quick save preparation: {e}", exc_info=True)
            self.ui_manager.messages.show_non_modal_message(
                kind=MessageKind.CRITICAL,
                title=self._tr("common.error"),
                text=f"{self._tr('image_compare.msg.failed_to_quick_save_image')}: {str(e)}",
            )

    def cancel_all_exports(self):
        self.save_flow._flow.cancel_all_exports()
