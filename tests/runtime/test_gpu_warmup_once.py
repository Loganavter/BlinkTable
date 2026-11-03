"""GPU warm-up must run once per process, and heavy imports must not freeze a paint.

Both pinned here by the 2026-10-24 GUI stall-watchdog log:

- ``[stall] gui-loop blocked 4253ms`` / ``4027ms`` right around a record toggle
  whose own cost was **0.5 ms**: the export warm-up created offscreen canvas
  widgets, each paying a ~4 s D3D shader/pipeline compile inside
  ``magnifier_pass.initialize``, and it re-ran on every session activation.
  Today there is one: the second one existed for the per-frame filmstrip tiles,
  deleted with that pipeline (strip atlas phase 3, 2026-11-03);
- ``[stall] blocked 1350ms`` importing ``scipy.stats``/``skimage.metrics`` and
  ``320ms`` importing a magnifier module (PySide feature check walks
  ``inspect.getsource``).

The warm-up now reports success so the caller stops retrying, and the heavy
imports are warmed on a daemon thread instead of inside a paint.
"""

from __future__ import annotations

import sys

import core.import_warmup as warm


def test_import_warmup_disabled_by_default_flag_off(monkeypatch):
    monkeypatch.setenv("IMGSLI_IMPORT_WARMUP", "0")
    assert warm.enabled() is False
    assert warm.start() is None


def test_import_warmup_starts_once_and_imports_modules(monkeypatch):
    monkeypatch.setenv("IMGSLI_IMPORT_WARMUP", "1")
    monkeypatch.setattr(warm.start, "_started", False, raising=False)
    marker = "json.decoder"  # cheap, certainly importable
    sys.modules.pop(marker, None)
    thread = warm.start((marker,))
    assert thread is not None
    thread.join(timeout=5.0)
    assert marker in sys.modules, "the warm-up thread did not import the module"
    # idempotent per process: a second start must not spawn another thread
    assert warm.start((marker,)) is None


def test_import_warmup_survives_a_missing_module(monkeypatch):
    monkeypatch.setenv("IMGSLI_IMPORT_WARMUP", "1")
    monkeypatch.setattr(warm.start, "_started", False, raising=False)
    thread = warm.start(("definitely_not_a_module_xyz", "json"))
    assert thread is not None
    thread.join(timeout=5.0)
    assert "json" in sys.modules, "one bad module must not stop the others"


def test_pending_reports_only_unimported_modules():
    import json  # noqa: F401 - ensure it is imported

    assert warm.pending(("json",)) == ()
    assert warm.pending(("definitely_not_a_module_xyz",)) == ("definitely_not_a_module_xyz",)


def test_gpu_warm_up_reports_success_so_the_caller_can_stop():
    """The plugin marks the warm-up done from the return value.

    Without it the activation handler re-created the offscreen widget on every
    session switch and paid the ~4 s compile again (the measured 4253/4027 ms
    pair). Source-level pin: the exporter must return whether anything warmed.
    """
    from pathlib import Path

    service = Path("src/tabs/image_compare/plugins/video_editor/services/video_export/service.py")
    text = service.read_text(encoding="utf-8")
    assert "def warm_up_gpu_widgets(self) -> bool:" in text, (
        "warm_up_gpu_widgets must return bool: the caller needs success, not a "
        "fire-and-forget call that re-runs the 4 s compile on every activation"
    )
    # The one shot must actually reach the surviving offscreen service and
    # return *its* verdict — a fire-and-forget call (or a hardcoded True) would
    # let the caller record a cold widget as warm (the freeze-on-activation
    # cause was repetition; the caller's `_gpu_warm_done` gate is above).
    assert "service = self.gpu_export_service" in text, (
        "the warm-up must warm the exporter's own offscreen service"
    )
    assert "return bool(service.warm_up())" in text, (
        "the warm-up must return the service's own readiness, not a constant"
    )

    plugin = Path("src/plugins/export/plugin.py").read_text(encoding="utf-8")
    assert "_gpu_warm_done = bool(warm_up())" in plugin, (
        "the session-activation handler must record warm-up success and skip "
        "the retry once a widget exists"
    )
