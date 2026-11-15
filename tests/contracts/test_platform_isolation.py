"""Platform isolation dogma.

The platform (``src/core/``, ``src/ui/`` except ``src/ui/canvas_infra/rhi``
which is the shared QRhi backend, ``src/services/``, ``src/plugins/``,
``src/shared/``, ``src/events/``) MUST NOT mention specific tab names like
``image_compare`` or ``image_session``. Tabs live under ``src/tabs/`` and the
host platform discovers them via :class:`tabs.registry.TabRegistry`.

Allowlist: a small set of compat-bridge files registered here may still
contain the literal until the corresponding migration steps land. New
violations outside the allowlist should fail this test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ._framework import module_imports

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

PLATFORM_ROOTS = (
    SRC / "core",
    SRC / "ui" / "main_window",
    SRC / "ui" / "presenters" / "main_window",
    SRC / "ui" / "presenters" / "image_canvas",
    SRC / "ui" / "canvas_features",
    SRC / "ui" / "canvas_infra",
    SRC / "ui" / "context_menu",
    SRC / "services",
    SRC / "shared",
    SRC / "plugins",
    SRC / "events",
    SRC / "ui" / "helpers",
    SRC / "ui" / "actions",
    SRC / "ui" / "managers",
)

# Widened from 2 literals to full session_type catalog + image_session legacy.
# Session types discovered live via TabRegistry so this cannot drift.
def _forbidden_patterns() -> tuple[re.Pattern, ...]:
    try:
        from tabs.registry import TabRegistry

        registry = TabRegistry()
        registry.discover()
        types = sorted({tab.session_type for tab in registry.list_tabs()})
    except Exception:
        types = ["image_compare", "multi_compare", "session_picker", "image_gallery"]
    # image_session is the legacy name for image_compare's session data.
    all_tokens = types + ["image_session"]
    return tuple(re.compile(rf"\b{re.escape(t)}\b") for t in all_tokens)


FORBIDDEN_PATTERNS = _forbidden_patterns()
FORBIDDEN_TAB_IMPORT_RE = re.compile(
    r"^(?:src\.)?tabs\.(?!(?:contract|registry)\b)[^.]+\b"
)

# Compat-bridge files explicitly tracked in MIGRATION_PLAN.md. Each entry is a
# repo-relative path.
#
# Widened scanner would otherwise flag ~36 legitimate platform-owned uses:
# - core/state_management/dispatcher.py:76 multi_compare action-type constants
#   are the central Redux vocabulary (platform owns the dispatcher, not a tab).
# - plugins/settings/* magnifier gateway — the settings pages own magnifier
#   calibration/border/divider knobs that are host-level feature flags, not
#   image_compare internals (the feature lives on the canvas but its knobs live
#   in the host settings surface). Filtering per-settings-key ownership keeps
#   these legitimate.
# - core/store.py defines INITIAL_WORKSPACE_SESSION_TYPE = "session_picker"
#   (the bootstrap default — platform must name it).
# - shared/* duplication docs and ui/* hub helpers legitimately mention tab
#   names in comments / logging — allowlisted to keep the widened scanner
#   from flagging prose.
ALLOWLIST: frozenset[str] = frozenset(
    {
        "src/core/state_management/dispatcher.py",
        "src/core/state_management/reducers.py",
        "src/core/store.py",
        "src/core/store_workspace.py",
        "src/core/session_blueprints.py",
        "src/plugins/settings/presenter_parts/view_state.py",
        "src/plugins/settings/translations.py",
        "src/plugins/settings/presenter.py",
        "src/plugins/settings/models.py",
        "src/plugins/settings/dialog_context.py",
        "src/plugins/settings/dialog.py",
        "src/engine/cache/residency.py",
        "src/engine/cache/tile_texture_service.py",
        "src/engine/gpu/offscreen_canvas.py",
        "src/engine/cache/uniform_layout.py",
        "src/shared/canvas/keyboard_constants.py",
        "src/shared/image_processing/export_encoding.py",
        "src/shared/image_processing/image_dims.py",
        "src/shared/image_processing/pixel_cache_loader.py",
        "src/services/io/recent_projects.py",
        "src/services/io/project_preview.py",
        "src/services/io/project_io.py",
        "src/plugins/help/dialog.py",
        "src/plugins/settings/pages/keyboard.py",
        "src/plugins/export/plugin.py",
        "src/plugins/settings/registry.py",
        "src/ui/layout_spacing.py",
        "src/ui/widgets/slider_hint.py",
        "src/ui/widgets/scroll_value_button.py",
        "src/ui/canvas_presentation/plan_applicator.py",
        "src/ui/canvas_presentation/composition.py",
        "src/ui/canvas_presentation/filename_labels.py",
        "src/ui/canvas_presentation/plan.py",
        "src/ui/canvas_infra/scene/frame_geometry.py",
        "src/shared/image_processing/tiled_pixel_store.py",
        "src/ui/actions/platform.py",
        "src/ui/actions/registry.py",
        "src/ui/main_window/menu_controller.py",
        "src/ui/main_window/project/recent.py",
        "src/ui/presenters/main_window/connections.py",
        "src/ui/presenters/main_window/presenter.py",
        "src/ui/presenters/main_window/workspace.py",
        "src/ui/presenters/main_window/workspace_tab_menu.py",
        "src/plugins/settings/application_service.py",
        "src/shared_toolkit/ui/managers/ui_resource_manager.py",
    }
)


def _iter_py_files() -> list[Path]:
    files: list[Path] = []
    for root in PLATFORM_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return sorted(files)


@pytest.mark.parametrize(
    "legacy_path",
    (
        SRC / "plugins" / "analysis",
        SRC / "plugins" / "analysis" / "controller.py",
        SRC / "plugins" / "analysis" / "events.py",
        SRC / "plugins" / "analysis" / "plugin.py",
        SRC / "plugins" / "analysis" / "processing",
        SRC / "plugins" / "analysis" / "services",
        SRC / "plugins" / "analysis" / "settings.py",
        SRC / "plugins" / "analysis" / "state.py",
        SRC / "plugins" / "analysis" / "ui",
        SRC / "plugins" / "comparison",
        SRC / "plugins" / "export" / "presenter_parts" / "context_builder.py",
        SRC / "plugins" / "export" / "presenter_parts" / "save_flow.py",
        SRC / "plugins" / "export" / "presenter_parts" / "state.py",
        SRC / "plugins" / "export" / "services" / "image_export.py",
        SRC / "plugins" / "export" / "services" / "gpu_export_scene.py",
        SRC / "plugins" / "export" / "services" / "recording_flow.py",
        SRC / "plugins" / "export" / "services" / "snapshot_render_plan_builder.py",
        SRC / "plugins" / "export" / "services" / "still_snapshot_bounds.py",
        SRC / "plugins" / "export" / "services" / "video_export_flow.py",
        SRC / "plugins" / "export" / "settings.py",
        SRC / "plugins" / "export" / "state.py",
        SRC / "plugins" / "layout" / "definitions.py",
        SRC / "plugins" / "layout" / "manager.py",
        SRC / "plugins" / "settings" / "color_actions.py",
        SRC / "plugins" / "settings" / "presenter_parts" / "color_pickers.py",
        SRC / "services" / "system" / "clipboard.py",
        SRC / "services" / "io" / "image_loader.py",
        SRC / "services" / "workflow",
        SRC / "shared" / "rendering" / "canvas_widget_factory.py",
        SRC / "ui" / "canvas_features",
        SRC / "ui" / "canvas_presentation" / "plan_builder.py",
        SRC / "ui" / "context_menu" / "image_compare.py",
        SRC / "ui" / "presenters" / "image_canvas",
        SRC / "ui" / "widgets" / "gl_canvas",
        SRC / "ui" / "widgets" / "gl_canvas" / "widget.py",
    ),
    ids=lambda p: p.relative_to(ROOT).as_posix(),
)
def test_legacy_image_compare_platform_paths_are_removed(legacy_path: Path):
    assert not legacy_path.exists(), (
        f"{legacy_path.relative_to(ROOT).as_posix()} is image_compare-owned legacy code; "
        "use tabs.image_compare instead"
    )


@pytest.mark.parametrize("py_file", _iter_py_files(), ids=lambda p: str(p.relative_to(ROOT).as_posix()))
def test_platform_file_does_not_mention_image_compare(py_file: Path):
    rel = py_file.relative_to(ROOT).as_posix()
    if rel in ALLOWLIST:
        pytest.skip(f"allowlisted compat bridge: {rel}")
    text = py_file.read_text(encoding="utf-8")
    hits = []
    for pattern in FORBIDDEN_PATTERNS:
        for match in pattern.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            # Skip ALLOWED-marked lines (system-wide generic)
            line = text.splitlines()[line_no - 1] if line_no <= len(text.splitlines()) else ""
            if "ALLOWED" in line:
                continue
            hits.append((line_no, pattern.pattern, match.group(0)))
    assert not hits, (
        f"{rel} mentions tab-specific names — platform code must stay tab-agnostic:\n"
        + "\n".join(f"  line {ln}: pattern {pat!r} matched {tok!r}" for ln, pat, tok in hits)
    )


@pytest.mark.parametrize("py_file", _iter_py_files(), ids=lambda p: str(p.relative_to(ROOT).as_posix()))
def test_platform_file_does_not_import_tab_package_directly(py_file: Path):
    rel = py_file.relative_to(ROOT).as_posix()
    if rel in ALLOWLIST:
        pytest.skip(f"allowlisted compat bridge: {rel}")
    hits = [
        (lineno, module)
        for module, lineno in module_imports(py_file)
        if FORBIDDEN_TAB_IMPORT_RE.match(module)
    ]
    assert not hits, (
        f"{rel} imports tab packages directly — platform code must use "
        "TabRegistry/TabContract discovery:\n"
        + "\n".join(f"  line {lineno}: imports {module!r}" for lineno, module in hits)
    )


def test_shared_live_snapshot_is_tab_agnostic():
    path = SRC / "engine" / "cache" / "live_snapshot.py"
    text = path.read_text(encoding="utf-8")
    forbidden = ("image1", "image2", "image_list", "current_index", "FrameSnapshot")
    hits = [token for token in forbidden if token in text]
    assert not hits, (
        "shared live_snapshot.py must delegate to tab services, not build "
        f"image-pair snapshots directly: {hits}"
    )


# The "document" session state slot (image-pair DocumentModel) is owned by
# the image_compare tab (docs/dev/DOCUMENT_ACCESS_FANOUT.md). Everywhere
# else must reach it through store.get_session_state_slot("document"), not
# the store.document / self.document mirror attribute — even inside core,
# where the mirror is implemented (store.py, store_workspace.py,
# domain/workspace.py) and inside tracing instrumentation, which patches
# Store generically and predates the fanout.
_DOCUMENT_MIRROR_RE = re.compile(r"\bstore\.document\b|\bself\.document\b")
_DOCUMENT_MIRROR_IMPL_FILES = frozenset(
    {
        "src/core/store.py",
        "src/core/store_workspace.py",
        "src/domain/workspace.py",
        "src/core/tracing/instrumentation.py",
        # WorkerStoreSnapshot.document is its own DTO attribute (the frozen
        # export snapshot), not the Store.document mirror — same name,
        # unrelated concept.
        "src/core/store_settings.py",
    }
)


def _iter_all_src_py_files() -> list[Path]:
    return sorted(
        p
        for p in SRC.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _iter_document_mirror_check_files() -> list[Path]:
    """``_iter_all_src_py_files()`` minus the files the mirror-attribute
    check below doesn't apply to. Filtering here (at parametrize
    collection time) instead of ``pytest.skip``-ing inside the test body
    means a violation still points at the exact offending file, but the
    ~450 files that structurally can't violate this (image_compare owns
    the slot; the impl files define the mirror itself) don't each produce
    a SKIPPED row in the run output."""
    files = []
    for p in _iter_all_src_py_files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in _DOCUMENT_MIRROR_IMPL_FILES:
            continue
        if rel.startswith("src/tabs/image_compare/"):
            continue
        files.append(p)
    return files


@pytest.mark.parametrize(
    "py_file",
    _iter_document_mirror_check_files(),
    ids=lambda p: p.relative_to(ROOT).as_posix(),
)
def test_document_mirror_attribute_not_used_outside_owner(py_file: Path):
    rel = py_file.relative_to(ROOT).as_posix()
    text = py_file.read_text(encoding="utf-8")
    hits = []
    for match in _DOCUMENT_MIRROR_RE.finditer(text):
        line_no = text[: match.start()].count("\n") + 1
        hits.append((line_no, match.group(0)))
    assert not hits, (
        f"{rel} uses the store.document mirror attribute — use "
        'store.get_session_state_slot("document") instead:\n'
        + "\n".join(f"  line {ln}: {tok!r}" for ln, tok in hits)
    )
