# -*- coding: utf-8 -*-
"""最小示例：不开窗口，把一条动画的关键帧渲染成 PNG。

    python examples/offline_check.py

用于确认工具链本身工作正常，也方便在没有图形界面的机器上出图。
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import numpy as np
from PIL import Image

import x4game, xac, xsm, rig, render

OUT = Path(__file__).resolve().parent.parent / "work" / "verify"
OUT.mkdir(parents=True, exist_ok=True)

g = x4game.GameArchive()
print("游戏:", g.root)

body_path = "assets/characters/argon/bodies/char_arg_f_body_civjacket_01.xac"
print("可用躯干:", [p for p in g.find("argon/bodies/char_arg_f_body") ][:10])

raw = g.read(body_path)
asset = xac.load_xac(body_path, raw)
print(f"资产 {body_path}: 节点 {len(asset.nodes)} 网格 {len(asset.meshes)} 材质 {len(asset.materials)}")
for m in asset.meshes:
    print(f"   mesh{m.mesh_id} node={m.node_id} 顶点={m.vertex_count} 三角={m.face_count} "
          f"uv={'有' if m.uvs is not None else '无'} 权重={'有' if m.bone_ids is not None else '无'}")

anim_path = "assets/characters/animations/anim_a-a_ar_fe_generic_01.xsm"
anim = xsm.parse_xsm(g.read(anim_path), anim_path)
print(f"动画 {anim_path}: 节点 {len(anim)} 时长 {anim.duration:.2f}s 帧率 {anim.frame_rate():.0f}fps")

t0 = time.time()
r = rig.Rig.build(asset, anim)
print(f"骨架: {len(r)} 根骨, 动画匹配率 {r.matched_fraction():.1%}, 构建 {time.time()-t0:.2f}s")
print("  未匹配:", r.missing[:12])

# 网格中心用于取景
pts = np.concatenate([m.positions for m in asset.meshes])
lo, hi = pts.min(axis=0), pts.max(axis=0)
center = (lo + hi) / 2
print("包围盒:", np.round(lo, 1), np.round(hi, 1))

cam = render.Camera(target=center, distance=340, azimuth=0, elevation=6, width=380, height=620)
images = []

# 1) 纯绑定姿态（不套动画）
import copy
static = rig.Rig.build(asset, None)
for label, rr, t in [("bind", static, 0.0), ("f0", r, 0.0), ("t=1s", r, 1.0),
                     ("t=2s", r, 2.0), ("t=3s", r, 3.0), ("t=4s", r, 4.0)]:
    t1 = time.time()
    parts = rr.pose_all(t)
    img = render.render(parts, cam, floor_y=float(lo[1]))
    images.append(render.add_label(img, f"{label}  ({time.time()-t1:.2f}s)"))
    print(f"  渲染 {label}: {time.time()-t1:.2f}s")

sheet = render.contact_sheet(images, columns=6)
out = OUT / "body_anim_frames.png"
sheet.save(out)
print("写出", out, sheet.size)

# 侧面
cam2 = render.Camera(target=center, distance=340, azimuth=90, elevation=4, width=380, height=620)
side = []
for label, t in [("side bind", 0.0), ("side t=1", 1.0), ("side t=2", 2.0), ("side t=3", 3.0)]:
    rr = static if "bind" in label else r
    side.append(render.add_label(render.render(rr.pose_all(t), cam2, floor_y=float(lo[1])), label))
out2 = OUT / "body_anim_side.png"
render.contact_sheet(side, columns=4).save(out2)
print("写出", out2)
