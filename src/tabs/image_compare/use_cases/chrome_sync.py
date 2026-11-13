"""Display-sync chrome for image_compare, moved out of the host
``MainWindowPresenter`` (see docs/dev/presenter-decoupling-plan.md Stage 4).

The host drives this object through ``window_presenter`` passed in as an
argument, and reactive store updates flow through ``store.state_changed``.
Audit-Meta: pattern=thin-owner-target size=exempt reason="use_cases target for MainWindowPresenter — display-sync chrome for document/viewport"
"""

from PySide6.QtCore import QObject, QSignalBlocker, QTimer

from core.state_management.viewport_scopes import LIGHTWEIGHT_VIEWPORT_SUBDOMAINS
from core.log_names import APP_LOGGER_NAME
from sli_ui_toolkit.i18n import tr


def _document(store):
    return store.get_session_state_slot("document")


# Original-file dimension memo: path -> (mtime_ns, size, (w, h)). Pure-read
# cache so repeated HUD refreshes don't hit disk; validated by the
# fail-closed (mtime+size) identity each call — an overwrite changing either
# re-probes instead of serving pre-overwrite dims.
_original_dims_cache: dict[str, tuple[int, int, tuple[int, int]]] = {}


def _probe_original_dimensions(path: str | None) -> tuple[int, int] | None:
    """File-header probe for the file's ORIGINAL dims (no full decode).

    Returns None on ANY failure (missing file, unknown format, oversized
    non-streamable, exception) so callers fall through to the existing
    image_state → pipeline chain unchanged.
    """
    if not path:
        return None
    try:
        import os

        identity = _current_file_identity(path)
        if identity is None:
            return None
        cached = _original_dims_cache.get(path)
        if cached is not None and (cached[0], cached[1]) == identity:
            return cached[2]
        from shared.image_processing import progressive_loader as _pl

        dims = _pl.get_image_dimensions(path)
        if dims is None:
            return None
        result = (int(dims[0]), int(dims[1]))
        _original_dims_cache[path] = (identity[0], identity[1], result)
        return result
    except Exception:
        return None


def _set_slider_value_quietly(slider, value: int) -> None:
    if slider.value() == value:
        return
    blocker = QSignalBlocker(slider)
    try:
        slider.setValue(value)
    finally:
        del blocker


def _session_provides_resource_namespace(
    session_manager,
    session,
    namespace: str,
) -> bool:
    try:
        blueprint = session_manager.get_session_blueprint(session.session_type)
    except Exception:
        blueprint = None
    if blueprint is None:
        return False
    for resource in getattr(blueprint, "resource_namespaces", ()):
        if getattr(resource, "namespace", None) == namespace:
            return True
    return False


def get_current_display_name(store, image_number: int) -> str:
    document = _document(store)
    if document is None:
        return ""
    return document.get_current_display_name(image_number)


def get_current_score(store, image_number: int) -> int | None:
    document = _document(store)
    if document is None:
        return None
    target_list, index = (
        (document.image_list1, document.current_index1)
        if image_number == 1
        else (document.image_list2, document.current_index2)
    )
    if 0 <= index < len(target_list):
        return target_list[index].rating
    return None


def _current_file_identity(path: str) -> tuple[int, int] | None:
    """Fail-closed (mtime_ns, size) identity for *path* — None on any doubt.

    Cache keys carry (path, mtime_ns, size); an entry is only the file's
    pixels when its identity matches the file on disk NOW. A missing file
    or failed stat returns None so callers skip rather than serve stale.
    """
    try:
        import os

        st = os.stat(path)
        return (int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        return None


def _peek_via_pipeline_state(store, path: str | None):
    if not path:
        return None
    try:
        ps = store.get_session_state_slot("pipeline")
        if ps is not None:
            import os

            from tabs.image_compare.pipeline.cache import _pixel_key, _preview_key

            for cache_dict, key_fn in (
                (getattr(ps, "pixel", None), _pixel_key),
                (getattr(ps, "preview", None), _preview_key),
            ):
                if cache_dict is None:
                    continue
                try:
                    k = key_fn(path, None, None)
                    v = cache_dict.get(k)
                    if v is not None:
                        if hasattr(v, "is_open") and not v.is_open:
                            pass
                        elif hasattr(v, "isNull") and v.isNull():
                            pass
                        else:
                            return v
                except Exception:
                    pass
                try:
                    norm = os.path.normpath(path)
                    # Fail-closed identity: the exact-key lookup above is
                    # self-validating (key_fn stats the live file), but this
                    # path-prefix scan matches on path alone — an overwrite
                    # keeps the path while changing dims. Only serve entries
                    # whose key (mtime_ns, size) still matches the file on
                    # disk; stale entries skip so the HUD never shows
                    # pre-overwrite dims.
                    identity = _current_file_identity(path)
                    if identity is None:
                        continue
                    for kk, vv in cache_dict.items():
                        if kk[0] == norm:
                            try:
                                if (
                                    len(kk) < 3
                                    or int(kk[1]) != identity[0]
                                    or int(kk[2]) != identity[1]
                                ):
                                    continue
                            except Exception:
                                continue
                            if hasattr(vv, "is_open") and not vv.is_open:
                                continue
                            if hasattr(vv, "isNull") and vv.isNull():
                                continue
                            return vv
                except Exception:
                    pass
    except Exception:
        pass
    return None


def get_image_dimensions(store, image_number: int) -> tuple[int, int] | None:
    document = _document(store)
    if document is None:
        return None
    path = document.image1_path if image_number == 1 else document.image2_path
    if not path:
        return None
    # W4: original file dims win from first paint — including after unify
    # replaces image_state with the max-canvas pair. Falls through to the
    # existing chain on ANY probe failure (no behavior change then).
    probed = _probe_original_dimensions(path)
    if probed is not None:
        return probed
    img = None
    try:
        vp = store.viewport.session_data.image_state
        cand = vp.image1 if image_number == 1 else vp.image2
        if cand is not None:
            try:
                if hasattr(cand, "isNull") and cand.isNull():
                    cand = None
                elif hasattr(cand, "is_open") and not cand.is_open:
                    cand = None
            except Exception:
                pass
            if cand is not None:
                img = cand
    except Exception:
        pass
    if img is None:
        img = _peek_via_pipeline_state(store, path)
    if img:
        if hasattr(img, "width") and hasattr(img, "height"):
            w = img.width() if callable(img.width) else img.width
            h = img.height() if callable(img.height) else img.height
            return (int(w), int(h))
        if hasattr(img, "size"):
            s = img.size() if callable(img.size) else img.size
            if hasattr(s, "width") and hasattr(s, "height"):
                return (int(s.width()), int(s.height()))
            if isinstance(s, (tuple, list)):
                return (int(s[0]), int(s[1]))
    return None



class ImageCompareChromeSync(QObject):
    def __init__(self, widget, store, resolve_window_presenter):
        super().__init__(widget)
        self.widget = widget
        self.store = store
        self._resolve_window_presenter = resolve_window_presenter
        # Reuse widget's StaleGate if present (dedup render/metrics/language)
        gate = getattr(widget, "_stale_gate", None)
        if gate is not None:
            self._stale_gate = gate
        else:
            from tabs.image_compare.use_cases.stale_gate import StaleGate

            self._stale_gate = StaleGate()
            try:
                widget._stale_gate = self._stale_gate  # type: ignore[attr-defined]
            except Exception:
                pass
        self.store.state_changed.connect(self._on_store_state_changed)

    @property
    def _workspace_language_stale(self) -> bool:  # type: ignore[override]
        return self._stale_gate.is_stale("language")

    @_workspace_language_stale.setter
    def _workspace_language_stale(self, v: bool) -> None:
        if v:
            self._stale_gate.mark("language")
        else:
            self._stale_gate.consume("language")

    @property
    def _render_stale(self) -> bool:  # type: ignore[override]
        return self._stale_gate.is_stale("render")

    @_render_stale.setter
    def _render_stale(self, v: bool) -> None:
        if v:
            self._stale_gate.mark("render")
        else:
            self._stale_gate.consume("render")

    def _is_visible(self) -> bool:
        widget = self.widget
        if widget is None:
            return True
        try:
            window = widget.window()
            ui = getattr(window, "ui", None)
            if ui is None:
                wp = self._window_presenter()
                ui = getattr(wp, "ui", None) if wp is not None else None
            stack = getattr(ui, "workspace_stack", None) if ui is not None else None
            if stack is not None:
                current = stack.currentWidget()
                if current is widget:
                    return True
                if current is not None and hasattr(current, "isAncestorOf"):
                    try:
                        if current.isAncestorOf(widget):
                            return True
                    except Exception:
                        pass
                return False
            return bool(widget.isVisible())
        except Exception:
            try:
                return bool(widget.isVisible())
            except Exception:
                return True

    def _has_full_res(self) -> bool:
        """Lazy gate: resolution/psnr/ssim not needed until full_res images are present."""
        try:
            img_state = self.store.viewport.session_data.image_state
            img1 = getattr(img_state, "image1", None)
            img2 = getattr(img_state, "image2", None)
            has1 = img1 is not None
            has2 = img2 is not None
            if not (has1 or has2):
                return False
            for img in (img1, img2):
                if img is None:
                    continue
                try:
                    if hasattr(img, "isNull") and img.isNull():
                        return False
                    if hasattr(img, "is_open") and not img.is_open:
                        return False
                except Exception:
                    pass
            # For resolution labels original shows only when both present
            # but we gate resolution on at least one present to avoid early waste
            return has1 or has2
        except Exception:
            return True

    def _window_presenter(self):
        try:
            return self._resolve_window_presenter()
        except Exception:
            return None

    def _toolbar_presenter(self, window_presenter):
        if window_presenter is not None and hasattr(window_presenter, "get_feature"):
            return window_presenter.get_feature("toolbar")
        return None

    def refresh_filename_toolbar(self, window_presenter=None) -> bool:
        """Drive the filename toolbar button from the Store flag.

        Single fanout entry for every branch that moves the bottom edit
        panel (visible/deferred/tab-switch/restore): resolves the toolbar
        presenter, runs the EXISTING ``update_toolbar_states`` fanout, and
        reports False (with a warning log) when there is nobody to sync
        through instead of a silent no-op. Never raises.
        """
        import logging

        _logger = logging.getLogger(APP_LOGGER_NAME)
        try:
            wp = window_presenter if window_presenter is not None else self._window_presenter()
            toolbar = self._toolbar_presenter(wp)
            if toolbar is not None and hasattr(toolbar, "update_toolbar_states"):
                toolbar.update_toolbar_states()
                return True
            _logger.warning(
                "refresh_filename_toolbar: no toolbar presenter reachable, "
                "button not synced with the Store"
            )
            return False
        except Exception:
            _logger.warning(
                "refresh_filename_toolbar: toolbar sync failed", exc_info=True
            )
            return False

    def _settings_presenter(self, window_presenter):
        if window_presenter is not None and hasattr(window_presenter, "get_feature"):
            return window_presenter.get_feature("settings")
        return None

    def _font_settings_flyout(self, window_presenter):
        transient = getattr(getattr(window_presenter, "ui_manager", None), "transient", None)
        return getattr(transient, "font_settings_flyout", None)

    def _unified_flyout(self, window_presenter):
        transient = getattr(getattr(window_presenter, "ui_manager", None), "transient", None)
        return getattr(transient, "unified_flyout", None)

    def _on_store_state_changed(self, domain):
        if domain == "workspace":
            return
        is_viewport_domain = domain == "viewport" or domain.startswith("viewport.")
        if not is_viewport_domain and domain not in ("document", "settings"):
            return
        viewport_subdomain = (
            domain.split(".", 1)[1] if is_viewport_domain and "." in domain else None
        )
        if viewport_subdomain in LIGHTWEIGHT_VIEWPORT_SUBDOMAINS:
            # Lightweight, purely-visual subdomain: skip the generic chrome
            # batch. The "overlay" one (font/label settings) still needs a
            # canvas repaint — kick it directly, same as the pan path.
            if viewport_subdomain == "overlay":
                window_presenter = self._window_presenter()
                if window_presenter is not None:
                    try:
                        window_presenter.schedule_canvas_update()
                    except Exception:
                        pass
            return
        window_presenter = self._window_presenter()
        if window_presenter is None:
            return
        self.handle_store_domain(window_presenter, domain)

    def handle_store_domain(self, window_presenter, domain):
        if not self._is_visible():
            self._render_stale = True
            try:
                widget = self.widget
                if widget is not None:
                    widget._render_stale = True  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                from core.tracing.tracer import Tracer
                if Tracer.enabled():
                    Tracer.instance().record("render.ic.chrome_deferred", "IC chrome deferred - background tab", {"domain": domain})
            except Exception:
                pass
            self.refresh_filename_toolbar(window_presenter)
            self.widget.toggle_edit_layout_visibility(
            self.store.viewport.render_config.include_file_names_in_saved
            )
            return
        # Видимая ветка двигает нижнюю edit-панель за флагом вместе с кнопкой
        # через общий refresh_filename_toolbar: иначе клик btn_file_names
        # меняет стор и панель, а кнопка после рестарта остаётся stale.
        self.refresh_filename_toolbar(window_presenter)
        try:
            widget = self.widget
            if widget is not None:
                widget.toggle_edit_layout_visibility(
                    self.store.viewport.render_config.include_file_names_in_saved
                )
        except Exception:
            import logging

            logging.getLogger(APP_LOGGER_NAME).debug(
                "handle_store_domain: edit panel toggle missed", exc_info=True
            )
        # Lazy: resolution/psnr/zoom not needed until full_res images are present
        batch = ["file_names", "combobox", "ratings", "window_schedule"]
        if self._has_full_res():
            batch.extend(["resolution", "zoom_indicator"])
        window_presenter.ui_batcher.schedule_batch_update(batch)
        # A render-config change (font size/weight/color, diff mode, ...) is a
        # purely visual canvas change. Kicking the canvas directly (same as the
        # pan path: interaction.set_pan -> widget.update()) lands the frame on
        # the next event-loop turn; the batched `file_names` branch also calls
        # schedule_canvas_update, but its QTimer.singleShot(0) hop added ~30ms
        # before the frame. schedule_update coalesces, so the later batched
        # call is a no-op.
        try:
            window_presenter.schedule_canvas_update()
        except Exception:
            import logging

            logging.getLogger(APP_LOGGER_NAME).debug(
                "handle_store_domain: direct canvas update missed", exc_info=True
            )

    def on_workspace_changed(self, window_presenter):
        self._refresh_active_session_canvas(window_presenter)
        if not self._is_visible():
            self._render_stale = True
            try:
                widget = self.widget
                if widget is not None:
                    widget._render_stale = True  # type: ignore[attr-defined]
            except Exception:
                pass
            return
        batch = ["file_names", "combobox", "ratings", "window_schedule"]
        if self._has_full_res():
            batch.extend(["resolution", "zoom_indicator"])
        window_presenter.ui_batcher.schedule_batch_update(batch)

    def apply_initial_state(self, window_presenter):
        _set_slider_value_quietly(
            self.widget.slider_speed,
            int(self.store.viewport.view_state.movement_speed_per_sec * 100),
        )
        self.widget.btn_file_names.setChecked(
            self.store.viewport.render_config.include_file_names_in_saved,
            emit_signal=False,
        )

        toolbar = self._toolbar_presenter(window_presenter)
        if toolbar is not None and hasattr(toolbar, "update_toolbar_states"):
            toolbar.update_toolbar_states()
        self.widget.toggle_edit_layout_visibility(
            self.store.viewport.render_config.include_file_names_in_saved
        )

        from domain.qt_adapters import color_to_qcolor

        flyout = self._font_settings_flyout(window_presenter)
        if flyout is not None:
            flyout.set_values(
                self.store.viewport.render_config.font_size_percent,
                self.store.viewport.render_config.font_weight,
                color_to_qcolor(self.store.viewport.render_config.file_name_color),
                color_to_qcolor(self.store.viewport.render_config.file_name_bg_color),
                self.store.viewport.render_config.draw_text_background,
                self.store.viewport.render_config.text_placement_mode,
                self.store.viewport.render_config.text_alpha_percent,
                self.store.settings.current_language,
            )

        settings_presenter = self._settings_presenter(window_presenter)
        if settings_presenter is not None:
            settings_presenter.update_interpolation_combo_box_ui()
            settings_presenter.setup_view_buttons()
        self.do_update_file_names_display(window_presenter)

    def _refresh_active_session_canvas(self, window_presenter):
        session_manager = getattr(window_presenter, "session_manager", None)
        if session_manager is None:
            return
        active = session_manager.get_active_session()
        if active is None or not _session_provides_resource_namespace(
            session_manager,
            active,
            "comparison",
        ):
            window_presenter._last_active_session_id = getattr(active, "id", None)
            return
        last_id = getattr(window_presenter, "_last_active_session_id", None)
        window_presenter._last_active_session_id = active.id
        if last_id == active.id:
            return

        for attr in (
            "_last_img_sig",
            "_last_mag_signature",
            "_last_bg_signature",
            "_last_label_dims",
        ):
            if hasattr(window_presenter, attr):
                setattr(window_presenter, attr, None)
        sessions = getattr(
            getattr(window_presenter, "main_controller", None), "sessions", None
        )
        if sessions is None:
            return
        try:
            sessions.set_current_image(1, emit_signal=False)
            sessions.set_current_image(2, emit_signal=False)
        except Exception:
            import logging

            logging.getLogger(APP_LOGGER_NAME).exception(
                "_refresh_active_session_canvas: set_current_image failed"
            )

    def do_update_resolution_labels(self, window_presenter):
        document = _document(self.store)
        if document is None:
            return
        # Guard: widget/labels may be deleted before batched timer fires
        widget = getattr(self, "widget", None)
        if widget is None:
            return
        try:
            import shiboken6  # type: ignore

            if not shiboken6.isValid(widget):
                return
        except Exception:
            pass
        has_image1 = bool(document.image1_path)
        has_image2 = bool(document.image2_path)
        has_both_images = has_image1 and has_image2

        res1_text = ""
        res2_text = ""
        if has_both_images:
            if dim := get_image_dimensions(self.store, 1):
                res1_text = f"{dim[0]}x{dim[1]}"
            if dim := get_image_dimensions(self.store, 2):
                res2_text = f"{dim[0]}x{dim[1]}"
        try:
            self.widget.update_resolution_labels(
                res1_text,
                res2_text,
                has_image1=has_image1,
                has_image2=has_image2,
            )
        except RuntimeError:
            return

        try:
            psnr_visible = self.store.viewport.session_data.image_state.auto_calculate_psnr
            self.widget.psnr_label.setVisible(psnr_visible)
            if psnr_visible:
                psnr = self.store.viewport.session_data.image_state.psnr_value
                if psnr is not None:
                    self.widget.psnr_label.setText(
                        f"{tr('image_compare.ui.psnr', self.store.settings.current_language)}: {psnr:.2f} dB"
                    )
                else:
                    self.widget.psnr_label.setText(
                        f"{tr('image_compare.ui.psnr', self.store.settings.current_language)}: --"
                    )

            ssim_visible = (
                self.store.viewport.session_data.image_state.auto_calculate_ssim
                or self.store.viewport.view_state.diff_mode == "ssim"
            )
            self.widget.ssim_label.setVisible(ssim_visible)
            if ssim_visible:
                ssim = self.store.viewport.session_data.image_state.ssim_value
                if ssim is not None:
                    self.widget.ssim_label.setText(
                        f"{tr('image_compare.ui.ssim', self.store.settings.current_language)}: {ssim:.4f}"
                    )
                else:
                    self.widget.ssim_label.setText(
                        f"{tr('image_compare.ui.ssim', self.store.settings.current_language)}: --"
                    )

            self.widget.footer_info_widget.setVisible(psnr_visible or ssim_visible)
        except RuntimeError:
            return

    def do_sync_zoom_indicator(self, window_presenter):
        try:
            zoom_indicator = getattr(self.widget, "zoom_indicator", None)
            image_label = getattr(self.widget, "image_label", None)
            if zoom_indicator is None or image_label is None or not zoom_indicator.isVisible():
                return
        except RuntimeError:
            return
        from ui.canvas_infra.viewport.state import get_zoom_level

        try:
            self.widget.update_zoom_indicator(get_zoom_level(image_label))
        except RuntimeError:
            return

    def do_update_file_names_display(self, window_presenter):
        document = _document(self.store)
        if document is None:
            return
        active_name1 = document.get_active_display_name(1)
        active_name2 = document.get_active_display_name(2)
        name1 = active_name1 or "-----"
        name2 = active_name2 or "-----"
        lang = self.store.settings.current_language
        show_labels = bool(name1 != "-----" or name2 != "-----")

        try:
            self.widget.update_file_names_display(
                name1_text=name1,
                name2_text=name2,
                is_horizontal=self.store.viewport.view_state.is_horizontal,
                current_language=lang,
                show_labels=show_labels,
                has_image1=bool(document.image1_path),
                has_image2=bool(document.image2_path),
            )
        except RuntimeError:
            return

        if not self.widget.edit_name1.hasFocus():
            self.widget.edit_name1.blockSignals(True)
            self.widget.edit_name1.setText(active_name1)
            self.widget.edit_name1.setCursorPosition(0)
            self.widget.edit_name1.blockSignals(False)

        if not self.widget.edit_name2.hasFocus():
            self.widget.edit_name2.blockSignals(True)
            self.widget.edit_name2.setText(active_name2)
            self.widget.edit_name2.setCursorPosition(0)
            self.widget.edit_name2.blockSignals(False)

        toolbar = self._toolbar_presenter(window_presenter)
        if toolbar is not None:
            toolbar.check_name_lengths()

    def do_update_combobox_displays(self, window_presenter):
        document = _document(self.store)
        if document is None:
            return
        settings_presenter = self._settings_presenter(window_presenter)
        if settings_presenter is not None:
            settings_presenter.update_interpolation_combo_box_ui()
            settings_presenter.setup_view_buttons()
        count1 = len(document.image_list1)
        idx1 = document.current_index1
        text1 = (
            get_current_display_name(self.store, 1)
            if 0 <= idx1 < count1
            else tr("image_compare.misc.select_an_image", self.store.settings.current_language)
        )
        self.widget.update_combobox_display(1, count1, idx1, text1, "")

        count2 = len(document.image_list2)
        idx2 = document.current_index2
        text2 = (
            get_current_display_name(self.store, 2)
            if 0 <= idx2 < count2
            else tr("image_compare.misc.select_an_image", self.store.settings.current_language)
        )
        self.widget.update_combobox_display(2, count2, idx2, text2, "")

        unified_flyout = self._unified_flyout(window_presenter)
        try:
            if unified_flyout is not None and unified_flyout.isVisible():
                unified_flyout.sync_from_store()
        except RuntimeError:
            pass

    def do_update_rating_displays(self, window_presenter):
        self.widget.update_rating_display(
            1, get_current_score(self.store, 1), self.store.settings.current_language
        )
        self.widget.update_rating_display(
            2, get_current_score(self.store, 2), self.store.settings.current_language
        )

        unified_flyout = self._unified_flyout(window_presenter)
        try:
            if unified_flyout is not None and unified_flyout.isVisible():
                document = _document(self.store)
                current_idx1 = document.current_index1
                current_idx2 = document.current_index2
                if current_idx1 >= 0:
                    unified_flyout.update_rating_for_item(1, current_idx1)
                if current_idx2 >= 0:
                    unified_flyout.update_rating_for_item(2, current_idx2)
                QTimer.singleShot(0, unified_flyout.refreshGeometry)
        except RuntimeError:
            pass

    def on_language_changed(self, window_presenter):
        # Mark stale unconditionally, then flush when the page is current.
        # Marking is what makes ``flush_stale_workspace_language`` cover BOTH
        # cases: a page stacked away at switch time (flushed on activation) and
        # a page that is already current (flushed right here). The old code
        # only refreshed the visible page and never marked the hidden one, so
        # the settings presenter chrome and the PSNR/SSIM prefixes stayed in the
        # previous language on either path.
        self._workspace_language_stale = True
        self.flush_stale_workspace_language(window_presenter)

    def flush_stale_render(self, window_presenter) -> None:
        # Dedup: single StaleGate — delegate to widget's flush if shared gate, else consume gate.
        gate = getattr(self, "_stale_gate", None)
        if gate is not None and not gate.is_stale("render"):
            return
        if not self._is_visible():
            return
        # If widget shares the same gate, let widget's flush be canonical (it also handles presenter schedule).
        widget = self.widget
        if widget is not None and getattr(widget, "_stale_gate", None) is gate and hasattr(widget, "_flush_stale_render"):
            try:
                widget._flush_stale_render()
            except Exception:
                pass
            # ensure gate consumed (widget already did)
            return
        # fallback: consume and schedule batch via window_presenter
        if gate is not None:
            gate.consume("render")
        else:
            self._render_stale = False
        if widget is not None:
            try:
                widget._render_stale = False  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            from core.tracing.tracer import Tracer
            if Tracer.enabled():
                Tracer.instance().record("render.ic.chrome_flush", "IC chrome stale flushed on show", {})
        except Exception:
            pass
        try:
            batch = ["file_names", "combobox", "ratings", "window_schedule"]
            if self._has_full_res():
                batch.extend(["resolution", "zoom_indicator"])
            window_presenter.ui_batcher.schedule_batch_update(batch)
        except Exception:
            pass

    def _refresh_visible_workspace_language(self, window_presenter, lang_code: str) -> None:
        from domain.qt_adapters import color_to_qcolor

        self.do_update_combobox_displays(window_presenter)
        self.do_update_rating_displays(window_presenter)
        self.do_update_file_names_display(window_presenter)
        # PSNR/SSIM prefixes ("PSNR:" / "ССИМ:") are built with tr(key, lang)
        # inside this call — without it a live switch leaves the old prefix.
        self.do_update_resolution_labels(window_presenter)
        flyout = self._font_settings_flyout(window_presenter)
        if flyout is not None:
            flyout.set_values(
                self.store.viewport.render_config.font_size_percent,
                self.store.viewport.render_config.font_weight,
                color_to_qcolor(self.store.viewport.render_config.file_name_color),
                color_to_qcolor(self.store.viewport.render_config.file_name_bg_color),
                self.store.viewport.render_config.draw_text_background,
                self.store.viewport.render_config.text_placement_mode,
                getattr(
                    self.store.viewport.render_config, "text_alpha_percent", 100
                ),
                lang_code,
            )
        if hasattr(window_presenter, "repopulate_flyouts"):
            window_presenter.repopulate_flyouts()

    def flush_stale_workspace_language(self, window_presenter) -> None:
        if not self._workspace_language_stale:
            return
        if self.widget is None or not self.widget.isVisible():
            return
        self._workspace_language_stale = False
        lang_code = self.store.settings.current_language
        settings_presenter = self._settings_presenter(window_presenter)
        if settings_presenter is not None:
            settings_presenter.on_language_changed()
        self._refresh_visible_workspace_language(window_presenter, lang_code)
