from __future__ import annotations

from pathlib import Path

import cv2
import keyring
import numpy as np
import pytest
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    """Stands in for the OS keyring so tests run on CI machines without one."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "workspace"
    monkeypatch.setenv("INNO_STUDIO_HOME", str(home))
    monkeypatch.setenv("INNO_STUDIO_USER", "tester")
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    yield home


@pytest.fixture
def memory_keyring():
    return keyring.get_keyring()


def write_video(path: Path, frames: int = 30, size: tuple[int, int] = (320, 240), fps: float = 20.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    assert writer.isOpened(), "OpenCV could not open a video writer"
    for i in range(frames):
        frame = np.full((size[1], size[0], 3), 40, np.uint8)
        cv2.circle(frame, (20 + i * 5, size[1] // 2), 15, (0, 200, 255), -1)
        writer.write(frame)
    writer.release()
    return path


def write_image(path: Path, seed: int, size: tuple[int, int] = (64, 48)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", image)
    assert ok
    path.write_bytes(buf.tobytes())
    return path


@pytest.fixture
def video(tmp_path) -> Path:
    return write_video(tmp_path / "footage" / "clip.mp4")


@pytest.fixture
def product_root(tmp_path) -> Path:
    """Three product folders, one with an Arabic name."""
    root = tmp_path / "products"
    seed = 0
    for name, count in (("cans", 3), ("bottles", 2), ("سياحي كبير", 4)):
        for i in range(count):
            seed += 1
            write_image(root / name / f"img_{i}.png", seed)
    return root


def make_constant_onnx(path, output: np.ndarray, imgsz: int) -> None:
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper

    inp = helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, imgsz, imgsz])
    out = helper.make_tensor_value_info("output0", TensorProto.FLOAT, list(output.shape))
    # output = const + 0 * mean(images): the input is used, the result is the constant
    nodes = [
        helper.make_node("ReduceMean", ["images"], ["m"], keepdims=0),
        helper.make_node("Mul", ["m", "zero"], ["z"]),
        helper.make_node("Add", ["const", "z"], ["output0"]),
    ]
    graph = helper.make_graph(nodes, "const", [inp], [out], initializer=[
        numpy_helper.from_array(output.astype(np.float32), "const"),
        numpy_helper.from_array(np.zeros((), np.float32), "zero")])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(path))


def make_rfdetr_onnx(path, dets: np.ndarray, logits: np.ndarray, hw: tuple[int, int] = (64, 64)) -> None:
    """A tiny graph shaped like an RF-DETR export: input `input`, outputs `dets` and `labels` (constants)."""
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper

    inp = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, hw[0], hw[1]])
    outs = [helper.make_tensor_value_info("dets", TensorProto.FLOAT, [1, *dets.shape]),
            helper.make_tensor_value_info("labels", TensorProto.FLOAT, [1, *logits.shape])]
    nodes = [helper.make_node("ReduceMean", ["input"], ["m"], keepdims=0),
             helper.make_node("Mul", ["m", "zero"], ["z"]),
             helper.make_node("Add", ["cd", "z"], ["dets"]),
             helper.make_node("Add", ["cl", "z"], ["labels"])]
    graph = helper.make_graph(nodes, "rfdetr_const", [inp], outs, initializer=[
        numpy_helper.from_array(dets[None].astype(np.float32), "cd"),
        numpy_helper.from_array(logits[None].astype(np.float32), "cl"),
        numpy_helper.from_array(np.zeros((), np.float32), "zero")])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(path))
