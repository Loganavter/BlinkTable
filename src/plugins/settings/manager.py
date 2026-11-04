# Audit-Meta: pattern=state-machine reason="single settings manager — load/save + change propagation"
import logging
import os
import re
import shutil
import tempfile
from functools import wraps
from typing import Any

from PySide6.QtCore import QSettings, QStandardPaths, QTimer
from PySide6.QtGui import QColor

from core.constants import AppConstants
from core.store import Store
from domain.qt_adapters import hex_to_color, color_to_hex
from shared.video_policy import CrfPolicy, FpsPolicy, clamp_crf, clamp_fps
from ui.canvas_infra.scene.property_access import (
    deserialize_canvas_feature_setting,
    read_canvas_feature_property,
    write_canvas_feature_property,
    serialize_canvas_feature_setting,
)
from ui.canvas_infra.scene.registry import get_canvas_registry
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

# Suffix of the last-good copy kept beside a file-backed settings store. Must
# stay ".backup": copies with this name already exist on users' disks and the
# restore path is expected to heal from them.
_FILE_BACKUP_SUFFIX = ".backup"
# Portable export used when the live store is NOT a file (Windows registry,
# any native backend without a filesystem path) — a file beside a registry key
# is impossible, so the last-good copy lives in the app config dir instead.
_PORTABLE_BACKUP_FILENAME = "settings-backup.ini"

# Registry key roots QSettings reports from ``fileName()`` on Windows
# ("HKCU\\Software\\org\\app"). Matched on the upper-cased text.
_REGISTRY_KEY_PREFIXES = (
    "HKCU\\",
    "HKLM\\",
    "HKCR\\",
    "HKU\\",
    "HKCC\\",
    "HKEY_",
)
# Windows filesystem roots: "C:\..." / "\\server\share\..." — backslashes
# there are path separators, not registry nesting.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

# One warning per process is enough to make an unavailable backup visible
# without spamming every save (the old code logged at debug and stayed silent).
_backup_warning_emitted = False


def is_file_backed_settings(path: str) -> bool:
    """True when ``path`` is a filesystem path a ``.backup`` can live beside.

    Pure (no Qt): ``QSettings.fileName()`` is either a real path or, on the
    native Windows backend, a registry key such as
    ``HKCU\\Software\\BlinkTable\\BlinkTable``. Copying "beside" a
    registry key silently does nothing — ``os.path.exists`` is always False —
    so the backup/self-heal path has to branch on this instead of trying.
    """
    if not isinstance(path, str):
        return False
    text = path.strip()
    if not text:
        return False
    if text.upper().startswith(_REGISTRY_KEY_PREFIXES):
        return False
    if _WINDOWS_DRIVE_RE.match(text) or text.startswith("\\\\"):
        return True
    # Registry keys are backslash-separated with no path separator. Real
    # posix paths (and the relative names QSettings reports) contain "/" or
    # nothing at all.
    if "\\" in text and "/" not in text:
        return False
    return True


def _portable_backup_dir(settings: QSettings) -> str:
    """Stable per-app config directory for the portable export, or "".

    Derived from the store's own org/app so the export cannot drift into a
    second app identity (see ``canonical_settings`` — one org/app pair).
    """
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.GenericConfigLocation
    )
    if not base:
        return ""
    parts = [base]
    for part in (settings.organizationName(), settings.applicationName()):
        text = str(part or "").strip()
        if text:
            parts.append(text)
    return os.path.join(*parts)


def settings_backup_path(settings: QSettings) -> str:
    """The ONE resolver of where the last-good settings copy lives.

    File-backed store: ``<fileName>.backup`` beside the settings file (same
    name as before, so existing last-good copies keep working). Non-file store
    (Windows registry): ``<app-config-dir>/settings-backup.ini``. Returns ""
    when a non-file store has no writable config directory (callers then warn
    once — never fail silently).
    """
    file_name = str(settings.fileName() or "")
    if is_file_backed_settings(file_name):
        return file_name + _FILE_BACKUP_SUFFIX
    directory = _portable_backup_dir(settings)
    if not directory:
        return ""
    return os.path.join(directory, _PORTABLE_BACKUP_FILENAME)


def _warn_backup_unavailable(reason: str) -> None:
    """Report a genuinely unavailable backup once per process."""
    global _backup_warning_emitted
    if _backup_warning_emitted:
        return
    _backup_warning_emitted = True
    logger.warning(
        "Settings backup/self-heal is unavailable (%s); a corrupt settings "
        "store cannot be restored on this platform.",
        reason,
    )


class _SettingsReadSession:
    """One-pass read view over the store for ``load_all_settings``.

    ``contains(key)`` + ``value(key)`` per setting is two backend reads for
    every key (102 for the ~51 startup keys — each one a QSettings lookup).
    One ``allKeys()`` pass answers membership for free; ``value()`` is then
    paid once per key that exists and memoised for the rest of the pass.

    Semantics are unchanged: a relative key the store does not hold is
    "absent" exactly as ``QSettings.contains`` reported it, and a present
    value is returned untouched (defaults still apply only to absent keys).
    """

    __slots__ = ("_settings", "_keys", "_values")

    def __init__(self, settings: QSettings):
        self._settings = settings
        self._keys = frozenset(settings.allKeys())
        self._values: dict[str, Any] = {}

    def __len__(self) -> int:
        return len(self._keys)

    def contains(self, key: str) -> bool:
        return key in self._keys

    def value(self, key: str, default: Any = None) -> Any:
        if key not in self._keys:
            return default
        if key in self._values:
            return self._values[key]
        val = self._settings.value(key)
        self._values[key] = val
        return val


def _settings_read_pass(method):
    """Install a fresh ``_SettingsReadSession`` for one load-only pass.

    Kept as a decorator so the load body stays one flat sequence of
    ``_get_setting`` calls (the settings-persistence contract scans THAT
    function for the typed load pairs). The previous session is restored in
    ``finally`` — a raising pass must never leave a stale key snapshot behind.
    """

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        previous = self._read_session
        self._read_session = _SettingsReadSession(self.settings)
        try:
            return method(self, *args, **kwargs)
        finally:
            self._read_session = previous

    return wrapper


def _refuse_test_write_to_live_config(settings) -> None:
    """A test run must never rewrite the user's real settings file.

    Measured 2026-10-26: the settings/gallery suites ran with the host XDG
    (the test sandbox redirected ``XDG_DATA_HOME``/``APPDATA`` but not
    ``XDG_CONFIG_HOME``), and one of them deleted or replaced
    ``~/.config/BlinkTable/BlinkTable.conf``. The owner's ``[image_gallery_ai]``
    group vanished while the API key stayed in the keyring — the app then
    reported "configure the API key" with the key present. The sandbox now
    redirects ``XDG_CONFIG_HOME`` too; this guard makes an un-sandboxed run fail
    loudly instead of eating a real profile.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        path = str(settings.fileName() or "")
    except Exception:
        return
    # Only the REAL user config tree is dangerous: a temp dir (a fixture, a fake
    # store, a portable export) is fair game.
    real_root = os.path.abspath(os.path.expanduser("~/.config")) + os.sep
    if not path or not os.path.abspath(path).startswith(real_root):
        return
    root = os.environ.get("XDG_CONFIG_HOME", "")
    if root and os.path.abspath(path).startswith(os.path.abspath(root) + os.sep):
        return
    raise RuntimeError(
        "refusing to write the real settings file from a test run: "
        f"{path or '<unknown>'} is outside XDG_CONFIG_HOME={root or '<unset>'}; "
        "the secret-store sandbox fixture sets it — run the suite through the "
        "root/gallery conftest, or point XDG_CONFIG_HOME at a temp dir"
    )


class SettingsManager:
    def __init__(self, organization_name, application_name):
        # The persisted identity moved (pre-rename pair -> BlinkTable): run the
        # one-way copy before this store is opened, so a manager built directly
        # with the canonical pair can never read an empty new namespace. Cached
        # per process — a dict check, and a no-op if a factory already ran it.
        from core.settings_migration import migrate_settings_once

        migrate_settings_once()
        self._organization_name = organization_name
        self._application_name = application_name
        self.settings = QSettings(organization_name, application_name)
        # Active only while a load-only pass runs (see ``_settings_read_pass``).
        self._read_session: _SettingsReadSession | None = None
        self._backup_path = settings_backup_path(self.settings)
        logger.debug(
            "[settings] SettingsManager init file=%s backup=%s",
            self.settings.fileName(),
            self._backup_path,
        )
        if self._restore_backup_if_corrupt():
            # The QSettings instance cached the corrupt state; re-read the
            # restored file with a fresh instance.
            self.settings = QSettings(organization_name, application_name)
            self._backup_path = settings_backup_path(self.settings)

    def _caller_summary(self) -> str:
        """Last 3 non-manager call frames, for load/save origin tracing.

        Settings resets are reported as "random" — the value of this log is
        catching *which* code path triggered a load/save, not just that one
        happened.
        """
        try:
            import traceback

            here = os.path.abspath(__file__)
            # extract_stack is ordered outermost-first; keep the nearest 3
            # non-manager frames and print nearest-first so the log reads
            # "who called us" -> "who called them".
            frames = [
                frame
                for frame in traceback.extract_stack()[:-1]
                if os.path.abspath(frame.filename) != here
            ][-3:]
            return " <- ".join(
                f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}"
                for frame in reversed(frames)
            )
        except Exception:
            return "unknown"

    def _store_is_present(self) -> bool:
        """Whether the live store holds anything at all.

        File-backed: the file exists (a fresh profile has none). Non-file
        (registry): presence is ``allKeys()`` — a registry key exists or it
        does not, and ``os.path.exists("HKCU\\...")`` is always False, which
        is exactly what made the health check dead code on Windows.

        A *fully* wiped registry store (every value gone — what a crash
        mid-write leaves behind) has no keys at all, so key presence alone
        would call it a fresh install and skip the self-heal precisely when it
        is needed. A store therefore also counts as present when a last-good
        copy exists to heal from; a genuinely fresh profile (no keys, no
        backup) stays silent instead of warning about corruption.
        """
        file_name = str(self.settings.fileName() or "")
        if is_file_backed_settings(file_name):
            return os.path.exists(file_name)
        if self.settings.allKeys():
            return True
        backup_path = settings_backup_path(self.settings)
        return bool(backup_path) and os.path.exists(backup_path)

    def _restore_backup_if_corrupt(self) -> bool:
        """Heal a truncated/corrupt settings store from the last-good backup.

        QSettings writes its INI in place (open-truncate-write-close), so a
        kill/crash mid-save leaves a truncated file. The next startup then
        loads defaults and a clean exit re-writes them — permanently wiping
        the user's settings (observed: language and UI prefs reset to
        defaults after dev iterations that kill the app). A startup that
        finds the store present but missing the critical keys restores the
        last-good copy instead: the ``.backup`` file beside an INI, or the
        portable INI export for a non-file (registry) store.
        """
        if not self._store_is_present():
            return False
        file_name = str(self.settings.fileName() or "")
        backup_path = settings_backup_path(self.settings)
        self._backup_path = backup_path
        logger.debug(
            "[settings] health check: store=%s keys=%d backup=%s",
            file_name,
            len(self.settings.allKeys()),
            backup_path or "<unavailable>",
        )
        if self.settings.contains("language") or self.settings.contains("theme"):
            return False
        if not backup_path:
            _warn_backup_unavailable("no writable app-config directory")
            return False
        if not os.path.exists(backup_path):
            logger.warning(
                "Settings store looks empty/corrupt but no backup exists to "
                "restore from: %s",
                backup_path,
            )
            return False
        if is_file_backed_settings(file_name):
            try:
                shutil.copy2(backup_path, file_name)
                logger.warning(
                    "Settings file was corrupt/truncated; restored from backup: %s",
                    file_name,
                )
                return True
            except Exception:
                logger.exception(
                    "Failed to restore settings from backup %s", backup_path
                )
                return False
        return self._restore_from_portable_backup(backup_path)

    def _restore_from_portable_backup(self, backup_path: str) -> bool:
        """Copy every leaf of a portable INI export back into the live store.

        Fail-open: an unreadable/empty export (a truncated backup, a garbage
        file) is ignored with one log line. Keys are only ever written, never
        cleared, so a bad export cannot wipe whatever the live store still
        holds.
        """
        try:
            export = QSettings(backup_path, QSettings.Format.IniFormat)
            keys = export.allKeys()
            if not keys:
                logger.warning(
                    "Portable settings backup is empty/unreadable; ignored: %s",
                    backup_path,
                )
                return False
            if not (export.contains("language") or export.contains("theme")):
                logger.warning(
                    "Portable settings backup has no critical keys; ignored: %s",
                    backup_path,
                )
                return False
            for key in keys:
                self.settings.setValue(key, export.value(key))
            self.settings.sync()
            logger.warning(
                "Settings store was corrupt/truncated; restored %d keys from "
                "portable backup: %s",
                len(keys),
                backup_path,
            )
            return True
        except Exception:
            logger.exception(
                "Failed to restore settings from portable backup %s", backup_path
            )
            return False

    def _settings_contains(self, key: str) -> bool:
        """Membership read: the active load pass answers it from its snapshot."""
        session = self._read_session
        if session is not None:
            return session.contains(key)
        return self.settings.contains(key)

    def _settings_value(self, key: str, default: Any = None) -> Any:
        """Value read with a default: snapshot when a load pass is active."""
        session = self._read_session
        if session is not None:
            return session.value(key, default)
        if not self.settings.contains(key):
            return default
        return self.settings.value(key)

    def _settings_key_count(self) -> int:
        """Number of keys the store holds, without a second ``allKeys()``."""
        session = self._read_session
        if session is not None:
            return len(session)
        return len(self.settings.allKeys())

    def _get_setting(self, key, default, target_type):
        if not self._settings_contains(key):
            return default
        val = self._settings_value(key)
        try:
            if target_type == bool:
                return str(val).lower() == "true"
            if target_type == int:
                return int(float(val))
            if target_type == float:
                return float(val)
            return target_type(val)
        except (TypeError, ValueError):
            logger.warning(
                "Setting %r has unusable value %r; falling back to default",
                key,
                val,
            )
            return default

    @_settings_read_pass
    def load_all_settings(self, store: Store):
        v, s = store.viewport, store.settings
        render = v.render_config
        view = v.view_state
        logger.debug(
            "[settings] load_all_settings START caller=%s file=%s keys_in_file=%d",
            self._caller_summary(),
            self.settings.fileName(),
            self._settings_key_count(),
        )

        render.max_name_length = self._get_setting("max_name_length", 50, int)

        view.movement_speed_per_sec = self._get_setting(
            "movement_speed_per_sec", 2.0, float
        )

        s.theme = self._get_setting("theme", "auto", str)
        s.current_language = self._get_setting("language", "en", str)
        s.ui_mode = self._get_setting("ui_mode", "beginner", str)
        s.ui_scale_factor = self._get_setting("ui_scale_factor", 1.0, float)
        s.ui_font_mode = self._get_setting("ui_font_mode", "builtin", str)
        s.ui_font_family = self._get_setting("ui_font_family", "", str)
        s.debug_mode_enabled = self._get_setting("debug_mode_enabled", False, bool)
        s.system_notifications_enabled = self._get_setting(
            "system_notifications_enabled", True, bool
        )
        s.auto_crop_black_borders = self._get_setting(
            "auto_crop_black_borders", True, bool
        )
        s.video_recording_fps = clamp_fps(
            self._get_setting("video_recording_fps", FpsPolicy.DEFAULT, int)
        )
        s.video_editor_preview_render_scale = self._get_setting(
            "video_editor_preview_render_scale", 1.0, float
        )
        s.rhi_backend = self._get_setting("rhi_backend", "default", str)
        s.keyboard_overrides = self._load_keyboard_overrides()

        render.font_size_percent = self._get_setting("font_size_percent", 120, int)
        render.font_weight = self._get_setting("font_weight", 0, int)
        render.text_alpha_percent = self._get_setting("text_alpha_percent", 100, int)
        render.file_name_color = hex_to_color(
            self._get_setting("filename_color", "#FFFF0000", str)
        )
        render.file_name_bg_color = hex_to_color(
            self._get_setting("filename_bg_color", "#FF000000", str)
        )
        render.draw_text_background = self._get_setting("draw_text_background", True, bool)
        render.text_placement_mode = self._get_setting("text_placement_mode", "edges", str)
        render.include_file_names_in_saved = self._get_setting(
            "include_file_names_in_saved", False, bool
        )

        main_interp = self._get_setting("interpolation_method", AppConstants.DEFAULT_INTERPOLATION_METHOD, str)
        render.interpolation_method = main_interp
        render.zoom_interpolation_method = self._get_setting(
            "zoom_interpolation_method",
            AppConstants.DEFAULT_INTERPOLATION_METHOD,
            str,
        )
        logger.debug(
            "SettingsManager.load_all_settings interpolation main=%s zoom=%s",
            main_interp,
            render.zoom_interpolation_method,
        )

        self._load_canvas_feature_settings(v)
        self._load_tab_canvas_feature_settings(store)

        s.window_width = self._get_setting("window_width", 1024, int)
        s.window_height = self._get_setting("window_height", 768, int)
        s.window_x = self._get_setting("window_x", 100, int)
        s.window_y = self._get_setting("window_y", 100, int)
        s.window_was_maximized = self._get_setting("window_was_maximized", False, bool)

        s.export_use_default_dir = self._get_setting(
            "export_use_default_dir", True, bool
        )
        s.export_default_dir = self._get_setting("export_default_dir", None, str)
        s.export_favorite_dir = self._get_setting("export_favorite_dir", None, str)
        s.export_last_format = self._get_setting("export_last_format", "PNG", str)
        s.export_quality = self._get_setting("export_quality", 95, int)
        s.export_fill_background = self._get_setting(
            "export_fill_background", False, bool
        )
        s.export_background_color = hex_to_color(
            self._get_setting("export_background_color", "#FFFFFFFF", str)
        )
        s.export_last_filename = self._get_setting("export_last_filename", "", str)
        s.export_png_compress_level = self._get_setting(
            "export_png_compress_level", 9, int
        )
        s.export_comment_text = self._get_setting("export_comment_text", "", str)
        s.export_comment_keep_default = self._get_setting(
            "export_comment_keep_default", False, bool
        )
        s.export_video_favorite_dir = self._get_setting(
            "export_video_favorite_dir", None, str
        )
        s.export_video_container = self._get_setting("export_video_container", "mp4", str)
        s.export_video_codec = self._get_setting(
            "export_video_codec", "h264 (AVC)", str
        )
        s.export_video_quality_mode = self._get_setting(
            "export_video_quality_mode", "crf", str
        )
        s.export_video_crf = clamp_crf(
            self._get_setting("export_video_crf", CrfPolicy.DEFAULT, int)
        )
        s.export_video_bitrate = self._get_setting(
            "export_video_bitrate", "8000k", str
        )
        s.export_video_preset = self._get_setting(
            "export_video_preset", "medium", str
        )
        s.export_video_pix_fmt = self._get_setting(
            "export_video_pix_fmt", "yuv420p", str
        )
        s.export_video_manual_args = self._get_setting(
            "export_video_manual_args",
            "-c:v libx264 -crf 23 -pix_fmt yuv420p",
            str,
        )
        logger.debug(
            "[settings] load_all_settings DONE theme=%s language=%s ui_mode=%s "
            "ui_scale=%s window=%dx%d keys_in_file=%d",
            s.theme,
            s.current_language,
            s.ui_mode,
            s.ui_scale_factor,
            s.window_width,
            s.window_height,
            self._settings_key_count(),
        )

    def schedule_persist(self, store: Store) -> None:
        """Debounced full-snapshot save — the single writer for dialog-apply
        state (coalesced: repeated calls within the debounce window save once).

        The settings file must only ever be written as a coherent snapshot of
        the authoritative Store. Writing individual keys from UI state (the
        previous ``apply()`` behavior) allowed a stale dialog widget to
        silently overwrite good values with its defaults — the observed
        "random settings reset" (ui_mode/scale/rhi reverted). Callers that
        mutate the Store (dispatches) call this; the snapshot then reflects
        whatever the Store holds.
        """
        if getattr(self, "_persist_pending", False):
            return
        self._persist_pending = True

        def _run() -> None:
            self._persist_pending = False
            try:
                self.save_all_settings(store)
            except Exception:
                logger.exception("[settings] scheduled full persist failed")

        QTimer.singleShot(150, _run)

    def save_all_settings(self, store: Store):
        _refuse_test_write_to_live_config(self.settings)
        v, s = store.viewport, store.settings
        render = v.render_config
        view = v.view_state
        logger.debug(
            "[settings] save_all_settings START caller=%s file=%s "
            "store_digest=theme=%s language=%s ui_mode=%s ui_scale=%s "
            "window=%dx%d rhi_backend=%s",
            self._caller_summary(),
            self.settings.fileName(),
            s.theme,
            s.current_language,
            s.ui_mode,
            s.ui_scale_factor,
            s.window_width,
            s.window_height,
            s.rhi_backend,
        )
        self._save_setting("max_name_length", render.max_name_length)

        self._save_tab_canvas_feature_settings(store)

        self._save_setting("movement_speed_per_sec", view.movement_speed_per_sec)

        self._save_setting("theme", s.theme)
        self._save_setting("language", s.current_language)
        self._save_setting("ui_mode", s.ui_mode)
        self._save_setting("ui_scale_factor", s.ui_scale_factor)
        self._save_setting("ui_font_mode", s.ui_font_mode)
        self._save_setting("ui_font_family", s.ui_font_family)
        self._save_setting("debug_mode_enabled", s.debug_mode_enabled)
        self._save_setting(
            "system_notifications_enabled", s.system_notifications_enabled
        )
        self._save_setting("auto_crop_black_borders", s.auto_crop_black_borders)
        self._save_setting("video_recording_fps", clamp_fps(s.video_recording_fps))
        self._save_setting(
            "video_editor_preview_render_scale",
            s.video_editor_preview_render_scale,
        )
        self._save_setting("rhi_backend", s.rhi_backend)
        self._save_keyboard_overrides(s.keyboard_overrides)

        self._save_setting("font_size_percent", render.font_size_percent)
        self._save_setting("font_weight", render.font_weight)
        self._save_setting("text_alpha_percent", render.text_alpha_percent)
        self._save_setting("filename_color", color_to_hex(render.file_name_color))
        self._save_setting("filename_bg_color", color_to_hex(render.file_name_bg_color))
        self._save_setting("draw_text_background", render.draw_text_background)
        self._save_setting("text_placement_mode", render.text_placement_mode)
        self._save_setting("include_file_names_in_saved", render.include_file_names_in_saved)

        self._save_setting("interpolation_method", render.interpolation_method)
        self._save_setting(
            "zoom_interpolation_method", render.zoom_interpolation_method
        )

        self._save_canvas_feature_settings(v)

        self._save_setting("window_width", s.window_width)
        self._save_setting("window_height", s.window_height)
        self._save_setting("window_x", s.window_x)
        self._save_setting("window_y", s.window_y)
        self._save_setting("window_was_maximized", s.window_was_maximized)
        self._save_setting("export_use_default_dir", s.export_use_default_dir)
        self._save_setting("export_default_dir", s.export_default_dir)
        self._save_setting("export_favorite_dir", s.export_favorite_dir)
        self._save_setting("export_last_format", s.export_last_format)
        self._save_setting("export_quality", s.export_quality)
        self._save_setting("export_fill_background", s.export_fill_background)
        self._save_setting(
            "export_background_color", color_to_hex(s.export_background_color)
        )
        self._save_setting("export_last_filename", s.export_last_filename)
        self._save_setting("export_png_compress_level", s.export_png_compress_level)
        self._save_setting("export_comment_text", s.export_comment_text)
        self._save_setting(
            "export_comment_keep_default", s.export_comment_keep_default
        )
        self._save_setting("export_video_favorite_dir", s.export_video_favorite_dir)
        self._save_setting("export_video_container", s.export_video_container)
        self._save_setting("export_video_codec", s.export_video_codec)
        self._save_setting("export_video_quality_mode", s.export_video_quality_mode)
        self._save_setting("export_video_crf", clamp_crf(s.export_video_crf))
        self._save_setting("export_video_bitrate", s.export_video_bitrate)
        self._save_setting("export_video_preset", s.export_video_preset)
        self._save_setting("export_video_pix_fmt", s.export_video_pix_fmt)
        self._save_setting("export_video_manual_args", s.export_video_manual_args)

        self.settings.sync()
        self._refresh_backup()
        logger.debug(
            "[settings] save_all_settings DONE keys_in_file=%d backup=%s",
            len(self.settings.allKeys()),
            self._backup_path,
        )

    def _refresh_backup(self) -> None:
        """Keep a last-good copy of the settings store.

        Refreshed after every successful save so the startup self-heal
        (``_restore_backup_if_corrupt``) restores recent settings rather
        than stale ones. File-backed stores copy the file beside it;
        non-file stores (Windows registry) export every key into a portable
        INI, written atomically (mkstemp -> close -> os.replace) so a crash
        mid-export can never leave a half-written last-good copy.
        """
        backup_path = settings_backup_path(self.settings)
        self._backup_path = backup_path
        if not backup_path:
            _warn_backup_unavailable("no writable app-config directory")
            return
        try:
            file_name = str(self.settings.fileName() or "")
            if is_file_backed_settings(file_name):
                if os.path.exists(file_name):
                    shutil.copy2(file_name, backup_path)
                    logger.debug("[settings] backup refreshed: %s", backup_path)
                return
            self._write_portable_backup(backup_path)
        except Exception as exc:
            _warn_backup_unavailable(f"backup write failed: {exc}")
            logger.debug("Settings backup refresh failed", exc_info=True)

    def _write_portable_backup(self, backup_path: str) -> None:
        """Atomically export the live settings tree to an INI file."""
        directory = os.path.dirname(backup_path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".settings-backup.",
            suffix=".tmp",
            dir=directory,
        )
        os.close(fd)
        try:
            export = QSettings(tmp_name, QSettings.Format.IniFormat)
            for key in self.settings.allKeys():
                export.setValue(key, self.settings.value(key))
            export.sync()
            # QSettings has no close(); drop the handle before replacing so
            # the writer never sees an open file (matters on Windows).
            del export
            os.replace(tmp_name, backup_path)
            logger.debug("[settings] portable backup refreshed: %s", backup_path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _iter_all_canvas_feature_properties(self):
        from tabs.registry import TabRegistry

        tab_registry = TabRegistry()
        tab_registry.discover()
        seen_keys: set[str] = set()
        for tab_type in tab_registry.registered_types:
            for prop in get_canvas_registry(tab_type).get_feature_properties():
                if not prop.setting_key or prop.setting_key in seen_keys:
                    continue
                seen_keys.add(prop.setting_key)
                yield prop

    def _load_canvas_feature_settings(self, viewport_state) -> None:
        for prop in self._iter_all_canvas_feature_properties():
            if not self._settings_contains(prop.setting_key):
                continue
            raw_value = self._settings_value(prop.setting_key)
            channels = deserialize_canvas_feature_setting(prop, raw_value)
            write_canvas_feature_property(viewport_state, prop, channels)

    def _save_canvas_feature_settings(self, viewport_state) -> None:
        for prop in self._iter_all_canvas_feature_properties():
            channels = read_canvas_feature_property(viewport_state, prop)
            raw_value = serialize_canvas_feature_setting(prop, channels)
            self._save_setting(prop.setting_key, raw_value)

    def _load_tab_canvas_feature_settings(self, store: Store) -> None:
        from tabs.registry import TabRegistry

        registry = TabRegistry()
        registry.discover()
        registry.create_service(
            "settings_canvas_feature_load",
            store,
            self._get_setting,
        )

    def _save_tab_canvas_feature_settings(self, store: Store) -> None:
        from tabs.registry import TabRegistry

        registry = TabRegistry()
        registry.discover()
        registry.create_service(
            "settings_canvas_feature_save",
            store,
            self._save_setting,
        )

    def _load_keyboard_overrides(self) -> dict[str, str]:
        import json

        raw = self._settings_value("keyboard_overrides", "")
        if not raw:
            return {}
        try:
            if isinstance(raw, dict):
                data = raw
            else:
                data = json.loads(str(raw))
            if not isinstance(data, dict):
                return {}
            return {str(k): str(v) for k, v in data.items()}
        except Exception:
            return {}

    def _save_keyboard_overrides(self, overrides: dict[str, str]) -> None:
        import json

        self.settings.setValue("keyboard_overrides", json.dumps(dict(overrides or {})))

    def _save_setting(self, key, value):
        if value is None:
            logger.debug(
                "[settings] skip saving %s (None) caller=%s",
                key,
                self._caller_summary(),
            )
            return
        self.settings.setValue(key, value)

    def _profile_has_user_state(self) -> bool:
        """True when the settings file carries anything beyond startup bookkeeping.

        A missing ``is_first_run`` key is not proof of a fresh install: a
        ``.backup`` restore, a partial conf restore, and an upgrade from a
        version that predates the key all leave a long-used profile without
        it (the key is only ever written on onboarding completion). The
        surviving user settings are the evidence of a real past run. Startup
        bookkeeping keys are excluded (see the inline note below) because
        ``core.bootstrap`` writes ``last_seen_app_version`` on EVERY start —
        including the very first one, before onboarding can be shown.
        """
        # QSettings keys this app writes on its own during startup, before
        # the user can interact: ``last_seen_app_version`` is written on
        # every start, including the first one (see
        # ``core.bootstrap.ApplicationContext._maybe_flag_cache_purge_notice``).
        # They are bookkeeping, not evidence of a past run.
        bookkeeping = frozenset({"is_first_run", "last_seen_app_version"})
        return any(
            key not in bookkeeping
            for key in self.settings.allKeys()
        )

    def is_first_run(self) -> bool:
        """True only for a profile with no sign of prior use.

        An explicit ``is_first_run`` value always wins (False = completed,
        True = deliberately armed, e.g. tests). When the key is absent the
        profile is *not* automatically fresh: that is also the shape of a
        restored/partially restored/upgraded config that lost the flag.
        Treating it as first run re-armed the onboarding overlay on profiles
        that were long past first run (observed after a ``.backup`` restore
        and a profile copy taken before the first save). Absence therefore
        means first run only when the file holds nothing but startup
        bookkeeping — a genuinely empty profile.
        """
        if self.settings.contains("is_first_run"):
            val = self.settings.value("is_first_run")
            if isinstance(val, bool):
                return val
            text = str(val).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off"}:
                return False
            return True
        return not self._profile_has_user_state()

    def set_first_run_completed(self):
        self._save_setting("is_first_run", False)

    def last_seen_app_version(self) -> str:
        """Version string recorded the last time this profile ran, or ""
        if never recorded. Existing installs from before this tracking
        existed also read back "" here -- see
        ``core.bootstrap.ApplicationContext._maybe_flag_cache_purge_notice``,
        which uses that (combined with ``is_first_run()`` being False) to
        tell "genuinely fresh install" apart from "upgraded from an
        untracked version"."""
        return self._get_setting("last_seen_app_version", "", str)

    def set_last_seen_app_version(self, version: str) -> None:
        self._save_setting("last_seen_app_version", version)