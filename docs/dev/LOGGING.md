# Logging

Host-specific logging setup. Shared conventions (stdlib `logging` only, unique-prefix recipe, temporary-diagnostics workflow, silencing) live in the canonical doc: [sli-ui-toolkit logging conventions](https://github.com/Loganavter/sli-ui-toolkit/blob/main/docs/dev/LOGGING.md).

## Setup

| Path | Role |
|---|---|
| `sli_ui_toolkit/core/logging.py:setup_logging` | Builds the `"BlinkTable"` logger: stream handler (**stderr**) + file handler (per-OS data dir), uniform formatter. The logger name is also the log-directory name — one string drives both |
| `src/core/log_names.py` | The one spelling: `APP_NAME`, `APP_LOGGER_NAME` (`"BlinkTable"`), plus `LEGACY_LOGGER_NAME` / `LEGACY_LOG_DIR_NAME` used **only** by the log-dir migration |
| `src/core/log_dir_migration.py:migrate_log_directory_once` | Called by `_configure_logging` **before** `setup_logging`: aliases the pre-rename app-data directory once (see below) |
| `src/core/bootstrap.py:_configure_logging` | `migrate_log_directory_once()` then `setup_logging(APP_LOGGER_NAME, effective_debug, "IMPROVE_DEBUG")` at startup |
| `src/core/startup_trace.py` | Optional phase timing when `IMGSLI_STARTUP_TRACE=1` (logger name `BlinkTable.startup`); also the `early_mark` facade over the pre-logging file sink |
| `src/core/startup_early_log.py` | The **early sink**: `startup_early.log`, written before logging exists (see [Early sink](#early-sink-the-window-before-logging-exists)) |
| `src/__main__.py` | CLI flags `--debug` / `--enable-logging` / `--disable-logging`; emits the pre-window marks |

Log file location (cleared on every start, `mode="w"`):
- Linux: `${XDG_DATA_HOME:-~/.local/share}/BlinkTable/log.txt`
- macOS: `~/Library/Application Support/BlinkTable/log.txt`
- Windows: `%APPDATA%/BlinkTable/log.txt`

`startup_early.log` lives in the same directory and is wiped on every **traced**
start (`IMGSLI_STARTUP_TRACE` / `IMGSLI_STALL_WATCH`); it is not created at all
otherwise.

### One-time import of the pre-rename directory

Builds before the rename wrote to `…/ImproveImgSLI`. On the first start of a
renamed build `migrate_log_directory_once()` runs before logging is configured:

| State on disk | Action | Return |
|---|---|---|
| new absent, old present, POSIX | `new` becomes a **symlink** to `old` (old is never moved — a live process may hold it open) | `linked` |
| new absent, old present, Windows | `old` is **renamed** to `new` (only when `new` is absent) | `renamed` |
| new present | left untouched — a real directory always wins | `kept` |
| new present but dangling (its legacy target was deleted) | the dead link is dropped, then the rows above apply — otherwise `setup_logging` could not create a file behind it | `none` / `linked` |
| neither, or anything failed | nothing created, nothing deleted | `none` |

It never raises and never deletes or overwrites anything; a failed migration
simply means the next run starts with a fresh log directory. Old paths keep
working because they are the same tree (`linked`/`renamed`), so scripts and
docs that still spell the legacy directory need no update.

Format:
```
2026-06-26 11:31:45,313 - [DEBUG] - (rhi_renderer.py:54) - [rhi-render-debug] render begin ...
```

## Log level — how it's resolved

`setup_logging` picks `DEBUG` vs `INFO` based on, in order:
1. `IMPROVE_SUPPRESS_DEBUG=1` env var → force `INFO` (override switch off).
2. `IMPROVE_DEBUG=1` env var → force `DEBUG`.
3. CLI flag `--debug` → `DEBUG` for this session.
4. Persisted setting `debug_mode_enabled` (in QSettings, toggled via `--enable-logging` / `--disable-logging` or the Settings UI) → `DEBUG` or `INFO`.
5. Default → `INFO`.

The level applies to both handlers. Switching it from the Settings UI does
**not** re-level the running process: that path only dispatches
`SetDebugModeEnabledAction` (`src/plugins/settings/application_service.py`),
whose reducer replaces the persisted field; `setup_logging` is called only from
`src/core/bootstrap.py:_configure_logging` (twice, both during init). The
persisted flag therefore applies on the **next start** — `--debug` is the
per-session switch.

## Getting a logger

Always use the `APP_LOGGER_NAME` namespace (or a sub-logger of it). The root logger isn't configured, and no literal spelling is allowed — `tests/runtime/test_log_identity.py` fails the suite if a `getLogger("ImproveImgSLI")` appears anywhere under `src/`.

```python
import logging
from core.log_names import APP_LOGGER_NAME

logger = logging.getLogger(APP_LOGGER_NAME)             # default; most modules use this
logger = logging.getLogger(f"{APP_LOGGER_NAME}.rhi")    # sub-logger for a subsystem
logger = logging.getLogger(f"{APP_LOGGER_NAME}.plugin.lifecycle")
```

The legacy name stays in `LEGACY_LOGGER_NAME`/`LEGACY_LOG_DIR_NAME` only, and
only the migration module reads them.

Sub-loggers inherit handlers + level. They show up in the format as `[DEBUG]` etc., but the filename column already tells you which module produced the line.

## Levels — when to use what

| Level | Use for |
|---|---|
| `logger.debug(...)` | Verbose diagnostic detail. Only visible with `--debug` / `debug_mode_enabled`. Default for everything that doesn't matter to a normal user. |
| `logger.info(...)` | Significant lifecycle events (plugin initialized, theme changed, etc.). Visible by default. Use sparingly — info should be readable. |
| `logger.warning(...)` | Something is off but the app continues (missing file with fallback, deprecated path, slow operation). |
| `logger.error(...)` | A real failure that the user/dev needs to know about. Pair with `exc_info=True` if there's an exception. |
| `logger.critical(...)` | Rare. Reserved for app-fatal conditions. |

```python
logger.error(f"Plugin {name} failed during {stage}: {err}", exc_info=True)
```

## Shared conventions (canonical)

- Unique-prefix recipe and rationale — see the [canonical doc](https://github.com/Loganavter/sli-ui-toolkit/blob/main/docs/dev/LOGGING.md); host streams below follow it (env-gated helper + bracketed prefix + documented var).
- Never-`print()` rationale — see the [canonical doc](https://github.com/Loganavter/sli-ui-toolkit/blob/main/docs/dev/LOGGING.md); committed code uses `logging` so output flows through the configured handlers into `log.txt`.
- Temporary-diagnostics methodology — see the [canonical doc](https://github.com/Loganavter/sli-ui-toolkit/blob/main/docs/dev/LOGGING.md); add greppable `logger.debug`/`logger.warning` lines with a unique prefix, then remove them after the fix.
- Silencing theory — see the [canonical doc](https://github.com/Loganavter/sli-ui-toolkit/blob/main/docs/dev/LOGGING.md); for env-gated host streams unset the env var, or raise a sub-logger level locally:

```python
logging.getLogger(f"{APP_LOGGER_NAME}.rhi").setLevel(logging.WARNING)
```

Do **not** wire a noisy subsystem's debug stream to the global `debug_mode_enabled` switch — that turns one log file into white noise.

## Environment variable prefix — `BLINKTABLE_` first, `IMGSLI_` still honoured

The app's user-facing identity is **BlinkTable**, and its env knobs gained the
matching prefix. **Both spellings are supported for this release**, and the
legacy prefix is **not** deprecated silently:

| Precedence | Spelling | Meaning |
|---|---|---|
| 1 | `BLINKTABLE_<X>` | new spelling; wins when set and non-empty |
| 2 | `IMGSLI_<X>` | historic spelling; still read, same vocabulary |
| 3 | the flag's built-in default | |

One resolver owns this: `src/shared/debug_flags.py` (`ENV_PREFIX`,
`LEGACY_ENV_PREFIX`, `alias_name()`, `env_value()`, and the two predicates
`env_flag` / `env_flag_strict`, which both call `env_value()`). Every
`env_flag(...)` call site therefore honours the alias with no edit of its own;
the inline `env_value(...)` sites (path/int knobs, cached-at-import gates) do
the same. An empty or whitespace-only value counts as *unset*, so an empty
`BLINKTABLE_<X>` cannot shadow a real `IMGSLI_<X>=1`.

User-facing flags (control / tracing / startup — same rule for every row, and
for every `IMGSLI_*` flag in the registry below):

| Purpose | New spelling | Legacy spelling |
|---|---|---|
| in-app control channel | `BLINKTABLE_CONTROL=1` | `IMGSLI_CONTROL=1` |
| tracing | `BLINKTABLE_TRACE=1` | `IMGSLI_TRACE=1` |
| trace kind filter | `BLINKTABLE_TRACE_KINDS` | `IMGSLI_TRACE_KINDS` |
| trace kind skip list | `BLINKTABLE_TRACE_SKIP` | `IMGSLI_TRACE_SKIP` |
| startup phase timing | `BLINKTABLE_STARTUP_TRACE=1` | `IMGSLI_STARTUP_TRACE=1` |
| perf trace export | `BLINKTABLE_PERF_TRACE=1` | `IMGSLI_PERF_TRACE=1` |
| GUI stall watchdog | `BLINKTABLE_STALL_WATCH=1` | `IMGSLI_STALL_WATCH=1` |
| fault/hang dumps | `BLINKTABLE_FAULT_DUMP=1` | `IMGSLI_FAULT_DUMP=1` |

Not covered by the alias layer: `IMPROVE_DEBUG` / `IMPROVE_SUPPRESS_DEBUG`
(toolkit-owned log-level switches, read by `setup_logging`) and the
`IMPROVE_IMGSLI_*` RHI switches — they keep their existing names.

## Host subsystem debug streams

Env-gated streams following the canonical unique-prefix recipe:

- `IMGSLI_RESIZE_DEBUG` → `[rhi-render-debug]` (`src/engine/cache/render_debug.py`, shared by every tab's tile-array path), `[resize-debug]` (`src/ui/main_window/runtime.py`); plus `IMGSLI_RESIZE_DEBUG_VISUAL` for the visual variant.
- `IMGSLI_MC_FIRST_FRAME_DEBUG` → Multi Compare first-frame timeline (`src/tabs/multi_compare/first_frame_debug.py`).
- `IMGSLI_IC_FIRST_FRAME_DEBUG` → Image Compare first-frame timeline (`src/tabs/image_compare/first_frame_debug.py`).
- `IMGSLI_GALLERY_DEBUG` → `[gallery-dnd]` DnD/open routing, `[gallery-debug]` gallery lifecycle (`src/tabs/image_gallery/debug.py`; IC helpers mirror the pattern in `src/tabs/image_compare/debug.py`).
- `IMGSLI_IMAGE_COMPARE_DEBUG` / `IMGSLI_IC_DEBUG` → `[ic-dnd]` drag/drop routing (`src/tabs/image_compare/debug.py:ic_dnd_debug / ic_debug`).
- `IMGSLI_IC_PREVIEW_DEBUG` → `[ic-preview]` preview display gate (`src/tabs/image_compare/debug.py:ic_preview_debug`): applied display tier per `apply_store_to_canvas` plus deferred/skip reasons.
- `IMGSLI_IC_GAP_DEBUG` → `[ic-gap]` gap diagnostics (`src/tabs/image_compare/debug.py:ic_gap_debug`): geometry, pick, `gap_correlation`, `resolve_lod`, `draw_plan`, bbox coverage. Also emits `core.tracing` `ic.gap.*` when `IMGSLI_TRACE=1`. Analyze with `python src/devtools/analyze_ic_gap.py` (defaults to `get_log_directory(APP_LOGGER_NAME)/log.txt`).
- `IMGSLI_IC_LOUPE_DEBUG` → `[ic-loupe]` magnifier residency/LOD trace (`src/tabs/image_compare/debug.py:ic_loupe_debug`): `TileTextureService.request_level` answers, dropped-slice reasons, texture-choice and dedup verdicts; the same points also record into the per-frame `FrameContext` (see `src/tabs/image_compare/canvas/loupe_trace.py` for the sig/time/once throttle recipes).
- `IMGSLI_IC_INVALIDATE_DEBUG` → `[ic-invalidate]` attributed invalidation bus (`src/tabs/image_compare/debug.py:ic_invalidate_debug`): every scheduler/keep-warm/timer arm and fire/skip records source+reason into `FrameContext` (`src/engine/frame/context.py`); flag off costs one env check per site.
- `IMGSLI_MULTI_COMPARE_DEBUG` / `IMGSLI_MC_DEBUG` → `[mc-dnd]` drag/drop routing + `[mc-zoom]` wheel-tick diagnostics (`src/tabs/multi_compare/debug.py:mc_dnd_debug`): each wheel tick also records an `invalidate.mc_zoom_tick` marker (tick id only, no timing) into the per-frame `FrameContext`; the existing `[mc-dnd]` tick log is untouched — the bus record is additive.
- MC decision bus (no env gate — record-only, same as IC): `MultiCompareRhiRenderer.frame_context` (`src/tabs/multi_compare/scene/renderer.py`, shared `engine.frame.context`, reset per `render()` incl. early-skip paths) records `prepare_frame` (ran/skipped), `resourceUpdate`, `mips`, `lod_commit`/`realize_slots` (`scene/passes/residency.py`), `fallback`/`coverage`/`submit` (`scene/passes/base_images.py`), `overlay.*` (feature passes), `invalidate.mc_zoom_tick` (wheel) and `invalidate.cascade_deferred` (mips deferral). Every `ids[0]` is `frame=N` (`make_ids`); the frozen per-frame copy surfaces via `snapshot_last_frame` (test hook; pilot: `src/tabs/multi_compare/tests/render/test_frame_context_pilot.py`). Existing `mc_dnd_debug`/`mc_first_frame` (`IMGSLI_MC_FIRST_FRAME_DEBUG`) logs stay as-is.
- `IMGSLI_AUTOCROP_DEBUG` → `[autocrop-debug]` auto-crop diagnostics (`src/shared/image_processing/autocrop/debug.py`; consumed at load/presentation sites).
- `UI_NAV_DEBUG` → nav routing diagnostics (`src/events/router.py` + `src/events/app_event_handler.py`): standalone, needs neither `--debug` nor toolkit debug flags. Since 2026-10-14 also gates the host shell section traces via `src/core/nav_debug.py` — `[nav-titlebar]`/`[nav-tabstrip]`/`[nav-picker]` (`core/app_shell_navigation.py`) and `[nav-card]`/`[nav-tab]` (`core/navigation_sections.py`). One flag for the whole nav firehose, never a fork.
- `IMGSLI_TRANSIENT_DEBUG` → `[transient]` lazy tab-service resolution (`src/ui/managers/transient_ui_manager.py`) + `[transient-close]` flyout close sweeps (`src/ui/managers/transient_ui_parts/closing.py`), via `src/ui/host_debug.py`.
- `IMGSLI_CTX_MENU_DEBUG` → `[ctx-menu]` open/hide/outside-close routing (`src/ui/context_menu/manager.py`, `src/ui/presenters/main_window/connections.py`), via `src/ui/host_debug.py`.
- `IMGSLI_WORKSPACE_DEBUG` → `[workspace-transition]` tab-switch cover lifecycle (`src/ui/presenters/main_window/workspace.py`, `src/ui/main_window/workspace_transition_mask.py`, `src/ui/main_window/layouts.py`), via `src/ui/host_debug.py`.
- `IMGSLI_LAYOUT_DEBUG` → `[layout-tree]` main-window widget-tree dump (`src/ui/main_window/lifecycle.py`, `_log_layout_summary`): the tree is not even built when the flag is unset.
- `IMGSLI_STARTUP_DEBUG` → `[startup]` main-window startup pipeline steps + post-show drain passes (`src/ui/main_window/lifecycle.py`), via `src/ui/host_debug.py`.
- `IMGSLI_RHI_PRIMER_DEBUG` → `[rhi-primer]` one-shot top-level RHI surface primer (`src/ui/canvas_infra/rhi/rhi_backend.py`), via `src/ui/host_debug.py`.
- `IMGSLI_SCROLL_VALUE_DEBUG` → `[scroll-value]` scroll-value control set/wheel/key stepping (`src/ui/widgets/scroll_value_button.py`), via `src/ui/host_debug.py` (fires on every wheel tick, so never wire to global DEBUG).
- `IMGSLI_CAPABILITY_DEBUG` → `[startup-service]` startup capability routing (`src/tabs/use_cases/capability_routing.py:create_startup_service`, local `_startup_service_debug`): one line per capability lookup.
- `IMGSLI_FLYOUT_DEBUG` also gates the `[flyout-debug]` font-apply trace in `src/shared_toolkit/ui/managers/font_manager.py` (gated inline with `shared/debug_flags.env_flag`; that module stays free of `ui.*` imports).

### Full env-flag registry (audit 2026-09-20, reorg waves W1–W3, live-gate pass 2026-10-22)

Most `IMGSLI_*` gates parse via `shared/debug_flags.env_flag` (permissive:
any non-empty value except `0/false/no/off`); rows whose Parsing cell says
`env_value` parse inline with the same predicate. Every read resolves through
`shared/debug_flags.env_value`, so `BLINKTABLE_<X>` is honoured first and the
legacy `IMGSLI_<X>` keeps working (see the prefix section above).
Scope legend: `log` = gated
log lines only; `visual` = changes pixels; `dump` = writes capture files;
`behavior` = changes runtime behavior (NOT a debug log — do not mistake for one);
`tuning` = numeric/path knob; `trace` = tracer/startup facility; `removed` = deleted.

| Flag | Scope | Parsing | Owner file(s) |
|---|---|---|---|
| `IMGSLI_TRACE`, `IMGSLI_TRACE_KINDS`, `IMGSLI_TRACE_SKIP` | trace | permissive / filter lists | `src/core/tracing/tracer.py` |
| `IMGSLI_STARTUP_TRACE` | trace | `env_flag` | `src/core/startup_trace.py` |
| `IMGSLI_STARTUP_EARLY_SELFTEST_SLEEP_MS` | tuning (test hook) | `env_value`, int ms, off when unset | `src/core/startup_early_log.py` — deliberate GUI block after the stall watchdog arms, before logging exists |
| `IMGSLI_PERF_TRACE` | trace | permissive (export gate at `write_trace`) | `src/engine/frame/trace_export.py` (spans: `src/engine/frame/context.py`; IC hooks: `src/tabs/image_compare/canvas/`; MC hooks: `src/tabs/multi_compare/scene/renderer.py`, `src/tabs/multi_compare/canvas/interaction.py`, `src/tabs/multi_compare/ui/canvas_widget.py`) |
| `IMGSLI_RESIZE_DEBUG` | log | `env_flag` | `src/engine/cache/render_debug.py`, `src/ui/main_window/runtime.py` |
| `IMGSLI_RESIZE_DEBUG_VISUAL` | visual | `env_flag` | `src/ui/main_window/runtime.py` |
| `IMGSLI_RESIZE_SHIELD` | behavior | `env_flag` | `src/ui/main_window/runtime.py` |
| `IMGSLI_FLYOUT_DEBUG` | log | `env_flag` | `src/ui/widgets/flyout_debug.py`; also gates `[flyout-debug]` in `src/shared_toolkit/ui/managers/font_manager.py` |
| `UI_NAV_DEBUG` | log | `env_flag` (router sets sub-logger level) | `src/events/router.py`, `src/events/app_event_handler.py`, `src/core/nav_debug.py` (consumers `core/app_shell_navigation.py`, `core/navigation_sections.py`) |
| `IMGSLI_TRANSIENT_DEBUG` | log | `env_flag` | `src/ui/host_debug.py` (`ui/managers/transient_ui_manager.py`, `ui/managers/transient_ui_parts/closing.py`) |
| `IMGSLI_CTX_MENU_DEBUG` | log | `env_flag` | `src/ui/host_debug.py` (`ui/context_menu/manager.py`, `ui/presenters/main_window/connections.py`) |
| `IMGSLI_WORKSPACE_DEBUG` | log | `env_flag` | `src/ui/host_debug.py` (`ui/presenters/main_window/workspace.py`, `ui/main_window/workspace_transition_mask.py`, `ui/main_window/layouts.py`) |
| `IMGSLI_LAYOUT_DEBUG` | log | `env_flag` | `src/ui/host_debug.py` (`ui/main_window/lifecycle.py`) |
| `IMGSLI_STARTUP_DEBUG` | log | `env_flag` | `src/ui/host_debug.py` (`ui/main_window/lifecycle.py`) |
| `IMGSLI_RHI_PRIMER_DEBUG` | log | `env_flag` | `src/ui/host_debug.py` (`ui/canvas_infra/rhi/rhi_backend.py`) |
| `IMGSLI_SCROLL_VALUE_DEBUG` | log | `env_flag` | `src/ui/host_debug.py` (`ui/widgets/scroll_value_button.py`) |
| `IMGSLI_CAPABILITY_DEBUG` | log | `env_flag` | `src/tabs/use_cases/capability_routing.py` (`[startup-service]`) |
| `IMGSLI_TILE_DUMP` | dump | cached at import (restart to toggle) | `src/engine/cache/tile_debug.py` |
| `IMGSLI_GALLERY_DEBUG` | log | `env_flag` | `src/tabs/image_gallery/debug.py` |
| `IMGSLI_IMAGE_COMPARE_DEBUG` / `IMGSLI_IC_DEBUG` | log | `env_flag` (aliases, same gate) | `src/tabs/image_compare/debug.py` |
| `IMGSLI_MULTI_COMPARE_DEBUG` / `IMGSLI_MC_DEBUG` | log | `env_flag` (aliases, same gate) | `src/tabs/multi_compare/debug.py` |
| `IMGSLI_IC_PREVIEW_DEBUG`, `IMGSLI_IC_GAP_DEBUG`, `IMGSLI_IC_LABEL_DEBUG`, `IMGSLI_IC_LOUPE_DEBUG`, `IMGSLI_IC_INVALIDATE_DEBUG`, `IMGSLI_MAGNIFIER_DEBUG` | log | `env_flag` | `src/tabs/image_compare/debug.py` |
| `IMGSLI_IC_FIRST_FRAME_DEBUG` / `IMGSLI_MC_FIRST_FRAME_DEBUG` | log | `env_flag` (thin wrappers, shared body) | `src/tabs/*/first_frame_debug.py`, `src/ui/canvas_infra/rhi/rhi_render.py` |
| `IMGSLI_VIDEO_EDITOR_DEBUG` / `IMGSLI_IC_VIDEO_DEBUG` | log | `env_flag` (aliases, editor zone) | `src/tabs/image_compare/debug.py:ic_video_debug`, `plugins/video_editor/*` (editor sites), `src/plugins/export/*` (platform-local inline gates) |
| `IMGSLI_TIMELINE_DEBUG` | log | `env_flag` (timeline zone only) | `plugins/video_editor/debug_timeline.py:timeline_debug_enabled`, dialog `shell.py` |
| `IMGSLI_THUMBNAIL_DEBUG` | removed | deleted with the video-editor per-frame thumbnail pipeline (strip atlas phase 3, 2026-11-03); read nowhere | — |
| `IMGSLI_VIDEO_PERF_DEBUG` / `IMGSLI_IC_PERF_DEBUG` | log | `env_flag` (aliases) | `src/tabs/image_compare/debug.py:ic_perf_debug` |
| `IMGSLI_LASER_DEBUG` | log | `env_flag` (all 14 sites verified log-only 2026-09-20) | guides/magnifier/scroll/pickers (5 files) |
| `IMGSLI_LASER_UI_DEBUG` | reserved | `env_flag` (read nowhere yet) | convention only — mandatory gate for any future laser UI-visibility use |
| `IMGSLI_AUTOCROP_DEBUG` | log | `env_flag` | `src/shared/image_processing/autocrop/debug.py` |
| `IMGSLI_DND_DEBUG` | log | `env_flag` | `src/events/app_event/window_events.py`, `src/events/window_event_handler.py` |
| `IMGSLI_DOUBLE_GEOM_DEBUG` | log | `env_flag` | `src/ui/widgets/unified_list_picker/debug.py` |
| `IMGSLI_SHELF_RESIZE_DEBUG` | log | single helper `sizing._shelf_resize_debug` | `src/tabs/session_picker/recent/use_cases/sizing.py`, `panel.py` |
| `IMGSLI_TILED_VIEW_DEBUG` | log + dump | `env_value` (truthy) | `src/engine/gpu/tiled_image_view.py` (`_draw_instances` first-draw line + `_issue_debug_readback` layer readback) |
| `IMGSLI_QL_COVER_DEBUG` | log | `env_value` (truthy) | `src/engine/gpu/tiled_image_view.py` (`_draw_instances`, same first-draw line as above) |
| `IMGSLI_QL_RESIZE_TRACE` | log | `env_flag` | `src/tabs/image_gallery/ui/quick_look/resize/cursors.py` (`_resize_trace_on`) — full quick-look resize firehose |
| `IMGSLI_JANK_WATCH` | log | `env_value`, permissive (`""/0/no/off` off) | `src/tabs/image_gallery/widget.py` — GUI-thread stall watchdog (`[jank] gui-loop gap …ms` on a >0.15 s inter-tick gap) |
| `IMGSLI_STALL_WATCH` (+ `_THRESHOLD_MS`, default 250) | log + stack dump | `env_flag`, permissive | `src/core/stall_watch.py`, armed from `src/__main__.py` — **host-wide** GUI-thread watchdog: on a block past the threshold it logs `[stall] gui-loop blocked …ms` **and the main thread's Python stack**, so a hitch deferred past its cause (queued slot, debounce, GC, lazy cache) is attributable; an empty stack means the block is in Qt/driver/syscall; reports and the `armed` line go to `startup_early.log` (below) until `ctx.logging_ready` |
| `SLI_FOCUS_DEBUG` | log | `env_flag` (`focus_debug_enabled`) | `src/ui/focus_debug.py` → `[focus-debug]`; installed from `src/ui/main_window/lifecycle.py` |
| `IMGSLI_PERF_TRACE_PATH` | tuning | path override | `src/engine/frame/trace_export.py:default_trace_path` — fixed path for the perf-trace dump (default is pid-suffixed in the system temp dir — `tempfile.gettempdir()`, i.e. `%TEMP%` on Windows; this is the override for the "Frame perf spans" export) |
| `IMPROVE_ENABLE_TRAY` | behavior | direct `os.environ.get` (unset = enabled; `=1` forces on, any other value off) | `src/ui/main_window/composer.py` (`_should_enable_system_tray`) |
| `IMGSLI_GLASS_PANEL_DEBUG` (+ `_SIZE`) | log | cached at import / truthy | `src/engine/gpu/glass_panel.py`, `src/ui/widgets/glass_hud/panel_display.py` |
| `IMGSLI_GLASS_PANEL_DEBUG_TINT` | visual | cached at import | `src/engine/gpu/glass_panel.py` |
| `IMGSLI_GLASS_PANEL_DEBUG_DUMP` | dump | `<dir>` path | `src/engine/gpu/glass_panel.py` |
| `IMGSLI_GLASS_PANEL_SYNC_PROBE` | removed | historical only — named in comments, no live `os.environ` read (`src/engine/gpu/glass_panel.py`) | — |
| `IMGSLI_GLASS_PANEL_DISPLAY_BACKEND` | removed | read nowhere (re-checked 2026-10-22); there is no `=rhi` env switch — the panel's RHI path is `GlassPanelDisplayWidget`'s own descriptor | — |
| `IMGSLI_FAULT_DUMP` (+ `_FILE`, `_INTERVAL`) | dump | `env_flag` + path + int w/ fail-open default 15 | `src/core/windowed_stdio.py` — armed from `src/__main__.py` before Qt/logging; an unwritable path **disables the dump** and writes its one `Fault dump disabled: …` warning to `startup_early.log` (below) as well as the logger, because at that point no handler is attached and a windowed build's stderr is `os.devnull` |
| `IMGSLI_SKIP_MIP_CASCADE` | behavior | cached at import, permissive (`""/0/false/no/off` = off) — the same parse in both canvas tabs | `src/tabs/multi_compare/scene/renderer.py:55` (`_SKIP_MIP_CASCADE`), `src/tabs/image_compare/canvas/rhi_renderer/renderer.py:108` (`_skip_mip_cascade_requested` → `_SKIP_MIP_CASCADE`) |
| `IMGSLI_MC_RMB_SURFACE` | behavior | `=in_window\|popup` | `src/tabs/multi_compare/canvas/interaction.py` |
| `IMGSLI_MC_DND_DIAG_LIGHT_MOVE` | behavior | diag-only A/B, never ship | `src/tabs/multi_compare/debug.py` |
| `IMGSLI_DND_KEEPWARM_MS`, `IMGSLI_DND_ENTER_DELAY_MS` | tuning | int ms | `src/tabs/image_compare/canvas/widget.py`, `src/events/window_event_handler.py` |
| `IMGSLI_SKIP_FILE_ASSOCIATION` | behavior | `env_flag` | `src/services/system/windows_file_association.py` |
| `IMGSLI_PROJECT_CACHE` | tuning | path override | `src/services/io/project_package.py` |
| `IMGSLI_PROFILE_MAGNIFIER_TOGGLE` | trace | `env_flag` (cProfile) | magnifier toolbar `handlers.py` |
| `IMPROVE_IMGSLI_RHI_BACKEND`, `IMPROVE_IMGSLI_ALLOW_LSFGVK`, `IMPROVE_IMGSLI_STRICT_CONTRACTS` | behavior | `IMPROVE_` prefix (legacy naming) | `src/ui/canvas_infra/rhi/rhi_backend.py`, `workspace_transition_mask.py` |
| `IMGSLI_ACTIVATION_PROBE` | removed | deleted 2026-09-20 (was TEMPORARY in `src/__main__.py`) | — |
| `IMGSLI_TOAST_DEBUG` | removed | deleted earlier (no `src/` refs; docs residue cleaned 2026-09-20) | — |

Notes: video-family flags are NARROW since 2026-09-20 (host-dbgflags W5):
`VIDEO_EDITOR`/`IC_VIDEO` gate the editor zone only (`ic_video_debug`),
`TIMELINE` the timeline zone only (`timeline_debug_enabled`). `THUMBNAIL`
lost its only consumers when the video-editor per-frame thumbnail pipeline
was deleted (strip atlas phase 3, 2026-11-03), so the flag and its emitter are
gone; the strip is one GPU band pass with no per-tile worker to trace.
Migration: set `IMGSLI_TIMELINE_DEBUG=1` for the timeline. `IMPROVE_IMGSLI_*`
vs `IMGSLI_*` prefix duality is historical; both spellings
are honored only where listed above.

## Startup trace

Cold-startup phase timing (bootstrap vs deferred plugin load):

```bash
IMGSLI_STARTUP_TRACE=1 python src/__main__.py
grep 'BlinkTable.startup' ~/.local/share/BlinkTable/log.txt
```

### Early sink: the window before logging exists

`main.entry`, the module-scope imports, `QApplication` creation, the RHI/D3D11
probe (307–374 ms cold) and `MainWindow` construction all run **before**
`bootstrap._configure_logging` attaches a handler. A `startup_mark` line
emitted there goes to an unconfigured logger and is dropped — and a windowed
(`--noconsole`) build points `stderr` at `os.devnull`, so the phase is
**invisible rather than absent**.

`src/core/startup_early_log.py` (behind `core.startup_trace.early_mark`) keeps
that window in `<app data dir>/startup_early.log`, next to `log.txt`:

- created only when `IMGSLI_STARTUP_TRACE` **or** `IMGSLI_STALL_WATCH` (or the
  `BLINKTABLE_` spelling) is on — with both off no directory is touched, no
  file is created, and every `early_mark` is a no-op;
- one exception, an error rather than a diagnostic: when the opt-in
  `IMGSLI_FAULT_DUMP` is **disabled** by an unusable dump path, its single
  `Fault dump disabled: …` warning is written here **even with both
  diagnostics flags off** (`core.windowed_stdio` → `write_error_record`). It
  fires from `__main__` before any log handler exists and a windowed build's
  stderr is `os.devnull`, so this file is its only sink. Exactly one record per
  process (repeated arming does not duplicate it); an *armed* dump writes
  nothing here and creates no file;
- opened `w` once per traced run (**wiped every time**), then one append +
  close per record: no long-lived handle, no Qt object, no `setup_logging`
  dependency;
- never raises — an unwritable data directory silently disables it;
- written into the legacy `…/ImproveImgSLI` directory when that is the only one
  on disk, so a traced run cannot disable the one-time log-dir migration (see
  above); after migration both spellings are the same tree.

Marks (offsets are ms since the earliest point a portable process can observe
itself — the import of the sink at the top of `src/__main__.py`; interpreter
start-up before that is excluded by construction):

| Mark | Emitted from |
|---|---|
| `main.entry` | module scope of `src/__main__.py`, before the heavy imports (+0 ms; a queued `Fault dump disabled` record follows it when that error armed the dump first) |
| `main.imports_done` | end of that import block (the previously invisible import cost) |
| `main.qapp_ready` | right after `QApplication(argv)` |
| `rhi.probe.start` / `rhi.probe.end` | around `resolve_rhi_backend_with_fallback` |
| `main.window_start` | before `MainWindow(...)` |
| `main.window.init_done` | after `window.start()` |

The stall watchdog's reports (`[stall]`, `src/core/stall_watch.py`) and its
`[stall] watchdog armed` line go to the same file until `ctx.logging_ready`
fires, then back to `log.txt` exactly as before — the early file is the
pre-logging fallback, never a second copy of the ordinary log.

A disabled fault dump is the other writer: with `IMGSLI_FAULT_DUMP=1` and an
unusable `IMGSLI_FAULT_DUMP_FILE` (or temp dir) the dump is dropped and exactly
one `Fault dump disabled: cannot open/wire …` record appears here, while startup
continues; that line is the only trace a `--noconsole` build can leave, since
its stderr is `os.devnull`. The default run (no `IMGSLI_FAULT_DUMP`) still
creates nothing.

That record is armed at `enable_faulthandler()`, i.e. **before**
`src/__main__.py` imports `core.startup_trace`. In a run that is both traced and
has a broken dump path the sink therefore already exists when the first mark is
written, so the record is **queued** and flushed right after it: `main.entry`
stays the file's first line and the warning follows as the second record,
exactly once. Every other traced run is unchanged.

```bash
IMGSLI_STARTUP_TRACE=1 IMGSLI_STALL_WATCH=1 python src/__main__.py
cat ~/.local/share/BlinkTable/startup_early.log
```

A deliberate reproduction of the "stall before logging" case:
`IMGSLI_STARTUP_EARLY_SELFTEST_SLEEP_MS=400` holds the GUI thread right after
the watchdog is armed (test hook; unset does nothing). The
`[stall] gui-loop blocked …` report then has to appear in `startup_early.log`,
not in `log.txt` — pinned by `tests/runtime/test_startup_early_log.py`.

## Frame perf spans (IC + MC zoom-burst repro)

CPU spans for the Image Compare wheel-to-photon chain
(`ic.wheel_tick` → `ic.enqueue` → `ic.prepare` → `ic.realize_main` /
`ic.realize_magnifier` → `ic.mips` → `ic.submit`, plus the enqueue-side
`ic.present_proxy` — compositor excluded by construction, never photon
timing) and its Multi Compare mirror (`mc.wheel_tick` →
`mc.enqueue` → `mc.prepare` → `mc.realize_slots` → `mc.mips` →
`mc.submit`, plus enqueue-side `mc.present_proxy`). Same `wheel-<id>`
join atom on both tabs; one Perfetto lane per `frame_id` merges both
tabs' same-tick spans by design (see `trace_export` field mapping).
Spans are always collected into each tab's own `FrameContext` ring;
`IMGSLI_PERF_TRACE=1` gates serialization at the export boundary only
(`src/engine/frame/trace_export.py:write_trace` — callers never
re-implement the gate):

```bash
IMGSLI_PERF_TRACE=1 python src/__main__.py   # interact, then export path writes trace.json
python src/devtools/analyze_perf_trace.py trace.json --budget-ms 25.0
```

The analyzer prints per-stage count/total/p50/p95/max (ms) and flags
ticks over budget with the dominant stage. Hook-point tests live in
`src/tabs/image_compare/tests/render/test_ic_perf_spans.py` (IC) and
`src/tabs/multi_compare/tests/render/test_mc_perf_spans.py` (MC,
including the synthetic 2-context export that proves both tabs share
one `frame-N` lane). See also
[TRACING.md](TRACING.md) (separate causal-tracer facility) for the
cross-link.

## Reading existing logs

```bash
tail -f ~/.local/share/BlinkTable/log.txt              # follow live
grep '\[ic-loupe\]' ~/.local/share/BlinkTable/log.txt
```

The log file is overwritten on every app start (`mode="w"`), so capture sessions you care about.

## See also

- [Canonical logging conventions](https://github.com/Loganavter/sli-ui-toolkit/blob/main/docs/dev/LOGGING.md) — shared recipe/rationale (unique-prefix, never-print, temp-diagnostics, silencing)
- [TRACING.md](TRACING.md) — structured tracer for Redux/EventBus/render chains (separate facility, complementary to plain logging)
- `sli_ui_toolkit/core/logging.py:setup_logging` — full source
- [AGENTS.md](../../AGENTS.md) — agent guide; see **Debugging Runtime Issues** and **Agent Tooling**
