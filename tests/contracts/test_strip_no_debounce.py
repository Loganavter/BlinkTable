"""Strip path: no debounce, no timer, no sleep — the redraw happens on the tick.

Dogma (``plan-video-strip-atlas-2026-10-30``, phase 4): the video editor's
filmstrip is ONE synchronous GPU pass. A scroll/zoom/resize tick reaches
``StripBandHost._ensure_window`` → ``strip_window.build_window`` →
``strip_pass.render_strip_window`` and the frame is drawn; a newer tick
supersedes an older window (generations + ``cancel``) and the stale window is
never applied. That replaced per-tile workers, per-frame caches and a settle
gate.

Coalescing is the failure mode this contract exists for: a ``QTimer`` /
``singleShot`` / ``SettleGate`` / ``time.sleep`` on this path reintroduces a
wait between the input and the frame, and the owner's number (any interaction
< 10 ms, ``src/devtools/bench_strip_latency.py``) stops meaning anything — the
frame is cheap and the timer hides it.

Rule: the strip path contains no timer type, no settle/coalescing call and no
``sleep``. Latest-wins is the only coalescing allowed, and it is structural
(``is_current``/``cancel``), not temporal.

Scope: the four app modules of the strip path, plus the toolkit ``StripBand``
source when a sibling toolkit checkout is reachable (``requirements-gui.txt``
installs it editable as ``-e ../sli-ui-toolkit``; a clone cell has no sibling,
so that half is skipped there and runs in the main checkout / CI).

Detection is AST-based, so a docstring or comment that *says* "no timer" is not
an offence — only code is. Each finding is reported as
``path:line: reason: <source line>``.
"""

from __future__ import annotations

import ast

import pytest

from tests.contracts._framework import ROOT, SRC, read, rel

#: The strip path: the window CPU half, the GPU pass, the editor's band host and
#: the recording seam the host hands the window.
STRIP_PATH = (
    SRC / "tabs" / "image_compare" / "services" / "video_snapshot_rendering" / "strip_window.py",
    SRC / "tabs" / "image_compare" / "services" / "video_snapshot_rendering" / "strip_pass.py",
    SRC / "tabs" / "image_compare" / "plugins" / "video_editor" / "presenter_parts" / "strip_band.py",
    SRC / "tabs" / "image_compare" / "plugins" / "video_editor" / "services" / "strip_recording.py",
)

#: Toolkit band (paint-only QRhiWidget): the seam the host drives. Present only
#: when the sibling checkout exists, i.e. NOT inside a clone cell.
TOOLKIT_BAND = (
    ROOT.parent
    / "sli-ui-toolkit"
    / "src"
    / "sli_ui_toolkit"
    / "ui"
    / "widgets"
    / "composite"
    / "strip_band.py"
)

#: Timer / settle-gate TYPES. A reference is enough — `QTimer.singleShot(...)`,
#: `x = QTimer()` and `from PySide6.QtCore import QTimer` all show up here.
BANNED_REFERENCES = {
    "qtimer",
    "qbasictimer",
    "qtimerevent",
    "settlegate",
}

#: Calls that coalesce or wait. Normalized (lower case, underscores dropped) so
#: `single_shot`/`singleShot` and `SettleGate.wait` cannot slip past spelling.
BANNED_CALLS = {
    "singleshot",
    "starttimer",
    "killtimer",
    "sleep",
    "debounce",
    "throttle",
    "coalesce",
    "settlegate",
}

#: Substrings that make a call debounce-like even under a name this list has not
#: seen (`schedule_debounced_rebuild`, `throttled_tick`).
BANNED_CALL_PARTS = ("debounce", "throttle", "coalesc")


def _normalize(name: str) -> str:
    return str(name).replace("_", "").lower()


def _callee_name(func) -> str | None:
    """The called name, for ``f()``, ``obj.f()`` and ``mod.obj.f()``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def offences_in_source(source: str, label: str) -> list[str]:
    """``["label:line: reason: source line"]`` for one module's offenders."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # a contract that cannot read the file must not pass
        return [f"{label}: cannot parse: {exc}"]
    lines = source.splitlines()
    findings: dict[int, str] = {}

    def add(node, reason: str) -> None:
        line = int(getattr(node, "lineno", 0) or 0)
        if line in findings:
            return
        text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        findings[line] = f"{label}:{line}: {reason}: {text}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if _normalize(node.id) in BANNED_REFERENCES:
                add(node, f"timer/settle-gate reference `{node.id}`")
        elif isinstance(node, ast.Attribute):
            if _normalize(node.attr) in BANNED_REFERENCES:
                add(node, f"timer/settle-gate reference `{node.attr}`")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if _normalize(alias.name) in BANNED_REFERENCES:
                    add(node, f"timer/settle-gate import `{alias.name}`")
        elif isinstance(node, ast.Call):
            name = _callee_name(node.func)
            if name is None:
                continue
            key = _normalize(name)
            if key in BANNED_CALLS or any(part in key for part in BANNED_CALL_PARTS):
                add(node, f"coalescing/timer call `{name}`")
    return [findings[line] for line in sorted(findings)]


def scanned_files() -> list:
    """Every strip-path module this checkout can see."""
    files = list(STRIP_PATH)
    if TOOLKIT_BAND.is_file():
        files.append(TOOLKIT_BAND)
    return files


@pytest.mark.parametrize("path", STRIP_PATH, ids=lambda path: rel(path))
def test_each_declared_strip_module_exists(path):
    # Non-vacuity: a moved/renamed file must break this contract, not silently
    # shrink the scan to nothing.
    assert path.is_file(), f"strip path module missing: {rel(path)}"


def test_strip_path_has_no_debounce():
    scanned = scanned_files()
    offences: list[str] = []
    for path in scanned:
        offences.extend(offences_in_source(read(path), rel(path)))
    assert not offences, (
        "The strip path must draw on the tick: no QTimer/SettleGate/sleep/"
        "debounce. Latest-wins (generations + cancel) is the only coalescing "
        "allowed — see plan-video-strip-atlas-2026-10-30 and "
        "src/devtools/bench_strip_latency.py.\n" + "\n".join(offences)
    )
    assert len(scanned) >= len(STRIP_PATH)


def test_toolkit_strip_band_is_scanned_when_reachable():
    if not TOOLKIT_BAND.is_file():
        pytest.skip(
            "no sibling sli-ui-toolkit checkout here (clone cell): the toolkit "
            "band is scanned in the main checkout / CI"
        )
    offences = offences_in_source(read(TOOLKIT_BAND), "sli_ui_toolkit/.../strip_band.py")
    assert not offences, "toolkit StripBand must draw on the tick:\n" + "\n".join(
        offences
    )


def test_detector_catches_a_timer():
    """The detector must fail on the thing it exists for (falsifiable)."""
    source = (
        "from PySide6.QtCore import QTimer\n"
        "\n"
        "def schedule(rebuild):\n"
        "    QTimer.singleShot(16, rebuild)\n"
    )
    offences = offences_in_source(source, "sample.py")
    assert offences, "no offence detected for a QTimer import + singleShot"
    assert offences[0].startswith("sample.py:1:"), offences
    assert any(":4:" in offence for offence in offences), offences


@pytest.mark.parametrize(
    "source",
    [
        "import time\n\ndef tick():\n    time.sleep(0.01)\n",
        "def tick(gate):\n    gate = SettleGate()\n    return gate\n",
        "class X:\n    def tick(self):\n        self._debounced_rebuild()\n",
        "def tick(win):\n    startTimer(16)\n",
    ],
    ids=["sleep", "settle-gate", "debounced-name", "start-timer"],
)
def test_detector_catches_each_banned_form(source):
    assert offences_in_source(source, "sample.py"), source


def test_detector_ignores_prose_and_real_coalescing():
    """A docstring saying "no timer" is not a timer; latest-wins is legal."""
    source = (
        '"""Cancellation is not debounce: no timers, no settle gate."""\n'
        "\n"
        "# no QTimer here either\n"
        "def tick(window, newer):\n"
        "    if not is_current(window):\n"
        "        return\n"
        "    cancel(window, 'superseded')\n"
    )
    assert offences_in_source(source, "sample.py") == []
