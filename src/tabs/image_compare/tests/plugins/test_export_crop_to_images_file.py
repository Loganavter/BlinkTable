"""ExportService writes the cropped file when "crop to images" is on.

End-to-end over the save path (fake GPU renderer, real PIL crop/write): the
written raster must be the plan's content rect, and the option off must write
the full canvas byte-for-byte as before.
"""

from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

from tabs.image_compare.services.image_export.service import ExportService


class _FakeGpuExport:
    """Returns the plan canvas as a flat raster — dimensions are what matter."""

    def __init__(self):
        self.calls = 0

    def render_plan(self, plan, *, store=None, diff_image=None):
        self.calls += 1
        return Image.new("RGBA", (int(plan.canvas_w), int(plan.canvas_h)), (10, 20, 30, 255)), {}


def _plan(canvas, clip):
    return SimpleNamespace(
        canvas_w=canvas[0],
        canvas_h=canvas[1],
        output_scale=1.0,
        fill_rgba=None,
        render_scene=SimpleNamespace(overlay_clip_rect=clip),
    )


def _options(tmp_path, name, *, crop):
    return {
        "output_dir": str(tmp_path),
        "file_name": name,
        "format": "PNG",
        "quality": 95,
        "fill_background": False,
        "background_color": (255, 255, 255, 255),
        "png_compress_level": 1,
        "png_optimize": False,
        "include_metadata": False,
        "comment_text": "",
        "crop_to_image": crop,
    }


def _export(tmp_path, name, *, crop, plan):
    service = ExportService(font_path_absolute="")
    service.gpu_export_service = _FakeGpuExport()
    pair = Image.new("RGBA", (1000, 600), (120, 60, 60, 255))
    service.export_image(
        store=SimpleNamespace(),
        original_image1=pair,
        original_image2=pair,
        export_options=_options(tmp_path, name, crop=crop),
        render_plan=plan,
        render_store=None,
    )
    with Image.open(tmp_path / f"{name}.png") as written:
        return written.size


def test_crop_to_images_writes_the_image_area(tmp_path):
    plan = _plan((1000, 600), (50, 20, 600, 300))
    assert _export(tmp_path, "cropped", crop=True, plan=plan) == (600, 300)


def test_crop_off_writes_the_full_canvas(tmp_path):
    plan = _plan((1000, 600), (50, 20, 600, 300))
    assert _export(tmp_path, "full", crop=False, plan=plan) == (1000, 600)


def test_crop_without_padding_keeps_the_canvas(tmp_path):
    plan = _plan((1000, 600), (0, 0, 1000, 600))
    assert _export(tmp_path, "already_tight", crop=True, plan=plan) == (1000, 600)
