"""Export "crop to images": the raster trims to the plan's content rect.

The option must keep parity between the preview and the file: one rect, scaled
to whatever raster the renderer produced. A plan whose content already covers
the canvas must come back untouched (today's export is the default).
"""

from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

from tabs.image_compare.services.image_export.content_crop import (
    content_rect_for_plan,
    crop_to_plan_content,
    scale_rect_to_raster,
)


def _plan(canvas, clip):
    return SimpleNamespace(
        canvas_w=canvas[0],
        canvas_h=canvas[1],
        render_scene=SimpleNamespace(overlay_clip_rect=clip),
    )


def test_content_rect_is_none_when_the_images_fill_the_canvas():
    plan = _plan((1000, 600), (0, 0, 1000, 600))
    assert content_rect_for_plan(plan) is None


def test_content_rect_reports_the_padded_window():
    plan = _plan((1000, 600), (50, 20, 600, 300))
    assert content_rect_for_plan(plan) == (50, 20, 600, 300, 1000, 600)


def test_content_rect_without_a_plan_is_none():
    assert content_rect_for_plan(None) is None


def test_scale_rect_maps_plan_px_to_the_raster():
    rect = (50, 20, 600, 300, 1000, 600)
    assert scale_rect_to_raster(rect, (1000, 600)) == (50, 20, 650, 320)
    # Half-size preview: same window, half the pixels.
    assert scale_rect_to_raster(rect, (500, 300)) == (25, 10, 325, 160)
    # Degenerate raster -> no crop.
    assert scale_rect_to_raster(rect, (0, 0)) is None


def test_crop_keeps_the_image_area_and_drops_the_pads():
    plan = _plan((1000, 600), (50, 20, 600, 300))
    image = Image.new("RGBA", (1000, 600), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (600, 300), (200, 40, 40, 255)), (50, 20))
    cropped = crop_to_plan_content(image, plan)
    assert cropped.size == (600, 300)
    assert cropped.getpixel((0, 0)) == (200, 40, 40, 255)


def test_crop_is_a_no_op_without_padding():
    plan = _plan((1000, 600), (0, 0, 1000, 600))
    image = Image.new("RGBA", (1000, 600), (10, 20, 30, 255))
    assert crop_to_plan_content(image, plan) is image


def test_crop_scales_with_the_raster():
    plan = _plan((1000, 600), (50, 20, 600, 300))
    image = Image.new("RGBA", (500, 300), (0, 0, 0, 0))
    cropped = crop_to_plan_content(image, plan)
    assert cropped.size == (300, 150)
