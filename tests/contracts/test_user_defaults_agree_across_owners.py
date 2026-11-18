"""A user-facing default has ONE owner; every copy must agree with it.

Why this exists (2026-11-18, two owner reports in one day):

- ``auto_crop_black_borders`` was ``True`` in three places, so a fresh install
  silently cropped the pair it was asked to compare while the HUD still printed
  the source size ("a 512x512 shows as 312x512").
- ``export_crop_to_image`` was ``False`` in three places, so the live canvas
  showed a cropped pair while the written file kept the full frame and its
  black borders ("does the exporter ignore the crop?").

Both times the *same* default was written down more than once and nothing
compared the copies, and no test said what a fresh install should do.

Ownership — which of the three owners is authoritative, and why the others are
copies:

1. ``SettingsState`` (``src/core/store_settings.py``) is the SOURCE OF TRUTH.
   Its dataclass default is literally what a fresh install gets, and it is the
   value every screen, canvas and export path reads at runtime.
2. ``SettingsManager.load_all_settings`` (``src/plugins/settings/manager.py``)
   is copy #2: ``self._get_setting("<key>", <literal>, <type>)`` supplies the
   value when QSettings holds no entry (first start, new key after an upgrade).
   It cannot be the owner — it runs *into* the dataclass, and it is written by
   hand — but it must agree, or the first unpersisted start behaves unlike the
   declared default.
3. ``getattr(settings, "<key>", <literal>)`` call sites under ``src/`` are
   copy #3 (and, for ``auto_crop_black_borders``, many more). They are defensive
   defaults for a settings object that lacks the field. For a real
   ``SettingsState`` they are dead code, which is exactly why they drift
   unnoticed — and then silently become the default for any detached/snapshot
   settings object that reaches them.

``PINNED_DEFAULTS`` is deliberately short: only defaults a user can observe on a
fresh install and that have already burned us. Adding a key here is cheap, and
it *requires* a fresh-install behaviour test in
``tests/plugins/test_user_defaults_fresh_install.py`` — a pinned default nobody
exercises is how the pair above drifted.

Pre-existing drift is pinned, never hidden: ``_KNOWN_FALLBACK_DRIFT`` lists the
``getattr`` fallbacks that still carry the pre-fix default. Each entry is debt
(a ``src/`` fix is a different change than this guard) and is keyed by
``(path, key, observed value)`` so a *new* disagreeing site still fails, and a
*repaired* site leaves a stale entry that fails the ratchet until the entry is
deleted.

Scanner notes: ``ast`` only, no app imports, no Qt, no runtime. Every failure
names ``path:line`` and both values (observed vs pinned).
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

from ._framework import ROOT, SRC, iter_py, parse, rel

pytestmark = pytest.mark.slow

#: The pinned table. ``key`` is both the ``SettingsState`` field name and the
#: QSettings key (they coincide for every entry here); the value is what a
#: fresh install must deliver.
PINNED_DEFAULTS: dict[str, bool] = {
    # OFF: a fresh install must not crop the pair it was asked to compare.
    "auto_crop_black_borders": False,
    # ON: the written file must be what the canvas shows (crop applied).
    "export_crop_to_image": True,
    "export_save_view": False,
    "export_fill_background": False,
}

STORE_SETTINGS_REL = "core/store_settings.py"
MANAGER_REL = "plugins/settings/manager.py"
FRESH_INSTALL_TEST_REL = "tests/plugins/test_user_defaults_fresh_install.py"

#: ``getattr(settings, "<key>", <value>)`` sites that still carry the PRE-fix
#: default, keyed by ``(path, key, observed value)`` with a SITE COUNT.
#:
#: All eight are ``auto_crop_black_borders``: the 2026-11-18 fix flipped the
#: dataclass, ``load_all_settings`` and one ``getattr`` in
#: ``application_service.py``, and a follow-up flipped the three single-line
#: ``getattr(..., "auto_crop_black_borders", True)`` sites. These eight are the
#: ones a single-line ``grep 'getattr(.*auto_crop_black_borders'`` cannot see
#: (the call is wrapped over several lines), which is why they survived both
#: sweeps. Each still says "no settings object → crop" and is dead code for a
#: real ``SettingsState`` (the field always exists); flipping it to ``False``
#: is a ``src/`` change of its own. Find the exact lines by AST (or with
#: ``grep -n -A3 'getattr($' <path>``).
#:
#: The count is the ratchet: a new disagreeing site (even in a listed file) has
#: no entry left to consume and fails; a repaired site leaves a surplus count
#: and fails as stale, forcing the entry to be dropped.
_KNOWN_FALLBACK_DRIFT: dict[tuple[str, str, bool], int] = {
    (
        "src/tabs/image_compare/_session_controller.py",
        "auto_crop_black_borders",
        True,
    ): 2,
    (
        "src/tabs/image_compare/canvas/features/magnifier/geometry/box_remap.py",
        "auto_crop_black_borders",
        True,
    ): 1,
    (
        "src/tabs/image_compare/ui/context_menu.py",
        "auto_crop_black_borders",
        True,
    ): 1,
    (
        "src/tabs/image_compare/use_cases/session_init.py",
        "auto_crop_black_borders",
        True,
    ): 1,
    ("src/tabs/multi_compare/use_cases/crop.py", "auto_crop_black_borders", True): 1,
    (
        "src/tabs/multi_compare/use_cases/loading.py",
        "auto_crop_black_borders",
        True,
    ): 1,
    ("src/ui/managers/dialog_manager.py", "auto_crop_black_borders", True): 1,
}


# ---------------------------------------------------------------------------
# AST helpers (shared by every check below)
# ---------------------------------------------------------------------------


def _bool_literal(node: ast.AST | None) -> bool | None:
    """The value of a ``True``/``False`` literal node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _str_literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _dataclass_bool_defaults(src_root: Path) -> dict[str, tuple[bool, int]]:
    """``SettingsState`` field -> (literal bool default, lineno)."""
    tree = parse(src_root / STORE_SETTINGS_REL)
    if tree is None:
        return {}
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == "SettingsState"):
            continue
        out: dict[str, tuple[bool, int]] = {}
        for stmt in node.body:
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is not None
            ):
                value = _bool_literal(stmt.value)
                if value is not None:
                    out[stmt.target.id] = (value, stmt.lineno)
        return out
    return {}


def _manager_load_fallbacks(src_root: Path) -> list[tuple[int, str, bool]]:
    """Every ``self._get_setting("<key>", <bool>, bool)`` in the manager."""
    tree = parse(src_root / MANAGER_REL)
    if tree is None:
        return []
    out: list[tuple[int, str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "_get_setting"):
            continue
        if len(node.args) != 3:
            continue
        key = _str_literal(node.args[0])
        value = _bool_literal(node.args[1])
        if key is None or value is None:
            continue
        out.append((node.lineno, key, value))
    return sorted(out)


def _getattr_fallbacks(src_root: Path) -> list[tuple[str, int, str, bool]]:
    """Every ``getattr(obj, "<key>", <bool>)`` under ``src/``."""
    out: list[tuple[str, int, str, bool]] = []
    for path in iter_py(src_root):
        tree = parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "getattr"):
                continue
            if len(node.args) != 3:
                continue
            key = _str_literal(node.args[1])
            value = _bool_literal(node.args[2])
            if key is None or value is None:
                continue
            out.append((rel(path), node.lineno, key, value))
    return sorted(out)


# ---------------------------------------------------------------------------
# (a) The dataclass owns the pinned value
# ---------------------------------------------------------------------------


def dataclass_problems(src_root: Path | None = None) -> list[str]:
    root = src_root or SRC
    defaults = _dataclass_bool_defaults(root)
    where = rel(root / STORE_SETTINGS_REL)
    problems: list[str] = []
    for key, pinned in sorted(PINNED_DEFAULTS.items()):
        owner = defaults.get(key)
        if owner is None:
            problems.append(
                f"{where} — SettingsState has no bool field `{key}` with a literal "
                f"default; the pinned table expects {pinned!r}"
            )
            continue
        value, lineno = owner
        if value != pinned:
            problems.append(
                f"{where}:{lineno} — SettingsState.{key} default is {value!r}, "
                f"pinned {pinned!r} (the dataclass is the source of truth; change "
                f"PINNED_DEFAULTS if the new default is intended)"
            )
    return problems


def test_dataclass_defaults_own_the_pinned_values():
    problems = dataclass_problems()
    assert not problems, (
        "SettingsState (src/core/store_settings.py) is the source of truth for a "
        "fresh install's defaults, and it disagrees with PINNED_DEFAULTS:\n  - "
        + "\n  - ".join(problems)
    )


# ---------------------------------------------------------------------------
# (b) SettingsManager's load fallbacks agree
# ---------------------------------------------------------------------------


def manager_problems(src_root: Path | None = None) -> list[str]:
    root = src_root or SRC
    where = rel(root / MANAGER_REL)
    fallbacks = [
        (lineno, key, value)
        for lineno, key, value in _manager_load_fallbacks(root)
        if key in PINNED_DEFAULTS
    ]
    problems: list[str] = []
    seen: set[str] = set()
    for lineno, key, value in fallbacks:
        seen.add(key)
        pinned = PINNED_DEFAULTS[key]
        if value != pinned:
            problems.append(
                f"{where}:{lineno} — load fallback for `{key}` is {value!r}, "
                f"pinned {pinned!r} (SettingsState default)"
            )
    for key in sorted(set(PINNED_DEFAULTS) - seen):
        problems.append(
            f"{where} — load_all_settings never reads `{key}` via "
            f"_get_setting({key!r}, <literal>, <type>); a pinned default that is "
            f"not loaded on start is not the default a user gets"
        )
    return problems


def test_manager_load_fallbacks_agree_with_the_dataclass():
    problems = manager_problems()
    assert not problems, (
        "SettingsManager.load_all_settings re-declares the SettingsState defaults "
        "by hand; a disagreement means the first unpersisted start behaves unlike "
        "the declared default:\n  - " + "\n  - ".join(problems)
    )


# ---------------------------------------------------------------------------
# (c) getattr fallbacks agree (`_KNOWN_FALLBACK_DRIFT` is a ratchet)
# ---------------------------------------------------------------------------


def _pinned_getattr_sites(src_root: Path) -> list[tuple[str, int, str, bool]]:
    return [site for site in _getattr_fallbacks(src_root) if site[2] in PINNED_DEFAULTS]


def getattr_problems(src_root: Path | None = None) -> list[str]:
    root = src_root or SRC
    problems: list[str] = []
    remaining = Counter(_KNOWN_FALLBACK_DRIFT)
    for path_rel, lineno, key, value in _pinned_getattr_sites(root):
        pinned = PINNED_DEFAULTS[key]
        if value == pinned:
            continue
        entry = (path_rel, key, value)
        if remaining[entry] > 0:
            remaining[entry] -= 1
            continue
        problems.append(
            f"{path_rel}:{lineno} — getattr fallback for `{key}` is {value!r}, "
            f"pinned {pinned!r} (SettingsState default in "
            f"src/core/store_settings.py)"
        )
    return problems


def stale_drift_problems(src_root: Path | None = None) -> list[str]:
    root = src_root or SRC
    remaining = Counter(_KNOWN_FALLBACK_DRIFT)
    for path_rel, _lineno, key, value in _pinned_getattr_sites(root):
        entry = (path_rel, key, value)
        if remaining[entry] > 0 and value != PINNED_DEFAULTS[key]:
            remaining[entry] -= 1
    return [
        f"{path_rel} — `{key}` fallback {value!r} is pinned in "
        f"_KNOWN_FALLBACK_DRIFT but no longer disagrees with PINNED_DEFAULTS; "
        f"drop the entry"
        for (path_rel, key, value), count in sorted(remaining.items())
        if count > 0
    ]


def test_getattr_fallbacks_agree_with_the_dataclass():
    problems = getattr_problems()
    assert not problems, (
        'getattr(<settings>, "<key>", <default>) call sites carry another copy of '
        "a user-facing default; they must agree with SettingsState "
        "(src/core/store_settings.py):\n  - " + "\n  - ".join(problems)
    )


def test_known_fallback_drift_entries_are_not_stale():
    problems = stale_drift_problems()
    assert not problems, (
        "The _KNOWN_FALLBACK_DRIFT ratchet holds entries that no longer describe a "
        "disagreement — the src site was fixed:\n  - " + "\n  - ".join(problems)
    )


# ---------------------------------------------------------------------------
# (d) Every pinned default has a fresh-install behaviour test
# ---------------------------------------------------------------------------


def _identifiers(node: ast.AST) -> set[str]:
    """Every identifier-ish token in ``node`` (names, attrs, string literals)."""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(sub.name)
        elif isinstance(sub, ast.arg):
            out.add(sub.arg)
        elif isinstance(sub, ast.Attribute):
            out.add(sub.attr)
        elif isinstance(sub, ast.Name):
            out.add(sub.id)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(sub.value)
        elif isinstance(sub, ast.keyword) and sub.arg:
            out.add(sub.arg)
    return out


def fresh_install_test_coverage(path: Path | None = None) -> dict[str, list[str]]:
    """Pinned key -> names of runtime test functions that reference it."""
    test_path = path or (ROOT / FRESH_INSTALL_TEST_REL)
    tree = parse(test_path)
    covered: dict[str, list[str]] = {key: [] for key in PINNED_DEFAULTS}
    if tree is None:
        return covered
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        names = _identifiers(node)
        for key in PINNED_DEFAULTS:
            if key in names:
                covered[key].append(node.name)
    return covered


def test_every_pinned_default_has_a_fresh_install_behaviour_test():
    covered = fresh_install_test_coverage()
    missing = sorted(key for key, owners in covered.items() if not owners)
    assert not missing, (
        f"{FRESH_INSTALL_TEST_REL} must prove what a fresh install delivers for "
        f"every pinned default. No test function references: {missing}. A "
        f"statically-agreeing default nobody exercises is how "
        f"auto_crop_black_borders/export_crop_to_image drifted (2026-11-18)."
    )
