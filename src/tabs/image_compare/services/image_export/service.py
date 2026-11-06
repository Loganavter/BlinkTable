import logging
import os
import threading
from typing import Any, Optional

from PIL import Image

from core.store import Store
from tabs.image_compare.plugins.video_editor.services.video_export_models import VideoRenderRequest
from shared.image_processing.format_capability import encode, formats_with_alpha
from shared.image_processing.pil_save import (
    SAVE_CANCELED_MESSAGE,
    safe_child_path,
    sanitize_export_basename,
)
from shared.image_processing.resize import resize_images_processor
from engine.cache import TargetSurfaceSpec, get_effective_export_interpolation_method
from tabs.image_compare.services.live_snapshot import build_live_frame_snapshot
from tabs.image_compare.services.snapshot_render_plan_builder import (
    calculate_still_snapshot_bounds,
)
from tabs.image_compare.services.video_snapshot_rendering import SnapshotFrameRenderer
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)


class ExportService:
    def __init__(self, font_path_absolute: str, gpu_export_service=None):
        self.font_path_absolute = font_path_absolute
        self.gpu_export_service = gpu_export_service

    @staticmethod
    def resolve_canvas_fill_rgba(export_options: dict) -> tuple[int, int, int, int] | None:
        if not export_options.get("fill_background", False):
            return None
        color = export_options.get("background_color")
        if not color or len(color) < 4:
            return None
        return (int(color[0]), int(color[1]), int(color[2]), int(color[3]))

    @staticmethod
    def plan_with_canvas_fill(plan, fill_rgba: tuple[int, int, int, int] | None):
        """Stamp current dialog fill onto a possibly cached render plan.

        Save context builds the plan once at dialog open; toggling fill must
        still reach GPU clear + letterboxFill on export.
        """
        if plan is None:
            return None
        if getattr(plan, "fill_rgba", None) == fill_rgba:
            return plan
        from dataclasses import replace

        return replace(plan, fill_rgba=fill_rgba)

    @staticmethod
    def apply_background_fill(image: Image.Image, background_color):
        if not background_color:
            return image
        bg = Image.new("RGBA", image.size, background_color)
        bg.paste(image, mask=image.split()[3] if image.mode == "RGBA" else None)
        return bg

    def export_image(
        self,
        store: Store,
        original_image1: Image.Image,
        original_image2: Image.Image,
        export_options: dict,
        render_plan=None,
        render_store=None,
        cancel_event: Optional[threading.Event] = None,
        progress_callback: Optional[Any] = None,
    ) -> str:
        """
        Экспортирует изображение сравнения в файл.
        """
        if cancel_event and cancel_event.is_set():
            raise RuntimeError(SAVE_CANCELED_MESSAGE)

        if not original_image1 or not original_image2:
            raise ValueError("Full resolution images are not available for saving.")
        if not export_options.get("output_dir") or not export_options.get("file_name"):
            raise ValueError("Output directory or file name not specified.")

        if progress_callback:
            progress_callback(5)

        renderer = SnapshotFrameRenderer(
            image_loader=lambda _path, _auto_crop=False: None,
            gpu_export_service=self.gpu_export_service,
        )
        current_render_plan = render_plan
        current_render_store = render_store
        canvas_fill_rgba = self.resolve_canvas_fill_rgba(export_options)

        override_w = export_options.get("width")
        override_h = export_options.get("height")
        try:
            override_w = int(override_w) if override_w else 0
            override_h = int(override_h) if override_h else 0
        except (TypeError, ValueError):
            override_w = override_h = 0

        plan_size = None
        if current_render_plan is not None:
            cw = int(getattr(current_render_plan, "canvas_w", 0) or 0)
            ch = int(getattr(current_render_plan, "canvas_h", 0) or 0)
            if cw > 0 and ch > 0:
                plan_size = (cw, ch)
        needs_rebuild = (
            current_render_plan is None
            or (
                override_w > 0
                and override_h > 0
                and plan_size is not None
                and plan_size != (override_w, override_h)
            )
        )

        if needs_rebuild:
            live_snapshot = build_live_frame_snapshot(store)
            resize_method = get_effective_export_interpolation_method(
                live_snapshot.viewport_state
            )
            image1_for_save, image2_for_save = resize_images_processor(
                original_image1,
                original_image2,
                resize_method,
            )
            if not image1_for_save or not image2_for_save:
                raise ValueError("Failed to unify images for export.")
            if override_w > 0 and override_h > 0:
                target_w = override_w
                target_h = override_h
            else:
                target_w = override_w if override_w > 0 else image1_for_save.width
                target_h = override_h if override_h > 0 else image1_for_save.height
            global_bounds = calculate_still_snapshot_bounds(
                live_snapshot,
                image1_for_save,
                image2_for_save,
            )
            if override_w <= 0 or override_h <= 0:
                target_w = (
                    int(global_bounds.base_width)
                    + int(global_bounds.pad_left)
                    + int(global_bounds.pad_right)
                )
                target_h = (
                    int(global_bounds.base_height)
                    + int(global_bounds.pad_top)
                    + int(global_bounds.pad_bottom)
                )
            prepared_frame = renderer.prepare_canvas_frame_from_images(
                live_snapshot,
                VideoRenderRequest(
                    target_surface=TargetSurfaceSpec(
                        width=target_w,
                        height=target_h,
                        fill_rgba=canvas_fill_rgba,
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
            current_render_plan = prepared_frame.plan
            current_render_store = prepared_frame.store

        if progress_callback:
            progress_callback(15)

        if self.gpu_export_service is None:
            raise RuntimeError("GPU export service is not configured")

        current_render_plan = self.plan_with_canvas_fill(
            current_render_plan, canvas_fill_rgba
        )

        diff_image_for_render = None
        try:
            render_cache = getattr(
                getattr(current_render_store, "viewport", None),
                "session_data",
                None,
            )
            render_cache = getattr(render_cache, "render_cache", None)
            diff_image_for_render = getattr(render_cache, "cached_diff_image", None)
        except Exception:
            diff_image_for_render = None

        final_img, _gpu_debug = self.gpu_export_service.render_plan(
            current_render_plan,
            store=current_render_store or store,
            diff_image=diff_image_for_render,
        )
        if final_img is None:
            raise ValueError("Failed to create the base image for saving.")

        if cancel_event and cancel_event.is_set():
            raise RuntimeError(SAVE_CANCELED_MESSAGE)

        # Encode/write is usually the long stretch — reserve most of the bar for it.
        if progress_callback:
            progress_callback(25)

        img_to_save = final_img
        if bool(export_options.get("crop_to_image", False)):
            # Trim to the image area (virtual-canvas pads / letterbox dropped).
            # Done before the fill so a requested fill only reaches transparency
            # INSIDE the kept area; a no-op rect returns the frame untouched.
            from tabs.image_compare.services.image_export.content_crop import (
                crop_to_plan_content,
            )

            img_to_save = crop_to_plan_content(final_img, current_render_plan)
        if export_options.get("fill_background", False):
            bg_color_tuple = export_options.get("background_color")
            if bg_color_tuple:
                img_to_save = self.apply_background_fill(img_to_save, bg_color_tuple)

        target_format = export_options.get("format", "PNG").upper()
        pil_format = "JPEG" if target_format == "JPG" else target_format
        output_dir = export_options["output_dir"]
        # Write boundary: the typed name is not trusted to be a bare child of
        # the chosen directory (an absolute/UNC/traversal name would replace
        # output_dir at os.path.join). ``sanitize_export_basename`` raises on
        # those, and ``safe_child_path`` re-verifies the resolved parent — the
        # existing error path aborts the save, nothing is written outside.
        base_name = sanitize_export_basename(export_options["file_name"])
        is_quick_save = export_options.get("is_quick_save", False)
        ext = f".{target_format.lower().replace('jpeg', 'jpg')}"

        if is_quick_save:
            unique_path = self._generate_unique_filepath(output_dir, base_name, ext)
            full_path = str(safe_child_path(output_dir, os.path.basename(unique_path)))
        else:
            full_path = str(safe_child_path(output_dir, f"{base_name}{ext}"))

        os.makedirs(output_dir, exist_ok=True)

        save_kwargs = {}
        if pil_format not in formats_with_alpha() and img_to_save.mode == "RGBA":
            bg_color_tuple = export_options.get("background_color") or (
                255,
                255,
                255,
                255,
            )
            bg_flat = Image.new("RGBA", img_to_save.size, bg_color_tuple)
            bg_flat.paste(img_to_save, mask=img_to_save.split()[3])
            img_to_save = bg_flat.convert("RGB")

        comment_text = (export_options.get("comment_text") or "").strip()
        include_metadata = bool(export_options.get("include_metadata", True))

        if pil_format == "JPEG":
            save_kwargs["quality"] = int(export_options.get("quality", 95))
            if comment_text and include_metadata:
                self._attach_comment_metadata(
                    img_to_save,
                    save_kwargs,
                    pil_format,
                    comment_text,
                )
        elif pil_format == "PNG":
            save_kwargs["compress_level"] = int(
                export_options.get("png_compress_level", 9)
            )
            save_kwargs["optimize"] = bool(export_options.get("png_optimize", True))
            if comment_text and include_metadata:
                self._attach_comment_metadata(
                    img_to_save,
                    save_kwargs,
                    pil_format,
                    comment_text,
                )
        elif pil_format in {"TIFF", "WEBP"}:
            if comment_text and include_metadata:
                self._attach_comment_metadata(
                    img_to_save,
                    save_kwargs,
                    pil_format,
                    comment_text,
                )
        elif pil_format == "JXL" and comment_text and include_metadata:
            logger.warning(
                "JXL export currently does not embed textual metadata; comment_text will not be saved."
            )

        if cancel_event and cancel_event.is_set():
            raise RuntimeError(SAVE_CANCELED_MESSAGE)

        try:
            # One writer for every format (shared.image_processing.
            # format_capability.encode): Pillow for the formats it registers,
            # imagecodecs for JXL. This tab used to carry its own JXL branch
            # here; keeping it would be a second implementation that the other
            # tab's save path could not reach (and did not: it crashed with
            # KeyError 'JXL' because Pillow has no JXL plugin).
            encode(
                img_to_save,
                path_or_sink=full_path,
                image_format=pil_format,
                save_kwargs=save_kwargs,
                cancel_event=cancel_event,
                progress=progress_callback,
                progress_start=25,
                progress_end=99,
            )
        except Exception:
            # Only the staged tmp belongs to this attempt. full_path is
            # published exclusively by the atomic os.replace inside the shared
            # writer — before that it is the user's PRE-EXISTING file (a
            # non-quick save overwrites in place), so deleting it here is data
            # loss, not cleanup.
            try:
                tmp_path = full_path + ".tmp"
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise

        if progress_callback:
            progress_callback(100)

        return full_path

    def _attach_comment_metadata(
        self,
        img_to_save,
        save_kwargs: dict,
        pil_format: str,
        comment_text: str,
    ) -> None:
        try:
            if pil_format == "PNG":
                import PIL.PngImagePlugin as PngImagePlugin

                meta = PngImagePlugin.PngInfo()
                meta.add_text("Comment", comment_text)
                save_kwargs["pnginfo"] = meta
                return

            exif = img_to_save.getexif()
            exif[0x9286] = comment_text
            save_kwargs["exif"] = exif.tobytes()
        except Exception:
            logger.debug(
                "Failed to attach export comment metadata for format=%s",
                pil_format,
                exc_info=True,
            )

    def _generate_base_filename(self, store: Store) -> str:
        import re

        name1 = (self._get_current_display_name(store, 1) or "image1").strip()
        name2 = (self._get_current_display_name(store, 2) or "image2").strip()

        def sanitize(s: str) -> str:
            s = re.sub(r'[\\/*?:"<>|]', "_", s)
            return s[:80]

        return f"{sanitize(name1)}_{sanitize(name2)}"

    def _get_current_display_name(self, store: Store, image_number: int) -> str:
        document = store.get_session_state_slot("document")
        target_list, index = (
            (document.image_list1, document.current_index1)
            if image_number == 1
            else (document.image_list2, document.current_index2)
        )
        if 0 <= index < len(target_list):
            return target_list[index].display_name
        return ""

    def _generate_unique_filepath(
        self,
        directory: str,
        base_name: str,
        extension: str,
    ) -> str:
        """Delegate to shared ``next_available_path`` (B7)."""
        from pathlib import Path

        from shared.image_processing.pil_save import next_available_path

        candidate = Path(directory) / f"{base_name}{extension}"
        return str(next_available_path(candidate, style="paren"))