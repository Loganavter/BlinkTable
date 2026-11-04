"""No ``setattr``-based Store bypass outside Dispatcher/Reducer.

Covers ``docs/dev/CONTRACTS.md`` / ``docs/dev/STORE.md`` invariants 1-2::

    Action ──► Dispatcher.dispatch ──► RootReducer.reduce (replace) ──► Store

Sibling ``test_no_direct_store_mutation.py`` catches only ``store.x.y = v``
``Assign`` syntax. This dogma closes the ``ast.Call`` hole: several call
sites spell the same in-place mutation as ``setattr(store_obj, ...,
value)`` — e.g. ``session_persistence.py`` comments admit using
``setattr`` "to avoid Assign dogma". The effect is identical (lock /
history / subscribers / session re-pointing skipped), so it fails the
same way.

Two shapes are flagged for ``setattr(target, name, value)``:

* (A) the first-arg chain contains an ``INTERMEDIATE`` Store-owned attr
  (``session_data`` / ``image_state`` / ``render_cache`` / ``document`` /
  ``viewport`` / ``render_config`` / ``view_state`` / ...), or
* (B) the attribute *name* literal itself is an ``INTERMEDIATE`` member —
  this covers the ``setattr(session, "document", doc)`` fallback shape in
  ``use_cases/persistence.py`` whose first-arg chain is a bare ``session``
  root and would otherwise be invisible.

Helpers (``INTERMEDIATE``, ``EXEMPT``, ``_get_chain``,
``_collect_transients``) are reused by import from the sibling module —
not fork-duplicated — so the two dogmas cannot drift apart.

RATCHET: ``ALLOWLIST`` pins every pre-existing site found on the current
tree; ``PENDING_MIGRATION`` tolerates the persistence-path offenders that
are being fixed in parallel. A new ``setattr`` bypass fails; a stale
``PENDING_MIGRATION`` entry (site already migrated) fails too, forcing
exemption cleanup at integration. ``ALLOWLIST`` is checked for staleness
the same way — an entry that no longer matches a live bypass fails, so the
exemption lists can only shrink (see the note at ``test_no_setattr_store_bypass``).

Known blind spot (documented, not scanned): ``setattr`` whose target is
itself a ``Call`` (``_get_chain`` returns ``None``), e.g.
``setattr(controller.store.get_session_state_slot('document'), ...)`` in
``use_cases/slot.py:322`` and ``setattr(get_magnifier_widget_state(...),
...)`` in ``magnifier/properties.py``. Extending the scan there is
supervisor follow-up work.
"""

from __future__ import annotations

import ast

from ._framework import SRC, iter_py, parse, rel
from .test_no_direct_store_mutation import (
    EXEMPT,
    INTERMEDIATE,
    _collect_transients,
    _get_chain,
)

# Pre-existing sites on the current tree — each verified by running the
# scan below. Do not extend without a migration ticket.
ALLOWLIST: frozenset[tuple[str, int, str]] = frozenset(
    {
        # -- settings presenter fallbacks (dispatch-failure path) --
        ("src/plugins/settings/presenter.py", 27, "self.view_state"),
        ("src/plugins/settings/presenter_parts/view_state.py", 44, "self.store.viewport.render_config"),
        ("src/plugins/settings/presenter_parts/view_state.py", 48, "self.store.viewport.render_config"),
        # -- image_compare transient interpolation menu fallbacks --
        # Migrated by task-transient-interp-setattr-2026-10-28: both fallbacks
        # (invalid-value repair + combo-less pick) now go through
        # Dispatcher.dispatch(SetInterpolationMethodAction) and drop the write
        # with a log line when no dispatcher is available.
        # -- canvas feature-state snapshot/projection writes (view_state param) --
        ("src/tabs/image_compare/canvas/features/capture/state/feature_state.py", 28, "view_state"),
        ("src/tabs/image_compare/canvas/features/capture/widget.py", 102, "view_state"),
        ("src/tabs/image_compare/canvas/features/capture/widget.py", 130, "snap.viewport_state.view_state"),
        ("src/tabs/image_compare/canvas/features/divider/properties.py", 19, "view_state"),
        ("src/tabs/image_compare/canvas/features/divider/properties.py", 24, "view_state"),
        ("src/tabs/image_compare/canvas/features/divider/properties.py", 25, "view_state"),
        ("src/tabs/image_compare/canvas/features/divider/properties.py", 76, "snap.viewport_state.view_state"),
        ("src/tabs/image_compare/canvas/features/divider/state/feature_state.py", 30, "view_state"),
        ("src/tabs/image_compare/canvas/features/guides/properties.py", 19, "view_state"),
        ("src/tabs/image_compare/canvas/features/guides/state/feature_state.py", 33, "view_state"),
        ("src/tabs/image_compare/canvas/features/magnifier/persistence.py", 216, "view_state"),
        ("src/tabs/image_compare/canvas/features/magnifier/state/feature_state.py", 75, "view_state"),
        ("src/tabs/image_compare/canvas/features/magnifier/state/store.py", 130, "view_state"),
        # -- filename_overlay snapshot render_config writes --
        ("src/tabs/image_compare/canvas/features/filename_overlay/properties.py", 36, "snap.viewport_state.render_config"),
        ("src/tabs/image_compare/canvas/features/filename_overlay/properties.py", 56, "snap.viewport_state.render_config"),
        ("src/tabs/image_compare/canvas/features/filename_overlay/properties.py", 76, "snap.viewport_state.render_config"),
        ("src/tabs/image_compare/canvas/features/filename_overlay/properties.py", 96, "snap.viewport_state.render_config"),
        ("src/tabs/image_compare/canvas/features/filename_overlay/properties.py", 117, "snap.viewport_state.render_config"),
        ("src/tabs/image_compare/canvas/features/filename_overlay/properties.py", 138, "snap.viewport_state.render_config"),
        ("src/tabs/image_compare/canvas/features/filename_overlay/properties.py", 156, "snap.viewport_state.render_config"),
        ("src/tabs/image_compare/canvas/features/filename_overlay/properties.py", 174, "snap.viewport_state.render_config"),
        ("src/tabs/image_compare/canvas/features/filename_overlay/properties.py", 194, "snap.viewport_state.render_config"),
        # -- magnifier geometry scratch (overlay screen center/size) --
        # NOTE: lines shifted 205/206 → 221/223 by the source-side no-op
        # dispatch gate (task-noop-dispatch-guards-2026-09-16); same
        # dispatch-failure fallback sites, now individually gated on value
        # change, not new bypasses.
        ("src/tabs/image_compare/canvas/features/magnifier/scene/apply.py", 221, "geometry_state"),
        ("src/tabs/image_compare/canvas/features/magnifier/scene/apply.py", 223, "geometry_state"),
        # -- magnifier worker cache writes --
        ("src/tabs/image_compare/canvas/features/magnifier/workers/diff_cache.py", 143, "presenter.store.viewport.session_data.render_cache"),
        ("src/tabs/image_compare/canvas/features/magnifier/workers/diff_cache.py", 148, "presenter.store.viewport.session_data.render_cache"),
        ("src/tabs/image_compare/canvas/features/magnifier/workers/diff_cache.py", 152, "presenter.store.viewport.session_data.render_cache"),
        ("src/tabs/image_compare/canvas/features/magnifier/workers/result_handlers.py", 29, "presenter.store.viewport.interaction_state"),
        ("src/tabs/image_compare/canvas/features/magnifier/workers/result_handlers.py", 33, "presenter.store.viewport.interaction_state"),
        # -- presentation geometry scratch --
        # NOTE: lines shifted 313-315 → 50-52 by the C3 display-tier wave
        # (sync_geometry_state moved to engine.cache.display_tier_service;
        # same dispatch-failure fallback sites, now in the tab-surface
        # _commit_geometry_attrs hook impl, not new bypasses).
        ("src/tabs/image_compare/canvas/presentation/plan_applicator.py", 50, "geometry_state"),
        ("src/tabs/image_compare/canvas/presentation/plan_applicator.py", 51, "geometry_state"),
        ("src/tabs/image_compare/canvas/presentation/plan_applicator.py", 52, "geometry_state"),
        ("src/tabs/image_compare/canvas/presentation/snapshot_frame.py", 55, "_store.viewport.geometry_state"),
        ("src/tabs/image_compare/canvas/presentation/snapshot_frame.py", 56, "_store.viewport.geometry_state"),
        ("src/tabs/image_compare/canvas/presentation/snapshot_frame.py", 61, "_store.viewport.geometry_state"),
        ("src/tabs/image_compare/canvas/presentation/snapshot_frame.py", 62, "_store.viewport.geometry_state"),
        # -- video keyframe engine: transient start.clone() interpolation scratch --
        ("src/tabs/image_compare/plugins/video_editor/services/keyframing/engine/values.py", 459, "interpolated.render_config"),
        ("src/tabs/image_compare/plugins/video_editor/services/keyframing/engine/values.py", 460, "interpolated.view_state"),
        # -- analysis cache / metric writes --
        # NOTE: lines shifted by the W3c box-aware analysis wave
        # (get_crop_service injection + box plumbing in cached_diff/metrics;
        # same fallback sites, not new bypasses).
        ("src/tabs/image_compare/services/analysis/cached_diff.py", 37, "render_cache"),
        ("src/tabs/image_compare/services/analysis/cached_diff.py", 42, "render_cache"),
        ("src/tabs/image_compare/services/analysis/cached_diff.py", 55, "self.store.viewport.session_data.render_cache"),
        ("src/tabs/image_compare/services/analysis/metrics.py", 109, "self.store.viewport.session_data.image_state"),
        ("src/tabs/image_compare/services/analysis/metrics.py", 123, "self.store.viewport.session_data.image_state"),
        # NOTE: lines shifted 355/357 → 359/361 by the S3 engine-next export
        # seam (logging import + LOD-probe call in build_render_plan; same
        # cached_diff fallback sites, not new bypasses), then back
        # 359/361 → 355/357 by the D2 dead-code sweep (duplicate
        # query_active_magnifier_divider_thickness body removed).
        ("src/tabs/image_compare/services/snapshot_render_plan_builder.py", 356, "self.store.viewport.session_data.render_cache"),
        ("src/tabs/image_compare/services/snapshot_render_plan_builder.py", 358, "self.store.viewport.session_data.render_cache"),
        # -- UI settings persistence writes --
        ("src/tabs/image_compare/ui/settings_persistence.py", 68, "image_state"),
        ("src/tabs/image_compare/ui/settings_persistence.py", 69, "image_state"),
        # transient_interpolation.py now has ZERO setattr bypasses — both
        # fallbacks dispatch SetInterpolationMethodAction and drop the write
        # when no dispatcher exists (task-transient-interp-setattr-2026-10-28);
        # the former 115/121/229/240 entries were swept as dead.
        ("src/tabs/image_compare/use_cases/navigation.py", 140, "controller.store.viewport.render_config"),
        # slot.py's current-index/document writes now dispatch too; the former
        # 412/456 `setattr(document, ...)` entries were swept as dead.
        # -- unified list picker document list writes --
        ("src/ui/widgets/unified_list_picker/common.py", 246, "document"),
        ("src/ui/widgets/unified_list_picker/common.py", 248, "document"),
        ("src/ui/widgets/unified_list_picker/common.py", 252, "document"),
        ("src/ui/widgets/unified_list_picker/common.py", 254, "document"),
    }
)

# Persistence-path offender not yet migrated — tolerated for now.
# Delete the entry (not the scan) once the site migrates to dispatch.
# (session_persistence.py sites were migrated by the dispatch-or-defer
# restore; use_cases/persistence.py widget IO was removed with it.)
# NOTE: line shifted 254 → 265 by the W5 crop_override persist fields
# (same _write_document fallback site, not a migration).
PENDING_MIGRATION: frozenset[tuple[str, int, str]] = frozenset(
    {
        ("src/tabs/image_compare/use_cases/persistence.py", 266, "session.document"),
    }
)


def _setattr_bypasses() -> set[tuple[str, int, str]]:
    """All ``setattr`` calls whose target is Store-owned state."""
    found: set[tuple[str, int, str]] = set()
    for path in iter_py(SRC):
        rel_path = rel(path)
        if rel_path in EXEMPT:
            continue
        tree = parse(path)
        if tree is None:
            continue
        transients = _collect_transients(tree, path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if not (
                (isinstance(func, ast.Name) and func.id == "setattr")
                or (isinstance(func, ast.Attribute) and func.attr == "setattr")
            ):
                continue
            chain = _get_chain(node.args[0])
            label: str | None = None
            if chain is not None and any(part in INTERMEDIATE for part in chain):
                label = ".".join(chain)
            elif (
                len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in INTERMEDIATE
                and chain is not None
            ):
                # setattr(session, "document", doc) shape — bare root, Store-owned name.
                label = ".".join([*chain, node.args[1].value])
            if label is None:
                continue
            if chain is not None and chain[0] in transients:
                continue
            found.add((rel_path, node.lineno, label))
    return found


def test_no_setattr_store_bypass():
    found = _setattr_bypasses()
    allowed = set(ALLOWLIST) | set(PENDING_MIGRATION)
    unexpected = sorted(found - allowed)
    assert not unexpected, (
        "setattr Store bypass outside Dispatcher/Reducer — "
        "use store.get_dispatcher().dispatch(Action, scope=...) → replace() instead "
        f"({len(unexpected)} hits):\n  "
        + "\n  ".join(f"{p}:{ln} — setattr({label}, ...)" for p, ln, label in unexpected)
    )
    stale = sorted(set(PENDING_MIGRATION) - found)
    assert not stale, (
        "Stale PENDING_MIGRATION entries — the site already migrated, "
        f"delete the exemption:\n  "
        + "\n  ".join(f"{p}:{ln} — setattr({label}, ...)" for p, ln, label in stale)
    )
    # Both exemption sets are RATCHET-ONLY-SHRINK: each entry is a debt pinned
    # to one concrete `path:line` on the current tree, and it is only consulted
    # when a bypass is found. Without this check a migrated/renamed site leaves
    # a permanently dead entry that no other test can notice — the list would
    # silently drift away from the real surface and hide a later regression
    # behind an exemption nobody can justify. Dead ⇒ delete the entry (never the
    # scan, never widen the allowlist).
    dead = sorted(set(ALLOWLIST) - found)
    assert not dead, (
        "Dead ALLOWLIST entries — the site no longer produces a setattr Store "
        "bypass (migrated to dispatch, or moved/renamed so the pinned line is "
        "gone). Delete the entry: if the site merely moved, the next run flags "
        "its new path:line as unexpected — re-pin that line, do not widen.\n  "
        + "\n  ".join(f"{p}:{ln} — setattr({label}, ...)" for p, ln, label in dead)
    )
