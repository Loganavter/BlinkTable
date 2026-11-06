from __future__ import annotations

from dataclasses import dataclass

from domain.types import Color

@dataclass(slots=True)
class ExportDialogState:
    current_language: str
    output_dir: str
    favorite_dir: str | None
    last_format: str
    quality: int
    png_compress_level: int
    fill_background: bool
    background_color: Color | None
    comment_text: str
    comment_keep_default: bool
    resolution_scale: float = 1.0
    # Image Compare: only when magnifier/uncrop grew the canvas past 0..1.
    # Multi Compare keeps True so transparent formats can still fill empty slots.
    virtual_canvas_active: bool = True
    # Image Compare only: trim the exported file to the area the images occupy
    # (virtual-canvas pads / letterbox background dropped). ``available`` is the
    # tab's capability flag, so the shared dialog hides the option for tabs that
    # have no such content rect (Multi Compare leaves it False).
    crop_to_image: bool = False
    crop_to_image_available: bool = False
    # Native (1x) size of that image area; the resolution fields follow it while
    # the option is on, so the field keeps meaning "what will be written".
    crop_native_size: tuple[int, int] | None = None
    # Preview raster of the cropped variant — the pane switches to it with the
    # option, so it shows exactly the file that will be written.
    crop_preview_image: object | None = None
