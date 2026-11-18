from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtGui import QColor

from ui.canvas_infra.scene.pass_contract import (
    CanvasRenderPass,
    SceneVisibility,
    is_single_image_preview_scene,
)
from ui.canvas_infra.scene.stacking_policy import CanvasStackRole
from engine.overlay import (
    DIVIDER_UNIFORM_SIZE,
    OverlayBinding,
    OverlayShell,
    RectItem,
    Rgba,
    SemanticOverlayItems,
    SemanticResolver,
    pack_divider,
)
from tabs.image_compare.canvas.rhi_feature_common import scissor_from_widget_rect
from tabs.image_compare.canvas.features.divider.overlay_model import (
    content_split_visual,
    resolve_divider_spec,
)

_SHADER_DIR = Path(__file__).resolve().parent / "shaders"

BINDING = OverlayBinding(
    shader_dir=_SHADER_DIR,
    shader_stem="divider",
    uniform_size=DIVIDER_UNIFORM_SIZE,
)


def _content_split_visual(ctx) -> float:
    return content_split_visual(ctx)


def _semantic_resolver(widget) -> SemanticResolver:
    """Ctx-first semantic resolver: ctx in, canvas-px items out.

    Geometry (bar + clip rects, already view-transformed to canvas-px by
    ``resolve_divider_spec``) arrives as engine-typed items; byte layout
    lives shell-side in ``pack_divider``.
    """

    def resolve(ctx: Any) -> SemanticOverlayItems:
        spec = resolve_divider_spec(ctx, widget)
        color = Rgba(spec.red, spec.green, spec.blue, spec.alpha)
        half = spec.thickness_px * 0.5
        if spec.is_horizontal:
            bar = RectItem(
                x=spec.clip_x,
                y=spec.position_px - half,
                w=spec.clip_w,
                h=spec.thickness_px,
                color=color,
            )
        else:
            bar = RectItem(
                x=spec.position_px - half,
                y=spec.clip_y,
                w=spec.thickness_px,
                h=spec.clip_h,
                color=color,
            )
        clip = RectItem(
            x=spec.clip_x,
            y=spec.clip_y,
            w=spec.clip_w,
            h=spec.clip_h,
            color=Rgba(0.0, 0.0, 0.0, 0.0),
        )
        return (bar, clip)

    return resolve


class DividerPass(CanvasRenderPass):
    stack_role = CanvasStackRole.UNDERLAY_SPLIT
    visibility = SceneVisibility.ALL

    def __init__(self) -> None:
        self._shell = OverlayShell(DIVIDER_UNIFORM_SIZE)

    @staticmethod
    def _resolve_divider_state(widget, ctx):
        # Stateless decision layer (overlay_model); this shim preserves the
        # historical tuple shape for should_paint/prepare below.
        spec = resolve_divider_spec(ctx, widget)
        return (
            spec.show,
            spec.position_px,
            spec.thickness_px,
            spec.is_horizontal,
            QColor.fromRgbF(spec.red, spec.green, spec.blue, spec.alpha),
            (spec.clip_x, spec.clip_y, spec.clip_w, spec.clip_h),
        )

    def initialize(self, rhi, target) -> None:
        self._shell.mount(rhi, target, BINDING.shader_dir, BINDING.shader_stem)

    def should_paint(self, ctx) -> bool:
        show, _position, thickness, _horizontal, _color, _clip = self._resolve_divider_state(
            ctx.widget, ctx
        )
        if is_single_image_preview_scene(ctx):
            return False
        images_uploaded = list(getattr(ctx, "images_uploaded", ()) or ())
        content_rect = getattr(ctx.scene_frame, "content_rect_px", None)
        return bool(
            show
            and thickness > 0.0
            and len(images_uploaded) >= 2
            and images_uploaded[0]
            and images_uploaded[1]
            and content_rect is not None
            and len(content_rect) >= 4
            and content_rect[2] > 0
            and content_rect[3] > 0
        )

    def prepare(self, widget, ctx, resource_updates) -> None:
        matrix = self._shell.clip_matrix()
        items = _semantic_resolver(widget)(ctx)
        blocks = pack_divider(items, matrix, float(ctx.width), float(ctx.height))
        self._shell.stage_packed(resource_updates, blocks)

    def record(self, command_buffer, widget, ctx) -> None:
        target_size = widget.renderTarget().pixelSize()
        # Full-target scissor only — content clip lives in the fragment shader
        # (src/tabs/image_compare/docs/investigations/divider-zoom-pan-detach.md).
        scissor = scissor_from_widget_rect(
            widget, self._shell.rhi, ctx, 0.0, 0.0, float(ctx.width), float(ctx.height)
        )
        self._shell.draw(
            command_buffer,
            width=float(target_size.width()),
            height=float(target_size.height()),
            scissor=scissor,
            count=1,
        )

    def release(self) -> None:
        self._shell.teardown()


RENDER_PASSES: list[CanvasRenderPass] = [DividerPass()]
RENDER_PASSES = RENDER_PASSES
