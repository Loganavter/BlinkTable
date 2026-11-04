from __future__ import annotations

# File-Size-Exempt: flat command/query/alias tables — one entry per command
# role; splitting the tuples across modules would add import indirection
# without reducing coupling (every entry is an independent one-liner).

from ui.canvas_infra.scene.widget_contract import (
    CanvasFeatureCommandAlias,
    CanvasFeatureStateCommand,
    CanvasFeatureStateQuery,
    CanvasWidgetFeature,
)

from tabs.image_compare.canvas.features.magnifier.commands import build_magnifier_commands as assemble_magnifier_commands
from tabs.image_compare.canvas.features.magnifier.input.gestures import build_magnifier_gesture_bindings
from tabs.image_compare.canvas.features.magnifier.input.context_menu_zones import (
    build_magnifier_context_menu_zones,
)
from tabs.image_compare.canvas.features.magnifier.render.overlay import apply_magnifier_overlay
from tabs.image_compare.canvas.features.magnifier.render.plan_overlay import apply_magnifier_plan_overlay
from tabs.image_compare.canvas.features.magnifier.properties import build_magnifier_properties
from tabs.image_compare.canvas.features.magnifier.reducers import (
    reduce_magnifier_geometry_state,
    reduce_magnifier_interaction_state,
    reduce_magnifier_render_config,
    reduce_magnifier_view_state,
)
from tabs.image_compare.canvas.features.magnifier.runtime_hooks import (
    build_magnifier_render_scene_overrides,
    command_build_render_canvas_payload,
    prepare_magnifier_worker_viewport,
)
from tabs.image_compare.canvas.features.magnifier.settings_bindings import build_magnifier_settings_event_bindings
from tabs.image_compare.canvas.features.magnifier.toolbar.bindings import build_magnifier_toolbar_bindings


def build_magnifier_commands():
    return assemble_magnifier_commands(command_build_render_canvas_payload)


def build_magnifier_state_queries():
    """Build state queries for direct feature state access."""
    from tabs.image_compare.canvas.features.magnifier.commands.queries import (
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

    return (
        CanvasFeatureStateQuery(query_id="is_horizontal", handler=query_is_horizontal),
        CanvasFeatureStateQuery(
            query_id="active_state", handler=query_active_magnifier_state
        ),
        CanvasFeatureStateQuery(query_id="total_count", handler=query_total_count),
        CanvasFeatureStateQuery(
            query_id="all_states", handler=query_all_magnifier_states
        ),
        CanvasFeatureStateQuery(
            query_id="spacing_limits", handler=query_spacing_limits
        ),
        CanvasFeatureStateQuery(
            query_id="behavior_settings", handler=query_behavior_settings
        ),
        CanvasFeatureStateQuery(
            query_id="should_show_panel", handler=query_should_show_panel
        ),
        CanvasFeatureStateQuery(
            query_id="are_all_frozen", handler=query_are_all_frozen
        ),
        CanvasFeatureStateQuery(
            query_id="active_combined", handler=query_active_combined
        ),
        CanvasFeatureStateQuery(
            query_id="active_divider_color", handler=query_active_divider_color
        ),
        CanvasFeatureStateQuery(
            query_id="active_divider_visible", handler=query_active_divider_visible
        ),
        CanvasFeatureStateQuery(
            query_id="active_border_color", handler=query_active_border_color
        ),
        CanvasFeatureStateQuery(
            query_id="active_capture_size", handler=query_active_capture_size
        ),
    )


def build_magnifier_state_commands():
    """Build state commands for direct feature state modification."""
    from tabs.image_compare.canvas.features.magnifier.commands.interaction import (
        begin_capture_drag,
        begin_internal_split_drag,
        end_capture_drag,
        end_internal_split_drag,
        update_capture_drag,
        update_internal_split_drag,
    )
    from tabs.image_compare.canvas.features.magnifier.commands.preview import (
        preview_begin,
        preview_restore,
        preview_set_side,
    )
    from tabs.image_compare.canvas.features.magnifier.commands.viewport import (
        viewport_add_instance,
        viewport_move_active_position,
        viewport_remove_active_instance,
        viewport_set_active_border_color,
        viewport_set_active_capture_size,
        viewport_set_active_combined,
        viewport_set_active_divider_color,
        viewport_set_active_freeze,
        viewport_set_active_instance,
        viewport_set_active_laser_enabled,
        viewport_set_active_offset,
        viewport_set_active_orientation,
        viewport_set_active_size,
        viewport_set_active_visibility_parts,
        viewport_set_all_freeze,
        viewport_set_instance_visibility,
        viewport_set_internal_split,
        viewport_toggle_enabled,
    )

    return (
        CanvasFeatureStateCommand(
            command_id="toggle_enabled", handler=viewport_toggle_enabled
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_size", handler=viewport_set_active_size
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_instance", handler=viewport_set_active_instance
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_capture_size",
            handler=viewport_set_active_capture_size,
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_visibility_parts",
            handler=viewport_set_active_visibility_parts,
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_orientation", handler=viewport_set_active_orientation
        ),
        CanvasFeatureStateCommand(
            command_id="move_active_position", handler=viewport_move_active_position
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_offset", handler=viewport_set_active_offset
        ),
        CanvasFeatureStateCommand(
            command_id="add_instance", handler=viewport_add_instance
        ),
        CanvasFeatureStateCommand(
            command_id="remove_active_instance", handler=viewport_remove_active_instance
        ),
        CanvasFeatureStateCommand(
            command_id="set_instance_visibility",
            handler=viewport_set_instance_visibility,
        ),
        CanvasFeatureStateCommand(
            command_id="set_internal_split", handler=viewport_set_internal_split
        ),
        CanvasFeatureStateCommand(
            command_id="set_all_freeze", handler=viewport_set_all_freeze
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_freeze", handler=viewport_set_active_freeze
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_combined", handler=viewport_set_active_combined
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_border_color",
            handler=viewport_set_active_border_color,
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_divider_color",
            handler=viewport_set_active_divider_color,
        ),
        CanvasFeatureStateCommand(
            command_id="set_active_laser_enabled",
            handler=viewport_set_active_laser_enabled,
        ),
        CanvasFeatureStateCommand(
            command_id="begin_capture_drag", handler=begin_capture_drag
        ),
        CanvasFeatureStateCommand(
            command_id="update_capture_drag", handler=update_capture_drag
        ),
        CanvasFeatureStateCommand(
            command_id="end_capture_drag", handler=end_capture_drag
        ),
        CanvasFeatureStateCommand(
            command_id="begin_internal_split_drag", handler=begin_internal_split_drag
        ),
        CanvasFeatureStateCommand(
            command_id="update_internal_split_drag", handler=update_internal_split_drag
        ),
        CanvasFeatureStateCommand(
            command_id="end_internal_split_drag", handler=end_internal_split_drag
        ),
        CanvasFeatureStateCommand(command_id="preview_begin", handler=preview_begin),
        CanvasFeatureStateCommand(
            command_id="preview_restore", handler=preview_restore
        ),
        CanvasFeatureStateCommand(
            command_id="preview_set_side", handler=preview_set_side
        ),
    )


MAGNIFIER_COMMAND_ALIASES = (
    CanvasFeatureCommandAlias("overlay.enabled", "query.enabled"),
    CanvasFeatureCommandAlias("overlay.is_horizontal", "query.is_horizontal"),
    CanvasFeatureCommandAlias("overlay.active_state", "query.active_state"),
    CanvasFeatureCommandAlias("overlay.all_states", "query.all_states"),
    CanvasFeatureCommandAlias("overlay.behavior_settings", "query.behavior_settings"),
    CanvasFeatureCommandAlias("overlay.active_combined", "query.active_combined"),
    CanvasFeatureCommandAlias("overlay.scene_graph", "query.scene_graph"),
    CanvasFeatureCommandAlias(
        "overlay.refresh_scene_graph", "runtime.refresh_scene_graph"
    ),
    CanvasFeatureCommandAlias(
        "overlay.active_divider_color", "query.active_divider_color"
    ),
    CanvasFeatureCommandAlias(
        "overlay.active_divider_visible", "query.active_divider_visible"
    ),
    CanvasFeatureCommandAlias(
        "overlay.active_divider_thickness", "query.active_divider_thickness"
    ),
    CanvasFeatureCommandAlias(
        "overlay.active_border_color", "query.active_border_color"
    ),
    CanvasFeatureCommandAlias(
        "overlay.active_capture_size", "query.active_capture_size"
    ),
    CanvasFeatureCommandAlias("overlay.total_count", "query.total_count"),
    CanvasFeatureCommandAlias("overlay.movement_handler", "query.movement_handler"),
    CanvasFeatureCommandAlias("overlay.canvas_payload", "render.canvas_payload"),
    CanvasFeatureCommandAlias("overlay.render_drawing_coords", "render.drawing_coords"),
    CanvasFeatureCommandAlias("overlay.render_build_layout", "render.build_layout"),
    CanvasFeatureCommandAlias(
        "overlay.render_shift_layout_to_tile", "render.shift_layout_to_tile"
    ),
    CanvasFeatureCommandAlias("overlay.snapshot_normalize", "snapshot.normalize_store"),
    CanvasFeatureCommandAlias(
        "overlay.snapshot_apply_virtual_layout",
        "snapshot.apply_virtual_layout",
    ),
    CanvasFeatureCommandAlias("overlay.settings_initialize", "settings.initialize"),
    CanvasFeatureCommandAlias("overlay.settings.persist", "settings.persist"),
    CanvasFeatureCommandAlias("overlay.toggle_enabled", "viewport.toggle_enabled"),
    CanvasFeatureCommandAlias("overlay.set_active_size", "viewport.set_active_size"),
    CanvasFeatureCommandAlias(
        "overlay.set_active_instance", "viewport.set_active_instance"
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_active_capture_size", "viewport.set_active_capture_size"
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_active_visibility_parts",
        "viewport.set_active_visibility_parts",
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_active_orientation", "viewport.set_active_orientation"
    ),
    CanvasFeatureCommandAlias(
        "overlay.move_active_position", "viewport.move_active_position"
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_active_offset", "viewport.set_active_offset"
    ),
    CanvasFeatureCommandAlias("overlay.should_show_panel", "query.should_show_panel"),
    CanvasFeatureCommandAlias("overlay.add_instance", "viewport.add_instance"),
    CanvasFeatureCommandAlias(
        "overlay.remove_active_instance", "viewport.remove_active_instance"
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_instance_visibility",
        "viewport.set_instance_visibility",
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_internal_split", "viewport.set_internal_split"
    ),
    CanvasFeatureCommandAlias("overlay.set_all_freeze", "viewport.set_all_freeze"),
    CanvasFeatureCommandAlias(
        "overlay.set_active_freeze", "viewport.set_active_freeze"
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_active_combined", "viewport.set_active_combined"
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_active_border_color", "viewport.set_active_border_color"
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_active_divider_color", "viewport.set_active_divider_color"
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_active_guides_color", "viewport.set_active_guides_color"
    ),
    CanvasFeatureCommandAlias(
        "overlay.set_active_laser_enabled", "viewport.set_active_laser_enabled"
    ),
    CanvasFeatureCommandAlias(
        "overlay.begin_capture_drag", "interaction.begin_capture_drag"
    ),
    CanvasFeatureCommandAlias(
        "overlay.update_capture_drag", "interaction.update_capture_drag"
    ),
    CanvasFeatureCommandAlias(
        "overlay.end_capture_drag", "interaction.end_capture_drag"
    ),
    CanvasFeatureCommandAlias(
        "overlay.begin_internal_split_drag", "interaction.begin_internal_split_drag"
    ),
    CanvasFeatureCommandAlias(
        "overlay.update_internal_split_drag", "interaction.update_internal_split_drag"
    ),
    CanvasFeatureCommandAlias(
        "overlay.end_internal_split_drag", "interaction.end_internal_split_drag"
    ),
    CanvasFeatureCommandAlias("preview.snapshot", "interaction.preview_begin"),
    CanvasFeatureCommandAlias("preview.restore", "interaction.preview_restore"),
    CanvasFeatureCommandAlias("preview.set_side", "interaction.preview_set_side"),
    CanvasFeatureCommandAlias(
        "preview.emit_changed", "interaction.emit_overlay_changed"
    ),
    CanvasFeatureCommandAlias(
        "keyboard_movement.build_controller",
        "interaction.build_keyboard_movement_controller",
    ),
    CanvasFeatureCommandAlias("overlay.rebuild", "runtime.rebuild_overlay"),
    CanvasFeatureCommandAlias("overlay.render_layer", "runtime.render_layer"),
    CanvasFeatureCommandAlias(
        "overlay.stop_interactive_movement", "runtime.stop_interactive_movement"
    ),
    CanvasFeatureCommandAlias(
        "overlay.update_capture_area_display", "runtime.update_capture_area_display"
    ),
    CanvasFeatureCommandAlias(
        "overlay.request_cached_diff", "runtime.request_cached_diff_image"
    ),
    CanvasFeatureCommandAlias(
        "overlay.settings.apply_behavior", "settings.apply_behavior"
    ),
    CanvasFeatureCommandAlias(
        "overlay.settings.set_stroke", "settings.set_stroke"
    ),
    CanvasFeatureCommandAlias(
        "overlay.settings.get_stroke", "settings.get_stroke"
    ),
    CanvasFeatureCommandAlias(
        "overlay.settings.toggle_divider_visibility",
        "settings.toggle_divider_visibility",
    ),
    CanvasFeatureCommandAlias(
        "overlay.settings.set_divider_color", "settings.set_divider_color"
    ),
    CanvasFeatureCommandAlias(
        "overlay.settings.set_divider_thickness", "settings.set_divider_thickness"
    ),
    CanvasFeatureCommandAlias(
        "overlay.settings.set_border_color", "settings.set_border_color"
    ),
    CanvasFeatureCommandAlias(
        "overlay.settings.set_optimize_movement", "settings.set_optimize_movement"
    ),
    CanvasFeatureCommandAlias(
        "overlay.settings.set_movement_interpolation",
        "settings.set_movement_interpolation",
    ),
    CanvasFeatureCommandAlias("project.serialize_magnifier", "project.serialize"),
    CanvasFeatureCommandAlias("project.restore_magnifier", "project.restore"),
)


def build_widget_feature() -> CanvasWidgetFeature:
    return CanvasWidgetFeature(
        name="magnifier",
        reduce_view_state=reduce_magnifier_view_state,
        reduce_render_config=reduce_magnifier_render_config,
        reduce_interaction_state=reduce_magnifier_interaction_state,
        reduce_geometry_state=reduce_magnifier_geometry_state,
        build_properties=build_magnifier_properties,
        build_toolbar_bindings=build_magnifier_toolbar_bindings,
        build_commands=build_magnifier_commands,
        command_aliases=MAGNIFIER_COMMAND_ALIASES,
        build_settings_event_bindings=build_magnifier_settings_event_bindings,
        build_gesture_bindings=build_magnifier_gesture_bindings,
        build_context_menu_zones=build_magnifier_context_menu_zones,
        build_state_queries=build_magnifier_state_queries,
        build_state_commands=build_magnifier_state_commands,
        build_render_scene_overrides=build_magnifier_render_scene_overrides,
        prepare_worker_viewport=prepare_magnifier_worker_viewport,
        apply_plan_runtime_overlay=apply_magnifier_plan_overlay,
        apply_live_runtime_overlay=apply_magnifier_overlay,
        i18n_namespace="features.magnifier",
        reducer_order=50,
        property_order=50,
    )


WIDGET_FEATURE = build_widget_feature()
