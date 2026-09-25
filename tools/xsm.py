# -*- coding: utf-8 -*-
"""X4 ``.xsm`` 骨骼动画读取器（逆向所得格式）。

文件结构::

    "XSM " + u32 version + 头部字段 + 导出器/源文件/日期三个字符串
    随后是 N 条节点记录，首尾相接::

        [记录头][u32 name_len][name][位置关键帧段][旋转关键帧段][可能的附加段]

记录头里与帧数有关的字段固定在 ``name_len 字段 - 0x50`` 处::

    +0x00 (相对该处)  u32 位置关键帧数
    +0x04            u32 旋转关键帧数
    +0x08            u32 附加段 A 帧数
    +0x0c            u32 附加段 B 帧数（老格式才有意义）

关键帧编码有两种（同一文件内一致）::

    A. 现代资产（X4 9.00 常见）
       记录头 96 字节，旋转段 12 字节/帧：i16 x,y,z,w（Q15 定点四元数）+ f32 t
       位置段恒定 16 字节/帧：f32 x,y,z,t
    B. 老资产（2008~2011 导出的 X3 时代动画）
       记录头 100 字节，旋转段 20 字节/帧：f32 x,y,z,w + f32 t
       位置段同样是 16 字节/帧；另有一段同样 20 字节/帧的附加轨道，
       关键帧之后还有 32 字节固定数据

坐标系与 `.xac` 一致（Y 轴向上，单位厘米），旋转是**相对父骨的局部旋转**。
现代 X4 资产的节点名与 `.xac` 骨架同名（``Bip01 ...``）；老资产用
``root/spine1/l_upleg`` 旧命名，与现役骨架对不上，只能作参考。

定位方式：先在文件头部扫出第一条记录并判定布局，之后按记录长度**链式推进**，
遇到不合法位置时在 ±24 字节内重新对齐。这样既快（不扫全文件）又能容忍
导出器版本差异。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.-+()[]:/\\"
)
_Q15 = 1.0 / 32767.0
_ALIGN_STEPS = (0, 4, -4, 8, -8, 12, -12, 16, -16, 20, -20, 24, -24)


class XsmError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    """一种记录布局（不同 3ds Max 导出器版本会有差异）。"""

    tag: str
    counts_back: int  # 帧数字段相对 name_len 字段的负偏移
    header_size: int  # 记录头长度
    rot_bytes: int  # 旋转段每帧字节数
    extra_index: int  # 附加段帧数在 counts 里的下标（-1 表示无）
    tail_bytes: int  # 关键帧之后的固定尾巴

    def key_bytes(self, counts) -> int:
        size = 16 * counts[0] + self.rot_bytes * counts[1]
        if self.extra_index >= 0:
            size += self.rot_bytes * counts[self.extra_index]
        return size + self.tail_bytes


LAYOUT_MODERN = Layout("q15-12", 20, 100, 12, -1, 0)
LAYOUT_LEGACY = Layout("f32-20", 20, 100, 20, 3, 32)
LAYOUTS = (LAYOUT_MODERN, LAYOUT_LEGACY)

MAX_FRAMES = 400_000


@dataclass
class Curve:
    """一条关键帧曲线。位置为 (N,3)，旋转为 (N,4) 单位四元数。"""

    times: np.ndarray
    values: np.ndarray

    def __len__(self):
        return int(self.times.shape[0])

    @property
    def duration(self) -> float:
        return float(self.times[-1]) if len(self) else 0.0

    def _span(self, t: float):
        t = float(min(max(t, self.times[0]), self.times[-1]))
        i = int(np.searchsorted(self.times, t, side="right") - 1)
        i = min(max(i, 0), len(self) - 2)
        t0, t1 = float(self.times[i]), float(self.times[i + 1])
        a = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return i, a

    def sample_pos(self, t: float) -> np.ndarray:
        if len(self) == 1:
            return self.values[0].copy()
        i, a = self._span(t)
        return self.values[i] * (1.0 - a) + self.values[i + 1] * a

    def sample_quat(self, t: float) -> np.ndarray:
        """相邻帧 nlerp；X4 的关键帧足够密，不必 slerp。"""
        if len(self) == 1:
            return self.values[0].copy()
        i, a = self._span(t)
        q0, q1 = self.values[i], self.values[i + 1]
        if float(np.dot(q0, q1)) < 0.0:  # 走短弧
            q1 = -q1
        q = q0 * (1.0 - a) + q1 * a
        n = float(np.linalg.norm(q))
        return q / n if n > 1e-9 else np.array([0.0, 0.0, 0.0, 1.0])


@dataclass
class NodeAnim:
    name: str
    pos_frames: int = 0
    rot_frames: int = 0
    extra_frames: tuple[int, int] = (0, 0)
    offset: int = 0
    pos: Curve | None = None
    rot: Curve | None = None
    bind_quat: np.ndarray | None = None
    bind_pos: np.ndarray | None = None
    bind_scale: np.ndarray | None = None

    @property
    def duration(self) -> float:
        d = 0.0
        if self.pos is not None:
            d = max(d, self.pos.duration)
        if self.rot is not None:
            d = max(d, self.rot.duration)
        return d


@dataclass
class Xsm:
    path: Path
    layout: str = ""
    exporter: str = ""
    source_file: str = ""
    export_date: str = ""
    nodes: list[NodeAnim] = field(default_factory=list)
    _by_name: dict[str, NodeAnim] = field(default_factory=dict, repr=False)

    def __len__(self):
        return len(self.nodes)

    def __contains__(self, name: str) -> bool:
        return _norm(name) in self._by_name

    def get(self, name: str) -> NodeAnim | None:
        return self._by_name.get(_norm(name))

    @property
    def duration(self) -> float:
        return max((n.duration for n in self.nodes), default=0.0)

    def frame_rate(self) -> float:
        deltas: list[float] = []
        for node in self.nodes:
            for curve in (node.pos, node.rot):
                if curve is not None and len(curve) > 2:
                    d = np.diff(curve.times)
                    d = d[d > 1e-6]
                    if d.size:
                        deltas.append(float(np.median(d)))
        if not deltas:
            return 15.0
        dt = float(np.median(deltas))
        return round(1.0 / dt) if dt > 1e-6 else 15.0


def _norm(name: str) -> str:
    return name.strip().lower().replace("_", " ").replace(".", " ").strip()


# ---------------------------------------------------------------------------
# 定位与推进
# ---------------------------------------------------------------------------


def _scan_names(data: bytes, start: int = 0x20, stop: int | None = None):
    """扫描 ``u32 长度 + ASCII 名字`` 候选。"""
    out: list[tuple[int, str]] = []
    end = len(data) if stop is None else min(stop, len(data))
    i = start
    while i < end - 8:
        length = struct.unpack_from("<I", data, i)[0]
        if 2 <= length <= 96 and i + 4 + length <= len(data):
            raw = data[i + 4:i + 4 + length]
            if all(chr(c) in NAME_CHARS for c in raw):
                out.append((i, raw.decode("ascii")))
                i += 4 + length
                continue
        i += 1
    return out


def _looks_like_record(data: bytes, off: int) -> bool:
    if off < 0 or off + 5 > len(data):
        return False
    n = struct.unpack_from("<I", data, off)[0]
    if not (2 <= n <= 96) or off + 4 + n > len(data):
        return False
    return all(chr(c) in NAME_CHARS for c in data[off + 4:off + 4 + n])


def _align_next(data: bytes, guess: int):
    for delta in _ALIGN_STEPS:
        if _looks_like_record(data, guess + delta):
            return guess + delta
    return None


def _read_counts(data: bytes, off: int, layout: Layout):
    start = off - layout.counts_back
    if start < 0 or start + 16 > len(data):
        return None
    n = 5 if layout.extra_index >= 0 else 4
    if start + 4 * n > len(data):
        return None
    vals = struct.unpack_from("<%dI" % n, data, start)
    if vals[0] > MAX_FRAMES or vals[1] > MAX_FRAMES:
        return None
    return vals


def _read_bind_pose(data: bytes, header_start: int, layout: Layout):
    """从记录头里取第二组姿态（通常是绑定姿态）。"""
    base = header_start + 0x38
    try:
        pos = np.frombuffer(data, dtype="<f4", count=3, offset=base).astype(np.float64)
        scl = np.frombuffer(data, dtype="<f4", count=3, offset=base + 12).astype(np.float64)
    except ValueError:
        return None, None, None
    if not np.all(np.isfinite(pos)) or (pos.size and float(np.abs(pos).max()) > 1e5):
        return None, None, None

    quat = None
    if layout.rot_bytes == 12:
        raw = np.frombuffer(data, dtype="<i2", count=8, offset=header_start).astype(np.float64) * _Q15
        if abs(np.linalg.norm(raw[4:]) - 1.0) < 0.05:
            quat = raw[4:]
        elif abs(np.linalg.norm(raw[:4]) - 1.0) < 0.05:
            quat = raw[:4]
    else:
        raw = np.frombuffer(data, dtype="<f4", count=8, offset=header_start).astype(np.float64)
        if abs(np.linalg.norm(raw[4:]) - 1.0) < 0.05:
            quat = raw[4:]
        elif abs(np.linalg.norm(raw[:4]) - 1.0) < 0.05:
            quat = raw[:4]
    return pos, quat, scl


def _parse_curves(data: bytes, kd_start: int, layout: Layout, pos_frames: int, rot_frames: int):
    pos_curve = rot_curve = None
    pos_bytes = 0

    if pos_frames > 0 and kd_start + pos_frames * 16 <= len(data):
        raw = np.frombuffer(data, dtype="<f4", count=pos_frames * 4, offset=kd_start).reshape(-1, 4)
        times = raw[:, 3].astype(np.float64)
        if np.all(np.isfinite(times)):
            order = np.argsort(times, kind="stable")
            pos_curve = Curve(times[order], raw[order, :3].astype(np.float64))
            pos_bytes = pos_frames * 16

    rot_start = kd_start + pos_bytes
    if rot_frames > 0:
        if layout.rot_bytes == 12:
            # 12 字节/帧 = 4 x i16（Q15 四元数）+ f32 时间，逐帧内联
            need = rot_frames * 12
            if rot_start + need <= len(data):
                block = np.frombuffer(data, dtype=np.uint8, count=need, offset=rot_start).reshape(-1, 12)
                ints = block[:, :8].copy().view("<i2").reshape(-1, 4)
                times = block[:, 8:12].copy().view("<f4").reshape(-1).astype(np.float64)
                quats = ints.astype(np.float64) * _Q15
                norm = np.linalg.norm(quats, axis=1, keepdims=True)
                quats = np.divide(quats, np.where(norm > 1e-9, norm, 1.0))
                if np.all(np.isfinite(times)):
                    order = np.argsort(times, kind="stable")
                    rot_curve = Curve(times[order], quats[order])
        else:
            need = rot_frames * 20
            if rot_start + need <= len(data):
                raw = np.frombuffer(data, dtype="<f4", count=rot_frames * 5, offset=rot_start).reshape(-1, 5)
                quats = raw[:, :4].astype(np.float64)
                norm = np.linalg.norm(quats, axis=1, keepdims=True)
                quats = np.divide(quats, np.where(norm > 1e-9, norm, 1.0))
                times = raw[:, 4].astype(np.float64)
                if np.all(np.isfinite(times)):
                    order = np.argsort(times, kind="stable")
                    rot_curve = Curve(times[order], quats[order])

    return pos_curve, rot_curve


def _find_first(data: bytes, window: int = 1 << 16):
    """在文件头部找第一条记录并判定布局。"""
    for off, name in _scan_names(data, 0x20, window):
        if off - 128 < 0:
            continue
        for layout in LAYOUTS:
            counts = _read_counts(data, off, layout)
            if counts is None:
                continue
            kd_start = off + 4 + len(name)
            kd = layout.key_bytes(counts)
            if kd_start + kd + layout.header_size > len(data):
                continue
            if _align_next(data, kd_start + kd + layout.header_size) is not None:
                return off, layout
    return None, None


# ---------------------------------------------------------------------------
# 主解析
# ---------------------------------------------------------------------------


def parse_xsm(data: bytes, path: str | Path = "<memory>") -> Xsm:
    if data[:4] != b"XSM ":
        raise XsmError(f"不是 XSM 文件：{data[:4]!r}")

    off, layout = _find_first(data)
    if off is None:
        raise XsmError("没能在文件头部定位到第一条记录")

    anim = Xsm(path=Path(path), layout=layout.tag)
    strings = [s for _, s in _scan_names(data, 0x20, max(0x400, off))]
    if strings:
        anim.exporter = strings[0]
    if len(strings) > 1:
        anim.source_file = strings[1]
    if len(strings) > 2:
        anim.export_date = strings[2]

    seen_offsets: set[int] = set()
    while off is not None and off < len(data) and off not in seen_offsets:
        seen_offsets.add(off)
        name_len = struct.unpack_from("<I", data, off)[0]
        name = data[off + 4:off + 4 + name_len].decode("ascii", "replace")
        counts = _read_counts(data, off, layout)
        if counts is None:
            break
        pos_frames, rot_frames = int(counts[0]), int(counts[1])
        kd_start = off + 4 + name_len
        pos_curve, rot_curve = _parse_curves(data, kd_start, layout, pos_frames, rot_frames)

        node = NodeAnim(
            name=name,
            pos_frames=pos_frames,
            rot_frames=rot_frames,
            extra_frames=(int(counts[2]), int(counts[3])),
            offset=off,
            pos=pos_curve,
            rot=rot_curve,
        )
        node.bind_pos, node.bind_quat, node.bind_scale = _read_bind_pose(
            data, off - layout.header_size, layout
        )
        anim.nodes.append(node)

        off = _align_next(data, kd_start + layout.key_bytes(counts) + layout.header_size)

    if not anim.nodes:
        raise XsmError("解析后没有任何节点")

    anim._by_name = {}
    for node in anim.nodes:
        anim._by_name.setdefault(_norm(node.name), node)
    return anim


def load_xsm(path: str | Path) -> Xsm:
    path = Path(path)
    return parse_xsm(path.read_bytes(), path)


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        a = load_xsm(arg)
        print(f"{arg}")
        print(f"  布局 {a.layout}  节点 {len(a)}  时长 {a.duration:.2f}s  帧率 {a.frame_rate():.0f}fps")
        print(f"  exporter={a.exporter!r} source={a.source_file!r} date={a.export_date!r}")
        for node in a.nodes[:5]:
            print(f"    {node.name:<24} pos={node.pos_frames:<5} rot={node.rot_frames:<5} "
                  f"dur={node.duration:.2f}")
