"""Serialize / restore image_compare viewport + canvas features for projects."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QTimer

from core.state_management.actions import (
    SetAutoCalculatePsnrAction,
    SetAutoCalculateSsimAction,
)
from core.store_viewport import RenderConfig, ViewState, ViewportState
from ui.canvas_infra.scene.property_access import (
    deserialize_canvas_feature_setting,
    read_canvas_feature_property,
    serialize_canvas_feature_setting,
    write_canvas_feature_property,
)
from ui.canvas_infra.scene.registry import get_canvas_registry
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


def serialize_view_state(view: ViewState) -> dict[str, Any]:
    return {
        "split_position": float(view.split_position),
        "split_position_visual": float(view.split_position_visual),
        "is_horizontal": bool(view.is_horizontal),
        "diff_mode": str(view.diff_mode or "off"),
        "channel_view_mode": str(view.channel_view_mode or "RGB"),
        "optimize_interactive_movement": bool(view.optimize_interactive_movement),
        "overlay_enabled": bool(view.overlay_enabled),
        "showing_single_image_mode": int(view.showing_single_image_mode),
        "movement_speed_per_sec": float(view.movement_speed_per_sec),
    }


def restore_view_state(view: ViewState, data: dict[str, Any] | None) -> None:
    if not data:
        return
    if "split_position" in data:
        view.split_position = float(data["split_position"])
    if "split_position_visual" in data:
        view.split_position_visual = float(data["split_position_visual"])
    elif "split_position" in data:
        view.split_position_visual = float(data["split_position"])
    if "is_horizontal" in data:
        view.is_horizontal = bool(data["is_horizontal"])
    if "diff_mode" in data and data["diff_mode"] is not None:
        view.diff_mode = str(data["diff_mode"])
    if "channel_view_mode" in data and data["channel_view_mode"] is not None:
        view.channel_view_mode = str(data["channel_view_mode"])
    # Pinned ON (see settings_persistence): the toggle is gone from the panel and
    # a restored session must not resurrect a hidden False.
    view.optimize_interactive_movement = True
    if "overlay_enabled" in data:
        view.overlay_enabled = bool(data["overlay_enabled"])
    if "showing_single_image_mode" in data:
        view.showing_single_image_mode = int(data["showing_single_image_mode"])
    if "movement_speed_per_sec" in data:
        view.movement_speed_per_sec = float(data["movement_speed_per_sec"])


def serialize_feature_settings(
    viewport: ViewportState, session_type: str = "image_compare"
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for prop in get_canvas_registry(session_type).get_feature_properties():
        if not prop.setting_key:
            continue
        channels = read_canvas_feature_property(viewport, prop)
        out[prop.setting_key] = serialize_canvas_feature_setting(prop, channels)
    return out


def restore_feature_settings(
    viewport: ViewportState,
    data: dict[str, Any] | None,
    session_type: str = "image_compare",
) -> None:
    if not data:
        return
    by_key = {
        prop.setting_key: prop
        for prop in get_canvas_registry(session_type).get_feature_properties()
        if prop.setting_key
    }
    for key, raw in data.items():
        prop = by_key.get(key)
        if prop is None:
            continue
        channels = deserialize_canvas_feature_setting(prop, raw)
        write_canvas_feature_property(viewport, prop, channels)


def serialize_image_state_prefs(image_state: Any) -> dict[str, Any]:
    if image_state is None:
        return {}
    return {
        "auto_calculate_psnr": bool(getattr(image_state, "auto_calculate_psnr", False)),
        "auto_calculate_ssim": bool(getattr(image_state, "auto_calculate_ssim", False)),
    }


def _get_dispatcher(store: Any | None):
    if store is None:
        return None
    getter = getattr(store, "get_dispatcher", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def restore_image_state_prefs(
    image_state: Any, data: dict[str, Any] | None, store: Any | None = None
) -> None:
    if image_state is None or not data:
        return
    has_psnr = "auto_calculate_psnr" in data
    has_ssim = "auto_calculate_ssim" in data
    if not has_psnr and not has_ssim:
        return
    dispatcher = _get_dispatcher(store)
    if dispatcher is not None:
        try:
            batch = getattr(store, "batch_changes", None)
            if callable(batch) and has_psnr and has_ssim:
                with store.batch_changes():
                    if has_psnr:
                        dispatcher.dispatch(
                            SetAutoCalculatePsnrAction(enabled=bool(data["auto_calculate_psnr"])),
                            scope="viewport",
                        )
                    if has_ssim:
                        dispatcher.dispatch(
                            SetAutoCalculateSsimAction(enabled=bool(data["auto_calculate_ssim"])),
                            scope="viewport",
                        )
            else:
                if has_psnr:
                    dispatcher.dispatch(
                        SetAutoCalculatePsnrAction(enabled=bool(data["auto_calculate_psnr"])),
                        scope="viewport",
                    )
                if has_ssim:
                    dispatcher.dispatch(
                        SetAutoCalculateSsimAction(enabled=bool(data["auto_calculate_ssim"])),
                        scope="viewport",
                    )
        except Exception:
            pass
        return
    if store is not None:
        # Early bootstrap – dispatcher not yet bound (src/core/bootstrap.py:176), defer.
        try:
            QTimer.singleShot(
                0, lambda: restore_image_state_prefs(image_state, data, store)
            )
        except Exception:
            pass
        return
    # store is None: no dispatcher to project through and nothing live to
    # defer against. The only production caller
    # (use_cases/persistence.deserialize_session) early-returns when
    # context.store is None, so this branch is reachable only from transient
    # test builders — which must supply a fake dispatcher store instead.
    # Deliberately no setattr fallback here: silent mutation of possibly
    # Store-attached objects routed around the no-direct-mutation AST dogma
    # (which scans Assign, not setattr) and hid stale-state bugs.
    return


def _serialize_magnifier(view_state: ViewState) -> dict[str, Any]:
    cmd = get_canvas_registry("image_compare").get_feature_command_by_alias(
        "project.serialize_magnifier"
    )
    if cmd is None:
        return {}
    return dict(cmd(view_state) or {})


def _restore_magnifier(view_state: ViewState, data: dict[str, Any] | None) -> None:
    if not data:
        return
    cmd = get_canvas_registry("image_compare").get_feature_command_by_alias(
        "project.restore_magnifier"
    )
    if cmd is None:
        return
    cmd(view_state, data)


def serialize_viewport_block(viewport: ViewportState | None) -> dict[str, Any]:
    if viewport is None:
        return {}
    return {
        "view_state": serialize_view_state(viewport.view_state),
        "render_config": viewport.render_config.to_dict(),
        "feature_settings": serialize_feature_settings(viewport),
        "magnifier": _serialize_magnifier(viewport.view_state),
        "image_state": serialize_image_state_prefs(
            getattr(viewport.session_data, "image_state", None)
        ),
    }


def _resolve_restore_presenter(store: Any | None) -> Any | None:
    """Best-effort presenter lookup for post-restore toolbar sync."""
    if store is None:
        return None
    for attr in ("presenter", "toolbar_presenter"):
        try:
            candidate = getattr(store, attr, None)
        except Exception:
            candidate = None
        if candidate is not None:
            return candidate
    try:
        window = getattr(store, "main_window", None)
    except Exception:
        window = None
    if window is not None:
        for attr in ("presenter", "toolbar_presenter"):
            try:
                candidate = getattr(window, attr, None)
            except Exception:
                candidate = None
            if candidate is not None:
                return candidate
    return None


def refresh_filename_overlay_toolbar(
    store: Any | None, presenter: Any | None = None
) -> bool:
    """Bring the filename-overlay toolbar control in sync with the Store.

    Reads ``store.viewport.render_config.include_file_names_in_saved`` and
    pushes it into the toolbar button via the EXISTING sync mechanism
    (``presenters.toolbar.state.update_toolbar_states``, which itself fans
    out to the canvas feature binding sync) — never a new sync path and
    never a direct feature import (canvas-features import dogma).
    ``presenter`` is optional and derived from ``store`` where possible.
    Best-effort and never raising, but never silent either: returns True
    when the button was driven from the Store, False on any miss (no
    presenter reachable, sync unavailable, sync failed) with a warning log
    so an early-bootstrap restore miss is visible instead of a stale button.
    """
    try:
        if presenter is None:
            presenter = _resolve_restore_presenter(store)
        if presenter is None:
            logger.warning(
                "refresh_filename_overlay_toolbar: no presenter reachable, "
                "button not synced with the Store"
            )
            return False
        try:
            from tabs.image_compare.presenters.toolbar.state import (
                update_toolbar_states as _update_toolbar_states,
            )
        except Exception:
            logger.warning(
                "refresh_filename_overlay_toolbar: toolbar sync unavailable",
                exc_info=True,
            )
            return False
        try:
            _update_toolbar_states(presenter)
        except Exception:
            logger.warning(
                "refresh_filename_overlay_toolbar: toolbar sync failed",
                exc_info=True,
            )
            return False
        return True
    except Exception:
        logger.warning(
            "refresh_filename_overlay_toolbar: unexpected failure",
            exc_info=True,
        )
        return False


def _restore_render_config_and_sync(
    viewport: ViewportState, restored: RenderConfig, store: Any | None
) -> None:
    """QTimer-deferred completion for ``_restore_render_config``.

    Retries the dispatch restore once the dispatcher is bound, then re-syncs
    the filename-overlay toolbar control with the landed Store value.
    """
    try:
        _restore_render_config(viewport, restored, store)
    except Exception:
        pass
    refresh_filename_overlay_toolbar(store)


def _restore_render_config(
    viewport: ViewportState, restored: RenderConfig, store: Any | None
) -> None:
    dispatcher = _get_dispatcher(store)
    if dispatcher is not None:
        try:
            from core.state_management.appearance_actions import (
                SetDrawTextBackgroundAction,
                SetFileNameBgColorAction,
                SetFileNameColorAction,
                SetFontSizePercentAction,
                SetFontWeightAction,
                SetIncludeFileNamesInSavedAction,
                SetInterpolationMethodAction,
                SetMaxNameLengthAction,
                SetMovementInterpolationMethodAction,
                SetTextAlphaPercentAction,
                SetTextPlacementModeAction,
            )
            from core.state_management.session_actions import SetZoomInterpolationMethodAction

            actions: list[Any] = []
            # Only dispatch where values differ to avoid noisy history
            cur = viewport.render_config
            if restored.interpolation_method != cur.interpolation_method:
                actions.append(SetInterpolationMethodAction(method=restored.interpolation_method))
            if (
                restored.interactive_movement_interpolation_method
                != cur.interactive_movement_interpolation_method
            ):
                # Interactive method has no core Action; project it through
                # the magnifier feature's own settings command via the
                # capability alias (canvas-features import dogma forbids
                # importing the feature's Action class here). The command
                # dispatches only when the value differs and also projects
                # movement onto the interactive value.
                try:
                    _move_cmd = get_canvas_registry(
                        "image_compare"
                    ).get_feature_command_by_alias(
                        "overlay.settings.set_movement_interpolation"
                    )
                except Exception:
                    _move_cmd = None
                if _move_cmd is not None:
                    try:
                        _move_cmd(
                            store,
                            restored.interactive_movement_interpolation_method,
                        )
                    except Exception:
                        pass
            try:
                live_movement = store.viewport.render_config.movement_interpolation_method  # type: ignore[union-attr]
            except Exception:
                live_movement = cur.movement_interpolation_method
            movement_changed = (
                restored.movement_interpolation_method != live_movement
            )
            if movement_changed:
                actions.append(
                    SetMovementInterpolationMethodAction(method=restored.movement_interpolation_method)
                )
            if restored.zoom_interpolation_method != cur.zoom_interpolation_method:
                actions.append(
                    SetZoomInterpolationMethodAction(method=restored.zoom_interpolation_method)
                )
            if restored.include_file_names_in_saved != cur.include_file_names_in_saved:
                actions.append(
                    SetIncludeFileNamesInSavedAction(enabled=restored.include_file_names_in_saved)
                )
            if restored.font_size_percent != cur.font_size_percent:
                actions.append(SetFontSizePercentAction(size=restored.font_size_percent))
            if restored.font_weight != cur.font_weight:
                actions.append(SetFontWeightAction(weight=restored.font_weight))
            if restored.text_alpha_percent != cur.text_alpha_percent:
                actions.append(SetTextAlphaPercentAction(alpha=restored.text_alpha_percent))
            if restored.file_name_color != cur.file_name_color:
                actions.append(SetFileNameColorAction(color=restored.file_name_color))
            if restored.file_name_bg_color != cur.file_name_bg_color:
                actions.append(SetFileNameBgColorAction(color=restored.file_name_bg_color))
            if restored.draw_text_background != cur.draw_text_background:
                actions.append(SetDrawTextBackgroundAction(enabled=restored.draw_text_background))
            if restored.text_placement_mode != cur.text_placement_mode:
                actions.append(SetTextPlacementModeAction(mode=restored.text_placement_mode))
            if restored.max_name_length != cur.max_name_length:
                actions.append(SetMaxNameLengthAction(length=restored.max_name_length))
            # NOTE: jpeg_quality has no dedicated Action/reducer (verified: it
            # is referenced only by RenderConfig defaults + to_dict/from_dict),
            # so it cannot be projected through Dispatcher. It stays at the
            # live value on attached stores — never bare setattr here — until
            # a SetJpegQuality-style action lands. It is still serialized, so
            # the value round-trips once such an action exists.
            batch = getattr(store, "batch_changes", None) if store is not None else None
            if actions:
                if callable(batch):
                    with store.batch_changes():  # type: ignore[union-attr]
                        for act in actions:
                            dispatcher.dispatch(act, scope="viewport")
                else:
                    for act in actions:
                        dispatcher.dispatch(act, scope="viewport")
            # Sync the toolbar button with the landed Store value (no-op when
            # no presenter is reachable from the store).
            refresh_filename_overlay_toolbar(store)
            return
        except Exception:
            logger.warning(
                "_restore_render_config: dispatch restore failed, Store keeps "
                "live values",
                exc_info=True,
            )
    if store is not None:
        # Dispatcher not yet bound — defer the whole restore; the deferred
        # completion retries the dispatch above and refreshes the toolbar.
        try:
            QTimer.singleShot(
                0, lambda: _restore_render_config_and_sync(viewport, restored, store)
            )
            return
        except Exception:
            pass
        # Deferral unavailable (no event loop): return without mutating. The
        # old setattr(viewport, "render_config", ...) fallback is gone — bare
        # mutation of a possibly-live viewport hid toolbar/store desyncs.
        return
    # store is None — see restore_image_state_prefs: transient builders only,
    # which must supply a fake dispatcher store. No setattr fallback.
    return


def restore_viewport_block(
    viewport: ViewportState | None,
    data: dict[str, Any] | None,
    store: Any | None = None,
) -> None:
    if viewport is None or not data:
        return
    restore_view_state(viewport.view_state, data.get("view_state"))
    if data.get("render_config"):
        restored = RenderConfig.from_dict(data.get("render_config"))
        _restore_render_config(viewport, restored, store)
    # Magnifier models first so feature property writes can target active state.
    _restore_magnifier(viewport.view_state, data.get("magnifier"))
    restore_feature_settings(viewport, data.get("feature_settings"))
    restore_image_state_prefs(
        getattr(viewport.session_data, "image_state", None),
        data.get("image_state"),
        store,
    )
