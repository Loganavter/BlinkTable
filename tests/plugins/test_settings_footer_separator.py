"""The settings footer: a hairline above Save/Ok, and labels that say what they do."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QFrame, QVBoxLayout


def _button_text(button) -> str:
    """The toolkit Button paints its label from its ``_main`` region, not a text()."""
    for region in getattr(button._controller, "regions", []):
        if region.id == "_main":
            return str(region.text or "")
    return ""


def _dialog(qapp):
    """Build the real settings dialog (offscreen) without opening the app."""
    from plugins.settings.dialog import SettingsDialog

    return SettingsDialog(
        current_language="en",
        current_theme="auto",
        current_max_length=50,
        min_limit=1,
        max_limit=100,
        debug_mode_enabled=False,
        system_notifications_enabled=True,
    )


def test_the_footer_is_separated_from_the_page(qapp):
    """Owner request 2026-11-04: a horizontal split line above Save / Ok."""
    dialog = _dialog(qapp)
    try:
        separator = getattr(dialog, "action_bar_separator", None)
        assert separator is not None, "the settings footer has no separator"
        assert separator.frameShape() == QFrame.Shape.HLine
        assert separator.objectName() == "SettingsActionBarSeparator"
        layout = dialog.content_layout
        assert isinstance(layout, QVBoxLayout)
        assert layout.indexOf(separator) + 1 == layout.indexOf(dialog.action_bar), (
            "the separator must sit directly above the action bar"
        )
        assert separator.height() >= 1
    finally:
        dialog.deleteLater()


def test_the_footer_labels_say_what_the_buttons_do(qapp):
    """Save applies and keeps the window open; Ok just closes.

    Asserted against the dialog's own translator, not a literal: this harness
    does not load the plugin catalogs, and the point is WHICH key each button
    carries (the primary used ``common.ok`` before this change).
    """
    dialog = _dialog(qapp)
    try:
        save_text = dialog._tr("settings.save_button", dialog.current_language)
        ok_text = dialog._tr("common.ok", dialog.current_language)
        assert _button_text(dialog.ok_button) == save_text
        assert _button_text(dialog.cancel_button) == ok_text
        assert save_text != ok_text
    finally:
        dialog.deleteLater()
