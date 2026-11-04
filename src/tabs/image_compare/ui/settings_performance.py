"""Image-compare specific groups appended to the platform ``builtin.performance`` settings page.

Resolution, interactive-optimization and video-recording controls only make
sense for the image-compare tab; render-backend is platform-owned and lives in
:mod:`plugins.settings.pages.performance`.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy
from sli_ui_toolkit.managers import as_nav_row, scaled_px
from sli_ui_toolkit.widgets import CheckBox, ComboBox, SpinBox

from plugins.settings.typography import (
    apply_settings_body,
    settings_body_label,
    settings_group,
)

from core.constants import AppConstants
from plugins.settings.search import SearchIndex, group
from shared.video_policy import FpsPolicy, clamp_fps

INTERACTIVE = group(
    "settings.interactive_optimization",
    "settings.optimize_magnifier_movement",
    "settings.optimize_laser_smoothing",
    "settings.magnifier_intersection_highlight",
    "settings.magnifier_auto_color_new_instances",
    "settings.magnifier_border_thickness",
    "settings.magnifier_capture_thickness",
)
VIDEO = group("image_compare.settings.video_recording", "image_compare.settings.recording_fps")
SEARCH = SearchIndex.of(INTERACTIVE, VIDEO)


def build_image_perf_extras(dialog, p) -> list:
    """Build this tab's performance extras and return their nav rows.

    The caller (``plugins/settings/pages/analysis.py`` /
    ``performance.py``) feeds the returned rows into its page's
    ``NavRowBuilder`` so they participate in keyboard navigation — these
    used to be added straight to the layout via ``add_layout()``, which
    made them permanently unreachable by Up/Down (see
    docs/legacy/plan_navigation_descriptor_unification.md §2.2).
    """
    layout = getattr(dialog, "_perf_layout", None)
    if layout is None:
        return []
    rows = []
    rows += _build_interactive_optimization_group(dialog, layout, p)
    rows += _build_video_group(dialog, layout, p)
    return rows


def _build_interactive_optimization_group(dialog, layout, p):
    dialog.interactive_opt_group = settings_group(dialog, INTERACTIVE)
    rows = []

    # «Zoom Interpolation» is gone from the panel (owner decision 2026-11-04):
    # live zoom is nearest-neighbour, period — the setting only let a canvas
    # resample while zoomed and read as a silent downscale/blur. Export keeps
    # its own high-quality filter (``gpu_export_scene`` overrides the
    # render-scene value), so nothing user-visible loses quality.
    # «Перемещение лупы» is gone from the panel (owner decision 2026-10-26):
    # the interactive-shortcut it toggled stopped being a quality trade once the
    # loupe had one filter (ImageRenderConfigReducer) — it only decided whether
    # keyboard/drag movement took the interactive refresh path, which is the
    # behaviour we want anyway. The widget stays built off-layout because the
    # dialog data/locale plumbing resolves it; the state is pinned True below.
    dialog.optimize_movement_checkbox = CheckBox(
        INTERACTIVE.text(dialog, "settings.optimize_magnifier_movement")
    )
    apply_settings_body(dialog.optimize_movement_checkbox)
    dialog.optimize_movement_checkbox.setChecked(True)
    # The loupe has ONE interpolation (see ImageRenderConfigReducer): no
    # second, movement-only filter is offered. The widget is still built because
    # the dialog data/locale plumbing reads it, but it never reaches the row —
    # a hidden value that only applied while dragging read as a bug ("Nearest"
    # shown in the panel, bicubic drawn during a drag, owner report 2026-10-26).
    dialog.combo_mag_interp = ComboBox()
    apply_settings_body(dialog.combo_mag_interp)
    dialog.combo_mag_interp.setMinimumWidth(scaled_px(140))
    dialog.combo_mag_interp.setSizePolicy(
        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
    )

    # «Сглаживание лазеров» is gone from the panel (owner decision 2026-10-26:
    # the laser guides are drawn as thin lines where a resampling filter is
    # invisible, so the toggle only added a state that looked broken). The
    # widgets stay built off-layout because the dialog data/locale plumbing
    # still resolves them; nothing reads them for behaviour any more.
    dialog.laser_smoothing_checkbox = CheckBox(
        INTERACTIVE.text(dialog, "settings.optimize_laser_smoothing")
    )
    apply_settings_body(dialog.laser_smoothing_checkbox)
    dialog.laser_smoothing_checkbox.setChecked(p.optimize_laser_smoothing)
    dialog.combo_laser_interp = ComboBox()
    apply_settings_body(dialog.combo_laser_interp)
    dialog.combo_laser_interp.setMinimumWidth(scaled_px(140))

    dialog.magnifier_intersection_highlight_checkbox = CheckBox(
        INTERACTIVE.text(dialog, "settings.magnifier_intersection_highlight")
    )
    apply_settings_body(dialog.magnifier_intersection_highlight_checkbox)
    dialog.magnifier_intersection_highlight_checkbox.setChecked(
        p.magnifier_intersection_highlight_enabled
    )
    INTERACTIVE.tag_member(
        dialog.magnifier_intersection_highlight_checkbox,
        "settings.magnifier_intersection_highlight",
    )
    intersection_row = as_nav_row(dialog.magnifier_intersection_highlight_checkbox)
    dialog.interactive_opt_group.add_widget(intersection_row)
    rows.append(intersection_row)

    dialog.magnifier_auto_color_checkbox = CheckBox(
        INTERACTIVE.text(dialog, "settings.magnifier_auto_color_new_instances")
    )
    apply_settings_body(dialog.magnifier_auto_color_checkbox)
    dialog.magnifier_auto_color_checkbox.setChecked(
        p.magnifier_auto_color_new_instances
    )
    INTERACTIVE.tag_member(
        dialog.magnifier_auto_color_checkbox,
        "settings.magnifier_auto_color_new_instances",
    )
    auto_color_row = as_nav_row(dialog.magnifier_auto_color_checkbox)
    dialog.interactive_opt_group.add_widget(auto_color_row)
    rows.append(auto_color_row)

    rows += _build_magnifier_stroke_rows(dialog, p)
    _populate_interpolation_combos(dialog, p)
    layout.addWidget(dialog.interactive_opt_group)
    return rows


#: Section the tab's perf extras live on (see use_cases/registration.py). The
#: host dialog carries tab values through ``tab_extras`` — the sandbox contract
#: forbids the host layer from naming a tab concept, so these two live here.
STROKE_SECTION = "image_compare.analysis"


def read_magnifier_stroke_payload(dialog) -> dict:
    """Collect the two stroke rows for ``tab_extras`` (dialog -> data)."""
    values: dict = {}
    for attr, key in (
        ("spin_magnifier_border_thickness", "magnifier_border_thickness"),
        ("spin_magnifier_capture_thickness", "magnifier_capture_thickness"),
    ):
        widget = getattr(dialog, attr, None)
        if widget is None:
            continue
        try:
            values[key] = int(widget.value())
        except Exception:
            continue
    return values


def seed_magnifier_stroke_payload(source) -> dict:
    """Seed the two rows from the live feature state (store -> dialog).

    Through the capability alias, never a canvas-feature import: the settings
    UI is shared code (``test_canvas_features_imports``).
    """
    try:
        from ui.canvas_infra.scene.registry import get_canvas_registry

        command = get_canvas_registry("image_compare").get_feature_command_by_alias(
            "overlay.settings.get_stroke"
        )
        if command is None:
            return {}
        values = command(source) or {}
        return {
            "magnifier_border_thickness": int(
                values.get("border_thickness", 2)
            ),
            "magnifier_capture_thickness": int(
                values.get("capture_thickness", 2)
            ),
        }
    except Exception:
        return {}


def _build_magnifier_stroke_rows(dialog, p):
    """Loupe outline + detection-area thickness (owner request 2026-11-04).

    Two spin rows, because they are two different strokes: the loupe's own
    border is a per-instance model field, the detection ring is the loupe's
    sampling area drawn by the capture pass. Both are view pixels, so the
    range is the useful band of a 1x-4x canvas, not a style token.
    """
    rows = []
    seeded = seed_magnifier_stroke_payload(getattr(p, "store", None)) or {}
    for attr, key, label_key, value in (
        (
            "spin_magnifier_border_thickness",
            "settings.magnifier_border_thickness",
            "settings.magnifier_border_thickness",
            int(seeded.get("magnifier_border_thickness", 2)),
        ),
        (
            "spin_magnifier_capture_thickness",
            "settings.magnifier_capture_thickness",
            "settings.magnifier_capture_thickness",
            int(seeded.get("magnifier_capture_thickness", 2)),
        ),
    ):
        label = settings_body_label(INTERACTIVE.text(dialog, label_key))
        spin = SpinBox(default_value=2)
        apply_settings_body(spin)
        spin.setRange(0, 12)
        spin.setValue(max(0, min(12, value)))
        spin.setFixedWidth(scaled_px(100))
        spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        setattr(dialog, attr, spin)
        INTERACTIVE.tag_member(spin, key)
        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(label)
        row_layout.addWidget(spin)
        row_layout.addStretch()
        row = as_nav_row(row_layout)
        dialog.interactive_opt_group.add_widget(row)
        rows.append(row)
    return rows


def _populate_interpolation_combos(dialog, p):
    interp_map = {
        "NEAREST": "magnifier.nearest_neighbor",
        "BILINEAR": "magnifier.bilinear",
        "BICUBIC": "magnifier.bicubic",
        "LANCZOS": "magnifier.lanczos",
        "EWA_LANCZOS": "magnifier.ewa_lanczos",
    }
    for key in AppConstants.INTERPOLATION_METHODS_MAP.keys():
        text = dialog._tr(interp_map.get(key, key), dialog.current_language)
        dialog.combo_mag_interp.addItem(text, key)
        dialog.combo_laser_interp.addItem(text, key)

    store_obj = p.store
    if store_obj and hasattr(store_obj, "viewport"):
        mag_method = getattr(
            store_obj.viewport.render_config,
            "magnifier_movement_interpolation_method",
            None,
        )
        laser_method = getattr(
            store_obj.viewport.render_config,
            "laser_smoothing_interpolation_method",
            None,
        )
    else:
        mag_method = p.movement_interpolation_method
        laser_method = "BILINEAR"
    for combo, value in (
        (dialog.combo_mag_interp, mag_method),
        (dialog.combo_laser_interp, laser_method),
    ):
        idx = combo.findData(value)
        if idx != -1:
            combo.setCurrentIndex(idx)


def _build_video_group(dialog, layout, p):
    dialog.video_group = settings_group(dialog, VIDEO)
    video_layout = QHBoxLayout()
    video_layout.setContentsMargins(0, scaled_px(5), 0, scaled_px(5))
    # Bare "FPS" — universal abbreviation, no translation needed (and the
    # spin keeps the Find Action member key for discoverability).
    dialog.lbl_fps = settings_body_label("FPS:")
    dialog.spin_fps = SpinBox(default_value=FpsPolicy.DEFAULT)
    apply_settings_body(dialog.spin_fps)
    VIDEO.tag_member(dialog.spin_fps, "image_compare.settings.recording_fps")
    dialog.spin_fps.setRange(FpsPolicy.MIN, FpsPolicy.MAX)
    dialog.spin_fps.setValue(clamp_fps(p.current_video_fps))
    dialog.spin_fps.setFixedWidth(scaled_px(100))
    dialog.spin_fps.setAlignment(Qt.AlignmentFlag.AlignCenter)
    video_layout.addWidget(dialog.lbl_fps)
    video_layout.addWidget(dialog.spin_fps)
    video_layout.addStretch()
    video_row = as_nav_row(video_layout)
    dialog.video_group.add_widget(video_row)
    layout.addWidget(dialog.video_group)
    return [video_row]