from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QTimer
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

_GPU_WARM_UP_DELAY_MS = 3000

from core.events import WorkspaceSessionActivatedEvent
from plugins.export.events import (
    ExportOpenVideoEditorEvent,
    ExportPasteImageFromClipboardEvent,
    ExportTogglePauseRecordingEvent,
    ExportToggleRecordingEvent,
)
from core.plugin_system import Plugin, plugin
from core.plugin_system.interfaces import (
    IControllablePlugin,
    IServicePlugin,
)
from plugins.export.controller import ExportController

@plugin(name="export", version="1.0", startup_tier="deferred", startup_order=10)
class ExportPlugin(Plugin, IControllablePlugin, IServicePlugin):
    def __init__(self):
        super().__init__()
        self.controller: ExportController | None = None
        self.recorder: Any | None = None
        self.video_exporter: Any | None = None
        self.clipboard_service: Any | None = None
        self.video_editor_plugin: Any | None = None
        self.thread_pool: Any | None = None
        self.event_bus: Any | None = None
        self.store: Any | None = None
        self.plugin_coordinator: Any | None = None

    def initialize(self, context: Any) -> None:
        super().initialize(context)
        self.store = getattr(context, "store", None)
        self.thread_pool = getattr(context, "thread_pool", None)
        self.event_bus = getattr(context, "event_bus", None)
        self.plugin_coordinator = getattr(context, "plugin_coordinator", None)
        self.video_editor_plugin = (
            self.plugin_coordinator.get_plugin("video_editor")
            if self.plugin_coordinator
            else None
        )
        # video_editor is deferred — at bootstrap time it is not yet discovered,
        # so the above is None. Resolve lazily on first use (see _ensure_video_editor_plugin).
        logger.debug("[video-editor-debug] ExportPlugin.initialize video_editor_plugin=%s deferred_loaded=%s", self.video_editor_plugin, getattr(context, "_deferred_plugins_loaded", None))

    def _ensure_video_editor_plugin(self) -> Any | None:
        if self.video_editor_plugin is not None:
            return self.video_editor_plugin
        if self.plugin_coordinator is None:
            return None
        # Ensure deferred tier is loaded before lookup — toolbar button may be
        # clicked while bootstrap session is active and deferred plugins not yet
        # initialized (event would have 0 subscribers and appear to do nothing).
        try:
            ctx = getattr(self.plugin_coordinator, "_context", None) or getattr(self.plugin_coordinator, "context", None)
            if ctx is None:
                # Fallback: try to get ApplicationContext via store context
                from core.bootstrap import ApplicationContext  # type: ignore
                # No direct handle — try plugin_coordinator's internal context
                pass
        except Exception:
            pass
        try:
            # If context has ensure_deferred, call it (may be no-op if already loaded)
            for attr in ("ensure_deferred_plugins_loaded", "load_deferred_plugins"):
                maybe_ctx = getattr(self.plugin_coordinator, "context", None) or getattr(self.store, "_context", None)
                if maybe_ctx is not None and hasattr(maybe_ctx, attr):
                    getattr(maybe_ctx, attr)()
                    break
        except Exception as exc:
            logger.debug("[video-editor-debug] ensure_deferred failed: %s", exc)
        try:
            self.video_editor_plugin = self.plugin_coordinator.get_plugin("video_editor")
        except Exception:
            pass
        from shared.debug_flags import env_flag as _env_flag
        # Platform-local editor-zone gate (no tab import — see
        # controller.open_video_editor note; umbrella frozen).
        if _env_flag("IMGSLI_VIDEO_EDITOR_DEBUG") or _env_flag("IMGSLI_IC_VIDEO_DEBUG"):
            logger.warning("[video-editor-debug] _ensure_video_editor_plugin -> %s", self.video_editor_plugin)
        return self.video_editor_plugin

    def configure_controller(
        self,
        main_controller: Any | None = None,
        presenter: Any | None = None,
    ) -> None:
        if self.controller:
            return
        self.recorder, self.video_exporter = self._create_recording_services(
            main_controller=main_controller,
            presenter=presenter,
        )
        self.clipboard_service = self._create_clipboard_service(main_controller)
        self.controller = ExportController(
            store=self.store,
            thread_pool=self.thread_pool,
            recorder=self.recorder,
            video_exporter=self.video_exporter,
            clipboard_service=self.clipboard_service,
            presenter=presenter,
            video_editor_plugin=self.video_editor_plugin,
            main_controller=main_controller,
            event_bus=self.event_bus,
        )
        if presenter and self.controller:
            self.controller.presenter = presenter

        warm_up = getattr(self.video_exporter, "warm_up_gpu_widgets", None)
        if callable(warm_up):
            # One warm-up per process: the offscreen widget's cold cost is a
            # ~4 s D3D shader/pipeline compile (measured 2026-10-24:
            # magnifier_pass.initialize is where the GUI thread blocks).
            # Re-running it on every session
            # activation turned that into a recurring freeze the user reads as
            # "it lags when I press record" (the toggle itself is 0.5 ms).
            # Off the startup critical path, well before the video editor
            # dialog is likely to be opened, so the GPU offscreen widget's
            # cold-start cost (window/QRhi surface creation) isn't paid
            # inline with the user's first preview request. Not
            # every tab can actually provide a canvas widget class (see
            # tab_canvas_services.get_canvas_widget_class), so this fixed
            # delay only succeeds if a tab that can already happens to be
            # the active session by the time it fires — the
            # WorkspaceSessionActivatedEvent subscription below catches the
            # case where the active session changes into (or starts as) a
            # canvas-providing tab at some other point.
            def _warm_once_after_startup() -> None:
                try:
                    self._gpu_warm_done = bool(warm_up())
                except Exception:
                    self._gpu_warm_done = False

            QTimer.singleShot(_GPU_WARM_UP_DELAY_MS, _warm_once_after_startup)

        if self.event_bus:
            self.event_bus.subscribe(
                WorkspaceSessionActivatedEvent, self._on_session_activated
            )

        if self.event_bus and self.controller:

            self.event_bus.subscribe(
                ExportToggleRecordingEvent, self.controller.on_toggle_recording
            )
            self.event_bus.subscribe(
                ExportTogglePauseRecordingEvent,
                self.controller.on_toggle_pause_recording,
            )
            self.event_bus.subscribe(
                ExportOpenVideoEditorEvent, self.controller.on_open_video_editor
            )
            self.event_bus.subscribe(
                ExportPasteImageFromClipboardEvent,
                self.controller.on_paste_image_from_clipboard,
            )

    def _on_session_activated(self, event: WorkspaceSessionActivatedEvent) -> None:
        # Deliberately not filtered by session_type — this file is platform
        # code and must stay tab-agnostic (see
        # tests/contracts/test_platform_isolation.py). warm_up() best-effort
        # no-ops when the newly active tab doesn't provide a canvas widget
        # class, and _ensure_widget() is idempotent past its first success.
        #
        # BUT the cold path is a ~4 s D3D pipeline compile per canvas, and a
        # canvas-providing tab that is not the one GPU resources were warmed
        # from still pays it. Retry only until the first success: a repeated
        # warm-up on every activation re-created the offscreen widget and the
        # freeze with it (measured 2026-10-24, GUI-thread stall watchdog:
        # 4253 ms + 4027 ms right around a record toggle whose own cost was
        # 0.5 ms).
        if getattr(self, "_gpu_warm_done", False):
            return
        warm_up = getattr(self.video_exporter, "warm_up_gpu_widgets", None)
        if callable(warm_up):
            self._gpu_warm_done = bool(warm_up())

    def _emit(self, event: str, payload: Any) -> None:
        if self.event_bus:
            self.event_bus.emit(event, payload)
    def get_controller(self) -> ExportController | None:
        return self.controller

    def handle_command(self, command: str, *args: Any, **kwargs: Any) -> Any:
        if self.controller and hasattr(self.controller, command):
            return getattr(self.controller, command)(*args, **kwargs)
        raise AttributeError(f"Export plugin has no command '{command}'")

    def bind_window_shell(self, window_shell: Any) -> None:
        if self.controller:
            if hasattr(window_shell, "get_feature"):
                self.controller.presenter = window_shell.get_feature("export")
            else:
                self.controller.presenter = window_shell

    def get_service(self) -> Any:
        return self.recorder

    def _create_recording_services(
        self,
        *,
        main_controller: Any | None,
        presenter: Any | None,
        extra_adapters: tuple[Any, ...] = (),
    ) -> tuple[Any, Any]:
        plugin = self.video_editor_plugin
        if plugin is None or not hasattr(plugin, "create_recording_services"):
            raise RuntimeError(
                "video_editor plugin must provide recording services for export controls"
            )
        return plugin.create_recording_services(
            store=self.store,
            main_controller=main_controller,
            gpu_export_service=getattr(presenter, "gpu_export_service", None),
            extra_adapters=extra_adapters,
        )

    def _create_clipboard_service(self, main_controller: Any | None) -> Any | None:
        """Bootstrap-default paste service for early shell wiring.

        Live Ctrl+V resolves through ``ExportController.paste_image_from_clipboard``
        → active-tab ``create_service("clipboard_paste_service")``. This startup
        instance remains as a fallback when no active tab owns paste (e.g.
        session_picker), and it is genuinely optional: a freshly discovered tab
        has no controller yet (``build_clipboard_service`` returns ``None`` until
        the tab's page exists), so the probe answers ``None`` on a normal cold
        start. Missing fallback therefore warns and leaves paste to the active
        tab instead of raising.

        Why not raise (2026-10-25): the raise aborted
        ``MainController.attach_deferred_plugins`` mid-loop — ``help`` and
        ``image_properties`` were never attached — and, once the deferred block
        moved past the first frame (app #659), the same exception reached
        ``sys.excepthook`` as the user-visible "Report a problem" dialog on
        every cold start. ``ExportController.paste_image_from_clipboard``
        already returns ``False`` when neither the active tab nor this fallback
        supplies the service.
        """
        from tabs.registry import TabRegistry

        registry = TabRegistry()
        registry.discover()
        service = registry.create_startup_service(
            "clipboard_paste_service",
            self.store,
            main_controller,
        )
        if service is None:
            logger.warning(
                "No tab offered clipboard_paste_service at startup; paste stays "
                "with the active tab (unavailable until a tab owns paste)"
            )
        return service


    def shutdown(self) -> None:
        super().shutdown()

        if self.video_exporter:
            try:
                self.video_exporter.cleanup()
            except Exception as e:
                import logging

                logging.getLogger(APP_LOGGER_NAME).error(
                    f"Ошибка при завершении VideoExporterService: {e}"
                )
