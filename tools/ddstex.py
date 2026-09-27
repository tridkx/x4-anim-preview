# -*- coding: utf-8 -*-
"""DDS + BC1/BC3 解码（纯 numpy），用于把 mod 的贴图喂给软光栅预览。

X4 的角色贴图在资产目录里以 ``.gz`` 存放，解压出来是标准 DDS：
mod 自己导出的贴图实测是 **DXT1（BC1）** 与 **DXT5（BC3）**、1024×1024、11 级 mip。
预览只看最清晰的 mip 0，所以这里只解第一层。

    >>> rgba = load_dds(gzip.decompress(Path('lumine_cloth_diff.gz').read_bytes()))
    >>> rgba.shape
    (1024, 1024, 4)
"""

from __future__ import annotations

import gzip
import struct
from pathlib import Path

import numpy as np

DDS_MAGIC = b"DDS "


class DdsError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 块压缩解码
# ---------------------------------------------------------------------------


def _rgb565(c: np.ndarray) -> np.ndarray:
    """(N,) uint16 -> (N,3) uint8。"""
    r = ((c >> 11) & 0x1F).astype(np.uint16) * 255 // 31
    g = ((c >> 5) & 0x3F).astype(np.uint16) * 255 // 63
    b = (c & 0x1F).astype(np.uint16) * 255 // 31
    return np.stack([r, g, b], axis=1).astype(np.uint8)


def _bc1_colors(blocks: np.ndarray):
    """8 字节/块的 BC1 颜色部分 -> (N,16,3) uint8。"""
    c0 = blocks[:, 0].astype(np.uint16) | (blocks[:, 1].astype(np.uint16) << 8)
    c1 = blocks[:, 2].astype(np.uint16) | (blocks[:, 3].astype(np.uint16) << 8)
    e0, e1 = _rgb565(c0).astype(np.int16), _rgb565(c1).astype(np.int16)

    n = blocks.shape[0]
    pal = np.zeros((n, 4, 3), dtype=np.int16)
    pal[:, 0] = e0
    pal[:, 1] = e1
    four = c0 > c1
    mid = np.where(four[:, None], (2 * e0 + e1) // 3, (e0 + e1) // 2)
    pal[:, 2] = mid
    pal[:, 3] = np.where(four[:, None], (e0 + 2 * e1) // 3, 0)

    # 每字节 = 一行 4 个像素，低 2 位是第一个
    idx_bytes = blocks[:, 4:8].astype(np.uint16)          # (N,4)
    shifts = np.array([0, 2, 4, 6], dtype=np.uint16)
    bits = (idx_bytes[:, :, None] >> shifts[None, None, :]) & 0x3   # (N,4,4)
    sel = bits.reshape(n, 16)                              # 行优先：byte k = 第 k 行
    return pal[np.arange(n)[:, None], sel].astype(np.uint8)


def _bc3_alpha(blocks: np.ndarray) -> np.ndarray:
    """16 字节/块的 BC3 前 8 字节 -> (N,16) uint8。

    a0 > a1 走 8 值插值，否则走 6 值 + 全透明/全不透明两档；索引是 48 位、每像素 3 位。
    """
    a0 = blocks[:, 0].astype(np.int32)
    a1 = blocks[:, 1].astype(np.int32)
    n = blocks.shape[0]

    packed = np.zeros(n, dtype=np.uint64)
    for k in range(6):
        packed |= blocks[:, 2 + k].astype(np.uint64) << np.uint64(8 * k)

    pal = np.zeros((n, 8), dtype=np.int32)
    pal[:, 0] = a0
    pal[:, 1] = a1
    eight = a0 > a1
    for i in range(6):
        v8 = ((6 - i) * a0 + (1 + i) * a1) // 7
        if i < 4:
            v6 = ((4 - i) * a0 + (1 + i) * a1) // 5
        elif i == 4:
            v6 = np.zeros(n, dtype=np.int32)
        else:
            v6 = np.full(n, 255, dtype=np.int32)
        pal[:, 2 + i] = np.where(eight, v8, v6)

    sel = np.zeros((n, 16), dtype=np.int64)
    for p in range(16):
        sel[:, p] = (packed >> np.uint64(3 * p)) & np.uint64(7)
    return pal[np.arange(n)[:, None], sel].astype(np.uint8)


def _blocks_to_image(px: np.ndarray, width: int, height: int, channels: int) -> np.ndarray:
    """(N,16,C) -> (H,W,C)，块按行优先排列。"""
    bw, bh = (width + 3) // 4, (height + 3) // 4
    img = px.reshape(bh, bw, 4, 4, channels)
    img = img.transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, channels)
    return img[:height, :width]


# ---------------------------------------------------------------------------
# DDS 容器
# ---------------------------------------------------------------------------


def _parse_header(data: bytes):
    if data[:4] != DDS_MAGIC:
        raise DdsError(f"不是 DDS：{data[:4]!r}")
    height, width = struct.unpack_from("<II", data, 12)
    mips = struct.unpack_from("<I", data, 28)[0]
    fourcc = struct.unpack_from("<I", data, 84)[0].to_bytes(4, "little")
    offset = 128
    dxgi = None
    if data[84:88] == b"DX10":
        dxgi = struct.unpack_from("<I", data, 128)[0]
        offset = 148
    return width, height, max(mips, 1), fourcc, dxgi, offset


def load_dds(data: bytes) -> np.ndarray:
    """返回 (H, W, 4) uint8 的 RGBA。只解 mip 0。"""
    width, height, _mips, fourcc, dxgi, offset = _parse_header(data)
    body = data[offset:]

    # DXGI 71 = BC1_UNORM，77 = BC3_UNORM
    if fourcc == b"DXT1" or (fourcc == b"DX10" and dxgi == 71):
        need = ((width + 3) // 4) * ((height + 3) // 4) * 8
        blocks = np.frombuffer(body[:need], dtype=np.uint8).reshape(-1, 8)
        rgb = _bc1_colors(blocks)
        img = _blocks_to_image(rgb, width, height, 3)
        alpha = np.full((height, width, 1), 255, dtype=np.uint8)
        return np.concatenate([img, alpha], axis=2)

    if fourcc == b"DXT5" or (fourcc == b"DX10" and dxgi == 77):
        need = ((width + 3) // 4) * ((height + 3) // 4) * 16
        blocks = np.frombuffer(body[:need], dtype=np.uint8).reshape(-1, 16)
        rgb = _bc1_colors(blocks[:, 8:16])
        a = _bc3_alpha(blocks)
        img = _blocks_to_image(rgb, width, height, 3)
        alpha = _blocks_to_image(a[:, :, None], width, height, 1)
        return np.concatenate([img, alpha], axis=2)

    if fourcc == 0 and struct.unpack_from("<I", data, 88)[0] == 32:
        # 未压缩 BGRA8
        need = width * height * 4
        arr = np.frombuffer(body[:need], dtype=np.uint8).reshape(height, width, 4)
        return arr[:, :, [2, 1, 0, 3]].copy()

    raise DdsError(f"暂不支持的 DDS 格式 fourcc={fourcc!r} dxgi={dxgi}")


def load_texture(path: str | Path) -> np.ndarray:
    """读 ``.gz`` 或裸 ``.dds`` 贴图，返回 (H, W, 4) uint8。"""
    p = Path(path)
    raw = p.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return load_dds(raw)


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        img = load_texture(arg)
        print(f"{arg}: {img.shape} 平均色 {img[:, :, :3].reshape(-1, 3).mean(axis=0).round(1)}")
