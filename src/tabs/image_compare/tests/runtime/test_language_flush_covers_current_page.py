"""A live language switch must flush chrome for the page that is ALREADY current.

``flush_stale_workspace_language`` used to run on page activation only, and
``ImageCompareChromeSync.on_language_changed`` refreshed just the visible page
without marking the hidden one stale. Two holes in one contract:

* page current  -> the settings presenter's own chrome (interpolation combo,
  view buttons) and the PSNR/SSIM prefixes were never re-applied, because
  only the flush path calls ``settings_presenter.on_language_changed()``;
* page hidden   -> the stale gate was never marked, so the activation flush
  returned early and the page came back in the previous language.

Measured 2026-10-29 as part of the live-retranslation defect: a switch on the
open Image Compare page left those strings in English until a restart.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tabs.image_compare.use_cases.chrome_sync import ImageCompareChromeSync


def _sync(*, visible: bool):
    refreshed: list[str] = []
    settings_calls: list[bool] = []
    widget = SimpleNamespace(isVisible=lambda: visible)
    store = SimpleNamespace(
        settings=SimpleNamespace(current_language="ru"),
        state_changed=SimpleNamespace(connect=lambda cb: None),
    )
    settings_presenter = SimpleNamespace(
        on_language_changed=lambda: settings_calls.append(True)
    )
    wp = SimpleNamespace(get_feature=lambda name: settings_presenter)
    # NB: QObject.__init__ rejects a SimpleNamespace parent — construct
    # parentless, then attach the fake widget (prod always passes a QWidget).
    sync = ImageCompareChromeSync(None, store, resolve_window_presenter=lambda: wp)
    sync.widget = widget
    sync._refresh_visible_workspace_language = (
        lambda window_presenter, lang: refreshed.append(lang)
    )
    return sync, wp, refreshed, settings_calls


def test_language_switch_on_the_current_page_flushes_the_settings_chrome():
    sync, wp, refreshed, settings_calls = _sync(visible=True)

    sync.on_language_changed(wp)

    assert refreshed == ["ru"]
    assert settings_calls == [True], (
        "a live switch on the already-current page must reach the settings "
        "presenter chrome — that call lives in the flush path"
    )
    assert sync._workspace_language_stale is False


def test_language_switch_on_a_hidden_page_marks_it_stale_for_activation():
    sync, wp, refreshed, settings_calls = _sync(visible=False)

    sync.on_language_changed(wp)

    assert refreshed == []
    assert settings_calls == []
    assert sync._workspace_language_stale is True, (
        "a page stacked away at switch time must be marked stale, or the "
        "activation flush returns early and the page stays in the old language"
    )
