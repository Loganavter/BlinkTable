from core.state_management.action_base import Action, ActionType
from core.state_management.appearance_actions import (
    SetCachedDiffImageAction,
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
from core.state_management.cache_actions import (
    ClearAllCachesAction,
    ClearImageSlotDataAction,
    InvalidateGeometryCacheAction,
    InvalidateRenderCacheAction,
)
from core.state_management.document_actions import (
    SetCropOverrideAction,
    SetCurrentIndexAction,
    SetFullResImageAction,
    SetImagePathAction,
    SetPreviewImageAction,
)
from core.state_management.geometry_actions import (
    SetFixedLabelDimensionsAction,
    SetImageDisplayRectAction,
    SetPixmapDimensionsAction,
)
from core.state_management.interaction_actions import (
    SetDraggingSplitLineAction,
    SetInteractionSessionIdAction,
    SetInteractiveInternalSplitVisualAction,
    SetInteractiveModeAction,
    SetInteractiveOffsetVisualAction,
    SetInteractiveSpacingVisualAction,
    SetLastHorizontalMovementKeyAction,
    SetLastSpacingMovementKeyAction,
    SetLastVerticalMovementKeyAction,
    SetPressedKeysAction,
    SetResizeInProgressAction,
    SetSpaceBarPressedAction,
    SetUserInteractingAction,
)
from core.state_management.session_actions import (
    SetAutoCalculatePsnrAction,
    SetAutoCalculateSsimAction,
    SetImageSessionImageAction,
    SetPendingUnificationPathsAction,
    SetPsnrValueAction,
    SetSsimValueAction,
    SetUnificationInProgressAction,
    SetZoomInterpolationMethodAction,
)
from core.state_management.settings_actions import (
    SetAutoCropBlackBordersAction,
    SetDebugModeEnabledAction,
    SetExportFavoriteDirAction,
    SetLanguageAction,
    SetKeyboardOverridesAction,
    SetRhiBackendAction,
    SetSystemNotificationsEnabledAction,
    SetThemeAction,
    SetUIFontFamilyAction,
    SetUIFontModeAction,
    SetUIScaleFactorAction,
    SetUIModeAction,
    SetVideoRecordingFpsAction,
    SetWindowGeometryAction,
    SetWindowWasMaximizedAction,
)
from core.state_management.viewport_actions import (
    SetCanvasWidgetStateAction,
    SetChannelViewModeAction,
    SetDiffModeAction,
    SetIsDraggingSliderAction,
    SetMovementSpeedAction,
    SetShowingSingleImageModeAction,
    SetSplitPositionAction,
    SetSplitPositionVisualAction,
    ToggleOrientationAction,
)

__all__ = [name for name in globals() if name.endswith("Action")] + ["Action", "ActionType"]