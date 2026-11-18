"""Guard the canvas content rect to one writer and one ruler.

Dogma source: ``docs/dev/rendering/coordinate-systems.md`` § "Single resolver
for both rects: ``resolve_canvas_content_geometry``" — the resolver is the
only owner of the fit — plus the same page's live-canvas paragraph naming
``engine/cache/display_tier_service.py`` as the module that commits
``_content_rect_px`` / ``_inner_content_rect_px``.

Why a VALUE-level sibling of ``test_canvas_content_geometry_single_owner.py``:
that contract pins the *functions* (nobody re-derives canvas bounds, nobody
recombines split position with canvas arithmetic). It stayed green through the
2026-11-18 divider mismatch because nothing in it looks at the values a caller
feeds the shared resolver. On that frame the image was painted from the plan's
crop-aware canvas basis, while the divider line/clip, the camera projection
(``devtools/canvas_scene/image_compare_instances.py``) and the drag->split
mapping (``events/image_label/geometry.py``) all read
``runtime_state._content_rect_px`` — which ``refresh_live_content_rect`` had
fitted from the UNCROPPED display store (``size=[512, 512]`` for a 312x512
crop). At split 0.5 both rects are centred, so the bug hid; at 0.25 the line
sat ~60 px off the painted cut. Two owners of one number, both calling the
sanctioned helper: a function-level scan cannot see that, a scan of *who
writes the field and who fits it* can. Prose alone (the doc already stated the
single-owner rule) did not hold it; this test scans source instead.

Two rules, both AST scans over ``src/`` (no runtime, no network):

1. One writer. ``state._content_rect_px`` / ``state._inner_content_rect_px``
   are assigned only in ``engine/cache/display_tier_service.py``. Any other
   module must appear in ``_RECT_WRITER_ALLOWLIST`` with a one-line reason.
   Class-body field declarations (``canvas/state.py``, bare ``Name``
   AnnAssign) are declarations, not writes, so they are not flagged.
   ``setattr(obj, "_content_rect_px", ...)`` and
   ``obj.__dict__["_content_rect_px"] = ...`` are flagged too — the obvious
   ways to bypass an attribute-assignment scan.

   Why each exempt entry is legitimate:
   - ``canvas/texture_parts/layers.py`` — single-pixmap preview upload
     (``set_pixmap``) sets the rect to the uploaded pixmap's own dims, and
     ``clear()`` resets both fields to ``None``. A texture-lifecycle reset,
     not a fit: a single pixmap has no pair to letterbox.
   - ``use_cases/canvas_invalidate.py`` — invalidation clears both rects to
     ``None`` so the next frame re-derives them through the service. Reset,
     not a fit.

2. One ruler. ``resolve_canvas_content_geometry(...)`` (and a future
   ``..._for_store`` wrapper — matched by prefix, as are import aliases) may
   only be *called* from the modules in ``_RULER_CALL_ALLOWLIST``. A tab-side
   or devtools caller fitting its own widget/image numbers is exactly the
   shape that produced the stale value above; it must read the committed rect
   (or the plan's geometry) instead.

   Why each exempt entry is legitimate:
   - ``ui/canvas_infra/scene/frame_geometry.py`` — the ruler itself (and any
     future internal caller): the single owner of the fit.
   - ``engine/cache/display_tier_service.py`` — the live/export geometry
     commit point, i.e. the writer rule's owner.
   - ``engine/cache/unified_envelope.py`` — eager max-envelope helper: one
     fit for both comparison sides, derived from the pair envelope.
   - ``tabs/image_compare/services/video_snapshot_rendering/strip_window.py``
     — video snapshot strip: a per-tile fit of the plan's canvas frame into a
     tile render target, with no widget/runtime-state resolved rect to read.
   - ``devtools/bench_strip_batch.py`` — strip-batch benchmark reproducing
     that same per-tile export fit; it calls the ruler (never local
     letterbox arithmetic) and has no widget to read a rect from. Devtools is
     NOT blanket-exempt: any new devtools caller fails this scan until it is
     added here with a reason.

Both scans report ``path:line`` so a failure names the offending statement.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ._framework import SRC, iter_py, parse, rel

pytestmark = pytest.mark.slow

_DOC = "docs/dev/rendering/coordinate-systems.md"

#: The scanned tree. A module-level name (not a scanner default argument) so a
#: throwaway copy of this file can point the same scanners at a planted tree.
SCAN_ROOT = SRC

#: The two runtime-state fields that hold one number under two names: the
#: padded canvas frame (outer) and the real image region inside it (inner).
_RECT_FIELDS = frozenset({"_content_rect_px", "_inner_content_rect_px"})

#: The ruler every fit goes through; the ``_for_store`` store-driven wrapper is
#: matched by prefix.
_RULER_PREFIX = "resolve_canvas_content_geometry"

#: The one module allowed to commit a fitted rect (rule 1's owner).
_RECT_OWNER = "src/engine/cache/display_tier_service.py"

#: Extra writers, each a lifecycle reset rather than a fit (reasons above).
_RECT_WRITER_ALLOWLIST: dict[str, str] = {
    "src/tabs/image_compare/canvas/texture_parts/layers.py": (
        "single-pixmap preview upload sets the rect to the pixmap's own dims "
        "and clear() resets both fields to None — texture lifecycle, not a fit"
    ),
    "src/tabs/image_compare/use_cases/canvas_invalidate.py": (
        "invalidation clears both rects to None so the next frame re-derives "
        "them through the display-tier service — reset, not a fit"
    ),
}

#: Every module allowed to CALL the ruler (reasons above).
_RULER_CALL_ALLOWLIST: dict[str, str] = {
    "src/ui/canvas_infra/scene/frame_geometry.py": (
        "the ruler itself — single owner of the fit"
    ),
    "src/engine/cache/display_tier_service.py": (
        "live/export geometry commit point — the writer rule's owner"
    ),
    "src/engine/cache/unified_envelope.py": (
        "eager max-envelope helper — one fit for both comparison sides"
    ),
    "src/tabs/image_compare/services/video_snapshot_rendering/strip_window.py": (
        "video snapshot strip — per-tile fit of the plan's canvas frame into a "
        "tile render target (no widget rect to read)"
    ),
    "src/devtools/bench_strip_batch.py": (
        "strip-batch benchmark reproducing the per-tile export fit through the "
        "ruler; no widget rect to read"
    ),
}


def _path_rel(path: Path, root: Path) -> str:
    """POSIX path relative to ``root``'s parent (``src/...``), or ``rel()``.

    ``root`` is normally ``SRC``, whose parent is the repo root — the same
    spelling ``_framework.rel()`` produces, so allowlist entries match on
    Windows CI as well as Unix. A planted tree under ``.scratch/`` also yields
    ``src/...`` for its own files, which keeps the scanners reusable there.
    """
    try:
        return path.relative_to(root.parent).as_posix()
    except ValueError:
        return rel(path)


def _is_allowlisted(path_rel: str, allowlist: dict[str, str]) -> bool:
    if path_rel in allowlist:
        return True
    # Test files are not part of the production single-owner surface.
    if path_rel.startswith("tests/"):
        return True
    return False


def _field_writes(tree: ast.Module) -> list[tuple[int, str]]:
    """``(lineno, field)`` for every rect-field write in ``tree``.

    Covers attribute assignment (``state._content_rect_px = ...``, plain /
    augmented / annotated), tuple targets, ``setattr(state, "_content_rect_px",
    ...)`` and ``state.__dict__["_content_rect_px"] = ...``. Class-body field
    declarations (``_content_rect_px: ... = None``) have a bare ``Name`` target
    and are therefore not writes.
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node.func) == "setattr":
            # ``setattr(obj, "_content_rect_px", ...)`` — a bare call
            # statement, not an assignment, so it needs its own branch.
            if len(node.args) >= 2 and _literal_field(node.args[1]) is not None:
                out.append((node.lineno, _literal_field(node.args[1]) or ""))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                out.extend(_write_in_target(target, node.lineno))
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            out.extend(_write_in_target(node.target, node.lineno))
    return out


def _write_in_target(target: ast.expr, lineno: int) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for sub in ast.walk(target):
        if isinstance(sub, ast.Attribute) and sub.attr in _RECT_FIELDS:
            out.append((lineno, sub.attr))
        elif (
            isinstance(sub, ast.Subscript)
            and _literal_field(sub.slice) is not None
        ):
            out.append((lineno, _literal_field(sub.slice) or ""))
    return out


def _literal_field(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and node.value in _RECT_FIELDS:
        return str(node.value)
    return None


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _content_rect_write_violations(root: Path) -> list[str]:
    """Rule 1: rect fields are written only by their single owner."""
    out: list[str] = []
    for path in iter_py(root):
        path_rel = _path_rel(path, root)
        if path_rel == _RECT_OWNER or _is_allowlisted(
            path_rel, _RECT_WRITER_ALLOWLIST
        ):
            continue
        tree = parse(path)
        if tree is None:
            continue
        for lineno, field in _field_writes(tree):
            out.append(
                f"{path_rel}:{lineno} writes {field} outside the geometry "
                f"owner ({_RECT_OWNER}) — fit through "
                f"resolve_canvas_content_geometry and let the display-tier "
                f"service commit the rect, or add a reasoned allowlist entry "
                f"(see {_DOC})"
            )
    return out


def _ruler_local_names(tree: ast.Module) -> set[str]:
    """Local names bound to the ruler by ``from ... import <ruler> as alias``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name.startswith(_RULER_PREFIX):
                    names.add(alias.asname or alias.name)
    return names


def _is_ruler_call(node: ast.Call, local_names: set[str]) -> str | None:
    name = _call_name(node.func)
    if name is None:
        return None
    if name.startswith(_RULER_PREFIX) or name in local_names:
        return name
    return None


def _ruler_call_violations(root: Path) -> list[str]:
    """Rule 2: only allowlisted owners fit geometry through the ruler."""
    out: list[str] = []
    for path in iter_py(root):
        path_rel = _path_rel(path, root)
        if _is_allowlisted(path_rel, _RULER_CALL_ALLOWLIST):
            continue
        tree = parse(path)
        if tree is None:
            continue
        local_names = _ruler_local_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _is_ruler_call(node, local_names)
            if name is None:
                continue
            out.append(
                f"{path_rel}:{node.lineno} calls {name}() directly — a "
                f"second fit owner; read the committed runtime content rect "
                f"(or the plan's geometry) instead, or add a reasoned "
                f"allowlist entry (see {_DOC})"
            )
    return out


def test_content_rect_fields_have_one_writer():
    violations = _content_rect_write_violations(SCAN_ROOT)
    assert not violations, "\n  - " + "\n  - ".join(violations)


def test_content_rect_ruler_calls_are_allowlisted():
    violations = _ruler_call_violations(SCAN_ROOT)
    assert not violations, "\n  - " + "\n  - ".join(violations)


def test_guard_is_not_vacuous():
    """The owner still writes both fields and every allowlist entry is live.

    Without this, renaming a field or moving the writer would turn both rules
    green-and-empty instead of red — the failure mode these guards exist for.
    """
    problems: list[str] = []
    owner_fields: set[str] = set()
    for path in iter_py(SCAN_ROOT):
        if _path_rel(path, SCAN_ROOT) != _RECT_OWNER:
            continue
        tree = parse(path)
        if tree is not None:
            owner_fields.update(field for _line, field in _field_writes(tree))
    missing = _RECT_FIELDS - owner_fields
    if missing:
        problems.append(
            f"{_RECT_OWNER} no longer writes {sorted(missing)} — the single "
            f"writer moved; update _RECT_OWNER / the allowlist rather than "
            f"leaving the scan empty"
        )
    for table in (_RECT_WRITER_ALLOWLIST, _RULER_CALL_ALLOWLIST):
        for path_rel, reason in table.items():
            if not (SCAN_ROOT.parent / path_rel).is_file():
                problems.append(
                    f"{path_rel}: allowlisted but missing on disk — drop the "
                    f"stale entry or fix the path"
                )
            if not reason.strip():
                problems.append(f"{path_rel}: allowlist entry has no reason")
    assert not problems, "\n  - " + "\n  - ".join(problems)
