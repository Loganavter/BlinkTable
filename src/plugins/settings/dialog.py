# Audit-Meta: pattern=qdialog-wiring reason="one Settings QDialog — 6 pages + load/save"
from sli_ui_toolkit.ui.inspector.spec import InspectSpec, SpecField  # noqa: E402
import logging

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from shared_toolkit.ui.themed_dialog import ThemedDialog

from plugins.settings.dialog_context import SettingsDialogContext
from plugins.settings.dialog_shell import (
    apply_styles,
    calculate_and_apply_geometry,
    create_scrollable_page,
    defer_geometry,
    page_scroll_area,
    setup_dialog_shell,
    setup_sidebar_items,
)
from plugins.settings.registry import (
    ensure_tab_settings_contributions,
    get_settings_registry,
)
from plugins.settings.models import SettingsDialogData
from shared.video_policy import FpsPolicy, clamp_fps
from resources.translations import tr as app_tr
from sli_ui_toolkit.theme import ThemeManager
from sli_ui_toolkit.widgets import CustomGroupWidget
from ui.actions.widget_pulse import PULSE_DURATION_MS
from ui.icon_manager import AppIcon
from core.log_names import APP_LOGGER_NAME
from utils.resource_loader import resource_path

logger = logging.getLogger(APP_LOGGER_NAME)

# The search line stays up while the matched controls pulse after activation;
# it is cleared shortly after the pulse finishes — the duration comes from
# the pulse mechanism itself (``PULSE_DURATION_MS``), with a small margin so
# the reset never races the ring's last fade-out tick.
_SEARCH_CLEAR_AFTER_PULSE_MS = PULSE_DURATION_MS + 150

class SettingsDialog(ThemedDialog):
    """Settings chrome. OK applies and keeps the window open; Cancel closes."""

    settings_confirmed = Signal()
    #: Sidebar page became current, by ``SettingsSection.section_id``.
    #: Lets a page re-fetch live data when the user actually enters it (the
    #: build callable runs once per dialog, not once per visit).
    section_shown = Signal(str)

    #: Flyout group for the untagged popups this window owns (the pages' toolkit
    #: ``ComboBox`` dropdowns carry no ``flyout_group`` of their own). Read by
    #: ``ui.flyout_policy._adopt_window_scope_group``; keeps those popups out of
    #: the ``default`` group so they cannot dismiss other windows' flyouts.
    flyout_scope_group = "settings"

    def __init__(
        self,
        current_language,
        current_theme,
        current_max_length,
        min_limit,
        max_limit,
        debug_mode_enabled,
        system_notifications_enabled,
        parent=None,
        tr_func=None,
        current_ui_font_mode: str = "builtin",
        current_ui_font_family: str = "",
        current_ui_scale_factor: float = 1.0,
        current_ui_mode: str = "beginner",
        optimize_magnifier_movement: bool = True,
        movement_interpolation_method: str = "BILINEAR",
        optimize_laser_smoothing: bool = False,
        interpolation_method: str = "LANCZOS",
        zoom_interpolation_method: str = "BILINEAR",
        magnifier_intersection_highlight_enabled: bool = True,
        magnifier_auto_color_new_instances: bool = True,
        auto_calculate_psnr: bool = False,
        auto_calculate_ssim: bool = False,
        auto_crop_black_borders: bool = True,
        current_video_fps: int = FpsPolicy.DEFAULT,
        rhi_backend: str = "default",
        store=None,
        active_tab: str | None = None,
    ):
        super().__init__(parent)
        self.setWindowIcon(QIcon(resource_path("resources/icons/icon.png")))
        self.setObjectName("SettingsDialog")
        # Never assign to ``self.tr`` — QObject.tr() is Qt's own API and
        # shadowing it makes translations look like raw keys.
        self._tr_func = tr_func if callable(tr_func) else app_tr
        self.current_language = current_language
        self.theme_manager = ThemeManager.get_instance()
        self.context = SettingsDialogContext(
            current_language=current_language,
            current_theme=current_theme,
            current_max_length=current_max_length,
            min_limit=min_limit,
            max_limit=max_limit,
            debug_mode_enabled=debug_mode_enabled,
            system_notifications_enabled=system_notifications_enabled,
            tr_func=self._tr_func,
            current_ui_font_mode=current_ui_font_mode,
            current_ui_font_family=current_ui_font_family,
            current_ui_scale_factor=current_ui_scale_factor,
            current_ui_mode=current_ui_mode,
            optimize_magnifier_movement=optimize_magnifier_movement,
            movement_interpolation_method=movement_interpolation_method,
            optimize_laser_smoothing=optimize_laser_smoothing,
            interpolation_method=interpolation_method,
            zoom_interpolation_method=zoom_interpolation_method,
            magnifier_intersection_highlight_enabled=magnifier_intersection_highlight_enabled,
            magnifier_auto_color_new_instances=magnifier_auto_color_new_instances,
            auto_calculate_psnr=auto_calculate_psnr,
            auto_calculate_ssim=auto_calculate_ssim,
            auto_crop_black_borders=auto_crop_black_borders,
            current_video_fps=current_video_fps,
            rhi_backend=rhi_backend,
            store=store,
            keyboard_overrides=dict(
                getattr(getattr(store, "settings", None), "keyboard_overrides", {}) or {}
            )
            if store is not None
            else {},
            tab_extras=get_settings_registry().seed_payloads(store),
        )
        self._custom_group_widget_cls = CustomGroupWidget

        self.setWindowTitle(self._tr("misc.settings", self.current_language))
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setSizeGripEnabled(True)

        from PySide6.QtWidgets import QHBoxLayout

        self.main_layout = QHBoxLayout(self)
        setup_dialog_shell(self)
        from shared_toolkit.ui.decorate_dialog import decorate_dialog, install_dialog_help_menu
        decorate_dialog(self, title=self._tr("misc.settings", self.current_language))
        install_dialog_help_menu(self, page="settings")
        # Shell navigation sections, in visual order: title bar → search →
        # (the sidebar and each page register themselves while the sections
        # below build) → action bar. See plugins/settings/shell_navigation.py.
        from plugins.settings.shell_navigation import (
            register_action_bar_navigation,
            register_search_navigation,
            register_title_bar_navigation,
        )

        register_title_bar_navigation(self)
        register_search_navigation(self)
        self.active_tab = active_tab
        self._search_mode = False
        self._search_prev_row = 0
        ensure_tab_settings_contributions()
        self._active_sections = get_settings_registry().sections_for(active_tab)
        for section in self._active_sections:
            section.build(self, self.context)
        register_action_bar_navigation(self)
        self._setup_sidebar_items()
        self._setup_in_dialog_search()
        self.install_dialog_geometry(self._calculate_and_apply_geometry)
        self.mark_theme_ui_ready()
        self.sidebar.setCurrentRow(0)
        calculate_and_apply_geometry(self)

        from resources.translations import translation_events

        # Stay open after Apply — retranslate chrome when language is confirmed.
        translation_events().language_changed.connect(self.update_language)

    def _tr(self, key: str, language: str | None = None) -> str:
        """Instance translator that never shadows ``QObject.tr``."""
        return self._tr_func(key, language or self.current_language)

    def select_section(self, section_id: str) -> None:
        """Select a sidebar page by ``SettingsSection.section_id``."""
        if not section_id:
            return
        sections = getattr(self, "_active_sections", None) or ()
        for index, section in enumerate(sections):
            if section.section_id == section_id:
                self.sidebar.setCurrentRow(index)
                return

    def sidebar_row_widget_for(self, section_id: str):
        """Return the nav-row button for ``section_id``, or ``None``.

        Uses toolkit ``IconListWidget.row_button`` so Find Action can pulse the
        sidebar without poking row internals.
        """
        if not section_id:
            return None
        sections = getattr(self, "_active_sections", None) or ()
        sidebar = getattr(self, "sidebar", None)
        if sidebar is None:
            return None
        row_button = getattr(sidebar, "row_button", None)
        for index, section in enumerate(sections):
            if section.section_id != section_id:
                continue
            if callable(row_button):
                return row_button(index)
            # Older toolkit: fall back through item proxy.
            item = sidebar.item(index)
            if item is None:
                return None
            spec = getattr(item, "_spec", None)
            return getattr(spec, "button", None) if spec is not None else None
        return None

    def group_widget_for(self, group_key: str):
        """Return the ``CustomGroupWidget`` tagged with ``group_key``, or ``None``.

        Scrolls the group into the page viewport so Find Action pulse is visible.
        """
        if not group_key:
            return None
        from PySide6.QtWidgets import QWidget
        from plugins.settings.member_resolve import scroll_widget_into_view
        from ui.actions.search_index import PROP_GROUP

        for child in self.findChildren(QWidget):
            if child.property(PROP_GROUP) == group_key:
                scroll_widget_into_view(child)
                return child
        return None

    def member_widget_for(self, group_key: str, member_key: str):
        """Return a tagged control inside ``group_key`` (or the dialog), prepared for reveal."""
        from plugins.settings.member_resolve import resolve_member_widget

        return resolve_member_widget(self, group_key, member_key)

    def changeEvent(self, event: QEvent):
        if event.type() == QEvent.Type.ApplicationFontChange:
            defer_geometry(self)
        super().changeEvent(event)

    def _calculate_and_apply_geometry(self):
        calculate_and_apply_geometry(self)

    def _setup_sidebar_items(self):
        setup_sidebar_items(self)

    def _update_sidebar_icons(self):
        self.sidebar.refresh_icons()

    def _create_scrollable_page(self):
        return create_scrollable_page()

    def _page_scroll_area(self, page):
        return page_scroll_area(page)

    def _on_category_changed(self, row):
        if self._search_mode:
            self._on_search_result_activated(row)
            return
        self.pages_stack.setCurrentIndex(row)
        self._update_sidebar_icons()
        sections = getattr(self, "_active_sections", None) or ()
        if 0 <= int(row) < len(sections):
            try:
                self.section_shown.emit(sections[row].section_id)
            except Exception:
                logger.debug("section_shown emit failed for %s", row, exc_info=True)

    # ---- in-dialog search (Find Action catalog reuse) ----

    def _setup_in_dialog_search(self) -> None:
        from PySide6.QtWidgets import QApplication

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self._apply_search)
        self._search_clear_timer = QTimer(self)
        self._search_clear_timer.setSingleShot(True)
        self._search_clear_timer.timeout.connect(self.clear_search)
        self.search_field.textChanged.connect(self._on_search_text_changed)
        self.sidebar.set_no_results_text(
            self._tr("settings.search_no_results", self.current_language)
        )
        self._search_escape = QShortcut(
            QKeySequence(Qt.Key.Key_Escape), self.search_field
        )
        # WidgetShortcut (not the QShortcut default WindowShortcut): a
        # window-scoped shortcut fires on Escape anywhere in this dialog,
        # regardless of what actually has focus — it was silently stealing
        # Escape from any other focused control in the window (e.g. an open
        # ComboBox dropdown, which never got the key to close itself) even
        # while the search field itself had nothing to clear.
        self._search_escape.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._search_escape.activated.connect(self.clear_search)
        # Do not let the field steal Ctrl+A / typing focus semantics from
        # normal usage: typing in the field filters, and only the filter.
        self.search_field.setAttribute(
            Qt.WidgetAttribute.WA_InputMethodEnabled, True
        )
        _ = QApplication

    def _on_search_text_changed(self, _text: str) -> None:
        if self._search_clear_timer.isActive():
            # The user is typing again — the pending post-pulse clear must
            # not wipe the new input.
            self._search_clear_timer.stop()
        if self._search_timer.isActive():
            self._search_timer.stop()
        self._search_timer.start()

    def _apply_search(self) -> None:
        query = self.search_field.text().strip()
        if not query:
            self.clear_search()
            return
        if not self._search_mode:
            self._search_prev_row = self.sidebar.currentRow()
            # Materialize the settings catalog once per search session; the
            # widget filters it in place per keystroke (no rebuilds).
            from plugins.settings.dialog_search import catalog_rows

            self._search_catalog = catalog_rows(self)
            self.sidebar.set_items(self._search_catalog)
        self.sidebar.set_search_text(query)
        self._search_mode = True
        self.sidebar.setCurrentRow(-1)

    def _on_search_result_activated(self, row: int) -> None:
        item = self.sidebar.item(row)
        if item is None:
            return
        result = item.data()
        if result is None:
            return
        from plugins.settings.dialog_search import navigate
        from ui.actions.widget_pulse import pulse_widgets

        # Highlight the matched controls inside the section (the least-deep
        # match level, 1..N).
        targets = navigate(self, result, query=self.search_field.text())
        if targets:
            pulse_widgets(targets)
        for index, section in enumerate(self._active_sections):
            if section.section_id == result.section_id:
                self._search_prev_row = index
                break
        if targets:
            # Clear the search line only after the highlight pulse finishes,
            # so the user sees the matched controls flash before the sidebar
            # returns to the section list. A new keystroke cancels the
            # pending clear.
            self._search_clear_timer.start(_SEARCH_CLEAR_AFTER_PULSE_MS)
        else:
            self.clear_search()

    def clear_search(self) -> None:
        if not self._search_mode:
            return
        if self._search_clear_timer.isActive():
            self._search_clear_timer.stop()
        self._search_mode = False
        if self.search_field.text():
            self.search_field.blockSignals(True)
            self.search_field.clear()
            self.search_field.blockSignals(False)
        self._setup_sidebar_items()
        self.sidebar.setCurrentRow(self._search_prev_row)

    def on_dialog_theme_changed(self) -> None:
        self._update_sidebar_icons()

    def _apply_styles(self):
        apply_styles(self)

    def get_settings(self):
        selected_language = next(
            (
                lang
                for radio, lang in {
                    self.radio_en: "en",
                    self.radio_ru: "ru",
                    self.radio_zh: "zh",
                    self.radio_pt_br: "pt_BR",
                }.items()
                if radio.isChecked()
            ),
            "en",
        )
        ui_font_mode = next(
            (
                mode
                for radio, mode in {
                    self.radio_font_system_default: "system_default",
                    self.radio_font_system_custom: "system_custom",
                }.items()
                if radio.isChecked()
            ),
            "builtin",
        )
        ui_mode = next(
            (
                mode
                for radio, mode in {
                    self.radio_ui_mode_expert: "expert",
                    self.radio_ui_mode_advanced: "advanced",
                    self.radio_ui_mode_beginner: "beginner",
                }.items()
                if radio.isChecked()
            ),
            "beginner",
        )

        def _val(attr, default):
            widget = getattr(self, attr, None)
            if widget is None:
                return default
            # Order matters: sli_ui_toolkit ComboBox inherits from a button-like
            # base and exposes isChecked() (always False) — currentData must win.
            # Same for SpinBox having a button base under the hood.
            if hasattr(widget, "currentData"):
                return widget.currentData()
            if hasattr(widget, "value") and not hasattr(widget, "isChecked"):
                return widget.value()
            if hasattr(widget, "isChecked"):
                return widget.isChecked()
            if hasattr(widget, "value"):
                return widget.value()
            return default

        ctx = self.context
        return SettingsDialogData(
            language=selected_language,
            theme=self.combo_theme.currentData(),
            max_name_length=self.spin_max_length.value(),
            debug_enabled=self.debug_checkbox.isChecked(),
            system_notifications_enabled=self.system_notifications_checkbox.isChecked(),
            ui_font_mode=ui_font_mode,
            ui_font_family=self.combo_font_family.currentData() or "",
            ui_scale_factor=(
                self.slider_ui_scale.value() / 100.0
                if hasattr(self, "slider_ui_scale")
                else ctx.current_ui_scale_factor
            ),
            optimize_magnifier_movement=_val(
                "optimize_movement_checkbox", ctx.optimize_magnifier_movement
            ),
            magnifier_interpolation_method=_val(
                "combo_mag_interp", ctx.movement_interpolation_method
            ) or "BILINEAR",
            optimize_laser_smoothing=_val(
                "laser_smoothing_checkbox", ctx.optimize_laser_smoothing
            ),
            laser_interpolation_method=_val(
                "combo_laser_interp", "BILINEAR"
            ) or "BILINEAR",
            zoom_interpolation_method=_val(
                "combo_zoom_interp", ctx.zoom_interpolation_method
            ) or "BILINEAR",
            magnifier_intersection_highlight_enabled=_val(
                "magnifier_intersection_highlight_checkbox",
                ctx.magnifier_intersection_highlight_enabled,
            ),
            magnifier_auto_color_new_instances=_val(
                "magnifier_auto_color_checkbox",
                ctx.magnifier_auto_color_new_instances,
            ),
            auto_calculate_psnr=(
                self.auto_psnr_checkbox.isChecked()
                if hasattr(self, "auto_psnr_checkbox")
                else self.context.auto_calculate_psnr
            ),
            auto_calculate_ssim=(
                self.auto_ssim_checkbox.isChecked()
                if hasattr(self, "auto_ssim_checkbox")
                else self.context.auto_calculate_ssim
            ),
            auto_crop_black_borders=(
                self.crop_checkbox.isChecked()
                if hasattr(self, "crop_checkbox")
                else self.context.auto_crop_black_borders
            ),
            ui_mode=ui_mode,
            video_recording_fps=clamp_fps(_val("spin_fps", ctx.current_video_fps)),
            rhi_backend=(_val("combo_rhi_backend", ctx.rhi_backend) or "default"),
            keyboard_overrides=dict(
                getattr(self, "_keyboard_overrides", None)
                or getattr(ctx, "keyboard_overrides", None)
                or {}
            ),
            tab_extras=get_settings_registry().read_payloads(self),
        )

    def update_language(self, lang_code: str):
        self.current_language = lang_code
        self.context.current_language = lang_code
        from plugins.settings.translations import apply_translations

        apply_translations(self, lang_code)
        try:
            from plugins.settings.pages import keyboard as keyboard_page

            keyboard_page.refresh_language(self)
        except Exception:
            logger.exception("Settings keyboard page retranslation failed")
        self.search_field.setPlaceholderText(
            self._tr("settings.search_placeholder", self.current_language)
        )
        self.sidebar.set_no_results_text(
            self._tr("settings.search_no_results", self.current_language)
        )
        self._setup_sidebar_items()
        curr = self.sidebar.currentRow()
        self.sidebar.setCurrentRow(-1)
        self.sidebar.setCurrentRow(curr)
        if self._search_mode:
            # Re-materialize the catalog (row texts are cached in the old
            # language) and re-run the live search.
            from plugins.settings.dialog_search import catalog_rows

            self._search_catalog = catalog_rows(self)
            self.sidebar.set_items(self._search_catalog)
            self.sidebar.set_search_text(self.search_field.text())
        defer_geometry(self)

    def confirm_settings(self):
        """Apply current values without closing the dialog."""
        self._reset_button_states()
        self.settings_confirmed.emit()

    def sync_from_store(self) -> None:
        """Re-read live store values into general toggles before showing.

        Must cover every control ``get_settings`` reads back, not just the
        checkboxes: the dialog can survive a hide() (the Find Action member
        path) and be re-shown with widget state that drifted from the store.
        A stale radio/slider/combo then reads back as the *widget default*
        and ``SettingsApplicationService.apply`` silently overwrites the
        user's real values on the next OK (observed reset: ui_mode ->
        beginner, ui_scale_factor -> widget default, rhi_backend -> default
        written over the persisted values without any log).
        """
        store = getattr(self.context, "store", None)
        settings = getattr(store, "settings", None) if store is not None else None
        if settings is None:
            return
        notifications = bool(
            getattr(settings, "system_notifications_enabled", True)
        )
        debug_enabled = bool(getattr(settings, "debug_mode_enabled", False))
        self.context.system_notifications_enabled = notifications
        self.context.debug_mode_enabled = debug_enabled
        checkbox = getattr(self, "system_notifications_checkbox", None)
        if checkbox is not None:
            checkbox.setChecked(notifications)
        debug_cb = getattr(self, "debug_checkbox", None)
        if debug_cb is not None:
            debug_cb.setChecked(debug_enabled)

        ui_mode = getattr(settings, "ui_mode", "beginner")
        radio_for_mode = {
            "expert": getattr(self, "radio_ui_mode_expert", None),
            "advanced": getattr(self, "radio_ui_mode_advanced", None),
            "beginner": getattr(self, "radio_ui_mode_beginner", None),
        }
        radio = radio_for_mode.get(ui_mode)
        if radio is not None:
            radio.setChecked(True)

        scale_slider = getattr(self, "slider_ui_scale", None)
        if scale_slider is not None:
            scale_slider.setValue(
                int(round(float(getattr(settings, "ui_scale_factor", 1.0) or 1.0) * 100))
            )

        rhi_combo = getattr(self, "combo_rhi_backend", None)
        rhi = getattr(settings, "rhi_backend", "default") or "default"
        if rhi_combo is not None and hasattr(rhi_combo, "setCurrentData"):
            try:
                rhi_combo.setCurrentData(rhi)
            except Exception:
                logger.debug("rhi combo sync failed for %s", rhi, exc_info=True)

    def accept(self):
        self.confirm_settings()

    def reject(self):
        self._reset_button_states()
        super().reject()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            # Settings window must not close on Esc — navigation owns Esc
            # for in-dialog search clearing and widget-local dismissals
            # (e.g. ComboBox dropdown). Consume the key so QDialog's
            # default Esc -> reject() never fires; clear search if active.
            if getattr(self, "_search_mode", False):
                self.clear_search()
            event.accept()
            return
        super().keyPressEvent(event)

    def _reset_button_states(self):
        if hasattr(self, "ok_button"):
            self.ok_button.setProperty("state", "normal")
            self.ok_button.update()
        if hasattr(self, "cancel_button"):
            self.cancel_button.setProperty("state", "normal")
            self.cancel_button.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clear_input_focus()
        super().mousePressEvent(event)

    def focus_initial_widget(self) -> None:
        """Give keyboard focus to the search field (widget-level only).

        Synchronous ``setFocus`` on the field consumes no xdg-activation
        token, so this runs inline from the show path — no ``requestActivate``
        and no ``singleShot`` retry ticks. Deferred window activation goes
        stale across event-loop turns on Wayland and the compositor answers
        with a demanding-attention banner instead of focus.
        """
        try:
            from shiboken6 import isValid  # type: ignore[attr-defined]

            if not isValid(self):
                return
            field = getattr(self, "search_field", None)
            if not isValid(field):
                return
        except ImportError:
            field = getattr(self, "search_field", None)
        if field is not None and self.isVisible():
            field.setFocus()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._pin_window_minimum()
        # Synchronous widget focus only — widget ``setFocus`` needs no
        # compositor round-trip, so no deferred ticks here. (Window
        # activation for the show itself is the caller's synchronous
        # show/raise/activateWindow on the user gesture.)
        self.focus_initial_widget()

    def _pin_window_minimum(self) -> None:
        """Re-pin the QWindow minimum to the current widget minimum.

        Post-apply re-pins are issued by the dialog manager (see
        ``DialogManager._pin_dialog_minimum``); this covers the show path.
        """
        try:
            handle = self.windowHandle()
        except RuntimeError:
            return
        if handle is not None:
            handle.setMinimumSize(self.minimumSize())

    def clear_input_focus(self):
        focused_widget = self.focusWidget()
        if focused_widget and hasattr(focused_widget, "clearFocus"):
            focused_widget.clearFocus()

SettingsDialog.inspect_spec = InspectSpec(
    family="SettingsDialog",
    state=(
        SpecField("current_section", lambda w: w.sidebar.currentRow()),
    ),
    docs="docs/dev/widgets/settings_dialog.md",
)

from sli_ui_toolkit.ui.widget_descriptor import InspectSection, WidgetDescriptor
SettingsDialog.widget_descriptor = WidgetDescriptor(
    family=SettingsDialog.inspect_spec.family,
    inspect=InspectSection(
        config=getattr(SettingsDialog.inspect_spec, 'config', ()),
        state=SettingsDialog.inspect_spec.state,
        token_family=getattr(SettingsDialog.inspect_spec, 'token_family', ()),
        regions=getattr(SettingsDialog.inspect_spec, 'regions', False),
        layers=getattr(SettingsDialog.inspect_spec, 'layers', False),
        docs=getattr(SettingsDialog.inspect_spec, 'docs', ''),
        preview_seed=getattr(SettingsDialog.inspect_spec, 'preview_seed', None),
        apply_config_refresh=getattr(SettingsDialog.inspect_spec, 'apply_config_refresh', None),
    ),
)
