from dataclasses import dataclass, field

from core.state_management.action_base import Action, ActionType

@dataclass
class SetLanguageAction(Action):
    language: str
    type: str = field(default=ActionType.SET_LANGUAGE.value, kw_only=True)
    def get_payload(self): return {"language": self.language}

@dataclass
class SetUIModeAction(Action):
    mode: str
    type: str = field(default=ActionType.SET_UI_MODE.value, kw_only=True)
    def get_payload(self): return {"mode": self.mode}

@dataclass
class SetAutoCropBlackBordersAction(Action):
    enabled: bool
    type: str = field(default=ActionType.SET_AUTO_CROP_BLACK_BORDERS.value, kw_only=True)
    def get_payload(self): return {"enabled": self.enabled}

@dataclass
class SetThemeAction(Action):
    theme: str
    type: str = field(default=ActionType.SET_THEME.value, kw_only=True)
    def get_payload(self): return {"theme": self.theme}

@dataclass
class SetUIFontModeAction(Action):
    mode: str
    type: str = field(default=ActionType.SET_UI_FONT_MODE.value, kw_only=True)
    def get_payload(self): return {"mode": self.mode}

@dataclass
class SetUIFontFamilyAction(Action):
    family: str
    type: str = field(default=ActionType.SET_UI_FONT_FAMILY.value, kw_only=True)
    def get_payload(self): return {"family": self.family}

@dataclass
class SetUIScaleFactorAction(Action):
    factor: float
    type: str = field(default=ActionType.SET_UI_SCALE_FACTOR.value, kw_only=True)
    def __post_init__(self):
        super().__post_init__()
        self.factor = float(self.factor)
    def get_payload(self): return {"factor": self.factor}

@dataclass
class SetDebugModeEnabledAction(Action):
    enabled: bool
    type: str = field(default=ActionType.SET_DEBUG_MODE_ENABLED.value, kw_only=True)
    def get_payload(self): return {"enabled": self.enabled}

@dataclass
class SetSystemNotificationsEnabledAction(Action):
    enabled: bool
    type: str = field(default=ActionType.SET_SYSTEM_NOTIFICATIONS_ENABLED.value, kw_only=True)
    def get_payload(self): return {"enabled": self.enabled}

@dataclass
class SetVideoRecordingFpsAction(Action):
    fps: int
    type: str = field(default=ActionType.SET_VIDEO_RECORDING_FPS.value, kw_only=True)
    def get_payload(self): return {"fps": self.fps}

@dataclass
class SetRhiBackendAction(Action):
    backend: str
    type: str = field(default=ActionType.SET_RHI_BACKEND.value, kw_only=True)
    def __post_init__(self):
        super().__post_init__()
        self.backend = (self.backend or "default").strip().lower() or "default"
    def get_payload(self): return {"backend": self.backend}

@dataclass
class SetWindowWasMaximizedAction(Action):
    was_maximized: bool
    type: str = field(default=ActionType.SET_WINDOW_WAS_MAXIMIZED.value, kw_only=True)
    def get_payload(self): return {"was_maximized": self.was_maximized}

@dataclass
class SetWindowGeometryAction(Action):
    x: int
    y: int
    width: int
    height: int
    type: str = field(default=ActionType.SET_WINDOW_GEOMETRY.value, kw_only=True)
    def get_payload(self): return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

@dataclass
class SetExportFavoriteDirAction(Action):
    path: str
    type: str = field(default=ActionType.SET_EXPORT_FAVORITE_DIR.value, kw_only=True)
    def get_payload(self): return {"path": self.path}

@dataclass
class SetExportCropToImageAction(Action):
    enabled: bool
    type: str = field(default=ActionType.SET_EXPORT_CROP_TO_IMAGE.value, kw_only=True)
    def get_payload(self): return {"enabled": self.enabled}

@dataclass
class SetKeyboardOverridesAction(Action):
    overrides: dict[str, str]
    type: str = field(default=ActionType.SET_KEYBOARD_OVERRIDES.value, kw_only=True)
    def __post_init__(self):
        super().__post_init__()
        self.overrides = dict(self.overrides)
    def get_payload(self): return {"overrides": dict(self.overrides)}
