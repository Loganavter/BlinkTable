from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QSizePolicy

from plugins.settings.layout_geometry import apply_settings_dialog_geometry
from shared_toolkit.ui.layout_sizing import defer_dialog_geometry
from sli_ui_toolkit.managers import scaled_px
from sli_ui_toolkit.ui.managers import connect_ui_metrics
from sli_ui_toolkit.ui.managers.ui_metrics import AXIS_SCALE
from sli_ui_toolkit.widgets import (
    DEFER_CLICK_AWAIT_RIPPLE,
    CustomLineEdit,
    ScrollableDialogPage,
    SidebarDialogShell,
)
from ui.icon_manager import AppIcon
from ui.layout_spacing import control_edge_padding, sidebar_header_host
from ui.theming import polish_themed_dialog
from ui.widgets.form_controls import DialogActionBar

def setup_dialog_shell(dialog):
    dialog.main_layout.setContentsMargins(0, 0, 0, 0)
    dialog.main_layout.setSpacing(0)

    dialog.search_field = CustomLineEdit()
    dialog.search_field.setPlaceholderText(
        dialog._tr("settings.search_placeholder", dialog.current_language)
    )
    dialog.search_field.setClearButtonEnabled(True)
    dialog.search_field.setObjectName("SettingsSearchField")

    dialog.search_header_host = sidebar_header_host(dialog.search_field)
    dialog.shell = SidebarDialogShell(
        sidebar_header=dialog.search_header_host,
        resizable_sidebar=True,
        # The footer rule runs from the section divider to the window edge and
        # takes the divider's own colour (owner request 2026-11-04), so the
        # shell owns the row: the content column goes flush and the pages keep
        # the padding on their own host.
        footer_separator=True,
        content_margins=(
            control_edge_padding(),
            control_edge_padding(),
            control_edge_padding(),
            control_edge_padding(),
        ),
    )
    dialog.main_layout.addWidget(dialog.shell)

    dialog.sidebar = dialog.shell.sidebar
    dialog.sidebar.setObjectName("SettingsSidebar")
    dialog.sidebar.currentRowChanged.connect(dialog._on_category_changed)

    dialog.content_area = dialog.shell.content_area
    dialog.content_area.setObjectName("SettingsContentArea")
    dialog.content_layout = dialog.shell.content_layout
    dialog.pages_stack = dialog.shell.pages_stack
    _install_content_padding_metrics(dialog)

    # Labels describe what the buttons DO here (owner request 2026-11-04): the
    # primary applies and keeps the window open, so it is Save, not OK; the
    # secondary just closes, so it is the plain OK. The footer is separated from
    # the page by a hairline.
    # Labels describe what the buttons DO here (owner request 2026-11-04): the
    # primary applies and keeps the window open, so it is Save, not OK; the
    # secondary just closes, so it is the plain OK.
    dialog.action_bar = DialogActionBar(
        dialog._tr("settings.save_button", dialog.current_language),
        dialog._tr("common.ok", dialog.current_language),
    )
    dialog.ok_button = dialog.action_bar.primary_button
    dialog.cancel_button = dialog.action_bar.secondary_button
    # Theme / language / font apply is GUI-thread-bound — finish the OK
    # ripple before the freeze (see BUTTON_API.md).
    dialog.ok_button.set_defer_click(DEFER_CLICK_AWAIT_RIPPLE)
    dialog.ok_button.clicked.connect(dialog.confirm_settings)
    dialog.cancel_button.clicked.connect(dialog.reject)
    # The footer rule is the SHELL's (footer_separator=True): it is painted in
    # the section divider's colour and spans the content column edge to edge.
    # It does NOT live inside DialogActionBar — that bar pins its own height
    # through ``lock_content_minimum_height`` and a second row inside it changes
    # what it settles to (tests/plugins/test_export_dialog_path_section_height.py
    # caught that on the first attempt).
    dialog.action_bar_separator = dialog.shell.footer_separator
    dialog.shell.footer_layout.addWidget(dialog.action_bar)

def setup_sidebar_items(dialog):
    sections = getattr(dialog, "_active_sections", None)
    if sections:
        dialog._sidebar_items_data = [
            (dialog._tr(s.title_key, dialog.current_language), s.icon)
            for s in sections
        ]
    else:
        dialog._sidebar_items_data = [
            (dialog._tr("settings.appearance", dialog.current_language), AppIcon.SETTINGS),
            (dialog._tr("label.view", dialog.current_language), AppIcon.TEXT_MANIPULATOR),
            (dialog._tr("settings.optimization", dialog.current_language), AppIcon.PLAY),
            (
                dialog._tr("label.details", dialog.current_language),
                AppIcon.HIGHLIGHT_DIFFERENCES,
            ),
        ]
    dialog.sidebar.set_items(dialog._sidebar_items_data)

def create_scrollable_page():
    page = ScrollableDialogPage()
    page.content_widget.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.MinimumExpanding
    )
    # QScrollArea defaults to StrongFocus, so clicking empty space between
    # controls parks real Qt focus on the bare scroll container itself
    # instead of any actual row/control. A NavigationSection registered on
    # the page (see pages/keyboard.py) only recognizes its own row widgets
    # via owns() — it never claims the scroll area, so arrow keys pressed
    # right after such a click go nowhere (NavigationManager finds no owner
    # and yields to native QAbstractScrollArea scrolling instead of routing
    # into the section). NavigationManager's own click-realign path already
    # exists to land focus on the nearest real control after a click on
    # non-focusable padding — but only fires when the scroll container
    # itself can't out-compete it for focus first.
    page.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return page, page.content_layout

def page_scroll_area(page):
    if page is None:
        return None
    return page.findChild(QScrollArea)

def apply_styles(dialog):
    polish_themed_dialog(dialog.theme_manager, dialog)
    dialog._update_sidebar_icons()

def defer_geometry(dialog):
    defer_dialog_geometry(dialog, dialog._calculate_and_apply_geometry)

def calculate_and_apply_geometry(dialog):
    apply_settings_dialog_geometry(dialog)

def _apply_content_padding(dialog):
    """Re-resolve the right column's insets at the live ``UiScale`` factor.

    ``SidebarDialogShell`` resolves ``content_margins`` once, at construction,
    and never again. A live interface-scale change therefore left the column's
    padding at design px while every row inside it scaled: the gap that
    separates the splitter divider from the first control group shrank and the
    divider line ended up against the groups' leftmost stroke (owner report,
    2026-10-28, at dpr 1.25). Keeping the app-side gap proportional is the app
    half of that report; the divider's own geometry (splitter handle width,
    DPR-snapped line) is the toolkit shell's, not this file's.
    """
    pad = control_edge_padding()
    layout = getattr(dialog, "content_layout", None)
    if layout is not None:
        layout.setContentsMargins(pad, pad, pad, pad)

def _install_content_padding_metrics(dialog):
    """Refresh the content padding (and the content-driven minimum) on scale."""

    def _on_scale_changed(*_args):
        _apply_content_padding(dialog)
        # The minimum width brackets the page content at the live factor
        # (layout_geometry.settings_minimum_floor), so it has to be recomputed
        # here too — otherwise a scale change leaves the window at the old
        # (design-px) floor and the right column clips again.
        if getattr(dialog, "_calculate_and_apply_geometry", None) is not None:
            defer_geometry(dialog)

    connect_ui_metrics(dialog, _on_scale_changed, axes=frozenset({AXIS_SCALE}))
