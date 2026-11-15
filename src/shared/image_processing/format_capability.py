"""Which image formats this build can actually *write* — one home, one writer.

Two drifts this module closes:

* **The offer outran the encoders.** The export dialog offered ``JXL`` to both
  comparison tabs, but only one tab had a JXL branch (through ``imagecodecs``)
  and the other handed ``format="JXL"`` straight to Pillow, which has no JXL
  plugin at all — ``KeyError: 'JXL'`` on save. A format may only be offered
  when :func:`writable_formats` proves an installed encoder produces it.
* **The alpha policy had two copies.** ``export_encoding`` and ``pil_save``
  each carried their own ``{"PNG", "TIFF", "WEBP", "JXL"}`` set, so a format
  added to one stayed flattened-to-RGB in the other. :func:`formats_with_alpha`
  is the single definition now.

Callers get one writer (:func:`encode`) instead of re-implementing the
Pillow-vs-alternative-backend fork: Pillow for the formats it registers,
``imagecodecs`` for JXL. Cancellation and progress behave the same on both
paths — a cancel still aborts the write and leaves no file at the final path.

Lives in ``shared`` (not the engine) on purpose: the engine may import
``shared`` but must never name ``tabs.*`` / ``plugins.*``, and both tabs plus
the shared dialog read this module — ``shared/image_processing`` is the only
layer all of them already depend on.

Re-verified by ``tests/contracts/test_export_formats_match_encoders.py``:
it fails if this module offers a format no installed encoder writes, and if
the export dialog offers anything else.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

from core.log_names import APP_LOGGER_NAME
from shared.image_processing import pil_save
from shared.image_processing.pil_save import SAVE_CANCELED_MESSAGE, _CancelableStream

logger = logging.getLogger(APP_LOGGER_NAME)

#: Curated offer order. A format is offered only when :func:`writable_formats`
#: proves it writable, so a build without a given encoder silently drops the
#: entry from the dialog instead of failing at save time.
_OFFER_ORDER: tuple[str, ...] = ("PNG", "JPEG", "WEBP", "BMP", "TIFF")
_OPTIONAL_ORDER: tuple[str, ...] = ("JXL",)

#: Canonical suffix per format — the existing export naming (``.jpg`` for
#: JPEG, ``.tiff`` for TIFF), kept byte-identical so saved file names do not
#: change when a format moves to the shared writer.
_SUFFIX_BY_FORMAT: dict[str, str] = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tiff",
    "JXL": ".jxl",
}

#: Suffix spellings accepted when mapping a file name back to a format.
_FORMAT_BY_SUFFIX: dict[str, str] = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".bmp": "BMP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".jxl": "JXL",
}

#: Formats whose encoders keep an alpha channel (define once, here).
_ALPHA_FORMATS: frozenset[str] = frozenset({"PNG", "TIFF", "WEBP", "JXL"})

#: Chunk size used when bytes are copied into a sink (cancel/progress check
#: granularity for backends that cannot write into a stream directly).
_COPY_CHUNK_BYTES = 256 * 1024


def _canonical(image_format: str | None) -> str:
    """Upper-case ``image_format`` and fold the ``JPG`` spelling into JPEG."""
    raw = (image_format or "PNG").strip().upper()
    return "JPEG" if raw == "JPG" else raw


@lru_cache(maxsize=1)
def _pillow_writable_formats() -> frozenset[str]:
    """Format names Pillow registers an *encoder* for in the installed build.

    ``Image.SAVE`` is empty until ``Image.init()`` runs — Pillow loads plugins
    lazily — so probing before that would report "nothing is writable".
    """
    from PIL import Image

    Image.init()
    return frozenset(str(name).upper() for name in Image.SAVE)


@dataclass(frozen=True)
class _JxlBackend:
    """Proven capability of the JXL encoder (``imagecodecs``).

    ``accepts_sink`` records whether the probed write went through a file-like
    object, which decides between a truly cancelable streaming write and the
    encode-then-copy fallback.

    ``encode_to_bytes`` records that the working encoder is the dedicated
    ``jpegxl_encode`` function rather than the generic ``imwrite``. imagecodecs
    2026.x wheels reject ``imwrite(..., codec="jxl")`` outright ("no imagecodec
    specified") while ``jpegxl_encode`` keeps working (measured 2026-11-15 on
    2026.11.11, the version a fresh ``requirements-gui.txt`` install resolves
    to) — without this the format silently disappeared from every export UI.
    """

    accepts_sink: bool
    encode_to_bytes: bool = False


def _jxl_encode_bytes(imagecodecs: Any, arr: Any, codec_kwargs: dict) -> bytes:
    """Encode *arr* to JXL bytes through whichever API this build exposes.

    The dedicated ``jpegxl_encode`` is tried FIRST: it is the stable name, and
    a build without libjxl raises there exactly like it did through the generic
    writer. ``imwrite`` stays as the fallback for builds that predate the
    dedicated function.
    """
    encode = getattr(imagecodecs, "jpegxl_encode", None)
    if callable(encode):
        kwargs = {k: v for k, v in codec_kwargs.items() if k != "codec"}
        data = encode(arr, **kwargs)
        if not data:
            raise RuntimeError("imagecodecs wrote no JXL bytes")
        return bytes(data)
    import io

    buffer = io.BytesIO()
    imagecodecs.imwrite(buffer, arr, **codec_kwargs)
    data = buffer.getvalue()
    if not data:
        raise RuntimeError("imagecodecs wrote no JXL bytes")
    return data


def _probe_jxl_encode(imagecodecs: Any, arr: Any, **codec_kwargs: Any) -> bytes | None:
    """Encode the tiny probe array to a sink; return the bytes or ``None``.

    The probe is the re-verification step: an importable ``imagecodecs`` whose
    libjxl is missing raises here, and the format is then honestly reported as
    unavailable instead of being offered and crashing at save time.
    """
    try:
        return _jxl_encode_bytes(imagecodecs, arr, codec_kwargs)
    except Exception:
        return None


@lru_cache(maxsize=1)
def _jxl_backend() -> _JxlBackend | None:
    """``None`` when no installed backend writes JXL, else the proven shape."""
    try:
        import imagecodecs  # type: ignore[import-untyped]
        import numpy as np  # type: ignore[import-untyped]
    except Exception:
        logger.debug(
            "JXL export: imagecodecs/numpy not installed; format not offered."
        )
        return None

    probe = np.zeros((2, 2, 3), dtype=np.uint8)
    kwargs: dict[str, Any] = {"codec": "jxl", "lossless": True}
    if _probe_jxl_encode(imagecodecs, probe, **kwargs) is not None:
        # The dedicated encoder writes bytes, so there is no streaming sink to
        # feed; the generic writer (older builds) still accepts one.
        return _JxlBackend(
            accepts_sink=not callable(getattr(imagecodecs, "jpegxl_encode", None)),
            encode_to_bytes=callable(getattr(imagecodecs, "jpegxl_encode", None)),
        )
    # Sink unsupported: retry through a scratch file to separate "libjxl works,
    # imwrite wants a path" from "no JXL encoder at all".
    directory = tempfile.mkdtemp(prefix="imgsli-jxl-probe-")
    scratch = os.path.join(directory, "probe.jxl")
    try:
        imagecodecs.imwrite(scratch, probe, **kwargs)
        if os.path.getsize(scratch) > 0:
            return _JxlBackend(accepts_sink=False)
    except Exception:
        logger.debug(
            "JXL export: imagecodecs present but its libjxl cannot encode; "
            "format not offered.",
            exc_info=True,
        )
        return None
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass
        try:
            os.rmdir(directory)
        except OSError:
            pass
    return None


def jxl_encoder_available() -> bool:
    """True when this build can really write JXL (probed, not merely importable).

    Public so a capability gate asks the SAME question the export UI asks: the
    contract test that re-checks ``.jxl`` in ``ACCEPTED_IMAGE_EXTENSIONS`` used
    its own ``imwrite(codec="jxl")`` probe, which imagecodecs 2026.x rejects —
    the gate then reported "no decoder" on a machine whose ``jpegxl_encode``
    works, and the Fast tier went red for a false negative.
    """
    return _jxl_backend() is not None


def _alternative_backends() -> dict[str, bool]:
    """Formats no Pillow plugin covers, mapped to a proven alternative encoder."""
    return {"JXL": _jxl_backend() is not None}


def writable_formats() -> tuple[str, ...]:
    """Formats this stack can really write, in stable offer order.

    Pillow-registered encoders cover the base family; :data:`_OPTIONAL_ORDER`
    formats are added only when their alternative backend re-verifies on this
    machine (see :func:`_jxl_backend`). Empty intersections are dropped, so a
    caller may render the result directly: a format absent here must not be
    offered in any export UI.
    """
    pillow = _pillow_writable_formats()
    alternatives = _alternative_backends()
    ordered = [*_OFFER_ORDER, *_OPTIONAL_ORDER]
    return tuple(
        fmt for fmt in ordered if fmt in pillow or alternatives.get(fmt, False)
    )


def suffix_for_format(fmt: str) -> str:
    """Canonical file suffix for *fmt* (``.jpg`` for JPEG, ``.tiff`` for TIFF)."""
    return _SUFFIX_BY_FORMAT.get(_canonical(fmt), f".{_canonical(fmt).lower()}")


def format_from_suffix(suffix: str) -> str | None:
    """Format name for a file suffix, or ``None`` when this build cannot write it.

    Only writable formats are returned: a ``.gif`` name must not be turned into
    a format the writer would then reject.
    """
    raw = suffix.strip().lower()
    if raw and not raw.startswith("."):
        raw = f".{raw}"
    fmt = _FORMAT_BY_SUFFIX.get(raw)
    if fmt is None or fmt not in writable_formats():
        return None
    return fmt


def formats_with_alpha() -> frozenset[str]:
    """Formats whose encoders keep an alpha channel — the single definition.

    Deliberately independent of :func:`writable_formats`: callers decide
    whether to flatten an RGBA image *before* they know whether the write will
    succeed, and a format must not change its alpha policy just because its
    encoder is missing (the error message should be about the encoder).
    """
    return _ALPHA_FORMATS


def describe_unavailable(fmt: str) -> str:
    """Human-readable reason why *fmt* cannot be written on this stack.

    Used for UI/toast text when a write is refused, so the user is told which
    backend is missing instead of seeing ``KeyError: 'JXL'``.
    """
    canonical = _canonical(fmt)
    if canonical == "JXL":
        return (
            "JXL export needs the imagecodecs backend (libjxl), which is not "
            "available in this build. Choose PNG, JPEG, WEBP, BMP or TIFF."
        )
    if canonical in _SUFFIX_BY_FORMAT or canonical in _OFFER_ORDER:
        return (
            f"{canonical} export is not available: this build's Pillow has no "
            "working encoder for it."
        )
    return f"{canonical} is not an image format this build can write."


def _jxl_codec_kwargs(save_kwargs: dict) -> dict:
    """Translate the export ``quality`` knob into libjxl's lossless/distance.

    Same mapping the pre-shared image-compare branch used (quality >= 100 is
    lossless, otherwise ``distance`` from 0.1 to 9.9), so switching the writer
    does not change the bytes a given quality setting produces.
    """
    quality = int(save_kwargs.get("quality", 95))
    if quality >= 100:
        return {"lossless": True}
    return {"distance": max(0.1, (100 - quality) / 10.0)}


def _encode_jxl(
    image: Any,
    target: Any,
    save_kwargs: dict,
    cancel_event: Optional[threading.Event],
    progress_callback: Optional[Callable[[int], None]],
    progress_start: int,
    progress_end: int,
) -> None:
    """Write *image* as JXL through ``imagecodecs`` into *target*.

    *target* is either a filesystem path (published atomically via tmp+replace,
    like every Pillow write) or a writable sink. A sink is used directly when
    the backend accepts file-like objects — that keeps the write cancelable
    mid-encode through ``_CancelableStream``. When it does not, the frame is
    encoded to a scratch file first and copied into the sink in chunks with a
    cancel check per chunk (mid-encode cancellation is then only honored once
    the encoder returns — the same limitation the per-tab JXL branch had).
    """
    import imagecodecs  # type: ignore[import-untyped]
    import numpy as np  # type: ignore[import-untyped]

    arr = np.array(image)
    codec_kwargs = {"codec": "jxl", **_jxl_codec_kwargs(save_kwargs)}

    if progress_callback is not None:
        progress_callback(progress_start)

    backend = _jxl_backend()
    encode_to_bytes = bool(backend is not None and backend.encode_to_bytes)

    if isinstance(target, (str, os.PathLike)):
        full_path = os.fspath(target)
        tmp_path = f"{full_path}.tmp"
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        try:
            if encode_to_bytes:
                data = _jxl_encode_bytes(imagecodecs, arr, codec_kwargs)
                with open(tmp_path, "wb") as handle:
                    handle.write(data)
            else:
                imagecodecs.imwrite(tmp_path, arr, **codec_kwargs)
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError(SAVE_CANCELED_MESSAGE)
            os.replace(tmp_path, full_path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise
        return

    if encode_to_bytes:
        data = _jxl_encode_bytes(imagecodecs, arr, codec_kwargs)
        expected = max(1, len(data))
        written = 0
        offset = 0
        while offset < len(data):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError(SAVE_CANCELED_MESSAGE)
            chunk = data[offset : offset + _COPY_CHUNK_BYTES]
            offset += len(chunk)
            target.write(chunk)
            written += len(chunk)
            if progress_callback is not None:
                progress_callback(
                    pil_save.progress_from_bytes_written(
                        written,
                        expected,
                        progress_start=progress_start,
                        progress_end=progress_end,
                    )
                )
        return

    if backend is not None and backend.accepts_sink:
        stream = _CancelableStream(
            target,
            cancel_event,
            progress_callback=progress_callback,
            progress_start=progress_start,
            progress_end=progress_end,
            expected_bytes=pil_save.estimate_encoded_size(
                image, "JXL", save_kwargs
            ),
        )
        imagecodecs.imwrite(stream, arr, **codec_kwargs)
        return

    with tempfile.TemporaryDirectory(prefix="imgsli-jxl-") as directory:
        scratch = os.path.join(directory, "frame.jxl")
        imagecodecs.imwrite(scratch, arr, **codec_kwargs)
        expected = max(1, os.path.getsize(scratch))
        written = 0
        with open(scratch, "rb") as source:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError(SAVE_CANCELED_MESSAGE)
                chunk = source.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                target.write(chunk)
                written += len(chunk)
                if progress_callback is not None:
                    progress_callback(
                        pil_save.progress_from_bytes_written(
                            written,
                            expected,
                            progress_start=progress_start,
                            progress_end=progress_end,
                        )
                    )


def encode(
    image: Any,
    *,
    path_or_sink: Any,
    image_format: str,
    save_kwargs: dict | None = None,
    cancel_event: Optional[threading.Event] = None,
    progress: Optional[Callable[[int], None]] = None,
    progress_start: int = 50,
    progress_end: int = 99,
) -> str:
    """Write *image* as *image_format* — the single writer for every export path.

    Pillow handles the formats it registers; JXL goes through ``imagecodecs``.
    A path target is published atomically (``.tmp`` + ``os.replace``), so a
    crash or cancel never leaves a truncated file at the final path and never
    destroys a pre-existing one. Cancellation and progress semantics match
    ``pil_save.write_pil_image_cancelable`` for both backends.

    *progress_start* / *progress_end* are the slice of the caller's 0..100 bar
    the encode occupies (each tab reserves a different tail: the composite save
    starts at 10, the single-view save at 25) — the callback must not be
    re-based here or the two paths would show different bars.

    Returns the id of the backend that wrote the bytes (``"pillow"`` or
    ``"imagecodecs"``); raises ``ValueError`` with
    :func:`describe_unavailable` when the format cannot be written here.
    """
    kwargs = dict(save_kwargs or {})
    fmt = _canonical(image_format)
    if fmt not in writable_formats():
        raise ValueError(describe_unavailable(fmt))

    backend = "pillow"
    if fmt not in _pillow_writable_formats():
        backend = "imagecodecs"

    if backend == "imagecodecs":
        _encode_jxl(
            image,
            path_or_sink,
            kwargs,
            cancel_event,
            progress,
            progress_start,
            progress_end,
        )
        return backend

    if isinstance(path_or_sink, (str, os.PathLike)):
        pil_save.write_pil_image_cancelable(
            image,
            os.fspath(path_or_sink),
            fmt,
            kwargs,
            cancel_event=cancel_event,
            progress_callback=progress,
            progress_start=progress_start,
            progress_end=progress_end,
        )
        return backend

    pil_save.write_pil_image_to_sink(
        image,
        path_or_sink,
        fmt,
        kwargs,
        cancel_event=cancel_event,
        progress_callback=progress,
        progress_start=progress_start,
        progress_end=progress_end,
    )
    return backend
