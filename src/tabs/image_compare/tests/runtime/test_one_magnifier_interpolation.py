"""The loupe has ONE interpolation — moving must not switch to a hidden one.

Owner report 2026-10-26: the toolbar panel showed "Ближайший сосед" (Nearest)
and the loupe still looked bicubic *while being dragged*. The movement had its
own filter (`interactive_movement_interpolation_method`) applied only while the
pointer interacted, and that value was visible nowhere in the panel.

Why the split was removed rather than documented: the filter costs nothing —
`MagnifierPass` packs it as a per-record uniform (`magnifier_pass.py:683`), while
tiles, LOD level selection and uploads are unchanged — so the "optimisation"
bought no frame time and produced a knob whose effect depends on whether you are
moving the mouse.
"""

from __future__ import annotations

from core.state_management.appearance_actions import SetInterpolationMethodAction
from core.store_viewport import RenderConfig
from tabs.image_compare.state.reducers import ImageRenderConfigReducer


def _config(**kw):
    base = dict(
        interpolation_method="LANCZOS",
        zoom_interpolation_method="BILINEAR",
        movement_interpolation_method="BICUBIC",
        interactive_movement_interpolation_method="BICUBIC",
    )
    base.update(kw)
    return RenderConfig(**base)


def test_setting_the_main_method_moves_the_movement_knobs_with_it():
    reduced = ImageRenderConfigReducer.reduce(
        _config(), SetInterpolationMethodAction(method="NEAREST")
    )
    assert reduced.interpolation_method == "NEAREST"
    assert reduced.movement_interpolation_method == "NEAREST"
    assert reduced.interactive_movement_interpolation_method == "NEAREST"
    # the zoom filter is a different gesture and stays independent
    assert reduced.zoom_interpolation_method == "BILINEAR"
