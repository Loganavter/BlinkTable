# Video Editor (tab-owned plugin)

The video editor is a **tab-owned sub-plugin** of Image Compare
(`src/tabs/image_compare/plugins/video_editor/`), not an app-wide plugin. It
records and animates image-compare states: recording, pause, timeline,
preview, keyframes, and video export.

Plugin system mechanics (tiers, `@plugin`, discovery): see
[PLUGINS.md](../../../../../docs/dev/PLUGINS.md). For how the editor fits the
tab, see [ARCHITECTURE.md](../ARCHITECTURE.md).

## Wiring

`@plugin(name="video_editor", startup_tier="deferred", startup_order=0)`
(loads before `export`, order 10). Implements `ISessionPlugin` (and is the
`image_compare` session's editor). 

- `initialize(context)` stores `event_bus`, `thread_pool`, `store`;
  subscribes `SettingsChangeLanguageEvent`.
- `open_editor(snapshots, export_controller, main_window_app)` opens the
  modeless `VideoEditorDialog`, re-showing the existing dialog if one is open.
- `create_recording_services(store, main_controller, gpu_export_service,
  extra_adapters=...)` → `(Recorder, VideoExporterService)`. Called by the
  **export** plugin (`ExportPlugin._create_recording_services`).
- `create_control_flows(controller)` → `(RecordingFlow, VideoExportFlow)`.
  Called by `ExportController` in the export plugin.

## Key modules

| Module | Role |
|---|---|
| `plugin.py` | `VideoEditorPlugin` — dialog open, recording-service factory, control-flow factory |
| `model.py` | `VideoProjectModel` — project resolution/aspect model |
| `dialog/` | `VideoEditorDialog` + related dialog UI |
| `presenter.py`, `presenter_parts/` | editor presenter (timeline/preview updates) |
| `services/recorder.py` | `Recorder` — records image-compare state over time |
| `services/recording_flow.py` | `RecordingFlow` — toggle/pause recording orchestration |
| `services/export.py` | `VideoExporterService` |
| `services/export_flow.py` | `VideoExportFlow` — export from editor frames |
| `services/timeline.py`, `services/playback.py` | timeline model, playback |
| `services/keyframing/` | keyframe adapters (`IVideoTrackProvider`-style) |
| `services/video_export/`, `services/video_snapshot_rendering.py` | video export + snapshot rendering for this tab |
| `services/strip_recording.py`, `presenter_parts/strip_band.py` | the filmstrip's one renderer: timeline blocks → one GPU band pass |
| `services/canvas_feature_gateway.py` | routes editor actions to canvas feature commands |
| `actions.py`, `search.py`, `translations.py`, `widgets/timeline/` | Find Action contributions, search, i18n, timeline widgets |

## Strip latency gate (live acceptance)

The filmstrip is one synchronous GPU pass, so its numbers are a gate, not a
preference: a full fill in **<= 50 ms** and any scroll/zoom/resize tick in
**< 10 ms** (`plan-video-strip-atlas-2026-10-30`). Both are measured by
`src/devtools/bench_strip_latency.py`.

It needs a **real or private display**: offscreen QRhi has no framebuffer, so
an offscreen "pass" would be fabricated and the tool exits `2` instead of
reporting one. Take a lease (never `:0`, one per agent):

```bash
lease=$(live-lease alloc --lane strip-latency --task fill-interaction | tail -1)
eval "$(live-lease env "$lease")"
python src/devtools/bench_strip_latency.py
live-lease release "$lease"
```

Verdict is the exit code plus the table
`case | build_ms | render_ms | total_ms | budget_ms | verdict`:

- **green** — exit `0`, every row `pass`. Rows: fill at 8/17/32 blocks, a
  scroll step and a zoom step. `build_ms` is `build_window` (planning +
  packing), `render_ms` is one `render_window` pass (its first-frame upload
  included), `total_ms` is what the budget compares.
- **red** — exit `1`, with the over-budget rows printed. Each row's detail
  line splits min/median/max so a red case is either "planning" or "the pass",
  never a mystery number.
- **no display** — exit `2`.

The framebuffer grab in the detail lines is a readback proof, not a per-tick
cost: it stays outside the budget. `--fill-budget-ms` /
`--interaction-budget-ms` override the numbers (prove a red gate on a measured
case), `--repeat` reports the median, and `--cpu-only` runs the planning half
with no verdict — a CPU number cannot answer the interaction budget. Numbers
are comparable between two runs on one host and backend, not across GPUs.

The other half of the acceptance is structural: the strip path contains no
timer, settle gate or sleep — `tests/contracts/test_strip_no_debounce.py`.

## Load order dependency

`video_editor` (order 0) must load before `export` (order 10): the export
plugin pulls recording services from it at `configure_controller` time.

## Host integration

App-level code reaches the editor through the registered plugin commands
(`plugin_coordinator.execute_command("video_editor", "open_editor", ...)`),
never by importing its internals.
