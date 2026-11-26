"""Contract: the app palette covers every token the toolkit (and the app) reads.

Dogma: ``ThemeManager`` resolves a token against the host palette only — the
toolkit ships no merge of its own defaults into a host's registered dict. A key
the host does not register therefore paints the painter's neutral fallback
(``#808080``) or a neighbouring role's colour and logs ``unknown theme token``
(measured 2026-11-26 after the toolkit's control-family migration: 52 keys —
``input.text`` / ``input.background`` read by ``CustomLineEdit``, the whole
``checkbox.*`` / ``menu.*`` / ``list_item.*`` state sets, ``progress.*``,
``slider.*``, ``switch.*``, ``label.*``, ``flyout.*``, ``link``, …). The
gallery had already grown a token-coverage test for its own keys; this one
covers the seam with the toolkit.

Three invariants, each a separate failure so a red run names the cause:

1. both palettes are supersets of ``sli_ui_toolkit.palettes.FLUENT_LIGHT`` /
   ``FLUENT_DARK`` — the toolkit can read any shipped key through the
   ``try_get_color(...) or default`` pattern that a literal scan cannot see;
2. every token the **toolkit** source passes to a colour lookup resolves;
3. every token the **app** source passes to a colour lookup resolves
   (``transparent`` is a deliberate optional probe, not a palette key).

THEMING.md: a palette edit is the fix, never an inline ``#xxxxxx`` in a widget.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
THEMES = ROOT / "src/resources/themes.json"
SRC = ROOT / "src"

#: Optional probes: a key the caller accepts as absent by design.
#: ``session_picker/recent/cards.py:_transparent_color`` asks for a generic
#: ``transparent`` token and falls back to ``#00000000`` when it is missing.
OPTIONAL_PROBES = {"transparent"}

_TOKEN_CALL = re.compile(
    r"""(?:get_color|try_get_color|resolve_theme_color|try_resolve_theme_color|resolve_token)"""
    r"""\s*\(\s*(?:[^,()]*,)?\s*["']([A-Za-z0-9_.]+)["']"""
)
_COLOR_TOKEN_KW = re.compile(r"""color_token\s*=\s*["']([A-Za-z0-9_.]+)["']""")
_TOKEN_FAMILY_KW = re.compile(r"""token_family\s*=\s*\(([^)]*)\)""")
_FAMILY_LITERAL = re.compile(r"""["']([A-Za-z0-9_.]+)["']""")


def _themes() -> dict[str, dict[str, str]]:
    return json.loads(THEMES.read_text(encoding="utf-8"))


def _toolkit_root() -> Path:
    import sli_ui_toolkit

    return Path(sli_ui_toolkit.__file__).resolve().parent


def _is_scanned(path: Path) -> bool:
    parts = set(path.parts)
    return "__pycache__" not in parts and "tests" not in parts


def _tokens_read(root: Path) -> dict[str, str]:
    """``token -> first "file:line"`` for every literal a painter resolves."""
    found: dict[str, str] = {}
    for path in root.rglob("*.py"):
        if not _is_scanned(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            keys = [m.group(1) for m in _TOKEN_CALL.finditer(line)]
            keys += [m.group(1) for m in _COLOR_TOKEN_KW.finditer(line)]
            for key in keys:
                found.setdefault(key, f"{path}:{lineno}")
    return found


def _families_read(root: Path) -> dict[str, str]:
    """``family prefix -> first "file:line"`` declared through ``token_family``.

    A family entry is a key *or* a prefix (``("surface",)`` means
    ``surface.*``; the toolkit's own gate accepts a declared runtime prefix).
    """
    found: dict[str, str] = {}
    for path in root.rglob("*.py"):
        if not _is_scanned(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _TOKEN_FAMILY_KW.finditer(line):
                for family in _FAMILY_LITERAL.findall(match.group(1)):
                    found.setdefault(family, f"{path}:{lineno}")
    return found


def _assert_covered(
    tokens: dict[str, str],
    palettes: dict[str, dict[str, str]],
    *,
    owner: str,
) -> None:
    problems: list[str] = []
    for mode, palette in palettes.items():
        for key, site in sorted(tokens.items()):
            if key in OPTIONAL_PROBES or key in palette:
                continue
            problems.append(f"  {mode}: {key!r} read at {site}")
    assert not problems, (
        f"{owner} reads {len(problems)} token(s) the app palette does not define.\n"
        + "\n".join(problems)
        + "\nAdd each key to src/resources/themes.json under BOTH \"light\" and \"dark\" "
        "(THEMING.md § Palettes). A missing key paints the neutral #808080 fallback."
    )


def test_palette_covers_the_shipped_toolkit_palettes() -> None:
    """Both palettes are supersets of FLUENT_LIGHT / FLUENT_DARK."""
    from sli_ui_toolkit.palettes import FLUENT_DARK, FLUENT_LIGHT

    palettes = _themes()
    for mode, shipped in (("light", FLUENT_LIGHT), ("dark", FLUENT_DARK)):
        missing = sorted(set(shipped) - set(palettes[mode]))
        assert not missing, (
            f"{mode} palette is missing {len(missing)} toolkit key(s): {missing}.\n"
            "The toolkit may read any shipped key as a default; mirror it into themes.json."
        )


def test_palette_covers_every_token_the_toolkit_reads() -> None:
    """Every literal token the installed toolkit resolves exists in both palettes."""
    _assert_covered(_tokens_read(_toolkit_root()), _themes(), owner="sli_ui_toolkit")


def test_palette_covers_every_token_the_app_reads() -> None:
    """Every literal token the app's own painters resolve exists in both palettes."""
    _assert_covered(_tokens_read(SRC), _themes(), owner="the app source")


def _assert_families_covered(root: Path, owner: str) -> None:
    families = _families_read(root)
    palette = _themes()["light"]
    keys = set(palette)
    problems = [
        f"  {family!r} declared at {site} has no palette key under it"
        for family, site in sorted(families.items())
        if family not in keys and not any(key.startswith(family + ".") for key in keys)
    ]
    assert not problems, (
        f"{owner} declares token families with no palette key:\n" + "\n".join(problems)
    )


def test_toolkit_token_families_have_palette_keys() -> None:
    """A ``token_family=("prefix",)`` declaration names keys the palette must hold."""
    _assert_families_covered(_toolkit_root(), "sli_ui_toolkit")


def test_app_token_families_have_palette_keys() -> None:
    """The app's ``token_family`` declarations also resolve to palette keys."""
    _assert_families_covered(SRC, "the app source")


def test_dark_palette_is_a_superset_of_light() -> None:
    """THEMING.md: dark must carry every light key — a flip never falls back."""
    themes = _themes()
    missing = sorted(set(themes["light"]) - set(themes["dark"]))
    assert not missing, f"dark palette is missing light-only keys: {missing}"
