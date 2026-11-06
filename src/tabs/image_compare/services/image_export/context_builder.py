from __future__ import annotations

import PIL.Image

from tabs.image_compare.plugins.video_editor.services.video_export_models import VideoRenderRequest
from shared.image_processing.resize import resize_images_processor
from engine.cache import TargetSurfaceSpec, get_effective_export_interpolation_method
from tabs.image_compare.services.image_export.models import ExportSaveContext
from tabs.image_compare.services.live_snapshot import build_live_frame_snapshot
from tabs.image_compare.services.snapshot_render_plan_builder import (
    calculate_still_snapshot_bounds,
)
from tabs.image_compare.services.video_snapshot_rendering import SnapshotFrameRenderer


class ExportContextBuilder:
    def __init__(self, store, gpu_export_service, state_coordinator, *, get_crop_service=None):
        self.store = store
        self.gpu_export_service = gpu_export_service
        self.state = state_coordinator
        # Optional zero-arg callable returning the session CropService
        # (controller._get_crop_service — None when autocrop is OFF). Unset →
        # boxes stay None and export runs full-frame exactly as before (W3c).
        self._get_crop_service = get_crop_service
        self.renderer = SnapshotFrameRenderer(
            image_loader=lambda _path, _auto_crop=False: None,
            gpu_export_service=gpu_export_service,
        )

    def _current_canvas_fill_rgba(self):
        dialog_state = self.state.build_export_dialog_state()
        bg = dialog_state.background_color
        if not dialog_state.fill_background or bg is None:
            return None
        return (bg.r, bg.g, bg.b, bg.a)

    def _peek_slot(self, slot: int):
        doc = self.store.get_session_state_slot("document")
        path = doc.image1_path if slot == 1 else doc.image2_path
        if not path:
            return None
        # 1) viewport PipelineView
        try:
            vp = self.store.viewport.session_data.image_state
            cand = vp.image1 if slot == 1 else vp.image2
            if cand is not None:
                try:
                    if hasattr(cand, "isNull") and cand.isNull():
                        cand = None
                    elif hasattr(cand, "is_open") and not cand.is_open:
                        cand = None
                except Exception:
                    pass
                if cand is not None:
                    return cand
        except Exception:
            pass
        # 2) pipeline state peek
        try:
            ps = self.store.get_session_state_slot("pipeline")
            if ps is not None:
                import os

                from tabs.image_compare.pipeline.cache import _pixel_key, _preview_key

                for cache_dict, key_fn in (
                    (ps.pixel, _pixel_key),
                    (ps.preview, _preview_key),
                ):
                    try:
                        k = key_fn(path, None, None)
                        v = cache_dict.get(k)
                        if v is not None:
                            if hasattr(v, "is_open") and not v.is_open:
                                continue
                            if hasattr(v, "isNull") and v.isNull():
                                continue
                            return v
                    except Exception:
                        pass
                    try:
                        norm = os.path.normpath(path)
                        for kk, vv in cache_dict.items():
                            if kk[0] == norm:
                                if hasattr(vv, "is_open") and not vv.is_open:
                                    continue
                                if hasattr(vv, "isNull") and vv.isNull():
                                    continue
                                return vv
                    except Exception:
                        pass
        except Exception:
            pass
        return None

    def has_images(self) -> bool:
        return bool(self._peek_slot(1) and self._peek_slot(2))

    def _resolve_export_boxes(self):
        """Crop windows for the current pair via the single box owner (W3c)."""
        try:
            doc = self.store.get_session_state_slot("document")
            path1 = getattr(doc, "image1_path", None)
            path2 = getattr(doc, "image2_path", None)
        except Exception:
            return None, None
        try:
            from tabs.image_compare.services.analysis.analysis_pair import (
                forced_service_from,
                resolve_crop_boxes_for_paths,
            )

            return resolve_crop_boxes_for_paths(
                path1,
                path2,
                self._get_crop_service,
                get_forced_crop_service=forced_service_from(
                    self._get_crop_service
                ),
            )
        except Exception:
            return None, None

    def build_save_context(self, include_preview: bool = True) -> ExportSaveContext:
        original1_full = self._peek_slot(1)
        original2_full = self._peek_slot(2)
        if not original1_full or not original2_full:
            raise ValueError("Full resolution images are not available for saving.")

        # W3c: export operates on the crop windows (black borders excluded),
        # sourced from the full-frame stores above (never baked at decode).
        # Both boxes None → views are the originals, identical to today.
        # Forwarded originals are the cropped views so a later size-override
        # rebuild unifies the same windows the plan was built from.
        from tabs.image_compare.services.analysis.analysis_pair import (
            crop_pair_to_boxes,
        )

        box1, box2 = self._resolve_export_boxes()
        original1_full, original2_full = crop_pair_to_boxes(
            original1_full, original2_full, box1, box2
        )
        if not original1_full or not original2_full:
            raise ValueError("Full resolution images are not available for saving.")

        live_snapshot = build_live_frame_snapshot(self.store)
        resize_method = get_effective_export_interpolation_method(
            live_snapshot.viewport_state
        )

        from engine.store.pixel_ops.unify import unify_pair

        image1_for_save, image2_for_save = unify_pair(
            original1_full,
            original2_full,
            resize_method,
        )
        if not image1_for_save or not image2_for_save:
            raise ValueError("Failed to unify images for export.")

        preview_img = None
        if include_preview:
            preview_img = self.build_export_preview_from_sources(
                original1_full,
                original2_full,
                live_snapshot=live_snapshot,
            )
        global_bounds = calculate_still_snapshot_bounds(
            live_snapshot,
            image1_for_save,
            image2_for_save,
        )
        canvas_w = (
            int(global_bounds.base_width)
            + int(global_bounds.pad_left)
            + int(global_bounds.pad_right)
        )
        canvas_h = (
            int(global_bounds.base_height)
            + int(global_bounds.pad_top)
            + int(global_bounds.pad_bottom)
        )
        prepared_frame = self.renderer.prepare_canvas_frame_from_images(
            live_snapshot,
            VideoRenderRequest(
                target_surface=TargetSurfaceSpec(
                    width=canvas_w,
                    height=canvas_h,
                    fill_rgba=self._current_canvas_fill_rgba(),
                ),
                font_path=None,
                auto_crop=False,
                fit_content=True,
                global_bounds=global_bounds,
            ),
            image1_for_save,
            image2_for_save,
            allow_feature_layout_fallback=True,
            normalize_snapshot=False,
        )

        plan = prepared_frame.plan
        native_w = int(getattr(plan, "canvas_w", 0) or image1_for_save.width)
        native_h = int(getattr(plan, "canvas_h", 0) or image1_for_save.height)
        # "Crop to images": the plan's own content rect (pads/letterbox dropped)
        # in native px, plus the same rect mapped onto the preview raster so the
        # dialog can crop its pane without a second render.
        from tabs.image_compare.services.image_export.content_crop import (
            content_rect_for_plan,
            scale_rect_to_raster,
        )

        content_rect = content_rect_for_plan(plan)
        crop_native_size = (
            (int(content_rect[2]), int(content_rect[3])) if content_rect else None
        )
        preview_crop_rect = (
            scale_rect_to_raster(content_rect, preview_img.size)
            if content_rect is not None and preview_img is not None
            else None
        )
        return ExportSaveContext(
            original1_full=original1_full,
            original2_full=original2_full,
            image1_for_save=image1_for_save,
            image2_for_save=image2_for_save,
            render_plan=plan,
            render_store=prepared_frame.store,
            preview_img=preview_img,
            suggested_filename=self.state.build_suggested_export_filename(),
            native_width=native_w,
            native_height=native_h,
            virtual_canvas_active=bool(global_bounds.extends_beyond_unit()),
            crop_native_size=crop_native_size,
            preview_crop_rect=preview_crop_rect,
        )

    def _downscale_for_preview(
        self,
        image,
        max_edge: int = 1200,
    ) -> PIL.Image.Image:
        from engine.store.pixel_ops.downscale import downscale_source_to_pil
        from engine.store.tiled_pixel_store import TiledPixelStore

        width, height = image.size
        longest_edge = max(width, height)
        if longest_edge <= max_edge:
            if isinstance(image, TiledPixelStore):
                return downscale_source_to_pil(image, (width, height))
            return image.copy()

        scale = max_edge / float(longest_edge)
        preview_size = (
            max(1, int(round(width * scale))),
            max(1, int(round(height * scale))),
        )
        return downscale_source_to_pil(image, preview_size)

    def build_export_preview_from_sources(
        self,
        image1_full,
        image2_full,
        *,
        live_snapshot=None,
    ):
        live_snapshot = live_snapshot or build_live_frame_snapshot(self.store)
        resize_method = get_effective_export_interpolation_method(
            live_snapshot.viewport_state
        )
        image1_preview_src = self._downscale_for_preview(image1_full)
        image2_preview_src = self._downscale_for_preview(image2_full)
        image1_preview, image2_preview = resize_images_processor(
            image1_preview_src,
            image2_preview_src,
            resize_method,
        )
        if not image1_preview or not image2_preview:
            raise ValueError("Failed to build preview export pair.")
        global_bounds = calculate_still_snapshot_bounds(
            live_snapshot,
            image1_preview,
            image2_preview,
        )
        canvas_w = (
            int(global_bounds.base_width)
            + int(global_bounds.pad_left)
            + int(global_bounds.pad_right)
        )
        canvas_h = (
            int(global_bounds.base_height)
            + int(global_bounds.pad_top)
            + int(global_bounds.pad_bottom)
        )
        # Preview GPU always leaves virtual-canvas pads transparent. The export
        # dialog composites the chosen fill under alpha so toggling
        # Fill background / color updates the previewer without a re-render.
        # Baking fill into the GPU frame made opaque theme/black pads that the
        # dialog could not recolor.
        result = self.renderer.render_from_images(
            live_snapshot,
            VideoRenderRequest(
                target_surface=TargetSurfaceSpec(
                    width=canvas_w,
                    height=canvas_h,
                    fill_rgba=None,
                ),
                font_path=None,
                auto_crop=False,
                fit_content=True,
                global_bounds=global_bounds,
            ),
            image1_preview,
            image2_preview,
            allow_feature_layout_fallback=True,
            normalize_snapshot=False,
        )
        return result.image