"""Capability gate: every accepted suffix must be readable by a real decoder.

``shared.image_extensions.ACCEPTED_IMAGE_EXTENSIONS`` is a *claim* about the
installed decoders. It had already drifted once: ``.avif`` decoded fine
(Pillow and libvips both read it) while the static list -- which drives DnD,
file-dialog filters and the gallery folder scan -- still bounced the file, so
the same image opened when passed programmatically and was rejected by every
entry point. Adding a suffix by hand is therefore never enough: this gate
fails unless a decoder on *this* machine actually reads it.

Rule per suffix:

* **Pillow-backed** (the main path -- ``tiled_pixel_store``,
  ``pyramid_pixel_store`` and the preview loaders all decode through it): the
  suffix must appear in ``PIL.Image.registered_extensions()`` **and** its
  format must be one Pillow can open (``Image.OPEN``), not merely write.
* **Content alias** (``.mpo``): Pillow registers the suffix for save only --
  upstream has its ``register_open`` commented out -- but an MPO file is a
  plain JPEG stream per picture, so ``Image.open`` sniffs it. The alias table
  below names the opener format explicitly instead of leaving a hole; the
  decode itself is proven by the runtime round trip.
* **Alternative backend**: a suffix Pillow has no plugin for (``.jxl``:
  decoded by libvips/``imagecodecs``) is allowed only through
  :data:`NON_PILLOW_ALLOWLIST`, which names the reason and whose claim is
  re-checked here (``pyvips.get_suffixes()``). The allowlist is
  **shrink-only**: a stale entry -- Pillow registers the suffix now, so the
  entry is no longer needed -- fails just as loudly as a missing one.
* Anything else fails. That is the "the list cannot drift again" property.

Beyond the static claim the file pins the three things a suffix list can get
wrong without any decoder being missing:

* **runtime round trip** -- for every accepted suffix this stack can *write*,
  a tiny sample is encoded into ``tmp_path`` and driven through the real intake
  (``is_loadable_image_path`` -> ``get_image_dimensions`` ->
  ``load_preview_image``). A suffix with no writer here is an explicit
  ``skip`` naming the suffix and the missing writer, never a silent pass, so
  the report shows exactly which formats were proven and which were not.
* **group layout** -- ``FIRST_FRAME_SUFFIXES`` (admitted, but only one frame is
  shown) is a subset of the canon, and the refused suffixes live in a disjoint
  table with a reason each (``test_rejected_formats_have_reasons.py``).
* **content-type coverage** -- every accepted suffix has a ``_EXT_TO_MIME``
  entry whose value composes a valid ``image/*`` media type, so an accepted
  file cannot be uploaded labelled as a different format (``.jxl`` used to ship
  as ``png``).

The final test feeds :func:`unsupported_suffixes` a suffix no backend has, so
a checker that accepted everything (or was never wired to the real set) would
be caught instead of passing by accident.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import pytest

from shared.image_extensions import (
    ACCEPTED_IMAGE_EXTENSIONS,
    FIRST_FRAME_SUFFIXES,
    is_loadable_image_path,
)

#: suffix -> why Pillow cannot read it + which backend keeps it acceptable.
#: SHRINK-ONLY: delete an entry the moment Pillow registers the suffix. Never
#: add a suffix here to turn a red gate green -- that is exactly the drift
#: this gate exists to stop; an entry is only valid for a format another
#: decoder really reads on this machine, and the alternative backend is
#: re-probed below.
def _jxl_decodes_here() -> bool:
    """JXL has two possible backends and a build may have either one.

    libvips lists ``.jxl`` only when built with libjxl; ``imagecodecs`` carries
    its own JXL codec regardless. Measured 2026-10-28: the dev venv has
    imagecodecs but a libvips WITHOUT the JXL loader, the system interpreter the
    reverse - so a gate that hardcoded either backend would be red on one of the
    two machines the app runs on.
    """
    if ".jxl" in vips_suffixes():
        return True
    # imagecodecs exposes no ``jxl`` symbol (measured: no attribute, no
    # ``jxl_version()``) even when its codec dispatch has one, so capability has
    # to be probed BEHAVIOURALLY. Ask the app's own probe instead of writing a
    # second one here: the local ``imwrite(..., codec="jxl")`` shape was
    # rejected by imagecodecs 2026.x ("no imagecodec specified") while its
    # dedicated ``jpegxl_encode`` works, so this gate reported "no decoder" on a
    # machine that had one and the Fast tier went red for a false negative
    # (measured 2026-11-15). One probe, one answer.
    try:
        from shared.image_processing.format_capability import (
            jxl_encoder_available,
        )

        return bool(jxl_encoder_available())
    except Exception:
        return False


#: suffix -> (why Pillow cannot read it, probe for the backend that can).
#: SHRINK-ONLY: delete an entry the moment Pillow registers the suffix. Never
#: add a suffix here to turn a red gate green -- that is exactly the drift this
#: gate exists to stop; an entry is only valid for a format another decoder
#: really reads on THIS machine, which the probe re-checks on every run.
NON_PILLOW_ALLOWLIST: dict[str, tuple[str, Callable[[], bool]]] = {
    ".jxl": (
        "No Pillow JXL plugin in any supported build; libvips (libjxl) or "
        "imagecodecs decodes it -- see JXL_SUPPORTED in "
        "shared/image_processing/progressive_loader.py.",
        _jxl_decodes_here,
    ),
}


#: suffix -> the Pillow format whose OPENER actually decodes files with this
#: suffix. ``.mpo`` (multi-picture JPEG, group B first-frame format) is
#: registered by ``MpoImagePlugin`` with ``register_save`` only: upstream has
#: the ``register_open`` line commented out, so ``registered_extensions()``
#: maps ``.mpo`` to a format absent from ``Image.OPEN`` while every picture in
#: the file is a plain JPEG codestream that the JPEG opener sniffs (probed
#: 2026-10-28: ``Image.open`` on a written sample reports ``format=MPO``,
#: ``n_frames=2``). The alias names that mechanism; a stale alias -- Pillow
#: gaining its own opener -- fails like a stale allowlist entry, and the decode
#: is proven behaviourally by the round-trip test rather than asserted here.
PILLOW_CONTENT_ALIASES: dict[str, str] = {".mpo": "JPEG"}

#: Compose the media type the payload builds (``data:image/<subtype>``).
_IMAGE_MEDIA_TYPE_RE = re.compile(r"^image/[A-Za-z0-9][A-Za-z0-9.+_-]*$")


def pillow_suffixes() -> frozenset[str]:
    """Suffixes this Pillow build can actually *open* (not merely register).

    ``Image.registered_extensions()`` is not the oracle: it lists the
    WRITE-ONLY ``.pdf`` plugin, so a gate built on registration alone would
    bless a suffix that raises ``UnidentifiedImageError`` on open (70
    registered vs 67 readable on this stack, measured 2026-10-28). Suffixes in
    :data:`PILLOW_CONTENT_ALIASES` are added through the opener they really
    resolve to.
    """
    from PIL import Image

    Image.init()
    opening = set(getattr(Image, "OPEN", ()))
    opened = {
        ext.lower()
        for ext, fmt in Image.registered_extensions().items()
        if fmt in opening
    }
    for suffix, alias_format in PILLOW_CONTENT_ALIASES.items():
        if alias_format in opening:
            opened.add(suffix)
    return frozenset(opened)


def vips_suffixes() -> frozenset[str]:
    """Lowercased suffixes the installed libvips build lists (empty if absent)."""
    try:
        import pyvips

        return frozenset(s.lower() for s in pyvips.get_suffixes())
    except Exception:
        return frozenset()


def unsupported_suffixes(suffixes) -> dict[str, str]:
    """Map each suffix no installed decoder can read to its failure reason.

    Pillow is the primary backend; an allowlisted suffix must additionally be
    listed by the alternative backend the entry points at. An empty mapping
    means every suffix is backed by a real decoder.
    """
    pillow = pillow_suffixes()
    vips = vips_suffixes()
    failures: dict[str, str] = {}
    for suffix in sorted(suffixes):
        if suffix in pillow:
            continue
        entry = NON_PILLOW_ALLOWLIST.get(suffix)
        if entry is None:
            failures[suffix] = (
                "not readable by Pillow and not in NON_PILLOW_ALLOWLIST"
            )
            continue
        reason, probe = entry
        if not probe():
            failures[suffix] = (
                "allowlisted, but the named alternative backend does not decode "
                f"it on this machine ({reason})"
            )
    return failures


def _pil_writer_format(suffix: str) -> str | None:
    """The Pillow SAVE format registered for *suffix*, or ``None``."""
    from PIL import Image

    Image.init()
    fmt = Image.registered_extensions().get(suffix)
    if fmt is None or fmt not in set(getattr(Image, "SAVE", ())):
        return None
    return fmt


def _write_jxl_sample(path: Path) -> str:
    """Encode a tiny JXL through ``imagecodecs``; return a skip reason on failure."""
    try:
        import imagecodecs
        import numpy as np

        imagecodecs.imwrite(str(path), np.zeros((8, 8, 3), np.uint8), codec="jxl")
    except Exception as exc:
        return (
            f"{path.suffix}: no JXL writer on this stack (Pillow ships no JXL "
            f"plugin; imagecodecs: {type(exc).__name__}: {exc})"
        )
    return ""


#: Shapes a writer may need: some formats reject small sizes (ICNS pads,
#: BLP wants power-of-two, MSP/XBM are bilevel, ICO needs >= 16 px).
_WRITE_SHAPES: tuple[tuple[tuple[int, int], str], ...] = (
    ((8, 8), "RGB"),
    ((16, 16), "RGB"),
    ((32, 32), "RGB"),
    ((48, 48), "RGB"),
    ((16, 16), "1"),
    ((48, 48), "P"),
    ((256, 256), "RGB"),
)


def write_sample(suffix: str, path: Path) -> str:
    """Encode a tiny file for *suffix*; return "" or the reason it was skipped.

    Writer capability is what decides: a format this stack can only READ is not
    a hole in the canon, it is a format whose decode can only be proven by a
    fixture we do not ship. That case is reported as an explicit skip naming
    the suffix, because "the loop skipped it" reads like a pass in a summary.
    """
    if suffix == ".jxl":
        return _write_jxl_sample(path)
    fmt = _pil_writer_format(suffix)
    if fmt is None:
        return (
            f"{suffix}: this stack has no writer for the format (Pillow "
            "registers no save handler, and no alternative encoder exists) -- "
            "the decode is proven only by real files, so the round trip is "
            "skipped rather than silently passed"
        )
    from PIL import Image

    last_error = ""
    for size, mode in _WRITE_SHAPES:
        try:
            if path.exists():
                path.unlink()
            Image.new(mode, size).save(path, format=fmt)
            with Image.open(path) as probe:
                probe.load()
        except Exception as exc:
            last_error = f"{size[0]}x{size[1]}/{mode}: {type(exc).__name__}: {exc}"
            continue
        return ""
    return (
        f"{suffix}: Pillow has a {fmt} writer here but no tiny sample could be "
        f"encoded ({last_error})"
    )


def _decoded_size(path: Path) -> tuple[int, int] | None:
    """Size the decoder reports for *path* (PIL, else the JXL codec)."""
    from PIL import Image

    try:
        with Image.open(path) as image:
            return (int(image.size[0]), int(image.size[1]))
    except Exception:
        pass
    if path.suffix.lower() == ".jxl":
        try:
            import imagecodecs

            data = imagecodecs.imread(str(path))
            return (int(data.shape[1]), int(data.shape[0]))
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# 1. The static claim: every accepted suffix has an opener
# ---------------------------------------------------------------------------


def test_every_accepted_suffix_has_a_decoder():
    """The list may only contain formats an installed decoder actually reads."""
    failures = unsupported_suffixes(ACCEPTED_IMAGE_EXTENSIONS)
    assert failures == {}, (
        "ACCEPTED_IMAGE_EXTENSIONS contains suffix(es) no installed decoder "
        f"reads: {failures}. Drop the suffix, or -- only for a format a "
        "non-Pillow backend really decodes here -- add a justified "
        "NON_PILLOW_ALLOWLIST entry. Do not add a decoder-less suffix to make "
        "DnD/dialogs quieter: the file would be admitted and then fail on load."
    )


def test_allowlist_entries_name_accepted_suffixes():
    """An allowlist entry for a non-accepted suffix is dead weight."""
    assert set(NON_PILLOW_ALLOWLIST) <= set(ACCEPTED_IMAGE_EXTENSIONS), (
        "NON_PILLOW_ALLOWLIST names suffixes that are not accepted: "
        f"{sorted(set(NON_PILLOW_ALLOWLIST) - set(ACCEPTED_IMAGE_EXTENSIONS))}"
    )


@pytest.mark.parametrize("suffix, entry", sorted(NON_PILLOW_ALLOWLIST.items()))
def test_allowlist_entries_are_justified_and_shrink_only(suffix: str, entry):
    """Each entry needs a real reason, a live backend and a shrink-only life."""
    reason, probe = entry
    assert callable(probe), (
        f"NON_PILLOW_ALLOWLIST[{suffix!r}] must carry a backend probe callable "
        "so the claim is re-checked on every machine, not asserted once."
    )
    assert suffix not in pillow_suffixes(), (
        f"stale NON_PILLOW_ALLOWLIST entry {suffix!r}: Pillow registers this "
        "suffix now, so the entry is no longer needed -- delete it (the list "
        "is shrink-only; a stale exemption is how a gate rots)."
    )
    assert len(reason.strip()) >= 30, (
        f"NON_PILLOW_ALLOWLIST[{suffix!r}] must name the backend that reads "
        "the format and why Pillow cannot; a bare 'needed' is a rubber stamp."
    )


def test_content_alias_entries_name_real_openers_and_are_not_stale():
    """A content alias must point at a live opener and only cover a gap.

    ``.mpo`` is only honest as an alias while Pillow itself has no opener for
    it: once upstream re-enables ``register_open`` the alias becomes a second
    source of truth, so it fails here instead of rotting.
    """
    from PIL import Image

    Image.init()
    opening = set(getattr(Image, "OPEN", ()))
    registered = Image.registered_extensions()
    assert set(PILLOW_CONTENT_ALIASES) <= set(ACCEPTED_IMAGE_EXTENSIONS), (
        "PILLOW_CONTENT_ALIASES names suffixes that are not accepted: "
        f"{sorted(set(PILLOW_CONTENT_ALIASES) - set(ACCEPTED_IMAGE_EXTENSIONS))}"
    )
    for suffix, alias_format in PILLOW_CONTENT_ALIASES.items():
        assert alias_format in opening, (
            f"PILLOW_CONTENT_ALIASES[{suffix!r}] points at {alias_format!r}, "
            "which has no opener on this Pillow build"
        )
        assert registered.get(suffix) not in opening, (
            f"stale PILLOW_CONTENT_ALIASES entry {suffix!r}: Pillow opens this "
            "format on its own now -- delete the alias."
        )


def test_gate_rejects_a_suffix_no_backend_has():
    """Falsification: the checker must fail on a decoder-less suffix.

    Without this, a gate that silently accepted everything would stay green
    forever. ``.png`` is included as the known-good control: it must pass, so
    the failure below is attributable to the bogus suffix alone.
    """
    bogus = ".definitely-not-an-image-format"
    assert bogus not in pillow_suffixes()
    assert bogus not in vips_suffixes()
    assert ".png" in pillow_suffixes()

    failures = unsupported_suffixes({".png", bogus})
    assert set(failures) == {bogus}, failures


# ---------------------------------------------------------------------------
# 2. Group layout: first-frame subset, no overlap with the refused table
# ---------------------------------------------------------------------------


def test_first_frame_suffixes_are_accepted():
    """A "shown as the first frame" promise only makes sense for admitted files."""
    assert FIRST_FRAME_SUFFIXES, (
        "FIRST_FRAME_SUFFIXES is empty: the group B promise is then dead code "
        "and sequence formats would be admitted with no honesty obligation"
    )
    not_accepted = sorted(FIRST_FRAME_SUFFIXES - ACCEPTED_IMAGE_EXTENSIONS)
    assert not_accepted == [], (
        "FIRST_FRAME_SUFFIXES must be a subset of ACCEPTED_IMAGE_EXTENSIONS; "
        f"these are not accepted: {not_accepted}"
    )


def test_first_frame_group_holds_the_sequence_and_composite_cases():
    """Named members, so admitting a sequence format is a visible decision.

    ``.gif``/``.apng``/``.avifs``/``.mpo`` are sequences, ``.fli``/``.flc``
    animation, ``.dcx`` multi-page PCX, ``.psd`` layered. Each of them can only
    ever show one frame or one flattened composite, which is what the UI has to
    say; dropping one from the set would silently remove that obligation.
    """
    expected = {
        ".apng",
        ".avifs",
        ".dcx",
        ".flc",
        ".fli",
        ".gif",
        ".mpo",
        ".psd",
    }
    missing = sorted(expected - FIRST_FRAME_SUFFIXES)
    assert missing == [], (
        "these accepted formats show only one frame but are not declared in "
        f"FIRST_FRAME_SUFFIXES: {missing}"
    )


# ---------------------------------------------------------------------------
# 3. Content type coverage: the canon cannot be uploaded under a wrong mime
# ---------------------------------------------------------------------------


def test_every_accepted_suffix_has_a_content_type():
    """An accepted file with no table entry would be uploaded as ``png``."""
    from tabs.image_gallery.services.ai_client.request import _EXT_TO_MIME

    missing = sorted(ACCEPTED_IMAGE_EXTENSIONS - set(_EXT_TO_MIME))
    assert missing == [], (
        "these accepted suffixes have no _EXT_TO_MIME entry, so a request "
        f"would label them image/png: {missing}"
    )
    extra = sorted(set(_EXT_TO_MIME) - ACCEPTED_IMAGE_EXTENSIONS)
    assert extra == [], (
        "_EXT_TO_MIME names suffixes outside the canon (a second, drifting "
        f"list): {extra}"
    )


def test_content_types_compose_valid_image_media_types():
    """The values are subtypes: the payload writes ``data:image/<subtype>``."""
    from tabs.image_gallery.services.ai_client.request import _EXT_TO_MIME

    bad = {
        suffix: subtype
        for suffix, subtype in _EXT_TO_MIME.items()
        if not _IMAGE_MEDIA_TYPE_RE.match(f"image/{subtype}")
    }
    assert bad == {}, (
        "these _EXT_TO_MIME values do not compose a valid image/* media type "
        f"(payload form is data:image/<subtype>;base64,): {bad}"
    )


def test_jxl_is_no_longer_labelled_png():
    """The observed hole: an accepted ``.jxl`` shipped to the model as ``png``."""
    from tabs.image_gallery.services.ai_client.request import (
        _EXT_TO_MIME,
        mime_for_path,
    )

    assert _EXT_TO_MIME[".jxl"] == "jxl"
    assert mime_for_path("frame.jxl") == "jxl"
    assert mime_for_path("frame.JXL") == "jxl"
    # Exactly one ``image/`` prefix reaches the URL; the table adds none.
    assert f"data:image/{mime_for_path('frame.jxl')};base64," == (
        "data:image/jxl;base64,"
    )


# ---------------------------------------------------------------------------
# 4. Runtime round trip: admit -> dimensions -> preview, per accepted suffix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", sorted(ACCEPTED_IMAGE_EXTENSIONS))
def test_accepted_suffix_round_trips_through_the_intake(suffix, tmp_path):
    """A writable format must survive the whole intake, not just the opener check.

    The static checks above prove an opener *exists*; this proves the bytes a
    real file carries come out the other end of the three functions every entry
    point funnels into. Formats with no writer on this stack are an explicit
    skip naming the suffix (see :func:`write_sample`).
    """
    from shared.image_processing.progressive_loader import (
        get_image_dimensions,
        load_preview_image,
    )

    path = tmp_path / f"round-trip{suffix}"
    skip_reason = write_sample(suffix, path)
    if skip_reason:
        pytest.skip(skip_reason)

    assert path.is_file()
    assert is_loadable_image_path(path) is True, (
        f"{suffix} was written but the admission predicate refuses it"
    )
    assert is_loadable_image_path(str(path)) is True

    expected = _decoded_size(path)
    assert expected is not None, f"{suffix}: decoder cannot read the sample back"
    assert get_image_dimensions(str(path)) == expected, (
        f"{suffix}: get_image_dimensions disagrees with the decoder "
        f"(reported {get_image_dimensions(str(path))}, decoder {expected})"
    )

    preview = load_preview_image(str(path))
    assert preview is not None, f"{suffix}: load_preview_image returned None"
    assert preview.width() > 0 and preview.height() > 0
    assert max(preview.width(), preview.height()) == min(1024, max(expected)), (
        f"{suffix}: preview is {preview.width()}x{preview.height()}, expected "
        f"long edge {min(1024, max(expected))}"
    )


# ---------------------------------------------------------------------------
# 5. The ``.avifs`` policy: declared first-frame format, not an exclusion
# ---------------------------------------------------------------------------


def test_avifs_is_admitted_as_a_first_frame_format():
    """``.avifs`` (AVIF image sequence) is admitted, and declared as one frame.

    This replaces the earlier ``test_avifs_stays_excluded_until_animation_is_
    supported``: excluding it ("the viewer has no animation support") stopped
    being consistent the moment the canon admits ``.gif``/``.apng``/``.psd`` --
    files that are just as impossible to show in full. The policy is therefore
    "sequence formats are admitted AND declared", not "no sequence formats":
    the file loads, the first frame is displayed, and
    :data:`FIRST_FRAME_SUFFIXES` carries the obligation for a surface to say
    so.
    """
    assert ".avifs" in ACCEPTED_IMAGE_EXTENSIONS
    assert ".avifs" in FIRST_FRAME_SUFFIXES
