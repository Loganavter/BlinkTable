from __future__ import annotations

from typing import Any

from tabs.image_compare.canvas.features.magnifier.commands.interaction import (
    begin_capture_drag,
    begin_internal_split_drag,
    end_capture_drag,
    end_internal_split_drag,
    update_capture_drag,
    update_internal_split_drag,
)
from tabs.image_compare.canvas.features.magnifier.commands.preview import preview_begin, preview_restore, preview_set_side
from tabs.image_compare.canvas.features.magnifier.commands.queries import (
    emit_overlay_changed,
    get_movement_handler,
    query_active_border_color,
    query_active_capture_size,
    query_active_combined,
    query_active_divider_color,
    query_active_divider_visible,
    query_active_magnifier_state,
    query_all_magnifier_states,
    query_are_all_frozen,
    query_behavior_settings,
    query_is_horizontal,
    query_should_show_panel,
    query_spacing_limits,
    query_total_count,
)
from tabs.image_compare.canvas.features.magnifier.commands.settings import (
    settings_apply_behavior,
    settings_get_stroke,
    settings_set_stroke,
    settings_initialize,
    settings_persist,
    settings_set_movement_interpolation,
    settings_set_optimize_movement,
)
from tabs.image_compare.canvas.features.magnifier.commands.viewport import (
    magnifier_scene_graph,
    viewport_add_instance,
    viewport_ensure_active,
    viewport_move_active_position,
    viewport_remove_active_instance,
    viewport_set_active_border_color,
    viewport_set_active_capture_size,
    viewport_set_active_combined,
    viewport_set_active_divider_color,
    viewport_set_active_freeze,
    viewport_set_active_guides_color,
    viewport_set_active_instance,
    viewport_set_active_laser_enabled,
    viewport_set_active_offset,
    viewport_set_active_orientation,
    viewport_set_active_size,
    viewport_set_active_spacing,
    viewport_set_active_visibility_parts,
    viewport_set_all_freeze,
    viewport_set_instance_visibility,
    viewport_set_internal_split,
    viewport_refresh_magnifier_scene,
    viewport_toggle_enabled,
)
from tabs.image_compare.canvas.features.magnifier.persistence import (
    restore_magnifier_from_project,
    serialize_magnifier_for_project,
)


def _settings_set_border_color(settings, color) -> bool:
    changed = settings.mutations.set_canvas_feature_setting(
        "magnifier.border.color",
        color,
        invalidate_render_cache=True,
        request_core_update=True,
    )
    viewport_set_active_border_color(settings.mutations.store, color)
    return changed


def _settings_set_divider_color(settings, color) -> bool:
    changed = settings.mutations.set_canvas_feature_setting(
        "magnifier.divider.color",
        color,
        invalidate_render_cache=True,
        request_core_update=True,
    )
    viewport_set_active_divider_color(settings.mutations.store, color)
    return changed


def _build_keyboard_movement_controller(store, *, presenter_provider, parent=None):
    from tabs.image_compare.canvas.features.magnifier.input.keyboard_movement import build_controller

    return build_controller(store, presenter_provider=presenter_provider, parent=parent)


def build_magnifier_commands(render_canvas_payload) -> dict[str, Any]:
    from tabs.image_compare.canvas.features.magnifier.geometry.bounds import (
        compute_magnifier_layout_requirement,
    )
    from tabs.image_compare.canvas.features.magnifier.render.overlay import apply_magnifier_overlay, build_magnifier_drawing_coords
    from tabs.image_compare.canvas.features.magnifier.geometry.layout_plan import build_magnifier_layout, shift_layout_to_tile
    from tabs.image_compare.canvas.features.magnifier.render.plan_overlay import apply_magnifier_plan_overlay
    from tabs.image_compare.canvas.features.magnifier.state.snapshot_store import (
        apply_virtual_canvas_layout_to_snapshot_store,
        normalize_snapshot_store,
    )
    from tabs.image_compare.canvas.features.magnifier.state.store import active_or_default_divider_thickness, magnifier_enabled
    from tabs.image_compare.canvas.features.magnifier.workers.diff_cache import request_cached_diff_image_async
    from tabs.image_compare.canvas.features.magnifier.workers.result_handlers import (
        stop_interactive_movement,
        update_capture_area_display,
    )
    from tabs.image_compare.canvas.features.magnifier.workers.scene_update import rebuild_magnifier_overlay
    from tabs.image_compare.canvas.features.magnifier.workers.worker_flow import render_magnifier_layer

    return {
        "interaction.begin_capture_drag": begin_capture_drag,
        "interaction.update_capture_drag": update_capture_drag,
        "interaction.end_capture_drag": end_capture_drag,
        "interaction.begin_internal_split_drag": begin_internal_split_drag,
        "interaction.update_internal_split_drag": update_internal_split_drag,
        "interaction.end_internal_split_drag": end_internal_split_drag,
        "settings.toggle_divider_visibility": lambda settings, visible: settings.mutations.set_canvas_feature_setting(
            "magnifier.divider.visible",
            bool(visible),
            invalidate_render_cache=True,
            request_core_update=True,
        ),
        "settings.set_divider_color": lambda settings, color: _settings_set_divider_color(
            settings, color
        ),
        "settings.set_divider_thickness": lambda settings, thickness: settings.mutations.set_canvas_feature_setting(
            "magnifier.divider.thickness",
            max(0, min(10, int(thickness))),
            invalidate_render_cache=True,
            request_core_update=True,
        ),
        "settings.set_border_color": lambda settings, color: _settings_set_border_color(
            settings, color
        ),
        "render.canvas_payload": render_canvas_payload,
        # The cached scene graph, as capabilities: a scene command verified
        # inside ONE call asks whether a usable graph exists and rebuilds it
        # through this feature's own builder (the two S2 callers live in
        # ``devtools/canvas_scene/commands.py`` and reach them by alias, never by
        # importing the feature). ``canvas`` is the widget — the same
        # ``(store, canvas)`` shape as ``apply_live_runtime_overlay``.
        "query.scene_graph": lambda _store, canvas: magnifier_scene_graph(canvas),
        "runtime.refresh_scene_graph": lambda store, canvas: viewport_refresh_magnifier_scene(
            store, canvas
        ),
        "render.drawing_coords": build_magnifier_drawing_coords,
        "render.layout_requirement": lambda store, *, drawing_width, drawing_height: compute_magnifier_layout_requirement(
            store,
            drawing_width=drawing_width,
            drawing_height=drawing_height,
        ),
        "render.build_layout": lambda viewport, **kwargs: build_magnifier_layout(
            viewport,
            **kwargs,
        ),
        "render.shift_layout_to_tile": lambda layout, *, tile_left, tile_top: shift_layout_to_tile(
            layout,
            tile_left=tile_left,
            tile_top=tile_top,
        ),
        "runtime.request_cached_diff_image": lambda presenter, image1, image2, diff_mode: request_cached_diff_image_async(
            presenter,
            image1,
            image2,
            diff_mode,
        ),
        "runtime.rebuild_overlay": rebuild_magnifier_overlay,
        "runtime.render_layer": render_magnifier_layer,
        "runtime.stop_interactive_movement": stop_interactive_movement,
        "runtime.update_capture_area_display": update_capture_area_display,
        "runtime.apply_overlay": apply_magnifier_overlay,
        "runtime.apply_plan_overlay": lambda _store, canvas, plan: apply_magnifier_plan_overlay(
            canvas,
            plan,
        ),
        "query.enabled": lambda store: bool(
            store is not None and magnifier_enabled(store.viewport.view_state)
        ),
        "query.active_state": query_active_magnifier_state,
        "query.all_states": query_all_magnifier_states,
        "query.spacing_limits": query_spacing_limits,
        "query.behavior_settings": query_behavior_settings,
        "query.total_count": query_total_count,
        "query.should_show_panel": query_should_show_panel,
        "query.are_all_frozen": query_are_all_frozen,
        "query.active_combined": query_active_combined,
        "query.active_divider_color": query_active_divider_color,
        "query.active_divider_visible": query_active_divider_visible,
        "query.active_border_color": query_active_border_color,
        "query.active_capture_size": query_active_capture_size,
        "query.active_divider_thickness": lambda store: (
            int(active_or_default_divider_thickness(store.viewport.view_state))
            if store is not None
            else 0
        ),
        "query.is_horizontal": query_is_horizontal,
        "project.serialize": serialize_magnifier_for_project,
        "project.restore": restore_magnifier_from_project,
        "viewport.toggle_enabled": viewport_toggle_enabled,
        "viewport.ensure_active": viewport_ensure_active,
        "viewport.set_active_size": viewport_set_active_size,
        "viewport.set_active_capture_size": viewport_set_active_capture_size,
        "viewport.set_active_offset": viewport_set_active_offset,
        "viewport.set_active_spacing": viewport_set_active_spacing,
        "viewport.set_active_border_color": viewport_set_active_border_color,
        "viewport.set_active_divider_color": viewport_set_active_divider_color,
        "viewport.set_active_guides_color": viewport_set_active_guides_color,
        "viewport.set_active_laser_enabled": viewport_set_active_laser_enabled,
        "viewport.set_active_visibility_parts": viewport_set_active_visibility_parts,
        "viewport.set_active_orientation": viewport_set_active_orientation,
        "viewport.move_active_position": viewport_move_active_position,
        "viewport.set_internal_split": viewport_set_internal_split,
        "viewport.add_instance": viewport_add_instance,
        "viewport.remove_active_instance": viewport_remove_active_instance,
        "viewport.set_active_instance": viewport_set_active_instance,
        "viewport.set_instance_visibility": viewport_set_instance_visibility,
        "viewport.set_all_freeze": viewport_set_all_freeze,
        "viewport.set_active_freeze": viewport_set_active_freeze,
        "viewport.set_active_combined": viewport_set_active_combined,
        "snapshot.normalize_store": lambda store: normalize_snapshot_store(store),
        "snapshot.apply_virtual_layout": lambda store, **kwargs: apply_virtual_canvas_layout_to_snapshot_store(
            store,
            **kwargs,
        ),
        "settings.apply_behavior": settings_apply_behavior,
        "settings.set_stroke": settings_set_stroke,
        "settings.get_stroke": settings_get_stroke,
        "settings.initialize": settings_initialize,
        "settings.persist": settings_persist,
        "settings.set_optimize_movement": settings_set_optimize_movement,
        "settings.set_movement_interpolation": settings_set_movement_interpolation,
        "query.movement_handler": get_movement_handler,
        "interaction.preview_begin": preview_begin,
        "interaction.preview_restore": preview_restore,
        "interaction.preview_set_side": preview_set_side,
        "interaction.emit_overlay_changed": emit_overlay_changed,
        "interaction.build_keyboard_movement_controller": _build_keyboard_movement_controller,
    }