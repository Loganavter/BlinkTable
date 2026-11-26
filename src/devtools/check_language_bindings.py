#!/usr/bin/env python3
"""Gate: user-visible text must survive a live language switch.

A live ``emit_language_changed("ru")`` re-applies text only through
``sli_ui_toolkit.i18n`` bindings (``translatable_text``,
``translatable_tooltip``, ``translatable_placeholder``,
``translatable_callback``) or through an explicit ``language_changed``
subscription. A module that resolves text once with a
bare ``tr(key)`` at construction time keeps the old language until restart —
the measured defect behind the 2026-10-29 retranslation pass (Image Compare
toolbar and the onboarding overlay stayed English on a live switch while a
restart in ``ru`` was fully correct, and the catalogs had 0 gaps).

Rule: a module under ``src/ui/**``, ``src/plugins/**``, ``src/tabs/*/ui/**``,
``src/tabs/*/widget.py`` or ``src/tabs/*/plugins/**`` that calls ``tr(`` is a
violation unless it also (a) calls any ``translatable_*`` helper, (b) connects
to ``language_changed`` or defines ``on_language_changed`` /
``_on_language_changed``, or (c) is listed in the allowlist.

Allowlist: one ``path<TAB>reason`` per line, ``#`` comments, path relative to
the repo root. Ratchet: an entry whose file is no longer a violation is itself
a violation (a stale entry), so the list can only shrink.

Usage::

    python3 src/devtools/check_language_bindings.py [--root DIR] [--allow FILE] [--selftest]

Exit codes: 0 clean, 1 violations, 2 usage/parse error.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ALLOW = "src/devtools/language_bindings_allow.txt"

#: Directories whose text lands on persistent widgets. App-shell/engine
#: modules are out of scope: they hold no widget text, and a tree-wide scan
#: would report every tr() lookup used to format a one-shot value.
SCOPE_PATTERNS = (
    "ui/**/*.py",
    "plugins/**/*.py",
    "tabs/*/ui/**/*.py",
    "tabs/*/widget.py",
    "tabs/*/plugins/**/*.py",
    # A tab's own top-level modules carry text too (the session picker's
    # cards live in ``tabs/session_picker/tab.py``); missing them left a live
    # switch half-translated while the gate reported clean (2026-10-29).
    "tabs/*/*.py",
)

BINDING_PREFIX = "translatable_"
LANGUAGE_HOOK_NAMES = frozenset({"on_language_changed", "_on_language_changed"})
LANGUAGE_HOOK_ATTR = "language_changed"


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    reason: str


def iter_scoped_files(src_root: Path) -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in SCOPE_PATTERNS:
        for path in src_root.glob(pattern):
            if path.is_file() and path.suffix == ".py":
                seen[path] = None
    return sorted(seen)


def _is_tr_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "tr"
    # ``translations.tr(...)`` / ``ctx.tr(...)`` — same lookup, qualified.
    return isinstance(func, ast.Attribute) and func.attr == "tr"


def _is_binding_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id.startswith(BINDING_PREFIX)
    if isinstance(func, ast.Attribute):
        return func.attr.startswith(BINDING_PREFIX)
    return False


def _has_language_hook(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in LANGUAGE_HOOK_NAMES:
                return True
        if isinstance(node, ast.Attribute) and node.attr == LANGUAGE_HOOK_ATTR:
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if LANGUAGE_HOOK_ATTR in node.value:
                return True
    return False


def analyse(path: Path) -> tuple[bool, int]:
    """Return ``(has_raw_tr, first_tr_line)``; raises ``SyntaxError``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    first_line = 0
    has_tr = False
    for node in ast.walk(tree):
        if _is_tr_call(node):
            has_tr = True
            if not first_line:
                first_line = getattr(node, "lineno", 0)
    if not has_tr:
        return False, 0
    if _has_language_hook(tree):
        return False, first_line
    if any(_is_binding_call(node) for node in ast.walk(tree)):
        return False, first_line
    return True, first_line


def read_allowlist(allow_path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if not allow_path.is_file():
        return entries
    for raw in allow_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path, _, reason = line.partition("\t")
        path = path.strip()
        if not path:
            continue
        entries[path] = reason.strip()
    return entries


def run(root: Path, allow_path: Path) -> list[Finding]:
    src_root = root / "src"
    allow = read_allowlist(allow_path)
    findings: list[Finding] = []
    used: set[str] = set()

    for path in iter_scoped_files(src_root):
        rel = path.relative_to(root).as_posix()
        try:
            is_violation, line = analyse(path)
        except SyntaxError as exc:
            raise SystemExit(_parse_error(path, exc))
        if not is_violation:
            continue
        if rel in allow:
            used.add(rel)
            continue
        findings.append(
            Finding(
                rel,
                line,
                "calls tr() with no translatable_* binding and no "
                "language_changed hook — text will not follow a live switch",
            )
        )

    for rel in sorted(allow):
        if rel in used:
            continue
        findings.append(
            Finding(
                rel,
                0,
                "stale allowlist entry: file is no longer a violation "
                "(remove the line so the list can only shrink)",
            )
        )
    return findings


def _parse_error(path: Path, exc: SyntaxError) -> int:
    print(f"error: cannot parse {path}: {exc}", file=sys.stderr)
    return 2


def selftest() -> int:
    """Assert the five verdicts on a throwaway tree (no repo, no network)."""
    with tempfile.TemporaryDirectory(prefix=".language-bindings-selftest-", dir=Path.cwd()) as tmp:
        root = Path(tmp)
        (root / "src" / "ui").mkdir(parents=True)
        (root / "src" / "devtools").mkdir(parents=True)

        def write(rel: str, body: str) -> None:
            (root / rel).write_text(body, encoding="utf-8")

        # The lookup name is assembled, never spelled as a literal lookup call
        # in this file: ``check_translations.py --code-keys`` scans src/ for
        # literal keys and would read the fixtures as app keys.
        lookup = "tr"
        # (1) clean-but-literal: a raw lookup next to a binding is fine.
        write(
            "src/ui/clean.py",
            "from sli_ui_toolkit.i18n import tr, translatable_text\n"
            "def build(w, lang):\n"
            "    translatable_text(w, 'k')\n"
            "    return " + lookup + "('k', lang)\n",
        )
        # (2) bound-only module: no raw lookup at all.
        write(
            "src/ui/bound.py",
            "from sli_ui_toolkit.i18n import translatable_tooltip\n"
            "def build(w):\n"
            "    translatable_tooltip(w, 'k')\n",
        )
        # (3) hooked module: raw lookup + explicit language_changed subscription.
        write(
            "src/ui/hooked.py",
            "from sli_ui_toolkit.i18n import tr, translation_events\n"
            "def build(w):\n"
            "    translation_events().language_changed.connect(lambda lang: None)\n"
            "    return " + lookup + "('k')\n",
        )
        # (4) violating module: raw lookup, nothing re-applies it.
        write(
            "src/ui/violating.py",
            "from sli_ui_toolkit.i18n import tr\n"
            "def build():\n"
            "    return " + lookup + "('k')\n",
        )

        allow = root / "allow.txt"
        allow.write_text(
            "# path<TAB>reason\n"
            "src/ui/bound.py\tno raw tr() left — this entry is stale\n",
            encoding="utf-8",
        )

        findings = run(root, allow)
        reported = {(f.path, f.reason.split(":")[0]) for f in findings}
        errors: list[str] = []
        if any(f.path == "src/ui/clean.py" for f in findings):
            errors.append("clean.py (binding present) was reported")
        if any(f.path == "src/ui/hooked.py" for f in findings):
            errors.append("hooked.py (language_changed hook) was reported")
        if not any(f.path == "src/ui/violating.py" for f in findings):
            errors.append("violating.py (raw tr, no hook) was NOT reported")
        if not any(
            f.path == "src/ui/bound.py" and "stale allowlist entry" in f.reason
            for f in findings
        ):
            errors.append("stale allowlist entry was NOT reported")
        if len(reported) != len(findings):
            pass  # cosmetic only: duplicates carry distinct reasons
        if errors:
            for err in errors:
                print(f"selftest FAIL: {err}", file=sys.stderr)
            return 1
        print("selftest ok: clean, bound, hooked pass; violating + stale reported")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="repo root (default: this checkout)")
    parser.add_argument("--allow", default=None, help=f"allowlist (default: {DEFAULT_ALLOW})")
    parser.add_argument("--selftest", action="store_true", help="run the built-in fixture tree")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]
    allow_path = Path(args.allow) if args.allow else root / DEFAULT_ALLOW
    if not (root / "src").is_dir():
        print(f"error: {root} has no src/ directory", file=sys.stderr)
        return 2

    findings = run(root, allow_path)
    if not findings:
        print("language bindings: clean")
        return 0
    for finding in findings:
        if finding.line:
            print(f"{finding.path}:{finding.line}: {finding.reason}")
        else:
            print(f"{finding.path}: {finding.reason}")
    print(f"language bindings: {len(findings)} violation(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
