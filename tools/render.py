# -*- coding: utf-8 -*-
"""纯 numpy 软件光栅器：把蒙皮后的网格画成图片。

预览器本身走 OpenGL（见 `viewer.py`），这里的软光栅用于**离线出图**：
无窗口环境（CI、批处理、agent）也能确认"数据解出来到底长什么样"。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

#: 不同网格的默认底色（按网格序号轮换）
PALETTE = [
    (222, 214, 205),
    (196, 205, 222),
    (214, 200, 214),
    (205, 222, 208),
    (222, 209, 190),
    (200, 200, 200),
]


@dataclass
class Camera:
    """绕目标点的轨道相机（X4 是 Y 轴向上）。"""

    target: np.ndarray
    distance: float
    azimuth: float = 0.0  # 度
    elevation: float = 8.0  # 度
    fov: float = 32.0
    width: int = 480
    height: int = 720
    near: float = 1.0

    @property
    def eye(self) -> np.ndarray:
        a = np.radians(self.azimuth)
        e = np.radians(self.elevation)
        d = np.array([np.sin(a) * np.cos(e), np.sin(e), np.cos(a) * np.cos(e)])
        return np.asarray(self.target, dtype=np.float64) + d * self.distance

    def view(self) -> np.ndarray:
        eye = self.eye
        fwd = np.asarray(self.target, dtype=np.float64) - eye
        fwd /= max(np.linalg.norm(fwd), 1e-9)
        up = np.array([0.0, 1.0, 0.0])
        if abs(float(np.dot(fwd, up))) > 0.999:
            up = np.array([0.0, 0.0, 1.0])
        right = np.cross(fwd, up)
        right /= max(np.linalg.norm(right), 1e-9)
        up2 = np.cross(right, fwd)
        m = np.eye(4)
        m[0, :3] = right
        m[1, :3] = up2
        m[2, :3] = -fwd
        m[:3, 3] = -m[:3, :3] @ eye
        return m


def _normalize(v: np.ndarray, axis=-1):
    n = np.linalg.norm(v, axis=axis, keepdims=True)
    return np.divide(v, np.where(n > 1e-12, n, 1.0))


def render(
    parts,
    camera: Camera,
    background=(38, 40, 46),
    floor_y: float | None = None,
    floor_extent: float = 300.0,
    ambient: float = 0.32,
) -> Image.Image:
    """`parts` 是 [(mesh, positions, normals), ...]；返回 PIL 图像。"""
    w, h = camera.width, camera.height
    color = np.zeros((h, w, 3), dtype=np.float32)
    color[:] = np.array(background, dtype=np.float32) / 255.0
    depth = np.full((h, w), np.inf, dtype=np.float32)

    view = camera.view()
    focal = (h * 0.5) / np.tan(np.radians(camera.fov) * 0.5)
    cx, cy = (w - 1) * 0.5, (h - 1) * 0.5

    light_dir = _normalize(np.array([0.35, 0.75, 0.55]))

    tri_list = []
    for mi, (mesh, pos, nrm) in enumerate(parts):
        if mesh.faces.size == 0:
            continue
        tri_list.append((mesh, np.asarray(pos, dtype=np.float64), nrm, PALETTE[mi % len(PALETTE)]))

    if floor_y is not None:
        g = floor_extent
        pts = np.array(
            [
                [-g, floor_y, -g],
                [g, floor_y, -g],
                [g, floor_y, g],
                [-g, floor_y, g],
            ]
        )
        nrms = np.tile(np.array([0.0, 1.0, 0.0]), (4, 1))

        class _Floor:
            faces = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int32)

        tri_list.insert(0, (_Floor(), pts, nrms, (58, 60, 68)))

    for mesh, pos, nrm, base in tri_list:
        faces = mesh.faces
        v = np.concatenate([pos, np.ones((pos.shape[0], 1))], axis=1)
        cam = v @ view.T
        z = -cam[:, 2]
        ok = z > camera.near
        if not ok.any():
            continue
        safe_z = np.where(ok, z, 1.0)
        sx = focal * cam[:, 0] / safe_z + cx
        sy = cy - focal * cam[:, 1] / safe_z

        face_ok = ok[faces].all(axis=1)
        if not face_ok.any():
            continue
        f = faces[face_ok]
        zf = z[f]
        x0, y0 = sx[f[:, 0]], sy[f[:, 0]]
        x1, y1 = sx[f[:, 1]], sy[f[:, 1]]
        x2, y2 = sx[f[:, 2]], sy[f[:, 2]]

        if nrm is not None:
            fn = _normalize(np.cross(pos[f[:, 1]] - pos[f[:, 0]], pos[f[:, 2]] - pos[f[:, 0]]))
            vn = (nrm[f[:, 0]] + nrm[f[:, 1]] + nrm[f[:, 2]]) / 3.0
            shade = np.abs(_normalize(vn) @ light_dir)
        else:
            fn = _normalize(np.cross(pos[f[:, 1]] - pos[f[:, 0]], pos[f[:, 2]] - pos[f[:, 0]]))
            shade = np.abs(fn @ light_dir)
        lam = ambient + (1.0 - ambient) * shade
        cols = np.clip(np.array(base, dtype=np.float64)[None, :] / 255.0 * lam[:, None], 0, 1)

        minx = np.floor(np.minimum(np.minimum(x0, x1), x2)).astype(int)
        maxx = np.ceil(np.maximum(np.maximum(x0, x1), x2)).astype(int)
        miny = np.floor(np.minimum(np.minimum(y0, y1), y2)).astype(int)
        maxy = np.ceil(np.maximum(np.maximum(y0, y1), y2)).astype(int)
        minx = np.clip(minx, 0, w - 1)
        maxx = np.clip(maxx, 0, w - 1)
        miny = np.clip(miny, 0, h - 1)
        maxy = np.clip(maxy, 0, h - 1)

        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        keep = (np.abs(area) > 1e-9) & (maxx >= minx) & (maxy >= miny)
        if not keep.any():
            continue

        for i in np.nonzero(keep)[0]:
            bx0, bx1 = int(minx[i]), int(maxx[i])
            by0, by1 = int(miny[i]), int(maxy[i])
            if bx1 - bx0 > w or by1 - by0 > h:
                continue
            xs = np.arange(bx0, bx1 + 1) + 0.5
            ys = np.arange(by0, by1 + 1) + 0.5
            gx, gy = np.meshgrid(xs, ys)
            d = area[i]
            w0 = ((x1[i] - gx) * (y2[i] - gy) - (x2[i] - gx) * (y1[i] - gy)) / d
            w1 = ((x2[i] - gx) * (y0[i] - gy) - (x0[i] - gx) * (y2[i] - gy)) / d
            w2 = 1.0 - w0 - w1
            inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            if not inside.any():
                continue
            zi = w0 * zf[i, 0] + w1 * zf[i, 1] + w2 * zf[i, 2]
            sub_depth = depth[by0:by1 + 1, bx0:bx1 + 1]
            closer = inside & (zi < sub_depth)
            if not closer.any():
                continue
            sub_depth[closer] = zi[closer]
            sub_color = color[by0:by1 + 1, bx0:bx1 + 1]
            sub_color[closer] = cols[i]

    img = Image.fromarray((np.clip(color, 0, 1) * 255).astype(np.uint8), "RGB")
    return img


def add_label(img: Image.Image, text: str) -> Image.Image:
    out = img.copy()
    draw = ImageDraw.Draw(out)
    draw.rectangle([0, 0, out.width, 18], fill=(20, 21, 25))
    draw.text((5, 4), text, fill=(225, 228, 235))
    return out


def contact_sheet(images, columns: int = 4, label: bool = True) -> Image.Image:
    """把多张图拼成一张对比图表。"""
    if not images:
        raise ValueError("没有图片")
    w, h = images[0].size
    rows = (len(images) + columns - 1) // columns
    sheet = Image.new("RGB", (w * columns, h * rows), (24, 25, 28))
    for i, img in enumerate(images):
        sheet.paste(img, ((i % columns) * w, (i // columns) * h))
    return sheet
