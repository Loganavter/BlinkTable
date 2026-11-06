import logging
import time

# File-Size-Exempt: single magnifier rebuild pipeline sharing presenter/image_label locals across its phases; P3 loupe trace records are additive call sites, not new responsibilities.
from dataclasses import replace as _dc_replace

from engine.cache.tile_debug import log_tile_event, tile_dump_enabled
from tabs.image_compare.canvas.loupe_trace import (
    frame_context_from_widget,
    trace_loupe,
)
from core.log_names import APP_LOGGER_NAME

_log = logging.getLogger(f"{APP_LOGGER_NAME}.magnifier.scene_update")

# P1B explicit-state verdict: sig-throttle for the SSIM diff-state log below
# (rebuild can run repeatedly while a diff is pending — same recipe as
# ``rhi_renderer/use_cases/fallback.py``'s ``_last_is_same_sig``:
# emit once per distinct decision signature, not once per rebuild).
_last_ssim_diff_state_sig: tuple | None = None

from tabs.image_compare.canvas.features.magnifier.geometry.box_remap import (
    image_full_size,
    resolve_crop_boxes_for_store,
    valid_box_for_full,
)
from tabs.image_compare.canvas.features.magnifier.geometry.layout_plan import build_magnifier_layout
from tabs.image_compare.canvas.features.magnifier.render.plan_overlay import apply_magnifier_plan_overlay
from tabs.image_compare.canvas.features.magnifier.state.store import active_or_default_divider_thickness
from ui.canvas_infra.scene.apply import apply_scene_to_canvas
from ui.canvas_infra.scene.builder import build_canvas_scene
from ui.canvas_infra.scene.pass_contract import SceneVisibility
from ui.canvas_presentation.plan import (
    plan_owns_overlay_state,
    resolve_plan_logical_image_rect,
)
from tabs.image_compare.canvas.helpers import reset_canvas_overlays

from tabs.image_compare.canvas.features.magnifier.workers.common import (
    get_effective_main_interpolation_method,
    get_live_image_label,
    is_effective_magnifier_interactive,
)
from engine.cache.image_identity import image_uid

from tabs.image_compare.canvas.features.magnifier.workers.diff_cache import ensure_cached_diff_image


def _build_and_apply_scene_snapshot(presenter, image_label, geometry):
    label_width, label_height = presenter.get_current_label_dimensions()
    scene = build_canvas_scene(
        presenter.store,
        image_label=image_label,
        label_width=label_width,
        label_height=label_height,
    )
    runtime_state = getattr(image_label, "runtime_state", None)
    if runtime_state is not None:
        runtime_state._canvas_scene_graph = scene
    apply_scene_to_canvas(
        scene,
        image_label,
        geometry,
        session_type="image_compare",
        use_quick_overlay=False,
        scene_visibility=SceneVisibility.INTERACTIVE,
    )
    return scene


def _resolve_magnifier_interpolation_method(vp, *, effective_interactive: bool) -> str:
    if effective_interactive:
        return str(
            getattr(
                vp.render_config,
                "interactive_movement_interpolation_method",
                "BILINEAR",
            )
            or "BILINEAR"
        )
    return str(get_effective_main_interpolation_method(vp) or "BILINEAR")


def _is_ssim_pending(presenter, diff_mode_str, tex_img1=None, tex_img2=None) -> bool:
    if diff_mode_str != "ssim":
        return False
    if getattr(presenter, "_pending_cached_diff_request_key", None) is not None:
        return True
    try:
        rc = presenter.store.viewport.session_data.render_cache
        if bool(getattr(rc, "unification_in_progress", False)):
            return True
    except Exception:
        pass
    for _img in (tex_img1, tex_img2):
        if _img is not None and hasattr(_img, "is_open") and not _img.is_open:
            return True
        try:
            if _img is not None and hasattr(_img, "generation"):
                from shared.image_processing.store_lease import StoreLease
                lease = StoreLease.capture(_img)
                if lease is not None and not lease.valid:
                    return True
        except Exception:
            pass
    return False


def rebuild_magnifier_overlay(presenter, frame_context=None):
    vp = presenter.store.viewport
    geometry = vp.geometry_state
    image_label = get_live_image_label(presenter)
    if image_label is None:
        return
    if not hasattr(image_label, "set_feature_overlay_gpu_params"):
        return
    if frame_context is None:
        # P3: live FrameContext read through the presenter widget -- no
        # caller rewiring (all current callers pass presenter only).
        frame_context = frame_context_from_widget(getattr(presenter, "widget", None))

    _debug_timing = tile_dump_enabled()
    _t_start = time.perf_counter() if _debug_timing else 0.0
    _checkpoints: list[tuple[str, float]] = []

    def _mark(label: str) -> None:
        if _debug_timing:
            _checkpoints.append((label, time.perf_counter() - _t_start))

    if hasattr(image_label, "begin_update_batch"):
        image_label.begin_update_batch()
    try:
        _build_and_apply_scene_snapshot(presenter, image_label, geometry)
        _mark("scene_snapshot")

        diff_mode_early = getattr(vp.view_state, "diff_mode", "off")
        plan = getattr(image_label, "_active_render_plan", None)
        if plan is None:
            if _is_ssim_pending(presenter, diff_mode_early):
                # P3 (M1 point 1): silent early return -> reason record.
                trace_loupe(
                    frame_context,
                    "loupe.scene_plan_none",
                    "skipped",
                    "scene plan None ssim pending keep previous frame"
                    f" diff_mode={diff_mode_early} pending_key="
                    f"{getattr(presenter, '_pending_cached_diff_request_key', None) is not None}",
                )
                return
            if diff_mode_early in ("highlight", "grayscale", "edges") and not bool(
                getattr(image_label, "_source_images_ready", False)
            ):
                return
            reset_canvas_overlays(image_label)
            return

        source_pil_images = getattr(image_label, "_source_pil_images", ())
        tex_img1 = source_pil_images[0] if len(source_pil_images) >= 1 else None
        tex_img2 = source_pil_images[1] if len(source_pil_images) >= 2 else None
        if tex_img1 is None or tex_img2 is None:
            # PipelineCache fallback
            try:
                store = presenter.store
                doc = store.get_session_state_slot("document")
                for slot, cur in ((1, tex_img1), (2, tex_img2)):
                    if cur is not None:
                        continue
                    path = doc.image1_path if slot == 1 else doc.image2_path
                    if not path:
                        continue
                    try:
                        _img_state = store.viewport.session_data.image_state
                        cand = _img_state.image1 if slot == 1 else _img_state.image2
                        if cand is not None and getattr(cand, "is_open", True):
                            try:
                                if hasattr(cand, "isNull") and cand.isNull():
                                    cand = None
                                elif hasattr(cand, "is_open") and not cand.is_open:
                                    cand = None
                            except Exception:
                                pass
                            if cand is not None:
                                if slot == 1:
                                    tex_img1 = cand
                                else:
                                    tex_img2 = cand
                                continue
                    except Exception:
                        pass
                    try:
                        ps = store.get_session_state_slot("pipeline")
                        if ps is not None:
                            import os

                            from tabs.image_compare.pipeline.cache import _pixel_key, _preview_key

                            for cache_dict, key_fn in ((ps.pixel, _pixel_key), (ps.preview, _preview_key)):
                                try:
                                    k = key_fn(path, None, None)
                                    v = cache_dict.get(k)
                                    if v is not None:
                                        if hasattr(v, "is_open") and not v.is_open:
                                            continue
                                        if hasattr(v, "isNull") and v.isNull():
                                            continue
                                        if slot == 1:
                                            tex_img1 = v
                                        else:
                                            tex_img2 = v
                                        break
                                except Exception:
                                    pass
                                if (tex_img1 if slot == 1 else tex_img2) is not None:
                                    break
                                try:
                                    norm = os.path.normpath(path)
                                    for kk, vv in cache_dict.items():
                                        if kk[0] == norm:
                                            if hasattr(vv, "is_open") and not vv.is_open:
                                                continue
                                            if hasattr(vv, "isNull") and vv.isNull():
                                                continue
                                            if slot == 1:
                                                tex_img1 = vv
                                            else:
                                                tex_img2 = vv
                                            break
                                except Exception:
                                    pass
                    except Exception:
                        pass
            except Exception:
                pass
        _mark("resolve_source_images")
        if not tex_img1 or not tex_img2:
            diff_mode_check = getattr(vp.view_state, "diff_mode", "off")
            if _is_ssim_pending(presenter, diff_mode_check, tex_img1, tex_img2):
                # P3 (M1 point 2): silent early return -> reason record.
                trace_loupe(
                    frame_context,
                    "loupe.scene_no_sources",
                    "skipped",
                    "scene source images missing ssim pending keep previous frame"
                    f" diff_mode={diff_mode_check} have1={tex_img1 is not None}"
                    f" have2={tex_img2 is not None} pending_key="
                    f"{getattr(presenter, '_pending_cached_diff_request_key', None) is not None}",
                )
                return
            if diff_mode_check in ("highlight", "grayscale", "edges") and not bool(
                getattr(image_label, "_source_images_ready", False)
            ):
                return
            reset_canvas_overlays(image_label)
            return

        diff_mode_str = getattr(vp.view_state, "diff_mode", "off")
        # W3b: full-frame tile grids sample the capture over the crop box.
        # Warmed-only lookup (dict hit, never GUI-thread IO); None → legacy.
        # W3d: the same warmed boxes thread into the SSIM diff below so the
        # diff computes over the crop windows, aligned with the remapped
        # lens capture. Both None → bit-identical to today.
        try:
            _box1, _box2 = resolve_crop_boxes_for_store(presenter.store)
            _full1 = image_full_size(tex_img1)
            _full2 = image_full_size(tex_img2)
            _box1 = valid_box_for_full(_box1, *(_full1 or (0, 0)))
            _box2 = valid_box_for_full(_box2, *(_full2 or (0, 0)))
        except Exception:
            _box1, _box2, _full1, _full2 = None, None, None, None
        cached_diff_image = (
            ensure_cached_diff_image(
                presenter,
                tex_img1,
                tex_img2,
                local_source1=tex_img1,
                local_source2=tex_img2,
                box1=_box1,
                box2=_box2,
            )
            if diff_mode_str == "ssim"
            else None
        )
        if diff_mode_str == "ssim" and cached_diff_image is None:
            try:
                pending_already = getattr(presenter, "_pending_cached_diff_request_key", None) is not None
                rc_async = getattr(presenter.store.viewport.session_data, "render_cache", None)
                unification_async = bool(getattr(rc_async, "unification_in_progress", False)) if rc_async is not None else False
                if not pending_already and not unification_async:
                    both_ready_async = tex_img1 is not None and tex_img2 is not None
                    if both_ready_async:
                        try:
                            if hasattr(tex_img1, "is_open") and not tex_img1.is_open:
                                both_ready_async = False
                            if hasattr(tex_img2, "is_open") and not tex_img2.is_open:
                                both_ready_async = False
                        except Exception:
                            pass
                    if both_ready_async:
                        try:
                            from tabs.image_compare.canvas.features.magnifier.workers.diff_cache import (
                                request_cached_diff_image_async,
                            )
                            request_cached_diff_image_async(
                                presenter,
                                tex_img1,
                                tex_img2,
                                diff_mode_str,
                                box1=_box1,
                                box2=_box2,
                            )
                        except Exception:
                            pass
            except Exception:
                pass
        current_uploaded_diff = getattr(image_label, "_diff_source_pil_image", None)
        diff_image_for_magnifier = cached_diff_image or current_uploaded_diff
        if cached_diff_image is not None:
            image_label.upload_diff_source_pil_image(cached_diff_image)
        elif diff_mode_str == "ssim":
            # P1B explicit state (was a bare ``pass``/``pass``): the SSIM diff
            # image is neither cached nor previously uploaded. Distinguish
            # "still computing, keep the previous frame" from "settled with
            # no diff, proceed without it" instead of silently falling through.
            # Semantics unchanged — both branches still fall through to the
            # layout build below with diff_image_for_magnifier as-is.
            _ssim_still_pending = _is_ssim_pending(
                presenter, diff_mode_str, tex_img1, tex_img2
            )
            _ssim_state_sig = (
                bool(_ssim_still_pending),
                cached_diff_image is None,
                current_uploaded_diff is None,
            )
            global _last_ssim_diff_state_sig
            if _ssim_state_sig != _last_ssim_diff_state_sig:
                _last_ssim_diff_state_sig = _ssim_state_sig
                if _ssim_still_pending:
                    _log.debug(
                        "magnifier ssim_diff: verdict=pending_keep_previous"
                        " reason=ssim compute still in flight (pending request,"
                        " unification, or image lease invalid) cached=%s"
                        " uploaded=%s -> hold previous diff frame",
                        cached_diff_image is None,
                        current_uploaded_diff is None,
                    )
                else:
                    _log.debug(
                        "magnifier ssim_diff: verdict=absent_proceed_without_diff"
                        " reason=ssim settled with no diff image available"
                        " cached=%s uploaded=%s -> proceed without diff overlay",
                        cached_diff_image is None,
                        current_uploaded_diff is None,
                    )
        else:
            if bool(getattr(image_label, "_source_images_ready", False)):
                image_label.upload_diff_source_pil_image(None)
            else:
                pass
        _mark("diff_cache")

        effective_interactive = is_effective_magnifier_interactive(vp)
        interpolation_method = _resolve_magnifier_interpolation_method(
            vp,
            effective_interactive=effective_interactive,
        )
        if diff_mode_str == "ssim":
            if diff_image_for_magnifier is None and _is_ssim_pending(presenter, diff_mode_str, tex_img1, tex_img2):
                # P3 (M1 point 3): silent early return -> reason record.
                trace_loupe(
                    frame_context,
                    "loupe.scene_ssim_pending",
                    "skipped",
                    "scene ssim diff pending keep previous frame"
                    f" pending_key={getattr(presenter, '_pending_cached_diff_request_key', None) is not None}",
                )
                return
            diff_mode_int = 4 if diff_image_for_magnifier is not None else 0
        else:
            diff_mode_int = {"highlight": 1, "grayscale": 2, "edges": 3}.get(
                diff_mode_str,
                0,
            )

        content_x, content_y, content_w, content_h = resolve_plan_logical_image_rect(
            plan
        )

        # Box resolution hoisted above the diff-cache section (W3d); the
        # warmed boxes below are the same ones the SSIM diff already used.
        layout = build_magnifier_layout(
            vp,
            width=max(1, int(content_w)),
            height=max(1, int(content_h)),
            canvas_width=max(1, int(plan.canvas_w)),
            canvas_height=max(1, int(plan.canvas_h)),
            content_offset_x=float(content_x),
            content_offset_y=float(content_y),
            display_scale=float(
                getattr(
                    getattr(image_label, "runtime_state", None),
                    "_content_sr",
                    1.0,
                )
                or 1.0
            ),
            divider_thickness_px=int(
                active_or_default_divider_thickness(vp.view_state)
            ),
            interpolation_method=interpolation_method,
            diff_mode_override=diff_mode_int,
            crop_box1=_box1,
            crop_box2=_box2,
            full_size1=_full1,
            full_size2=_full2,
        )
        _mark("build_layout")
        if layout is None:
            if plan_owns_overlay_state(plan):
                # The live rebuild could not build a layout, but the incoming
                # plan already carries one (recorded snapshot / video-preview
                # plan). Never null it and never hand the canvas to
                # ``reset_canvas_overlays``: an overlay the live path did not
                # build is not the live path's to clear. The scene apply above
                # half-cleared the applied GPU overlay (quads/capture/guides),
                # so re-apply the plan's own layout from a clean slate.
                image_label.clear_feature_overlay_gpu()
                apply_magnifier_plan_overlay(image_label, plan)
                trace_loupe(
                    frame_context,
                    "loupe.scene_layout_unavailable_plan_kept",
                    "skipped",
                    "live layout build returned None; kept the plan-carried"
                    f" overlay_layout slots={len(plan.overlay_layout.slots)}",
                )
                return
            image_label._active_render_plan = _dc_replace(plan, overlay_layout=None)
            reset_canvas_overlays(image_label)
            return

        updated_plan = _dc_replace(plan, overlay_layout=layout)
        image_label._active_render_plan = updated_plan
        apply_magnifier_plan_overlay(image_label, updated_plan)
        _mark("apply_plan_overlay")
    finally:
        if _debug_timing:
            total = time.perf_counter() - _t_start
            if total > 0.05:
                log_tile_event(
                    "magnifier.rebuild_overlay_timing",
                    total_s=total,
                    checkpoints={label: round(dt, 4) for label, dt in _checkpoints},
                )
        if hasattr(image_label, "end_update_batch"):
            image_label.end_update_batch()
        if hasattr(image_label, "_request_update"):
            image_label._request_update()
