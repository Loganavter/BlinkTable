"""Export defaults must consult QStandardPaths, not a hardcoded ``~/Downloads``.

The resolver ``default_downloads_dir`` lives in the image-compare export state;
every export default (still-image export state, video editor output field,
video editor browse dialog) must route through it. A fake ``QStandardPaths``
proves the call sites ask Qt for the location and that a redirected folder
(OneDrive) wins over ``~/Downloads``.

The tab-free source gate (no dead probe, a single home-Downloads literal)
lives in ``tests/plugins/test_export_default_downloads.py`` alongside the
other host-level export checks.

What this suite cannot prove: the path Qt actually answers on a real Windows
host (redirected or localised profile) — that needs a live host.

The same coordinator owns the export settings write path, so the suite also
pins that ``persist_export_settings`` dispatches the "crop to images" scalar
(``SetExportCropToImageAction``) instead of mutating ``store.settings``
directly.

No ``QApplication`` is created here: ``QStandardPaths.writableLocation`` works
without one on the offscreen platform. If a platform ever requires an app
instance for it, this suite fails loudly instead of silently adding one.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QStandardPaths as _RealQStandardPaths

from tabs.image_compare.plugins.video_editor.dialog import persistence as persistence_module
from tabs.image_compare.plugins.video_editor.presenter_parts import common as common_module
from tabs.image_compare.plugins.video_editor.presenter_parts.output_paths import (
    OutputPathCoordinator,
)
from tabs.image_compare.services.image_export.state import (
    ExportStateCoordinator,
    default_downloads_dir,
)


def _live_store():
    """Real Store + Dispatcher — the reducer path, not a mocked mutation."""
    from core.state_management.dispatcher import Dispatcher
    from core.store import Store

    store = Store()
    store.set_dispatcher(Dispatcher(store))
    return store


def test_persist_export_settings_dispatches_the_crop_scalar():
    """The crop flag is a dispatched settings scalar, not a direct store write.

    Red-before: with the ``SetExportCropToImageAction`` reducer branch removed
    the action reduces to a no-op, so ``store.settings`` never changes (and no
    action reaches the dispatcher). The identity check below can only pass if
    the value arrived through dispatch → SettingsReducer → replace.
    """
    store = _live_store()
    seen: list[str] = []
    store.get_dispatcher().subscribe(lambda action: seen.append(action.type))
    coordinator = ExportStateCoordinator(store, None, None)
    options = {"output_dir": "/tmp/out", "file_name": "shot"}

    coordinator.persist_export_settings({**options, "crop_to_image": True})
    assert "SET_EXPORT_CROP_TO_IMAGE" in seen
    assert store.settings.export_crop_to_image is True

    coordinator.persist_export_settings({**options, "crop_to_image": False})
    assert store.settings.export_crop_to_image is False


def _fake_standard_paths(location: str):
    class _FakeQStandardPaths:
        StandardLocation = _RealQStandardPaths.StandardLocation

        @staticmethod
        def writableLocation(_kind):
            return location

    return _FakeQStandardPaths


@pytest.fixture
def redirected(tmp_path, monkeypatch):
    """Pretend the OS reports a redirected (OneDrive-style) Downloads folder."""
    target = tmp_path / "OneDrive" / "Downloads"
    target.mkdir(parents=True)
    monkeypatch.setattr(
        "PySide6.QtCore.QStandardPaths", _fake_standard_paths(str(target))
    )
    return target


class _LineEditStub:
    def __init__(self, text: str = ""):
        self._text = text

    def setText(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text

    def setCursorPosition(self, _position: int) -> None:
        pass


class _OutputPathCoordinatorProbe(OutputPathCoordinator):
    """Keeps only the output-dir defaulting logic of ``initialize_output_fields``."""

    def generate_suggested_name(self) -> str:
        return "video_export"

    def refresh_unique_output_filename(self) -> None:
        pass


def _output_path_coordinator(settings):
    view = SimpleNamespace(
        edit_output_dir=_LineEditStub(), edit_filename=_LineEditStub()
    )
    main_controller = SimpleNamespace(store=SimpleNamespace(settings=settings))
    editor_service = SimpleNamespace(get_snapshot_at_time=lambda _t: None)
    return _OutputPathCoordinatorProbe(
        view, main_controller, editor_service, SimpleNamespace()
    )


def _browse_dialog(settings):
    return SimpleNamespace(
        edit_output_dir=_LineEditStub(""),
        export_controller=SimpleNamespace(store=SimpleNamespace(settings=settings)),
        _tr=lambda key: key,
    )


def test_resolver_uses_redirected_standard_location(redirected):
    assert default_downloads_dir() == str(redirected.resolve())


def test_export_state_default_uses_redirected_standard_location(redirected):
    coordinator = ExportStateCoordinator(
        store=None, main_controller=None, main_window_app=None
    )
    assert coordinator.get_os_default_downloads() == str(redirected.resolve())

    # The dead ``_get_os_default_downloads`` probe must stay dead: an app object
    # exposing that attribute must not hijack the resolved location.
    legacy_app = SimpleNamespace(_get_os_default_downloads=lambda: "/legacy/wrong")
    legacy_coordinator = ExportStateCoordinator(
        store=None, main_controller=None, main_window_app=legacy_app
    )
    assert legacy_coordinator.get_os_default_downloads() == str(redirected.resolve())


def test_video_editor_output_dir_uses_redirected_standard_location(redirected):
    coordinator = _output_path_coordinator(
        SimpleNamespace(export_video_favorite_dir=None, export_default_dir="")
    )
    coordinator.initialize_output_fields()
    assert coordinator.view.edit_output_dir.text() == str(redirected.resolve())


def test_browse_output_dir_falls_back_to_redirected_standard_location(
    redirected, monkeypatch
):
    opened = {}

    class _FakeFileDialog:
        class Option:
            ShowDirsOnly = 1
            DontUseNativeDialog = 2

        @staticmethod
        def getExistingDirectory(_parent, _caption, directory, _options):
            opened["directory"] = directory
            return ""

    monkeypatch.setattr(persistence_module, "QFileDialog", _FakeFileDialog)
    persistence = persistence_module.VideoEditorDialogPersistence.__new__(
        persistence_module.VideoEditorDialogPersistence
    )
    persistence.dialog = _browse_dialog(SimpleNamespace(export_default_dir=""))

    persistence.browse_output_dir()

    assert opened["directory"] == str(redirected.resolve())


def test_empty_standard_location_falls_back_to_home_downloads(monkeypatch):
    monkeypatch.setattr("PySide6.QtCore.QStandardPaths", _fake_standard_paths(""))

    expected = str((Path.home() / "Downloads").resolve())
    result = default_downloads_dir()
    assert isinstance(result, str)
    assert result == expected
    assert common_module.default_downloads_dir() == expected
    assert (
        ExportStateCoordinator(None, None, None).get_os_default_downloads() == expected
    )


def test_missing_redirected_directory_returns_str_without_raising(tmp_path, monkeypatch):
    target = tmp_path / "does" / "not" / "exist"
    monkeypatch.setattr(
        "PySide6.QtCore.QStandardPaths", _fake_standard_paths(str(target))
    )

    result = default_downloads_dir()

    assert isinstance(result, str)
    assert result.endswith("exist")


def test_home_downloads_location_keeps_existing_behaviour(monkeypatch):
    home_downloads = Path.home() / "Downloads"
    monkeypatch.setattr(
        "PySide6.QtCore.QStandardPaths", _fake_standard_paths(str(home_downloads))
    )

    expected = str(home_downloads.resolve())
    assert default_downloads_dir() == expected
    assert common_module.default_downloads_dir() == expected
    assert (
        ExportStateCoordinator(None, None, None).get_os_default_downloads() == expected
    )
