"""Fresh install: every pinned user-facing default is what a user actually gets.

Companion of ``tests/contracts/test_user_defaults_agree_across_owners.py``: the
contract proves the *declared* defaults agree across their owners (the
``SettingsState`` dataclass in ``src/core/store_settings.py`` is the source of
truth; ``SettingsManager.load_all_settings`` and the ``getattr`` fallbacks are
copies). This file proves the runtime load path over an EMPTY ``QSettings``
delivers the same values — a statically-agreeing table that the loader never
applies is exactly the 2026-11-18 bug, and a table nobody stakes a behaviour on
is how it drifted in the first place.

The expected value is always read from ``PINNED_DEFAULTS`` (one pin, one
place); each test names its key so the contract's coverage check can see it.
Offscreen Qt only — no window, no dialogs.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from core.store import Store
from plugins.settings.manager import SettingsManager
from tests.contracts.test_user_defaults_agree_across_owners import PINNED_DEFAULTS


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(scope="module")
def fresh_settings(qapp, tmp_path_factory):
    """``SettingsState`` loaded from an empty, file-backed QSettings store."""
    tmp_path = tmp_path_factory.mktemp("fresh-install-defaults")
    ini_path = str(tmp_path / "fresh.ini")
    settings = QSettings(ini_path, QSettings.Format.IniFormat)
    assert settings.allKeys() == [], (
        f"{settings.fileName()} is not a fresh install — it already stores "
        f"{settings.allKeys()}"
    )
    manager = SettingsManager("improve-imgsli-defaults", "defaults")
    # Same injection as tests/plugins/test_export_save_view.py::_manager_for:
    # keep the load path, replace only where it reads from.
    manager.settings = settings
    manager._backup_path = settings.fileName() + ".backup"
    store = Store()
    manager.load_all_settings(store)
    return store.settings


def test_fresh_install_does_not_crop_the_compared_pair(fresh_settings):
    """The pair is shown whole; cropping is an explicit user choice."""
    assert (
        fresh_settings.auto_crop_black_borders
        is PINNED_DEFAULTS["auto_crop_black_borders"]
    )


def test_fresh_install_export_follows_the_canvas_crop(fresh_settings):
    """The written file is what the canvas shows, crop window included."""
    assert (
        fresh_settings.export_crop_to_image is PINNED_DEFAULTS["export_crop_to_image"]
    )


def test_fresh_install_exports_the_whole_canvas_not_only_the_view(
    fresh_settings,
):
    """Save-view is opt-in; a fresh install writes the full canvas."""
    assert fresh_settings.export_save_view is PINNED_DEFAULTS["export_save_view"]


def test_fresh_install_keeps_the_background_transparent(fresh_settings):
    """No fill colour is added unless the user asks for it."""
    assert (
        fresh_settings.export_fill_background
        is PINNED_DEFAULTS["export_fill_background"]
    )
