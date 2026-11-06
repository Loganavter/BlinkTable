from dataclasses import replace
from typing import TYPE_CHECKING, Any

# Audit-Meta: pattern=state-machine reason="single reducer machine — viewport/settings/slot/transaction reduction scanned by settings-scalar AST dogma, must stay one file"
# Long-term Store lock fix (plan_image_compare_dnd_tiles.md):
# All ViewportState / slot copies (dict shallow copies, ViewState/ViewportState
# replacements) are prepared here inside RootReducer.reduce — which Dispatcher
# now calls *outside* Dispatcher._lock. The critical section in
# Dispatcher.dispatch only does ``is``-comparison and an atomic swap of the
# already-prepared objects. This file must not use deepcopy; shallow
# ``dict(...)`` / ``replace`` is sufficient because feature states are
# immutable.

from core.store import (
    GeometryState,
    InteractionState,
    RenderConfig,
    SessionData,
    SettingsState,
    ViewportState,
    ViewState,
)
from ui.canvas_infra.scene.registry import get_canvas_registry

from .extension_reducers import (
    reduce_render_config_extensions,
    reduce_session_data_extensions,
)
from .slot_reducers import iter_state_slot_reducers

if TYPE_CHECKING:
    from core.store import Store

from domain.types import Rect

from .actions import (
    Action,
    ClearAllCachesAction,
    InvalidateGeometryCacheAction,
    InvalidateRenderCacheAction,
    SetAutoCropBlackBordersAction,
    SetChannelViewModeAction,
    SetDebugModeEnabledAction,
    SetDiffModeAction,
    SetDraggingSplitLineAction,
    SetExportFavoriteDirAction,
    SetFixedLabelDimensionsAction,
    SetImageDisplayRectAction,
    SetInteractionSessionIdAction,
    SetInteractiveModeAction,
    SetIsDraggingSliderAction,
    SetLanguageAction,
    SetKeyboardOverridesAction,
    SetRhiBackendAction,
    SetLastHorizontalMovementKeyAction,
    SetLastSpacingMovementKeyAction,
    SetLastVerticalMovementKeyAction,
    SetMovementSpeedAction,
    SetPixmapDimensionsAction,
    SetPressedKeysAction,
    SetResizeInProgressAction,
    SetShowingSingleImageModeAction,
    SetSpaceBarPressedAction,
    SetSplitPositionAction,
    SetSplitPositionVisualAction,
    SetSystemNotificationsEnabledAction,
    SetThemeAction,
    SetUIFontFamilyAction,
    SetUIFontModeAction,
    SetUIScaleFactorAction,
    SetUIModeAction,
    SetUserInteractingAction,
    SetVideoRecordingFpsAction,
    SetWindowGeometryAction,
    SetWindowWasMaximizedAction,
    ToggleOrientationAction,
)


def _build_viewport_state(
    state: ViewportState,
    *,
    render_config: RenderConfig | None = None,
    session_data: SessionData | None = None,
    view_state: ViewState | None = None,
    interaction_state: InteractionState | None = None,
    geometry_state: GeometryState | None = None,
) -> ViewportState:
    return ViewportState(
        render_config=render_config or state.render_config,
        session_data=session_data or state.session_data,
        view_state=view_state or state.view_state,
        interaction_state=interaction_state or state.interaction_state,
        geometry_state=geometry_state or state.geometry_state,
    )


class ViewStateReducer:
    @staticmethod
    def reduce(view_state: ViewState, action: Action, session_type: str | None = None) -> ViewState:
        feature_name = getattr(action, "feature", None)
        feature_state = getattr(action, "state", None)
        if feature_name is not None and feature_state is not None and getattr(action, "type", None) in (
            "SET_CANVAS_WIDGET_STATE",
            "UPDATE_CANVAS_FEATURE_STATE",
        ):
            # Shallow dict copy — no deepcopy (removed from critical section).
            # Live-to-live only: the derived ViewState deliberately shares the
            # OTHER features' mutable state objects; copying every feature on
            # every action is not free and dispatch runs off the lock.
            # Snapshot freeze is a different path: ``ViewState.clone()`` copies
            # each value, because a recorded frame must not follow live edits.
            current = dict(getattr(view_state, "canvas_widget_state", None) or {})
            if current.get(feature_name) is feature_state:
                return view_state
            current[feature_name] = feature_state
            return replace(view_state, canvas_widget_state=current)
        if isinstance(action, SetSplitPositionAction):
            clamped = max(0.0, min(1.0, action.position))
            if view_state.split_position == clamped:
                return view_state
            return replace(view_state, split_position=clamped)
        if isinstance(action, SetSplitPositionVisualAction):
            clamped = max(0.0, min(1.0, action.position))
            if view_state.split_position_visual == clamped:
                return view_state
            return replace(view_state, split_position_visual=clamped)
        if isinstance(action, ToggleOrientationAction):
            if view_state.is_horizontal == action.is_horizontal:
                return view_state
            return replace(view_state, is_horizontal=action.is_horizontal)
        if isinstance(action, SetMovementSpeedAction):
            if view_state.movement_speed_per_sec == action.speed:
                return view_state
            return replace(view_state, movement_speed_per_sec=action.speed)
        if isinstance(action, SetDiffModeAction):
            if view_state.diff_mode == action.mode:
                return view_state
            return replace(view_state, diff_mode=action.mode)
        if isinstance(action, SetChannelViewModeAction):
            if view_state.channel_view_mode == action.mode:
                return view_state
            return replace(view_state, channel_view_mode=action.mode)
        if isinstance(action, SetShowingSingleImageModeAction):
            if view_state.showing_single_image_mode == action.mode:
                return view_state
            return replace(view_state, showing_single_image_mode=action.mode)
        for feature in sorted(
            get_canvas_registry(session_type).get_widget_features(),
            key=lambda item: (item.reducer_order, item.name),
        ):
            reduced = feature.reduce_view_state(view_state, action)
            if reduced is not view_state:
                return reduced
        if isinstance(action, InvalidateRenderCacheAction):
            if (
                view_state.text_bg_visual_height == 0.0
                and view_state.text_bg_visual_width == 0.0
            ):
                return view_state
            return replace(
                view_state, text_bg_visual_height=0.0, text_bg_visual_width=0.0
            )
        if isinstance(action, InvalidateGeometryCacheAction):
            return replace(
                view_state, text_bg_visual_height=0.0, text_bg_visual_width=0.0
            )
        if isinstance(action, ClearAllCachesAction):
            return replace(
                view_state, text_bg_visual_height=0.0, text_bg_visual_width=0.0
            )
        return view_state


class InteractionStateReducer:
    @staticmethod
    def reduce(
        interaction_state: InteractionState, action: Action, session_type: str | None = None
    ) -> InteractionState:
        if isinstance(action, SetIsDraggingSliderAction):
            if interaction_state.is_dragging_any_slider == action.is_dragging:
                return interaction_state
            return replace(interaction_state, is_dragging_any_slider=action.is_dragging)
        if isinstance(action, SetResizeInProgressAction):
            if interaction_state.resize_in_progress == action.enabled:
                return interaction_state
            return replace(interaction_state, resize_in_progress=action.enabled)
        if isinstance(action, SetInteractiveModeAction):
            if interaction_state.is_interactive_mode == action.enabled:
                return interaction_state
            return replace(interaction_state, is_interactive_mode=action.enabled)
        if isinstance(action, SetDraggingSplitLineAction):
            if interaction_state.is_dragging_split_line == action.enabled:
                return interaction_state
            return replace(interaction_state, is_dragging_split_line=action.enabled)
        if isinstance(action, SetPressedKeysAction):
            if interaction_state.pressed_keys == action.keys:
                return interaction_state
            return replace(interaction_state, pressed_keys=set(action.keys))
        if isinstance(action, SetSpaceBarPressedAction):
            if interaction_state.space_bar_pressed == action.enabled:
                return interaction_state
            return replace(interaction_state, space_bar_pressed=action.enabled)
        if isinstance(action, SetInteractionSessionIdAction):
            if interaction_state.interaction_session_id == action.session_id:
                return interaction_state
            return replace(interaction_state, interaction_session_id=action.session_id)
        if isinstance(action, SetUserInteractingAction):
            if interaction_state.is_user_interacting == action.enabled:
                return interaction_state
            return replace(interaction_state, is_user_interacting=action.enabled)
        if isinstance(action, SetLastHorizontalMovementKeyAction):
            if interaction_state.last_horizontal_movement_key == action.key:
                return interaction_state
            return replace(interaction_state, last_horizontal_movement_key=action.key)
        if isinstance(action, SetLastVerticalMovementKeyAction):
            if interaction_state.last_vertical_movement_key == action.key:
                return interaction_state
            return replace(interaction_state, last_vertical_movement_key=action.key)
        if isinstance(action, SetLastSpacingMovementKeyAction):
            if interaction_state.last_spacing_movement_key == action.key:
                return interaction_state
            return replace(interaction_state, last_spacing_movement_key=action.key)
        for feature in sorted(
            get_canvas_registry(session_type).get_widget_features(),
            key=lambda item: (item.reducer_order, item.name),
        ):
            if feature.reduce_interaction_state is None:
                continue
            reduced = feature.reduce_interaction_state(interaction_state, action)
            if reduced is not interaction_state:
                return reduced
        return interaction_state


class GeometryStateReducer:
    @staticmethod
    def reduce(
        geometry_state: GeometryState, action: Action, session_type: str | None = None
    ) -> GeometryState:
        if isinstance(action, InvalidateGeometryCacheAction):
            if (
                geometry_state.pixmap_width == 0
                and geometry_state.pixmap_height == 0
                and geometry_state.image_display_rect_on_label == Rect()
                and geometry_state.fixed_label_width is None
                and geometry_state.fixed_label_height is None
            ):
                return geometry_state
            return replace(
                geometry_state,
                pixmap_width=0,
                pixmap_height=0,
                image_display_rect_on_label=Rect(),
                fixed_label_width=None,
                fixed_label_height=None,
            )
        if isinstance(action, ClearAllCachesAction):
            if (
                geometry_state.pixmap_width == 0
                and geometry_state.pixmap_height == 0
                and geometry_state.image_display_rect_on_label == Rect()
                and geometry_state.fixed_label_width is None
                and geometry_state.fixed_label_height is None
            ):
                return geometry_state
            return replace(
                geometry_state,
                pixmap_width=0,
                pixmap_height=0,
                image_display_rect_on_label=Rect(),
                fixed_label_width=None,
                fixed_label_height=None,
            )
        if isinstance(action, SetPixmapDimensionsAction):
            if (
                geometry_state.pixmap_width == action.width
                and geometry_state.pixmap_height == action.height
            ):
                return geometry_state
            return replace(
                geometry_state, pixmap_width=action.width, pixmap_height=action.height
            )
        if isinstance(action, SetImageDisplayRectAction):
            if geometry_state.image_display_rect_on_label == action.rect:
                return geometry_state
            return replace(geometry_state, image_display_rect_on_label=action.rect)
        if isinstance(action, SetFixedLabelDimensionsAction):
            if (
                geometry_state.fixed_label_width == action.width
                and geometry_state.fixed_label_height == action.height
            ):
                return geometry_state
            return replace(
                geometry_state,
                fixed_label_width=action.width,
                fixed_label_height=action.height,
            )
        for feature in sorted(
            get_canvas_registry(session_type).get_widget_features(),
            key=lambda item: (item.reducer_order, item.name),
        ):
            if feature.reduce_geometry_state is None:
                continue
            reduced = feature.reduce_geometry_state(geometry_state, action)
            if reduced is not geometry_state:
                return reduced
        return geometry_state


class ViewportReducer:
    def __init__(self):
        self.view_state_reducer = ViewStateReducer()
        self.interaction_state_reducer = InteractionStateReducer()
        self.geometry_state_reducer = GeometryStateReducer()

    def reduce(
        self, state: ViewportState, action: Action, session_type: str | None = None
    ) -> ViewportState:
        new_view_state = self.view_state_reducer.reduce(state.view_state, action, session_type)
        new_interaction_state = self.interaction_state_reducer.reduce(
            state.interaction_state, action, session_type
        )
        new_geometry_state = self.geometry_state_reducer.reduce(
            state.geometry_state, action, session_type
        )
        new_session_data = reduce_session_data_extensions(
            state.session_data, action, session_type
        )

        if (
            new_view_state is state.view_state
            and new_interaction_state is state.interaction_state
            and new_geometry_state is state.geometry_state
            and new_session_data is state.session_data
        ):
            return state

        return _build_viewport_state(
            state,
            session_data=new_session_data,
            view_state=new_view_state,
            interaction_state=new_interaction_state,
            geometry_state=new_geometry_state,
        )


class SettingsReducer:
    """Equality-guarded settings reduction.

    Every branch returns the *identical* ``SettingsState`` when the incoming
    value already equals the stored one. Without the guard each dispatch
    allocates a fresh instance, which defeats the dispatcher's ``is``-based
    no-op detection and emits a state change for a no-op.

    Branches stay explicit (one ``isinstance`` per action class) rather than
    table-driven: ``tests/contracts/test_settings_scalar_registry.py`` scans
    this file's AST for those branches.
    """

    @staticmethod
    def reduce(settings: SettingsState, action: Action) -> SettingsState:
        if isinstance(action, SetLanguageAction):
            if settings.current_language == action.language:
                return settings
            return replace(settings, current_language=action.language)
        if isinstance(action, SetUIModeAction):
            if settings.ui_mode == action.mode:
                return settings
            return replace(settings, ui_mode=action.mode)
        if isinstance(action, SetAutoCropBlackBordersAction):
            if settings.auto_crop_black_borders == action.enabled:
                return settings
            return replace(settings, auto_crop_black_borders=action.enabled)
        if isinstance(action, SetThemeAction):
            if settings.theme == action.theme:
                return settings
            return replace(settings, theme=action.theme)
        if isinstance(action, SetUIFontModeAction):
            if settings.ui_font_mode == action.mode:
                return settings
            return replace(settings, ui_font_mode=action.mode)
        if isinstance(action, SetUIFontFamilyAction):
            if settings.ui_font_family == action.family:
                return settings
            return replace(settings, ui_font_family=action.family)
        if isinstance(action, SetUIScaleFactorAction):
            if settings.ui_scale_factor == action.factor:
                return settings
            return replace(settings, ui_scale_factor=action.factor)
        if isinstance(action, SetDebugModeEnabledAction):
            if settings.debug_mode_enabled == action.enabled:
                return settings
            return replace(settings, debug_mode_enabled=action.enabled)
        if isinstance(action, SetSystemNotificationsEnabledAction):
            if settings.system_notifications_enabled == action.enabled:
                return settings
            return replace(settings, system_notifications_enabled=action.enabled)
        if isinstance(action, SetVideoRecordingFpsAction):
            if settings.video_recording_fps == action.fps:
                return settings
            return replace(settings, video_recording_fps=action.fps)
        if isinstance(action, SetRhiBackendAction):
            if settings.rhi_backend == action.backend:
                return settings
            return replace(settings, rhi_backend=action.backend)
        if isinstance(action, SetWindowWasMaximizedAction):
            if settings.window_was_maximized == action.was_maximized:
                return settings
            return replace(settings, window_was_maximized=action.was_maximized)
        if isinstance(action, SetWindowGeometryAction):
            if (
                settings.window_x == action.x
                and settings.window_y == action.y
                and settings.window_width == action.width
                and settings.window_height == action.height
            ):
                return settings
            return replace(
                settings,
                window_x=action.x,
                window_y=action.y,
                window_width=action.width,
                window_height=action.height,
            )
        if isinstance(action, SetExportFavoriteDirAction):
            if settings.export_favorite_dir == action.path:
                return settings
            return replace(settings, export_favorite_dir=action.path)
        if isinstance(action, SetKeyboardOverridesAction):
            incoming = dict(action.overrides)
            if dict(settings.keyboard_overrides) == incoming:
                return settings
            return replace(settings, keyboard_overrides=incoming)
        return settings


class RootReducer:
    def __init__(self):
        self.viewport_reducer = ViewportReducer()
        self.settings_reducer = SettingsReducer()

    @staticmethod
    def _slot_get(store: "Store", slot_name: str, session_id: str | None):
        """Session-addressed slot read (Wave CORE session targeting).

        ``session_id=None`` keeps the exact historical call shape (active
        session) so legacy fake stores without the kwarg keep working;
        explicit ids address that session's slots. Viewport/settings stay
        active-scoped in both paths — only slots are session-addressed.
        """
        if session_id is None:
            return store.get_session_state_slot(slot_name)
        return store.get_session_state_slot(slot_name, session_id=session_id)

    @staticmethod
    def _session_for_type(store: "Store", session_id: str | None):
        """Session whose ``session_type`` gates viewport reduction.

        Explicit ids resolve via ``get_workspace_session`` (unknown ids
        fail open to the active session, mirroring Dispatcher); the
        viewport VALUE reduced is still the active one — only the type
        gate follows the target session.
        """
        if session_id is not None:
            get_one = getattr(store, "get_workspace_session", None)
            if callable(get_one):
                try:
                    target = get_one(session_id)
                except Exception:
                    target = None
                if target is not None:
                    return target
        return store.get_active_workspace_session()

    def _reduce_one(self, store: "Store", action: Action, session_type: str | None, session_id: str | None = None) -> tuple[Any, Any, dict[str, Any], bool, bool]:
        """Single-action reduction without allocating Store. Returns (new_viewport, new_settings, new_slot_values, any_slot_changed, viewport_changed)."""
        new_viewport = self.viewport_reducer.reduce(store.viewport, action, session_type)
        new_render_config = reduce_render_config_extensions(
            new_viewport.render_config, action, session_type
        )
        if new_render_config is not new_viewport.render_config:
            new_viewport = _build_viewport_state(
                new_viewport, render_config=new_render_config
            )
        new_settings = self.settings_reducer.reduce(store.settings, action)
        new_slot_values: dict[str, Any] = {}
        any_slot_changed = False
        for slot_name, slot_reducer in iter_state_slot_reducers():
            # For intermediate steps in a transaction we need current accumulated value, not original store's
            # This helper is only for single action; caller handles accumulation for transaction.
            current_value = RootReducer._slot_get(store, slot_name, session_id)
            new_value = slot_reducer(current_value, action)
            new_slot_values[slot_name] = new_value
            if new_value is not current_value:
                any_slot_changed = True
        return new_viewport, new_settings, new_slot_values, any_slot_changed, new_viewport is not store.viewport

    def reduce(self, store: "Store", action: Action, session_id: str | None = None) -> "Store":
        from core.store import Store

        # Transaction — single Store alloc for N inner actions (plan_image_pipeline.md Phase 5)
        if getattr(action, "type", None) == "TRANSACTION":
            inner = getattr(action, "actions", None) or []
            if not inner:
                return store
            type_session = RootReducer._session_for_type(store, session_id)
            session_type = type_session.session_type if type_session is not None else None
            cur_viewport = store.viewport
            cur_settings = store.settings
            # accumulate slot values in dict, starting from current store
            cur_slots: dict[str, Any] = {
                name: RootReducer._slot_get(store, name, session_id) for name, _ in iter_state_slot_reducers()
            }
            any_changed = False
            for sub in inner:
                # reduce viewport/settings incrementally
                new_viewport = self.viewport_reducer.reduce(cur_viewport, sub, session_type)
                new_render_config = reduce_render_config_extensions(new_viewport.render_config, sub, session_type)
                if new_render_config is not new_viewport.render_config:
                    new_viewport = _build_viewport_state(new_viewport, render_config=new_render_config)
                new_settings = self.settings_reducer.reduce(cur_settings, sub)
                # reduce each slot against accumulated value
                new_slots: dict[str, Any] = {}
                slot_changed_this_step = False
                for slot_name, slot_reducer in iter_state_slot_reducers():
                    cur_val = cur_slots.get(slot_name)
                    new_val = slot_reducer(cur_val, sub)
                    new_slots[slot_name] = new_val
                    if new_val is not cur_val:
                        slot_changed_this_step = True
                        any_changed = True
                if new_viewport is not cur_viewport:
                    any_changed = True
                if new_settings is not cur_settings:
                    any_changed = True
                if slot_changed_this_step:
                    # will set any_changed already
                    pass
                cur_viewport, cur_settings, cur_slots = new_viewport, new_settings, new_slots
            if not any_changed:
                return store
            new_store = Store()
            new_store.viewport = cur_viewport
            for slot_name, new_value in cur_slots.items():
                new_store.set_session_state_slot(slot_name, new_value, emit_scope="")
            new_store.settings = cur_settings
            new_store.recorder = store.recorder
            new_store._dispatcher = store._dispatcher
            return new_store

        active_session = RootReducer._session_for_type(store, session_id)
        session_type = active_session.session_type if active_session is not None else None
        new_viewport = self.viewport_reducer.reduce(store.viewport, action, session_type)
        new_render_config = reduce_render_config_extensions(
            new_viewport.render_config, action, session_type
        )
        if new_render_config is not new_viewport.render_config:
            new_viewport = _build_viewport_state(
                new_viewport, render_config=new_render_config
            )

        new_settings = self.settings_reducer.reduce(store.settings, action)

        new_slot_values: dict[str, Any] = {}
        any_slot_changed = False
        for slot_name, slot_reducer in iter_state_slot_reducers():
            current_value = RootReducer._slot_get(store, slot_name, session_id)
            new_value = slot_reducer(current_value, action)
            new_slot_values[slot_name] = new_value
            if new_value is not current_value:
                any_slot_changed = True

        if (
            new_viewport is store.viewport
            and not any_slot_changed
            and new_settings is store.settings
        ):
            return store

        new_store = Store()
        new_store.viewport = new_viewport
        for slot_name, new_value in new_slot_values.items():
            new_store.set_session_state_slot(slot_name, new_value, emit_scope="")
        new_store.settings = new_settings
        new_store.recorder = store.recorder
        new_store._dispatcher = store._dispatcher
        return new_store