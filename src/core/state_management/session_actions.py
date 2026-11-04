from dataclasses import dataclass, field
from typing import Any, Optional

from core.state_management.action_base import Action, ActionType

@dataclass
class SetImageSessionImageAction(Action):
    slot: int
    image: Any
    type: str = field(default=ActionType.SET_IMAGE_SESSION_IMAGE.value, kw_only=True)

    def get_payload(self):
        return {"slot": self.slot, "image": self.image}

@dataclass
class SetUnificationInProgressAction(Action):
    enabled: bool
    type: str = field(default=ActionType.SET_UNIFICATION_IN_PROGRESS.value, kw_only=True)

    def get_payload(self):
        return {"enabled": self.enabled}

@dataclass
class SetPendingUnificationPathsAction(Action):
    paths: Optional[tuple[str, str]]
    type: str = field(default=ActionType.SET_PENDING_UNIFICATION_PATHS.value, kw_only=True)

    def get_payload(self):
        return {"paths": self.paths}

@dataclass
class SetZoomInterpolationMethodAction(Action):
    method: str
    type: str = field(default=ActionType.SET_ZOOM_INTERPOLATION_METHOD.value, kw_only=True)

    def get_payload(self):
        return {"method": self.method}

@dataclass
class SetAutoCalculatePsnrAction(Action):
    enabled: bool
    type: str = field(default=ActionType.SET_AUTO_CALCULATE_PSNR.value, kw_only=True)

    def get_payload(self):
        return {"enabled": self.enabled}

@dataclass
class SetAutoCalculateSsimAction(Action):
    enabled: bool
    type: str = field(default=ActionType.SET_AUTO_CALCULATE_SSIM.value, kw_only=True)

    def get_payload(self):
        return {"enabled": self.enabled}

@dataclass
class SetPsnrValueAction(Action):
    value: Optional[float]
    type: str = field(default=ActionType.SET_PSNR_VALUE.value, kw_only=True)

    def get_payload(self):
        return {"value": self.value}

@dataclass
class SetSsimValueAction(Action):
    value: Optional[float]
    type: str = field(default=ActionType.SET_SSIM_VALUE.value, kw_only=True)

    def get_payload(self):
        return {"value": self.value}
