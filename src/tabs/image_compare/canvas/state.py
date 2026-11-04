from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QPixmap

from ui.canvas_infra.viewport.state import ensure_zoom_viewport_state


@dataclass(slots=True)
class _FeatureOverlayGpuState:
    _pixmap: QPixmap | None = None
    _top_left: QPoint | None = None
    _centers: list[QPointF] = field(default_factory=list)
    _radius: float = 0.0
    _border_color: QColor = field(default_factory=lambda: QColor(255, 255, 255, 248))
    _border_width: float = 2.0
    _quads: list = field(default_factory=list)
    _use_circle_mask: list[bool] = field(default_factory=list)
    _gpu_active: bool = False
    _gpu_slots: list = field(default_factory=list)
    _gpu_channel_mode: int = 0
    _gpu_diff_mode: int = 0
    _gpu_diff_threshold: float = 20.0 / 255.0
    _gpu_interp_mode: int = 1
    _gpu_widget_geometry_sig: tuple | None = None


@dataclass(slots=True)
class CanvasRuntimeState:
    _background_pixmap: QPixmap | None = None
    _images_uploaded: list[bool] = field(default_factory=lambda: [False, False])
    _stored_pil_images: list = field(default_factory=lambda: [None, None])
    _stored_image_ids: object | None = None
    _source_pil_images: list = field(default_factory=lambda: [None, None])
    _source_image_ids: list = field(default_factory=lambda: [0, 0])
    _source_images_ready: bool = False
    _diff_source_pil_image: object | None = None
    _diff_source_image_id: int = 0
    _diff_source_ready: bool = False
    _source_preload_scheduled: bool = False
    _shader_letterbox_mode: bool = False
    _content_rect_px: tuple[int, int, int, int] | None = None
    _inner_content_rect_px: tuple[int, int, int, int] | None = None
    _inner_split_position: float | None = None
    _content_sr: float = 1.0
    _clip_overlays_to_content_rect: bool = False
    _content_scissor_depth: int = 0
    _canvas_scene_graph: object | None = None
    _letterbox_params: list = field(default_factory=lambda: [None, None])
    # Expanding-canvas frame in widget UV + pad fill (video uncrop / export).
    # Disabled when zw==(0,0) or fill.a==0 — see base.frag canvasLetterbox.
    _canvas_frame_letterbox: tuple[float, float, float, float] | None = None
    _letterbox_fill_rgba: tuple[float, float, float, float] | None = None
    _store: object | None = None
    _render_scene: object | None = None
    _split_position_sync: object | None = None
    _apply_channel_mode_in_shader: bool = True
    _read_only: bool = False
    _update_batch_depth: int = 0
    _update_pending: bool = False
    _drag_overlay_visible: bool = False
    _drag_overlay_horizontal: bool = False
    _drag_overlay_texts: tuple[str, str] = ("", "")
    _drag_overlay_cache_key: object | None = None
    _drag_overlay_cached_image: object | None = None
    _pending_texture_uploads: list = field(default_factory=list)
    # OrderedDict (not plain dict) so cache reads can call move_to_end() to
    # track real recency -- see evict_texture_upload_cache_over_budget in
    # texture_parts/upload_queue.py and docs/dev/rendering/tile-rendering-system.md
    # Phase 2.
    _texture_upload_cache: OrderedDict = field(default_factory=OrderedDict)
    _host_texture_upload_cache: object | None = None
    # docs/dev/rendering/tile-rendering-system.md Phase 3: bounded LRU of
    # already-decoded QImages keyed by image_uid(), see
    # texture_parts/upload_queue.py's queue_texture_upload().
    _qimage_by_uid_cache: OrderedDict = field(default_factory=OrderedDict)
    _resize_overlay_sync_active: bool = False
    _zoom_viewport_state: object | None = None
    # See render_context.py's _schedule_glass_settle_frame: one extra
    # repaint after the last real content update, so
    # shared.rendering.glass_panel's render_backdrops() can catch up to the
    # colorTexture() that update just produced (it always reads the
    # *previous* frame's colorTexture() by design -- see that module's
    # docstring). Guards against stacking up multiple settle frames during
    # a burst of updates.
    _glass_settle_pending: bool = False
    _dynamic_feature_overrides: dict = field(default_factory=dict)
    _feature_overlay_gpu: _FeatureOverlayGpuState = field(
        default_factory=_FeatureOverlayGpuState
    )
    _capture_center: object | None = None
    _capture_radius: float = 0.0
    _capture_circles: list = field(default_factory=list)
    _guide_sets: list = field(default_factory=list)
    _hidden_capture_circles: list = field(default_factory=list)
    _occluded_capture_arcs: list = field(default_factory=list)
    _hidden_overlay_circles: list = field(default_factory=list)
    _show_guides: bool = False
    _laser_color: object = field(default_factory=QColor)
    _guides_thickness: int = 0
    _capture_color: object = field(default_factory=QColor)
    _export_canvas_viewport: tuple | None = None
    # Union letterbox hold after put_unified: keep prev union letterbox for
    # UNION_LETTERBOX_HOLD_MS (350ms) or until tile more_pending False.
    # See base_images.update_common_letterbox_geometry.
    _union_letterbox_hold_until: float = 0.0
    _tile_more_pending: bool = False
    #: One-shot guard for the PIL letterbox refresh scheduled from the resize
    #: path (render_context._schedule_pil_letterbox_refresh). It was written
    #: with setattr on this slots dataclass, so every resize raised
    #: AttributeError and the refresh never ran from that path.
    _pil_letterbox_refresh_scheduled: bool = False


def init_widget_state(widget):
    widget.setMouseTracking(True)

    widget.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    widget.setAutoFillBackground(True)

    widget._quad_vertices = np.array(
        [
            -1.0,
            1.0,
            0.0,
            0.0,
            -1.0,
            -1.0,
            0.0,
            1.0,
            1.0,
            1.0,
            1.0,
            0.0,
            1.0,
            -1.0,
            1.0,
            1.0,
        ],
        dtype=np.float32,
    )

    widget.split_position = 0.5
    widget.is_horizontal = False

    widget.shader_program = None
    widget.vao = None
    widget.vbo = None
    widget.textures = [None, None]

    widget.texture_ids = ["stored_0", "stored_1"]
    widget._source_texture_ids = ["source_0", "source_1"]
    widget._diff_source_texture_id = "diff"
    widget._feature_overlay_tex_ids = []
    widget._feature_overlay_aux_tex_ids = []
    widget._circle_mask_tex_id = 0
    widget._feature_overlay_tex_id = 0

    widget._feature_passes = []
    widget.runtime_state = CanvasRuntimeState()
    ensure_zoom_viewport_state(widget)
