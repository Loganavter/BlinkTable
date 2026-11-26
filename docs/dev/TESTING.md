# Testing

Guide to test layout, how to run tests, and how to write new ones.

## Running tests

```bash
# from the repository root
pytest                                   # full suite (top-level tests/ + all src/tabs/*/tests/)
pytest tests/contracts                   # cross-cutting architectural contracts only
./launcher.sh test tests/contracts -q    # same, via launcher (preferred for agents)
pytest src/tabs/image_compare/tests      # image_compare tab tests only
pytest src/tabs/multi_compare/tests/render -q
pytest -k divider                        # all tests whose name contains divider
pytest tests/runtime/test_event_bus_depth.py::test_circular_chain_stops_at_max_depth
```

## Tiers: which slice to run when

The full suite is **10 562 tests, ~9 min** (measured 2026-10-27); the contract
tier alone is **~2:18** (same measurement). Running all of it for a one-module
edit is the wrong default — but running *nothing* is worse. The rule is: **the
smaller the blast radius you can prove, the smaller the run you may skip.**

### 1. Tier markers (by location, not by hand)

`pytest.ini` registers one marker per test folder; the **root `conftest.py`**
attaches it in `pytest_collection_modifyitems` from the table `TIER_MARKERS`.
No per-file `pytestmark` — a new test inherits its tier from the folder it is
created in, and a moved file cannot keep a stale marker.

| Path | Marker | Command |
|---|---|---|
| `tests/contracts/` | `contracts` | `pytest tests/contracts -m contracts` |
| `tests/devtools/` | `devtools` | `pytest tests/devtools -m devtools` |
| `tests/plugins/` | `plugins` | `pytest tests/plugins -m plugins` |
| `tests/runtime/` | `runtime` | `pytest tests/runtime -m runtime` |
| `tests/render/` | `render` | `pytest tests/render -m render` |
| `tests/ui/` | `ui` | `pytest tests/ui -m ui` |
| `tests/video/` | `video` | `pytest tests/video -m video` |
| `tests/target_bug/` | `target_bug` | `pytest tests/target_bug -m target_bug` |
| `src/tabs/**/tests/**` | `tabs` | `pytest src/tabs -m tabs` |

Files directly under `tests/` (`test_*.py` app-shell modules) and helpers carry
**no** tier marker on purpose: no folder owns them and they must not be claimed
by a neighbour's marker — they belong to the full run. `qt` stays owned by
`pytest-qt`; `slow`/`flaky` stay per-file (below).

### 2. Affected tests from the import graph — `tools/test_affected.py`

The tier markers bound a *folder*; they do not answer "which tests import the
file I just edited". `tools/test_affected.py` does, from the import graph
(`ast`, no imports executed, no plugins, no network, ~4 s over 2.5k modules):

```bash
python3 tools/test_affected.py src/tabs/image_gallery/services/folder_scanner.py
python3 tools/test_affected.py --since origin/main            # git diff --name-only
python3 tools/test_affected.py --since origin/main --explain   # + the import chain
python3 tools/test_affected.py --depth 1 <module>              # direct importers only
```

Output contract: one repo-relative test path per line, sorted; `--explain` adds
an indented `why: a.py -> b.py -> test.py` chain per test. Exit codes:

| Code | Meaning |
|---|---|
| `0` | the printed list is the affected set (may be empty; skips are explained on stderr) |
| `2` | **full tier required** — stdout starts with `full-tier:` (fail-closed) |
| `1` | usage / `git` error |

Fail-closed set (a change here means "run everything", never a short list):
`src/core/**`, `src/engine/**`, `src/shared/**`, `src/main*.py`,
`src/__main__.py`, **any** `conftest.py`, `pytest.ini`, `requirements*.txt`,
`tests/helpers/**`, `tests/fixtures/**`, and the tool itself. Changes under
`docs/**` and `src/tabs/*/docs/**` map to
`tests/devtools/test_docs_link_graph.py` by an explicit path rule (that test
reads the docs as text; nothing imports them).

Known limits, stated rather than hidden: dynamic imports
(`importlib.import_module("x")`) and string references are invisible to `ast`;
non-Python resources with no path rule print a `note:` and contribute nothing.

### 3. Policy — what to run for what

| Moment | Run | Why this and not more |
|---|---|---|
| Edit inside one module / one tab | `python3 tools/test_affected.py <files>`; if exit 2 or the list looks wrong, the tab folder instead: `pytest src/tabs/<tab>/tests -q` | The graph bounds it to tests that actually import the changed module (2 hops up). |
| Before `git push` (pre-push) | `tools/test_affected.py --since origin/main` **plus** `pytest tests/contracts -q` | Contracts are the architectural gate: a feature that violates a dogma goes red here, and a subset run would not see it (~2:18, cheap next to the 9-min full run). |
| Merge / pre-merge (supervisor) | `pytest -q` (full — parallel is now the DEFAULT, `pytest.ini`) | Cross-cutting breakage lives in suites the import graph cannot see (Qt plugin/fixture discovery, session order, `slow` whole-tree scans). One parallel full run ≈ the local merge-readiness signal; CI is the async backstop. |
| Nightly / release | `pytest -q` + `pytest -m "slow" -q` separately, and the toolkit pair | Nightly wants the unparallelized signal (order + timing flakiness) and the slow scans measured on their own; release additionally pins the sibling toolkit commit. |

### 3a. The parallel mode — measured, OPT-IN, and why it is not the default

`addopts` runs the tier **serial** (one process, one order — that is what makes the
pinned `--randomly-seed` mean something). The parallel run is a fast MODE, invoked
explicitly:

```bash
pytest <scope> -q -n 4 --dist loadfile --timeout=300
```

Measured on this host 2026-10-28, per scope, identical result sets:

| scope | serial | `-n 4 --dist loadfile` |
|---|---|---|
`tests/contracts` | 185 s | **66 s** |
`src/tabs/image_gallery/tests` | 196 s | **56 s** (4/4 runs green) |

**Why it is not the default** (corrected the same day, within the hour): the FULL
tier under `-n 4` showed five cross-file leaks that only a multi-worker run exposes —
`tests/devtools/test_control_discovery.py` (two pre-rename fallbacks),
`tests/devtools/test_mcp_live_attach.py` (pre-rename data dir),
`tests/runtime/test_stall_watch.py` (a real GUI block) and
`tests/runtime/test_dialog_title_bar_nav.py` (Help title bar). All five are green alone
AND green together in ONE serial process, so they are worker-state pollution of the
same class toolkit #129 fixed, not product bugs. A gate that flakes is worse than a
slower gate.

`--dist loadgroup` (one TEST per worker) is also not usable: its dynamic scheduling
makes the ORDER itself non-reproducible — two runs of the same pinned seed failed on
two different tests. If the parallel mode ever becomes the default, the unit has to be
the FILE, and the five leaks have to be fixed first.

### 4. Duration markers (unchanged by the tier table)

Markers are registered in `pytest.ini` (`slow`, `flaky`). Its one `addopts`
is `--randomly-seed=20261021`: `pytest-randomly` stays on (order bugs still
surface) but the shuffled order — and therefore the failure set — is
reproducible. Duration tiers are opt-in via `-m`:

```bash
pytest tests -m "not slow" -n auto -q     # fast tier: everything except the
                                          # whole-tree scans (14 files / 44 tests); PR default
pytest -n auto -q                        # full suite: tests/ + src/tabs/*/tests/ (nightly / pre-merge)
pytest -m "slow" -q                       # slow scans only
```

`slow` is assigned per-file (`pytestmark`) to whole-tree scans taking
20 s+ (durations 2026-09-22: settings-mutation 64 s, paint-scale, i18n,
upload-queue, geometry, fullscreen, capability-ids, store-mutation,
draw-plan-math, widget-ownership, settings-persistence, activation,
viewport-slots). A new test that scans all of `src/` and exceeds ~20 s
must carry the mark. It is deliberately **not** derived from a path table:
duration is a property of what a test does, and the local timing journal
(`.git/test-journal.jsonl`) is untracked per checkout, so there is nothing
for the tier table to read on a fresh clone.


### Quarantine (`flaky`)

Timing-sensitive tests that fail only under parallel load are marked
`flaky` and stay in the suite (no skipping — visibility over silence):

| Test | Symptom |
|---|---|
| `tests/contracts/test_chrome_lifetime.py::test_reveal_pulse_exception_swallowed` | `QTimer.singleShot(0)` pulse may not fire before teardown under `-n 8` load; 3/3 green alone. Fix direction: explicit signal wait instead of `delay_ms=0` (see the internal notes on KNOWN_BUGS Qt quirks). |
| `src/tabs/session_picker/tests/runtime/test_keyboard_navigation.py::test_down_from_last_header_enters_recent_items` | Offscreen focus handoff (header → recent items) can be dropped under full `-n` load (whole tree in one session); green in the tab suite alone. Hardened to `widget.focusWidget()` + `qtbot.waitUntil`; residual is Qt-offscreen focus timing, not a product bug. |
| `src/tabs/session_picker/tests/runtime/test_session_picker_refresh_focus.py::test_tab_stores_widget_and_hooks_are_order_safe` | Tab lifecycle + focus timing under parallel load; green alone. |
| `src/tabs/session_picker/tests/runtime/test_keyboard_navigation.py::test_up_from_first_recent_item_returns_to_last_create_card` | Same offscreen focus handoff as the Down case; fails only in full-suite order (journal `1bf913a7` and 2026-10-14), green in isolation. |
| `src/tabs/image_gallery/tests/isolation/test_algorithms_action_issues_no_post.py::test_algorithms_action_issues_no_post` | **Order-dependent, not timing**: green serially, in its own file and under the tier default (`--dist loadfile`, 4/4 runs); fails in SOME orders when xdist schedules one TEST per worker and each worker shuffles with its own seed (measured 2026-10-28: the failure set CHANGED between two runs of the same pinned seed — this test once, `test_grid_reflows_during_analysis` the next — so there is no single polluter to bisect). |
| `src/tabs/image_gallery/tests/runtime/test_grid_reflows_during_analysis.py::test_a_resize_does_not_turn_a_chunked_build_into_a_full_build` | Same measurement and same order class as the row above; the counter it asserts (a chunked build must not become a full build) is state from an earlier file in the same worker. |
| `tests/devtools/test_control_nav_state.py::test_nav_key_on_an_unowned_widget_is_reported_unhandled` | Global `NavigationManager` state left by a previous test makes the key report `surface` instead of `unhandled/focus`; fails only in full-suite order (journal `1bf913a7`), green in isolation. |

These stay in the fast tier (no deselect): the list is documentation, not
silence. `test-journal.sh` carries the per-commit evidence for each.

### Segfault attribution (CI)

Ubuntu CI dies ~2× with `Fatal Python error: Segmentation fault` inside
`pytestqt/plugin.py _process_events` (Qt loop under xdist; not reproducible
on Termux). `pytest.ini: faulthandler_timeout = 120` prints the Python stack
on the next crash — read it before quarantining anything. Prime suspects
(CI hardening epic, queue `task-ci-suite-hardening-2026-09-22`, F2 recon): unguarded
`singleShot` emitters firing post-teardown (`palette/dialog.py`
`_accept_and_run` now `isValid`-guarded — same precedent as the toolbar
shiboken guards) + tests without a `DeferredDelete` sweep
(`test_flyout_fade` `_teardown` now drains deterministically, mirroring the
`tests/runtime` sweep fixture). Rule: guard the callback (`isValid`), never
`sleep()`-retry the test.

## Live-translation coverage (language bindings)

A live language switch re-applies text only through `sli_ui_toolkit.i18n`
bindings (`translatable_text` / `_tooltip` / `_placeholder` / `_callback`) or
through an explicit `language_changed` subscription. A widget that resolves
its text once with a bare `tr(key)` keeps the language it was built in — the
catalogs pass `check_translations.py` while the UI stays English until a
restart (measured 2026-10-29: the Image Compare toolbar and the onboarding
overlay).

`src/devtools/check_language_bindings.py` gates that class statically over
`src/ui/**`, `src/plugins/**`, `src/tabs/*/ui/**`, `src/tabs/*/widget.py` and
`src/tabs/*/plugins/**`: a module calling `tr(` is a violation unless it also
uses a `translatable_*` helper, connects to `language_changed` / defines
`on_language_changed`, or carries a measured entry in
`src/devtools/language_bindings_allow.txt` (`path<TAB>reason`, relative to the
repo root). The allowlist is a **ratchet**: an entry whose file is no longer a
violation is itself a violation, so the list can only shrink — add a line only
after reading the call site. Rerun after touching widget text:

```bash
python3 src/devtools/check_language_bindings.py            # whole tree, exit 0 = clean
python3 src/devtools/check_language_bindings.py --selftest # the gate's own fixtures
```

The runtime half of the same rule is
`tests/runtime/test_live_language_switch_retranslate.py` (bound text follows
`emit_language_changed`; the onboarding overlay is the red-before surface).

## Test-run journal (baseline before "is it new?")

When a suite fails, the first question is "new or pre-existing?" — do not answer
it by re-running the parent commit by hand. `test-journal.sh` (packed in the
internal-docs `machine/bin/`) records runs keyed to the commit:

- `test-journal.sh run <repo> [--scope NAME] [-- <paths/args>]` after any
  non-trivial run (a whole-suite run with no paths is refused unless `--full`);
- `test-journal.sh import <repo> <commit> <log>` to backfill an existing log;
- `test-journal.sh query <repo> --test <substr>` / `last <repo> --test <substr>`
  for the per-commit history of one test;
- `test-journal.sh baseline <repo> --ref REF` (machine-readable lookup that
  acceptance uses, `delta:` when `--current` counts are supplied) and
  `show <repo> [N]` (recent records).

The journal is `<repo>/.git/test-journal.jsonl` (untracked). A failure present at
the pre-change commit is pre-existing; an isolated-green vs a combined-run fail is
a flake, not a regression — record both scopes. This also shortens the segfault
attribution above: record the crashing scope once, then compare commits by lookup.

## Static type checking (mypy)

`mypy` is wired in as a dev dependency (`requirements-dev.txt`) with
`mypy.ini` at the repository root. The codebase is expected to be clean:

```bash
mypy                      # checks src/ (config: files = src, mypy_path = src)
```

Run it locally before pushing; it is not currently wired into the app CI
workflows (the sibling `sli-ui-toolkit` repo runs mypy in its own CI).
Notes for contributors:

- Packages are rooted at `src/` (app runs with `PYTHONPATH=src`), so
  `mypy.ini` sets `mypy_path = src` and `files = src`.
- `follow_imports = skip` keeps the error surface to each module itself;
  third-party stub packages (PySide6, shiboken6, PIL, numpy, pyvips) are
  overridden to `follow_imports = normal` so their real stubs stay live.
- Qt dynamic attributes (e.g. `dialog._csd_decorating`, `widget.runtime_state`)
  and test monkeypatches use targeted `# type: ignore[...]` with a comment;
  `warn_unused_ignores = True` keeps those honest.
- `# type: ignore[call-overload]` on `bytes(QByteArray)` /
  `QImage.save(..., "PNG")` mark PySide6 stub quirks (runtime accepts `str`
  formats, stubs type them as `bytes`); keep the runtime call unchanged.
- Platform quirks: `winreg`/`ctypes.WinDLL` on Linux are silenced per-module
  (`disable_error_code = attr-defined` for
  `services/system/windows_file_association.py`), `sys._MEIPASS` gets an
  inline ignore (PyInstaller-only).
- Running `mypy <single file>` is misleading for cross-module errors —
  always confirm with a full `mypy` run.

For top-level `tests/`, `sys.path` for `src/` is added automatically via
`tests/conftest.py`. Tab tests under `src/tabs/<tab>/tests/` do not need that:
from `src/` down to the test file is an unbroken chain of packages with
`__init__.py`, so pytest inserts `src/` into `sys.path` when building the
rootdir package (standard prepend-import-mode). `sli-ui-toolkit` is installed
as an external dependency from `requirements-gui.txt`; there is no local
fallback path to a vendored toolkit. App tests cover app integration with the
toolkit, not the external library's public API.

The repository uses a minimal `pytest.ini` (marker registration +
`faulthandler_timeout` + the pinned `--randomly-seed` described above) —
discovery follows pytest defaults. Tier markers are attached by the root
`conftest.py` from the path table `TIER_MARKERS` (it does nothing else);
`tools/test_affected.py` is the import-graph selector described in § Tiers.

## Layout

Tests are organized along two axes: **check type**
(contracts/render/runtime/plugins) within **owner** (app-wide code in `tests/`,
tab-specific code in `src/tabs/<tab>/tests/`).

**Ownership rule:** if a test imports `tabs.<tab_name>.*` (including lazily,
inside the test function body) and asserts behavior of that tab specifically —
it lives in `src/tabs/<tab_name>/tests/`, not in `tests/`. If a test exercises
a shared mechanism (tab registry, `TabContract`, Feature State API, event bus,
stacking policy, plugin isolation, etc.) — it stays in `tests/`, even when it
instantiates a concrete tab as an example. Folder structure details —
[tabs/isolation.md](tabs/isolation.md).

| Folder | What it checks | Style |
|---|---|---|
| `tests/contracts/` | **Architectural dogmas** (AST), not the interface field catalogs. Isolation rules such as “no direct feature imports”. See [CONTRACTS.md](CONTRACTS.md#three-senses-of-contract); feature layout also in [rendering/index.md](rendering/index.md). | Static analysis |
| `tests/runtime/` | Shared import/behavior checks: registry, event bus, stacking policy, presentation isolation, graceful degradation. Not the same as render-pass *lifecycle* docs. | Import + assertions |
| `tests/render/` | Shared composition/render-plan behavior, not tied to a specific tab. | Fake-context |
| `tests/plugins/` | Plugin behavior (`src/plugins/*`) not owned by one tab: settings, help, clipboard, workspace chrome. | Unit/integration |
| `tests/devtools/` | Developer tooling: docs link graph, UI-inspector wiring, capture-surface/help-figure gates, control-surface (MCP) routing, launcher checks. | Mixed |
| `tests/*.py` (top level) | App-shell mechanics with no subfolder of their own: navigation manager/coverage, context menu, flyout fade, session state. No tier marker (see § Tiers). | Unit/integration |
| `tests/{video,ui,target_bug}/`, `tests/helpers/` | Remnants and support: `video/` keeps one module (the video suite moved to the image_compare tab), `ui/` one dialog-layer test, `target_bug/` one regression, `helpers/` importable fixtures (no `test_*.py`). | Mixed |
| `src/tabs/image_compare/tests/{contracts,render,runtime,plugins,video}/` | Everything specific to image_compare (canvas features, magnifier, divider, video editor). | Mixed — see [tabs/isolation.md](tabs/isolation.md) |
| `src/tabs/multi_compare/tests/{contracts,render,runtime,plugins}/` | Everything specific to multi_compare (composition, dividers, labels, context menu). | Mixed — see [tabs/isolation.md](tabs/isolation.md) |

Shared helper for contract tests in `tests/` —
`tests/contracts/_framework.py`: paths (`SRC`, `CANVAS_FEATURES`, `PLUGINS`),
`iter_py`, `read`, `module_imports`, `list_canvas_features`, `list_plugins`,
`feature_name`. Use it instead of hand-rolled `Path(__file__).parent…`. Tab-specific
contract tests do not import this helper directly —
`src/tabs/image_compare/tests/contracts/_framework.py` keeps a narrower copy
with paths recomputed for `src/tabs/<tab>/tests/contracts/` depth so the tab
folder stays self-contained.

## File catalog

The load-bearing files, not every file — so you do not have to read sources one
by one. Each test's dogma is the first line of its docstring, so the catalog can
be regenerated from the tree (`grep -rn '^"""' tests/<dir>`); it is not a
complete inventory. Below covers only `tests/` (cross-cutting contracts). For a
specific tab's catalog, look next to that tab:
`src/tabs/image_compare/tests/`, `src/tabs/multi_compare/tests/` (the docstring
first line plays the same role — we do not duplicate a per-tab table here).

### `contracts/` — structural dogmas (AST scan, no runtime)

| File | What it guards |
|---|---|
| `test_canvas_features_manifest.py` | Every feature under `ui/canvas_features/` exports `WIDGET_FEATURE` with `name`; names are unique. |
| `test_canvas_features_imports.py` | Shared code does not import `tabs.image_compare.canvas.features.<name>` directly. |
| `test_canvas_features_layout.py` | No feature-named helpers in `canvas_presentation`; a feature does not reintroduce its own render pipeline. |
| `test_canvas_features_render_passes.py` | Render passes declare `stack_role`, do not hardcode layer/priority; shared `shader_sources` has no feature shaders. |
| `test_canvas_widget_ownership.py` | Magnifier/toolbar code lives under `tabs.image_compare.*`, not in the shared canvas layer (path-based check). |
| `test_canvas_content_geometry_single_owner.py` | Single owner of content-geometry math; no direct canvas-bounds access outside the allowlist. |
| `test_events_no_feature_branching.py` | `mouse.py`/events do not branch on feature flags or call feature aliases directly. |
| `test_no_manual_theming.py` | No manual theming calls outside theme infrastructure. |
| `test_no_system_tooltips.py` | No system Qt tooltips bypassing the shared tooltip interceptor. |
| `test_plugins_structure.py` | Every plugin: entry point, decorator+base, name matches folder, names unique. |
| `test_plugins_isolation.py` | A plugin does not import canvas features or other plugins' internals. |
| `test_platform_isolation.py` | Platform (non-tab-specific) code does not mention or import concrete tabs directly. |
| `test_tabs.py` | Every tab under `src/tabs/` is a filled `TabContract`; `session_type` is unique. |
| `test_tabs_isolation.py` | Tab does not import app i18n/theme/`ui.icon_manager`; JSON translations use only its own namespace. |
| `test_tab_icons.py` | Tab with `icons.py` has SVGs in `resources/icons/{light,dark}/` for every `Icon` member; resolve is non-null. |
| `test_viewport_state_slots.py` | `ViewportState` is slotted (no `__dict__`); `overlay_clip_rect` lives in runtime cache; writing it on `ViewportState` fails. |

### `runtime/` — import/runtime contracts

| File | What it guards |
|---|---|
| `test_interaction_contracts.py` | Event layer isolated from features; interaction aliases resolve; hit-test pipeline filled with callables. |
| `test_shared_presentation_isolation.py` | `shared/` and plugins use aliases only; Phase-5 aliases resolve. |
| `test_event_bus_depth.py` | EventBus depth guard: cyclic chain stops at `MAX_EMIT_DEPTH`; counter resets between top-level emits. |
| `test_plugin_graceful_degradation.py` | Empty `plugins/` and unknown alias do not break bootstrap (graceful degradation). |
| `test_tabs_lifecycle.py` | Drop routed via `accepts_drop`; `dispose()` is idempotent (generic registry mechanism; uses `ImageCompareTab` only as example). |
| `test_keyboard_movement_contracts.py` | Keyboard movement gestures not tied to a specific tab. |
| `test_dialog_auto_decoration.py` | Dialog auto-decoration (icons/title bar) applied at shared host level. |
| `test_main_window_resize_runtime.py` | Main window resize not tied to a specific tab. |
| `test_tooltip_interceptor.py` | Shared tooltip interceptor captures hints for all widgets, including tab bar. |
| `test_language_broadcast.py` | Language change broadcast to all i18n widgets via `resources.translations`; host sets `store` before `setupUi`. |

### `render/` — shared composition/render-plan behavior (fake-context)

| File | What it guards |
|---|---|
| `test_composition_plan.py` | Shared composition-plan assembly not tied to a specific tab. |
| `test_plan_applicator_composition.py` | Applicator combines composition plans correctly regardless of tab. |
| `test_qrhi_backend_selection.py` | QRhi backend selection is a shared mechanism, not tab-specific. |
| `src/tabs/image_compare/tests/render/test_qrhi_canvas_resize_contract.py` | QRhi canvas resize delegates to shared resize-geometry pipeline. |

### `plugins/` — plugin behavior not owned by one tab

| File | What it guards |
|---|---|
| `test_settings_controller.py` | `apply_font_settings` batches and caps; skips cap when unchanged. |
| `test_clipboard_paste_shortcut.py` | Ctrl+V on canvas emits paste event. |
| `test_help_dialog_anchors.py` | Help: anchor suffix strip, heading-id generation, TOC from h3 sections. |
| `test_image_properties.py` | Image properties readable regardless of active tab. |
| `test_main_window_canvas_theme_background.py` | Theme change updates canvas container background and placeholder at host level. |
| `src/tabs/image_compare/tests/plugins/test_main_window_save_button.py` | Main-window save button is host chrome, not tab-specific. |
| `test_settings_dialog_geometry.py` | Settings dialog geometry. |
| `test_workspace_session_menu_translations.py` | Workspace session menu uses host-level translations. |
| `test_workspace_tab_close.py` | Closing last workspace tab closes main window (workspace host mechanics, not a specific tab). |
| `test_workspace_tabs_layout.py` | `workspace_tabs` layout (QTabBar) — host tab-management widget, not a tab itself. |

## Two kinds of tests

### 1. Architectural contracts (`tests/contracts/`, `src/tabs/*/tests/contracts/`)

This is the **architectural dogma** sense of “contract” in
[CONTRACTS.md](CONTRACTS.md#three-senses-of-contract) — not `TabContract` /
`CanvasWidgetFeature` field lists. Do not launch the app — parse sources and
enforce discipline:

- `test_canvas_features_manifest.py` — every feature under `src/ui/canvas_features/` exports `WIDGET_FEATURE` with `name`.
- `src/tabs/image_compare/tests/contracts/test_canvas_features_aliases.py` — capability aliases declared and unique.
- `test_canvas_features_imports.py` — features do not import each other directly (communicate via Store/EventBus/aliases).
- `test_canvas_features_layout.py`, `test_canvas_features_render_passes.py` — required submodules and pass signatures.
- `test_plugins_structure.py`, `test_plugins_isolation.py` — same for `src/plugins/*`.

Parameterize such tests via `list_canvas_features()` / `list_plugins()` and
`pytest.mark.parametrize(..., ids=...)` — then a new feature automatically
falls under all rules without editing tests.

### 2. Behavior contracts (`tests/render/`, `tests/runtime/`, `src/tabs/*/tests/{render,runtime,video}/`)

Import runtime classes and build minimal context by hand — no Qt application,
no QRhi/GPU context. Pattern: `SimpleNamespace` instead of mocks:

```python
from types import SimpleNamespace

def _build_ctx(*, show_divider, thickness, images_uploaded, content_rect):
    return SimpleNamespace(
        widget=SimpleNamespace(width=lambda: 100, height=lambda: 100, runtime_state=None),
        images_uploaded=list(images_uploaded),
        scene_frame=SimpleNamespace(
            feature_payloads={"show_divider": show_divider, "divider_thickness": thickness},
            is_horizontal=False,
            content_rect_px=content_rect,
        ),
    )
```

Assert *what* a pass does (did it call the painter, what commands did it record),
not *how* — without a Qt window or real QRhi rendering.

## Rules for writing new tests

1. **One test per contract, not per feature.** If a rule applies to all features — write a parameterized test in `tests/contracts/` that automatically covers future features.
2. **One file — one topic.** Name like `test_<thing>_contracts.py` for contract tests, `test_<feature>.py` for behavioral tests.
3. **No Qt/QRhi in unit tests.** If a test needs `QApplication` — that is integration; place it next to the subsystem under test and create `QApplication.instance() or QApplication([])` explicitly in a fixture.
4. **`SimpleNamespace` over `MagicMock`.** Mocks only fail when invoked; namespaces fail at `AttributeError`. Tests should fail on missing contract fields, not pass silently.
5. **Document the dogma in the docstring.** First line — link to a section in `docs/dev/`, so when a rule changes it is clear which doc to update with the test.
6. **Do not mock Store.** To test a reducer — dispatch a real action into a real store; for a pass — build `scene_frame` by hand.
7. **Agent-written tests are low-trust; require a falsification story.** A new regression test must be shown failing before the fix (a test green on both sides proves nothing). Prefer human-authored contracts, ground-truth harnesses, and content-identity assertions over agent-generated parity that merely restates the implementation. Rationale: the 2026-10-08 agent-tests study (agent-written unit/integration tests did not improve outcomes) and our own `~180 green tests with swapped previews` lesson — see `improve-imgsli-internal-docs` `inv-test-suite-state-2026-10-09`.

## Hermeticity (tests must never touch real user data)

Every test must run without reading or writing the developer's / CI runner's
real profile. This is enforced by the shared redirect fixtures in
`tests/conftest.py` / `src/tabs/conftest.py` (QSettings `NativeFormat` +
`IniFormat` paths, `TiledPixelStore` spill dir, theme/tab singletons) — there
is no separate hermetic CI job. Observed incidents that shaped these rules:

- `QSettings("org", "app")` resolves with **`NativeFormat`** on Linux, and the
  2-arg constructor ignores `setDefaultFormat`. Redirecting only the
  `IniFormat` path silently does nothing, and full-snapshot saves landed in
  the real `~/.config/improve-imgsli/improve-imgsli.conf`, wiping user
  settings on every test run. **Any redirect fixture must set
  `setPath(NativeFormat, …)` AND `setPath(IniFormat, …)`** (pattern:
  `tests/runtime/test_settings_full_pass.py::_redirect_qsettings`).
- `TiledPixelStore` / `PyramidPixelStore` without `tmp_dir` create real
  spill/memmap dirs under the cache location. The shared fixture in
  `tests/conftest.py` / `src/tabs/conftest.py`
  (`_redirect_tps_spill_to_tmp`) covers this for the whole suite; a test
  passing an explicit `tmp_dir` wins over it.
- Unparented `QTimer.singleShot(0, …)` callbacks must guard the captured
  object with a shiboken `isValid` check — firing against a destroyed widget
  raises inside the event loop and poisons unrelated later tests
  (pattern: `session_picker/recent/items_view.py::request_window_chrome_refresh`).

Rule of thumb: anything disk-based goes through `tmp_path`/`tmp_path_factory`;
any singleton mutation is restored in a fixture `finally`; no test may write
to `Path.home()`, `QStandardPaths`, or a bare `QSettings(org, app)` without a
redirect fixture active.

## One owner per invariant

An invariant (a roundtrip fixpoint, a layout contract, a stack role…) has
exactly **one** owning test. If a generic sweep covers a value, specific
tests must not re-assert the same value on the same inputs — the pair
diverges when the contract changes and the sweep is edited without touching
the copy. When adding a test, check whether a sweep already covers the case
(`settings_full_pass`, parity matrices, feature-property sweeps) and either
extend the sweep or leave the case to it.

## Dead tests

A test that cannot fail is worse than a missing test (false confidence).
Before adding a test, make sure it contains at least one assertion that can
actually fail (no assert-less AST scan exists in the suite — this stays a
review rule). If a specific test's assertions are fully subsumed by a
stronger sibling, delete the weaker one — do not keep it "for documentation".

## When tests break

- Failure "new vs pre-existing" — answer with the **test-run journal**
  (`test-journal.sh query|last`), not a hand re-run of the parent commit (see below).
- Contract test fails after adding a feature/plugin — the feature violates the dogma (`docs/dev/rendering/index.md` and the page it
  points at for the changed subsystem, or `CONTRACTS.md`). Fix the feature, not
  the test.
- Render contract fails — pass behavior changed. If intentional, update the test docstring and assertion in one change.
- Import failure — check that new code does not import Qt/QRhi at module top level. Use lazy import inside functions.

## What is NOT covered

- Real QRhi rendering — shader pixels (needs GPU context; backend- and driver-dependent). The seam around QRhi (render plan, `render_scene` params, fake-GPU payload) is covered; see `src/tabs/image_compare/tests/video/test_video_export_preview_parity_matrix.py`.
- **Live latency gate** — the video editor's strip is the one path held to a number instead of a render test: `src/devtools/bench_strip_latency.py` (50 ms fill / 10 ms interaction, on a leased display; see `src/tabs/image_compare/docs/plugins/video_editor.md` § Strip latency gate). Its structural half — no timer/settle gate/sleep on the strip path — is `contracts/test_strip_no_debounce.py`.
- Multi-window scenarios and OS drag-and-drop (**transport**). Drop routing (`route_drop`/`accepts_drop`) is covered in `contracts/test_tabs.py` + `runtime/test_tabs_lifecycle.py`.
- **Transport** of interactive gestures — live mouse/trackpad event delivery. Gesture **resolution** (priority, `matches`/`is_active` predicates) is covered in `src/tabs/image_compare/tests/runtime/test_gesture_resolution.py`.

For those checks — manual run via `python -m src` or the `verify` skill.
