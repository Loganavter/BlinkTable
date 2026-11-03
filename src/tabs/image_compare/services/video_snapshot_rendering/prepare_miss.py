"""Cache-miss prepare path for snapshot frames."""

from __future__ import annotations

import time

from tabs.image_compare.canvas.presentation.plan_builder import (
    CanvasGeometry,
    build_render_frame_presentation,
    build_snapshot_store_presentation,
)
from tabs.image_compare.services.snapshot_render_plan_builder import (
    SnapshotRenderPlanBuilder,
)
from tabs.image_compare.services.video_snapshot_rendering.geometry import (
    cover_source_to_content,
    fit_source_to_content,
    geometry_with_aspect_insets,
    is_thumbnail_target_size,
    resolve_scaled_content_geometry,
)
from tabs.image_compare.services.video_snapshot_rendering.models import (
    ImagePrepCacheEntry,
    PreparedCanvasFrame,
)
from tabs.image_compare.services.video_snapshot_rendering.plan_build import (
    fill_frame_debug,
    trace_plan,
)
from ui.canvas_presentation.layout import compute_content_layout
from ui.canvas_presentation.models import CanvasTarget


def prepare_from_cache_miss(
    snap,
    request,
    img1,
    img2,
    *,
    image_prep_key,
    scaled_global_bounds,
    resize_method: str,
    normalize_snapshot: bool,
    allow_feature_layout_fallback: bool,
    caches,
    debug: dict,
    trace=None,
) -> PreparedCanvasFrame:
    debug["image_prep_cache"] = "miss"
    build_store_started = time.perf_counter()
    presentation = build_snapshot_store_presentation(
        snap,
        img1,
        img2,
        fit_content=request.fit_content,
        global_bounds=scaled_global_bounds,
        fill_color=request.target_surface.fill_rgba,
        resize_method=resize_method,
        normalize_snapshot=normalize_snapshot,
    )
    debug["build_store_ms"] = (time.perf_counter() - build_store_started) * 1000.0
    debug["pair_resize_method"] = resize_method
    if request.fit_content and request.global_bounds is not None:
        debug["global_bounds_pad_left"] = float(request.global_bounds.pad_left)
        debug["global_bounds_pad_right"] = float(request.global_bounds.pad_right)
        debug["global_bounds_pad_top"] = float(request.global_bounds.pad_top)
        debug["global_bounds_pad_bottom"] = float(request.global_bounds.pad_bottom)

    layout_started = time.perf_counter()
    target = CanvasTarget(
        width=max(1, int(request.target_surface.width)),
        height=max(1, int(request.target_surface.height)),
        fill_rgba=request.target_surface.fill_rgba,
    )
    output_layout = compute_content_layout(
        target,
        image_width=presentation.display_image1.width,
        image_height=presentation.display_image1.height,
    )
    frame = build_render_frame_presentation(
        presentation,
        target=target,
    )
    target_size, content_size, pad_left, pad_top = resolve_scaled_content_geometry(frame)
    # A FILMSTRIP TILE IS FULL-BLEED. The strip pass draws one tile STRETCHED
    # over one timeline block cell, and a cell is not 16:9 (measured live
    # 2026-11-03: a 160x90 tile in a 127x72 cell). With the fit-inside content
    # box every frame kept its letterbox bars, so the stretched cell showed a
    # narrow strip of the frame and empty space beside it — the "band renders
    # the wrong pixels" report (a portrait source collapses to 40 of 160 px,
    # i.e. 32 of a 128 px cell). Covering the WHOLE canvas is also what the
    # CPU filmstrip composer already does
    # (``cpu_thumbnail._cover_pil(source, tile_size)`` fills the tile).
    if is_thumbnail_target_size(
        request.target_surface.width, request.target_surface.height
    ):
        content_size = (max(1, int(target_size[0])), max(1, int(target_size[1])))
        pad_left = 0
        pad_top = 0
        # The frame's own layout is the fit-inside one; the plan, the render
        # destination and the debug table must all read this same full-bleed
        # geometry, or a consumer pastes a full-canvas image at the old fit
        # offset.
        output_layout = compute_content_layout(
            target,
            image_width=content_size[0],
            image_height=content_size[1],
        )
    _src1_sz = (
        (presentation.source_image1.width, presentation.source_image1.height)
        if presentation.source_image1
        else None
    )
    _src2_sz = (
        (presentation.source_image2.width, presentation.source_image2.height)
        if presentation.source_image2
        else None
    )
    _disp1_sz = (
        (presentation.display_image1.width, presentation.display_image1.height)
        if presentation.display_image1
        else None
    )
    if trace is not None:
        trace(
            "video.render.layout",
            f"layout target={target_size[0]}x{target_size[1]} content={content_size[0]}x{content_size[1]}",
            {
                "target_surface": (
                    int(request.target_surface.width),
                    int(request.target_surface.height),
                ),
                "source1_size": _src1_sz,
                "source2_size": _src2_sz,
                "display1_size": _disp1_sz,
                "target_size": tuple(int(v) for v in target_size),
                "content_size": tuple(int(v) for v in content_size),
                "pad_left": int(pad_left),
                "pad_top": int(pad_top),
                "fit_content": bool(request.fit_content),
                "global_bounds": repr(request.global_bounds),
                "scaled_global_bounds": repr(scaled_global_bounds),
                "resize_method": resize_method,
            },
        )
    canvas_geometry = CanvasGeometry(
        image_width=max(1, int(content_size[0])),
        image_height=max(1, int(content_size[1])),
        canvas_width=max(1, int(target_size[0])),
        canvas_height=max(1, int(target_size[1])),
        padding_left=max(0, int(pad_left)),
        padding_top=max(0, int(pad_top)),
        padding_right=max(0, int(target_size[0] - content_size[0] - pad_left)),
        padding_bottom=max(0, int(target_size[1] - content_size[1] - pad_top)),
        virtual_layout=frame.virtual_layout,
    )
    # Strip thumbnails (exactly the 160x90 target) render full-bleed cover of
    # the whole canvas so no transparent letterbox pads reach the strip (the
    # content box was already widened to the canvas above); every other target
    # — including all export sizes — keeps fit-inside + aspect pads.
    if is_thumbnail_target_size(
        request.target_surface.width, request.target_surface.height
    ):
        scaled_source1 = cover_source_to_content(
            presentation.source_image1,
            content_size,
            resize_method,
        )
        scaled_source2 = cover_source_to_content(
            presentation.source_image2,
            content_size,
            resize_method,
        )
    else:
        scaled_source1 = fit_source_to_content(
            presentation.source_image1,
            content_size,
            request.target_surface.fill_rgba,
            resize_method,
        )
        scaled_source2 = fit_source_to_content(
            presentation.source_image2,
            content_size,
            request.target_surface.fill_rgba,
            resize_method,
        )
    # Prefer side-1 size; sources are unified so sizes should match.
    fitted_size = (
        (scaled_source1.width, scaled_source1.height)
        if scaled_source1 is not None
        else content_size
    )
    canvas_geometry = geometry_with_aspect_insets(canvas_geometry, fitted_size)

    entry = ImagePrepCacheEntry(
        display_img1=presentation.display_image1,
        display_img2=presentation.display_image2,
        source_img1=presentation.source_image1,
        source_img2=presentation.source_image2,
        source_key=presentation.images.source_key,
        display_cache_key=presentation.images.display_cache_key,
        scaled_source1=scaled_source1,
        scaled_source2=scaled_source2,
        canvas_geometry=canvas_geometry,
        output_layout=output_layout,
        target_size=target_size,
        content_size=content_size,
        pad_left=pad_left,
        pad_top=pad_top,
        render_w=frame.render_width,
        render_h=frame.render_height,
    )
    caches.image_prep = (image_prep_key, entry)

    plan = SnapshotRenderPlanBuilder(frame.store).build_render_plan(
        scaled_source1,
        scaled_source2,
        source_image1=scaled_source1,
        source_image2=scaled_source2,
        source_key=(
            frame.source_key,
            target_size,
            content_size,
            pad_left,
            pad_top,
        ),
        display_cache_key=(
            presentation.display_cache_key,
            target_size,
        ),
        target_surface=request.target_surface,
        canvas_fill_rgba=request.target_surface.fill_rgba,
        canvas_geometry=canvas_geometry,
        allow_feature_layout_fallback=allow_feature_layout_fallback,
        scene_images_cache=caches.scene_images,
    )
    trace_plan(trace, plan)
    fill_frame_debug(
        debug,
        target_size=target_size,
        content_size=content_size,
        content_x=output_layout.content_x,
        content_y=output_layout.content_y,
        canvas_geometry=canvas_geometry,
        plan_build_started=layout_started,
    )
    return PreparedCanvasFrame(
        store=frame.store,
        plan=plan,
        output_width=request.target_surface.width,
        output_height=request.target_surface.height,
        image_dest_x=output_layout.content_x,
        image_dest_y=output_layout.content_y,
        fill_rgba=request.target_surface.fill_rgba,
        debug=debug,
    )
