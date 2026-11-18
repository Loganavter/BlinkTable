"""Plain (non-array) tile textures must not be sampled with mip filtering.

Owner report 2026-11-18: "why does Multi Compare paint Malevich squares for
these images?" — every slot whose tile grid is 1x1 (any image at or below
``SLOT_LIVE_TILE_EXTENT``: a 512x512 pair, a 64x64 pair) painted an opaque
black rectangle with the filename label on top, while a 2048x2048 slot in the
same canvas painted fine.

Measured on a leased instance, one frame, one slot:

* the source QImage handed to the upload was ``512x512 Format_RGBA8888`` with
  the correct pixel (``(200, 30, 30, 255)``);
* the plain draw ran (``draws=1``) with a non-null SRB and a ``512x512``
  texture bound;
* forcing the array path for the same slot made it paint correctly.

That isolates the plain path, and the difference is the sampler: plain tiles
are uploaded as single-level textures, but the plain SRB bound
``renderer.sampler`` whose mipmap filter is ``Linear``. On a texture without a
mip chain that is mipmap-INCOMPLETE and samples black, so every single-tile
slot was blank by construction.

The fix binds ``renderer.plain_sampler`` (mipmap filter ``None``) for plain
tiles; these tests pin the binding, which is what a regression would break.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tabs.multi_compare.scene.passes import BaseImagesPass
from tabs.multi_compare.scene.passes import slot_resources as slot_resources_module


class _FakeStageFlag:
    VertexStage = 1
    FragmentStage = 2


class _FakeBinding:
    StageFlag = _FakeStageFlag

    @staticmethod
    def uniformBufferWithDynamicOffset(*args):
        return ("uniform_dynamic", args)

    @staticmethod
    def sampledTexture(*args):
        return ("texture", args)


class _FakeSrb:
    def __init__(self):
        self.bindings = []

    def setBindings(self, bindings):
        self.bindings = bindings

    def create(self):
        return True

    def destroy(self):
        return None


class _FakeRhi:
    def newShaderResourceBindings(self):
        return _FakeSrb()


@pytest.fixture
def bindings(monkeypatch):
    monkeypatch.setattr(
        slot_resources_module, "QRhiShaderResourceBinding", _FakeBinding
    )
    render_pass = BaseImagesPass()
    render_pass.slot_resources.slot_uniform_buffers.append(object())
    return render_pass.slot_resources


def test_plain_tile_srb_binds_the_non_mipmapped_sampler(bindings):
    """Pre-fix this bound ``renderer.sampler`` (mipmap Linear) → black tiles."""
    mip_sampler = object()
    plain_sampler = object()
    renderer = SimpleNamespace(
        rhi=_FakeRhi(), sampler=mip_sampler, plain_sampler=plain_sampler
    )

    srb = bindings.ensure_tile_srb(renderer, 0, 7, object())

    sampled = [b for b in srb.bindings if b[0] == "texture"]
    assert sampled, "the tile SRB must bind a texture"
    assert sampled[0][1][3] is plain_sampler, (
        "a single-level plain texture sampled through a mipmapped filter is "
        "mipmap-incomplete and reads as black; bind renderer.plain_sampler"
    )


def test_plain_tile_srb_falls_back_to_the_default_sampler(bindings):
    """Headless fakes without ``plain_sampler`` keep working unchanged."""
    default = object()
    renderer = SimpleNamespace(rhi=_FakeRhi(), sampler=default)

    srb = bindings.ensure_tile_srb(renderer, 0, 3, object())

    sampled = [b for b in srb.bindings if b[0] == "texture"]
    assert sampled and sampled[0][1][3] is default
