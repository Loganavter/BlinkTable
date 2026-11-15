"""Capability gate: every *offered* export format must be really writable.

``writable_formats()`` is a claim about the installed encoders, and it drives
the export dialog's format combo for both comparison tabs. That pair had
already drifted: the combo offered ``JXL`` to both tabs while only one tab had
a JXL encoder branch — the other handed ``format="JXL"`` straight to Pillow,
which has no JXL plugin, so the save died with ``KeyError: 'JXL'`` after the
user had configured everything. Offering a format is therefore never a static
list decision; this gate fails unless an encoder on *this* machine really
produces it.

Rule per format:

* **Pillow-backed** (the primary writer, ``shared.image_processing.
  format_capability.encode``): the format must appear in ``PIL.Image.SAVE``
  (the encoder registry, not the decoder one) of the installed build.
* **Alternative backend**: a format Pillow has no encoder for is allowed only
  through :data:`NON_PILLOW_ALLOWLIST`, which names the reason and the backend.
  The claim is re-probed here *independently* of the module under test — if the
  backend cannot encode on this machine the format must not be offered. The
  allowlist is **shrink-only**: an entry whose format Pillow encodes now is
  stale and fails just as loudly as a missing one.
* Anything else fails.

The last tests keep the gate from being vacuous: :func:`unsupported_formats`
is fed a format no backend has (and must report it), the format row of the
export dialog is read from its AST (it must iterate ``writable_formats()`` and
must not re-introduce a literal list), and every offered format is required to
round-trip through the shared writer. The end-to-end claim for the multi-
compare *service* (the path that crashed with ``KeyError: 'JXL'``) lives in
that tab's own suite, because root tests must not deep-import tab internals.
"""

from __future__ import annotations

import ast
import io
import io

import pytest
from PIL import Image

from shared.image_processing.format_capability import (
    encode,
    formats_with_alpha,
    suffix_for_format,
    writable_formats,
)

#: format -> (why Pillow cannot write it, backend id the probe below uses).
#: SHRINK-ONLY: delete an entry the moment Pillow registers an encoder for the
#: format. Never add an entry to turn a red gate green -- that is exactly the
#: drift this gate exists to stop.
NON_PILLOW_ALLOWLIST: dict[str, tuple[str, str]] = {
    "JXL": (
        "No Pillow JXL plugin in any supported build; imagecodecs (libjxl) "
        "writes it -- see imagecodecs.imwrite(..., codec='jxl') in "
        "shared/image_processing/format_capability.py.",
        "imagecodecs",
    ),
}


def pillow_writable_formats() -> frozenset[str]:
    """Format names Pillow registers an *encoder* for (installed build)."""
    Image.init()
    return frozenset(str(name).upper() for name in Image.SAVE)


def _imagecodecs_writes_jxl() -> bool:
    """Independent re-probe: can ``imagecodecs`` actually encode JXL here?

    Deliberately does not import the module under test -- a gate that asks the
    implementation whether it is right proves nothing.
    """
    try:
        import imagecodecs
        import numpy as np
    except Exception:
        return False
    probe = np.zeros((2, 2, 3), dtype=np.uint8)
    # The dedicated encoder first: imagecodecs 2026.x rejects
    # ``imwrite(..., codec="jxl")`` ("no imagecodec specified") while
    # ``jpegxl_encode`` works, and a gate that only knew the old spelling
    # reported "no JXL encoder" on a machine that had one (measured 2026-11-15).
    encode = getattr(imagecodecs, "jpegxl_encode", None)
    try:
        if callable(encode):
            return bool(encode(probe, lossless=True))
        buffer = io.BytesIO()
        imagecodecs.imwrite(buffer, probe, codec="jxl", lossless=True)
    except Exception:
        return False
    return bool(buffer.getvalue())


_BACKEND_PROBES = {"imagecodecs": _imagecodecs_writes_jxl}


def unsupported_formats(formats) -> dict[str, str]:
    """Map each format no installed encoder can write to its failure reason.

    An empty mapping means every offered format is backed by a real encoder.
    """
    pillow = pillow_writable_formats()
    failures: dict[str, str] = {}
    for fmt in sorted(formats):
        if fmt in pillow:
            continue
        entry = NON_PILLOW_ALLOWLIST.get(fmt)
        if entry is None:
            failures[fmt] = "not writeable by Pillow and not in NON_PILLOW_ALLOWLIST"
            continue
        reason, backend = entry
        probe = _BACKEND_PROBES.get(backend)
        if probe is None:
            failures[fmt] = f"allowlisted for unknown backend {backend!r}"
            continue
        if not probe():
            failures[fmt] = (
                f"allowlisted for {backend}, but that backend cannot encode it "
                "on this machine"
            )
    return failures


def test_every_offered_format_has_an_encoder():
    """The offered list may only contain formats an installed encoder writes."""
    failures = unsupported_formats(writable_formats())
    assert failures == {}, (
        "writable_formats() offers format(s) no installed encoder can write: "
        f"{failures}. Drop the format from the offer list, or -- only for a "
        "format a non-Pillow backend really encodes here -- add a justified "
        "NON_PILLOW_ALLOWLIST entry. A format offered without an encoder is "
        "exactly the 'KeyError: JXL on save' bug this gate exists to prevent."
    )


def test_offer_order_is_stable_and_known():
    """Base formats first, then optional ones; nothing unknown sneaks in."""
    offered = writable_formats()
    known = {"PNG", "JPEG", "WEBP", "BMP", "TIFF", "JXL"}
    assert set(offered) <= known, f"unexpected offered formats: {set(offered) - known}"
    base = [fmt for fmt in offered if fmt != "JXL"]
    assert base == ["PNG", "JPEG", "WEBP", "BMP", "TIFF"][: len(base)], (
        "writable_formats() must keep the stable base order "
        f"PNG, JPEG, WEBP, BMP, TIFF -- got {base}"
    )
    assert "JXL" not in offered[:-1], "optional formats must come last"
    assert isinstance(offered, tuple)


def test_allowlist_entries_are_justified_and_shrink_only():
    """Each entry needs a real reason, and must vanish once Pillow covers it."""
    offered = set(writable_formats())
    for fmt, (reason, backend) in sorted(NON_PILLOW_ALLOWLIST.items()):
        assert fmt not in pillow_writable_formats(), (
            f"stale NON_PILLOW_ALLOWLIST entry {fmt!r}: Pillow registers an "
            "encoder for it now, so the entry is no longer needed -- delete it "
            "(the list is shrink-only; a stale exemption is how a gate rots)."
        )
        assert len(reason.strip()) >= 30, (
            f"NON_PILLOW_ALLOWLIST[{fmt!r}] must name the backend that writes "
            "the format and why Pillow cannot; a bare 'needed' is a rubber stamp."
        )
        assert backend in _BACKEND_PROBES, (
            f"NON_PILLOW_ALLOWLIST[{fmt!r}] names backend {backend!r}, which "
            "has no independent probe here -- add one instead of trusting the "
            "implementation to report itself healthy."
        )
        # Offered iff the backend really encodes it: both directions matter,
        # a silently dropped working format is a regression too.
        probe_ok = _BACKEND_PROBES[backend]()
        assert (fmt in offered) is probe_ok, (
            f"{fmt!r} offered={fmt in offered} but the {backend} probe "
            f"reports {probe_ok} -- the offer list and the encoder disagree."
        )


def test_alpha_set_is_single_sourced():
    """The alpha policy has one definition; modules must not re-declare it."""
    from pathlib import Path

    assert formats_with_alpha() == frozenset({"PNG", "TIFF", "WEBP", "JXL"})
    root = Path(__file__).resolve().parents[2]
    for relative in (
        "src/shared/image_processing/export_encoding.py",
        "src/shared/image_processing/pil_save.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "_FORMATS_WITH_ALPHA" not in source, (
            f"{relative} re-declares the alpha set; import "
            "formats_with_alpha() from shared.image_processing.format_capability."
        )


def _build_format_row_node():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    path = root / "src/plugins/export/dialog_sections.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_format_row":
            return node
    raise AssertionError("build_format_row not found in dialog_sections.py")


def test_export_dialog_offers_writable_formats_only():
    """The combo is built from the capability list, not a literal format list."""
    node = _build_format_row_node()

    iterated_sources = []
    for for_node in (n for n in ast.walk(node) if isinstance(n, ast.For)):
        iterated_sources.append(ast.unparse(for_node.iter))
    assert any(
        "writable_formats()" in source for source in iterated_sources
    ), (
        "build_format_row must populate the format combo from "
        f"writable_formats(); it iterates {iterated_sources!r} instead."
    )

    literal_names = set()
    for list_node in (n for n in ast.walk(node) if isinstance(n, ast.List)):
        for element in list_node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                literal_names.add(element.value.upper())
    assert not literal_names & {"PNG", "JPEG", "WEBP", "BMP", "TIFF", "JXL"}, (
        "build_format_row re-introduces a literal format list "
        f"({sorted(literal_names)}); the combo must follow writable_formats() "
        "so a format whose encoder is missing cannot be offered."
    )


def _module_imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _called_names(tree: ast.AST) -> set[str]:
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name):
                called.add(function.id)
            elif isinstance(function, ast.Attribute):
                called.add(function.attr)
    return called


def test_single_writer_has_no_second_jxl_branch():
    """Both save paths go through one writer; no tab keeps a private branch."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    services = {
        "image_compare": root
        / "src/tabs/image_compare/services/image_export/service.py",
        "multi_compare": root / "src/tabs/multi_compare/services/image_export.py",
    }

    for name, path in services.items():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert "imagecodecs" not in _module_imports(tree), (
            f"{name} export service imports imagecodecs directly; the JXL "
            "encoder belongs to shared.image_processing.format_capability."
        )
        assert "imagecodecs" not in _called_names(tree), (
            f"{name} export service calls imagecodecs directly; that is a "
            "second JXL writer the other tab cannot reach."
        )
        assert "JXL_SUPPORTED" not in source, (
            f"{name} export service gates JXL itself; the capability probe in "
            "shared.image_processing.format_capability is the single gate."
        )
        assert "encode" in _called_names(tree), (
            f"{name} export service does not call the shared writer encode()."
        )


def test_gate_rejects_a_format_no_backend_has():
    """Falsification: the checker must fail on an encoder-less format.

    Without this a gate that silently accepted everything would stay green
    forever. ``PNG`` is the known-good control, so the failure below is
    attributable to the bogus format alone.
    """
    bogus = "DEFINITELY-NOT-AN-IMAGE-FORMAT"
    pillow = pillow_writable_formats()
    assert bogus not in pillow
    assert bogus not in NON_PILLOW_ALLOWLIST
    assert "PNG" in pillow

    failures = unsupported_formats({"PNG", bogus})
    assert set(failures) == {bogus}, failures


def test_gate_rejects_an_allowlisted_format_without_a_working_backend(monkeypatch):
    """An allowlist entry is only as good as the backend probe behind it."""
    monkeypatch.setitem(
        _BACKEND_PROBES, "imagecodecs", lambda: False
    )
    real_backend = NON_PILLOW_ALLOWLIST["JXL"][1]
    assert real_backend == "imagecodecs"
    failures = unsupported_formats({"JXL"})
    assert set(failures) == {"JXL"}, (
        "an allowlisted format must fail the gate when its backend cannot "
        f"encode here; got {failures}"
    )


def test_every_writable_format_round_trips_through_the_shared_writer(tmp_path):
    """The writer produces a file each offered format reads back.

    Deliberately through ``encode`` (the shared buyer every save path uses),
    not through a tab's service: this file is a host-level contract and root
    tests must not deep-import tab internals. The end-to-end claim for the
    multi-compare *service* lives in that tab's own suite
    (``src/tabs/multi_compare/tests/plugins/test_mc_jxl_export.py``), which also
    holds the red-before record for the JXL crash.
    """
    from shared.image_processing.export_encoding import (
        build_save_kwargs,
        flatten_alpha_if_needed,
    )

    image = Image.new("RGBA", (16, 12), (40, 80, 120, 255))
    for image_format in writable_formats():
        target = tmp_path / f"roundtrip{suffix_for_format(image_format)}"
        encode(
            flatten_alpha_if_needed(image, image_format, (255, 255, 255, 255)),
            path_or_sink=str(target),
            image_format=image_format,
            save_kwargs=build_save_kwargs(image_format),
        )
        assert target.stat().st_size > 0, f"{image_format} wrote an empty file"
        if image_format == "JXL":
            with open(target, "rb") as handle:
                assert handle.read(2) == b"\xff\x0a", "not a JXL codestream"
            continue
        with Image.open(target) as saved:
            assert saved.size == (16, 12)
            # JPEG/WEBP are lossy, so the flat colour is compared with a
            # tolerance; the claim is that the frame reached the file intact.
            red, green, blue = saved.convert("RGB").getpixel((0, 0))
            assert (abs(red - 40), abs(green - 80), abs(blue - 120)) <= (8, 8, 8)


def test_encode_rejects_an_unwritable_format_with_a_reason(tmp_path):
    """Refusal is a readable message, never ``KeyError`` from Pillow."""
    image = Image.new("RGB", (8, 8), (0, 0, 0))
    with pytest.raises(ValueError) as excinfo:
        encode(image, path_or_sink=str(tmp_path / "x.gif"), image_format="GIF")
    message = str(excinfo.value)
    assert "GIF" in message and "not" in message.lower()
    assert not (tmp_path / "x.gif").exists()


# ---------------------------------------------------------------------------
# JXL glue, proven without the optional codec.
#
# ``imagecodecs`` is not installed in every dev/CI environment (it is absent
# from the cell this was written in), so the branch that *uses* it would
# otherwise never execute here. These tests substitute a fake module to pin the
# glue: capability probing, atomic publication, the streaming write when the
# backend accepts a sink, the encode-then-copy fallback when it does not, and
# cancellation on both. They prove our side of the seam, NOT that libjxl
# encodes -- that claim needs the real codec (see the skipped end-to-end test).
# ---------------------------------------------------------------------------

_FAKE_JXL_BYTES = b"\xff\x0a" + b"fake-jxl-codestream" * 400


class _FakeImageCodecs:
    """Minimal ``imagecodecs`` stand-in with a controllable API shape.

    ``accepts_sink`` models the historical generic writer; ``dedicated`` models
    the 2026.x shape, where ``imwrite(..., codec="jxl")`` raises
    ``ValueError("no imagecodec specified")`` and only ``jpegxl_encode``
    works (measured 2026-11-15 on imagecodecs 2026.11.11).
    """

    def __init__(self, *, accepts_sink: bool, dedicated: bool = False) -> None:
        self.accepts_sink = accepts_sink
        self.dedicated = dedicated
        self.calls: list[dict] = []
        if dedicated:
            self.jpegxl_encode = self._dedicated_encode  # type: ignore[assignment]

    def _dedicated_encode(self, arr, **kwargs) -> bytes:
        self.calls.append({"kwargs": kwargs, "api": "jpegxl_encode"})
        return _FAKE_JXL_BYTES

    def imwrite(self, target, arr, **kwargs) -> None:
        self.calls.append({"kwargs": kwargs, "sink": hasattr(target, "write")})
        if self.dedicated:
            raise ValueError("no imagecodec specified")
        data = _FAKE_JXL_BYTES
        if hasattr(target, "write"):
            if not self.accepts_sink:
                raise TypeError("imwrite() expects a file name")
            target.write(data)
            return
        with open(target, "wb") as handle:
            handle.write(data)


@pytest.fixture
def fake_imagecodecs(monkeypatch):
    """Install a fake ``imagecodecs`` module and reset the capability caches."""
    import sys
    import types

    from shared.image_processing import format_capability as caps

    def install(accepts_sink: bool, *, dedicated: bool = False) -> _FakeImageCodecs:
        fake = _FakeImageCodecs(accepts_sink=accepts_sink, dedicated=dedicated)
        module = types.ModuleType("imagecodecs")
        module.imwrite = fake.imwrite  # type: ignore[attr-defined]
        if dedicated:
            module.jpegxl_encode = fake.jpegxl_encode  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "imagecodecs", module)
        caps._jxl_backend.cache_clear()
        caps._pillow_writable_formats.cache_clear()
        return fake

    try:
        yield install
    finally:
        caps._jxl_backend.cache_clear()
        caps._pillow_writable_formats.cache_clear()


def test_jxl_capability_follows_the_backend_probe(fake_imagecodecs):
    """JXL is offered exactly when the probed backend encodes it."""
    fake_imagecodecs(accepts_sink=True)
    assert "JXL" in writable_formats()
    assert writable_formats()[-1] == "JXL", "optional formats must come last"


def test_jxl_write_publishes_atomically_with_progress(fake_imagecodecs, tmp_path):
    """A path write goes to ``.tmp`` and is renamed, reporting progress."""
    install = fake_imagecodecs
    fake = install(accepts_sink=True)
    emitted: list[int] = []
    target = tmp_path / "out.jxl"

    backend = encode(
        Image.new("RGB", (8, 8), (10, 20, 30)),
        path_or_sink=str(target),
        image_format="JXL",
        save_kwargs={"quality": 95},
        progress=emitted.append,
        progress_start=25,
        progress_end=99,
    )

    assert backend == "imagecodecs"
    assert target.read_bytes() == _FAKE_JXL_BYTES
    assert not (tmp_path / "out.jxl.tmp").exists()
    assert emitted[0] == 25
    # quality 95 -> lossy distance 0.5, the same mapping the per-tab branch used
    # calls[-1] is the real write; the earlier ones are capability probes.
    assert fake.calls[-1]["kwargs"] == {"codec": "jxl", "distance": 0.5}
    assert fake.calls[-1]["sink"] is False


def test_jxl_write_is_lossless_at_quality_100(fake_imagecodecs, tmp_path):
    """Quality 100 keeps the previous lossless mapping (not a distance)."""
    fake = fake_imagecodecs(accepts_sink=True)
    encode(
        Image.new("RGB", (4, 4), (1, 2, 3)),
        path_or_sink=str(tmp_path / "lossless.jxl"),
        image_format="JXL",
        save_kwargs={"quality": 100},
    )
    assert fake.calls[-1]["kwargs"] == {"codec": "jxl", "lossless": True}


def test_jxl_write_streams_into_a_sink_when_supported(fake_imagecodecs):
    """A backend that accepts a file object writes into the given sink."""
    fake_imagecodecs(accepts_sink=True)
    sink = io.BytesIO()

    encode(
        Image.new("RGB", (8, 8), (1, 2, 3)),
        path_or_sink=sink,
        image_format="JXL",
        save_kwargs={"quality": 90},
    )

    assert sink.getvalue() == _FAKE_JXL_BYTES


def test_jxl_write_falls_back_to_chunked_copy(fake_imagecodecs):
    """A path-only backend still fills the sink, copying in chunks."""
    fake = fake_imagecodecs(accepts_sink=False)
    sink = io.BytesIO()
    emitted: list[int] = []

    encode(
        Image.new("RGB", (8, 8), (1, 2, 3)),
        path_or_sink=sink,
        image_format="JXL",
        save_kwargs={"quality": 90},
        progress=emitted.append,
    )

    assert sink.getvalue() == _FAKE_JXL_BYTES
    assert fake.calls[-1]["sink"] is False
    assert emitted and all(0 <= value <= 100 for value in emitted)


def test_jxl_write_cancel_leaves_no_file(fake_imagecodecs, tmp_path):
    """Cancelling refuses before publishing — no truncated file at the path."""
    import threading

    from shared.image_processing.pil_save import SAVE_CANCELED_MESSAGE

    cancel_event = threading.Event()
    cancel_event.set()
    target = tmp_path / "canceled.jxl"
    fake_imagecodecs(accepts_sink=True)

    with pytest.raises(RuntimeError) as excinfo:
        encode(
            Image.new("RGB", (8, 8), (1, 2, 3)),
            path_or_sink=str(target),
            image_format="JXL",
            save_kwargs={"quality": 95},
            cancel_event=cancel_event,
        )

    assert str(excinfo.value) == SAVE_CANCELED_MESSAGE
    assert not target.exists()
    assert not (tmp_path / "canceled.jxl.tmp").exists()


def test_jxl_survives_the_imagecodecs_2026_api_shape(fake_imagecodecs, tmp_path):
    """The dedicated encoder is enough; the generic writer is not required.

    imagecodecs 2026.x raises ``ValueError("no imagecodec specified")`` for
    ``imwrite(..., codec="jxl")`` while ``jpegxl_encode`` works — a build with
    only that shape must still offer JXL and write a real file, or the format
    silently disappears from every export UI (measured 2026-11-15).
    """
    fake = fake_imagecodecs(accepts_sink=False, dedicated=True)

    assert "JXL" in writable_formats()

    target = tmp_path / "out.jxl"
    encode(
        Image.new("RGB", (8, 8), (1, 2, 3)),
        path_or_sink=str(target),
        image_format="JXL",
        save_kwargs={"quality": 95},
    )

    assert target.exists() and target.stat().st_size > 0
    assert any(call.get("api") == "jpegxl_encode" for call in fake.calls), (
        "the write must go through the dedicated encoder on this shape"
    )
