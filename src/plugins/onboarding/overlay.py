from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent, QMouseEvent, QPainter, QPalette
from PySide6.QtGui import QResizeEvent, QShowEvent, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from resources.translations import tr
from sli_ui_toolkit.i18n import translatable_callback, translatable_text
from sli_ui_toolkit.theme import ThemeManager
from sli_ui_toolkit.managers import scaled_px
from sli_ui_toolkit.widgets import Button
from plugins.onboarding.indicator import DotIndicator
from plugins.onboarding.pages import build_modes, create_slide_for_mode, scale_all_slides
from ui.theming import resolve_theme_color


def write_onboarding_ui_mode(store, settings_manager, mode_key: str) -> bool:
    """Single mode-write channel for first-run onboarding.

    Both ``OnboardingOverlay._finish`` and ``OnboardingPlugin._apply_ui_mode``
    funnel here: the Store is mutated via ``Dispatcher`` (``SetUIModeAction``,
    tracer-visible and history-recorded) and the file is written as a
    debounced full snapshot via ``schedule_persist`` — never an incremental
    per-key write. Returns True when the dispatch path was taken.

    Degenerate hosts (unit-test doubles, teardown races) without a live
    dispatcher fall back to persisting the raw value so the choice is not
    lost; there is no live Store to dispatch to in that case.
    """
    from core.state_management.actions import SetUIModeAction

    get_dispatcher = getattr(store, "get_dispatcher", None)
    dispatcher = get_dispatcher() if callable(get_dispatcher) else None
    if dispatcher is None or settings_manager is None:
        if settings_manager is not None:
            settings_manager._save_setting("ui_mode", mode_key)
        return False
    dispatcher.dispatch(SetUIModeAction(mode_key))
    schedule = getattr(settings_manager, "schedule_persist", None)
    if callable(schedule):
        schedule(store)
    return True


class OnboardingOverlay(QWidget):
    completed = Signal(str)

    def __init__(self, settings_manager, store, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.store = store
        self.setObjectName("onboarding-overlay")
        self.theme_manager = ThemeManager.get_instance()
        self._current_index = 0
        self._scaling = False
        self._last_scale = 1.0
        self._slide_widgets = []

        self._base_window_width = 1920
        self._base_window_height = 1080

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.modes = build_modes(store)

        self._init_ui()
        self._apply_theme()
        # Prefer the host window size so the first paint is already scaled —
        # deferred singleShot left one frame of default fonts/metrics.
        self._sync_geometry_from_host()
        self._scale_all()

    def prepare_for_display(self) -> None:
        """Apply host geometry + scale before the widget becomes visible."""
        self._sync_geometry_from_host()
        self._apply_theme()
        self._scale_all()
        self._update_state(self._current_index)

    def _resolve_target_size(self):
        """Size of the startup stack (content below CSD), not the full window."""
        min_edge = 64
        parent = self.parentWidget()
        if (
            parent is not None
            and parent.width() >= min_edge
            and parent.height() >= min_edge
        ):
            return parent.size()

        win = self.window()
        if win is None or win.width() < min_edge or win.height() < min_edge:
            return None

        # Window includes the title bar; onboarding lives under it in the stack.
        title = getattr(win, "_custom_title_bar", None)
        title_h = 0
        if title is not None and title.isVisible():
            title_h = max(0, title.height())
        if title_h < 8:
            title_h = 36
        from PySide6.QtCore import QSize

        return QSize(win.width(), max(min_edge, win.height() - title_h))

    def _sync_geometry_from_host(self) -> None:
        target = self._resolve_target_size()
        if target is None:
            return
        # Stack already owns geometry once laid out — don't fight it by growing
        # to the full window height (that pushes «Начать работу» below the clip).
        parent = self.parentWidget()
        if (
            isinstance(parent, QStackedWidget)
            and parent.width() >= 64
            and parent.height() >= 64
            and abs(self.width() - parent.width()) <= 1
            and abs(self.height() - parent.height()) <= 1
        ):
            return
        if (
            self.width() >= 64
            and self.height() >= 64
            and target.width() * target.height() < self.width() * self.height() * 0.5
        ):
            return
        self.resize(target)

    def showEvent(self, event: QShowEvent):
        super().showEvent(event)
        # In-stack raise only: never escalate to host-window activation
        # (the host shell owns window activation).
        self.raise_()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.prepare_for_display()

    def resizeEvent(self, event: QResizeEvent):
        super().resizeEvent(event)
        self._scale_all()

    def changeEvent(self, event: QEvent):
        super().changeEvent(event)
        # Toolkit/theme polish resets Button setFont() to app 12pt; re-apply
        # after the style pass so paint stays scaled.
        if event.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.ApplicationFontChange,
            QEvent.Type.StyleChange,
        ):
            self._scale_all()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Window))
        super().paintEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        event.accept()

    def wheelEvent(self, event: QWheelEvent):
        event.accept()

    def keyPressEvent(self, event: QKeyEvent):
        # The overlay owns exactly two things here: Escape (finish with the
        # current mode) and letting the NavigationManager's section graph
        # drive Tab/arrows over the chips and Start. Every other key must
        # reach the focused control — the blanket accept() this used to do
        # swallowed characters and dead-ended keyboard navigation.
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self._finish()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent):
        super().keyReleaseEvent(event)

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self.top_container = QWidget()
        self.top_layout = QVBoxLayout(self.top_container)

        self.stack = QStackedWidget()
        self._init_slides()

        slide_wrapper = QWidget()
        slide_layout = QVBoxLayout(slide_wrapper)
        slide_layout.setContentsMargins(0, 0, 0, 0)
        slide_layout.addWidget(self.stack)
        slide_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.top_layout.addWidget(slide_wrapper, 1)

        self.hint_label = QLabel()
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Two keys on one label: a composite callback, bound (not resolved
        # once) so a live language switch reaches the hint.
        translatable_callback(
            self.hint_label,
            lambda lang: self.hint_label.setText(
                f"{tr('onboarding.mode_hint', lang)}\n"
                f"{tr('onboarding.magnifier_controls_hint', lang)}"
            ),
        )
        self.top_layout.addWidget(self.hint_label)

        self.mode_buttons_layout = QHBoxLayout()
        self.mode_buttons_layout.addStretch()

        self.mode_buttons = []
        for i, mode in enumerate(self.modes):
            btn = Button(text="", variant="surface", corner_radius=8)
            # Chip label is a bound key (mode["name"] stays the first-paint
            # string for host code that reads it).
            translatable_text(btn, mode["name_key"])
            btn.setObjectName(f"onboarding-mode-{mode.get('key', i)}")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            # Avoid QWidget's default 640x480 sizeHint before the first _scale_all.
            btn.setFixedSize(scaled_px(150), scaled_px(42))
            btn.clicked.connect(
                lambda checked=False, idx=i: self._on_mode_btn_clicked(idx)
            )
            self.mode_buttons.append(btn)
            self.mode_buttons_layout.addWidget(btn)

        self.mode_buttons_layout.addStretch()
        self.top_layout.addLayout(self.mode_buttons_layout)

        self.dots = DotIndicator(count=len(self.modes))
        dots_layout = QHBoxLayout()
        dots_layout.addStretch()
        dots_layout.addWidget(self.dots)
        dots_layout.addStretch()
        self.top_layout.addLayout(dots_layout)

        self.bottom_container = QWidget()
        self.bottom_layout = QVBoxLayout(self.bottom_container)
        self.bottom_layout.setContentsMargins(0, 0, 0, 0)
        # Pre-pass default; ``_scale_all`` re-derives the live value from the
        # same design px (window-fit x UiScale) on every resize/metrics change.
        self.bottom_layout.setSpacing(scaled_px(8))
        self.bottom_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.welcome_lbl = QLabel()
        self.welcome_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        translatable_text(self.welcome_lbl, "onboarding.welcome")
        self.bottom_layout.addWidget(self.welcome_lbl)

        self.btn_start = Button(text="", variant="surface")
        translatable_text(self.btn_start, "onboarding.start_button")
        self.btn_start.setObjectName("onboarding-start")
        self.btn_start.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start.setFixedSize(scaled_px(200), scaled_px(48))
        self.btn_start.clicked.connect(self._finish)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_start)
        btn_layout.addStretch()
        self.bottom_layout.addLayout(btn_layout)

        # Bottom chrome keeps its sizeHint; top takes the remaining space.
        self.main_layout.addWidget(self.top_container, 1)
        self.main_layout.addWidget(self.bottom_container, 0)

        self._update_state(0)

        # Chips (one row, left→right) above Start: Down/Up walk them, so the
        # overlay no longer needs to swallow keys. Registered after the chips
        # and button exist so the section's rows are the real controls.
        from shared_toolkit.ui.decorate_dialog import register_dialog_auto_navigation

        register_dialog_auto_navigation(
            self,
            [list(self.mode_buttons), [self.btn_start]],
            tag="onboarding",
        )

    def _init_slides(self):
        self._slide_widgets = []
        for mode in self.modes:
            page = create_slide_for_mode(self, mode)
            self._slide_widgets.append(page)
            self.stack.addWidget(page)

    def _on_mode_btn_clicked(self, index):
        self._update_state(index)

    def _update_state(self, index):
        self._current_index = index
        self.stack.setCurrentIndex(index)
        self.dots.set_current(index)

        accent_color = resolve_theme_color(self.theme_manager, "accent")
        text_color = resolve_theme_color(self.theme_manager, "dialog.text")
        highlighted_text = resolve_theme_color(self.theme_manager, "HighlightedText")

        for i, btn in enumerate(self.mode_buttons):
            if i == index:
                # Selected chip: exact accent, no hover wash (would bleach to white).
                btn.set_override_bg_color(accent_color)
                btn.set_bg_locked(True)
                btn.setProperty("textColor", highlighted_text)
                btn.update()
            else:
                btn.set_override_bg_color(None)
                btn.set_bg_locked(False)
                btn.setProperty("textColor", text_color)
                btn.update()

    def _get_scale_factor(self):
        width = max(1, self.width())
        height = max(1, self.height())
        if width < 32 or height < 32:
            # Not laid out yet — use host window if available.
            host = self.window()
            if host is not None and host.width() >= 32 and host.height() >= 32:
                width, height = host.width(), host.height()
            else:
                width, height = self._base_window_width, self._base_window_height
        width_scale = width / self._base_window_width
        height_scale = height / self._base_window_height
        scale = min(width_scale, height_scale)
        if scale < 0.7:
            scale = 0.7 + (scale - 0.5) * 0.2
        elif scale > 1.0:
            scale = 1.0 + (scale - 1.0) * 0.5
        scale = max(0.7, min(1.5, scale))
        # Compose with the global interface scale so onboarding chrome follows
        # the user's UiScale setting on top of the window-relative fit.
        try:
            from sli_ui_toolkit.managers import UiScale

            scale *= UiScale.get_instance().factor()
        except Exception:
            pass
        return max(0.5, min(2.5, scale))

    @staticmethod
    def _ui_base_font() -> QFont:
        try:
            from sli_ui_toolkit.managers import UiFont

            # base_font() — UiFont has no .font(); the old call always fell
            # through to QApplication.font() via a swallowed AttributeError.
            return QFont(UiFont.get_instance().base_font())
        except Exception:
            app = QApplication.instance()
            return QFont(app.font()) if isinstance(app, QApplication) else QFont()

    def _scale_all(self):
        if self._scaling:
            return
        self._scaling = True
        try:
            self._scale_all_impl()
        finally:
            self._scaling = False

    def _scale_all_impl(self):
        scale = self._get_scale_factor()

        if scale > 1.0:
            margin_horizontal = max(2, int(10 / scale))
            margin_top = max(5, int(15 / scale))
            bottom_margin = max(2, int(5 / scale))
        else:
            margin_horizontal = max(5, int(10 * scale))
            margin_top = max(10, int(15 * scale))
            bottom_margin = max(2, int(5 * scale))

        spacing = max(6, int(10 * scale)) if scale <= 1.0 else max(4, int(10 / scale))

        self.top_layout.setContentsMargins(
            margin_horizontal, margin_top, margin_horizontal, 0
        )
        self.top_layout.setSpacing(spacing)

        self.bottom_layout.setSpacing(max(6, int(8 * scale)))
        self.bottom_layout.setContentsMargins(0, 0, 0, bottom_margin)
        self.main_layout.setSpacing(0)

        text_col = resolve_theme_color(self.theme_manager, "WindowText").name()

        hint_font_size = max(12, int(15 * scale))

        hint_font = self._ui_base_font()
        hint_font.setPixelSize(hint_font_size)
        hint_font.setWeight(QFont.Weight.DemiBold)
        self.hint_label.setFont(hint_font)
        hint_palette = self.hint_label.palette()
        hint_palette.setColor(self.hint_label.foregroundRole(), QColor(text_col))
        self.hint_label.setPalette(hint_palette)

        btn_width = max(150, int(200 * scale))
        btn_height = max(42, int(52 * scale))
        btn_spacing = int(20 * scale)

        self.mode_buttons_layout.setSpacing(btn_spacing)

        mode_btn_font_size = max(13, int(16 * scale))
        mode_btn_font = self._ui_base_font()
        mode_btn_font.setPixelSize(mode_btn_font_size)
        mode_btn_font.setBold(True)

        for btn in self.mode_buttons:
            btn.setFixedSize(btn_width, btn_height)
            btn.setFont(mode_btn_font)
            btn.update()

        welcome_font_size = max(22, int(28 * scale))
        w_font = self._ui_base_font()
        w_font.setPixelSize(welcome_font_size)
        w_font.setBold(True)
        self.welcome_lbl.setFont(w_font)

        start_btn_width = max(200, int(260 * scale))
        start_btn_height = max(48, int(56 * scale))

        start_font_size = max(14, int(17 * scale))
        custom_font = self._ui_base_font()
        custom_font.setPixelSize(start_font_size)
        custom_font.setBold(True)

        self.btn_start.setFont(custom_font)
        self.btn_start.setFixedSize(start_btn_width, start_btn_height)
        self.btn_start.update()

        dots_height = max(20, int(26 * scale))
        self.dots.setFixedHeight(dots_height)

        self._last_scale = scale
        scale_all_slides(self, scale)

    def _apply_theme(self):
        bg_window = resolve_theme_color(self.theme_manager, "surface.background").name()
        text_col = resolve_theme_color(self.theme_manager, "WindowText").name()
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(bg_window))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(text_col))
        self.setPalette(palette)

    def _finish(self):
        key = self.modes[self._current_index]["key"]
        write_onboarding_ui_mode(self.store, self.settings_manager, key)
        if self.settings_manager:
            self.settings_manager.set_first_run_completed()
        self.completed.emit(key)