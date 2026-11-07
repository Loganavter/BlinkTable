"""Widget-tree → element inventory (and retry-until-rendered resolution).

Owns the ``Element`` row, the traversal that flattens every live top-level
window (including ``Qt.Popup`` / ``Qt.Tool`` menus) into interactive elements,
and :func:`resolve_element` — the retry loop that lets an agent call
``ui_click`` before the element is realized. See the package docstring for the
id/rect contract and the role mapping (kept in :mod:`.roles`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication, QMenu, QTabBar, QWidget

from .query import scalar_properties
from .roles import (
    _has_area,
    _is_menu_like,
    _label_of,
    _role_of,
    _safe_visible,
)


def rect_list(rect: QRect) -> list[int]:
    """``QRect`` → ``[x, y, w, h]`` (JSON-safe)."""
    return [rect.x(), rect.y(), rect.width(), rect.height()]


def screen_rect_of(widget: QWidget, rect: QRect) -> QRect:
    """Map a widget-local *rect* to global coordinates (best-effort)."""
    try:
        top_left = widget.mapToGlobal(rect.topLeft())
    except Exception:
        top_left = rect.topLeft()
    return QRect(top_left, rect.size())


@dataclass(slots=True)
class Element:
    """One interactive element in the live UI.

    ``rect`` is widget-local (see the package docstring); ``widget`` is the
    object that receives an injected event and must stay alive while the
    caller uses the element.
    """

    id: str
    widget: QWidget
    role: str
    label: str
    rect: QRect
    window: str
    surface: str
    action_id: str | None
    owner_tab: str | None
    enabled: bool
    visible: bool
    #: Serialisable content identity read from the widget's own dynamic
    #: properties (see :func:`query.scalar_properties`), e.g. ``gallery_path``
    #: for a gallery card. Empty when the widget carries none.
    properties: dict[str, Any] = field(default_factory=dict)
    #: Global screen point this element was addressed at, when it came from a
    #: ``point:<x>,<y>`` reference (``resolve.resolve_element``) rather than the
    #: inventory. Pointer delivery uses it as the event position, so a wheel or
    #: click lands where the caller aimed instead of at the widget's centre.
    #: ``None`` for every inventory row — those keep the centre convention.
    point: Any = None

    @property
    def screen_rect(self) -> QRect:
        return screen_rect_of(self.widget, self.rect)

    @property
    def target(self) -> str:
        return type(self.widget).__name__

    def payload(self) -> dict[str, Any]:
        """The frozen ``ui_elements`` row shape."""
        return {
            "id": self.id,
            "role": self.role,
            "label": self.label,
            "rect": rect_list(self.rect),
            "screen_rect": rect_list(self.screen_rect),
            "window": self.window,
            "owner_tab": self.owner_tab,
            "action_id": self.action_id,
            "enabled": self.enabled,
            "visible": self.visible,
            "properties": dict(self.properties),
        }


# -- traversal ----------------------------------------------------------------


def _action_descriptors(registry: Any) -> dict[int, list[Any]]:
    """Live widget id → the ``ActionDescriptor``s whose target resolves to it.

    Mirrors ``devtools.ui_layout_dump._widget_action_map`` but keeps the
    descriptors (not just ids) so the inventory can carry ``owner_tab``.
    """
    mapping: dict[int, list[Any]] = {}
    try:
        actions = list(registry.all_actions())
    except Exception:
        return mapping
    for action in actions:
        target = getattr(action, "target", None)
        if target is None:
            continue
        candidates = [getattr(target, "widget", None)]
        resolve = getattr(target, "resolve_widget", None)
        if callable(resolve):
            try:
                candidates.append(resolve())
            except Exception:
                pass
        for widget in candidates:
            if widget is not None:
                mapping.setdefault(id(widget), []).append(action)
    return mapping


def _best_action(descriptors: list[Any] | None) -> Any | None:
    if not descriptors:
        return None
    return sorted(descriptors, key=lambda a: getattr(a, "action_id", ""))[0]


def _is_popup_window(window: QWidget) -> bool:
    try:
        window_type = window.windowType()
    except Exception:
        return False
    popup_flags = Qt.WindowType.Popup | Qt.WindowType.Tool
    return bool(window_type & popup_flags)


def _window_keys(windows: list[QWidget]) -> list[str]:
    """Deterministic per-window identity; duplicates get a ``#n`` suffix."""
    bases: list[str] = []
    for window in windows:
        try:
            base = window.objectName()
        except Exception:
            base = ""
        bases.append(str(base or type(window).__name__ or "window"))
    counts: dict[str, int] = {}
    for base in bases:
        counts[base] = counts.get(base, 0) + 1
    seen: dict[str, int] = {}
    keys: list[str] = []
    for base in bases:
        if counts[base] > 1:
            index = seen.get(base, 0)
            seen[base] = index + 1
            keys.append(f"{base}#{index}")
        else:
            keys.append(base)
    return keys


def _top_level_widgets() -> list[QWidget]:
    # Generic, tab-agnostic top-level walk (same family as devtools/ui_layout_dump).
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return []
    try:
        return [w for w in app.topLevelWidgets() if isinstance(w, QWidget)]  # ALLOWED: generic top-level walk, mirrors devtools/ui_layout_dump
    except Exception:
        return []


def _make_element(
    *,
    widget: QWidget,
    element_id: str,
    role: str,
    label: str,
    rect: QRect,
    window: str,
    surface: str,
    action: Any | None,
    point: Any = None,
) -> Element:
    action_id = getattr(action, "action_id", None) if action is not None else None
    owner_tab = getattr(action, "owner_tab", None) if action is not None else None
    try:
        enabled = bool(widget.isEnabled())
    except Exception:
        enabled = False
    try:
        visible = bool(widget.isVisible())
    except Exception:
        visible = False
    return Element(
        id=element_id,
        widget=widget,
        role=role,
        label=label,
        rect=rect,
        window=window,
        surface=surface,
        action_id=action_id,
        owner_tab=owner_tab,
        enabled=enabled,
        visible=visible,
        properties=scalar_properties(widget),
        point=point,
    )


def _menu_rows(
    menu: QMenu, window: str, surface: str, path: str, include_disabled: bool
) -> list[Element]:
    """One element per actionable ``QMenu`` row, using ``actionGeometry``."""
    rows: list[Element] = []
    try:
        actions = list(menu.actions())
    except Exception:
        return rows
    for index, action in enumerate(actions):
        try:
            if action.isSeparator():
                continue
            geometry = menu.actionGeometry(action)
        except Exception:
            continue
        try:
            enabled = bool(action.isEnabled())
        except Exception:
            enabled = True
        if not include_disabled and not enabled:
            continue
        rows.append(
            Element(
                id=f"{window}/{path}.a{index}",
                widget=menu,
                role="menu-row",
                label=str(action.text() or ""),
                rect=geometry,
                window=window,
                surface=surface,
                action_id=None,
                owner_tab=None,
                enabled=enabled,
                visible=menu.isVisible(),
            )
        )
    return rows


def _tab_rows(
    tab_bar: QTabBar, window: str, surface: str, path: str, include_disabled: bool
) -> list[Element]:
    """One element per tab, using ``tabRect`` (click position selects it)."""
    rows: list[Element] = []
    try:
        count = int(tab_bar.count())
    except Exception:
        return rows
    bar_enabled = bool(tab_bar.isEnabled())
    if not include_disabled and not bar_enabled:
        return rows
    for index in range(count):
        try:
            geometry = tab_bar.tabRect(index)
            label = str(tab_bar.tabText(index) or "")
        except Exception:
            continue
        rows.append(
            Element(
                id=f"{window}/{path}.t{index}",
                widget=tab_bar,
                role="tab",
                label=label,
                rect=geometry,
                window=window,
                surface=surface,
                action_id=None,
                owner_tab=None,
                enabled=bar_enabled,
                visible=bool(tab_bar.isVisible()),
            )
        )
    return rows


def _walk(
    widget: QWidget,
    *,
    window: str,
    surface: str,
    path: str,
    action_map: dict[int, list[Any]],
    out: list[Element],
    seen: set[int],
    include_disabled: bool,
    in_menu: bool = False,
    in_grid_view: bool = False,
) -> None:
    if id(widget) in seen:
        return
    seen.add(id(widget))

    action = _best_action(action_map.get(id(widget)))
    action_id = getattr(action, "action_id", None) if action is not None else None
    element_id = f"{window}/{path}"
    if action_id:
        element_id = f"{element_id}@{action_id}"

    menu_like = _is_menu_like(widget)
    # A toolkit menu/flyout is a plain child widget (in-window overlay); only
    # its rows are interesting while it is open, so a hidden one is skipped
    # rather than polluting the inventory. Stock ``QMenu`` keeps its own path.
    if menu_like and not isinstance(widget, QMenu) and not _safe_visible(widget):
        return

    grid_here = in_grid_view or "GridView" in type(widget).__name__
    role = _role_of(widget, in_menu=in_menu, in_grid_view=grid_here)

    if isinstance(widget, QMenu):
        menu_element = _make_element(
            widget=widget,
            element_id=element_id,
            role="menu",
            label=_label_of(widget, action),
            rect=widget.rect(),
            window=window,
            surface=surface,
            action=action,
        )
        if include_disabled or menu_element.enabled:
            out.append(menu_element)
        out.extend(_menu_rows(widget, window, surface, path, include_disabled))
    elif isinstance(widget, QTabBar):
        out.extend(_tab_rows(widget, window, surface, path, include_disabled))
    elif role is not None or action_id is not None:
        rect = widget.rect()
        if _has_area(rect):
            element = _make_element(
                widget=widget,
                element_id=element_id,
                role=role or "action",
                label=_label_of(widget, action),
                rect=rect,
                window=window,
                surface=surface,
                action=action,
            )
            if include_disabled or element.enabled:
                out.append(element)

    try:
        children = widget.findChildren(
            QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
        )
    except Exception:
        children = []
    for index, child in enumerate(children):
        # Windows (popups, dialogs, menus) are enumerated by the top-level pass
        # so a menu parented to a button is attributed to its own window.
        try:
            if child.isWindow():
                continue
        except Exception:
            pass
        _walk(
            child,
            window=window,
            surface=surface,
            path=f"{path}.{index}",
            action_map=action_map,
            out=out,
            seen=seen,
            include_disabled=include_disabled,
            in_menu=in_menu or menu_like,
            in_grid_view=grid_here,
        )


def _scope_page(scope: str) -> QWidget | None:
    """The live page widget a tab id maps to, or ``None`` if not open.

    ``TabRegistry`` creates tab pages lazily, so a registered-but-never-opened
    tab has no page and yields no elements (an honest empty result).
    """
    try:
        from tabs.registry import TabRegistry

        return TabRegistry().get_page(scope)
    except Exception:
        return None


def _within_page(widget: QWidget, page: QWidget) -> bool:
    if widget is page:
        return True
    try:
        return bool(page.isAncestorOf(widget))
    except Exception:
        return False


def collect_elements(
    *,
    registry: Any,
    scope: str | None = None,
    include_disabled: bool = False,
) -> list[Element]:
    """Flatten every live top-level window into interactive elements.

    Popup/menu top-levels (``Qt.Popup`` / ``Qt.Tool``) are included so an open
    menu is discoverable *while it is open*, and in-window toolkit flyouts are
    walked as ordinary children (see :func:`_is_menu_like`).

    ``scope`` is a **tab id**: elements are filtered to those contained in
    ``TabRegistry().get_page(scope)``. A scope that is not an open tab (or was
    never registered) returns an empty list — never an error. For backward
    compatibility a scope that names no page but does match a live window
    identity still filters by that window.
    """
    windows = _top_level_widgets()
    keys = _window_keys(windows)
    action_map = _action_descriptors(registry)
    out: list[Element] = []
    seen: set[int] = set()
    for window, key in zip(windows, keys):
        surface = "popup" if _is_popup_window(window) else "window"
        try:
            _walk(
                window,
                window=key,
                surface=surface,
                path="0",
                action_map=action_map,
                out=out,
                seen=seen,
                include_disabled=include_disabled,
                # A popup-surface toolkit menu is its own top-level: seed the
                # menu context so its rows are ``menu-row``, not ``button``.
                in_menu=_is_menu_like(window),
            )
        except Exception:
            continue
    if scope:
        page = _scope_page(str(scope))
        if page is not None:
            out = [element for element in out if _within_page(element.widget, page)]
        else:
            needle = str(scope).lower()
            out = [element for element in out if needle in element.window.lower()]
    return out


def inventory_payload(elements: list[Element], *, total: int | None = None) -> dict[str, Any]:
    """The frozen ``ui_elements`` result shape.

    ``total`` is the match count before ``limit`` truncation; it equals
    ``count`` (and is reported for every call) so a client never has to guess
    whether a short answer was narrowed.
    """
    rows = [element.payload() for element in elements]
    windows = sorted({element.window for element in elements})
    return {
        "count": len(rows),
        "total": len(rows) if total is None else int(total),
        "window_count": len(windows),
        "elements": rows,
    }

