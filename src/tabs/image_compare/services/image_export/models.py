from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ExportSaveContext:
    original1_full: object
    original2_full: object
    image1_for_save: object
    image2_for_save: object
    render_plan: object | None
    render_store: object | None
    preview_img: object | None
    suggested_filename: str
    native_width: int = 0
    native_height: int = 0
    virtual_canvas_active: bool = False
    # "Crop to images" variant: the pair cropped to the effective crop windows
    # (what the live canvas shows), rendered on its own so the preview and the
    # file agree. ``None`` when the pair has no crop window — the full variant
    # is then the only one, and the option is a no-op.
    crop_native_size: tuple[int, int] | None = None
    crop_plan: object | None = None
    crop_store: object | None = None
    crop_image1_for_save: object | None = None
    crop_image2_for_save: object | None = None
    crop_original1: object | None = None
    crop_original2: object | None = None
    crop_preview_img: object | None = None

    def has_crop_variant(self) -> bool:
        # The preview is optional (quick save builds no preview at all); the
        # plan is what the save actually renders.
        return self.crop_plan is not None

    def select(self, export_options: dict) -> ExportSaveContext:
        """Pick the variant the accepted options ask for (same object otherwise)."""
        if not bool(export_options.get("crop_to_image", False)) or not self.has_crop_variant():
            return self
        return ExportSaveContext(
            original1_full=self.crop_original1 or self.original1_full,
            original2_full=self.crop_original2 or self.original2_full,
            image1_for_save=self.crop_image1_for_save or self.image1_for_save,
            image2_for_save=self.crop_image2_for_save or self.image2_for_save,
            render_plan=self.crop_plan,
            render_store=self.crop_store,
            preview_img=self.crop_preview_img,
            suggested_filename=self.suggested_filename,
            native_width=self.crop_native_size[0] if self.crop_native_size else self.native_width,
            native_height=self.crop_native_size[1] if self.crop_native_size else self.native_height,
            virtual_canvas_active=self.virtual_canvas_active,
            crop_native_size=self.crop_native_size,
            crop_plan=self.crop_plan,
            crop_store=self.crop_store,
            crop_image1_for_save=self.crop_image1_for_save,
            crop_image2_for_save=self.crop_image2_for_save,
            crop_original1=self.crop_original1,
            crop_original2=self.crop_original2,
            crop_preview_img=self.crop_preview_img,
        )
