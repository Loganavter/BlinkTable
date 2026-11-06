from __future__ import annotations

from dataclasses import dataclass, field

from .composition import CompositionNode, CompositionPlan

@dataclass(frozen=True)
class CaptureCircle:
    center: object
    radius: float
    color: object

@dataclass(frozen=True)
class GuideSet:
    capture_center: object
    capture_radius: float
    target_centers: tuple[object, ...]
    target_radii: tuple[float, ...]
    color: object

@dataclass(frozen=True)
class OverlaySlot:
    center: object
    radius: float
    uv_rect: tuple[float, float, float, float]
    uv_rect2: tuple[float, float, float, float]
    source: int
    is_combined: bool
    internal_split: float
    horizontal: bool
    divider_visible: bool
    divider_color: tuple[float, float, float, float]
    divider_thickness_uv: float
    border_color: object
    border_width: float

@dataclass(frozen=True)
class OverlayLayout:
    slots: tuple[OverlaySlot, ...] = field(default_factory=tuple)
    capture_circles: tuple[CaptureCircle, ...] = field(default_factory=tuple)
    guide_sets: tuple[GuideSet, ...] = field(default_factory=tuple)
    capture_center: object | None = None
    capture_radius: float = 0.0
    overlay_centers: tuple[tuple[float, float], ...] = field(default_factory=tuple)
    overlay_radius: float = 0.0
    border_color: object | None = None
    border_width: float = 2.0
    channel_mode: int = 0
    diff_mode: int = 0
    interp_mode: int = 1

@dataclass(frozen=True)
class CanvasRenderPlan:
    """
    Immutable frame descriptor — everything needed to render one GL canvas frame.

    ``canvas_w`` × ``canvas_h`` are the dimensions of the uploaded image pair.
    Feature-owned overlay geometry is stored in canvas-px and converted to
    widget-px only inside the shared runtime-overlay applicator.
    """

    image1: object
    image2: object
    source_image1: object
    source_image2: object
    source_key: tuple
    canvas_w: int
    canvas_h: int
    render_scene: object
    overlay_layout: OverlayLayout | None
    capture_visible: bool
    capture_color: object
    guides_enabled: bool
    guides_color: object
    guides_thickness: int
    fill_rgba: tuple[int, int, int, int] | None = None
    display_cache_key: tuple | None = None
    output_scale: float = 1.0
    preserve_zoom: bool = False
    image_is_padded_composite: bool = False
    """
    Set by the plan builder (never inferred downstream). ``True`` means
    ``image1``/``image2`` already have virtual-canvas padding baked into
    their pixels (dimensions == ``canvas_w x canvas_h``). ``False`` (default)
    means they are the raw, unpadded source pair.
    """
    geometry_letterbox: bool = False
    """
    Export / video snapshot prepare: ``canvas_w/h`` is the authoritative
    padded framebuffer and unpadded images are placed via
    ``overlay_clip_rect`` + shader letterbox. Must stay ``False`` on the
    live main-window canvas — live may set ``overlay_clip_rect`` for
    uncrop/magnifier without making the canvas the letterbox owner.
    """
    envelope_dims: tuple[int, int, int, int] | None = None
    """
    The comparison envelope the canvas basis was sized from, in SOURCE space:
    ``(w1, h1, w2, h2)`` of each side's own decoded pixels (not the installed
    display tier). Set by ``plan_builder.build_canvas_plan`` on the live
    default plan only; every downstream owner that fits the pair
    (``display_tier_service.update_common_letterbox_geometry`` /
    ``refresh_live_content_rect``) consumes it instead of re-deriving the
    envelope from ``image1``/``image2``.

    Why source space: each preview is normalised per side to a 1024 long edge,
    so the envelope of a PREVIEW pair has a different aspect than the envelope
    of the source pair (2000x1400 = 1.43 vs 1024x1024 = 1.0). Fitting the
    canvas/split/loupe to the preview aspect and then re-fitting when the
    unified store lands is exactly the "comparison jumps for a frame or two on
    a pair replace" report (2026-10-27).

    ``None`` (default) keeps today's behaviour bit-for-bit: export/snapshot
    plans and an active crop box (the box-clipped display dims are the truth
    there). A frame whose basis cannot be stated consistently sets
    ``envelope_hold`` instead (see below) — it never carries a mix.
    """
    envelope_hold: bool = False
    """
    This frame's pair envelope is a PREVIEW guess on one side, so no consistent
    envelope can be stated: hold the last presented frame instead of applying.

    A preview is normalised per side to a 1024 long edge, so
    ``max(w1,w2) x max(h1,h2)`` over ``(preview1, source2)`` describes neither
    pair (measured live 2026-10-27: canvas ``[1024,1400]`` between two settled
    ``[1600,1400]`` frames while a replaced slot's source tier was still
    decoding). The live apply (``presentation/surface.apply_store_to_canvas``)
    keeps the previously presented frame and its overlays untouched; a held
    frame repeats byte-identically (no re-fit, no geometry change). A blank
    frame is preferable to one that describes neither pair, and the state is
    inherently transient (a preview is installed only until the side's own
    pixels land).

    Set only by ``plan_builder.build_canvas_plan`` on the live default plan;
    ``False`` for export/snapshot plans, crop-box plans, and any frame where
    both sides have a real basis (own pixels or an already-real display).
    """
    composition_root: CompositionNode | None = None
    """
    Optional composition tree describing N-layer frames (multi-compare grid,
    future scene-graph workspaces). Legacy 2-image comparison plans leave
    this ``None`` and the applicator uses the ``image1`` / ``image2`` path
    unchanged. When set, the applicator routes through composition-aware
    handlers; the legacy fields hold a single-image placeholder so existing
    code paths that read them remain safe.
    """
    composition_plan: CompositionPlan | None = None
    """
    Optional source ``CompositionPlan`` (see composition.py), carried through
    verbatim. When set, the applicator uses it as-is instead of rebuilding a
    ``CompositionPlan`` from ``composition_root``/``canvas_w``/``canvas_h``/
    ``fill_rgba`` alone — that piecemeal rebuild silently drops any field the
    original plan had beyond those four (e.g. multi-compare's
    ``divider_settings``/``label_settings``), which is exactly what starved
    the offscreen exporter of divider/label styling despite the live widget
    (which builds and keeps its own ``CompositionPlan`` directly) rendering
    correctly. Untyped as ``object`` to avoid a hard dependency here; callers
    that set it must pass a real ``CompositionPlan``.
    """

def plan_owns_overlay_state(plan) -> bool:
    """``plan.overlay_layout is not None`` ⇒ the plan owns the overlay state.

    A recorded/snapshot plan (export, video-preview) carries the overlay
    geometry it was rendered with: its loupe slots, diff mode, capture
    circles, guide sets and guide/capture params. Applying such a plan must
    not run the live overlay recompute — that path re-derives the overlay
    from the *live* store and, when it cannot build a layout, nulls what the
    plan carried (``scene_update.rebuild_magnifier_overlay``).

    A plan WITHOUT an overlay layout (the overlay was off when it was built)
    keeps the live recompute exactly as before. Shared by the engine plan
    applicator and the tab's magnifier live rebuild, so both sides read the
    same rule; ``src/engine`` may import ``ui.*`` (the seal forbids only
    ``tabs.*`` / ``plugins.*`` / ``core.store``).
    """
    return getattr(plan, "overlay_layout", None) is not None


def resolve_plan_logical_image_rect(
    plan: CanvasRenderPlan,
) -> tuple[int, int, int, int]:
    clip_rect = getattr(plan.render_scene, "overlay_clip_rect", None)
    if clip_rect is not None:
        clip_x, clip_y, clip_w, clip_h = clip_rect
        return (
            int(clip_x),
            int(clip_y),
            max(1, int(clip_w)),
            max(1, int(clip_h)),
        )
    return (0, 0, max(1, int(plan.canvas_w)), max(1, int(plan.canvas_h)))
