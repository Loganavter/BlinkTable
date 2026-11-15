"""MC slots to demand pyramid (GIMP-style, E2 ``demand_level`` consumer).

Arrival kicks ``demand_level(k_fit)`` — never the eager full build — and
zoom-ins demand only the newly needed level through the same coordinator
(same toast/abort routing; built levels never rebuild). The resolve
pyramid gate (``pipeline/cache.py``, untouched) keeps holding the preview
until the needed tier is ready — verified here, not redesigned.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtGui import QColor, QImage

from tabs.multi_compare.use_cases import loading as loading_use_cases


class _Store:
    """Sized pixel-store stand-in (``size`` + ``is_open`` protocol)."""

    def __init__(self, w, h):
        self._size = (int(w), int(h))
        self.is_open = True

    @property
    def size(self):
        return self._size


class _Pyramid:
    """Demand-observable pyramid: only level 0 built unless told otherwise."""

    def __init__(self, *, complete=False, built=(0,)):
        self._complete = complete
        self._built = set(built)
        self.valid = True

    def is_level_built(self, k):
        return k in self._built

    def is_complete(self):
        return self._complete

    @property
    def level_count(self):
        return max(self._built) + 1 if self._built else 1


class _SpyCoord:
    """E2-shaped spy: records ``demand_level``/``start_build`` calls."""

    def __init__(self):
        self.demanded: list[tuple[int, dict]] = []
        self.started: list = []
        self.aborts: list = []

    def demand_level(self, store, k, **kw):
        self.demanded.append((int(k), kw))
        abort = kw.get("should_abort")
        if abort is not None:
            self.aborts.append(abort)
        return True

    def start_build(self, store, **kw):
        self.started.append(kw)
        return True


class _Rect:
    def __init__(self, w, h):
        self._w, self._h = w, h

    def width(self):
        return self._w

    def height(self):
        return self._h


def _color_image(r=40, g=120, b=200):
    img = QImage(8, 8, QImage.Format.Format_RGBA8888)
    img.fill(QColor(r, g, b))
    assert not img.isNull()
    return img


def _write_file(path: Path, payload: bytes = b"x") -> Path:
    path.write_bytes(payload)
    return path


def _make_controller(*, coord, slots, zoom=1.0, canvas=None, cache=None):
    dispatched: list = []

    def _dispatch(action):
        dispatched.append(action)

    mc_store = SimpleNamespace(dispatch=_dispatch, dispatched=dispatched)
    widget = SimpleNamespace(
        state=SimpleNamespace(slots=list(slots), zoom=zoom),
        store=mc_store,
        canvas=canvas,
    )
    controller = SimpleNamespace(
        widget=widget,
        pixel_cache=cache,
        _pyramid_coordinator=coord,
    )
    return controller, dispatched


def _slot(sid, path):
    return SimpleNamespace(id=sid, path=path)


def _patch_pyramid(monkeypatch, pyramid):
    import engine.store.pyramid_registry as _reg

    monkeypatch.setattr(_reg, "ensure_pyramid", lambda _s: pyramid)


def test_arrival_kicks_demand_k_fit_never_full_build(monkeypatch, tmp_path):
    """Full arrival demands k_fit=3 (8000x6000 fit into 1000x800 @1x)."""
    from tabs.multi_compare.pipeline.cache import MultiComparePixelCache

    f = _write_file(tmp_path / "big.png", b"big-payload")
    cache = MultiComparePixelCache()
    cache.put_preview(f, _color_image())
    coord = _SpyCoord()
    leaf = SimpleNamespace(slot_id=7)
    canvas = SimpleNamespace(_leaf_rects=lambda: [(leaf, _Rect(1000, 800))])
    controller, dispatched = _make_controller(
        coord=coord, slots=[_slot(7, f)], zoom=1.0, canvas=canvas, cache=cache
    )
    _patch_pyramid(monkeypatch, _Pyramid(complete=False))
    store = _Store(8000, 6000)

    loading_use_cases.apply_full_resolution(controller, 7, f, store)

    assert [a for a in dispatched if getattr(a, "tier", None) == "full"]
    assert cache.get_pixel(f) is store
    assert coord.started == [], "arrival must never kick the eager full build"
    assert len(coord.demanded) == 1
    k, kw = coord.demanded[0]
    assert k == 3, f"k_fit for fit-view dest_scale 0.125 over 4 levels is 3, got {k}"
    assert kw.get("slot_id") == 7
    assert callable(kw.get("should_abort"))


def test_arrival_small_image_demands_zero(monkeypatch, tmp_path):
    """A 400x300 image fits at scale>1: demand k=0 (base suffices, no worker)."""
    from tabs.multi_compare.pipeline.cache import MultiComparePixelCache

    f = _write_file(tmp_path / "small.png", b"small-payload")
    cache = MultiComparePixelCache()
    cache.put_preview(f, _color_image())
    coord = _SpyCoord()
    leaf = SimpleNamespace(slot_id=3)
    canvas = SimpleNamespace(_leaf_rects=lambda: [(leaf, _Rect(1000, 800))])
    controller, _ = _make_controller(
        coord=coord, slots=[_slot(3, f)], zoom=1.0, canvas=canvas, cache=cache
    )
    _patch_pyramid(monkeypatch, _Pyramid(complete=False))
    store = _Store(400, 300)

    loading_use_cases.apply_full_resolution(controller, 3, f, store)

    assert coord.started == []
    assert len(coord.demanded) == 1
    assert coord.demanded[0][0] == 0


def test_zoom_in_demands_newly_needed_level(monkeypatch, tmp_path):
    """Zoom 1x→2x on the big slot needs k=2 (only base+fit built so far)."""
    from tabs.multi_compare.pipeline.cache import MultiComparePixelCache

    f = _write_file(tmp_path / "zoom.png", b"zoom-payload")
    cache = MultiComparePixelCache()
    cache.put_preview(f, _color_image())
    store = _Store(8000, 6000)
    cache.put_pixel(f, store)
    coord = _SpyCoord()
    controller, _ = _make_controller(
        coord=coord, slots=[_slot(7, f)], zoom=2.0, cache=cache
    )
    _patch_pyramid(monkeypatch, _Pyramid(complete=False, built=(0,)))

    ok = loading_use_cases.demand_for_zoom(
        controller, 7, 2.0, rect=(1000, 800), fit=(1.0, 0.9375)
    )

    assert ok is True
    assert coord.started == [], "zoom must never kick the eager full build"
    assert len(coord.demanded) == 1
    assert coord.demanded[0][0] == 2
    assert coord.demanded[0][1].get("slot_id") == 7


def test_built_levels_never_rebuild_real_coordinator():
    """E2 already-built short-circuit: True with no pool, no worker."""
    from engine.pyramid.coordinator import PyramidBuildCoordinator

    coord = PyramidBuildCoordinator(
        get_thread_pool=lambda: None,  # would fail if a worker were attempted
        toast_coordinator=None,
        get_pyramid=lambda _s: _Pyramid(complete=False, built=(0, 1, 2)),
    )
    assert coord.demand_level(_Store(8000, 6000), 2, slot_id=9) is True
    assert coord.pyramid_builds == set(), "no worker for a built level"


def test_slot_remove_aborts_pending_demand(monkeypatch, tmp_path):
    """The captured abort flips once the slot is removed or recycled."""
    from tabs.multi_compare.pipeline.cache import MultiComparePixelCache

    f = _write_file(tmp_path / "abort.png", b"abort-payload")
    other = _write_file(tmp_path / "other.png", b"other-payload")
    cache = MultiComparePixelCache()
    cache.put_preview(f, _color_image())
    store = _Store(8000, 6000)
    cache.put_pixel(f, store)
    coord = _SpyCoord()
    slots = [_slot(7, f)]
    controller, _ = _make_controller(
        coord=coord, slots=slots, zoom=2.0, cache=cache
    )
    live = controller.widget.state.slots  # mutate the live list below
    _patch_pyramid(monkeypatch, _Pyramid(complete=False, built=(0,)))

    assert loading_use_cases.demand_for_zoom(
        controller, 7, 2.0, rect=(1000, 800), fit=(1.0, 0.9375)
    ) is True
    assert len(coord.aborts) == 1
    abort = coord.aborts[0]
    assert abort() is False, "live slot must not abort"

    slots.clear()  # alias kept in sync: clear the live list too
    live.clear()
    assert abort() is True, "removed slot aborts the pending demand"

    live.append(_slot(7, other))  # id recycled by a newer generation
    assert abort() is True, "recycled id aborts the stale demand"

    live[:] = [_slot(7, f)]
    assert abort() is False, "live generation does not abort"


def test_resolve_gate_holds_preview_until_needed_tier_ready(monkeypatch, tmp_path):
    """Gate + demand agree: k_fit built but incomplete → preview holds."""
    from engine.store.tiled_pixel_store import TiledPixelStore
    from tabs.multi_compare.pipeline import cache as mc_cache

    class _GatedStore(TiledPixelStore):
        # Declared, not inherited: the double skips ``__init__``, so the real
        # ``is_open`` (which reads the mapping) would read it as closed and the
        # cache's usability gate would drop it before the pyramid gate ran.
        is_open = True

        def __init__(self):
            pass

    store = _GatedStore()
    preview = _color_image(9, 200, 9)
    path = str(tmp_path / "gate.png")
    c = mc_cache.MultiComparePixelCache()
    c.put_pixel(path, store)
    c.put_preview(path, preview)
    pyramid = _Pyramid(complete=False, built=(0, 1, 2, 3))

    monkeypatch.setattr(mc_cache, "pyramid_for", lambda _s: pyramid, raising=False)
    import engine.store.pyramid_registry as _reg

    monkeypatch.setattr(_reg, "pyramid_for", lambda _s: pyramid, raising=False)

    assert c.resolve(path) == preview, (
        "demand-built k_fit must not flip the display early: "
        "the gate holds the preview until the pyramid completes"
    )
    assert c.source_size(path) == (8, 8)
    pyramid._complete = True
    assert c.resolve(path) is store, "gate opens once the needed tier is ready"


def test_no_geometry_falls_back_to_eager_parity(monkeypatch, tmp_path):
    """Headless arrival (no canvas): legacy eager kick, no demand."""
    from tabs.multi_compare.pipeline.cache import MultiComparePixelCache

    f = _write_file(tmp_path / "headless.png", b"headless-payload")
    cache = MultiComparePixelCache()
    cache.put_preview(f, _color_image())
    coord = _SpyCoord()
    controller, _ = _make_controller(
        coord=coord, slots=[_slot(5, f)], zoom=1.0, canvas=None, cache=cache
    )
    _patch_pyramid(monkeypatch, _Pyramid(complete=False))
    store = _Store(8000, 6000)

    assert (
        loading_use_cases.demand_pyramid_for_slot(controller, store, slot_id=5)
        is False
    )
    assert coord.demanded == []
    assert len(coord.started) == 1, "eager parity fallback keeps today's behavior"
