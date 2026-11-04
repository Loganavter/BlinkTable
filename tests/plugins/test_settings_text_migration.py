"""Settings display labels use the unified ``Text`` widget (P3 wave 1).

The settings typography helpers were the last ``Label`` constructions under
``src/plugins/settings``. They now return ``Text`` in plain mode (``Label``
parity), so every settings page — built-in and tab-owned — gets the unified
widget through the same two factory functions.

Mapping table (old call site -> new config):

=========================================  ===========================================
old                                         new
=========================================  ===========================================
``Label(text, pixel_size=16, parent=...)``  ``Text(text, parent=..., mode="plain",
                                            pixel_size=16)``  (variant ``body``)
``Label(text, pixel_size=14, parent=...)``  ``Text(text, parent=..., mode="plain",
                                            pixel_size=14)``  (variant ``body``)
=========================================  ===========================================

Effective sizes are unchanged: ``BODY`` = 16 px, ``CAPTION`` = 14 px, and the
variant remains the default ``body`` (its 12 px preset is overridden by the
explicit pixel size, exactly as it was for ``Label``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from plugins.settings.dialog import SettingsDialog
from plugins.settings.typography import (
    SETTINGS_BODY_PX,
    SETTINGS_CAPTION_PX,
)
from sli_ui_toolkit.ui.widgets.atomic.text_labels import Label  # internal: documented Label exception (queue task-text-unification-retirement-2026-10-21)
from sli_ui_toolkit.widgets import Text

_SETTINGS_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "plugins" / "settings"
)

#: No allowlist: every settings display label is migrated in this wave.
_ALLOWED_LABEL_USES: dict[str, str] = {}


def _label_uses(path: Path) -> list[str]:
    """Imports/constructions of ``Label`` recorded as ``<kind>:<name>``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    uses: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "Label":
                    uses.append(f"import:{alias.asname or alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "Label" or alias.name.endswith(".Label"):
                    uses.append(f"import:{alias.asname or alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "Label":
                uses.append(f"call:{func.id}")
            elif isinstance(func, ast.Attribute) and func.attr == "Label":
                uses.append(f"call:{func.attr}")
    return uses


def test_settings_sources_have_no_label_construction_or_import():
    offenders: list[str] = []
    sources = sorted(_SETTINGS_ROOT.rglob("*.py"))
    assert sources, "settings plugin sources must exist"
    for path in sources:
        rel = str(path.relative_to(_SETTINGS_ROOT))
        if rel in _ALLOWED_LABEL_USES:
            continue
        for use in _label_uses(path):
            offenders.append(f"{rel}: {use}")
    assert not offenders, (
        "settings display labels must use the unified Text widget; found "
        f"remaining Label usage: {offenders}"
    )


@pytest.fixture(autouse=True)
def _scale_one():
    from sli_ui_toolkit.managers import UiScale

    UiScale.get_instance().set_factor(1.0)
    yield
    UiScale.get_instance().set_factor(1.0)


def _dialog() -> SettingsDialog:
    dialog = SettingsDialog("en", "dark", 60, 10, 200, False, True)
    dialog.show()
    QApplication.processEvents()
    return dialog


def _show_page_containing(dialog: SettingsDialog, widget) -> None:
    stack = dialog.pages_stack
    for index in range(stack.count()):
        page = stack.widget(index)
        if page.isAncestorOf(widget) or widget is page:
            stack.setCurrentWidget(page)
            QApplication.processEvents()
            return
    pytest.fail(f"no settings page contains {widget!r}")


def test_settings_body_labels_are_plain_text(qapp: QApplication):
    """Keyboard row labels: BODY (16 px) plain ``Text``, still wrapping."""
    dialog = _dialog()
    try:
        assert dialog._keyboard_action_labels
        for name, _key in dialog._keyboard_action_labels:
            assert isinstance(name, Text), type(name)
            assert not isinstance(name, Label)
            assert name.mode() == "plain"
            assert name.variant() == "body"
            assert name.font().pixelSize() == SETTINGS_BODY_PX
            assert name.wordWrap() is True
    finally:
        dialog.close()


def test_settings_caption_hint_is_plain_text(qapp: QApplication):
    """Render-backend hint: CAPTION (14 px) plain ``Text``; layout intact."""
    dialog = _dialog()
    try:
        hint = dialog.lbl_rhi_backend_hint
        assert isinstance(hint, Text), type(hint)
        assert not isinstance(hint, Label)
        assert hint.mode() == "plain"
        assert hint.variant() == "body"
        assert hint.font().pixelSize() == SETTINGS_CAPTION_PX
        assert hint.wordWrap() is True

        # Existing geometry invariant (see test_settings_text_sizes): the
        # wrapped hint takes exactly its heightForWidth, no dead space.
        _show_page_containing(dialog, hint)
        assert hint.width() > 0
        assert hint.height() == hint.heightForWidth(hint.width())
        assert hint.isVisible()
    finally:
        dialog.close()


def test_settings_tab_owned_labels_are_plain_text(qapp: QApplication):
    """Tab-owned settings pages inherit the migration through the helpers."""
    dialog = _dialog()
    try:
        for widget in (dialog.lbl_zoom_interp, dialog.lbl_fps):
            assert isinstance(widget, Text), type(widget)
            assert not isinstance(widget, Label)
            assert widget.mode() == "plain"
            assert widget.variant() == "body"
            assert widget.font().pixelSize() == SETTINGS_BODY_PX
        assert dialog.lbl_fps.text() == "FPS:"
    finally:
        dialog.close()
