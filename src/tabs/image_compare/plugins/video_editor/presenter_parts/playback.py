class PlaybackCoordinator:
    def __init__(
        self,
        view,
        editor_service,
        playback_engine,
        model,
        export_controller,
        preview_coordinator,
        emit_timeline_position,
        emit_playback_state,
        emit_buttons_state,
    ):
        self.view = view
        self.editor_service = editor_service
        self.playback_engine = playback_engine
        self.model = model
        self.export_controller = export_controller
        self.preview_coordinator = preview_coordinator
        self.emit_timeline_position = emit_timeline_position
        self.emit_playback_state = emit_playback_state
        self.emit_buttons_state = emit_buttons_state

    def detach_view(self):
        self.view = None

    def toggle_playback(self):
        if self.playback_engine.is_playing():
            self.pause_playback()
        else:
            self.start_playback()

    def start_playback(self):
        if self.view is None:
            return
        start_frame, end_frame = self.view.get_selection_range()
        self.playback_engine.set_range(start_frame, end_frame)

        current_frame = self.playback_engine.get_current_frame()
        if current_frame >= end_frame or current_frame < start_frame:
            self.playback_engine.set_current_frame(start_frame)
            self.emit_timeline_position(start_frame)

        self.playback_engine.set_fps(self.editor_service.get_fps())
        self.playback_engine.play()

    def pause_playback(self):
        self.playback_engine.pause()

    def seek_to_frame(self, frame_index: int):
        self.playback_engine.seek(frame_index)
        if not self.playback_engine.is_playing():
            self.preview_coordinator.schedule_update()

    def undo(self):
        if self.editor_service.undo():
            self.on_data_changed()

    def redo(self):
        if self.editor_service.redo():
            self.on_data_changed()

    def trim_selection(self):
        if self.view is None:
            return
        if hasattr(self.view, "has_selection") and not self.view.has_selection():
            return
        start_frame, end_frame = self.view.get_selection_range()
        if self.editor_service.delete_selection(start_frame, end_frame):
            self.on_data_changed()

    def on_frame_changed(self, frame_index: int):
        self.emit_timeline_position(frame_index)
        if self.playback_engine.is_playing():
            self.preview_coordinator.do_update_preview_heavy()
        else:
            self.preview_coordinator.schedule_update()

    def on_playback_state_changed(self, is_playing: bool):
        self.emit_playback_state(is_playing)
        if not is_playing and self.view is not None:
            start_frame, end_frame = self.view.get_selection_range()
            current_frame = self.playback_engine.get_current_frame()
            if current_frame >= end_frame:
                self.playback_engine.set_current_frame(start_frame)
                self.emit_timeline_position(start_frame)
                self.preview_coordinator.schedule_update()

    def on_data_changed(self):
        if self.view is not None:
            self.view.set_snapshots(
                self.editor_service.get_current_snapshots(),
                fps=self.editor_service.get_fps(),
                timeline_model=self.editor_service.get_timeline_model(),
            )

        total_frames = self.editor_service.get_frame_count()
        self.playback_engine.set_total_frames(total_frames)
        self.playback_engine.set_current_frame(0)
        self.emit_timeline_position(0)

        self.update_buttons_state()

        # Bounds live in the service: invalidate there (single reset path),
        # then recompute in the background; preview reads via the service.
        exporter = getattr(self.export_controller, "video_exporter", None)
        if exporter is not None:
            exporter.invalidate_bounds_cache()
        # Unconditional: keeps the fit-content toggle's enabled state correct
        # as edits change whether the canvas still fits within 0..1.
        self.preview_coordinator.recalculate_global_bounds()

        self.preview_coordinator.schedule_update()

    def update_buttons_state(self):
        self.emit_buttons_state(
            self.editor_service.can_undo(), self.editor_service.can_redo()
        )

    def on_fps_changed(self, fps: int):
        self.model.fps = max(1, fps)

    def on_aspect_ratio_lock_changed(self, locked: bool):
        self.model.aspect_ratio_locked = locked
        if locked and self.model.height > 0:
            self.model.original_ratio = self.model.width / self.model.height
        self.update_buttons_state()
