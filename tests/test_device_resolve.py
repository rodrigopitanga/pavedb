# (C) 2026 Rodrigo Rodrigues da Silva <rodrigo@flowlexi.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest

from pave.embedders._device import resolve_device


class _FakeTorch:
    """Minimal torch stand-in exposing cuda/backends.mps availability."""

    def __init__(self, *, cuda: bool = False, mps: bool = False):
        self.cuda = type("_C", (), {"is_available": staticmethod(lambda: cuda)})()
        _mps = type("_M", (), {"is_available": staticmethod(lambda: mps)})()
        self.backends = type("_B", (), {"mps": _mps})()


def test_auto_prefers_cuda_then_mps_then_cpu():
    assert resolve_device("auto", _FakeTorch(cuda=True)) == "cuda"
    assert resolve_device("auto", _FakeTorch(mps=True)) == "mps"
    assert resolve_device("auto", _FakeTorch()) == "cpu"
    assert resolve_device(None, _FakeTorch()) == "cpu"  # None -> auto


def test_gpu_alias_normalizes_to_cuda():
    assert resolve_device("gpu", _FakeTorch(cuda=True)) == "cuda"
    with pytest.raises(RuntimeError, match="CUDA is unavailable"):
        resolve_device("GPU", _FakeTorch(cuda=False))


def test_explicit_cuda_requires_availability():
    assert resolve_device("cuda", _FakeTorch(cuda=True)) == "cuda"
    with pytest.raises(RuntimeError, match="CUDA is unavailable"):
        resolve_device("cuda", _FakeTorch(cuda=False))


def test_explicit_mps_requires_availability():
    assert resolve_device("mps", _FakeTorch(mps=True)) == "mps"
    with pytest.raises(RuntimeError, match="MPS is unavailable"):
        resolve_device("mps", _FakeTorch(mps=False))


def test_indexed_cuda_preserved():
    assert resolve_device("cuda:1", _FakeTorch(cuda=True)) == "cuda:1"
    assert resolve_device("gpu:0", _FakeTorch(cuda=True)) == "cuda:0"
    with pytest.raises(RuntimeError, match="CUDA is unavailable"):
        resolve_device("cuda:0", _FakeTorch(cuda=False))


def test_unknown_base_passes_through():
    # Other torch backends (xpu, hpu, ...) are accepted verbatim, as before.
    assert resolve_device("xpu", _FakeTorch()) == "xpu"


def test_cpu_always_ok():
    assert resolve_device("cpu", _FakeTorch()) == "cpu"
