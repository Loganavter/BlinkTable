import logging
from PySide6.QtCore import (
    QEvent,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QMoveEvent,
    QShowEvent,
    QColor,
    QIcon,
    QPalette,
    QResizeEvent,
)
from PySide6.QtWidgets import QApplication, QWidget

from core.bootstrap import ApplicationContext
from core.runtime_flags import RuntimeFlags
from core.system_theme import resolve_theme
from shared_toolkit.ui.decorate_dialog import (
    CUSTOM_DECORATION_RESIZE_MARGIN,
    resolve_csd_band,
)
from shared_toolkit.ui.overlay_layer import OverlayLayer
from ui.main_window.actions import MainWindowActions
from ui.main_window.appearance import MainWindowAppearance
from ui.main_window.lifecycle import (
    MainWindowShutdownPipeline,
    MainWindowStartupController,
)
from ui.main_window.runtime import MainWindowRuntime
from ui.main_window.startup import MainWindowStartupRuntime
from ui.theming import install_application_theme, resolve_theme_color
from tabs.registry import get_shared_tab_registry
from core.log_names import APP_LOGGER_NAME
from utils.geometry import GeometryManager
from utils.resource_loader import resource_path

# PIL's decompression-bomb ceiling is a process-wide decode setting, not a
# window property; it must be lifted before the first image decode, but NOT
# at module import — that dragged ``PIL.Image`` (~35 ms, 10 modules) into
# every ``from ui.main_window.window import MainWindow`` (tools, tests, and
# the window-import phase of cold launch) for one constant.
_pil_pixel_limit_installed = False


def _install_pil_pixel_limit() -> None:
    """Lift PIL's decompression-bomb ceiling (large images are legitimate).

    Anchored at ``MainWindow.start()``: the last point before the deferred
    startup phase (timer-driven from ``show``) can restore a document and
    begin decoding, and after the first visible frame is requested — so the
    import lands off the path to first paint. Idempotent; a missing/broken
    PIL must not break startup (decode paths import PIL themselves and would
    fail with a clearer error).
    """
    global _pil_pixel_limit_installed
    if _pil_pixel_limit_installed:
        return
    _pil_pixel_limit_installed = True
    try:
        from PIL import Image as _PILImage
    except Exception:
        logger.warning("PIL unavailable; decompression-bomb guard not lifted")
        return
    _PILImage.MAX_IMAGE_PIXELS = None


logger = logging.getLogger(APP_LOGGER_NAME)


def _request_app_quit(app) -> None:
    """Ask the application to leave its event loop, in this same turn.

    The close path is the only owner of the quit (the app is frameless and
    keeps a tray icon, so nothing else disposes of it): whatever happens
    earlier in ``closeEvent`` — a shutdown step raising, a re-entrant close —
    this is still owed. It therefore runs in the same turn and is not a
    ``QTimer.singleShot(0, app.exit)``: a deferred request lives or dies with
    the lines before it, which is exactly where the request was lost when the
    window closed and the process stayed in ``exec()`` (2026-10-26 magnifier
    storm). ``quit()`` is ``exit(0)``: it stops every event loop running on
    this thread, so nothing later can drop the request on the floor.
    Module-level on purpose — the close path's single quit point, and the
    seam the tests spy on.
    """
    if app is None:
        return
    try:
        app.quit()
    except RuntimeError:
        # Application already torn down (a close during interpreter teardown).
        pass


class MainWindow(QWidget):
    startupVisualReady = Signal()

    def __init__(
        self,
        parent=None,
        debug_mode: bool = False,
        runtime_flags: RuntimeFlags | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("ImageComparisonApp")
        self.runtime_flags = runtime_flags or RuntimeFlags(debug=debug_mode)

        self.CORNER_RADIUS = 10
        self._window_bg_color = QColor("#1e1e1e")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        from sli_ui_toolkit import apply_frameless

        # Outer resize band: the surface carries transparent margin beyond
        # the visible body so the frameless edge can be grabbed from outside
        # (the same band the dialogs get via WindowChrome).
        apply_frameless(self, outer_band=CUSTOM_DECORATION_RESIZE_MARGIN)

        from ui.canvas_infra.rhi.rhi_backend import ensure_window_rhi

        ensure_window_rhi(self)

        self._is_ui_stable = False
        self._application_initialized = False
        self._main_app_bootstrapped = False
        self._main_app_revealed = False
        self._startup_expects_initial_canvas_content = False
        self._startup_visual_ready_emitted = False
        self._deferred_startup_loaded = False
        self._startup_canvas_first_frame_rendered = False
        self._startup_canvas_first_visual_ready = False
        self._offscreen_prewarm_active = False
        self._startup_controller = MainWindowStartupController()
        self._shutdown_pipeline = MainWindowShutdownPipeline()
        self.appearance = MainWindowAppearance(self)
        self.runtime = MainWindowRuntime(self)
        self.startup_runtime = MainWindowStartupRuntime(self)
        self.action_registry = MainWindowActions(self)
        self.setWindowIcon(QIcon(resource_path("resources/icons/icon.png")))

        self.app_context = ApplicationContext(runtime_flags=self.runtime_flags)
        self.app_context.initialize()

        self.store = self.app_context.store
        self.settings_manager = self.app_context.settings_manager
        self.theme_manager = self.app_context.theme_manager
        self.notification_service = self.app_context.notification_service
        self.thread_pool = self.app_context.thread_pool

        app = QApplication.instance()
        if app:
            install_application_theme(self.app_context, app)
            self.theme_manager.theme_changed.connect(self.appearance.on_theme_changed)

        self.ui = None
        self.overlay_layer = OverlayLayer(self)
        self.tray_manager = None
        self.main_controller = None
        self.event_handler = None
        self.presenter = None
        self.ui_resource_manager = None
        self._toast_manager = None
        self.save_task_counter = 0

        self.geometry_manager = GeometryManager(
            self,
            self.settings_manager.settings,
            self.store,
        )
        self._menu_controller = None  # set in startup.build_shell()
        # Title-bar File/Help widths are measured at strip construction. Apply
        # the UI face first so Cyrillic labels are not sized with a fallback
        # font (Help then sits where File should be until FontChange remeasure).
        try:
            from shared_toolkit.ui.managers.font_manager import FontManager

            FontManager.get_instance().apply_from_state(self.store)
        except Exception:
            pass
        self.startup_runtime.build_shell()
        self._wire_csd_scale_resync()

        try:
            theme_bg = QColor(
                resolve_theme_color(self.theme_manager, "label.image.background")
            )
        except Exception:
            theme_bg = QColor("#1e1e1e")
        self._window_bg_color = theme_bg
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, theme_bg)
        pal.setColor(QPalette.ColorRole.Base, theme_bg)
        pal.setColor(self.backgroundRole(), theme_bg)
        self.setPalette(pal)

        try:
            from shared_toolkit.ui.managers.font_manager import FontManager

            self.font_path_absolute = FontManager.get_instance().get_font_path_for_image_text(self.store)
        except Exception:
            self.font_path_absolute = None

        self.setAcceptDrops(True)

    def paintEvent(self, event):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter

        from sli_ui_toolkit.ui.windows.rounded_body import (
            paint_rounded_window_background,
        )

        painter = QPainter(self)
        try:
            squared = self.isMaximized() or self.isFullScreen()
            band = resolve_csd_band(self)
            paint_rounded_window_background(
                painter,
                QRectF(self.rect()).adjusted(band, band, -band, -band),
                color=self._window_bg_color,
                radius=float(self.CORNER_RADIUS),
                squared=squared,
            )
        finally:
            painter.end()

    def _sync_csd_content_band(self) -> None:
        """Zero the root-layout band inset in maximized/fullscreen.

        The root layout insets all content by the outer resize band (so the
        visible body sits inside the transparent grab margin). In
        maximized/fullscreen the band is gone (see ``resolve_csd_band``) —
        keeping the inset would leave a strip of window background around
        the content. Re-insets when the window returns to windowed mode.
        """
        layout = getattr(self, "_root_layout", None)
        if layout is None:
            return
        band = resolve_csd_band(self)
        layout.setContentsMargins(band, band, band, band)
        layout.invalidate()
        layout.activate()
        startup_runtime = getattr(self, "startup_runtime", None)
        if startup_runtime is not None:
            try:
                startup_runtime.sync_cover_geometry()
            except Exception:
                pass

    def _apply_rounded_mask(self) -> None:
        from sli_ui_toolkit.ui.windows.rounded_body import (
            apply_bottom_rounded_mask,
        )

        squared = self.isMaximized() or self.isFullScreen()
        # Shell paints an AA rounded fill — do not setMask it (binary masks
        # destroy the antialiased edge). Clip opaque child hosts instead.
        self.clearMask()
        stack = getattr(self, "_startup_stack", None)
        host = getattr(self, "_app_host", None)
        # Non-current stack pages do not receive QStackedLayout geometry. Keep
        # app_host sized with the stack before masking, otherwise a resize
        # during first-run onboarding freezes a ~100x30 mask on the warm host.
        if (
            stack is not None
            and host is not None
            and stack.width() >= 64
            and stack.height() >= 64
            and (host.width() != stack.width() or host.height() != stack.height())
        ):
            host.resize(stack.size())
        for attr in ("_startup_stack", "_startup_cover", "_app_host"):
            child = getattr(self, attr, None)
            if child is None:
                continue
            apply_bottom_rounded_mask(
                child,
                radius=float(self.CORNER_RADIUS),
                squared=squared,
            )

    @property
    def menu_controller(self):
        """Public accessor for the CSD/title-bar controller.

        Prefer over ``getattr(window, "_menu_controller")`` (private
        attribute hunt flagged by cross-module review C7).
        """
        return getattr(self, "_menu_controller", None)

    @property
    def toast_manager(self):
        if self._toast_manager is not None:
            return self._toast_manager
        try:
            layout_plugin = getattr(self.main_controller, "layout", None)
            toast_manager = (
                getattr(layout_plugin, "toast_manager", None)
                if layout_plugin is not None
                else None
            )
            if toast_manager is not None:
                self._toast_manager = toast_manager
                return self._toast_manager
        except Exception:
            logger.exception("Failed to resolve toast manager from layout plugin")
        return None

    def start(self):
        _install_pil_pixel_limit()
        self._startup_controller.start(self)

    def apply_application_theme(self, theme_name: str):
        # ThemeManager.set_theme (sli-ui-toolkit ≥ 3.1.5) batches top-level
        # updates across QSS apply + theme_changed so the UI does not fill in
        # gradually mid-freeze.
        app = QApplication.instance()
        self.theme_manager.set_theme(resolve_theme(theme_name), app)

    def changeEvent(self, event: QEvent):
        from shared_toolkit.ui.layout_sizing import defer_dialog_geometry
        from ui.layout_geometry import apply_main_window_minimum

        if event.type() == QEvent.Type.ApplicationFontChange:
            defer_dialog_geometry(self, lambda: apply_main_window_minimum(self))
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_csd_content_band()
            self._apply_rounded_mask()
            self.update()
            QTimer.singleShot(0, self.schedule_update)

    def resizeEvent(self, event: QResizeEvent):
        # Mark resize_in_progress before layout so the canvas skips heavy PIL
        # letterbox rebuilds while still doing live shader geometry + redraw.
        self.runtime.notify_resize()
        super().resizeEvent(event)
        self._apply_rounded_mask()
        # CSD: the title bar's layout can stay at its construction-time
        # activation (zones squeezed to tiny widths — clipped File/Help
        # buttons, elided title) until the first real geometry lands; on
        # Wayland that arrives as an asynchronous configure AFTER the window
        # is shown. Force a fresh bar layout + balance on the first real
        # resize so the first visible frame is correct.
        bar = getattr(self, "_custom_title_bar", None)
        if bar is not None:
            sync = getattr(bar, "_sync_balance_spacer", None)
            if callable(sync):
                try:
                    sync()
                except Exception:
                    pass

    def _wire_csd_scale_resync(self) -> None:
        """Force title-bar repaints shortly after a live metrics change.

        The deferred bar relayout heals the geometry on the next tick; on
        some Wayland compositors the healed frame is never repainted
        (translucent-window quirk) and the stale CSD stays on screen until
        a window resize. Force synchronous repaints after the heal so the
        correct state is guaranteed visible. Wired through the unified
        metrics channel (a font change heals the same stale frame), with
        teardown owned by ``connect_ui_metrics`` via ``destroyed``.
        """
        try:
            from sli_ui_toolkit.ui.managers import connect_ui_metrics

            connect_ui_metrics(self, self._schedule_csd_resync)
        except Exception:
            pass

    def _schedule_csd_resync(self) -> None:
        QTimer.singleShot(50, self._force_csd_repaint)
        QTimer.singleShot(250, self._force_csd_repaint)

    def _force_csd_repaint(self) -> None:
        try:
            bar = getattr(self, "_custom_title_bar", None)
            if bar is not None:
                bar.repaint()
            self.repaint()
            from PySide6.QtWidgets import QApplication

            for top in QApplication.topLevelWidgets():  # ALLOWED: system-wide CSD repaint — generic top-level bar repaint, not tab-specific
                dialog_bar = getattr(top, "_csd_title_bar", None)
                if dialog_bar is not None and dialog_bar is not getattr(
                    self, "_custom_title_bar", None
                ):
                    dialog_bar.repaint()
        except Exception:
            pass
    def closeEvent(self, event: QCloseEvent):
        logger.debug("Начало закрытия главного окна...")
        from ui.main_window.project import busy as project_busy

        if project_busy.defer_close_for_project_worker(
            getattr(self, "project_io", None), event
        ):
            # A project open/save worker is still on the pool: accepting the
            # close would tear the app down under it (see the helper).
            return
        if getattr(self, "_shutdown_started", False):
            # A second close request while the pipeline is still running must
            # not re-run every shutdown step. It is reachable: the frameless
            # CSD close button stays clickable, and the magnifier flyout used
            # to re-open over it (the 2026-10-26 storm) — the first request
            # already asked for the quit, so only settle this event.
            event.accept()
            return
        self._shutdown_started = True
        try:
            self._shutdown_pipeline.run(self)
        except Exception:
            # One failing step must not cost the rest of the close: the hide,
            # the accepted event and — above all — ``_request_app_quit``
            # below are still owed. Uncaught, a raise here left the window on
            # screen and the app inside ``exec()`` (the 2026-10-26 lost quit).
            logger.exception("Ошибка в шаге закрытия главного окна")

        logger.debug("Завершение закрытия главного окна")
        try:
            self.hide()
        except Exception:
            pass
        event.accept()
        super().closeEvent(event)
        try:
            self.deleteLater()
        except Exception:
            pass
        _request_app_quit(QApplication.instance())

    def moveEvent(self, event: QMoveEvent):
        super().moveEvent(event)
        self.runtime.handle_move()

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        self._apply_rounded_mask()
        self.runtime.handle_show()

    def mousePressEvent(self, event):
        focused = self.focusWidget()
        tab = get_shared_tab_registry().get_active_tab()
        if tab is not None:
            tab.create_service("clear_transient_text_focus", focused)
        super().mousePressEvent(event)

    def schedule_update(self):
        if self.presenter is not None:
            self.presenter.schedule_canvas_update()

