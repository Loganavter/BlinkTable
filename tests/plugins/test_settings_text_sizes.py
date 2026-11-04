"""Settings dialog typography groups (TITLE / BODY / CAPTION).

Regression: settings pages must size text through
``plugins.settings.typography`` instead of hardcoded ``pixel_size=``
literals, so the whole dialog scales as one system:

- TITLE (18px) — every ``CustomGroupWidget`` caption;
- BODY (16px) — checkboxes, radios, combos, edits, spins, primary labels;
- CAPTION (14px) — hints, warnings, footnotes.

Toolkit-owned chrome (sidebar rows, search fields, ``Button`` family) is
out of scope and keeps its own sizing.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from plugins.settings.dialog import SettingsDialog
from plugins.settings.typography import (
    SETTINGS_BODY_PX,
    SETTINGS_CAPTION_PX,
    SETTINGS_TITLE_PX,
)
from sli_ui_toolkit.managers import UiScale
from sli_ui_toolkit.widgets import (
    CheckBox,
    ComboBox,
    CustomGroupWidget,
    CustomLineEdit,
    Text,
)


@pytest.fixture(autouse=True)
def _scale_one():
    UiScale.get_instance().set_factor(1.0)
    yield
    UiScale.get_instance().set_factor(1.0)


def _dialog(qapp: QApplication) -> SettingsDialog:
    dialog = SettingsDialog("en", "dark", 60, 10, 200, False, True)
    dialog.show()
    qapp.processEvents()
    return dialog


def _px(widget: QWidget) -> int:
    return widget.font().pixelSize()


def test_typography_ladder_ordering():
    assert SETTINGS_TITLE_PX > SETTINGS_BODY_PX > SETTINGS_CAPTION_PX
    assert (SETTINGS_TITLE_PX, SETTINGS_BODY_PX, SETTINGS_CAPTION_PX) == (18, 16, 14)


def test_settings_group_titles_are_title(qapp: QApplication):
    dialog = _dialog(qapp)
    try:
        groups = dialog.findChildren(CustomGroupWidget)
        assert groups, "settings dialog must build group widgets"
        for group in groups:
            assert _px(group) == SETTINGS_TITLE_PX, group.get_title()
            # Near-flush sides: rows align to the group edge, keeping only
            # the 10px guard so opaque children clear the 1px border
            # (vertical title clearance untouched).
            margins = group.layout().contentsMargins()
            assert margins.left() == 10, group.get_title()
            assert margins.right() == 10, group.get_title()
    finally:
        dialog.close()


def test_settings_group_children_clear_border(qapp: QApplication):
    """No visible child may reach the 1px group border (it gets overpainted).

    Regression: zero side insets let opaque QLineEdit backgrounds chew
    notches into the group frame. Walks every settings page.
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QWidget

    from plugins.settings.typography import SettingsGroupWidget

    dialog = _dialog(qapp)
    try:
        stack = dialog.pages_stack
        for i in range(stack.count()):
            stack.setCurrentIndex(i)
            qapp.processEvents()
            page = stack.widget(i)
            for group in page.findChildren(SettingsGroupWidget):
                gw = group.width()
                lay = group.layout()
                # Rows span the full content width — no extra side insets
                # at row level, only the 5px group guard.
                for i in range(lay.count()):
                    row = lay.itemAt(i).widget()
                    if row is None or not row.isVisibleTo(group):
                        continue
                    left = row.mapTo(group, QPoint(0, 0)).x()
                    right = row.mapTo(group, QPoint(row.width(), 0)).x()
                    assert left == 10, (group.get_title(), row, left)
                    assert right == gw - 10, (group.get_title(), row, right, gw)
                for child in group.findChildren(QWidget):
                    if not child.isVisibleTo(group) or child.width() <= 0:
                        continue
                    left = child.mapTo(group, QPoint(0, 0)).x()
                    right = child.mapTo(group, QPoint(child.width(), 0)).x()
                    assert left >= 10, (group.get_title(), child, left)
                    assert right <= gw - 10, (group.get_title(), child, right, gw)
    finally:
        dialog.close()


def test_settings_checkboxes_are_body(qapp: QApplication):
    dialog = _dialog(qapp)
    try:
        for box in (
            dialog.system_notifications_checkbox,
            dialog.debug_checkbox,
            dialog.crop_checkbox,
            dialog.auto_psnr_checkbox,
            dialog.auto_ssim_checkbox,
            dialog.optimize_movement_checkbox,
            dialog.laser_smoothing_checkbox,
            dialog.magnifier_intersection_highlight_checkbox,
            dialog.magnifier_auto_color_checkbox,
        ):
            assert isinstance(box, CheckBox)
            assert _px(box) == SETTINGS_BODY_PX, box.text()
    finally:
        dialog.close()


def test_settings_radios_combos_spins_are_body(qapp: QApplication):
    dialog = _dialog(qapp)
    try:
        radios = (
            dialog.radio_en,
            dialog.radio_ru,
            dialog.radio_zh,
            dialog.radio_pt_br,
            dialog.radio_ui_mode_beginner,
            dialog.radio_ui_mode_advanced,
            dialog.radio_ui_mode_expert,
            dialog.radio_font_builtin,
            dialog.radio_font_system_default,
            dialog.radio_font_system_custom,
        )
        assert radios
        for rb in radios:
            assert _px(rb) == SETTINGS_BODY_PX, rb.text()
        for combo in (
            dialog.combo_theme,
            dialog.combo_font_family,
            dialog.combo_rhi_backend,
            dialog.combo_zoom_interp,
            dialog.combo_mag_interp,
            dialog.combo_laser_interp,
        ):
            assert isinstance(combo, ComboBox)
            assert _px(combo) == SETTINGS_BODY_PX
        assert _px(dialog.spin_max_length) == SETTINGS_BODY_PX
    finally:
        dialog.close()


def test_settings_primary_labels_are_body(qapp: QApplication):
    dialog = _dialog(qapp)
    try:
        assert dialog._keyboard_action_labels
        for name, _key in dialog._keyboard_action_labels:
            assert _px(name) == SETTINGS_BODY_PX, name.text()
    finally:
        dialog.close()


def test_settings_gallery_ai_page_is_body(qapp: QApplication):
    dialog = _dialog(qapp)
    try:
        assert isinstance(dialog.gallery_ai_enabled, CheckBox)
        assert _px(dialog.gallery_ai_enabled) == SETTINGS_BODY_PX
        for field in (
            dialog.gallery_ai_base_url,
            dialog.gallery_ai_api_key,
            dialog.gallery_ai_model,
        ):
            assert isinstance(field, CustomLineEdit)
            assert _px(field) == SETTINGS_BODY_PX, field.placeholderText()
        # password echo + placeholder survive the typography pass.
        # ``CustomLineEdit.echoMode()`` returns the toolkit's int-valued
        # ``EchoMode`` (not ``QLineEdit.EchoMode``), so compare against the
        # widget's own enum value.
        assert dialog.gallery_ai_api_key.placeholderText()
        assert (
            int(dialog.gallery_ai_api_key.echoMode())
            == dialog.gallery_ai_api_key.EchoMode.Password
        )
    finally:
        dialog.close()


def test_settings_perf_extras_are_body(qapp: QApplication):
    dialog = _dialog(qapp)
    try:
        assert isinstance(dialog.lbl_zoom_interp, Text)
        assert _px(dialog.lbl_zoom_interp) == SETTINGS_BODY_PX
        assert isinstance(dialog.lbl_fps, Text)
        assert _px(dialog.lbl_fps) == SETTINGS_BODY_PX
        assert dialog.lbl_fps.text() == "FPS:"
        assert _px(dialog.spin_fps) == SETTINGS_BODY_PX
        dialog.spin_fps.setValue(60)
        assert dialog.spin_fps.value() == 60
    finally:
        dialog.close()


def test_settings_gallery_labels_fit(qapp: QApplication):
    """Gallery AI row labels are wide enough for their text (BODY size).

    Regression: at 110px the Cyrillic labels clipped under the line edit.
    """
    from PySide6.QtGui import QFontMetrics

    dialog = _dialog(qapp)
    try:
        for label in (
            dialog.gallery_ai_base_url_label,
            dialog.gallery_ai_api_key_label,
            dialog.gallery_ai_model_label,
            dialog.gallery_ai_ask_tokens_label,
            dialog.gallery_ai_effort_label,
            dialog.gallery_ai_samples_label,
        ):
            need = QFontMetrics(label.font()).horizontalAdvance(label.text())
            assert label.width() >= need, (label.text(), label.width(), need)
    finally:
        dialog.close()


def test_settings_capture_buttons_are_uniform(qapp: QApplication):
    """Shortcut capture buttons share one cell regardless of chord text.

    Regression: the base Button clears fixed min/max on the first
    empty→text transition, so heights stretched after wrapped labels
    and widths followed the chord text.
    """
    from sli_ui_toolkit.managers import scaled_px

    dialog = _dialog(qapp)
    try:
        dialog.pages_stack.setCurrentWidget(dialog.page_keyboard)
        qapp.processEvents()
        assert dialog._keyboard_capture_rows
        for action_id, cap in dialog._keyboard_capture_rows.items():
            assert (cap.width(), cap.height()) == (
                scaled_px(140),
                scaled_px(28),
            ), (action_id, cap.width(), cap.height())
    finally:
        dialog.close()


def test_settings_keyboard_rows_are_uniform(qapp: QApplication):
    """Capture rows share one height — no uneven gaps.

    Regression: single-line rows collapsed to 28px next to 44px wrapped
    ones, so the vertical rhythm fell apart.
    """
    dialog = _dialog(qapp)
    try:
        dialog.pages_stack.setCurrentWidget(dialog.page_keyboard)
        qapp.processEvents()
        assert dialog._keyboard_filter_rows
        heights = set()
        for row, _group, _entry in dialog._keyboard_filter_rows:
            assert row.isVisibleTo(dialog.page_keyboard)
            heights.add(row.height())
        assert len(heights) == 1, sorted(heights)
    finally:
        dialog.close()


def test_settings_captions_are_caption(qapp: QApplication):
    """Secondary text (hints/warnings) uses CAPTION, shell chrome stays smaller."""
    dialog = _dialog(qapp)
    try:
        assert _px(dialog.lbl_rhi_backend_hint) == SETTINGS_CAPTION_PX
        hint = dialog.lbl_rhi_backend_hint
        # Wrapped hint takes exactly its heightForWidth — no dead space
        # above/below the line (Fixed vertical + wiped hfw flag used to
        # pin the stale multi-line sizeHint height).
        assert hint.height() == hint.heightForWidth(hint.width())
        for field in (dialog.search_field, dialog._keyboard_search):
            assert isinstance(field, CustomLineEdit)
            assert field.placeholderText(), "search field must keep its hint"
            assert _px(field) < SETTINGS_BODY_PX
    finally:
        dialog.close()


def test_settings_keyboard_capture_keeps_one_owner_per_chord(qapp: QApplication):
    """Settings cannot leave two actions on one chord in one tab.

    End-to-end through the page's own commit path (``_ShortcutCaptureButton``
    → ``_set_override`` → ``steal_chord_in_overrides``): type a chord that
    another action of the SAME tab already owns, then assert the invariant on
    the dialog's live override map — the loser is explicitly unbound and the
    typed action holds the chord alone.
    """
    from plugins.settings.keymap_defaults import collect_keymap_defaults
    from ui.actions.keymap import effective_shortcut_for_id, normalize_sequence
    from ui.actions.keymap_uniqueness import (
        assert_unique_bindings_all_scopes,
        scope_bindings,
    )

    defaults = {
        entry.action_id: (entry.default_shortcut, entry.owner_tab)
        for entry in collect_keymap_defaults().all_entries()
    }
    pair = next(
        (
            (victim, taker)
            for victim, (chord, owner) in defaults.items()
            if owner and chord
            for taker, (_taker_chord, taker_owner) in defaults.items()
            if taker_owner == owner and taker != victim
        ),
        None,
    )
    assert pair is not None, "catalog needs two actions of one tab"
    victim, taker = pair

    dialog = _dialog(qapp)
    try:
        dialog.pages_stack.setCurrentWidget(dialog.page_keyboard)
        qapp.processEvents()
        chord = effective_shortcut_for_id(
            victim,
            default=defaults[victim][0],
            overrides=dialog._keyboard_overrides,
        )
        assert chord, "victim must hold a chord to be stolen"
        dialog._keyboard_capture_rows[taker]._commit(chord)

        assert_unique_bindings_all_scopes(defaults, dialog._keyboard_overrides)
        holders = scope_bindings(
            defaults, dialog._keyboard_overrides, tab=defaults[taker][1]
        )[normalize_sequence(chord)]
        assert holders == (taker,), holders
        assert dialog._keyboard_overrides.get(victim) == ""
    finally:
        dialog.close()
