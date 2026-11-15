"""Quick-save / dialog-save export orchestration for the Multi Compare tab --
split out of ``MultiCompareController`` to keep that class down to
wiring/composition, mirroring image_compare's own ``use_cases/`` split.
Every function here takes the controller as its first argument; the actual
background file write lives in ``services/save_flow.py`` +
``services/image_export.py``, this module only prepares the composed
``QImage``/options dict that gets handed to it.
Audit-Meta: pattern=thin-owner reason="Save admission gate (has_exportable_pixels refusal, policy B4) shares the module with the quick/dialog save flows it gates; entry validation beside the flows it validates"
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QDialog

from tabs.multi_compare.models import slot_ids_in_tree
from tabs.multi_compare.services.composition_builder import build_composition_plan
from tabs.multi_compare.services.gpu_export import MultiCompareGpuExporter
from tabs.multi_compare.services.save_flow import MultiCompareSaveFlowCoordinator
from ui.canvas_presentation.composition import compute_native_canvas_size
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)

#: Upper bound for the dialog's suggested filename (dialog field, not a
#: filesystem limit — owner decision 2026-10-28).
EXPORT_SUGGESTED_NAME_MAX_LEN = 80


def get_save_flow(controller) -> MultiCompareSaveFlowCoordinator:
    if controller._save_flow is None:
        main_window = getattr(controller.context, "main_window", None)
        thread_pool = getattr(controller.context, "thread_pool", None)
        controller._save_flow = MultiCompareSaveFlowCoordinator(
            main_window_app=main_window,
            tr_func=controller.translate,
            thread_pool=thread_pool,
        )
    return controller._save_flow


def background_color_from_settings(settings) -> QColor:
    color = getattr(settings, "export_background_color", None)
    if color is None:
        return QColor(20, 20, 20)
    if isinstance(color, QColor):
        return QColor(color)
    channels = (
        getattr(color, "r", 20),
        getattr(color, "g", 20),
        getattr(color, "b", 20),
        getattr(color, "a", 255),
    )
    return QColor(*channels)


def persist_export_preferences(controller, options: dict) -> None:
    settings = getattr(controller.store, "settings", None)
    if settings is None:
        return
    settings.export_default_dir = options["output_dir"]
    if "favorite_dir" in options:
        settings.export_favorite_dir = options["favorite_dir"]
    settings.export_last_format = options["format"]
    settings.export_quality = int(options["quality"])
    if "png_compress_level" in options:
        settings.export_png_compress_level = int(options["png_compress_level"])
    if "png_optimize" in options:
        settings.export_png_optimize = bool(options["png_optimize"])
    if bool(options.get("fill_background_editable", True)):
        settings.export_fill_background = bool(options["fill_background"])
    if "resolution_scale" in options:
        settings.export_resolution_scale = float(options["resolution_scale"])
    # "Save view" is a dispatched scalar (the direct-write ratchet pins the
    # legacy assignments in this function by line number). Dispatching keeps
    # quick save and the next dialog in step with what the user chose.
    try:
        from core.state_management.actions import SetExportSaveViewAction

        controller.store.get_dispatcher().dispatch(
            SetExportSaveViewAction(bool(options.get("save_view", False))),
            scope="settings",
        )
    except Exception:
        pass
    if "comment_text" in options:
        settings.export_comment_text = options["comment_text"]
    if "comment_keep_default" in options:
        settings.export_comment_keep_default = bool(options["comment_keep_default"])
    try:
        from domain.types import Color

        settings.export_background_color = Color(*options["background_color"])
    except Exception:
        pass


def set_favorite_dir(controller, path: str) -> None:
    settings = getattr(controller.store, "settings", None)
    if settings is not None:
        settings.export_favorite_dir = path


def default_dir(controller) -> str:
    if controller.store is not None:
        settings = getattr(controller.store, "settings", None)
        if settings is not None:
            d = getattr(settings, "export_default_dir", None)
            if d:
                return d
    return str(Path.home())


def live_view_size(controller) -> tuple[int, int]:
    width = max(1, int(controller.widget.canvas.width()))
    height = max(1, int(controller.widget.canvas.height()))
    return width, height


def _cropped_export_source(source: object, box: tuple[int, int, int, int]) -> object | None:
    """Crop-then-compose helper: ``source`` clipped to ``box`` (full-frame px).

    Returns the cropped in-memory source, the original ``source`` when the
    box covers the full frame (fail-open to pre-change behavior), and
    ``None`` when the intersection is empty (fail-closed gap — caller skips
    the slot, same documented skip as an imageless leaf). Never raises;
    never decodes (``TiledPixelStore.crop`` / ``QImage.copy`` / ``PIL.crop``
    / numpy slice level only).
    """
    try:
        from engine.store.crop_window import intersect_box
        from engine.store.tiled_pixel_store import TiledPixelStore, pixel_source_size
        try:
            full_w, full_h = pixel_source_size(source)
        except Exception:
            return source
        if full_w <= 0 or full_h <= 0:
            return source
        full = (0, 0, int(full_w), int(full_h))
        try:
            clipped = intersect_box(full, box)
        except Exception:
            return source
        if clipped is None:
            return None
        if tuple(clipped) == tuple(full):
            return source
        left, top, right, bottom = (int(v) for v in clipped)
        if isinstance(source, TiledPixelStore):
            try:
                return TiledPixelStore.from_pil(source.crop(clipped))
            except Exception:
                return source
        try:
            from PySide6.QtGui import QImage
            if isinstance(source, QImage):
                try:
                    return source.copy(left, top, right - left, bottom - top)
                except Exception:
                    return source
        except Exception:
            pass
        try:
            from PIL import Image as _PILImage
            if isinstance(source, _PILImage.Image):
                try:
                    return source.crop((left, top, right, bottom))
                except Exception:
                    return source
        except Exception:
            pass
        try:
            import numpy as np
            if isinstance(source, np.ndarray):
                try:
                    return np.ascontiguousarray(source[top:bottom, left:right].copy())
                except Exception:
                    return source
        except Exception:
            pass
        return source
    except Exception:
        return source


def _export_sources(controller) -> dict[int, object]:
    """Resolved pixel sources for export composition (B1).

    Cache first; on miss a synchronous demand fill (export already runs
    synchronously, so an evicted-but-present file must not silently drop
    out of the saved image the way an imageless leaf would). Unreadable
    files stay imageless (documented skip, same as before B1).

    Parametric crop (plan §3 export contract): each slot's cached FULL
    store is clipped in-memory to its ``effective_crop_box`` before
    composition (crop-then-compose, never a second decode). A slot whose
    intersection is empty is skipped (documented gap, same as imageless).
    Uncropped slots keep the cached object itself (byte-identical to
    pre-change behavior).
    """
    sources: dict[int, object] = {}
    cache = getattr(controller, "pixel_cache", None)
    if cache is None:
        return sources
    try:
        crop_service = controller._get_crop_service()
    except Exception:
        crop_service = None
    try:
        slots = list(controller.widget.state.slots)
    except Exception:
        return sources
    for slot in slots:
        try:
            source = cache.resolve(slot.path)
            if source is None and slot.path is not None:
                source = cache.get_or_load_pixel(slot.path, crop_service=crop_service)
            if source is None:
                continue
            try:
                from tabs.multi_compare.use_cases.crop import effective_crop_box

                box = effective_crop_box(controller, slot)
            except Exception:
                box = None
            if box is not None:
                try:
                    cropped = _cropped_export_source(source, box)
                except Exception:
                    cropped = source
                if cropped is None:
                    continue
                source = cropped
            sources[int(slot.id)] = source
        except Exception:
            continue
    return sources


def native_canvas_size(controller) -> tuple[int, int] | None:
    """Smallest canvas where every loaded slot renders at native resolution.

    Delegates to the composition module so live render, export, and the
    export dialog's suggested resolution all share one source of truth.
    """
    plan = build_composition_plan(
        controller.widget.state, include_labels=False, sources=_export_sources(controller)
    )
    if plan is None:
        return None
    return compute_native_canvas_size(plan.root)


def _native_canvas_size_safe(controller) -> tuple[int, int] | None:
    """``controller._native_canvas_size()`` without raising on a bare fake."""
    try:
        size = controller._native_canvas_size()
    except Exception:
        return None
    if not size:
        return None
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def save_view_state_fields(controller) -> dict:
    """The three dialog-state fields the host export dialog reads (save view).

    The scene camera is the tab's camera (``scene/projection.py``); the rect
    comes from the shared ``visible_view_rect`` helper so the exported crop is
    exactly the region the canvas shows. save_view_available is False at
    identity camera: saving "the view" is then the whole composition, i.e. the
    option would be a no-op (the dialog greys it out rather than promising a
    crop it would not apply).

    Fail-open: an unknown canvas size yields ``None`` rect/size and an
    unavailable option, never an exception into the dialog.
    """
    # Function-local imports (module style here): keeps this module's import
    # block — and therefore every line number above — untouched, because the
    # no-direct-settings-mutation contract pins the pre-existing
    # ``settings.*`` writes in this file by line number.
    from shared.image_processing.view_geometry import (
        visible_view_rect,
        view_native_size,
    )
    from tabs.multi_compare.scene.projection import (
        scene_camera,
        scene_camera_is_identity,
    )

    state = getattr(getattr(controller, "widget", None), "state", None)
    camera = scene_camera(state)
    zoom, pan_x, pan_y = camera
    rect: tuple[int, int, int, int] | None = None
    size: tuple[int, int] | None = None
    native = _native_canvas_size_safe(controller)
    if native is not None:
        rect = visible_view_rect(native[0], native[1], zoom, pan_x, pan_y)
        size = view_native_size(native[0], native[1], zoom, pan_x, pan_y)
    return {
        "save_view_available": not scene_camera_is_identity(camera),
        "view_rect": rect,
        "view_native_size": size,
    }


def _save_view_crop(options: dict) -> tuple[int, int, int, int] | None:
    """The crop rect an export run must apply, or ``None`` for the plain path.

    ``options["save_view"]`` is set by the dialog (checkbox) or by
    :func:`build_quick_export_options` from the persisted setting; the rect is
    the snapshot the dialog showed. Malformed/absent rects degrade to the
    historical full-frame path instead of failing the save.
    """
    try:
        if not bool(options.get("save_view")):
            return None
        raw = options.get("view_rect")
        if not raw or len(raw) != 4:
            return None
        left, top, width, height = (int(round(float(v))) for v in raw)
    except (TypeError, ValueError, KeyError):
        return None
    if width <= 0 or height <= 0:
        return None
    return max(0, left), max(0, top), width, height


def _pil_lanczos():
    """Pillow's LANCZOS resample filter, across Pillow majors."""
    from PIL import Image

    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return resampling.LANCZOS
    return Image.LANCZOS


def compose_export_image(controller, options: dict):
    """Compose the export frame for ``options`` as a PIL image.

    Single entry for the dialog save and the quick save, so the save-view
    branch cannot exist in one flow and not the other. Save view renders the
    composition at its NATIVE canvas size (one canvas px per output px — the
    density the live view has at scene zoom 1), crops the visible rect the
    dialog showed, then resamples to ``width`` x ``height`` only when the
    requested output differs from the crop. Without the option this is the
    historical compose-at-output-size path, unchanged.
    """
    from shared.image_processing.qt_conversion import qimage_to_pil

    width = max(1, int(options["width"]))
    height = max(1, int(options["height"]))
    background_color = QColor(*options["background_color"])
    fill_background = bool(options["fill_background"])
    rect = _save_view_crop(options)
    native = _native_canvas_size_safe(controller) if rect is not None else None
    if rect is None or native is None:
        image = controller._compose_image(
            width,
            height,
            background_color=background_color,
            fill_background=fill_background,
        )
        return qimage_to_pil(image)
    image = controller._compose_image(
        native[0],
        native[1],
        background_color=background_color,
        fill_background=fill_background,
    )
    pil_image = qimage_to_pil(image)
    left, top, crop_w, crop_h = rect
    left = min(left, max(0, pil_image.width - 1))
    top = min(top, max(0, pil_image.height - 1))
    right = min(pil_image.width, left + crop_w)
    bottom = min(pil_image.height, top + crop_h)
    if right > left and bottom > top:
        pil_image = pil_image.crop((left, top, right, bottom))
    if (pil_image.width, pil_image.height) != (width, height):
        pil_image = pil_image.resize((width, height), _pil_lanczos())
    return pil_image


def compose_image(
    controller,
    w: int | None = None,
    h: int | None = None,
    *,
    background_color: QColor | None = None,
    fill_background: bool = False,
) -> QImage:
    """Render the multi-compare scene at ``w x h``.

    The composition canvas is always the native size (image-extent driven);
    ``w x h`` is the framebuffer / output. The renderer letterboxes the
    canvas into it via ``sr = min(w/canvas_w, h/canvas_h)``.
    """
    state = controller.widget.state
    composition = build_composition_plan(state, sources=_export_sources(controller))
    if composition is None:
        return QImage(
            max(1, int(w or controller.SAVE_OUTPUT_W)),
            max(1, int(h or controller.SAVE_OUTPUT_H)),
            QImage.Format.Format_RGBA8888,
        )
    output_w = int(w) if w else composition.canvas_w
    output_h = int(h) if h else composition.canvas_h
    if controller._gpu_exporter is None:
        controller._gpu_exporter = MultiCompareGpuExporter()
    return controller._gpu_exporter.render_to_qimage(
        composition,
        output_w=output_w,
        output_h=output_h,
        background_color=background_color,
        fill_background=fill_background,
    )


def render_export_preview(
    controller,
    width: int,
    height: int,
    background_color: QColor,
    fill_background: bool,
) -> QPixmap:
    longest = max(int(width), int(height))
    if longest > controller.PREVIEW_MAX_EDGE:
        scale = controller.PREVIEW_MAX_EDGE / float(longest)
        width = max(1, int(round(width * scale)))
        height = max(1, int(round(height * scale)))
    return QPixmap.fromImage(
        controller._compose_image(
            width,
            height,
            background_color=background_color,
            fill_background=fill_background,
        )
    )


def export_dialog_state_kwargs(controller) -> dict:
    """Raw field values for the host's export dialog state.

    Kept as a plain dict (not a dataclass import) so this module never
    imports the host's export-dialog package -- the
    "open_image_export_dialog" host service (see ui/main_window/layouts.py)
    owns building the actual dialog-state object.
    """
    from domain.qt_adapters import qcolor_to_color

    settings = getattr(controller.store, "settings", None)
    fields = dict(
        current_language=getattr(settings, "current_language", "en"),
        output_dir=default_dir(controller),
        favorite_dir=getattr(settings, "export_favorite_dir", None),
        last_format=getattr(settings, "export_last_format", "PNG"),
        quality=int(getattr(settings, "export_quality", 95) or 95),
        png_compress_level=int(
            getattr(settings, "export_png_compress_level", 9) or 9
        ),
        fill_background=bool(getattr(settings, "export_fill_background", False)),
        background_color=qcolor_to_color(
            background_color_from_settings(settings)
        ),
        comment_text=getattr(settings, "export_comment_text", "") or "",
        comment_keep_default=bool(
            getattr(settings, "export_comment_keep_default", False)
        ),
        resolution_scale=float(
            getattr(settings, "export_resolution_scale", 1.0) or 1.0
        ),
        virtual_canvas_active=True,
    )
    # Save-view ("export what the canvas shows"): the scene camera decides both
    # whether the option is applicable and which rect it would crop. The
    # remembered choice is the persisted scalar; the dialog hides the option
    # when the camera is at identity, so a stale True is inert.
    fields.update(save_view_state_fields(controller))
    fields["save_view"] = bool(
        getattr(settings, "export_save_view", False)
    )
    return fields


def resolve_quick_save_output_dir(controller) -> str:
    settings = getattr(controller.store, "settings", None) if controller.store else None
    if settings is not None:
        if (
            getattr(settings, "export_use_default_dir", True)
            and getattr(settings, "export_default_dir", None)
        ):
            return settings.export_default_dir
        favorite = getattr(settings, "export_favorite_dir", None)
        if favorite:
            return favorite
        default = getattr(settings, "export_default_dir", None)
        if default:
            return default
    return str(Path.home())


def build_quick_export_options(controller) -> dict:
    """Options for quick save -- last export prefs, no dialog interaction."""
    settings = getattr(controller.store, "settings", None)
    native_w, native_h = controller._native_canvas_size() or live_view_size(controller)
    scale = float(
        getattr(settings, "export_resolution_scale", 1.0) or 1.0
    ) if settings is not None else 1.0
    bg = background_color_from_settings(settings)
    keep_comment = bool(
        getattr(settings, "export_comment_keep_default", False)
    ) if settings is not None else False
    options = {
        "output_dir": resolve_quick_save_output_dir(controller),
        "file_name": "multi_compare",
        "format": (
            getattr(settings, "export_last_format", "PNG") or "PNG"
        ) if settings is not None else "PNG",
        "quality": int(
            getattr(settings, "export_quality", 95) or 95
        ) if settings is not None else 95,
        "png_compress_level": int(
            getattr(settings, "export_png_compress_level", 9) or 9
        ) if settings is not None else 9,
        "png_optimize": bool(
            getattr(settings, "export_png_optimize", True)
        ) if settings is not None else True,
        "fill_background": bool(
            getattr(settings, "export_fill_background", False)
        ) if settings is not None else False,
        "background_color": (bg.red(), bg.green(), bg.blue(), bg.alpha()),
        "comment_text": (
            getattr(settings, "export_comment_text", "") or ""
        ) if keep_comment and settings is not None else "",
        "include_metadata": keep_comment,
        "width": max(1, int(round(native_w * scale))),
        "height": max(1, int(round(native_h * scale))),
        "is_quick_save": True,
    }
    # Persisted "save view" preference: when the scene camera is zoomed, a
    # quick save writes what the canvas shows (native-density crop), not the
    # whole composition. Absent setting = off; identity camera = option has
    # nothing to crop, so it stays off too.
    from shared.image_processing.view_geometry import (
        visible_view_rect,
        view_native_size,
    )
    from tabs.multi_compare.scene.projection import (
        scene_camera,
        scene_camera_is_identity,
    )

    save_view_pref = bool(
        getattr(settings, "export_save_view", False)
    ) if settings is not None else False
    camera = scene_camera(getattr(getattr(controller, "widget", None), "state", None))
    if save_view_pref and not scene_camera_is_identity(camera):
        zoom, pan_x, pan_y = camera
        rect = visible_view_rect(native_w, native_h, zoom, pan_x, pan_y)
        view_w, view_h = view_native_size(native_w, native_h, zoom, pan_x, pan_y)
        options["save_view"] = True
        options["view_rect"] = rect
        options["width"] = max(1, int(round(view_w * scale)))
        options["height"] = max(1, int(round(view_h * scale)))
    return options


def has_exportable_pixels(controller) -> bool:
    """Pixels gate for export admission (B4: IC ``has_images`` parity).

    True when at least one in-tree slot resolves a cached pixel source
    via the single boolean gate (:func:`pipeline.cache.slot_has_pixels` —
    the same gate backing the context menu, drag & drop, and demand
    fill). A tree of imageless slots (no tier yet, evicted, or pathless)
    reads as non-exportable so quick/dialog save refuses instead of
    writing a blank image. Cache-only read: never decodes, never raises.
    """
    try:
        root = controller.widget.state.root
    except Exception:
        return False
    try:
        wanted = slot_ids_in_tree(root)
    except Exception:
        return False
    if not wanted:
        return False
    try:
        slots = list(controller.widget.state.slots)
    except Exception:
        return False
    by_id = {}
    for slot in slots:
        try:
            by_id[int(slot.id)] = slot
        except Exception:
            continue
    try:
        from tabs.multi_compare.pipeline.cache import slot_has_pixels
    except Exception:
        return False
    cache = getattr(controller, "pixel_cache", None)
    for sid in wanted:
        slot = by_id.get(sid)
        if slot is None:
            continue
        try:
            if slot_has_pixels(cache, slot):
                return True
        except Exception:
            continue
    return False


def on_quick_save_requested(controller) -> None:
    """Save immediately with last export settings -- no dialog."""
    if not slot_ids_in_tree(controller.widget.state.root):
        return
    if not has_exportable_pixels(controller):
        return
    try:
        options = build_quick_export_options(controller)
        pil_image = compose_export_image(controller, options)
        controller._get_save_flow().start_save_worker(pil_image, options)
    except Exception:
        logger.exception("Multi Compare quick save failed")


def sanitize_export_name(value: str) -> str:
    """Replace filesystem-hostile characters in a proposed export name.

    Deliberately a copy of image_compare's one-liner
    (``services/image_export/state.py`` ``sanitize_export_name``) rather than
    a shared import: the rule is a single ``re.sub`` and the owner chose a
    local copy over a new cross-tab seam. Keep the two in sync if the
    character class ever changes.
    """
    return re.sub(r'[\\/*?:"<>|]', "_", value)


def _slot_export_stem(slot) -> str:
    """Name source for one slot: ``path`` basename without extension, else ``label``."""
    path = getattr(slot, "path", None)
    if path:
        try:
            stem = Path(path).stem
        except (TypeError, ValueError):
            stem = ""
        if stem:
            return stem
    return str(getattr(slot, "label", "") or "")


def _filled_export_slots(controller) -> list:
    """Slots that carry a name source (path stem or non-empty label).

    Reads the active session's ``multi_compare.state`` exactly the way the
    rest of this module does — ``controller.widget.state`` is the bound
    store's session slot, not a second copy of it.
    """
    try:
        slots = list(controller.widget.state.slots)
    except Exception:
        return []
    return [slot for slot in slots if _slot_export_stem(slot)]


def build_suggested_export_filename(controller) -> str:
    """Suggested dialog name: ``<stem of first filled slot>_<N>images``.

    N is the number of filled slots, so a four-up grid reads
    "AniScale2_DITN_i16_75K_4images". Falls back to the bare literal
    ``multi_compare`` when no slot carries a path or label. Total length is
    capped at :data:`EXPORT_SUGGESTED_NAME_MAX_LEN` by trimming the stem —
    the ``_<N>images`` tail always survives whole. No uniquification (by
    owner decision Multi Compare has none, unlike image compare).
    """
    filled = _filled_export_slots(controller)
    if not filled:
        return "multi_compare"
    suffix = f"_{len(filled)}images"
    stem = sanitize_export_name(_slot_export_stem(filled[0])) or "multi_compare"
    budget = max(1, EXPORT_SUGGESTED_NAME_MAX_LEN - len(suffix))
    return f"{stem[:budget]}{suffix}"


def on_save_requested(controller) -> None:
    if not slot_ids_in_tree(controller.widget.state.root):
        return
    if not has_exportable_pixels(controller):
        return

    native_size = controller._native_canvas_size() or live_view_size(controller)

    if not callable(controller.open_export_dialog):
        logger.error("MultiCompareController: no export dialog service wired")
        return

    preview = render_export_preview(
        controller,
        *native_size,
        background_color_from_settings(getattr(controller.store, "settings", None)),  # type: ignore[call-arg]  # width/height are unpacked from native_size
        bool(
            getattr(
                getattr(controller.store, "settings", None),
                "export_fill_background",
                False,
            )
        ),
    )

    controller.open_export_dialog(
        dialog_state=export_dialog_state_kwargs(controller),
        preview_image=preview,
        suggested_filename=build_suggested_export_filename(controller),
        native_size=native_size,
        on_set_favorite_dir=lambda path: set_favorite_dir(controller, path),
        on_finished=lambda code, options: start_composite_save(controller, code, options),
    )


def start_composite_save(controller, result_code, options: dict) -> None:
    """Compose and save once the (non-modal) export dialog reports back.

    Continuation of :func:`on_save_requested` — the dialog returns immediately
    now, so this runs from its ``finished`` handler. ``code != Accepted`` is a
    cancel: nothing is composed, nothing is written.
    """
    if int(result_code) != int(QDialog.DialogCode.Accepted):
        return
    try:
        pil_image = compose_export_image(controller, options)

        persist_export_preferences(controller, options)
        controller._get_save_flow().start_save_worker(pil_image, options)
    except Exception:
        logger.exception("Composite save failed")