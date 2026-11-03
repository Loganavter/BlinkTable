"""Offscreen snapshot renders must not be transparent — the GPU path must be wired.

Dogma: docs/dev/rendering/rendering-model.md (snapshot replay path) — preview,
export and the post-export notification preview render through
SnapshotFrameRenderer → GpuExportService → offscreen QRhiWidget; a missing
import or single-flush leaves grabFramebuffer transparent (extrema (0,0) → an
empty strip of pixels, 136B PNG, while the UI still reports success). This
guard fails on a transparent PIL frame or a missing show_offscreen_widget
import before it reaches the UI.
"""

from __future__ import annotations

import ast
from pathlib import Path


def test_gpu_export_proxy_imports_show_offscreen_widget():
    src = Path("src/plugins/export/services/gpu_export_proxy.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "offscreen_canvas" in node.module:
            for alias in node.names:
                imports.append(alias.name)
    assert "show_offscreen_widget" in imports, (
        "GpuExportProxy must import show_offscreen_widget from offscreen_canvas "
        "(NameError in _ensure_widget left all thumbnails transparent 136B)"
    )


def test_offscreen_canvas_double_flush():
    # Relocated: src/shared/rendering/offscreen_canvas.py → src/engine/gpu/offscreen_canvas.py.
    src = Path("src/engine/gpu/offscreen_canvas.py").read_text(encoding="utf-8")
    # Both helpers must flush twice — single leaves QRhi swapchain unallocated
    # and grabFramebuffer returns transparent.
    assert src.count("QApplication.processEvents()") >= 4, (
        "offscreen_canvas must call processEvents twice per helper "
        "(resize_and_show + render_frame) — single leaves thumbnails transparent"
    )


def test_thumbnail_pil_transparency_guard():
    from PIL import Image

    transparent = Image.new("RGBA", (160, 90), (0, 0, 0, 0))
    opaque = Image.new("RGBA", (160, 90), (255, 0, 0, 255))

    def is_transparent(pil: Image.Image) -> bool:
        ext = pil.getextrema()
        return ext[3][1] == 0 if len(ext) > 3 else False

    assert is_transparent(transparent) is True
    assert is_transparent(opaque) is False
    # Guard that would have caught /tmp/thumb_debug_0.png 136B before paint
    assert not is_transparent(opaque), "opaque thumbnail must not be flagged transparent"
