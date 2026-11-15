"""Data models for multi-compare session — container-tree layout (i3/sway-style)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Union


@dataclass
class CompareSlot:
    """One image slot referenced by a Leaf in the layout tree.

    Path-only ``SlotSource`` (B1, IC ``ImageItem`` parity): pixels live in
    the session-owned :class:`pipeline.cache.MultiComparePixelCache`
    (keyed ``(normpath, mtime, size)`` + crop flag), never here — so Redux
    snapshots (incl. undo) stay small, serializable, and free of closable
    stores.
    ``revision`` bumps once per decoded-tier arrival (``NoteSlotPixels``)
    so tier fills still produce a new state object for subscribers without
    carrying any pixels through the reducer.
    ``crop_override`` mirrors IC ``ImageItem.crop_override``: ``True``/``False``
    force crop on/off for this slot, ``None`` (Auto) follows the global
    ``auto_crop_black_borders`` setting. MC bakes crop at decode (unlike IC
    render-time clip), so a toggle evicts the slot's cache tiers and
    re-decodes (see ``use_cases/crop.py``).
    """

    id: int
    path: Path | None = None
    label: str = ""
    revision: int = 0
    crop_override: bool | None = None


@dataclass
class LeafNode:
    """Holds a single image slot reference."""

    slot_id: int


@dataclass
class SplitNode:
    """A horizontal or vertical split with N children.

    direction:
        "h" — children laid out left-to-right (vertical dividers).
        "v" — children laid out top-to-bottom (horizontal dividers).
    weights: positive floats; effective share = w_i / sum(weights).
    """

    direction: str
    children: list
    weights: list[float]

    def normalized_weights(self) -> list[float]:
        s = sum(self.weights) or 1.0
        return [w / s for w in self.weights]


LayoutNode = Union[LeafNode, SplitNode]


@dataclass(frozen=True)
class MultiCompareLabelSettings:
    """Camera-fixed filename label styling for multi-compare."""

    #: Master switch: ``False`` hides every per-cell filename label (live and
    #: export). Styling below is kept so re-enabling restores the same look.
    visible: bool = True
    font_size_percent: int = 100
    font_weight: int = 0
    text_rgba: tuple[int, int, int, int] = (255, 255, 255, 255)
    bg_rgba: tuple[int, int, int, int] = (0, 0, 0, 255)
    draw_background: bool = True
    text_alpha_percent: int = 100


# Default / safe fallback when unset or alpha is zero — match image_compare /
# domain.qt_adapters.DEFAULT_VISIBLE_COLOR (opaque white).
DEFAULT_DIVIDER_COLOR_RGBA: tuple[int, int, int, int] = (255, 255, 255, 255)


@dataclass(frozen=True)
class MultiCompareDividerSettings:
    """User-controllable divider line styling for multi-compare splits."""

    visible: bool = True
    thickness: int = 4
    color_rgba: tuple[int, int, int, int] = DEFAULT_DIVIDER_COLOR_RGBA


def leaves(node: LayoutNode | None) -> list[LeafNode]:
    if node is None:
        return []
    if isinstance(node, LeafNode):
        return [node]
    out: list[LeafNode] = []
    for child in node.children:
        out.extend(leaves(child))
    return out


def slot_ids_in_tree(node: LayoutNode | None) -> set[int]:
    return {leaf.slot_id for leaf in leaves(node)}


def find_path(node: LayoutNode | None, slot_id: int) -> list[int] | None:
    """Return list of child indices from root down to the leaf, or None."""
    if node is None:
        return None
    if isinstance(node, LeafNode):
        return [] if node.slot_id == slot_id else None
    for i, child in enumerate(node.children):
        sub = find_path(child, slot_id)
        if sub is not None:
            return [i, *sub]
    return None


# NOTE (P2 hygiene): the mutating tree helpers (remove_leaf, swap_slot_ids,
# insert_beside_path, insert_beside) used to live here beside the frozen
# ``MultiCompareState`` — mutating a tree owned by a frozen state is a
# frozen-violation. The pure path is :mod:`tabs.multi_compare.scene.tree_ops`
# (fresh trees, never in-place); this module keeps data + pure readers only.



def node_at_path(node: LayoutNode | None, path: tuple[int, ...]) -> LayoutNode | None:
    cur = node
    for i in path:
        if not isinstance(cur, SplitNode) or i < 0 or i >= len(cur.children):
            return None
        cur = cur.children[i]
    return cur


@dataclass(frozen=True)
class MultiCompareState:
    """State for the multi-compare view.

    Frozen so consumers cannot mutate fields outside the reducer. Inner
    containers (``slots`` list, ``SplitNode.children`` / ``weights``) are
    conventionally treated as immutable too — reducers in
    :mod:`tabs.multi_compare.scene.store` always build new lists / trees via
    :mod:`tabs.multi_compare.scene.tree_ops`.
    """

    slots: list[CompareSlot] = field(default_factory=list)
    root: LayoutNode | None = None
    focused_slot_id: int | None = None
    zoom: float = 1.0
    pan_x: float = 0.0
    pan_y: float = 0.0
    # Scene camera (``scene/projection.py``): zooms the WHOLE composition
    # (every slot cell, divider and label moves together), unlike ``zoom``
    # above which magnifies the image inside one slot cell. Same shader
    # convention as the content camera -- ``screen_uv = (canvas_uv - 0.5 +
    # pan) * zoom + 0.5`` -- with ``zoom >= 1`` and pan clamped to
    # ``|pan| <= (zoom-1)/(2*zoom)`` so the viewport never shows void
    # (``scene.projection.scene_camera`` owns the clamp).
    scene_zoom: float = 1.0
    scene_pan_x: float = 0.0
    scene_pan_y: float = 0.0

    drag_active: bool = False
    drag_target_path: tuple[int, ...] | None = None
    drag_target_side: str | None = None
    drag_target_root: bool = False
    drag_target_swap_slot_id: int | None = None
    drag_internal: bool = False
    drag_source_slot_id: int | None = None
    label_settings: MultiCompareLabelSettings = field(
        default_factory=MultiCompareLabelSettings
    )
    divider_settings: MultiCompareDividerSettings = field(
        default_factory=MultiCompareDividerSettings
    )

    @property
    def is_focused(self) -> bool:
        return self.focused_slot_id is not None

    def slot_count(self) -> int:
        return len(slot_ids_in_tree(self.root))