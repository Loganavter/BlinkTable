"""The settings footer: a hairline above Save/Ok, and labels that say what they do."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QFrame


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
        assert separator is dialog.shell.footer_separator
        # The SHELL's hairline: one device column in the divider colour, and a
        # row of its own so it can run edge to edge.
        from sli_ui_toolkit.ui.widgets.composite.dialog_shell import _DialogHairline

        assert isinstance(separator, _DialogHairline)
        # Toolkit #147/#149 moved the hairline into the shell's own RULE ROW:
        # a flush QLayout (a fresh one takes the style's ~9 px margins) so the
        # rule reaches the splitter column, and the column itself stays flush
        # so it reaches both edges. Assert the contract, not the old parent.
        rule_row = getattr(dialog.shell, "_rule_row", None)
        assert rule_row is not None, "the shell has no rule row for the footer rule"
        assert separator.parent() is rule_row
        rule_layout = rule_row.layout()
        assert rule_layout is not None, "the footer rule's row has no layout"
        assert rule_layout.contentsMargins().left() == 0
        assert rule_layout.contentsMargins().right() == 0
        assert dialog.shell.content_layout.contentsMargins().left() == 0
        assert dialog.shell.content_layout.contentsMargins().right() == 0
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
