# -*- coding: utf-8 -*-
"""纯 numpy 软件光栅器：把蒙皮后的网格画成图片。

预览器本身走 OpenGL（见 `glview.py`），这里的软光栅用于**离线出图**：
无窗口环境（CI、批处理、AI agent）也能确认"数据解出来到底长什么样"。

支持：

* 逐材质贴图（albedo，按 UV 采样；alpha 低于阈值直接丢弃，头发镂空能出来）
* 骨骼线段叠加（亮绿，穿透显示）
* 地板网格（固定在世界 Y=0，和指标口径一致）
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

#: 没有贴图时的兜底底色（按网格序号轮换）
PALETTE = [
    (222, 214, 205),
    (196, 205, 222),
    (214, 200, 214),
    (205, 222, 208),
    (222, 209, 190),
    (200, 200, 200),
]

BONE_COLOR = (70, 255, 120)


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

    def project(self, pts: np.ndarray):
        """世界坐标 -> 屏幕坐标 (x, y) 与深度 z（给画骨骼线用）。"""
        pts = np.asarray(pts, dtype=np.float64)
        v = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
        cam = v @ self.view().T
        z = -cam[:, 2]
        focal = (self.height * 0.5) / np.tan(np.radians(self.fov) * 0.5)
        safe = np.where(z > self.near, z, 1.0)
        sx = focal * cam[:, 0] / safe + (self.width - 1) * 0.5
        sy = (self.height - 1) * 0.5 - focal * cam[:, 1] / safe
        return np.stack([sx, sy], axis=1), z


def _normalize(v: np.ndarray, axis=-1):
    n = np.linalg.norm(v, axis=axis, keepdims=True)
    return np.divide(v, np.where(n > 1e-12, n, 1.0))


def _sample(tex: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """双线性采样，UV 按 wrap 处理。X4 的 v 轴与图像一致（v=0 在顶部）。

    发片这类 alpha 镂空几何用最近邻会碎成一片点，双线性能明显改善边缘。
    """
    h, w = tex.shape[:2]
    x = np.mod(u, 1.0) * w - 0.5
    y = np.mod(v, 1.0) * h - 0.5
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    x0m, x1m = np.mod(x0, w), np.mod(x0 + 1, w)
    y0m, y1m = np.mod(y0, h), np.mod(y0 + 1, h)
    c00 = tex[y0m, x0m].astype(np.float32)
    c10 = tex[y0m, x1m].astype(np.float32)
    c01 = tex[y1m, x0m].astype(np.float32)
    c11 = tex[y1m, x1m].astype(np.float32)
    top = c00 + (c10 - c00) * fx
    bot = c01 + (c11 - c01) * fx
    return top + (bot - top) * fy


def render(
    parts,
    camera: Camera,
    background=(38, 40, 46),
    floor_y: float | None = None,
    floor_extent: float = 300.0,
    ambient: float = 0.32,
    textures: dict | None = None,
    bones=None,
    bone_color=BONE_COLOR,
    alpha_cutoff: float = 0.5,
) -> Image.Image:
    """``parts`` 是 ``[(mesh, positions, normals), ...]``。

    :param textures: ``{material_id: SurfaceTex}``，或者与 ``parts`` 等长的列表
                     （每个 part 一张表，用于多资产共用 material_id 的场景）。
                     ``SurfaceTex.alpha_test`` 为 ``None`` 表示该材质**不做** alpha 裁剪
                     ——这是照游戏 shader 来的：只有 ``p1_hair`` 里写了 ``a < 0.5 discard``，
                     ``p1_character`` 完全没有裁剪。
    :param bones:    ``[(a, b), ...]`` 世界坐标线段，画在网格之上（穿透显示）
    """
    w, h = camera.width, camera.height
    color = np.zeros((h, w, 3), dtype=np.float32)
    color[:] = np.array(background, dtype=np.float32) / 255.0
    depth = np.full((h, w), np.inf, dtype=np.float32)

    view = camera.view()
    focal = (h * 0.5) / np.tan(np.radians(camera.fov) * 0.5)
    cx, cy = (w - 1) * 0.5, (h - 1) * 0.5
    light_dir = _normalize(np.array([0.35, 0.75, 0.55]))

    # 按「材质」而不是按「网格」分批：一个网格常含多个材质槽
    groups = []
    tex_list = textures if isinstance(textures, (list, tuple)) else None
    for mi, (mesh, pos, nrm) in enumerate(parts):
        if mesh.faces.size == 0:
            continue
        pos = np.asarray(pos, dtype=np.float64)
        # 每个 part 用自己的贴图表。注意别覆盖外层的 textures——
        # 之前就是在这里把它改成"最后一个 part 的表"，结果所有网格都查不到自己的材质
        part_map = tex_list[mi] if (tex_list is not None and mi < len(tex_list)) else textures
        subs = getattr(mesh, "submesh_material", None)
        if subs:
            for first, count, mat_id in subs:
                if count > 0:
                    groups.append((mesh, pos, nrm, mesh.faces[first:first + count],
                                   mat_id, mi, part_map))
        else:
            groups.append((mesh, pos, nrm, mesh.faces, None, mi, part_map))

    if floor_y is not None:
        g = floor_extent
        pts = np.array([[-g, floor_y, -g], [g, floor_y, -g],
                        [g, floor_y, g], [-g, floor_y, g]])

        class _Floor:
            faces = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int32)

        groups.insert(0, (_Floor(), pts, np.tile(np.array([0.0, 1.0, 0.0]), (4, 1)),
                          _Floor.faces, None, -1, None))

    for mesh, pos, nrm, faces, mat_id, mi, part_map in groups:
        base = (58, 60, 68) if mi < 0 else PALETTE[mi % len(PALETTE)]
        tex = part_map.get(mat_id) if (part_map and mat_id is not None) else None
        if tex is not None and getattr(mesh, "uvs", None) is None:
            tex = None
        tex_img = getattr(tex, "image", tex)          # 兼容直接传 ndarray 的调用方
        tex_cut = getattr(tex, "alpha_test", alpha_cutoff) if tex is not None else None

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

        fn = _normalize(np.cross(pos[f[:, 1]] - pos[f[:, 0]], pos[f[:, 2]] - pos[f[:, 0]]))
        if nrm is not None:
            vn = (nrm[f[:, 0]] + nrm[f[:, 1]] + nrm[f[:, 2]]) / 3.0
            shade = np.abs(_normalize(vn) @ light_dir)
        else:
            shade = np.abs(fn @ light_dir)
        lam = ambient + (1.0 - ambient) * shade

        uv = mesh.uvs[f] if tex_img is not None else None
        if tex_img is None:
            cols = np.clip(np.array(base, dtype=np.float64)[None, :] / 255.0 * lam[:, None], 0, 1)

        minx = np.clip(np.floor(np.minimum(np.minimum(x0, x1), x2)).astype(int), 0, w - 1)
        maxx = np.clip(np.ceil(np.maximum(np.maximum(x0, x1), x2)).astype(int), 0, w - 1)
        miny = np.clip(np.floor(np.minimum(np.minimum(y0, y1), y2)).astype(int), 0, h - 1)
        maxy = np.clip(np.ceil(np.maximum(np.maximum(y0, y1), y2)).astype(int), 0, h - 1)

        area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)
        keep = (np.abs(area) > 1e-9) & (maxx >= minx) & (maxy >= miny)
        if not keep.any():
            continue

        for i in np.nonzero(keep)[0]:
            bx0, bx1 = int(minx[i]), int(maxx[i])
            by0, by1 = int(miny[i]), int(maxy[i])
            gx, gy = np.meshgrid(np.arange(bx0, bx1 + 1) + 0.5,
                                 np.arange(by0, by1 + 1) + 0.5)
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

            if tex_img is not None:
                u = w0 * uv[i, 0, 0] + w1 * uv[i, 1, 0] + w2 * uv[i, 2, 0]
                vv = w0 * uv[i, 0, 1] + w1 * uv[i, 1, 1] + w2 * uv[i, 2, 1]
                rgba = _sample(tex_img, u, vv).astype(np.float64) / 255.0
                if tex_cut is not None and rgba.ndim == 3 and rgba.shape[-1] == 4:
                    closer = closer & (rgba[:, :, 3] >= tex_cut)
                    if not closer.any():
                        continue
                px = np.clip(rgba[:, :, :3] * lam[i], 0, 1)
            else:
                px = np.broadcast_to(cols[i], (closer.shape[0], closer.shape[1], 3))

            sub_depth[closer] = zi[closer]
            color[by0:by1 + 1, bx0:bx1 + 1][closer] = px[closer]

    img = Image.fromarray((np.clip(color, 0, 1) * 255).astype(np.uint8), "RGB")

    if bones:
        draw = ImageDraw.Draw(img)
        pts = np.array([p for seg in bones for p in seg], dtype=np.float64)
        if len(pts) >= 2:
            scr, z = camera.project(pts)
            for k in range(0, len(pts) - 1, 2):
                if z[k] <= camera.near or z[k + 1] <= camera.near:
                    continue
                draw.line([tuple(scr[k]), tuple(scr[k + 1])], fill=bone_color, width=2)
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
