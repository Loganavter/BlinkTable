from __future__ import annotations

from dataclasses import dataclass, field

from tabs.image_compare.canvas.style_tokens import (
    CanvasStyleTokens,
    DEFAULT_CANVAS_STYLE_TOKENS,
)
from ui.canvas_presentation.label_style import FilenameOverlayStyle
from ui.canvas_infra.rhi.render_metrics import resolve_font_px, resolve_view_px

# Phase 2 canvas-only overlay: DragDropOverlayPass (TransientPreview, HUD 50)
# is registered via ``canvas/features/drag_drop_overlay/passes.py:RENDER_PASSES``
# and auto-discovered by ``registry().get_render_passes()`` — no explicit list
# here. Stacking is via ``CanvasStackRole.TRANSIENT_PREVIEW`` (after base_images,
# before HUD_LABEL), so tiles composite over the tiled image but under labels.
# The pass sets ``requires_content = False`` so empty-canvas drop hints still show.


@dataclass(frozen=True, slots=True)
class SceneFrame:
    feature_payloads: dict[str, object] = field(default_factory=dict)
    blank_white: bool = False
    single_image_preview: int = 0
    clip_overlays_to_image_bounds: bool = False
    is_horizontal: bool = False
    split_position_visual: float = 0.5
    overlay_clip_rect: tuple[int, int, int, int] | None = None
    channel_mode_int: int = 0
    diff_mode_active: bool = False
    diff_mode_int: int = 0
    zoom_interpolation_method: str = "BILINEAR"
    content_rect_px: tuple[int, int, int, int] | None = None
    image_rect_px: tuple[int, int, int, int] | None = None
    split_override: float | None = None


@dataclass(frozen=True, slots=True)
class ResolvedCanvasStyle:
    annotation_ring_stroke_px: float
    annotation_line_stroke_px: float
    annotation_arc_stroke_px: float
    annotation_selection_stroke_px: float
    filename_overlay: FilenameOverlayStyle


@dataclass(frozen=True, slots=True)
class BaseImagePrimitive:
    split_position: float
    is_horizontal: bool
    zoom: float
    pan_offset_x: float
    pan_offset_y: float
    diff_mode_int: int
    channel_mode_int: int
    use_hires: bool
    letterbox1: tuple[float, float, float, float]
    letterbox2: tuple[float, float, float, float]
    canvas_letterbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    letterbox_fill: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class RenderList:
    base_image: BaseImagePrimitive | None = None


def resolve_canvas_style(
    scene_frame: SceneFrame,
    render_metrics,
    tokens: CanvasStyleTokens = DEFAULT_CANVAS_STYLE_TOKENS,
) -> ResolvedCanvasStyle:
    overlay_cfg = (
        scene_frame.feature_payloads.get("filename_overlay")
        if isinstance(scene_frame.feature_payloads, dict)
        else None
    )
    raw_font_pct = getattr(overlay_cfg, "font_size_percent", None)
    font_scale = max(
        0.01,
        float(raw_font_pct if raw_font_pct is not None else 100) / 100.0,
    )
    raw_alpha_pct = getattr(overlay_cfg, "text_alpha_percent", None)
    text_alpha = max(
        0.05,
        min(
            1.0,
            float(raw_alpha_pct if raw_alpha_pct is not None else 100) / 100.0,
        ),
    )
    return ResolvedCanvasStyle(
        annotation_ring_stroke_px=resolve_view_px(
            tokens.capture_ring_stroke_du, render_metrics
        ),
        annotation_line_stroke_px=resolve_view_px(
            tokens.guides_stroke_du, render_metrics
        ),
        annotation_arc_stroke_px=resolve_view_px(
            tokens.occluded_arc_stroke_du, render_metrics
        ),
        annotation_selection_stroke_px=resolve_view_px(
            tokens.hidden_selection_stroke_du, render_metrics
        ),
        filename_overlay=FilenameOverlayStyle(
            font_pixel_size=resolve_font_px(
                float(
                    getattr(
                        overlay_cfg,
                        "font_base_pixel_size",
                        tokens.filename_font_base_du,
                    )
                )
                * font_scale,
                render_metrics,
            ),
            label_safe_gap_px=resolve_view_px(
                tokens.filename_label_safe_gap_du, render_metrics
            ),
            label_padding_x_px=resolve_view_px(
                tokens.filename_label_padding_x_du, render_metrics
            ),
            label_padding_y_px=resolve_view_px(
                tokens.filename_label_padding_y_du, render_metrics
            ),
            glyph_overscan_px=resolve_view_px(
                tokens.filename_glyph_overscan_du, render_metrics
            ),
            label_corner_radius_px=resolve_view_px(
                tokens.filename_label_corner_radius_du, render_metrics
            ),
            text_inset_px=resolve_view_px(tokens.filename_text_inset_du, render_metrics),
            text_alpha=text_alpha,
        ),
    )


def build_scene_frame(
    *,
    render_scene,
    content_rect_px: tuple[int, int, int, int] | None,
    image_rect_px: tuple[int, int, int, int] | None = None,
    split_override: float | None = None,
    feature_payloads: dict | None = None,
) -> SceneFrame:
    if feature_payloads is None:
        feature_payloads = dict(getattr(render_scene, "feature_overrides", {}) or {})
    split_position_visual = getattr(render_scene, "split_position_visual", 0.5)
    if split_position_visual is None:
        split_position_visual = 0.5
    return SceneFrame(
        feature_payloads=feature_payloads,
        blank_white=bool(getattr(render_scene, "blank_white", False)),
        single_image_preview=int(getattr(render_scene, "single_image_preview", 0) or 0),
        clip_overlays_to_image_bounds=bool(
            getattr(render_scene, "clip_overlays_to_image_bounds", False)
        ),
        is_horizontal=bool(getattr(render_scene, "is_horizontal", False)),
        split_position_visual=float(split_position_visual),
        overlay_clip_rect=getattr(render_scene, "overlay_clip_rect", None),
        channel_mode_int=int(getattr(render_scene, "channel_mode_int", 0) or 0),
        diff_mode_active=bool(getattr(render_scene, "diff_mode_active", False)),
        diff_mode_int=int(getattr(render_scene, "diff_mode_int", 0) or 0),
        zoom_interpolation_method=str(
            getattr(render_scene, "zoom_interpolation_method", "BILINEAR")
            or "BILINEAR"
        ),
        content_rect_px=content_rect_px,
        image_rect_px=image_rect_px,
        split_override=split_override,
    )


def build_render_list(
    scene_frame: SceneFrame,
    *,
    base_image: BaseImagePrimitive | None,
) -> RenderList:
    return RenderList(base_image=base_image)
