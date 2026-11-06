# Audit-Meta: pattern=qdialog-wiring reason="video export dialog sections — one dialog assembly"
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtGui import QFont, QIntValidator
from PySide6.QtWidgets import (
    QHBoxLayout,
    QFrame,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from tabs.image_compare.plugins.video_editor.dialog.nav import claim_tab_bar_keys
from tabs.image_compare.plugins.video_editor.dialog.surface_widgets import (
    ThemedExportProgressBar,
    VideoEditorToolbar,
)
from tabs.image_compare.plugins.video_editor.dialog.tab_cursor import (
    VideoEditorTopTabHost,
)
from tabs.image_compare.plugins.video_editor.layout_geometry import (
    MIN_VIDEO_EDITOR_TIMELINE_HEIGHT_PX,
    VIDEO_EDITOR_SETTINGS_STATIC_MARGINS_PX,
    VIDEO_EDITOR_SETTINGS_STATIC_SPACING_PX,
    VIDEO_EDITOR_TOOLBAR_HEIGHT_PX,
)
from tabs.image_compare.plugins.video_editor.services.export_config import ExportConfigBuilder
from tabs.image_compare.plugins.video_editor.widgets.timeline import VideoTimelineWidget
from sli_ui_toolkit.managers import scaled_px
from sli_ui_toolkit.ui.managers.ui_font import ui_font
from sli_ui_toolkit.widgets import (
    Button,
    CustomLineEdit,
    ComboBox,
    LogConsoleWidget,
    SpinBox,
    SurfaceScrollArea,
    Text,
)
from tabs.image_compare.icons import Icon, get_icon
from tabs.image_compare.plugins.video_editor.search import (
    EXPORT_FOOTER,
    EXPORT_TABS,
    PREVIEW_QUALITY,
    RESOLUTION,
    TOOLBAR,
)
from ui.widgets.form_controls import OutputPathSection
from ui.widgets.themed_surface import ThemedBackgroundContainer
from shared.video_policy import CrfPolicy

class _NoWheelEventFilter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel:
            delta = event.angleDelta().y()
            parent = obj.parentWidget() if hasattr(obj, "parentWidget") else None
            while parent is not None:
                if isinstance(parent, QScrollArea):
                    scrollbar = parent.verticalScrollBar()
                    step = scrollbar.singleStep() or 24
                    old_value = scrollbar.value()
                    if delta == 0:
                        event.accept()
                        return True
                    direction = -1 if delta > 0 else 1
                    new_value = old_value + direction * step
                    scrollbar.setValue(new_value)
                    event.accept()
                    return True
                parent = parent.parentWidget()
            return True
        return super().eventFilter(obj, event)

def _install_no_wheel_filter_recursive(root: QWidget, event_filter: QObject) -> None:
    if event_filter is None:
        return
    for child in root.findChildren(QWidget):
        if isinstance(child, (CustomLineEdit, SpinBox, ComboBox)):
            child.installEventFilter(event_filter)


def _wrap_row_container(item: QWidget | QLayout, attr: str) -> QWidget:
    """Wrap *item* (a bare control or an unparented layout) in a zero-margin
    container widget — the shape a nav row must have (``ToolbarRowsSection``
    rows are QWidgets: it tests ``row.isVisible()``/``row.isAncestorOf()`` and
    searches ``row.findChildren()``). The container adopts the layout as-is,
    so the visual result is identical to adding the layout directly.
    """
    if isinstance(item, QWidget):
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.addWidget(item)
    else:
        container = QWidget()
        container.setLayout(item)
    container.setObjectName(attr)
    return container


def _wrap_in_place(item: QWidget, attr: str) -> QWidget:
    """Wrap a control that already sits in its parent's layout in a row
    container, taking over *item*'s slot in that layout.

    Distinct from :func:`_wrap_row_container`, which requires an unparented
    item: reparenting *item* into a fresh container removes it from the old
    layout, so the container is re-inserted at the vacated index — same
    position, same margins, one nav row instead of a bare control.
    """
    parent = item.parentWidget()
    parent_layout = parent.layout() if parent is not None else None
    index = parent_layout.indexOf(item) if parent_layout is not None else -1
    container = QWidget(parent)
    container.setObjectName(attr)
    container_layout = QVBoxLayout(container)
    container_layout.setContentsMargins(0, 0, 0, 0)
    container_layout.addWidget(item)
    if parent_layout is not None and index >= 0:
        parent_layout.insertWidget(index, container)
    return container

def build_settings_panel(dialog):
    settings_panel = QFrame()
    settings_panel.setObjectName("VideoEditorSettingsPanel")

    sp_layout = QVBoxLayout(settings_panel)
    sp_layout.setContentsMargins(0, 0, 0, 0)
    sp_layout.setSpacing(0)

    dialog._settings_no_wheel_filter = _NoWheelEventFilter(settings_panel)

    static_container = ThemedBackgroundContainer(settings_panel)
    static_layout = QVBoxLayout(static_container)
    _static_left, _static_top, _static_right, _static_bottom = (
        VIDEO_EDITOR_SETTINGS_STATIC_MARGINS_PX
    )
    static_layout.setContentsMargins(
        scaled_px(_static_left),
        scaled_px(_static_top),
        scaled_px(_static_right),
        scaled_px(_static_bottom),
    )
    static_layout.setSpacing(scaled_px(VIDEO_EDITOR_SETTINGS_STATIC_SPACING_PX))
    # Owned layout, not just the container: the fit-to-screen pass switches
    # this block to the compact density on short screens (layout_geometry).
    dialog.settings_static_layout = static_layout
    # Each form row is wrapped in a plain container so the keyboard-nav
    # section can treat it as one row (nav rows must be QWidgets — a bare
    # QHBoxLayout has no isVisible()/isAncestorOf identity). Zero-margin
    # container adopts the layout: visually identical to addLayout().
    dialog.res_row = _wrap_row_container(create_resolution_settings(dialog), "res_row")
    static_layout.addWidget(dialog.res_row)
    dialog.fps_row = _wrap_row_container(create_fps_settings(dialog), "fps_row")
    static_layout.addWidget(dialog.fps_row)
    dialog.preview_row = _wrap_row_container(
        create_preview_quality_settings(dialog), "preview_row"
    )
    static_layout.addWidget(dialog.preview_row)
    sp_layout.addWidget(static_container)
    dialog.settings_static_container = static_container

    content_container = ThemedBackgroundContainer()
    content_layout = QVBoxLayout(content_container)
    content_layout.setContentsMargins(scaled_px(12), scaled_px(8), scaled_px(12), 0)
    content_layout.setSpacing(0)

    dialog.tabs = create_export_tabs(dialog)
    content_layout.addWidget(dialog.tabs, stretch=1)
    sp_layout.addWidget(content_container, stretch=1)

    footer_gap_top = ThemedBackgroundContainer(settings_panel)
    footer_gap_top.setFixedHeight(scaled_px(10))
    sp_layout.addWidget(footer_gap_top)

    footer_gap_bottom = ThemedBackgroundContainer(settings_panel)
    footer_gap_bottom.setFixedHeight(scaled_px(12))
    sp_layout.addWidget(footer_gap_bottom)

    dialog.export_progress = ThemedExportProgressBar()
    dialog.export_progress.setObjectName("VideoEditorExportProgress")
    dialog.export_progress.setProperty("state", "active")
    dialog.export_progress.setVisible(False)
    dialog.export_progress.setTextVisible(False)
    dialog.export_progress.setFixedHeight(scaled_px(4))
    sp_layout.addWidget(dialog.export_progress)

    dialog.btn_export = Button(
        get_icon(Icon.EXPORT_VIDEO), text=dialog._tr("image_compare.action.export_video"),
        variant="surface", size=(0, 48), corner_radius=8,
    )
    dialog.btn_export.set_footer_mode(True)
    dialog.btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
    dialog.btn_export.clicked.connect(dialog._on_export_clicked)

    dialog.btn_stop_export = Button(
        get_icon(Icon.STOP),
        variant="ghost",
        size=(24, 24),
        icon_size=12,
        parent=dialog.btn_export,
    )
    dialog.btn_stop_export.setObjectName("btnStopVideoExport")
    dialog.btn_stop_export.setToolTip(dialog._tr("image_compare.button.stop"))
    dialog.btn_stop_export.setCursor(Qt.CursorShape.PointingHandCursor)
    dialog.btn_stop_export.clicked.connect(dialog._on_stop_export_clicked)
    dialog.btn_stop_export.hide()
    # Same row-container wrap as the settings form rows: one nav row.
    dialog.export_row = _wrap_row_container(dialog.btn_export, "export_row")
    sp_layout.addWidget(dialog.export_row)

    EXPORT_FOOTER.tag_member(dialog.btn_export, "image_compare.action.export_video")
    EXPORT_FOOTER.tag_member(dialog.btn_stop_export, "image_compare.button.stop")

    return settings_panel

def create_resolution_settings(dialog):
    res_layout = QHBoxLayout()
    res_layout.setSpacing(scaled_px(8))

    dialog.lbl_resolution = Text(dialog._tr("label.resolution") + ":", mode="plain", variant="group-title")
    res_layout.addWidget(dialog.lbl_resolution)

    dialog.edit_width = CustomLineEdit()
    dialog.edit_width.setValidator(QIntValidator(16, 8192))
    dialog.edit_width.setText("1920")
    dialog.edit_width.setFixedWidth(scaled_px(60))
    dialog.edit_width.setAlignment(Qt.AlignmentFlag.AlignCenter)
    dialog.edit_width.installEventFilter(dialog._settings_no_wheel_filter)
    res_layout.addWidget(dialog.edit_width)

    dialog.btn_lock_ratio = Button(icon=(Icon.UNLINK, Icon.LINK), toggle=True, size=(32, 32))
    dialog.btn_lock_ratio.setObjectName("btnLockRatio")
    dialog.btn_lock_ratio.setChecked(True)
    dialog.btn_lock_ratio.setToolTip(dialog._tr("video.lock_aspect_ratio"))
    dialog.btn_lock_ratio.toggled.connect(dialog._on_ratio_lock_toggled)
    RESOLUTION.tag_member(dialog.btn_lock_ratio, "video.lock_aspect_ratio")
    res_layout.addWidget(dialog.btn_lock_ratio)

    dialog.edit_height = CustomLineEdit()
    dialog.edit_height.setValidator(QIntValidator(16, 8192))
    dialog.edit_height.setText("1080")
    dialog.edit_height.setFixedWidth(scaled_px(60))
    dialog.edit_height.setAlignment(Qt.AlignmentFlag.AlignCenter)
    dialog.edit_height.installEventFilter(dialog._settings_no_wheel_filter)
    res_layout.addWidget(dialog.edit_height)

    dialog.btn_fit_content = Button(icon=(Icon.CROP_IN, Icon.CROP_OUT), toggle=True, size=(32, 32))
    dialog.btn_fit_content.setObjectName("btnFitContent")
    dialog.btn_fit_content.setChecked(False)
    dialog.btn_fit_content.setToolTip(dialog._tr("image_compare.magnifier.fit_mode_toggle"))
    dialog.btn_fit_content.toggled.connect(dialog._on_fit_content_toggled)
    RESOLUTION.tag_member(dialog.btn_fit_content, "image_compare.magnifier.fit_mode_toggle")
    res_layout.addWidget(dialog.btn_fit_content)

    dialog.btn_fit_fill_color = Button(Icon.DIVIDER_COLOR, show_underline=True, size=(32, 32))
    dialog.btn_fit_fill_color.setObjectName("btnFitFillColor")
    dialog.btn_fit_fill_color.setToolTip(dialog._tr("export.select_background_color"))
    dialog.btn_fit_fill_color.clicked.connect(dialog._on_fit_fill_color_clicked)
    dialog.btn_fit_fill_color.setVisible(False)
    RESOLUTION.tag_member(dialog.btn_fit_fill_color, "export.select_background_color")
    if hasattr(dialog, "_update_fit_fill_color_button"):
        dialog._update_fit_fill_color_button()
    res_layout.addWidget(dialog.btn_fit_fill_color)

    res_layout.addStretch()

    dialog.edit_width.editingFinished.connect(dialog._on_width_edited)
    dialog.edit_height.editingFinished.connect(dialog._on_height_edited)

    return res_layout

def create_fps_settings(dialog):
    fps_layout = QHBoxLayout()
    dialog.lbl_fps = Text(dialog._tr("image_compare.label.fps") + ":", mode="plain", variant="group-title")
    fps_layout.addWidget(dialog.lbl_fps)

    # Fps single source (A-shell S1): the initial value comes from
    # resolve_initial_fps — the same function that seeds VideoEditorService
    # at presenter construction — never a second snapshot/store derivation.
    # The spinbox max caps at the recording fps when known (same cap
    # shell.py applies before first show).
    from tabs.image_compare.plugins.video_editor.presenter_parts.common import (
        resolve_initial_fps,
    )

    export_controller = getattr(dialog, "export_controller", None)
    main_controller = None
    try:
        main_controller = getattr(
            getattr(export_controller, "presenter", None), "main_controller", None
        )
    except Exception:
        main_controller = None
    initial_fps = resolve_initial_fps(
        dialog, getattr(dialog, "snapshots", None), main_controller
    )

    max_fps = initial_fps or 240
    first_snapshot = (
        dialog._get_first_snapshot() if hasattr(dialog, "_get_first_snapshot") else None
    )
    if first_snapshot and hasattr(first_snapshot, "settings_state"):
        recording_fps = getattr(
            first_snapshot.settings_state, "video_recording_fps", None
        )
        if recording_fps:
            max_fps = recording_fps
            initial_fps = min(initial_fps, max_fps) if initial_fps else recording_fps

    dialog.edit_fps = SpinBox(default_value=initial_fps)
    dialog.edit_fps.setRange(1, max_fps)
    dialog.edit_fps.setValue(initial_fps)
    dialog.edit_fps.setFixedWidth(scaled_px(100))
    dialog.edit_fps.setAlignment(Qt.AlignmentFlag.AlignCenter)
    dialog.edit_fps.installEventFilter(dialog._settings_no_wheel_filter)
    fps_layout.addWidget(dialog.edit_fps)

    fps_layout.addStretch()

    dialog.edit_fps.valueChanged.connect(dialog._on_fps_changed)

    return fps_layout

def create_preview_quality_settings(dialog):
    preview_layout = QHBoxLayout()
    preview_layout.setSpacing(scaled_px(8))
    dialog.lbl_preview_quality = Text(dialog._tr("video.preview_quality") + ":", mode="plain", variant="group-title")
    preview_layout.addWidget(dialog.lbl_preview_quality)

    dialog.combo_preview_scale = ComboBox()
    PREVIEW_QUALITY.tag_combo(dialog.combo_preview_scale)
    for key, value in (
        ("video.preview_quality_full", 1.0),
        ("video.preview_quality_balanced", 0.75),
        ("video.preview_quality_performance", 0.5),
        ("video.preview_quality_draft", 0.25),
    ):
        dialog.combo_preview_scale.addItem(dialog._tr(key), value)
        PREVIEW_QUALITY.note_combo_option(dialog.combo_preview_scale, key)
    dialog.combo_preview_scale.setCurrentIndex(0)
    dialog.combo_preview_scale.installEventFilter(dialog._settings_no_wheel_filter)
    dialog.combo_preview_scale.currentIndexChanged.connect(
        lambda _index: dialog._on_preview_scale_changed()
    )
    preview_layout.addWidget(dialog.combo_preview_scale)
    preview_layout.addStretch()
    return preview_layout

def create_export_tabs(dialog):
    tabs = VideoEditorTopTabHost()
    tabs.setObjectName("VideoEditorTabs")

    dialog.tab_standard = create_standard_export_tab(dialog)
    tabs.addTab(dialog.tab_standard, dialog._tr("video.standard"))
    EXPORT_TABS.tag_tab_page(tabs, dialog.tab_standard, "video.standard")

    dialog.tab_manual = create_manual_export_tab(dialog)
    tabs.addTab(dialog.tab_manual, dialog._tr("video.manual_cli"))
    EXPORT_TABS.tag_tab_page(tabs, dialog.tab_manual, "video.manual_cli")

    dialog.tab_output = create_output_tab(dialog)
    tabs.addTab(dialog.tab_output, dialog._tr("image_compare.label.output"))
    EXPORT_TABS.tag_tab_page(tabs, dialog.tab_output, "image_compare.label.output")

    dialog.tab_log = create_log_tab(dialog)
    tabs.addTab(dialog.tab_log, dialog._tr("video.export_log"))
    EXPORT_TABS.tag_tab_page(tabs, dialog.tab_log, "video.export_log")

    # Keyboard entry to the page stack. The toolkit tab bar's tab buttons are
    # all NoFocus and the bar has no ``keyPressEvent`` of its own, so without
    # this the bar is unreachable and the export pages cannot be switched from
    # the keyboard. The app gives the bar StrongFocus and the dialog's nav
    # section (dialog/nav.py) handles ←/→ and Enter/Space while the bar is the
    # focused widget. The app's ``VideoEditorTopTabHost``/``VideoEditorTabBar``
    # (dialog/tab_cursor.py) add the workspace-strip cursor idiom: ←/→ move a
    # keyboard cursor (the page does not change), Enter/Space activate it, and
    # the cursor tab carries the focus ring. The bar also claims its activation
    # keys against the dialog-level Space shortcut (see ``claim_tab_bar_keys``).
    tabs.tab_bar.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    claim_tab_bar_keys(tabs.tab_bar)
    # Per-tab nav rows, in tab order: the dialog nav section exposes ONLY the
    # active tab's rows (each tab's content is a vertical form — see the
    # per-tab ``create_*_tab`` builders).
    dialog.export_tab_rows = [
        dialog.standard_tab_rows,
        dialog.manual_tab_rows,
        dialog.output_tab_rows,
        dialog.log_tab_rows,
    ]

    dialog.combo_container.currentTextChanged.connect(dialog._on_container_changed)
    dialog.combo_codec.currentTextChanged.connect(dialog._on_codec_changed)

    return tabs

def create_log_tab(dialog) -> QWidget:
    tab = QWidget()
    tab.setObjectName("VideoEditorTabContent")
    layout = QVBoxLayout(tab)
    layout.setContentsMargins(scaled_px(8), scaled_px(8), scaled_px(8), scaled_px(8))
    layout.setSpacing(0)

    log_edit = LogConsoleWidget()
    log_edit.setObjectName("VideoExportLog")
    mono_font = ui_font(point_size=9)
    mono_font.setFamily("Monospace")
    mono_font.setStyleHint(QFont.StyleHint.Monospace)
    log_edit.set_output_font(mono_font)

    dialog.export_log_edit = log_edit
    layout.addWidget(log_edit)
    # One nav row: the console's own ConsoleView is the StrongFocus child the
    # ring lands on (the LogConsoleWidget itself is NoFocus by dialog policy).
    dialog.log_tab_rows = [log_edit]
    return tab

def _wrap_tab_scroll(content: QWidget) -> QWidget:
    tab = QWidget()
    tab.setObjectName("VideoEditorTabContent")
    tab.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    tab.setAutoFillBackground(False)
    tab.setSizePolicy(
        QSizePolicy.Policy.Expanding,
        QSizePolicy.Policy.Expanding,
    )
    layout = QVBoxLayout(tab)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    content.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    content.setAutoFillBackground(False)
    # Prefer Expanding so equal stretch pads share leftover viewport height.
    # Callers may already set Expanding; Minimum would collapse pads to zero.
    if content.sizePolicy().verticalPolicy() != QSizePolicy.Policy.Expanding:
        content.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
    content.setMinimumWidth(0)
    scroll_area = SurfaceScrollArea(tab)
    # Host pane already paints rounded chrome; a viewport mask here clips
    # content and can bleed neighbouring framebuffer pixels.
    scroll_area.set_corner_radius(0)
    scroll_area.set_reserve_scrollbar_space(False)
    scroll_area.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    scroll_area.setAutoFillBackground(False)
    scroll_area.setWidget(content)
    scroll_area.setSizePolicy(
        QSizePolicy.Policy.Expanding,
        QSizePolicy.Policy.Expanding,
    )
    layout.addWidget(scroll_area)
    return tab


def _expand_form_control(widget: QWidget) -> None:
    """Form fields must fill the pane width (ComboBox sizeHint is content-sized)."""
    widget.setSizePolicy(
        QSizePolicy.Policy.Expanding,
        QSizePolicy.Policy.Fixed,
    )
    widget.setMinimumWidth(0)
    widget.setMaximumWidth(16777215)


def _lock_form_block_height(widget: QWidget) -> None:
    """Section hosts must not absorb vertical stretch (DIALOGS.md stretch pads)."""
    widget.setSizePolicy(
        QSizePolicy.Policy.Expanding,
        QSizePolicy.Policy.Maximum,
    )


def _add_labeled_field_column(
    parent_layout: QVBoxLayout,
    label: QWidget,
    field: QWidget,
    *,
    attr: str,
    field_spacing: int = 6,
) -> QWidget:
    """Tight label→field pair; stretch pads live *between* these blocks.

    Returns the block so it can be exposed as one keyboard-nav row (see
    ``create_standard_export_tab``); ``attr`` names it for the nav tests.
    """
    block = QWidget()
    block.setObjectName(attr)
    _lock_form_block_height(block)
    col = QVBoxLayout(block)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(scaled_px(field_spacing))
    col.addWidget(label)
    col.addWidget(field)
    parent_layout.addWidget(block)
    return block


def _sync_quality_stack_page(dialog) -> None:
    """Map quality mode data → stack page (crf/cq share the value page)."""
    mode = dialog.combo_quality_mode.currentData()
    dialog.stack_quality.setCurrentIndex(1 if mode == "bitrate" else 0)


def create_standard_export_tab(dialog):
    content = QWidget()
    # Expand with the scroll viewport so equal addStretch(1) pads can share
    # leftover height (same recipe as export dialog / docs/dev/DIALOGS.md).
    content.setSizePolicy(
        QSizePolicy.Policy.Expanding,
        QSizePolicy.Policy.Expanding,
    )
    layout = QVBoxLayout(content)
    layout.setContentsMargins(scaled_px(12), scaled_px(16), scaled_px(12), scaled_px(16))
    # Base gap between stacked rows; extra height goes to addStretch slots.
    layout.setSpacing(scaled_px(8))
    # The fields are one vertical form: each is exposed as its own keyboard-nav
    # row (in this build order = visual top→bottom), so ↑/↓ step field by field
    # instead of treating the whole tab as a single row.
    rows: list[QWidget] = []

    dialog.lbl_container = Text(dialog._tr("image_compare.label.container") + ":", mode="plain", variant="group-title")
    dialog.combo_container = ComboBox()
    for container in ExportConfigBuilder.get_available_containers():
        dialog.combo_container.addItem(dialog._tr(container), container)
    dialog.combo_container.setCurrentIndex(max(0, dialog.combo_container.findData("mp4")))
    _expand_form_control(dialog.combo_container)
    rows.append(
        _add_labeled_field_column(
            layout,
            dialog.lbl_container,
            dialog.combo_container,
            attr="row_export_container",
        )
    )

    layout.addStretch(1)

    dialog.lbl_video_codec = Text(dialog._tr("image_compare.label.video_codec") + ":", mode="plain", variant="group-title")
    dialog.combo_codec = ComboBox()
    for codec in ExportConfigBuilder.get_codecs_for_container("mp4"):
        dialog.combo_codec.addItem(
            dialog._tr(ExportConfigBuilder.get_codec_display_key(codec)), codec
        )
    _expand_form_control(dialog.combo_codec)
    rows.append(
        _add_labeled_field_column(
            layout,
            dialog.lbl_video_codec,
            dialog.combo_codec,
            attr="row_export_codec",
        )
    )

    layout.addStretch(1)

    dialog.pix_fmt_container = QWidget()
    dialog.pix_fmt_container.setObjectName("row_export_pixel_format")
    _lock_form_block_height(dialog.pix_fmt_container)
    pf_layout = QVBoxLayout(dialog.pix_fmt_container)
    pf_layout.setContentsMargins(0, 0, 0, 0)
    pf_layout.setSpacing(scaled_px(6))
    dialog.lbl_pix_fmt = Text(dialog._tr("video.pixel_format") + ":", mode="plain", variant="group-title")
    pf_layout.addWidget(dialog.lbl_pix_fmt)
    dialog.combo_pix_fmt = ComboBox()
    for pix_fmt in ExportConfigBuilder.get_pixel_formats_for_codec("h264 (AVC)"):
        dialog.combo_pix_fmt.addItem(pix_fmt, pix_fmt)
    dialog.combo_pix_fmt.setCurrentText("yuv420p")
    _expand_form_control(dialog.combo_pix_fmt)
    pf_layout.addWidget(dialog.combo_pix_fmt)
    layout.addWidget(dialog.pix_fmt_container)
    rows.append(dialog.pix_fmt_container)

    layout.addStretch(1)

    # Quality mode and CRF/bitrate value are separate stretch-separated
    # sections (same recipe as Container → Codec), not one glued block.
    dialog.quality_controls_container = QWidget()
    dialog.quality_controls_container.setObjectName("row_export_quality_mode")
    _lock_form_block_height(dialog.quality_controls_container)
    qc_layout = QVBoxLayout(dialog.quality_controls_container)
    qc_layout.setContentsMargins(0, 0, 0, 0)
    qc_layout.setSpacing(scaled_px(6))

    dialog.lbl_quality_control = Text(
        dialog._tr("video.quality_control") + ":",
        mode="plain",
        variant="group-title",
    )
    dialog.combo_quality_mode = ComboBox()
    dialog.combo_quality_mode.addItem(dialog._tr("video.crf_constant_quality"), "crf")
    dialog.combo_quality_mode.addItem(dialog._tr("video.bitrate_cbrvbr"), "bitrate")
    _expand_form_control(dialog.combo_quality_mode)
    qc_layout.addWidget(dialog.lbl_quality_control)
    qc_layout.addWidget(dialog.combo_quality_mode)
    layout.addWidget(dialog.quality_controls_container)
    rows.append(dialog.quality_controls_container)

    layout.addStretch(1)

    dialog.stack_quality = create_quality_stack(dialog)
    dialog.stack_quality.setObjectName("row_export_quality_value")
    layout.addWidget(dialog.stack_quality)
    rows.append(dialog.stack_quality)

    layout.addStretch(1)

    dialog.preset_container = QWidget()
    dialog.preset_container.setObjectName("row_export_preset")
    _lock_form_block_height(dialog.preset_container)
    p_layout = QVBoxLayout(dialog.preset_container)
    p_layout.setContentsMargins(0, 0, 0, 0)
    p_layout.setSpacing(scaled_px(6))

    dialog.lbl_preset = Text(
        dialog._tr("video.encoding_speed_preset") + ":",
        mode="plain",
        variant="group-title",
    )
    p_layout.addWidget(dialog.lbl_preset)
    dialog.combo_preset = ComboBox()
    for preset in ExportConfigBuilder.get_encoding_presets():
        dialog.combo_preset.addItem(dialog._tr_preset(preset), preset)
    dialog.combo_preset.setCurrentIndex(max(0, dialog.combo_preset.findData("medium")))
    _expand_form_control(dialog.combo_preset)
    p_layout.addWidget(dialog.combo_preset)

    layout.addWidget(dialog.preset_container)
    rows.append(dialog.preset_container)

    dialog.combo_quality_mode.currentIndexChanged.connect(
        lambda _index: _sync_quality_stack_page(dialog)
    )
    _sync_quality_stack_page(dialog)

    layout.addStretch(1)
    _install_no_wheel_filter_recursive(content, dialog._settings_no_wheel_filter)
    dialog.standard_tab_rows = rows
    return _wrap_tab_scroll(content)

def create_manual_export_tab(dialog):
    content = QWidget()
    content.setSizePolicy(
        QSizePolicy.Policy.Expanding,
        QSizePolicy.Policy.Expanding,
    )
    layout = QVBoxLayout(content)
    layout.setContentsMargins(scaled_px(12), scaled_px(16), scaled_px(12), scaled_px(16))
    layout.setSpacing(scaled_px(8))

    dialog.lbl_manual_args_hint = Text(
        dialog._tr("video.ffmpeg_output_args_hint"),
        mode="plain",
        variant="group-title",
    )
    dialog.lbl_manual_args_hint.setWordWrap(True)
    layout.addWidget(dialog.lbl_manual_args_hint)

    dialog.edit_manual_args = CustomLineEdit()
    dialog.edit_manual_args.setPlaceholderText("-c:v libx264 -crf 23 -pix_fmt yuv420p")
    _expand_form_control(dialog.edit_manual_args)
    layout.addWidget(dialog.edit_manual_args)

    layout.addStretch(1)
    _install_no_wheel_filter_recursive(content, dialog._settings_no_wheel_filter)
    # The args field is the tab's one focusable control — one nav row. The
    # hint label above it is not a row (a row must own a StrongFocus child).
    dialog.manual_tab_rows = [
        _wrap_in_place(dialog.edit_manual_args, "row_export_manual_args")
    ]
    return _wrap_tab_scroll(content)

def _harden_text_buttons(*buttons) -> None:
    """Keep text buttons sized to their content (same recipe as export dialog).

    Do not use ``setFixedHeight`` here — toolkit text buttons hint at
    ``fontMetrics.height() + 16`` (often > 32), and a tighter fixed height
    clips the bottom edge / corner radius and never re-applies on
    ``UiScale.scale_changed`` (external min/fixed sizes are not part of
    ``Button``'s design-px bookkeeping).
    """
    for button in buttons:
        if button is None:
            continue
        # Drop any leftover fixed-height clamp from construction.
        button.setMinimumHeight(0)
        button.setMaximumHeight(16777215)
        hint = button.sizeHint()
        button.setMinimumSize(
            max(button.minimumWidth(), hint.width()),
            max(scaled_px(32), hint.height()),
        )
        button.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )


def create_output_tab(dialog):
    content = QWidget()
    content.setSizePolicy(
        QSizePolicy.Policy.Expanding,
        QSizePolicy.Policy.Expanding,
    )
    layout = QVBoxLayout(content)
    layout.setContentsMargins(scaled_px(16), scaled_px(16), scaled_px(16), scaled_px(16))
    layout.setSpacing(scaled_px(8))

    section = OutputPathSection(
        directory_label_text=dialog._tr("export.select_output_directory") + ":",
        browse_text=dialog._tr("button.browse"),
        set_favorite_text=dialog._tr("misc.set_as_favorite"),
        use_favorite_text=dialog._tr("tooltip.use_favorite"),
        filename_label_text=dialog._tr("label.file_name") + ":",
        on_browse=dialog._browse_output_dir,
        on_set_favorite=dialog._on_set_favorite_clicked,
        on_use_favorite=dialog._on_use_favorite_clicked,
        use_custom_line_edit=True,
        filename_editor_factory=CustomLineEdit,
    )
    _harden_text_buttons(
        section.btn_browse_dir,
        section.btn_set_favorite,
        section.btn_use_favorite,
    )
    section.apply_to(dialog)
    # Video-editor-specific aliases (different names from the standardized ones).
    dialog.edit_output_dir = dialog.edit_dir
    dialog.btn_browse_output = dialog.btn_browse_dir
    dialog.edit_filename = dialog.edit_name
    EXPORT_FOOTER.tag_member(dialog.btn_browse_output, "button.browse")
    EXPORT_FOOTER.tag_member(dialog.btn_set_favorite, "misc.set_as_favorite")
    EXPORT_FOOTER.tag_member(dialog.btn_use_favorite, "tooltip.use_favorite")
    layout.addWidget(dialog.output_section)

    # The output form's three focusable rows (folder+Browse is one horizontal
    # row; favorites the next; the filename field is wrapped in place so it is
    # a row too). Labels stay outside the rows — a row must own a StrongFocus
    # child or the row walk stalls on it.
    section.dir_picker_row.setObjectName("row_export_output_dir")
    section.favorite_actions.setObjectName("row_export_output_favorites")
    dialog.output_tab_rows = [
        section.dir_picker_row,
        section.favorite_actions,
        _wrap_in_place(dialog.edit_filename, "row_export_output_filename"),
    ]

    layout.addStretch(1)
    _install_no_wheel_filter_recursive(content, dialog._settings_no_wheel_filter)
    return _wrap_tab_scroll(content)

def create_quality_stack(dialog):
    stack = QStackedWidget()
    _lock_form_block_height(stack)

    p_crf = QWidget()
    _lock_form_block_height(p_crf)
    l_crf = QVBoxLayout(p_crf)
    l_crf.setContentsMargins(0, 0, 0, 0)
    l_crf.setSpacing(scaled_px(6))
    dialog.lbl_quality_value = Text(
        dialog._tr("video.crf_value_hint") + ":",
        mode="plain",
        variant="group-title",
    )
    dialog.edit_crf = CustomLineEdit()
    dialog.edit_crf.setValidator(QIntValidator(CrfPolicy.MIN, CrfPolicy.MAX))
    dialog.edit_crf.setText(str(CrfPolicy.DEFAULT))
    dialog.edit_crf.setPlaceholderText(str(CrfPolicy.DEFAULT))
    dialog.edit_crf.setFixedWidth(scaled_px(80))
    dialog.edit_crf.setAlignment(Qt.AlignmentFlag.AlignCenter)
    l_crf.addWidget(dialog.lbl_quality_value)
    l_crf.addWidget(dialog.edit_crf)

    stack.addWidget(p_crf)

    p_bit = QWidget()
    _lock_form_block_height(p_bit)
    l_bit = QVBoxLayout(p_bit)
    l_bit.setContentsMargins(0, 0, 0, 0)
    l_bit.setSpacing(scaled_px(6))
    dialog.lbl_bitrate = Text(dialog._tr("video.bitrate_hint") + ":", mode="plain", variant="group-title")
    dialog.edit_bitrate = CustomLineEdit()
    dialog.edit_bitrate.setText("8000k")
    _expand_form_control(dialog.edit_bitrate)
    l_bit.addWidget(dialog.lbl_bitrate)
    l_bit.addWidget(dialog.edit_bitrate)

    stack.addWidget(p_bit)

    return stack

def create_toolbar(dialog):
    toolbar_frame = VideoEditorToolbar()
    toolbar_frame.setObjectName("VideoEditorToolbar")
    toolbar_frame.setFixedHeight(scaled_px(VIDEO_EDITOR_TOOLBAR_HEIGHT_PX))

    toolbar_layout = QHBoxLayout(toolbar_frame)
    toolbar_layout.setContentsMargins(scaled_px(10), scaled_px(5), scaled_px(10), scaled_px(5))
    toolbar_layout.setSpacing(scaled_px(8))

    dialog.btn_play = Button(icon=(Icon.PLAY, Icon.PAUSE), toggle=True)
    dialog.btn_play.setToolTip(
        dialog._tr("image_compare.button.play") + " / " + dialog._tr("image_compare.button.pause")
    )
    dialog.btn_play.toggled.connect(dialog._on_play_toggled)
    TOOLBAR.tag_member(dialog.btn_play, "image_compare.button.play")

    dialog.btn_undo = Button(Icon.UNDO)
    dialog.btn_undo.setToolTip(dialog._tr("image_compare.button.undo_ctrlz"))
    dialog.btn_undo.clicked.connect(dialog._on_undo_clicked)
    TOOLBAR.tag_member(dialog.btn_undo, "image_compare.button.undo_ctrlz")

    dialog.btn_redo = Button(Icon.REDO)
    dialog.btn_redo.setToolTip(dialog._tr("image_compare.button.redo"))
    dialog.btn_redo.clicked.connect(dialog._on_redo_clicked)
    TOOLBAR.tag_member(dialog.btn_redo, "image_compare.button.redo")

    dialog.btn_trim = Button(Icon.SCISSORS)
    dialog.btn_trim.setToolTip(dialog._tr("image_compare.button.trim_to_selection"))
    dialog.btn_trim.clicked.connect(dialog._on_trim_clicked)
    TOOLBAR.tag_member(dialog.btn_trim, "image_compare.button.trim_to_selection")

    toolbar_layout.addWidget(dialog.btn_play)
    toolbar_layout.addWidget(dialog.btn_undo)
    toolbar_layout.addWidget(dialog.btn_redo)
    toolbar_layout.addWidget(dialog.btn_trim)

    toolbar_layout.addStretch()
    # One nav row: wrap the toolbar (``VideoEditorToolbar`` keeps its style
    # objectName inside) in the same zero-margin row container the other
    # outer rows use, so the row carries the nav name ``toolbar_frame``.
    return _wrap_row_container(toolbar_frame, "toolbar_frame")

def create_timeline_scroll_area(dialog):
    scroll_area = SurfaceScrollArea()
    scroll_area.setObjectName("VideoEditorTimelineScrollArea")
    scroll_area.setMinimumHeight(scaled_px(MIN_VIDEO_EDITOR_TIMELINE_HEIGHT_PX))
    # One horizontal control, not two (plan-timeline-event-synchrony-2026-11-04,
    # phase P3; owner decision 2026-11-06): the timeline draws its own footer
    # bar for this axis, so the host's overlay thumb is turned off instead of
    # painting over it. The native horizontal bar keeps range and value — it
    # stays the scrolling mechanism, and the vertical overlay thumb is untouched.
    scroll_area.set_horizontal_scrollbar_enabled(False)

    store = None
    if hasattr(dialog, "export_controller") and dialog.export_controller:
        if (
            hasattr(dialog.export_controller, "store")
            and dialog.export_controller.store
        ):
            store = dialog.export_controller.store
    dialog.timeline = VideoTimelineWidget([], store=store)
    dialog.timeline.setSizePolicy(
        QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Preferred
    )
    scroll_area.setWidget(dialog.timeline)

    dialog.timeline.headMoved.connect(dialog._on_head_moved)
    dialog.timeline.deletePressed.connect(dialog._on_trim_clicked)
    dialog.timeline.playPauseRequested.connect(dialog._on_play_pause_requested)

    return scroll_area