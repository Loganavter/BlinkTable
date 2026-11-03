"""Video / still snapshot frame rendering for image_compare.

Package layout:

- ``models`` — ``PreparedCanvasFrame``, cache entry shapes
- ``caches`` — ``FrameRenderCaches``
- ``geometry`` / ``images`` / ``store_rebuild`` — pure helpers
- ``prepare_hit`` / ``prepare_miss`` / ``prepare`` — prepare pipeline
- ``render`` — GPU composite of a prepared frame
- ``renderer`` — ``SnapshotFrameRenderer`` orchestrator
- ``strip_window`` — the video strip's visible blocks as ONE GPU pass (window
  planning + per-tile packing + generation/cancellation, CPU side)
- ``strip_pass`` — the GPU half of that pass (per-RHI resources, uploads,
  one ``beginPass``/``endPass``), driven by ``strip_window.render_window``
"""

from __future__ import annotations

from tabs.image_compare.services.video_snapshot_rendering.models import PreparedCanvasFrame
from tabs.image_compare.services.video_snapshot_rendering.renderer import (
    SnapshotFrameRenderer,
)

__all__ = ["PreparedCanvasFrame", "SnapshotFrameRenderer"]
