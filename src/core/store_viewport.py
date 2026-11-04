from __future__ import annotations

import copy
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Set

from domain.types import Color, Point, Rect
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

@dataclass
class RenderConfig:
    interpolation_method: str = "NEAREST"
    zoom_interpolation_method: str = "NEAREST"
    movement_interpolation_method: str = "NEAREST"
    interactive_movement_interpolation_method: str = "NEAREST"
    jpeg_quality: int = 95

    include_file_names_in_saved: bool = False
    font_size_percent: int = 120
    font_weight: int = 0
    text_alpha_percent: int = 100
    file_name_color: Color = field(default_factory=lambda: Color(255, 0, 0, 255))
    file_name_bg_color: Color = field(default_factory=lambda: Color(0, 0, 0, 255))
    draw_text_background: bool = True
    text_placement_mode: str = "edges"
    max_name_length: int = 50

    def clone(self):
        return copy.copy(self)

    def to_dict(self) -> dict:
        return {
            "interpolation_method": self.interpolation_method,
            "zoom_interpolation_method": self.zoom_interpolation_method,
            "movement_interpolation_method": self.movement_interpolation_method,
            "interactive_movement_interpolation_method": self.interactive_movement_interpolation_method,
            "jpeg_quality": self.jpeg_quality,
            "include_file_names_in_saved": self.include_file_names_in_saved,
            "font_size_percent": self.font_size_percent,
            "font_weight": self.font_weight,
            "text_alpha_percent": self.text_alpha_percent,
            "file_name_color": (
                self.file_name_color.r,
                self.file_name_color.g,
                self.file_name_color.b,
                self.file_name_color.a,
            ),
            "file_name_bg_color": (
                self.file_name_bg_color.r,
                self.file_name_bg_color.g,
                self.file_name_bg_color.b,
                self.file_name_bg_color.a,
            ),
            "draw_text_background": self.draw_text_background,
            "text_placement_mode": self.text_placement_mode,
            "max_name_length": self.max_name_length,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "RenderConfig":
        cfg = cls()
        if not data:
            return cfg

        def _color(value, default: Color) -> Color:
            from domain.qt_adapters import ensure_visible_color

            if value is None:
                return default
            if isinstance(value, Color):
                return ensure_visible_color(value, fallback=default)
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                a = int(value[3]) if len(value) > 3 else 255
                return ensure_visible_color(
                    (int(value[0]), int(value[1]), int(value[2]), a),
                    fallback=default,
                )
            if isinstance(value, dict):
                return ensure_visible_color(
                    (
                        int(value.get("r", default.r)),
                        int(value.get("g", default.g)),
                        int(value.get("b", default.b)),
                        int(value.get("a", default.a)),
                    ),
                    fallback=default,
                )
            return default

        for key in (
            "interpolation_method",
            "zoom_interpolation_method",
            "movement_interpolation_method",
            "interactive_movement_interpolation_method",
            "text_placement_mode",
        ):
            if key in data and data[key] is not None:
                setattr(cfg, key, str(data[key]))
        for key in (
            "jpeg_quality",
            "font_size_percent",
            "font_weight",
            "text_alpha_percent",
            "max_name_length",
        ):
            if key in data and data[key] is not None:
                setattr(cfg, key, int(data[key]))
        for key in ("include_file_names_in_saved", "draw_text_background"):
            if key in data and data[key] is not None:
                setattr(cfg, key, bool(data[key]))
        if "file_name_color" in data:
            cfg.file_name_color = _color(data["file_name_color"], cfg.file_name_color)
        if "file_name_bg_color" in data:
            cfg.file_name_bg_color = _color(
                data["file_name_bg_color"], cfg.file_name_bg_color
            )
        return cfg

class SessionData:
    """Generic container for a tab's per-session, non-``state_slots`` data.

    ``image_state``/``render_cache`` are opaque to ``core`` — the concrete
    shapes are owned by whichever tab supplies them, via
    ``create_session_data(tab_name)`` (see below), never imported here.
    The bare-construction default is ``None`` for both; any cross-session
    code that reads these fields must tolerate ``None`` for session types
    that don't opt in (see ``register_session_data_factory``).
    """

    __slots__ = ("_image_state", "_render_cache")

    def __init__(
        self,
        image_state: Optional[Any] = None,
        render_cache: Optional[Any] = None,
    ):
        self._image_state = image_state
        self._render_cache = render_cache

    @property
    def image_state(self) -> Optional[Any]:
        return self._image_state

    @image_state.setter
    def image_state(self, val):
        self._image_state = val

    @property
    def render_cache(self) -> Optional[Any]:
        return self._render_cache

    @render_cache.setter
    def render_cache(self, val):
        self._render_cache = val

    def clone(self):
        return SessionData(
            image_state=self._image_state.clone() if self._image_state is not None else None,
            render_cache=self._render_cache.clone() if self._render_cache is not None else None,
        )

_session_data_factories: dict[str, Callable[[], Optional["SessionData"]]] = {}

def register_session_data_factory(
    session_type: str, factory: Callable[[], Optional["SessionData"]]
) -> None:
    """Let a tab supply its own default `SessionData` for its session type.

    `ImageSessionState`/`RenderCacheState` are comparison-tab-specific
    (image1/image2, PSNR/SSIM, diff caches); `core` should not hardcode them
    as the default for every session type. Tabs register a zero-arg factory
    (typically a bound method) during `TabRegistry.discover()`; a factory
    that returns `None` falls back to the generic default below.
    """
    _session_data_factories[session_type] = factory

def create_session_data(session_type: Optional[str]) -> "SessionData":
    factory = _session_data_factories.get(session_type) if session_type else None
    if factory is not None:
        result = factory()
        if result is not None:
            return result
    return SessionData()

@dataclass
class GeometryState:
    pixmap_width: int = 0
    pixmap_height: int = 0
    image_display_rect_on_label: Rect = field(default_factory=lambda: Rect())
    fixed_label_width: Optional[int] = None
    fixed_label_height: Optional[int] = None

    active_overlay_screen_center: Point = field(default_factory=lambda: Point())
    active_overlay_screen_size: int = 0

    loaded_geometry: bytes = b""
    loaded_was_maximized: bool = False
    loaded_previous_geometry: bytes = b""
    loaded_debug_mode_enabled: bool = False

    def clone(self):
        return copy.copy(self)

@dataclass
class InteractionState:
    resize_in_progress: bool = False

    is_interactive_mode: bool = False
    is_dragging_split_line: bool = False
    is_dragging_overlay_handle: bool = False
    is_dragging_overlay_split: bool = False
    is_dragging_any_slider: bool = False
    interaction_session_id: int = 0
    is_user_interacting: bool = False
    pressed_keys: Set[int] = field(default_factory=set)
    last_horizontal_movement_key: int | None = None
    last_vertical_movement_key: int | None = None
    last_spacing_movement_key: int | None = None
    space_bar_pressed: bool = False
    interactive_offset_relative_visual: Point = field(
        default_factory=lambda: Point(0.0, 0.0)
    )
    interactive_spacing_relative_visual: float = 0.1
    interactive_internal_split_visual: float = 0.5

    def clone(self):
        new_obj = copy.copy(self)
        new_obj.pressed_keys = set(self.pressed_keys)
        return new_obj

@dataclass
class ViewState:
    split_position: float = 0.5
    split_position_visual: float = 0.5
    is_horizontal: bool = False

    diff_mode: str = "off"
    channel_view_mode: str = "RGB"

    canvas_widget_state: dict[str, Any] = field(default_factory=dict)
    optimize_interactive_movement: bool = True
    overlay_enabled: bool = False

    highlighted_overlay_element: Optional[str] = None

    showing_single_image_mode: int = 0
    movement_speed_per_sec: float = 2.0
    text_bg_visual_height: float = 0.0
    text_bg_visual_width: float = 0.0

    def clone(self):
        # Long-term Store lock fix: avoid heavy deepcopy inside the critical
        # section. ``canvas_widget_state`` values are immutable feature states,
        # so a shallow dict copy is sufficient and keeps the prepare step
        # cheap. Deepcopy (if ever needed) is done outside Dispatcher._lock.
        new_obj = copy.copy(self)
        new_obj.canvas_widget_state = dict(self.canvas_widget_state)
        return new_obj

class ViewportState:
    __slots__ = (
        "_render_config",
        "_session_data",
        "_view_state",
        "_interaction_state",
        "_geometry_state",
    )

    def __init__(
        self,
        render_config: Optional[RenderConfig] = None,
        session_data: Optional[SessionData] = None,
        view_state: Optional[ViewState] = None,
        interaction_state: Optional[InteractionState] = None,
        geometry_state: Optional[GeometryState] = None,
    ):
        self._render_config = render_config or RenderConfig()
        self._session_data = session_data or SessionData()
        self._view_state = view_state or ViewState()
        self._interaction_state = interaction_state or InteractionState()
        self._geometry_state = geometry_state or GeometryState()

    @property
    def render_config(self) -> RenderConfig:
        return self._render_config

    @render_config.setter
    def render_config(self, val):
        self._render_config = val

    @property
    def session_data(self) -> SessionData:
        return self._session_data

    @session_data.setter
    def session_data(self, val):
        self._session_data = val

    @property
    def view_state(self) -> ViewState:
        return self._view_state

    @view_state.setter
    def view_state(self, val):
        self._view_state = val

    @property
    def interaction_state(self) -> InteractionState:
        return self._interaction_state

    @interaction_state.setter
    def interaction_state(self, val):
        self._interaction_state = val

    @property
    def geometry_state(self) -> GeometryState:
        return self._geometry_state

    @geometry_state.setter
    def geometry_state(self, val):
        self._geometry_state = val

    def clone(self):
        return ViewportState(
            render_config=self._render_config.clone(),
            session_data=self._session_data.clone(),
            view_state=self._view_state.clone(),
            interaction_state=self._interaction_state.clone(),
            geometry_state=self._geometry_state.clone(),
        )

    def freeze_for_export(self):
        frozen = self.clone()
        frozen.interaction_state.space_bar_pressed = False
        frozen.interaction_state.pressed_keys = set()
        frozen.interaction_state.is_dragging_split_line = False
        frozen.interaction_state.is_dragging_overlay_handle = False
        frozen.interaction_state.is_dragging_overlay_split = False
        frozen.interaction_state.is_dragging_any_slider = False
        frozen.interaction_state.is_interactive_mode = False
        frozen.interaction_state.is_user_interacting = False
        return frozen