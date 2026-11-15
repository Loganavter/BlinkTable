"""Startup placeholder / zoom indicator / focus-dim chrome for ``MultiCompareWidget``.

Split out of ``widget.py`` to keep that class down to composition/wiring --
mirrors the ``drag_drop`` split. Every function here takes the widget as its
first argument and reads/writes its instance state directly.

Qt calls ``resizeEvent``/``hideEvent``/``showEvent`` by name on the widget
itself, so those stay defined as thin methods on ``MultiCompareWidget`` that
delegate into this module.
"""

from __future__ import annotations

from tabs.multi_compare.debug import mc_dnd_debug as _dnd_log
from core.log_names import APP_LOGGER_NAME


def sync_zoom_indicator(widget) -> None:
    indicator = getattr(widget, "zoom_indicator", None)
    if indicator is None:
        return
    st = widget.store.state
    zoom = float(getattr(st, "zoom", 1.0))
    pan_x = float(getattr(st, "pan_x", 0.0))
    pan_y = float(getattr(st, "pan_y", 0.0))
    # Scene camera first: when the whole composition is zoomed the chip shows
    # THAT zoom (and its pan decides visibility), because it is the camera the
    # user last moved and the one ``reset_view`` clears. At identity the chip
    # falls back to the content zoom exactly as before.
    scene_zoom = float(getattr(st, "scene_zoom", 1.0))
    scene_pan_x = float(getattr(st, "scene_pan_x", 0.0))
    scene_pan_y = float(getattr(st, "scene_pan_y", 0.0))
    if abs(scene_zoom - 1.0) > 1e-6:
        zoom, pan_x, pan_y = scene_zoom, scene_pan_x, scene_pan_y
    # Skip redundant Qt churn (setText + reposition + raise per zoom tick):
    # the chip only renders an int percent and a visible/hidden state, so
    # ticks that change neither (pan drift, sub-percent repeats) reuse the
    # current chip as-is. Key mirrors ``ZoomIndicator.update_zoom``'s own
    # percent/visibility formulas -- if those change, this key must follow.
    # The chip label is language-sensitive, so the language joins the key: a
    # language switch still refreshes the chip via show/event-driven resync
    # even when the numbers are unchanged (read off the indicator's own
    # provider -- tab code must not import app i18n ``resources.translations``
    # here, see the tabs-isolation contract). Target geometry joins too: a
    # resize with an unchanged zoom still needs a reposition.
    percent = int(round(zoom * 100))
    visible = (
        abs(zoom - 1.0) > 1e-3 or abs(pan_x) > 1e-4 or abs(pan_y) > 1e-4
    )
    try:
        lang_provider = getattr(indicator, "_lang_provider", None)
        lang = lang_provider() if callable(lang_provider) else "en"
    except Exception:
        lang = "en"
    target = getattr(indicator, "_target_widget", None)
    try:
        geom = (target.width(), target.height()) if target is not None else None
    except Exception:
        geom = None
    sig = (lang, percent, visible, geom)
    if sig == getattr(widget, "_zoom_indicator_sig", None):
        return
    widget._zoom_indicator_sig = sig
    from ui.widgets.flyout_debug import flyout_debug, flyout_debug_enabled

    if flyout_debug_enabled():
        flyout_debug(
            "mc-zoom-indicator: _sync_zoom_indicator() zoom=%.4f pan=(%.2f, %.2f) "
            "widget.size=%r canvas_container.size=%r canvas.size=%r",
            float(getattr(st, "zoom", 1.0)),
            float(getattr(st, "pan_x", 0.0)),
            float(getattr(st, "pan_y", 0.0)),
            widget.size(),
            widget._canvas_container.size(),
            widget.canvas.size(),
        )
    indicator.update_zoom(zoom, pan_x, pan_y)


def dismiss_placeholder_for_dnd(widget) -> bool:
    """Hide the startup placeholder once a live drop preview is dispatched.

    Empty canvas rarely accumulates the 10 presents the first-frame gate
    needs (on-demand repaints: ~2 frames at startup, then idle), so the
    opaque placeholder would otherwise cover the drop-zone overlay for the
    whole first drag — RASTER+COMMIT happen underneath it, invisibly. After
    the first drop, uploads/mip-cascades drive enough frames to cross the
    gate and the placeholder dismisses itself, which is why only the first
    DnD looks empty. (Both canvases are on-demand; image_compare usually
    crosses its gate at startup simply by rendering restored session
    content, so it rarely hits this.)
    Placeholder bg == canvas bg, so hiding is seamless. Kept only while the
    surface never presented (presents == 0): then nothing renders yet and
    the placeholder still covers a real transparent hole.
    Returns True when it actually hid something (for ``[mc-dnd]`` logging).
    """
    placeholder = getattr(widget, "_startup_placeholder", None)
    canvas = getattr(widget, "canvas", None)
    presents = getattr(canvas, "_rhi_presents_completed", -1)
    first_frame = getattr(canvas, "_first_frame_emitted", "?")
    try:
        ph_visible = bool(placeholder.isVisible()) if placeholder is not None else None
    except Exception:
        ph_visible = "?"
    # Log the decision (deduped): this is the line that tells whether the
    # drop zone was covered, and why the cover was kept or lifted. presents
    # enters the sig as alive/not-alive only — exact counts would re-log
    # every frame while the cover is up.
    alive = isinstance(presents, int) and presents > 0
    sig = (placeholder is None, ph_visible, alive)
    changed = sig != getattr(widget, "_dnd_ph_sig", None)
    if changed:
        widget._dnd_ph_sig = sig
        _dnd_log(
            "placeholder check: present=%s visible=%s presents=%s first_frame=%s",
            placeholder is not None, ph_visible, presents, first_frame,
        )
    if placeholder is None or not ph_visible:
        return False
    if not alive:
        # Inside the sig gate: without this, per-move re-checks flood the
        # log with identical KEPT lines while presents==0.
        if changed:
            _dnd_log("placeholder KEPT (surface never presented)")
        return False
    placeholder.hide()
    release_transition_mask(widget)
    _dnd_log("placeholder DISMISSED (was covering drop preview)")
    return True


def on_first_frame(widget) -> None:
    from tabs.multi_compare.first_frame_debug import mc_first_frame_debug

    placeholder = widget._startup_placeholder
    mc_first_frame_debug(
        widget.canvas,
        "first frame -> placeholder hidden (was_visible=%s was_covering=%s)",
        bool(placeholder is not None and placeholder.isVisible()),
        bool(
            placeholder is not None
            and placeholder.isVisible()
            and placeholder.geometry().intersects(widget.canvas.geometry())
        ),
    )
    if placeholder is not None:
        placeholder.hide()
    release_transition_mask(widget)


def release_transition_mask(widget) -> None:
    """Drop the workspace transition cover once an opaque frame is up.

    Mirrors image_compare's widget: without this the cover would stay for
    its whole ``max_duration_ms`` (400 ms) on every tab enter, because the
    mask force-releases only on its deadline unless told otherwise.
    """
    context = getattr(widget, "_context", None)
    services = getattr(context, "services", None) if context else None
    if not services:
        return
    mask = services.get("workspace.transition_mask")
    if mask is None:
        return
    try:
        mask.release()
    except Exception:
        import logging

        logging.getLogger(APP_LOGGER_NAME).exception(
            "[workspace-transition] MC mask.release failed"
        )


def resize_event(widget, event) -> None:
    placeholder = getattr(widget, "_startup_placeholder", None)
    if placeholder is not None:
        # Always sync — during the first show/layout the placeholder is
        # still at its construction-time default size (100x30) and is not
        # "visible" yet, so an isVisible() guard would skip the resize and
        # leave the canvas uncovered (transparent) for its first frames.
        placeholder.sync_geometry()
    indicator = getattr(widget, "zoom_indicator", None)
    if indicator is not None and indicator.isVisible():
        indicator.sync_position()
    sync_focus_dim_overlays(widget)


def exit_focus(widget) -> None:
    from tabs.multi_compare.scene import actions

    if widget.state.is_focused:
        widget.store.dispatch(actions.set_focus(None))


def sync_focus_dim_overlays(widget) -> None:
    dim_toolbar = getattr(widget, "_focus_dim_toolbar", None)
    dim_footer = getattr(widget, "_focus_dim_footer", None)
    if dim_toolbar is None or dim_footer is None:
        return
    dim_toolbar.setGeometry(widget.toolbar.geometry())
    dim_footer.setGeometry(widget.footer.geometry())
    dim_toolbar.raise_()
    dim_footer.raise_()


def hide_event(widget, event) -> None:
    # ZoomIndicator is a `pinned` flyout reparented onto the app-wide
    # OverlayLayer host (see ui/flyout_policy.py), not a child of this
    # page -- Qt's own hideEvent on this page (fired when the workspace
    # switches to another tab/session) does not cascade to it, so
    # without this it kept rendering above whichever tab became active
    # next. show_event below resyncs it from current state when this tab
    # is shown again.
    indicator = getattr(widget, "zoom_indicator", None)
    if indicator is not None:
        indicator.hide()


def show_event(widget, event) -> None:
    placeholder = getattr(widget, "_startup_placeholder", None)
    if placeholder is not None:
        # Re-size/raise before the canvas's first paint: at construction
        # the placeholder tracked a 100x30 container, and leaving it there
        # exposes the unrendered (transparent) QRhi surface on the first
        # frames (see the placeholder probe in canvas_widget.py).
        placeholder.sync_geometry()
    sync_zoom_indicator(widget)
