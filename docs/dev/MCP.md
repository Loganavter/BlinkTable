# MCP server (app control)

A local **Model Context Protocol** server that exposes the app to LLM agents —
the app-side counterpart to the docs MCP server. It speaks MCP revision
**`2026-07-28`** over **stdio** and also accepts the legacy `initialize`
handshake, so today's clients work unchanged.

Source: [`mcp-server/app/`](../../mcp-server/app/) — see its
[README](../../mcp-server/app/README.md) for the full surface and security model.
The catalog is declared in `mcp-server/app/catalog.py`, implemented in
`backend.py`, and transported by `protocol.py`.

```bash
python3 mcp-server/app/server.py --check    # print the declared catalog
python3 mcp-server/app/server.py            # serve on stdio
```

## Wiring

Add an `app` server under `mcpServers` in your opencode config
(`~/.config/opencode/opencode.jsonc`), alongside any other MCP servers you
already run (e.g. a docs server) — the snippet is the block to add, not a
description of a default install:

```jsonc
{
  "mcpServers": {
    "app": {
      "type": "local",
      "command": "python3",
      "args": ["/abs/path/to/Improve-ImgSLI/mcp-server/app/server.py"],
      "environment": { "IMGSLI_REPO_ROOT": "/abs/path/to/Improve-ImgSLI" }
    }
  }
}
```

The app venv is auto-detected (`venv/bin/python` when present). Knobs:
`IMGSLI_MCP_ARTIFACTS` (scratch dir), `IMGSLI_MCP_TIMEOUT` (boot budget),
`IMGSLI_MCP_CONTROL_SOCKET` / `IMGSLI_CONTROL_DIR` (which live instance to
attach to), `IMGSLI_MCP_DISPLAY` (opt-in private display for a visible
fallback window, e.g. `:97`).

### Environment variables — `BLINKTABLE_` first, `IMGSLI_` still honoured

The app-side knobs resolve through one helper
(`src/shared/debug_flags.py:env_value`): `BLINKTABLE_<X>` is honoured first,
the historic `IMGSLI_<X>` keeps working, both spellings are supported for this
release, and the legacy prefix is not deprecated silently. An empty or
whitespace-only value counts as unset.

| Purpose | New spelling | Legacy spelling |
|---|---|---|
| in-app control channel | `BLINKTABLE_CONTROL=1` | `IMGSLI_CONTROL=1` |
| attach control dir | `BLINKTABLE_CONTROL_DIR` | `IMGSLI_CONTROL_DIR` |
| tracing | `BLINKTABLE_TRACE=1` | `IMGSLI_TRACE=1` |
| trace kind filter / skip list | `BLINKTABLE_TRACE_KINDS` / `BLINKTABLE_TRACE_SKIP` | `IMGSLI_TRACE_KINDS` / `IMGSLI_TRACE_SKIP` |
| startup phase timing | `BLINKTABLE_STARTUP_TRACE=1` | `IMGSLI_STARTUP_TRACE=1` |
| artifacts/scratch dir (app side) | `BLINKTABLE_MCP_ARTIFACTS` | `IMGSLI_MCP_ARTIFACTS` |

The MCP server is a separate stdlib-only process (`mcp-server/app/`) that does
not import `src/`, so its own bootstrap knobs keep the `IMGSLI_*` spelling in
this release: `IMGSLI_REPO_ROOT`, `IMGSLI_MCP_TIMEOUT`, `IMGSLI_MCP_DISPLAY`,
`IMGSLI_MCP_CONTROL_SOCKET`, plus the server-side read of
`IMGSLI_MCP_ARTIFACTS`. Set both spellings when one run must satisfy the app and
the server.

## Surface at a glance

30 tools — 12 read-only, 18 mutating (reads and writes never share a tool):

| Tool | Kind | What it does |
|---|---|---|
| `list_actions` | read | ActionRegistry catalog (id, label, owner tab, shortcut) |
| `list_sessions` | read | workspace sessions (id, type, title, active) |
| `get_state` | read | Redux Store slice by dotted path (+ `tabs.*` assertion slices) |
| `read_log` | read | tail `log.txt`, optional level filter |
| `query_trace` | read | search `trace.jsonl` newest-first |
| `dump_ui_layout` | read | widget tree JSON (geometry, colors, bound action ids) |
| `list_surfaces` | read | capture-surface names for `capture_surface` (live/capturable flags) |
| `ui_elements` | read | live inventory of interactive elements (id, role, label, rects, enabled, properties bag; role/label/properties/limit filters) |
| `ui_nav_state` | read | live keyboard-navigation model: sections + focus + overlay children no section owns (live-only) |
| `jobs_list` | read | live in-flight long passes (kind, status, progress, elapsed_s, message) |
| `wait_for` | read | block until one expectation holds (element id visible+enabled, role+label exists, or jobs settle); a timeout returns the last observed state (live-only) |
| `get_canvas_scene` | read | one tab's canvas scene as deterministic JSON: panes, dividers, every magnifier instance (hidden ones flagged), canvas size, camera/projection transform; every geometry field names its coordinate space and its source (live-only) |
| `open_tab` | write | open/focus a tab kind (live) or launch it (fallback) |
| `run_action` | write | run one ActionRegistry id; with `params`/`expect` it also runs the seven named scene commands (magnifier visible/position/size/combined, divider visible/thickness, MC split weights) and can verify the change against scene paths (live-only) |
| `set_scene_commands` | write | run an ordered list of value-taking scene commands; stops at the first error, and with `verify=true` each command declares expected scene paths and the scene is re-read (live-only) |
| `open_folder` | write | hand a folder to a tab (`tab=` targets one; `slot=`/`side=` place it; live: routed) |
| `open_file` | write | load one image file into a tab that accepts it (`tab=` forces one; `slot=`/`side=` place it — two calls compose a pair; a video is refused by name) — live-only |
| `open_properties` | write | show the app's image-properties dialog for a file, non-modally (live-only) |
| `set_theme` | write | set `theme` (light/dark); live-now or persisted |
| `set_language` | write | set `language` (en/ru/zh/pt_BR) |
| `set_setting` | write | write one whitelisted scalar setting (`rhi_backend`); `theme`/`language` keep their own verbs |
| `capture_surface` | write | screenshot a named surface or the live active window to PNG |

**Driving a full gallery AI run from a script:** `src/devtools/live_gallery_ai_run.py` boots the
app in the lease environment, opens the gallery tab, drops a folder, switches the judgment domain
to AI and waits on a LOG condition (`judge run done` + a quiet window) instead of a timeout, then
prints a JSON verdict (cards, misses, calls, profile source, `judge_pairwise.*` lines, tracebacks).
Use it instead of hand-rolled `sleep`+`grep` sequences: those report a result whether or not the
folder ever opened (2026-10-26).

| `press_keys` | write | inject keyboard combos (`target=` aims them at one element); the window is activated first and the result reports `activated`/`focus_is_ours` |
| `nav_key` | write | press keys through the app's own navigation sections and report what took them (live-only) |
| `ui_click` | write | click an element by id, in-process (live-only) |
| `ui_batch` | write | run an ordered list of the single-action verbs in ONE call (`ui_click`/`ui_hover`/`ui_focus`/`ui_press`/`ui_wheel`/`press_keys`/`wait_for`/`run_action`/`set_setting`); per-item results, stops at the first failure unless `stop_on_error=false`, no rollback (each item is as live-only as its own tool) |
| `ui_hover` | write | hover an element by id, in-process (live-only) |
| `ui_focus` | write | focus an element by id, in-process (live-only) |
| `ui_press` | write | press an element's left button, optionally leaving it held (`release=false`, `hold_ms`) (live-only) |
| `ui_wheel` | write | send `count` wheel events to an element (by id, `property:<name>=<value>`, or a global `point:<x>,<y>`) with a chosen Qt scroll phase (`none`/`begin`/`update`/`end`) and modifiers (`ctrl`/`shift`/`alt`) — reproduces a discrete notch, a high-resolution stream, or a Ctrl+wheel canvas zoom; `raw=true` keeps the event on the element (a canvas always keeps it) (live-only) |
| `jobs_cancel` | write | cancel a running job by id — real cancel, or a named refusal (live-only) |

Resources: `app://actions`, `app://state`, `app://sessions`, `app://log`,
`app://ui-layout`, `app://theme`; templates `app://action/{id}`,
`app://tab/{name}/layout`. Prompts: `diagnose_gallery`, `capture_and_inspect`.

The catalog itself is the source of truth — read it with `--check` or a
`tools/list` request rather than trusting this table.

## How it drives the app

Every tool answers from the **live instance** when one is running, and falls
back to the headless Phase-1 path when none is. Results say which path answered
(`"source": "live"` or `"source": "fallback"`).

### Live attach (preferred)

The app runs a control server on a unix socket `control.sock` inside a control
dir, with a pointer file `instance.json` (`{pid, socket, version, uid}`)
beside it. Discovery order, highest priority first:

1. `IMGSLI_MCP_CONTROL_SOCKET` — an explicit socket path;
2. `IMGSLI_CONTROL_DIR` — an explicit control dir;
3. `$XDG_RUNTIME_DIR` **only when it is private** (owned by you, no group/other
   access — a `0755` runtime dir is used by neither side), else
   `<app data dir>/run` (`~/.local/share/ImproveImgSLI/run`).

A pointer whose `uid` is not yours is treated as "no live instance": the app
publishes for its own user only, and its socket would refuse the connection
anyway.

One tool call is one newline-delimited JSON request
(`{"id", "method", "params"}`) answered by
`{"id", "ok": true, "result"}` or
`{"id", "ok": false, "error", "hint"}`. On this path the tools act on the window
the user is driving: `open_tab` switches in place, `run_action` runs live,
`open_folder` really routes the folder (`routed: true`), `set_theme` /
`set_language` apply now (`applies_on: "now"`), and `press_keys` makes the
target window OS-active before injecting combos the Find Action catalog does
not cover — it reports `activated` (this call had to bring the window forward)
and `focus_is_ours` (the key went to the window holding OS focus), and
**refuses** rather than answering `ok` when the app still reports no active
window (the hint names `xdotool windowfocus <window-id>`), because a key handed
to a background window is dropped by the window system. `ui_elements` inventories
the live window's interactive widgets (id, role, label, widget `rect` and
screen-space `screen_rect`, `enabled`, bound action id, and a `properties`
bag; optional `role`/`label`/`properties`/`limit` filters — see
[Element ids](#element-ids-filters-and-stable-property-targets)) and
`ui_click` / `ui_hover` / `ui_focus` act on one by its session-scoped id —
in-process, at the element's centre, with an unknown id retried ~3 s and then
failed with close matches. In-process delivery fires the handler even when the
element is off-screen; only the human-visible effect needs it on screen. Both
rects are 4-number arrays; `rect` is widget-local and `screen_rect` is
screen-space (best-effort on Wayland). Mutations with no Phase-1 analogue carry
`live: true` (there is no fresh `pid` to report).

Staleness and failure are non-events for the caller: a pointer whose socket is
unusable is stale, a socket nobody listens on is "no live instance", and a
connection the app refuses or closes surfaces as a fallback — never as a
user-visible error. The pointer's `pid` is only a *hint*: a Flatpak app runs in
its own PID namespace, so the host's `kill(pid, 0)` can call a live app dead —
the server probes the socket instead and only declares staleness when nothing
answers (see [Flatpak sandbox](#flatpak-installed-app-p0)). After a transport
failure the server pauses re-attaching for a couple of seconds so a dead socket
is not retried on every call. The app refuses connections from another uid (its
own `SO_PEERCRED` check); this client only has to degrade cleanly.

### Asserting what a tab shows (`get_state` tab slices)

`get_state {path}` also answers **tab-scoped assertion slices** under `tabs.*`,
so "Quick Look shows image N" / "the gallery has this selection" / "compare is
on the left image" is one in-channel call — no overlay labels, no tracer
enabled at app start, no screenshot. The slot-derived keys come from that tab's
own state (its Redux session slot) and are answered by the fallback snapshot
too; only widget-local facts (Quick Look, gallery sort) are live-only and
report `null` when the page was never built.

| Path | Keys |
|---|---|
| `tabs.image_gallery` | `active`, `folder`, `item_count`, `selection` (paths), `selection_count`, `filter_text`, `sort`, `judgment_domain`, `metrics_cache_size` (items with live metrics; `null` without a page) |
| `tabs.image_gallery.quick_look` | `open`, `path`, `index` (0-based in the visible series), `total`, `zoom`, `pan` (`[x, y]`), `source_side` (`preview` when the open item *is* the source, `inset` when the source inset shows another image) |
| `tabs.image_compare` | `active`, `left_path`, `right_path`, `mode` (`0` both / `1` left / `2` right), `split_ratio`, `diff_mode` |
| `tabs.multi_compare` | `active`, `slot_count`, `filled` (paths), `focused_slot` |

`active` says whether that tab is the **active workspace session**. A tab that
is not active (or has no live page/slot) answers `active: false` plus nulls —
never an exception; `quick_look.open` is `false` only when the gallery itself
is active and the panel is shut, `null` when the gallery is not the visible
tab. A value the app cannot read is reported as `null`, never as a plausible
default. Unknown paths keep the existing `no state at path` error.

```
get_state {path: "tabs.image_gallery.quick_look"} →
  {"active": true, "open": true, "path": "/shots/12.png",
   "index": 2, "total": 9, "zoom": 2.75, "pan": [12.0, -4.0],
   "source_side": "inset"}
```

### Canvas control: scene reads and verified commands (`get_canvas_scene`, scene commands)

A canvas tab is not driveable through the element inventory alone: the panes,
dividers and magnifier instances are painted, not widgets. Three tools cover it,
and they share one rule — **a field is either measured and labelled, or named as
unavailable, never approximated**.

`get_canvas_scene` (read) answers one tab's scene as deterministic JSON:

```json
{"schema": "imgsli-canvas-scene/1", "tab": "image_compare",
 "canvas": {"w": 640, "h": 480, "space": "canvas_px", "source": "ic_render_plan"},
 "panes": [], "dividers": [{"kind": null, "visible": true}],
 "magnifiers": [{"id": "default", "visible": true, "active": true,
                 "model": {"position_relative": [0.5, 0.5], "size_relative": 0.2,
                           "divider_visible": true, "divider_thickness": 2,
                           "offset_relative": [0.0, 0.0]}}],
 "transform": {"camera": {"zoom": 1.0, "pan": [0.0, 0.0], "space": "relative"}},
 "sources": {"magnifiers": "magnifier_widget_state (no instances)"},
 "unavailable": [{"field": "canvas", "reason": "no rendered frame …"}]}
```

- **Every geometry field names its space and its source.** `space` is
  `canvas_px` or `relative`; `source` names the owner the value was read from.
  Two owners can disagree on purpose — the scene graph keeps hidden instances
  and canvas pixels, the renderer paints a filtered layout — so a value is only
  meaningful together with its source.
- **Honest gaps instead of guesses.** A field that cannot be derived is absent
  from the value and listed in `unavailable` with a reason (`no rendered frame
  and no decoded image-pair size`, `no split line without a content rect`, …).
  A caller must treat `unavailable` as "not measurable right now", not as zero.
- **Hidden magnifier instances are listed**, flagged with `visible: false`, and
  keep their state; `active` names the one the app is currently driving.
- **The read does not change the scene.** It must not create state on first
  read; a getter that materializes state is a bug in the getter.

Seven **scene commands** take absolute values (never toggles, never deltas):

| command | params | canvas |
|---|---|---|
| `magnifier.set_visible` | `id` (instance id exactly as listed), `visible: bool` | image_compare |
| `magnifier.set_position` | `id`, `x`/`y` as fractions `0..1` of the canvas | image_compare |
| `magnifier.set_size` | `id`, `size` as a fraction of the short side (default 0.2) | image_compare |
| `magnifier.set_combined` | `id`, `combined: bool` — folds the two sides into one loupe (spacing 0) or splits them again; needs both sides visible, and a hidden instance is refused (`is_combined` is `null` for it) | image_compare |
| `divider.set_visible` | `visible: bool` | image_compare |
| `divider.set_thickness` | `thickness: int px` `0..10` (0 also hides) | image_compare |
| `layout.set_split_weights` | `path: [int]` (child-index chain, root `[]`), `weights: [float]` summing to ~1.0 (clamped per child ≥ 0.15) | multi_compare |

Two ways to run them, both rejecting anything outside the schema:

- `run_action {"action_id": "<command>", "params": {…}, "expect": {…}, "tab": …}` —
  a scene command id routes to the command; any other id runs the Find Action.
  Supplying `expect` turns the call into a verified one.
- `set_scene_commands {"commands": [{"action": …, "params": {…}, "expect": {…}}],
  "verify": true, "tab": …}` — an **ordered** list, stopping at the first error,
  each command re-reading the scene when `verify` is on.

`expect` maps a scene path (as `get_canvas_scene` names it, e.g.
`magnifiers.0.model.size_relative`, `dividers.0.visible`) to the value that must
hold afterwards. A command that ran but changed nothing is a **failure**, not a
success: the answer carries the observed value next to the expected one. Unknown
key, wrong type, or out-of-range value is refused with the schema in the hint.

Scene commands dispatch through the app's own handlers (the same path a user
action takes) — they never mutate the Redux Store directly.

### Batched actions (one call, many verbs)

Building a stand or driving a form one call per click is what makes an agent
turn expensive: the same wall-clock and context cost is paid N times. `ui_batch`
takes an ordered list and runs it as **one** call:

```json
{"actions": [
  {"tool": "run_action", "arguments": {"action_id": "platform.settings"}},
  {"tool": "set_setting", "arguments": {"key": "rhi_backend", "value": "vulkan"}},
  {"tool": "ui_click", "arguments": {"id": "settings.apply"}},
  {"tool": "wait_for", "arguments": {"element": "settings.status", "timeout_ms": 3000}},
  {"tool": "capture_surface", "arguments": {"surface": "settings"}}
], "settle_ms": 50}
```

- **Same verbs, same validation.** Each item calls the single tool's own handler,
  so live/fallback routing and error shapes are not duplicated. `BATCH_TOOLS`
  is the explicit whitelist; an unknown tool, an unknown key, a missing required
  key, or a nested `ui_batch` is refused **before anything runs**, with the
  allowed list in the hint.
- **A script, not a transaction.** Items run in order; there is no rollback and
  no undo. An earlier item stays applied when a later one fails, and `results`
  says exactly what ran (`{index, tool, ok, result|error}`). Default
  `stop_on_error=true` stops at the first failure (`stopped_at`); `false` runs
  the rest and still reports the first error.
- **Bounded on purpose.** At most 64 items, `settle_ms` 0–1000 per gap, and at
  most 30 s of accumulated settle; the pre-flight refusal means one call cannot
  occupy the request channel for minutes. Prefer `wait_for` inside the batch
  when the expectation is observable; `settle_ms` is for animations only.
- **No new honesty.** A batch inherits each verb's blindness: `ui_click` is
  in-process, so a batched click still does not prove a control is reachable
  (`ui_nav_state` + `nav_key` do), and `press_keys target=` is still aimed
  rather than typed. One batch result is N tool results, not stronger evidence.

### Element ids, filters, and stable property targets

`ui_elements` inventories the whole live window by default. Three optional
filters narrow **server-side** (the client no longer has to fetch every row and
diff it): `role` (exact), `label` (case-insensitive substring) and `properties`
(every name→value pair must equal the element's own value, exactly). `limit`
caps the returned rows at 2000; `total` is always the pre-limit match count, so
a short answer is never mistaken for a complete one. Filters narrow in **tree
order** and leave ids untouched — an id learned before a filter still resolves
after it. An unknown role/label/property value is an honest empty list, not an
error.

Every row carries a `properties` bag built from the widget's own Qt dynamic
properties — `gallery_path` on a gallery card is the motivating one. The rule
is deliberately narrow: scalars only (`str`/`bool`/`int`/`float`), names
starting with `_` skipped (Qt internals / private markers), oversized strings
dropped rather than truncated. Pointers, containers and enums never enter the
payload.

That bag also backs a **stable element reference**:
`property:<name>=<value>` (e.g. `property:gallery_path=/gallery/card_0.png`).
It is accepted in the same `id` position as an inventory id — `ui_click`,
`ui_hover`, `ui_focus`, `press_keys target=`, `nav_key anchor=` — and matches
the element's own property, so it survives tree reordering that changes
positional ids. The first match in tree order wins. A malformed reference, an
unknown property name, or zero matches is the existing named error: the hint
lists close property names (or the values that do exist) and points at
`ui_elements`. (`wait_for` matches rows by their inventory id, so it takes the
literal id, not a property reference.)

A second stable reference form aims at a **place** instead of a widget:
`point:<x>,<y>`. The pair is **global screen coordinates** — the same space a
row's `screen_rect` is in (add a widget-local offset from `rect` to it) — and
it resolves the widget a real pointer at that point would reach
(`QApplication.widgetAt`, which skips `WA_TransparentForMouseEvents` children,
so an overlay does not eat the event). It is accepted in the same `id` position
as an inventory id and a `property:` reference, which makes a widget the
inventory never lists — a paint-only timeline band, a mouse-transparent
overlay — addressable without the control layer naming a tab or a widget
class. Delivery follows the real pointer path: with `raw=true` the event stays
on the widget under the point; otherwise that widget gets it first and only an
unaccepted wheel falls through to the owning `QAbstractScrollArea` (a canvas
always keeps it). So a plain background still scrolls its area, while a widget
that owns the wheel — the timeline's `Ctrl+wheel` zoom — receives it at the
aimed point (a widget may zoom around the cursor, not its centre). A malformed
reference, or a point no visible window covers, is a named error listing the
live window bounds — never a silent delivery to the window.

`list_surfaces` keeps the two flags the app computes: `live` (the row is a
registered tab kind the live instance drives) and `capturable` (that kind's
page is OPEN right now, so `capture_surface surface=<name>` can grab it).
A registered-but-closed kind is `live: true, capturable: false` — open it
first. Devtool demo names are `live: false, capturable: false`.

### Judging keyboard reachability (`ui_nav_state` + `nav_key`)

The app's keyboard cursor is **not** `QApplication.focusWidget()`. Arrow keys
belong to `sli_ui_toolkit`'s `NavigationManager`, which keeps an ordered list of
*sections* (title bar → tab strip → session picker → a tab's toolbar/grid rows)
and hands a key to the section whose `owns()` accepts the focused widget; whole
surfaces (the Quick Look overlay, flyouts) instead handle keys in their own
`keyPressEvent` and are owned by no section. `press_keys` cannot judge that
model: it aims one event at one widget (right for a canvas, whose shortcuts are
not in the focus chain) and its events carry no focus attribution, so the
sections never engage and "the key reached the control" is indistinguishable
from "the key went nowhere". It does make the window OS-active first and report
`activated`/`focus_is_ours` (the same two fields `nav_key` reports), so a
delivered key is at least distinguishable from one the window system dropped.

Two tools close that gap; both are **live-only**:

| Call | What it answers |
|---|---|
| `ui_nav_state {}` | the model itself: ordered `sections` (section + owner + `extra_keys`), `focus`, `last_keyboard_focus`, `modality`, and every open in-window `overlays` entry with its focusable `children` — each child carrying `owned_by_section` (`null` = Qt could focus it, no section drives it: the honest "a key cannot reach this" signal) and `keyboard_ring` (the toolkit's own cursor-visibility flag) |
| `nav_key {keys, anchor?}` | what a key *does*: the event goes to the widget a user's key would reach (optionally focusing `anchor=<element id>` first, with the app's own focus-reason policy) and travels through the app's real filters; each `steps[]` entry reports `handled` — `section:<name>` (a navigation section acted), `focus` (Qt moved focus, no section), `surface` (the widget/surface's own `keyPressEvent` took it without moving focus), `unhandled` (the key was lost) — plus `accepted`, `focus_before`/`focus_after` and the owner before/after. `activated: false` means the app window could not be made OS-active, so the measurement describes an app a real keyboard could not reach either |

Reachability is therefore *measured*, not assumed: `ui_nav_state` shows which
controls no section owns, and `nav_key` shows whether a key aimed at one of
them changes anything (an `unhandled` step is the evidence, not a guess). Do not
substitute `ui_click` for this judgement — it delivers in-process, so it fires
a handler even for a widget a real mouse or keyboard cannot reach. Conversely,
`ui_click`'s in-process path cannot *reveal* a keyboard gap: `nav_key` is the
only honest instrument for it. The 2026-10-17 gallery session measured 20/20
Quick Look children unowned with `Down` unhandled at the action row; that panel
now registers its own modal section on show (`ui/quick_look/nav_section.py`,
`SECTION_ATTR`), so treat those numbers as the historical baseline, not as the
current state — re-measure with `ui_nav_state`/`nav_key` before quoting them.

`nav_key` measures the anchor's **window**, not always the main one. When an
`anchor` (also accepted as `from` / `element_id`) is given, the keys are
delivered in that widget's own top-level window — a dialog such as Settings or
Help registers its own sections and keeps its own focus chain, so a
dialog-anchored `Down` reports the *dialog's* section, where delivering to the
main window reported `section:SessionPickerSection` and moved focus inside the
main window instead. `window_source` in the result says which window was driven:
`"anchor"` (the anchor's own top-level), `"main"` (no anchor — the historical
target, unchanged for existing callers), or `"main-fallback"` (the anchor's
window disappeared mid-call). The `window` field describes that window in the
same row shape as `anchor`/`focus`, so a `handled` verdict is always attributable
to one window; an id that resolves to no widget in this instance still fails the
way it always has rather than falling back to the main window.

### Jobs: watching and stopping the long passes (P3)

`jobs_list` and `jobs_cancel` expose the passes that take minutes. Both are
**live-only** (in-flight work is in-memory state of a running app) and both
report only what the app already keeps — never a fabricated percentage.

`jobs_list` returns `{count, jobs: [{id, kind, status, progress, elapsed_s,
message}]}` with one row per *active* pass (an idle pass is absent — an empty
list really means nothing is running). Kinds, in priority order:

| kind | progress from | cancel safe? |
|---|---|---|
| `gallery_pool` | `GalleryController.sched_stats()` (`active`/`pending`/`queued`) + each item's `thumbnail_status` (`{done, total}`) | no |
| `metrics_pass` | the controller's own `_proxy_done` / `_proxy_total` chunk counters (`{done, total}` — the same numbers the app feeds `widget.set_background_scanning`) | no |
| `ai_analysis` | per-item `analysis_status` aggregated (`{done, total}` — there is no batch object) | no |
| `export` | the save task's toast progress bar (0–1 float) when the toast manager is already materialized; `null` otherwise | **yes** |

`status` is `running` or `paused` (the gallery's background thumb lane pauses
while the page is hidden). `progress` is `{done, total}` or a 0–1 float, or
`null` when the app reports no total — the honest alternative to inventing one.

**`elapsed_s` is a lower bound.** The app stamps no start time for these passes,
so it measures from the first `jobs_list` that saw the job running (app-process
scoped) and is `null` until then. It is never back-dated.

**`jobs_cancel {id}` — the real cancel, or a named refusal.** Only `export` has
a cooperative cancel today: the id names one in-flight save task, and the cancel
sets that task's own `threading.Event` — the same handle the toast's Cancel
button sets. `ExportService` checks it at each checkpoint and
`write_pil_image_cancelable` writes atomically (tmp + `os.replace`), removing
its tmp file on abort, so a cancelled export leaves **no partial artifact** at
the output path. A cancel-requested entry is then dropped from `jobs_list`
immediately rather than lingering as a "cancelling" zombie (the coordinator's
own registry entry survives until shutdown on the one worker shape that swallows
the cancel sentinel and returns `None` — an app-side follow-up, tracked in
`TODO.md`-style notes, not a lie in the API).

Every other kind answers a *named* refusal, because no safe cancel exists: the
gallery pool is a `QThreadPool` (`clear()` would silently drop queued thumbnails
and leave items unloaded), and the metrics and AI passes carry no cooperative
token. Cancelling there would be a fake, so it is refused:

```
jobs_cancel {id: "metrics:image_gallery"} →
  ok: false · "cancel is not supported for kind 'metrics_pass'"
  hint: "only exports expose a safe cancel today …"
```

**`notifications/progress` is deliberately not carried.** The transport
(`protocol.py`) is stdio request/response and emits no server→client
notifications, so progress cannot be pushed without faking it. The honest
substitute is `wait_for` (below) or a `jobs_list` poll while a long tool call is
pending. Recorded as a gap, not a feature.

### Waiting for an asynchronous expectation (`wait_for`)

Hand-written "poll `ui_elements` until the row appears / poll `jobs_list` until
the pass ends" loops are the expensive part of driving the app: the canonical
gallery scenario spent 3–5 of its 8–12 calls on bare polls. `wait_for` replaces
them. It is **read-only and live-only** (it observes an in-memory app, so with no
instance it refuses exactly like `ui_elements`).

Pass **exactly one** selector:

| Selector | Holds when |
|---|---|
| `element: <id>` | that id resolves **and** the row is `visible` and `enabled` (the inventory is read with disabled rows included, so "present but disabled" is observable rather than invisible) |
| `role: <role>` + `label: <label>` | an element with that exact role and exact, case-sensitive label exists (matched against the reported label) |
| `job_settled: true` (`job_kind:` optional) | no listed (running or paused) job remains; with `job_kind` only rows of that kind count |

`timeout_ms` defaults to 5000 and is hard-capped at 60000; `poll_ms` defaults to
100 with a 25 ms floor. Every observation goes through the same machinery the
read tools use (`ui_elements` / `jobs_list`), so `wait_for` invents neither a
second resolver nor a second job view. Sleeps are clamped to the remaining
budget, so the call does not overshoot `timeout_ms` by more than one poll
interval (a slow observation adds its own duration).

**`wait_for` saves tool calls, not wire round-trips.** The polling happens
inside the call, and every poll is still one `ui_elements` / `jobs_list` request
on the control socket — a caller pays them whether or not it sees them. With a
fixed `poll_ms` a wait that burns its whole budget costs
`ceil(timeout_ms / poll_ms)` requests in the worst case. Keep the two numbers
apart when reasoning about agent ergonomics: `src/devtools/scenario_cost.py`
reports `logical_steps` (one per declared step) and `wire_calls` (transport,
polls included) plus their ratio, and only the logical count is budgeted by
default (`--wire-budget` opts into a transport budget).

A success and a timeout are both **normal results**, never a tool error:

```
wait_for {element: "gallery.thumb.12", timeout_ms: 4000} →
  ok: true · waited_ms: 214 · source: "live"
  observed: {id: "gallery.thumb.12", role: "cell", visible: true, enabled: true, …}

wait_for {job_settled: true, job_kind: "export"} →
  ok: false · reason: "timeout" · waited_ms: 60000
  observed: [{id: "export:image_compare:3", kind: "export", status: "running", …}]
  hint: "a job of kind 'export' was still listed as running/paused; …"
```

`observed` is the last observed state — the element row, or the job rows (`[]`
when everything settled; `null` when a role+label pair never appeared) — so a
caller never needs a follow-up read just to see *why* the predicate missed.
Zero or more than one selector, a lone `role` or `label`, `job_settled: false`,
and a `job_kind` without `job_settled` are named validation errors; an
out-of-range budget is `-32602` from the input schema.

### Mutation contract: reads retry once, edits never

The control transport is local, so this is honest labelling more than
resilience — but it is what stops an agent from double-applying an edit:

- **Reads retry exactly once** when the wire times out (`list_actions`,
  `list_sessions`, `get_state`, `read_log`, `query_trace`, `dump_ui_layout`,
  `list_surfaces`, `ui_elements`, `ui_nav_state`, `jobs_list`); a second timeout
  falls back like any other transport failure.
- **Mutations never auto-retry** (`open_tab`, `run_action`, `open_folder`,
  `set_theme`, `set_language`, `capture_surface`, `press_keys`, `nav_key`,
  `ui_click`, `ui_hover`, `ui_focus`, `jobs_cancel`). A retry could double-apply
  the edit.
- **The two failure kinds stay distinguishable in the message text**: a
  transport failure *before* the request left the process says **"not sent"**
  (connect/write); one *after* the request was written says **"sent, no
  reply"** (read timeout, EOF, read error).
- **A mutating tool's timeout says the operation may have completed.** When a
  live edit was already on the wire and the deadline expired, the server does
  *not* fall back to a fresh process (that would risk applying the edit twice);
  it answers `isError` with the wire message plus "the operation may have
  completed", and the hint tells the caller to re-read state rather than retry.
  `list_actions` rows carry `enabled` + `reason` for exactly this: plan around a
  precondition instead of failing into it.

### A call that opens the app's own modal dialog is answered early

`press_keys`, `run_action` and `ui_click` run the action synchronously, and some
actions open a modal dialog: `ctrl+shift+o` → `platform.open_project` →
`QFileDialog.getOpenFileName` (`exec()`). A modal loop keeps the handler — and
therefore its reply — inside until someone closes the dialog, so the client used
to time out ("sent, no reply") on a chord that had already worked, and a retry
opened a second dialog.

The channel now answers such a request itself: if a handler is still running
after **2 s** (`MODAL_ACK_BUDGET_MS` in `src/core/control/server.py`), a
watchdog timer fires *inside* the dialog's nested loop and writes

```json
{"id": 7, "ok": true,
 "result": {"accepted": true, "answered_early": true, "still_running": true,
            "modal_open": true, "method": "press_keys", "budget_ms": 2000}}
```

- `ok: true` because the request *is* being carried out. A chord that reaches no
  window, an unknown key name, or an unknown action id still fails fast with its
  own named error — those refusals happen before the budget, not after it.
- `modal_open` is measured (`QApplication.activeModalWidget()`), not assumed: a
  native portal dialog is not a `QWidget`, so `false` there means "no Qt modal
  widget", not "nothing is open".
- **One frame per request.** The late real result is dropped instead of being
  sent as a second frame; a failure that arrives after the early answer is
  logged (visible through `read_log`), never re-sent.
- A fast call is untouched: full result, no `answered_early`.
- **The early result is transport-level, not the tool's own output shape.** It
  carries the fields above instead of e.g. `press_keys`' `pressed`/`count`. The
  catalog declares both shapes (`outputSchema.anyOf`) for the four tools whose
  handler can open a modal — `run_action`, `press_keys`, `ui_click`, `ui_press` —
  so a host that validates the declared schema accepts either frame. The MCP
  proxy still answers in its normalised result shape either way, so when the wire
  frame was the early one those fields describe an answer that has not arrived:
  the request is still running, so re-read state (or press `escape`) instead of
  trusting `pressed`/`count`.
- **Follow-up calls work while the dialog is up.** The MCP client opens one
  connection per request, and a new connection's first line is served *inside*
  the dialog's loop (`ok` as usual), so "read state to confirm / press `escape`"
  after an early answer behaves normally. A second line on the *same* connection
  waits until the dialog closes (Qt guards `readyRead` against re-entry).
- The dialog itself stays on screen and still has to be closed — send another
  `press_keys` (`escape` / `enter`) or let a human do it. When the goal is only
  to load content, prefer `open_folder` / `open_file` / `open_properties`, which
  open their surface without a modal loop.

### Flatpak-installed app (P0)

A host-side `mcp-server/app/server.py` **cannot attach to the app installed as a
Flatpak with the current defaults**, and no protocol change fixes that — the
blocker is the path. Inside the sandbox `$XDG_RUNTIME_DIR` is a private per-app
tmpfs mounted at the *same string* (`/run/user/1000`, observed as
`tmpfs[/.flatpak/io.github.Loganavter.Improve-ImgSLI/xdg-run]`, mode 700), so
host and app write two different `control.sock` files that happen to share a
name. Unix sockets themselves cross a *granted* path fine in both directions
(verified: a sandbox-created socket connected from the host, `SO_PEERCRED` uid
1000 on both sides).

Two things make route **A** work today with no rebuild — `flatpak override
--user --env=…` on the app, `IMGSLI_MCP_CONTROL_SOCKET` on the server:

```bash
# app side: keep the channel on and point it at the per-app data dir, whose path
# and inode are the same on both sides (Flatpak maps ~/.var/app/<id>/data into
# the sandbox; observed identical inode host vs sandbox).
flatpak override --user \
  --env=IMGSLI_CONTROL=1 \
  --env=IMGSLI_CONTROL_DIR="$HOME/.var/app/io.github.Loganavter.Improve-ImgSLI/data/ImproveImgSLI/run" \
  io.github.Loganavter.Improve-ImgSLI

# server side: name the socket explicitly (or set this env in the MCP client's
# `environment` block, next to IMGSLI_REPO_ROOT).
IMGSLI_MCP_CONTROL_SOCKET="$HOME/.var/app/io.github.Loganavter.Improve-ImgSLI/data/ImproveImgSLI/run/control.sock" \
  python3 mcp-server/app/server.py
```

The user restarts the Flatpak app with the channel enabled and sets the server
env var (or adds it to the MCP client config). No manifest change, no rebuild.

**PID namespace caveat (handled server-side).** The pointer's `pid` is
sandbox-local: a host `os.kill(pid, 0)` can raise `ProcessLookupError` for a live
sandboxed app (or `PermissionError` for an alien pid), which would make a
pid-only staleness check report "no live instance" while the socket is
connectable. `mcp-server/app/live.py` therefore treats the pid as a hint: a
pointer is stale only when the pid looks dead **and** a probe connect to the
socket fails; a socket that answers is used. The app-side
`src/core/control/discovery.py:stale_pointer_alive()` (startup single-instance
guard) is still pid-only — a leftover pointer from a crashed sandbox instance
can still make the next launch refuse, which is why route A wants an explicit
control dir and remains a documented fallback rather than the default.

Route **B** is the future zero-config end state: grant
`--filesystem=xdg-run/improve-imgsli:create` in `finish-args`, point the control
dir at `$XDG_RUNTIME_DIR/improve-imgsli`, and add that path as a discovery
candidate on both sides. The grant mechanism is verified on an installed app
(Discord's `xdg-run/pipewire-0` shows the host node at the same path with the
same inode); the app-created `:create` subdir is **not** verified yet — it needs
the manifest change and a real build. Route **C** (run the MCP server inside the
sandbox) is rejected: `build/Flatpak-template/io.github.Loganavter.Improve-ImgSLI.yaml`
installs `src/` only, so `mcp-server/` is absent from `/app`.

**Re-verify on the next Flatpak build** (recipe; nothing here builds): (1) inside
the sandbox, `findmnt -T "$XDG_RUNTIME_DIR"` must still show a private per-app
tmpfs and `$XDG_DATA_HOME`/`~/.var/app/<id>/data` the same inode as the host;
(2) with route A set, the host must see
`~/.var/app/io.github.Loganavter.Improve-ImgSLI/data/ImproveImgSLI/run/control.sock`
and `IMGSLI_MCP_CONTROL_SOCKET=… python3 mcp-server/app/server.py --check` must
answer; (3) for route B, grant the subdir and confirm a socket the sandbox
creates at `$XDG_RUNTIME_DIR/improve-imgsli` is present on the host at the same
string, then a host connect must succeed.

### Fallback (Phase 1: no live instance)

Phase 1 composes the primitives the app already ships:

- **State/catalog/sessions** — a read-only devtool flag,
  `python src/__main__.py --dump-app-snapshot <json>`, serializes the
  ActionRegistry catalog and Redux `Store` slices
  ([`src/devtools/app_snapshot.py`](../../src/devtools/app_snapshot.py)) and
  exits. Wired exactly like `--dump-ui-layout`. Snapshot reads are cached ~30 s.
- **Widget tree** — `--dump-ui-layout` (see [UI_LAYOUT_DUMP.md](UI_LAYOUT_DUMP.md)).
- **Log / tracer** — `log.txt` and `trace.jsonl` under the app data dir
  (see [LOGGING.md](LOGGING.md), [TRACING.md](TRACING.md)).
- **Mutations** — a detached `python src/__main__.py --open-tab … --run-action …`.
- **Screenshots** — `src/devtools/capture_surface.py` on a private Xvfb
  (see [SCREENSHOTS.md](SCREENSHOTS.md)).
- **Theme / language** — the app's own QSettings keys via a headless Qt
  subprocess; applies on next start.

Fallback safety rules (`mcp-server/app/process_policy.py`):

- **Headless by default.** A fallback boot gets
  `QT_QPA_PLATFORM=offscreen` with `DISPLAY`, `WAYLAND_DISPLAY` and
  `XDG_SESSION_TYPE` scrubbed, so it can never land a window on the user's
  desktop. (On this machine's Wayland session Qt ignores `DISPLAY` unless `xcb`
  is forced, which is exactly how a fallback spawn once opened a window on the
  live desktop.) `IMGSLI_MCP_DISPLAY` (a private `:97` Xvfb) is the explicit
  opt-in for a visible fallback window.
- **No duplicate windows.** `open_tab` reuses the tab's live fallback process —
  the result gains `reused: true` instead of spawning a second window.
- **The live log survives.** A boot truncates `log.txt`, so the server copies it
  to the artifacts dir (`live-log.txt`) before any boot while a live instance is
  attached, and `read_log` serves that preserved copy.

**Limitations, stated plainly:** fallback reads boot a throwaway headless app
and may be ~30 s stale; fallback mutations start a *new* process (they affect a
running window only through live attach); fallback `open_folder` reports
`routed: false`, so the folder must be dropped on the gallery (it opens
`--open-tab <tab>` when one was named). `press_keys` is
**live-only** — with no instance it errors honestly, it never fakes input. The
element-targeting tools (`ui_elements`, `ui_click`, `ui_hover`, `ui_focus`) are
live-only too: ids exist only in a running app, so with no instance they answer
an honest error rather than booting a throwaway app. A **live** capture
composites each visible canvas frame into the PNG (the swapchain surface is
in neither grab path), so the QRhi/GL canvas does show its content; a
devtool-booted surface still cannot grab it (see
[SCREENSHOTS.md](SCREENSHOTS.md) §Known limitation).

### Capture provenance (`capture_surface`)

`surface` is **optional**: omitting it captures the live instance's active
window (the app's own `resolve_capture_target` path). With no live instance the
fallback devtool has nothing to grab, so the call is refused — "surface is
required when no live instance is attached" — never answered with a different
surface. With a live instance but no window to grab, the app refuses with
"no live window to capture".

A live result keeps the app's provenance instead of flattening it: `what`
(what was actually grabbed), `method` (`grabWindow` / `widget_grab`), `size`
`[w, h]`, and `rhi_layers` (composited canvas frames; `0` = no presenting
canvas). Read `method`/`rhi_layers` before judging pixels — the
live-verification runbook makes them mandatory for exactly this reason.

`theme` and `out` are **fallback-only** parameters: the live handler takes only
`what`, writes to the app's artifacts directory and captures the running
window's own theme. A live result therefore reports `theme: null` and lists
nothing in `options_applied`; a fallback result names the applied ones
(`theme`, `out`) and puts `method`/`rhi_layers` in `unavailable` rather than
inventing values. (Fallback `size` is parsed from the devtool's own
`saved <path> <w>x<h>` line, `null` if absent.)

## Security model

- **Local only.** stdio subprocess, never TCP/HTTP; no network code.
- **Allowlisted actions.** `run_action` rejects ids absent from the
  ActionRegistry snapshot *before* launching, with close-match suggestions.
- **Existing directories only** for `open_folder`; no traversal. `tab=<kind>`
  targets that tab instead of the first tab that accepts a directory (the tab
  is opened if absent); a file-only tab (`multi_compare`, `image_compare`)
  cannot accept a directory, so it receives the folder's images as files
  (non-recursive, shared extension set) — that is the supported way to load
  images into those tabs, whose own "add images" dialog a client cannot drive.
- **A drop can be placed without a pointer.** A mouse drop derives the target
  from the cursor; a headless client has none, so `open_folder` and `open_file`
  accept the same two hints the tabs already read, one per call (both together
  is an error): `slot=1|2` picks the Image Compare slot (left/right — two
  calls compose a pair, and for `open_file` that is the whole pair: one call
  per slot) and `side=left|right|top|bottom` picks the Multi Compare split axis
  used for the folder's images after the first, so a multi-file folder composes
  a row (`right`) instead of the auto column. The first image of a folder still
  lands by the usual auto placement (it has no target to split yet), and a
  single-file drop is therefore unaffected by `side`. Anything other than the
  listed values is rejected with the app's own error (`unknown slot`,
  `unknown side`) rather than silently dropped. Omitting both keeps the plain
  auto-placement route. A hint is a *placement* request, not a promise the
  receiving tab understands it: a tab that ignores `slot`/`side` still gets the
  file, exactly as with a mouse drop outside a slot.
- **No file contents.** Only the app's own log/tracer/catalog/layout artifacts
  are returned — never user images or arbitrary files.
- **Read-only default**; mutations are distinct named tools with honest
  `annotations` (`run_action` sets `destructiveHint: true`; `press_keys` and
  `ui_click` are non-destructive but non-idempotent).
- **Keyboard input is live-only.** `press_keys` injects into the running app
  through the control socket; with no instance it answers an honest error
  rather than pretending. It activates the target window first and reports
  `activated`/`focus_is_ours`; when the app still reports no active window the
  call is refused with the `xdotool windowfocus <window-id>` hint instead of
  answering `ok` for keys nobody saw. Key names are allowlisted app-side: named keys,
  single letters/digits, and the punctuation a path or text field needs
  (`/`, `.`, `-`, `_`, `~`, `=`, `:`, …) — one combo per key, e.g.
  `/,t,m,p,/,a,.,p,n,g` types `/tmp/a.png` (`','` and `'+'` are the
  separator characters and stay unavailable). `target=<id from ui_elements>`
  aims the sequence at that element: a live canvas is not in the window's
  focus chain, so canvas shortcuts (`=`, `-`, `0` in multi-compare) only
  arrive when the canvas id is named.
- **Element targeting is live-only.** `ui_elements` reads the running window's
  widget inventory; `ui_click`/`ui_hover`/`ui_focus` act on an id (or a
  `property:<name>=<value>` / global `point:<x>,<y>` reference) from it. With no
  instance they answer an honest error — no throwaway app is booted to fake a
  target.

### Refusals (no silent truncation)

Every refusal below is a named, actionable answer — never a truncated or
fabricated result:

| Refused | Enforced by | Answer |
|---|---|---|
| unknown MCP method | this server (`protocol.py`) | JSON-RPC `-32601` |
| request line over the 1 MiB cap | this server (stdio) | JSON-RPC `-32600` "request line exceeds the … byte cap" |
| unknown control-channel method | live app | `ok: false` + hint, connection survives |
| foreign uid | live app (`SO_PEERCRED`, before reading a byte) | connection closed → the call falls back |
| non-live `open_file` | this server (`_fallback_open_file`) | `isError` "no live instance; open_file routes a file into a running tab" (the path is validated first; there is no headless fallback that opens anything — a `slot`/`side` hint never turns this refusal into a success) |
| non-live `press_keys` / `nav_key` / `ui_elements` / `ui_nav_state` / `ui_click` / `ui_hover` / `ui_focus` | this server | `isError` "needs a running app" (never a throwaway app faking a target) |
| live `press_keys` with no OS-active window | live app (`press_keys_into`) | `ok: false` "the app window is not the OS-active window, so the key would be dropped" + "activate it first (xdotool windowfocus <window-id>, …)" — never `ok` for a key the window system drops |
| the app died while a request was in flight | this server (`death_message`) | `isError` "sent, no reply: the app died while request `'press_keys'` (`keys='ctrl+p'`) was in flight — pid … is gone; its exit status and signal are not observable" + a re-read hint; a request that hit the deadline is reported as possibly applied, and a missing instance as a death when the pointer/note say one existed — never a plain "no live instance" |
| non-live `jobs_list` / `jobs_cancel` | this server | `isError` "no live instance; job state needs a running app" (never a fake empty list) |
| non-live `wait_for` | this server | `isError` "no live instance; waiting for app state needs a running app" (never a silent 5 s timeout) |
| `jobs_cancel` on a kind with no cooperative cancel | live app | `ok: false` "cancel is not supported for kind …" + hint; never a fake cancel |
| `jobs_cancel` on an unknown or settled id | live app | `ok: false` "no running job with id …" + `jobs_list` hint |
| unknown action id / tab / state path | this server (fallback) | `isError` + close-match suggestions |
| unknown surface | live app (`capture_live`) or this server (`capture_surface` fallback) | `isError` "unknown surface" + `list_surfaces` hint |
| surface known but not open in the live app | live app | `isError` "surface … is not open in the live app" |
| no `surface` and no live instance | this server (`capture_surface` fallback) | `isError` "surface is required when no live instance is attached" + `list_surfaces` hint |
| no active window for a live capture (no `surface`) | live app (`capture_live`) | `ok: false` "no live window to capture" + "pass what=<tab> … or focus a window first" |
| more than 32 key combos in one `press_keys` | live app | `ok: false` "too many key combos" |

The death answer leans on `<runtime>/inflight.json`: the app writes the request
it is about to handle there — method, a redacted param digest, pid, timestamp —
before the handler runs and removes it once the reply is sent, so a note left
behind is the one thing a sibling process can read after a segfault (no
teardown, no reply) and it names what the app died doing.

### Annotation audit (2026-10-17)

The `annotations` hints were checked against what each handler actually does,
because that is what lets a client auto-approve reads:

- **Read-only tools really read.** All twelve (`list_actions`, `list_sessions`,
  `get_state`, `read_log`, `query_trace`, `dump_ui_layout`, `list_surfaces`,
  `ui_elements`, `ui_nav_state`, `jobs_list`, `wait_for`, `get_canvas_scene`)
  carry `readOnlyHint: true, destructiveHint: false`
  and touch no app state — `ui_elements` only *inventories* the live window,
  `jobs_list` only reads the app's own counters (it never resolves a lazy tab
  service, which would materialize a tab as a side effect of listing), and
  `wait_for` only repeats those two observations until a predicate holds. Reads
  may retry once (above); none mutate.
- **Mutating tools are honestly marked.** All eighteen carry `readOnlyHint: false`.
  `run_action` alone advertises `destructiveHint: true` (it can run any
  registered action, including `platform.quit_app`). `press_keys`, `nav_key`,
  `ui_click`, `ui_wheel`, `ui_press` and
  `jobs_cancel` are `destructiveHint: false` but `idempotentHint: false`
  (repeating the same combo/click/press is not a no-op; re-sending a settled
  job id is an error, not a second success). `ui_hover`/`ui_focus` settle to the
  same state, so they keep `idempotentHint: true`.
  `open_tab`/`open_folder`/`open_file`/`open_properties`/`set_theme`/
  `set_language`/`set_setting`/`capture_surface`/`set_scene_commands` are
  non-destructive and idempotent.
- `openWorldHint: false` on every tool (the app is local). **Result: no
  annotation was found to over- or under-state what its tool does.** The test
  suite pins the classification (`test_read_and_write_tools_do_not_mix_annotations`,
  `test_element_tool_annotations_are_audited`,
  `test_job_tool_annotations_are_audited`).

The live channel is the in-app control server: a unix socket in the control dir
(env-gated, no TCP), refusing connections from other uids. Reads and mutations
on a live instance are the same tools — the answering path is visible in every
result as `source`.

## Verifying a live instance

A capture or a state read answers a question only after you know **what you are
looking at**. This section is the method; how the instance is hosted (display,
virtual screen, container) is outside this document.

### Prove the canvas is live before judging it

- `ui_elements` must list a row with `"role": "canvas"` for the tab you are
  about to judge (a live `QRhiWidget` inventories as `canvas`).
- The log must show the canvas working (`query_trace` / `read_log`): a first
  frame or takeover line for that tab.
- `capture_live` / `capture_surface` report `rhi_layers`: `0` on a tab that has
  a canvas means **this build cannot composite it** — not that the canvas is
  blank.

### Three layers, three owners

| Layer | Who owns it | Visible to |
|---|---|---|
| cover / plate (the pre-first-frame image) | a sibling `QLabel` | window grab **and** raster grab |
| canvas raster frame (the image inside the canvas) | the canvas' own pass | the canvas framebuffer (`rhi_layers`) |
| tiled handover (seam-safe tiles) | the canvas' own pass | same as above |

A capture that composes canvas frames shows all three; one that does not shows
chrome and whatever raster layer sits behind the canvas.

### Artifact signatures

| Signature | It is | Do |
|---|---|---|
| an all-black band whose edge sits at `screen_width − window_x` | the window is partly **off screen** | check the window rect against the screen size; move it or use a build that fits its saved rect |
| the whole canvas area black while the log says it drew | canvas not composited, or genuinely off screen | read `rhi_layers` first, then the geometry |
| black over toolbar/footer/controls | the same off-screen band, not broken chrome | same as above |
| the window grab is black but the canvas framebuffer has content | presentation vs content split | trust the canvas frame, then re-check geometry |
| keys do nothing | they went to whatever holds focus | aim them at the target element, or use `nav_key` |
| a pointer gesture does nothing | the pointer was outside the window, or no leaf under it | move the pointer inside first; prefer keys |
| two captures in the same second overwrite each other | filename granularity is 1 s | wait ≥ 1 s between captures |

### Before calling something broken

1. Is the window fully on screen (rect vs screen)?
2. Is the capture honest (`method`, `rhi_layers`, plus the log line that should
   accompany the state)?
3. Did the state change with an input you can name (hash before/after)?
4. Does it reproduce in a second, independent probe (canvas frame, log, moved
   or resized window)?

A "defect" that exists in one capture with no log or second-probe support is an
artifact until proven otherwise.

### Do not

- Judge a canvas from a capture of a window that is not fully on screen.
- Read black as a broken widget before checking geometry.
- Conclude "the render path paints black" from a window grab alone.
- Expect a chord that opens one of the app's own file dialogs to return the
  dialog's *outcome* — it is answered `answered_early` while the dialog is up
  (see "A call that opens the app's own modal dialog is answered early"). Use
  `open_folder {path, tab}` when you only need content loaded (a file-only tab
  receives the folder's images as files).
- Restore a saved window rect without fitting it to the current screen.
- Invent a second read path for a scene value: `get_canvas_scene` is the one
  read verb, `set_scene_commands` the one write verb (see the canvas section).

### Appearance step (interface scale / language) — mandatory for UI waves

The parity gate (`tests/runtime/test_ui_metrics_parity.py`) is the offscreen
tripwire; this is the observed half:

1. Set the interface scale to **1.25** (Settings → Appearance), keep the window
   size, and read the dialog under test: its minimum width must still cover the
   scaled content (no clipped row, no label under its field, no bracket narrower
   than its contents). Repeat at **1.5**.
2. Change the language with the dialog open where the surface supports it:
   labels and the CSD menu strip must retranslate in the same interaction. A
   surface the host rebuilds per open (export, image-properties, the AI settings
   page) only has to be correct *when opened after* the change — the gate
   records that as `REBUILD`.
3. Capture the dialog at each factor (same window, no resize) and compare row
   pitch and the focus ring: the ring must stay separated from the control's
   content at every factor.
4. A surface that only re-lays-out after a second event-loop turn is a bug, not
   a slow render.

## Known limitations

- **`ui_click {button: right}` hides the menu it just opened (P1 tool defect,
  recorded 2026-10-17).** A right-click by element id delivers the mouse button
  event *and* an extra `QContextMenuEvent`. The app's menu manager treats that
  second event as the "click that opened me", which consumes its open-click
  grace window, so the context menu collapses ~150 ms after opening. The click
  itself reaches the handler; only the visible menu life is wrong. **Not fixed
  here** — the fix belongs to the gallery / control-element lane (the same
  surface as the collapsed-card-menu bug).
- A live QRhi/GL canvas capture is composited from the canvas's own last
  frame (see [SCREENSHOTS.md](SCREENSHOTS.md) §Known limitation) — a canvas
  that has not rendered yet contributes nothing.

## The `mcp` SDK

`mcp-server/requirements.txt` pins `mcp>=2.3` (verified to support
`2026-07-28`). It is optional: `mcp-server/app/protocol.py` implements the
stdio JSON-RPC surface with the standard library only, because this
environment's Python is PEP 668 externally-managed with root-owned
site-packages. The catalog/backend are transport-agnostic and can be registered
with the SDK's low-level `Server` unchanged.

## See also

- [mcp-server/app/README.md](../../mcp-server/app/README.md) — full surface + wiring
- [TRACING.md](TRACING.md) — the causal tracer `query_trace` reads
- [LOGGING.md](LOGGING.md) — `log.txt` layout `read_log` parses
- [UI_LAYOUT_DUMP.md](UI_LAYOUT_DUMP.md) — the widget-tree dump
- [SCREENSHOTS.md](SCREENSHOTS.md) — Xvfb requirements for `capture_surface`
- Private `improve-imgsli-internal-docs` repo →
  `docs/dev/RUNBOOK_LIVE_MCP_VERIFICATION.md` — the **host** half only:
  private display + isolated `XDG_*`, the RHI backend matrix for this machine,
  and the window-on-screen check. The method (what to prove, which artifact to
  trust, what not to conclude) lives in § *Verifying a live instance* above.
