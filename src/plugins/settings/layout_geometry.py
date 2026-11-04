"""Settings dialog content-driven geometry.

Design px live here as plain ints; every resolver below reads the live
``UiScale`` factor **at call time**. These numbers used to be consumed raw,
so at interface scale 1.25/1.5 the declared floor/bounds stayed at design px
while the dialog's rows (which do scale) grew — the shell bracketed content
that no longer fit ("dialogs stranded at design px" class, plan
``plan-live-appearance-fixes-2026-10-19``). Nothing in this module scales at
import: the working sibling is ``plugins/export/layout_geometry.py``.
"""

from __future__ import annotations

from PySide6.QtWidgets import QScrollArea, QStyle

from shared_toolkit.ui.layout_sizing import (
    GeometryApplyPolicy,
    apply_dialog_geometry,
    clamp,
    clamp_to_screen,
    measure_scroll_pages_stack,
)
from sli_ui_toolkit.managers import scaled_px

# Design px (unscaled). Resolved through the resolvers below, never at import.
SETTINGS_MIN_WIDTH = 800
SETTINGS_MAX_WIDTH = 1200
SETTINGS_CONTENT_MARGIN_EXTRA_PX = 40
SETTINGS_SIDEBAR_ROW_HEIGHT_PX = 45
SETTINGS_SIDEBAR_VERTICAL_PADDING_PX = 40
SETTINGS_BOTTOM_CONTROLS_HEIGHT_PX = 80
SETTINGS_HEIGHT_SCREEN_MARGIN_PX = 100
SETTINGS_HEIGHT_EXTRA_PX = 5
# Pages are QScrollArea-backed; do not size the shell to the tallest
# scroll *content* (Keyboard alone is dozens of rows). Cap the viewport
# budget so open height stays comfortable on any monitor.
SETTINGS_MAX_CONTENT_HEIGHT_PX = 520
# Resizability floor, not a content-fit guarantee (pages scroll): low by
# design, but it still has to bracket the scaled chrome at factor > 1.
SETTINGS_FLOOR_WIDTH_PX = 300
SETTINGS_FLOOR_HEIGHT_PX = 200


def settings_min_dialog_width() -> int:
    return scaled_px(SETTINGS_MIN_WIDTH)


def settings_max_dialog_width() -> int:
    return scaled_px(SETTINGS_MAX_WIDTH)


def settings_content_margin_extra() -> int:
    return scaled_px(SETTINGS_CONTENT_MARGIN_EXTRA_PX)


def settings_sidebar_row_height() -> int:
    return scaled_px(SETTINGS_SIDEBAR_ROW_HEIGHT_PX)


def settings_sidebar_vertical_padding() -> int:
    return scaled_px(SETTINGS_SIDEBAR_VERTICAL_PADDING_PX)


def settings_bottom_controls_height() -> int:
    return scaled_px(SETTINGS_BOTTOM_CONTROLS_HEIGHT_PX)


def settings_height_screen_margin() -> int:
    return scaled_px(SETTINGS_HEIGHT_SCREEN_MARGIN_PX)


def settings_height_extra() -> int:
    return scaled_px(SETTINGS_HEIGHT_EXTRA_PX)


def settings_max_content_height() -> int:
    return scaled_px(SETTINGS_MAX_CONTENT_HEIGHT_PX)


def settings_minimum_floor(dialog=None) -> tuple[int, int]:
    """The dialog's resizability floor: content-driven width, design-height.

    Owner report (2026-10-28): "the right column is practically not taken
    into account in the window's minimum size" — the floor used to be the
    bare design constant, so the shell could be shrunk to a 147 px content
    column that no page is authored for (pages scroll, they are not a
    50-60 px strip). The width half now brackets the **widest page's**
    intrinsic content plus the shell chrome (sidebar, splitter handle,
    column margins, vertical scrollbar), so the vertical scrollbar is the
    only scrollbar an ordinary page needs. Height stays a design floor on
    purpose: pages are taller than any sensible window, so a vertical
    scrollbar there is normal — the window floor is a resizability floor,
    not a content-fit guarantee (same dogma as
    ``tests/contracts/test_window_minimum_size_design_space.py``).

    ``dialog=None`` (the policy-only path used by tests/tools) keeps the
    static design floor. A duck-typed stand-in without the real shell also
    keeps it: the chrome widths this floor reasons about only exist there.
    """
    width = scaled_px(SETTINGS_FLOOR_WIDTH_PX)
    if dialog is not None:
        content_width = _content_minimum_window_width(dialog)
        if content_width > 0:
            width = _cap_minimum_width_to_screen(dialog, max(width, content_width))
    return (width, scaled_px(SETTINGS_FLOOR_HEIGHT_PX))


def _content_minimum_window_width(dialog) -> int:
    """Window width that fits the widest page without a horizontal scrollbar.

    Returns 0 when ``dialog`` has no real shell (the chrome whose widths are
    added up lives there), so the caller keeps the design floor.
    """
    shell = getattr(dialog, "shell", None)
    sidebar = getattr(dialog, "sidebar", None)
    pages_stack = getattr(dialog, "pages_stack", None)
    content_layout = getattr(dialog, "content_layout", None)
    if shell is None or sidebar is None or pages_stack is None:
        return 0
    if content_layout is None:
        return 0
    content_width = measure_scroll_pages_stack(pages_stack)[0]
    if content_width <= 0:
        return 0
    # The padding that surrounds the PAGES, not the column: since the footer
    # rule spans the column edge to edge (owner request 2026-11-04) the column
    # itself is flush and the padding lives on the pages' host. Reading the
    # column here would budget 2x padding less than the pages actually need and
    # the minimum window width would open a horizontal scrollbar.
    pages_host = getattr(shell, "pages_host", None)
    if pages_host is not None and pages_host.layout() is not None:
        margins = pages_host.layout().contentsMargins()
    else:
        margins = content_layout.contentsMargins()
    splitter = getattr(shell, "splitter", None)
    handle_width = int(splitter.handleWidth()) if splitter is not None else 0
    # The viewport is the scroll area minus the column margins and the
    # vertical bar, so the vertical bar has to be budgeted here or the
    # minimum itself would trigger a horizontal scrollbar on a tall page.
    return (
        int(sidebar.minimumWidth())
        + handle_width
        + margins.left()
        + margins.right()
        + int(content_width)
        + _vertical_scrollbar_extent(dialog)
    )


def _vertical_scrollbar_extent(dialog) -> int:
    """Width of the pages' vertical scrollbar, in live px (0 when unknown)."""
    extent = 0
    pages_stack = getattr(dialog, "pages_stack", None)
    count = int(pages_stack.count()) if pages_stack is not None else 0
    for index in range(count):
        wrapper = pages_stack.widget(index)
        if wrapper is None:
            continue
        scroll_area = wrapper.findChild(QScrollArea)
        if scroll_area is None:
            continue
        bar = scroll_area.verticalScrollBar()
        if bar is not None:
            extent = max(extent, int(bar.sizeHint().width()))
    if extent > 0:
        return extent
    try:
        return int(dialog.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent))
    except (AttributeError, TypeError, RuntimeError):
        return 0


def _cap_minimum_width_to_screen(dialog, width: int) -> int:
    """Never enforce a minimum wider than the screen the dialog opens on.

    A floor above the monitor's width would put part of the window out of
    reach; on such a screen an horizontal scrollbar is the lesser evil.
    """
    screen = None
    screen_getter = getattr(dialog, "screen", None)
    if callable(screen_getter):
        screen = screen_getter()
    available_width = 0
    if screen is not None:
        available_width = int(screen.availableGeometry().width())
    if available_width <= 0:
        return width
    return min(width, available_width)


def settings_geometry_policy(dialog=None) -> GeometryApplyPolicy:
    """The dialog's geometry policy, with floor/bounds resolved at THIS call."""
    return GeometryApplyPolicy(
        resize_when_hidden=True,
        update_minimum=True,
        minimum_floor=settings_minimum_floor(dialog),
        width_bounds=(settings_min_dialog_width(), settings_max_dialog_width()),
        center_on_parent=True,
        remember_key="settings",
    )


def compute_settings_dialog_size(dialog) -> tuple[int, int]:
    dialog.ensurePolished()
    sidebar_width = dialog.sidebar.width()
    content_margins = dialog.content_layout.contentsMargins()
    total_width_margins = (
        content_margins.left()
        + content_margins.right()
        + settings_content_margin_extra()
    )

    max_content_width, max_content_height = measure_scroll_pages_stack(
        dialog.pages_stack,
        group_widget_cls=getattr(dialog, "_custom_group_widget_cls", None),
    )
    max_content_height = min(max_content_height, settings_max_content_height())

    required_width = sidebar_width + max_content_width + total_width_margins
    final_width = clamp(
        required_width,
        minimum=settings_min_dialog_width(),
        maximum=settings_max_dialog_width(),
    )

    sidebar_req_height = (
        dialog.sidebar.count() * settings_sidebar_row_height()
        + settings_sidebar_vertical_padding()
    )
    required_height = max(
        sidebar_req_height,
        max_content_height + settings_bottom_controls_height(),
    )
    _final_w, final_height = clamp_to_screen(
        final_width,
        required_height + settings_height_extra(),
        margin=settings_height_screen_margin(),
    )
    return final_width, final_height


def apply_settings_dialog_geometry(dialog) -> None:
    width, height = compute_settings_dialog_size(dialog)
    apply_dialog_geometry(
        dialog,
        width,
        height,
        policy=settings_geometry_policy(dialog),
    )
