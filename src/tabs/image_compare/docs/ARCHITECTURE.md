# Image Compare Architecture

## Boundary

`src/tabs/image_compare/` is a workspace tab with a large internal feature
domain. It owns the classic two-image comparison workflow: file lists,
split/divider interaction, magnifier, guides, paste/capture overlays, analysis
modes, WYSIWYG image export, live snapshots, and video editor integration.

The host shell owns startup, window chrome, global settings, plugin discovery,
workspace sessions, and common services. The host may call the tab through
`TabContract` hooks or registered plugin commands; it should not import
tab-internal modules for feature behavior.

## Module Layout

```text
src/tabs/image_compare/
    tab.py                    # ImageCompareTab(TabContract)
    plugin.py                 # comparison plugin + image_compare session blueprint
    service_factory.py        # create_service ID → implementation branches
    widget.py                 # tab page wrapper, still assembles some host primitives
    _session_controller.py    # loading/navigation/session orchestration
    canvas/                   # QRhi canvas, scene contracts, render pipeline
        features/             # divider, magnifier, guides, capture, filename_overlay,
                              # drag_drop_overlay, _template
        presentation/         # render plan builder/applicator for this tab
        texture_parts/        # image and overlay texture uploads
    events/                   # tab-scoped canvas input and drop routing
    presenters/               # toolbar/export/canvas presenters
    services/                 # export, live snapshot, analysis, playlist, keyframe adapters
    state/                    # tab-owned models, actions, reducers
    ui/                       # tab-local settings/layout/transient UI helpers
    use_cases/                # plain functions over the tab/controller (loading, chrome sync, …)
    pipeline/                 # tiled render/compose pipeline
    actions/                  # action catalog + keymap defaults
    plugins/video_editor/     # recording, timeline, preview, keyframes, video export
    resources/                # i18n (image_compare.json, tooltips.json), help, icons
```

## Runtime Flow

1. `ComparisonPlugin` registers the `image_compare` session blueprint and
   creates tab services that need app context: store, event bus, thread pool.
2. `TabRegistry` discovers `ImageCompareTab`, installs its page, and calls its
   lifecycle hooks when workspace sessions switch.
3. UI gestures are routed through `tabs.image_compare.events`, converted into
   tab actions or feature commands, and dispatched through the store.
4. Tab reducers in `state/reducers.py` handle image-compare-specific state via
   extension reducer registration. Core reducers must not import this module.
5. `canvas/presentation/plan_builder.py` builds the live render plan. Export
   and video snapshot services reuse the same semantics instead of rebuilding a
   separate layout model.

## Canvas Features

Image-compare canvas features live under `canvas/features/<name>/`. A feature
package owns its manifest, state, actions, commands, passes, properties,
settings bindings, runtime hooks, and optional toolbar wiring.

Current feature packages include:

- `divider`
- `magnifier`
- `guides`
- `filename_overlay`
- `capture`
- `drag_drop_overlay`
- `_template` (skeleton for a new feature package)

Feature packages register through the generic canvas contracts in
`src/ui/canvas_infra/scene/`, but the feature implementations are tab-local.
Generic host modules should depend on contracts and registries only.

## Export And Snapshot Services

The old shared/export split has been collapsed into tab-owned services:

| Responsibility | Current owner |
|---|---|
| Still image export | `services/image_export/` (`service.py`, `context_builder.py`, …) |
| Export context construction | `services/image_export/context_builder.py` |
| GPU export scene | `services/gpu_export_scene.py` |
| Snapshot render plan | `services/snapshot_render_plan_builder.py` |
| Live snapshot | `services/live_snapshot.py` |
| Video snapshot render | `services/video_snapshot_rendering/` (still/export) + `plugins/video_editor/services/video_snapshot_rendering.py` (video frames) |
| Video editor export pipeline | `plugins/video_editor/services/` |

Two snapshot renderers coexist and both are live: still-image export goes
through `services/video_snapshot_rendering/` (`renderer.py`, `prepare.py`, pulled
in by `services/image_export/{service,context_builder}.py`), while the video
export's frame loop pulls its own `SnapshotFrameRenderer` from
`plugins/video_editor/services/video_snapshot_rendering.py`
(`services/video_export/service.py`). Live canvas is
`canvas/presentation/plan_builder.py`. A visual change must be checked in all
three.

When changing visual output, inspect live canvas, still export, and video
snapshot paths together. A fix in only one path is usually incomplete.

## Analysis

The tab owns the **orchestration** of diff/metric work; the pixel kernels are a
real shared contract in `src/shared/analysis/`, and the tile geometry in
`src/engine/store/`:

```text
src/tabs/image_compare/services/analysis/   # tab-local orchestration
    analysis_pair.py        # pair-level entry (crop/resample via engine.store)
    background_layers.py    # highlight/edges/ssim layer builders (calls the kernels)
    cached_diff.py          # diff image cache over TiledPixelStore
    metrics.py              # metric computation + cache
    runtime.py              # analysis runtime/threading glue

src/shared/analysis/                        # shared kernels (not tab-local)
    differ.py, diff_source.py, ssim_source.py,
    edge_detector.py, channel_analyzer.py,
    metrics.py, output.py, tile_boxes.py

src/engine/store/regions.py                  # tile grid geometry used by the kernels
```

So "diff/metric processing is tab-local" is no longer the rule: the tab owns
*when and how* analysis runs, the shared modules own *how a pixel becomes a
diff/ssim/edge/channel value*. Tab code imports `shared.analysis.*` and
`engine.store.*` directly (`services/analysis/background_layers.py` does both);
new shared kernels belong in `src/shared/analysis/`, and a helper that only this
tab needs stays in `services/analysis/`.

## Video Editor

The video editor is part of the image-compare domain:

```text
plugins/video_editor/
    plugin.py
    presenter.py              # editor logic; presenter_parts/ holds the split-out parts
    presenter_parts/          # bootstrap, playback, preview, strip_band, exporting, ...
    dialog/                   # package: shell, export, nav, sections, persistence,
                              #          runtime, surface_widgets, tab_cursor
    services/                 # editor, recorder, timeline, keyframing/, video_export/
    widgets/timeline/
```

It records and animates image-compare states, so its services and keyframe
adapters stay inside this tab. App-level code should reach it through
registered plugin/session commands, not by importing its internals.

## Host Integration

`ImageCompareTab` is the integration boundary for host-owned services such as
workspace activation, drag/drop routing, settings contribution, transition-mask
release, and shutdown cleanup. New image-compare behavior should be added under
this package and exposed through tab/plugin contracts instead of by adding
feature-specific branches to the main window.