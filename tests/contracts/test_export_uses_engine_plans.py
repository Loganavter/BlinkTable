"""S3: IC export plan-build resolves LOD/tiles through the R4 engine vocabulary.

Pins the Phase S-c seam (task-engine-next-migration-2026-09-16, ruling 3:
parity by construction, harness as tripwire):

* ``snapshot_render_plan_builder`` imports the engine tile vocabulary
  (``engine.cache.draw_plans.plan_tiles_for_grid``) and enumerates export
  tiles through it -- never through a tab-local ``visible_tiles`` call.
* LOD is picked through exactly one wrapper
  (``resolve_export_lod_level``) over ``engine.cache.lod.demand_level``;
  the single ``demand_level`` call site in the module is that wrapper.
* ``snapshot_lod_tiles_for_sources`` is a thin harness-compat delegate over
  the same engine-backed core (``resolve_export_snapshot_lod_tiles``) --
  no second implementation (ruling 2).
* ``SnapshotRenderPlanBuilder.build_render_plan`` exercises that core, so
  export LOD/tile decisions come from the R4 vocabulary by construction;
  ``test_live_export_parity.py`` stays the tripwire (kept green, never
  weakened here).
* rg invariant: tab-local export LOD/tile decision code is zero outside an
  explicit allow-list. Prescale/resample + pure-geometry helpers
  (``prescale_pair``, ``fit_source_to_content``, ``resolve_prescale_target``,
  ...) are out of scope per the harness docstring -- they are named in the
  allow-list below so the invariant stays checkable instead of drifting.

S3 owns this file; S4 extends it only by supervisor ruling. Keep the
engine-call seam minimal: one entry used by export
(``resolve_export_snapshot_lod_tiles``); do not fork the R4 API.
"""

from __future__ import annotations

import ast
import re

from ._framework import ROOT, parse, read

BUILDER = (
    ROOT
    / "src"
    / "tabs"
    / "image_compare"
    / "services"
    / "snapshot_render_plan_builder.py"
)

EXPORT_SCOPE_DIRS = (
    ROOT / "src" / "tabs" / "image_compare" / "services" / "image_export",
    ROOT / "src" / "tabs" / "image_compare" / "services" / "video_snapshot_rendering",
)

# The one engine-call seam used by export (S3): LOD wrapper + LOD/tile core +
# harness-compat delegate. New decision homes must not appear beside these.
_SEAM_NAMES = (
    "resolve_export_lod_level",
    "resolve_export_snapshot_lod_tiles",
    "snapshot_lod_tiles_for_sources",
)

# LOD/tile decision homes that must never be (re-)defined tab-side: the R4
# engine vocabulary plus the live LOD resolver (owned by
# ``rhi_renderer/residency.py``) and raw tile-service enumeration (owned by
# ``plan_tiles_for_grid`` after S3). Mirrors
# ``test_no_tab_draw_plan_math._TAB_FORBIDDEN_DEFS`` plus the S3 additions.
_FORBIDDEN_DEFS = frozenset(
    {
        "to_common_space",
        "rects_overlap",
        "intersection_rect",
        "covered_fraction",
        "slot_visible_rect",
        "plan_tiles_for_grid",
        "build_slot_plan_tiles",
        "best_overlap_tile_index",
        "content_scale_for",
        "build_compare_pair_plan",
        "_array_side_tiles",
        "_resolve_diff_tile_index",
        "_best_diff_tile_index",
        "_content_scale",
        "select_level",
        "visible_tiles",
        "resolve_lod_texture_keys",
    }
)

# Geometry/coverage calls that must not appear anywhere in export scope
# (checked as AST call attributes/names). ``select_level`` /
# ``plan_tiles_for_grid`` are pinned separately (exactly-once wrapper /
# at-least-once core); ``visible_tiles`` must be zero (S3 moved the only
# call site into the engine builder).
_FORBIDDEN_CALLS = frozenset(
    {
        "to_common_space",
        "rects_overlap",
        "intersection_rect",
        "covered_fraction",
        "_covered_fraction",
        "best_tile_overlap_index",
        "_apron_rect",
        "_visible_side_image_rect",
        "visible_tiles",
    }
)

# Prescale/resample + pure-geometry helpers: explicitly out of scope
# (``prescale_pair`` / ``fit_source_to_content`` stay; CPU-prescale untouched
# per harness docstring). Named here so the rg invariant is checkable: any
# prescale/resample/downscale identifier outside this list fails the suite
# and forces a deliberate allow-list update.
_PRESCALE_RESAMPLE_ALLOW_LIST = frozenset(
    {
        # Frozen from AST ground truth (Name/Attribute/FunctionDef ids
        # matching /prescale|resample|downscale/i across export scope).
        "_downscale_for_preview",
        "_resample_for_method",
        "_resolve_prescale_target",
        "downscale_source_to_pil",
        "get_resample",
        "prescale_pair",
        "prescale_started",
        "prescale_target",
        "prescaled",
        "resample",
        "resolve_prescale_target",
        "write_resampled_to_store",
        # Task-named out-of-scope helpers (no /prescale|resample|downscale/
        # token, so the scan below never sees them -- listed for audit).
        "fit_source_to_content",
        "resize_images_processor",
        "scale_global_bounds",
        "resolve_scaled_content_geometry",
        "geometry_with_aspect_insets",
    }
)

# Exact decision-vocabulary footprint across export scope (AST call names and
# defined names matching /lod|tile|resample|prescale|pyramid|level|visible|grid/
# frozen from ground truth). A new decision-ish name anywhere in export scope
# fails here and forces a deliberate update -- the checkable rg invariant.
_KNOWN_SUSPICIOUS_CALLS = frozenset(
    {
        "LevelKey",
        "SnapshotLodTiles",
        "TileTextureService",
        "_resample_for_method",
        "get_resample",
        "level",
        "plan_tiles_for_grid",
        "prescale_pair",
        "pyramid_for",
        "resolve_export_lod_level",
        "resolve_export_snapshot_lod_tiles",
        "resolve_prescale_target",
        "demand_level",
        "write_resampled_to_store",
        # Phase 1 of the strip atlas (plan-video-strip-atlas-2026-10-30): the
        # strip window packs ONE tile per visible block — letterbox, uniform,
        # instance, divider and the production upload call. That is per-tile
        # render packing, not export LOD/tile *decision* math (which stays in
        # the engine, pinned by the tests above); the names are listed so the
        # export-scope vocabulary stays exactly known instead of drifting.
        "StripTile",
        "_ensure_tile_array",
        "_ensure_tile_buffers",
        "_placeholder_tile",
        "_stage_tile_uniforms",
        "_tile_sides",
        "divider_spec_for_tile",
        "grid_for",
        "pack_tile_divider",
        "pack_tile_instance",
        "pack_tile_uniforms",
        "scaled_tile_rects",
        "tile_letterbox_geometry",
        "tile_rect",
        "tile_source_key",
        "upload_tile_to_array",
        # Level model of the strip (strip-zoom-levels, 2026-11-04): a level is
        # ``(tile_w, tile_h, dpr)`` — the strip's own tile geometry identity and
        # its ahead-of-time plan cache. Listed for the same reason as the strip
        # packing names above: it is per-block render planning, NOT the export
        # tile/LOD decision math those names are pinned against.
        "LevelPreparation",
        "StripLevel",
        "_plan_tile",
        "level_layout",
        "strip_level",
    }
)

_KNOWN_SUSPICIOUS_DEFS = frozenset(
    {
        "SnapshotLodTiles",
        "_resample_for_method",
        "resolve_export_lod_level",
        "resolve_export_snapshot_lod_tiles",
        "resolve_prescale_target",
        "snapshot_lod_tiles_for_sources",
        # Strip-window packing vocabulary (see the call set): strip tiles are
        # per-block render packing, not a second export tile/LOD planner.
        "StripTile",
        "StripTileCache",
        "_ensure_tile_buffers",
        "_placeholder_tile",
        "_stage_tile_uniforms",
        "_tile_sides",
        "divider_spec_for_tile",
        "grid_for",
        "pack_tile_divider",
        "pack_tile_instance",
        "pack_tile_uniforms",
        "placeholder_tiles",
        "scaled_tile_rects",
        "tile_letterbox_geometry",
        "tile_rect",
        "tile_source_key",
        # Strip level model (see the call set): geometry identity + its caches.
        "LevelPreparation",
        "StripLevel",
        "_plan_tile",
        "drop_level",
        "has_level",
        "level_for_layout",
        "level_layout",
        "level_tiles",
        "levels",
        "next_level",
        "prepare_level",
        "strip_level",
        "tile_size",
    }
)

_SUSPICIOUS_RE = re.compile(
    r"lod|tile|resample|prescale|pyramid|level|visible|grid", re.I
)
_PRESCALE_RE = re.compile(r"prescale|resample|downscale", re.I)


def _parse(path):
    tree = parse(path)
    if tree is None:
        raise SyntaxError(f"cannot parse {path}")
    return tree


def _scope_files():
    files = [BUILDER]
    for directory in EXPORT_SCOPE_DIRS:
        files.extend(sorted(p for p in directory.glob("*.py") if p.is_file()))
    return files


def _call_names_in(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _defined_names(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _function_node(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == name
        ):
            return node
    return None


def _function_segment(path, func_name: str) -> str:
    text = read(path)
    tree = parse(path)
    assert tree is not None, f"unparsable {path}"
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func_name
        ):
            seg = ast.get_source_segment(text, node)
            assert seg is not None, f"{path}:{node.lineno} no source segment"
            return seg
    raise AssertionError(f"{path}: no function {func_name!r}")


def _imported_from(tree: ast.Module, module: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(a.asname or a.name for a in node.names)
    return names


def _enclosing_function(tree: ast.Module, target: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(child is target for child in ast.walk(node)):
                return node.name
    return None


# ---------------------------------------------------------------------------
# Engine-call edge: builder -> engine.cache.draw_plans
# ---------------------------------------------------------------------------


def test_builder_imports_engine_tile_vocabulary():
    tree = _parse(BUILDER)
    names = _imported_from(tree, "engine.cache.draw_plans")
    assert "plan_tiles_for_grid" in names, (
        "snapshot_render_plan_builder must import plan_tiles_for_grid from "
        "engine.cache.draw_plans (the R4 tile-enumeration vocabulary)"
    )


def test_seam_signatures_are_pinned():
    tree = _parse(BUILDER)
    wrapper = _function_node(tree, "resolve_export_lod_level")
    assert wrapper is not None, "missing resolve_export_lod_level wrapper"
    assert [a.arg for a in wrapper.args.args] == [
        "dest_scale",
        "shared_level_count",
    ], "resolve_export_lod_level signature drift (engine select_level shape)"
    for name in ("resolve_export_snapshot_lod_tiles", "snapshot_lod_tiles_for_sources"):
        func = _function_node(tree, name)
        assert func is not None, f"missing {name}"
        assert {a.arg for a in func.args.kwonlyargs} == {
            "canvas_size_px",
            "zoom",
            "letterboxes",
            "texture_keys",
            "image_space_rect",
            "tile_service",
        }, f"{name} kwonly drift (harness-compat shape)"
        assert "mode" not in {a.arg for a in func.args.kwonlyargs}, (
            f"{name} grew a mode argument (no `if mode == ...` branches)"
        )


def test_demand_level_called_only_through_wrapper():
    tree = _parse(BUILDER)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else (func.attr if isinstance(func, ast.Attribute) else None)
        )
        if name == "demand_level":
            sites.append(_enclosing_function(tree, node))
    assert len(sites) == 1 and sites[0] == "resolve_export_lod_level", (
        f"demand_level must have exactly one call site (the wrapper), "
        f"got {sites}"
    )
    seg = _function_segment(BUILDER, "resolve_export_lod_level")
    assert "demand_level(" in seg, "wrapper must call engine demand_level"


def test_tile_enumeration_goes_through_engine():
    tree = _parse(BUILDER)
    assert "plan_tiles_for_grid" in _call_names_in(tree), (
        "builder never calls plan_tiles_for_grid (tile enumeration must come "
        "from the R4 vocabulary)"
    )
    seg = _function_segment(BUILDER, "resolve_export_snapshot_lod_tiles")
    assert "plan_tiles_for_grid(" in seg, (
        "the export LOD/tile core must enumerate through plan_tiles_for_grid"
    )


def test_helper_is_thin_delegate_no_second_implementation():
    seg = _function_segment(BUILDER, "snapshot_lod_tiles_for_sources")
    assert "resolve_export_snapshot_lod_tiles(" in seg, (
        "snapshot_lod_tiles_for_sources must delegate to the engine-backed core"
    )
    for smuggled in (
        "select_level(",
        "demand_level(",
        "visible_tiles(",
        "plan_tiles_for_grid(",
        "register_source(",
        "LevelKey(",
    ):
        assert smuggled not in seg, (
            f"snapshot_lod_tiles_for_sources inlines {smuggled!r} -- second "
            f"implementation (ruling 2); the core owns all decision math"
        )


def test_build_render_plan_calls_engine_entry():
    seg = _function_segment(BUILDER, "build_render_plan")
    assert "resolve_export_snapshot_lod_tiles(" in seg, (
        "SnapshotRenderPlanBuilder.build_render_plan must call the engine "
        "LOD/tile entry (parity by construction, Phase S-c)"
    )


# ---------------------------------------------------------------------------
# No second implementations + checkable rg invariant across export scope
# ---------------------------------------------------------------------------


def test_no_second_decision_definitions_in_export_scope():
    offenders: list[str] = []
    for path in _scope_files():
        defined = _defined_names(_parse(path))
        clash = defined & _FORBIDDEN_DEFS
        if clash:
            offenders.append(f"{path.name}: {sorted(clash)}")
    assert not offenders, (
        "export scope re-defines engine/live decision homes:\n" + "\n".join(offenders)
    )


def test_no_forbidden_decision_calls_in_export_scope():
    offenders: list[str] = []
    for path in _scope_files():
        calls = _call_names_in(_parse(path))
        clash = calls & _FORBIDDEN_CALLS
        if clash:
            offenders.append(f"{path.name}: {sorted(clash)}")
    assert not offenders, (
        "export scope calls tab-local decision math:\n" + "\n".join(offenders)
    )


def test_export_scope_decision_vocabulary_is_exactly_known():
    actual_calls: set[str] = set()
    actual_defs: set[str] = set()
    for path in _scope_files():
        tree = _parse(path)
        actual_calls.update(
            name for name in _call_names_in(tree) if _SUSPICIOUS_RE.search(name)
        )
        actual_defs.update(
            name for name in _defined_names(tree) if _SUSPICIOUS_RE.search(name)
        )
    assert actual_calls == _KNOWN_SUSPICIOUS_CALLS, (
        "export-scope decision call vocabulary drifted:\n"
        f"  added: {sorted(actual_calls - _KNOWN_SUSPICIOUS_CALLS)}\n"
        f"  removed: {sorted(_KNOWN_SUSPICIOUS_CALLS - actual_calls)}\n"
        "new LOD/tile names need a deliberate contract update"
    )
    assert actual_defs == _KNOWN_SUSPICIOUS_DEFS, (
        "export-scope decision definition vocabulary drifted:\n"
        f"  added: {sorted(actual_defs - _KNOWN_SUSPICIOUS_DEFS)}\n"
        f"  removed: {sorted(_KNOWN_SUSPICIOUS_DEFS - actual_defs)}"
    )


def test_prescale_resample_helpers_stay_allow_listed():
    offenders: list[str] = []
    for path in _scope_files():
        tree = _parse(path)
        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, ast.Name):
                candidates.append(node.id)
            elif isinstance(node, ast.Attribute):
                candidates.append(node.attr)
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                candidates.append(node.name)
            for name in candidates:
                if _PRESCALE_RE.search(name) and name not in (
                    _PRESCALE_RESAMPLE_ALLOW_LIST
                ):
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, (
        "non-allow-listed prescale/resample identifiers in export scope:\n"
        + "\n".join(sorted(set(offenders)))
        + "\n(prescale/resample stays CPU-side per harness; allow-list additions "
        "need a deliberate contract update)"
    )


# ---------------------------------------------------------------------------
# S4 (additive, supervisor ruling §12): MC grid export onto the R4 engine
# tile vocabulary. S0 verdict: MC export builds no plan object of its own
# (save_composite = pure PIL encode; MultiCompareGpuExporter wraps the
# caller-supplied CompositionPlan built upstream by the single-owner shared
# live/export constructor build_composition_plan, boundary-pinned by
# test_plan_application_boundary and NOT touched here); per-slot tile
# enumeration already lives in scene/tile_geometry.py (S2/S5 scope,
# engine-pattern-owned) and export framebuffer tiling parity is already by
# construction (engine TiledFramebufferExporter + offscreen_canvas).
#
# The S3 mirror inside S4 scope (services/gpu_export.py only): a single
# engine-backed entry (resolve_mc_export_slot_tiles) enumerating each
# layer's whole-image tile set through plan_tiles_for_grid, exercised per
# render by render_to_qimage as a tripwire probe (records, never alters).
# These tests are ADDITIVE ONLY -- no S3 test above is modified.
# ---------------------------------------------------------------------------

MC_GPU_EXPORT = (
    ROOT
    / "src"
    / "tabs"
    / "multi_compare"
    / "services"
    / "gpu_export.py"
)

_MC_SEAM_NAME = "resolve_mc_export_slot_tiles"

# Size adaptation only (_export_layer_size, _iter_export_layers): no tile
# math may live beside the seam. Mirrors the S3 _FORBIDDEN_DEFS shape for
# the MC export scope.
_MC_FORBIDDEN_DEFS = frozenset(
    {
        "to_common_space",
        "rects_overlap",
        "intersection_rect",
        "covered_fraction",
        "slot_visible_rect",
        "plan_tiles_for_grid",
        "build_slot_plan_tiles",
        "best_overlap_tile_index",
        "content_scale_for",
        "build_compare_pair_plan",
        "select_level",
        "visible_tiles",
        "resolve_lod_texture_keys",
    }
)


def _mc_parse():
    return ast.parse(read(MC_GPU_EXPORT))


def test_mc_exporter_imports_engine_tile_vocabulary():
    tree = _mc_parse()
    names = _imported_from(tree, "engine.cache.draw_plans")
    assert "plan_tiles_for_grid" in names, (
        "multi_compare gpu_export must import plan_tiles_for_grid from "
        "engine.cache.draw_plans (the R4 tile-enumeration vocabulary)"
    )


def test_mc_export_entry_enumerates_through_engine():
    tree = _mc_parse()
    assert "plan_tiles_for_grid" in _call_names_in(tree), (
        "gpu_export never calls plan_tiles_for_grid (MC export tile "
        "enumeration must come from the R4 vocabulary)"
    )
    seg = _function_segment(MC_GPU_EXPORT, _MC_SEAM_NAME)
    assert "plan_tiles_for_grid(" in seg, (
        "the MC export tile entry must enumerate through plan_tiles_for_grid"
    )
    for smuggled in (
        "select_level(",
        "visible_tiles(",
        "LevelKey(",
        "_apron_rect(",
        "build_slot_plan_tiles(",
    ):
        assert smuggled not in seg, (
            f"resolve_mc_export_slot_tiles inlines {smuggled!r} -- second "
            f"implementation (ruling 2); LOD/viewport math lives elsewhere"
        )


def test_mc_render_to_qimage_calls_engine_entry():
    seg = _function_segment(MC_GPU_EXPORT, "render_to_qimage")
    assert f"{_MC_SEAM_NAME}(" in seg, (
        "MultiCompareGpuExporter.render_to_qimage must call the engine "
        "tile entry (parity by construction, Phase S-c)"
    )


def test_mc_export_defines_no_second_decision_math():
    defined = _defined_names(_mc_parse())
    clash = defined & _MC_FORBIDDEN_DEFS
    assert not clash, (
        f"multi_compare gpu_export re-defines engine decision homes: "
        f"{sorted(clash)} (ruling 2: no second implementations)"
    )


# ---------------------------------------------------------------------------
# R4 (additive, task-engine-leftovers-2026-09-16 Phase R-d, ruling 4):
# the S3/S4 probes are the PERMANENT seam, not a tripwire. Consumption
# (probe output feeding export-first-frame residency) was attempted and
# ruled infeasible: the live entry points take no authoritative-tile
# parameter and the export first frame is a single budget-limited grab --
# see the evidence comments in snapshot_render_plan_builder.py
# (build_render_plan) and multi_compare gpu_export.py (module header).
# These tests pin that verdict mechanically:
# * each probe result is record-only (bound once, logged once, never
#   passed into plan construction / the render below);
# * the live entry points still take no authoritative-tile parameter, so
#   a half-wired consumption cannot sneak in without failing here first.
# Probe==live equality itself stays pinned by
# src/tabs/image_compare/tests/render/test_live_export_parity.py (the
# SNAPSHOTS matrix), not duplicated here. ADDITIVE ONLY -- no S3/S4 test
# above is modified.
# ---------------------------------------------------------------------------

IC_RESIDENCY = (
    ROOT
    / "src"
    / "tabs"
    / "image_compare"
    / "canvas"
    / "rhi_renderer"
    / "residency.py"
)

MC_RESIDENCY = (
    ROOT / "src" / "tabs" / "multi_compare" / "scene" / "passes" / "residency.py"
)

# Parameter names that would carry an authoritative tile set into residency.
_AUTHORITATIVE_PARAM_RE = re.compile(
    r"authoritative|tile_set|tileset|pre_reg|prereg", re.I
)


def _all_arg_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = func.args
    names = [a.arg for a in list(args.args) + list(args.kwonlyargs)]
    if args.vararg is not None:
        names.append(args.vararg.arg)
    if args.kwarg is not None:
        names.append(args.kwarg.arg)
    return names


def test_r4_ic_probe_result_is_record_only():
    seg = _function_segment(BUILDER, "build_render_plan")
    assert "resolve_export_snapshot_lod_tiles(" in seg
    assert seg.count("_export_lod_probe") == 3, (
        "build_render_plan must only bind the probe (assignment) and log it "
        "(logger.debug levels + tile_sets) -- any further use is "
        "consumption, not a seam"
    )
    idx = seg.find("plan = build_canvas_plan(")
    assert idx != -1, "build_render_plan must still build via build_canvas_plan"
    assert "_export_lod_probe" not in seg[idx:], (
        "the probe result must never flow into build_canvas_plan (record "
        "only -- R4 pinned seam)"
    )


def test_r4_mc_probe_result_is_record_only():
    seg = _function_segment(MC_GPU_EXPORT, "render_to_qimage")
    assert f"{_MC_SEAM_NAME}(" in seg
    assert seg.count("_export_tile_probe") == 3, (
        "render_to_qimage must only bind the probe (assignment) and log it "
        "(logger.debug slots + tile counts) -- any further use is "
        "consumption, not a seam"
    )
    idx = seg.find("def prepare_frame")
    assert idx != -1, "render_to_qimage must still render via prepare_frame"
    assert "_export_tile_probe" not in seg[idx:], (
        "the probe result must never flow into the render below (record "
        "only -- R4 pinned seam)"
    )


def test_r4_live_entry_points_take_no_authoritative_tiles():
    offenders: list[str] = []
    ic_tree = parse(IC_RESIDENCY)
    assert ic_tree is not None, f"unparsable {IC_RESIDENCY}"
    for node in ast.walk(ic_tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "realize_tile_plan"
        ):
            bad = [
                name
                for name in _all_arg_names(node)
                if _AUTHORITATIVE_PARAM_RE.search(name)
            ]
            if bad:
                offenders.append(f"IC realize_tile_plan: {bad}")
    mc_tree = parse(MC_RESIDENCY)
    assert mc_tree is not None, f"unparsable {MC_RESIDENCY}"
    for node in ast.walk(mc_tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "realize"
        ):
            bad = [
                name
                for name in _all_arg_names(node)
                if _AUTHORITATIVE_PARAM_RE.search(name)
            ]
            if bad:
                offenders.append(f"MC realize: {bad}")
    assert not offenders, (
        "live residency entry points grew authoritative-tile parameters:\n"
        + "\n".join(offenders)
        + "\n(consumption needs live-path changes -- out of R4 scope; "
        "update the R4 verdict deliberately, not silently)"
    )
