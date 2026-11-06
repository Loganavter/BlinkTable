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
    # "Crop to images": the image area (native px) and the same rect in preview
    # px, so the dialog can show the file it will write. ``None`` = the plan has
    # no padding to trim (full canvas == content).
    crop_native_size: tuple[int, int] | None = None
    preview_crop_rect: tuple[int, int, int, int] | None = None
