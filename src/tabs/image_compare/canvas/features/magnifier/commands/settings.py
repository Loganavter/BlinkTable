from __future__ import annotations

from tabs.image_compare.canvas.features.magnifier.input.actions import (
    SetMagnifierMovementInterpolationMethodAction,
    SetOptimizeMagnifierMovementAction,
)
from tabs.image_compare.canvas.features.magnifier.state.feature_state import get_magnifier_widget_state


def settings_set_optimize_movement(store, enabled: bool) -> bool:
    if store is None or getattr(store, "viewport", None) is None:
        return False
    enabled = bool(enabled)
    if store.viewport.view_state.optimize_interactive_movement == enabled:
        return False
    store.get_dispatcher().dispatch(
        SetOptimizeMagnifierMovementAction(enabled),
        scope="viewport",
    )
    if hasattr(store, "invalidate_render_cache"):
        store.invalidate_render_cache()
    if hasattr(store, "emit_viewport_change"):
        store.emit_viewport_change()
    return True


def settings_set_movement_interpolation(store, method: str) -> bool:
    if store is None or getattr(store, "viewport", None) is None:
        return False
    method = str(method)
    current = store.viewport.render_config.interactive_movement_interpolation_method
    if current == method:
        return False
    store.get_dispatcher().dispatch(
        SetMagnifierMovementInterpolationMethodAction(method),
        scope="viewport",
    )
    if hasattr(store, "invalidate_render_cache"):
        store.invalidate_render_cache()
    if hasattr(store, "emit_viewport_change"):
        store.emit_viewport_change()
    return True


def settings_set_stroke(
    store, border_thickness=None, capture_thickness=None
) -> dict:
    """Set the loupe outline and detection-ring thickness on EVERY instance.

    The owner asked for both in the settings window (2026-11-04). The outline
    is a per-instance model field (``MagnifierModel.border_thickness``), so a
    window-level setting writes every live instance and the default new ones are
    created with; the detection ring has no per-instance field and is drawn from
    the canvas style, so its value lands on the feature state and the ring
    resolver reads it (``None`` keeps the style default).
    """
    if store is None or getattr(store, "viewport", None) is None:
        return {}
    state = get_magnifier_widget_state(store.viewport.view_state)
    changed: dict = {}
    border = None if border_thickness is None else max(0, int(border_thickness))
    capture = None if capture_thickness is None else max(0, int(capture_thickness))
    if border is not None and int(state.default_border_thickness) != border:
        state.default_border_thickness = border
        changed["border_thickness"] = border
    if capture is not None and state.default_capture_thickness != capture:
        state.default_capture_thickness = capture
        changed["capture_thickness"] = capture
    if border is not None:
        for model in state.models.values():
            if model is None:
                continue
            if int(getattr(model, "border_thickness", -1)) != border:
                model.border_thickness = border
    if changed:
        if hasattr(store, "invalidate_render_cache"):
            store.invalidate_render_cache()
        if hasattr(store, "emit_state_change"):
            store.emit_state_change()
    return changed


def settings_get_stroke(store) -> dict:
    """Current loupe outline / detection-ring thickness (capability alias).

    The settings dialog seeds its two rows through this: the UI layer may not
    import a canvas feature (``test_canvas_features_imports``), so it asks the
    registry for ``overlay.settings.get_stroke`` instead.
    """
    if store is None or getattr(store, "viewport", None) is None:
        return {}
    state = get_magnifier_widget_state(store.viewport.view_state)
    capture = getattr(state, "default_capture_thickness", None)
    return {
        "border_thickness": int(getattr(state, "default_border_thickness", 2)),
        "capture_thickness": 2 if capture is None else int(capture),
    }


def settings_apply_behavior(store, behavior: dict) -> dict:
    state = get_magnifier_widget_state(store.viewport.view_state)
    changes = {}
    if "intersection_highlight_enabled" in behavior:
        new_val = bool(behavior["intersection_highlight_enabled"])
        if new_val != state.intersection_highlight_enabled:
            state.intersection_highlight_enabled = new_val
            changes["intersection_highlight_enabled"] = new_val
    if "auto_color_new_instances" in behavior:
        new_val = bool(behavior["auto_color_new_instances"])
        if new_val != state.auto_color_new_instances:
            state.auto_color_new_instances = new_val
            changes["auto_color_new_instances"] = new_val
    return changes


def settings_initialize(store, get_setting) -> None:
    from tabs.image_compare.canvas.features.magnifier.constants import (
        DEFAULT_CAPTURE_POS_RELATIVE,
        DEFAULT_MAGNIFIER_OFFSET_RELATIVE,
        DEFAULT_MAGNIFIER_SPACING_RELATIVE,
    )
    from tabs.image_compare.canvas.features.magnifier.state.service import (
        MagnifierStoreService,
    )
    from tabs.image_compare.canvas.features.magnifier.state.store import (
        default_capture_size,
        default_magnifier_size,
        set_default_capture_size,
        set_default_magnifier_size,
    )

    view = store.viewport.view_state
    set_default_magnifier_size(view, get_setting("magnifier_size_relative", 0.4, float))
    set_default_capture_size(view, get_setting("capture_size_relative", 0.1, float))
    # Loupe outline + detection-ring thickness (settings window, 2026-11-04):
    # the values a session starts with, before any instance exists.
    state = get_magnifier_widget_state(view)
    state.default_border_thickness = max(
        0, int(get_setting("magnifier_border_thickness", 2, int))
    )
    stored_capture = get_setting("magnifier_capture_thickness", None, int)
    state.default_capture_thickness = (
        None if stored_capture is None else max(0, int(stored_capture))
    )
    initial_spacing = get_setting(
        "magnifier_spacing_relative", DEFAULT_MAGNIFIER_SPACING_RELATIVE, float
    )

    scene_state = MagnifierStoreService(store)
    default_magnifier = scene_state.ensure_active_magnifier(create_if_missing=False)
    if default_magnifier is not None:
        scene_state.move_object_source_position(
            default_magnifier.id, DEFAULT_CAPTURE_POS_RELATIVE
        )
        scene_state.set_active_magnifier_offset(DEFAULT_MAGNIFIER_OFFSET_RELATIVE)
        scene_state.set_active_magnifier_spacing(initial_spacing)
        scene_state.set_active_magnifier_size(default_magnifier_size(view))
        scene_state.set_active_capture_size(default_capture_size(view))


def settings_persist(store, save_setting) -> None:
    from tabs.image_compare.canvas.features.magnifier.constants import DEFAULT_MAGNIFIER_SPACING_RELATIVE
    from tabs.image_compare.canvas.features.magnifier.state.service import (
        MagnifierStoreService,
    )
    from tabs.image_compare.canvas.features.magnifier.state.store import (
        default_capture_size,
        default_magnifier_size,
    )

    view = store.viewport.view_state
    scene_state = MagnifierStoreService(store)
    active_magnifier = scene_state.get_active_or_first_magnifier()
    save_setting(
        "magnifier_size_relative",
        (
            active_magnifier.size_relative
            if active_magnifier is not None
            else default_magnifier_size(view)
        ),
    )
    save_setting(
        "capture_size_relative",
        (
            active_magnifier.capture_size_relative
            if active_magnifier is not None
            else default_capture_size(view)
        ),
    )
    save_setting(
        "magnifier_spacing_relative",
        (
            active_magnifier.spacing_relative
            if active_magnifier is not None
            else DEFAULT_MAGNIFIER_SPACING_RELATIVE
        ),
    )
    # The settings window's two stroke rows (owner request 2026-11-04). They
    # live on the feature state, so the dialog's own ``_save_setting`` — which
    # only schedules a snapshot of the HOST settings — never reaches them; this
    # is the tab's real persistence funnel (``save_image_compare_feature_settings``
    # -> ``overlay.settings.persist``). A capture thickness of ``None`` means
    # "keep the canvas style width", so nothing is written for it.
    _state = get_magnifier_widget_state(view)
    save_setting(
        "magnifier_border_thickness", int(_state.default_border_thickness)
    )
    if _state.default_capture_thickness is not None:
        save_setting(
            "magnifier_capture_thickness", int(_state.default_capture_thickness)
        )
