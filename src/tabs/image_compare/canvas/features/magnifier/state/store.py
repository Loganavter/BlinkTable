from __future__ import annotations

from domain.types import Color, Point
from tabs.image_compare.canvas.features.magnifier.constants import (
    DEFAULT_MAGNIFIER_SPACING_RELATIVE,
)
from tabs.image_compare.canvas.features.magnifier.state.models import MagnifierModel
from tabs.image_compare.canvas.features.magnifier.state.feature_state import (
    MagnifierWidgetState,
    get_magnifier_widget_state,
)
from tabs.image_compare.canvas.registry import registry

DEFAULT_MAGNIFIER_ID = "default"

_AUTO_COLOR_PALETTE: tuple[dict[str, tuple[int, int, int, int]], ...] = (
    {
        "border": (0, 190, 255, 248),
        "divider": (110, 225, 255, 235),
        "capture": (0, 190, 255, 230),
        "guides": (110, 225, 255, 255),
    },
    {
        "border": (255, 184, 0, 248),
        "divider": (255, 214, 92, 235),
        "capture": (255, 184, 0, 230),
        "guides": (255, 214, 92, 255),
    },
    {
        "border": (58, 220, 122, 248),
        "divider": (134, 245, 174, 235),
        "capture": (58, 220, 122, 230),
        "guides": (134, 245, 174, 255),
    },
    {
        "border": (255, 92, 150, 248),
        "divider": (255, 150, 190, 235),
        "capture": (255, 92, 150, 230),
        "guides": (255, 150, 190, 255),
    },
    {
        "border": (178, 120, 255, 248),
        "divider": (210, 172, 255, 235),
        "capture": (178, 120, 255, 230),
        "guides": (210, 172, 255, 255),
    },
    {
        "border": (255, 126, 64, 248),
        "divider": (255, 174, 126, 235),
        "capture": (255, 126, 64, 230),
        "guides": (255, 174, 126, 255),
    },
)


def _color_from_rgba(rgba: tuple[int, int, int, int]) -> Color:
    return Color(int(rgba[0]), int(rgba[1]), int(rgba[2]), int(rgba[3]))


def _copy_model(
    model: MagnifierModel, *, magnifier_id: str | None = None
) -> MagnifierModel:
    cloned = model.clone()
    cloned.id = magnifier_id or model.id
    return cloned


def _build_default_magnifier_model(
    view_state, render_config, magnifier_id: str | None = None
) -> MagnifierModel:
    state = get_magnifier_widget_state(view_state)
    _get_capture_state = registry().get_feature_command_by_alias("capture.widget_state")
    capture_state = (
        _get_capture_state(view_state) if _get_capture_state is not None else None
    )
    return MagnifierModel(
        id=magnifier_id or state.active_id or DEFAULT_MAGNIFIER_ID,
        visible=bool(state.enabled),
        position=Point(0.5, 0.5),
        size_relative=float(state.default_size_relative or 0.2),
        capture_size_relative=float(state.default_capture_size_relative or 0.1),
        border_color=state.default_border_color,
        divider_color=state.default_divider_color,
        offset_relative=Point(0.0, 0.0),
        spacing_relative=DEFAULT_MAGNIFIER_SPACING_RELATIVE,
        is_horizontal=False,
        internal_split=0.5,
        divider_visible=state.default_divider_visible,
        divider_thickness=state.default_divider_thickness,
        border_thickness=int(getattr(state, "default_border_thickness", 2)),
        visible_left=True,
        visible_center=True,
        visible_right=True,
        freeze=False,
        frozen_position=None,
        show_capture_area=capture_state.visible if capture_state is not None else True,
        interpolation_method=render_config.interpolation_method,
    )


def _apply_auto_instance_color(
    state: MagnifierWidgetState, model: MagnifierModel
) -> None:
    if not state.auto_color_new_instances or model.id == DEFAULT_MAGNIFIER_ID:
        return
    existing_custom_count = sum(
        1
        for magnifier_id in state.models.keys()
        if magnifier_id != DEFAULT_MAGNIFIER_ID
    )

    palette_index = (existing_custom_count + 1) % len(_AUTO_COLOR_PALETTE)
    palette = _AUTO_COLOR_PALETTE[palette_index]
    model.border_color = _color_from_rgba(palette["border"])
    model.divider_color = _color_from_rgba(palette["divider"])
    model.capture_color = _color_from_rgba(palette["capture"])
    model.guides_color = _color_from_rgba(palette["guides"])


def _state(view_state) -> MagnifierWidgetState:
    return get_magnifier_widget_state(view_state)


def magnifier_enabled(view_state) -> bool:
    return bool(_state(view_state).enabled)


def set_magnifier_enabled_flag(view_state, enabled: bool) -> None:
    _state(view_state).enabled = bool(enabled)
    setattr(view_state, "overlay_enabled", bool(enabled))


def active_magnifier_id(view_state) -> str | None:
    return _state(view_state).active_id


def set_active_magnifier_id(view_state, magnifier_id: str | None) -> None:
    _state(view_state).active_id = magnifier_id


def default_magnifier_size(view_state) -> float:
    return float(_state(view_state).default_size_relative)


def set_default_magnifier_size(view_state, value: float) -> None:
    _state(view_state).default_size_relative = float(value)


def default_capture_size(view_state) -> float:
    return float(_state(view_state).default_capture_size_relative)


def active_or_default_divider_visible(view_state) -> bool:
    state = _state(view_state)
    active = state.models.get(state.active_id or DEFAULT_MAGNIFIER_ID)
    if active is not None:
        return bool(active.divider_visible)
    return bool(state.default_divider_visible)


def active_or_default_divider_thickness(view_state) -> int:
    state = _state(view_state)
    active = state.models.get(state.active_id or DEFAULT_MAGNIFIER_ID)
    if active is not None:
        return int(active.divider_thickness)
    return int(state.default_divider_thickness)


def active_or_default_divider_color(view_state):
    state = _state(view_state)
    active = state.models.get(state.active_id or DEFAULT_MAGNIFIER_ID)
    return active.divider_color if active is not None else state.default_divider_color


def active_or_default_border_color(view_state):
    state = _state(view_state)
    active = state.models.get(state.active_id or DEFAULT_MAGNIFIER_ID)
    return active.border_color if active is not None else state.default_border_color


def set_default_capture_size(view_state, value: float) -> None:
    _state(view_state).default_capture_size_relative = float(value)


def _ensure_collection(view_state):
    return _state(view_state).models


def iter_magnifier_models(view_state, render_config) -> list[MagnifierModel]:
    raw_models = _state(view_state).models
    models: list[MagnifierModel] = []
    active_id = active_magnifier_id(view_state) or DEFAULT_MAGNIFIER_ID

    for magnifier_id, model in raw_models.items():
        if model is None:
            continue
        models.append(_copy_model(model, magnifier_id=magnifier_id))

    models.sort(key=lambda model: (model.id != active_id, model.id))
    return models


def add_magnifier_model(
    view_state, render_config, magnifier_id: str | None = None, position=None
) -> MagnifierModel:
    models = _ensure_collection(view_state)
    state = _state(view_state)
    target_id = magnifier_id or DEFAULT_MAGNIFIER_ID
    model = _build_default_magnifier_model(view_state, render_config, target_id)
    if position is not None:
        model.position = position
    model.visible = True
    _apply_auto_instance_color(state, model)
    models[target_id] = model
    set_active_magnifier_id(view_state, target_id)
    return model


def update_magnifier_model(
    view_state, render_config, magnifier_id: str, **updates
) -> MagnifierModel | None:
    models = _ensure_collection(view_state)
    if magnifier_id not in models:
        if magnifier_id != DEFAULT_MAGNIFIER_ID:
            return None
        models[magnifier_id] = _build_default_magnifier_model(
            view_state, render_config, magnifier_id
        )

    model = models[magnifier_id]
    for key, value in updates.items():
        if hasattr(model, key):
            setattr(model, key, value)
    models[magnifier_id] = model
    return model


def set_magnifier_model_visibility(
    view_state, render_config, magnifier_id: str | None, visible: bool
) -> MagnifierModel | None:
    target_id = magnifier_id or active_magnifier_id(view_state) or DEFAULT_MAGNIFIER_ID
    model = update_magnifier_model(
        view_state,
        render_config,
        target_id,
        visible=bool(visible),
    )
    models = _state(view_state).models
    if model is not None and target_id == DEFAULT_MAGNIFIER_ID and len(models) <= 1:
        set_magnifier_enabled_flag(view_state, visible)
    return model


def remove_magnifier_model(view_state, render_config, magnifier_id: str) -> None:
    models = _ensure_collection(view_state)
    models.pop(magnifier_id, None)
    if active_magnifier_id(view_state) == magnifier_id:
        set_active_magnifier_id(view_state, next(iter(models.keys()), None))
        if active_magnifier_id(view_state) is None:
            set_magnifier_enabled_flag(view_state, False)

