"""Parametric-crop box resolution for multi-compare (IC context-crop parity).

Single construction site for the question "what to cut for this slot":
per-slot ``crop_override`` wins, ``None`` (Auto) follows the global
``auto_crop_black_borders`` setting, and the shared ``CropService`` helper
resolves the suggestion box. The decode path is never involved (MC decodes
full-frame; crop applies at sampling) — this module only answers with a
box in full-frame source px, or ``None`` for no crop. Never raises.
"""

from __future__ import annotations


def effective_crop_box(controller, slot) -> tuple[int, int, int, int] | None:
    """Crop window for ``slot`` in full-frame source px, or ``None``.

    ``override False`` → ``None``. ``override True`` → the session
    service's box (``None`` when the service reports no box). ``override
    None`` → the controller's global-aware service first (respects the
    setting), same outcome. Any failure (no service, probe error,
    missing path) yields ``None`` = show the full frame.
    """
    try:
        override = getattr(slot, "crop_override", None)
        if override is False:
            return None
        service = None
        if override is True:
            service = getattr(controller, "_crop_service", None)
        else:
            getter = getattr(controller, "_get_crop_service", None)
            if callable(getter):
                try:
                    service = getter()
                except Exception:
                    service = None
            if service is None:
                service = getattr(controller, "_crop_service", None)
                try:
                    store = getattr(controller, "store", None)
                    should = getattr(
                        getattr(store, "settings", None),
                        "auto_crop_black_borders",
                        False,
                    )
                    if not should:
                        service = None
                except Exception:
                    pass
        if service is None:
            return None
        path = getattr(slot, "path", None)
        if path is None:
            return None
        try:
            box = service.get(path)
        except Exception:
            return None
        if box is None:
            return None
        try:
            return box.to_tuple()
        except Exception:
            try:
                left, top, right, bottom = (int(v) for v in box)
                return (left, top, right, bottom)
            except Exception:
                return None
    except Exception:
        return None
