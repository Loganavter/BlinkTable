"""A live language switch re-applies bound text — and only bound text.

Measured 2026-10-29 (11.0.1): ``set_language ru`` on a running instance left
the Image Compare toolbar and the onboarding overlay in English, while a
restart in ``ru`` was fully correct and ``check_translations.py --strict
--code-keys`` reported 0 gaps. So the catalogs were fine and the *coverage*
was not: a surface that resolves its text once with a bare ``tr(key)`` keeps
the language it was built in, and only a restart replaces it.

The two cases pin the class from both sides:

1. ``translatable_text`` (the toolkit declarative binding) follows
   ``emit_language_changed`` — this is the mechanism every migrated surface
   now uses.
2. The first-run onboarding overlay (built once at startup with raw ``tr``)
   did NOT follow it — this case is red on the base commit and green after the
   bindings landed in ``plugins/onboarding/{overlay,pages}.py``.

The static half of the same rule is ``src/devtools/check_language_bindings.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from resources.translations import add_i18n_root

_REPO = Path(__file__).parents[2]


def _install_onboarding_catalog() -> None:
    """Register the plugin-owned catalogs (the registry does this at bootstrap).

    ``settings.ui_mode_*`` lives in the settings plugin's catalog — the mode
    chips borrow those names, so both roots are needed.
    """
    add_i18n_root(_REPO / "src" / "plugins" / "onboarding" / "resources" / "i18n")
    add_i18n_root(_REPO / "src" / "plugins" / "settings" / "resources" / "i18n")


@pytest.fixture
def language_restored():
    """Leave the global language as we found it (the manager is a singleton)."""
    from sli_ui_toolkit.i18n import get_current_language

    previous = get_current_language()
    _install_onboarding_catalog()
    yield
    from sli_ui_toolkit.i18n import emit_language_changed

    emit_language_changed(previous)


def _button_text(button) -> str:
    """Button renders through the painter pipeline — text lives on the style API."""
    text = getattr(button, "_text", "") or ""
    if text:
        return text
    for region in getattr(button, "regions", lambda: [])() or []:
        text = getattr(region, "text", "") or ""
        if text:
            return text
    return ""


class _FakeSettings:
    """First-run settings double (the overlay only needs the two writes)."""

    def __init__(self) -> None:
        self.saved: dict[str, object] = {}

    def is_first_run(self) -> bool:
        return True

    def _save_setting(self, key: str, value: object) -> None:
        self.saved[key] = value

    def set_first_run_completed(self) -> None:
        pass

    def schedule_persist(self, store) -> None:
        return None


def _build_overlay():
    from core.store import Store
    from plugins.onboarding.overlay import OnboardingOverlay

    overlay = OnboardingOverlay(_FakeSettings(), Store())
    overlay.show()
    QApplication.processEvents()
    return overlay


def test_translatable_text_follows_a_live_language_switch(language_restored, qapp):
    """Declarative binding: the text is re-resolved on ``language_changed``."""
    from sli_ui_toolkit.i18n import emit_language_changed, translatable_text

    emit_language_changed("en")
    label = QLabel()
    translatable_text(label, "onboarding.start_button")
    assert label.text() == "Get Started"

    emit_language_changed("ru")
    assert label.text() == "Начать работу"


def test_onboarding_overlay_retranslates_on_a_live_language_switch(
    language_restored, qapp
):
    """The overlay is built once at startup — every string must be bound.

    Red on the base commit: the overlay resolved ``tr(key, current_lang)`` in
    ``_init_ui``/``create_slide_for_mode``, so after the switch the start
    button still read "Get Started" and the hint still read the English pair.
    """
    from sli_ui_toolkit.i18n import emit_language_changed

    emit_language_changed("en")
    overlay = _build_overlay()
    try:
        assert _button_text(overlay.btn_start) == "Get Started"
        assert overlay.welcome_lbl.text() != "onboarding.welcome"
        assert _button_text(overlay.mode_buttons[0]) == "Beginner"
        slide = overlay.stack.widget(0)
        assert slide._onboarding_title.text() == "Beginner"
        assert slide._onboarding_captions[0].text() == "Rotate"

        emit_language_changed("ru")

        assert _button_text(overlay.btn_start) == "Начать работу"
        assert overlay.welcome_lbl.text() == "Добро пожаловать"
        assert _button_text(overlay.mode_buttons[0]) == "Начинающий"
        assert slide._onboarding_title.text() == "Начинающий"
        assert slide._onboarding_captions[0].text() == "Поворот"
        assert "Режим интерфейса" in overlay.hint_label.text()
    finally:
        overlay.close()
        overlay.deleteLater()
