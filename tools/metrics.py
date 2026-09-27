# -*- coding: utf-8 -*-
"""量化判据：把"动作看着不对劲"翻译成可以比较的数字。

这些指标都是**相对 vanilla 基线**才有意义的——绝对值说明不了问题，
所以每个指标都返回 mod / vanilla 两个值以及它们的比值。

判据来源（实测自三次完整工程，见 skill 文档）：

- **关节撕裂**：每个顶点到"主导它的那根骨"的距离。绑定正确时该值接近 vanilla；
  蒙皮错了会明显偏大（实测错误版本手指 23.9 cm vs vanilla 1.11 cm）。
- **两脚间距**：左右脚踝的横向距离，判断"猫步"。只推到 vanilla 的 77%
  就已经肉眼可见，所以要跟 vanilla 比，不是跟上一版比。
- **骨链横向单调性**：大腿 < 小腿 < 脚踝的横向位置必须单调外撇。逐级递减的
  横向阻尼会把骨链**折弯**，关节落在几何内侧，静止看不出来、一动就是猫步。
- **脚底离地**：最低顶点到地板的高度，判断悬空 / 陷地。
- **绕序**：`dot(面法线, 顶点法线)` 为负的比例。绕序翻转时两者反平行，该比例会
  明显抬头。**不要用"面法线背离质心"那一套**——实测它对多部件/非凸网格不可靠
  （同一个 mod 的躯干 0.63、头部 0.76，vanilla 反而 0.76、0.61，完全不可比，
  会把正常资产判成绕序翻转）。
- **顶点法线 vs 面法线**：`mean|dot|` 接近 1 说明是平面着色（满脸面片），
  平滑着色应该在 0.2~0.6 量级（取决于模型密度）。
"""

from __future__ import annotations

import numpy as np

#: 骨骼名归一化（与 xac/xsm 一致）
def norm(name: str) -> str:
    return name.strip().lower().replace("_", " ").replace(".", " ").strip()


# ---------------------------------------------------------------------------
# 静态：只看资产，不需要动画
# ---------------------------------------------------------------------------


def mesh_static(asset) -> dict:
    """顶点数、法线平滑度、绕序。"""
    total_v = total_f = 0
    smooth, winding = [], []
    per_mesh = []
    for mesh in asset.meshes:
        if mesh.vertex_count == 0 or mesh.face_count == 0:
            continue
        total_v += mesh.vertex_count
        total_f += mesh.face_count
        p, f = mesh.positions, mesh.faces
        fn = np.cross(p[f[:, 1]] - p[f[:, 0]], p[f[:, 2]] - p[f[:, 0]])
        n = np.linalg.norm(fn, axis=1, keepdims=True)
        fn = np.divide(fn, np.where(n > 1e-9, n, 1.0))

        s = None
        flipped = None
        if mesh.normals is not None:
            vn = mesh.normals[f].mean(axis=1)
            nv = np.linalg.norm(vn, axis=1, keepdims=True)
            vn = np.divide(vn, np.where(nv > 1e-9, nv, 1.0))
            dot = (fn * vn).sum(axis=1)
            s = float(np.abs(dot).mean())
            flipped = float((dot < 0).mean())
            smooth.append(s)
            winding.append(flipped)

        c = p.mean(axis=0)
        outward = float((((p[f].mean(axis=1) - c) * fn).sum(axis=1) > 0).mean())
        per_mesh.append({
            "mesh": mesh.mesh_id, "verts": mesh.vertex_count, "tris": mesh.face_count,
            "normal_flatness": None if s is None else round(s, 4),
            "flipped_face_ratio": None if flipped is None else round(flipped, 4),
            "centroid_outward": round(outward, 4),   # 仅供参考，判据不用它
        })

    return {
        "verts": total_v,
        "tris": total_f,
        "normal_flatness": round(float(np.mean(smooth)), 4) if smooth else None,
        "flipped_face_ratio": round(float(np.mean(winding)), 4) if winding else None,
        "meshes": per_mesh,
    }


# ---------------------------------------------------------------------------
# 动态：需要骨架 + 姿态
# ---------------------------------------------------------------------------


def _bone_segments(rig, world: np.ndarray):
    """每根骨的线段 (head, tail)。tail 取主链第一个子骨，没有子骨就退化成一个点。"""
    n = len(rig)
    parents = rig.parents
    first_child = np.full(n, -1, dtype=np.int32)
    for i in range(n):
        p = parents[i]
        if p >= 0 and first_child[p] < 0:
            first_child[p] = i
    heads = world[:, :3, 3]
    tails = heads.copy()
    has = first_child >= 0
    tails[has] = world[first_child[has], :3, 3]
    return heads, tails, has


def _point_seg_distance(pts: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    denom = (ab * ab).sum(axis=1)
    denom = np.where(denom > 1e-9, denom, 1.0)
    t = np.clip(((pts - a) * ab).sum(axis=1) / denom, 0.0, 1.0)
    proj = a + ab * t[:, None]
    return np.linalg.norm(pts - proj, axis=1)


def joint_tear(rig, mesh, positions: np.ndarray, world: np.ndarray) -> float:
    """顶点到主导骨线段的平均距离（cm）。"""
    if mesh.bone_ids is None or mesh.bone_weights is None or mesh.bone_ids.size == 0:
        return float("nan")
    heads, tails, has = _bone_segments(rig, world)
    slot = np.argmax(mesh.bone_weights, axis=1)
    dom_global = mesh.bone_ids[np.arange(mesh.bone_ids.shape[0]), slot]
    local = rig.node_to_local[dom_global]
    ok = (local >= 0) & has[np.clip(local, 0, len(has) - 1)]
    if not ok.any():
        return float("nan")
    idx = local[ok]
    d = _point_seg_distance(positions[ok], heads[idx], tails[idx])
    return float(d.mean())


def _foot_centers(rig, mesh, positions: np.ndarray, side: str):
    """某侧脚部（含脚趾）顶点的横向中心。用几何而不是骨位置——

    骨架逐字节一致时，骨位置在 mod 和 vanilla 上完全相同，拿它算"两脚间距"
    必然得出两边一样的假结论；而猫步本来就是"关节落在几何内侧"的几何问题。
    """
    if mesh.bone_ids is None or mesh.bone_weights is None or mesh.bone_ids.size == 0:
        return None
    names = [norm(rig.asset.nodes[nid].name) for nid in rig.bone_ids]
    slot = np.argmax(mesh.bone_weights, axis=1)
    dom = mesh.bone_ids[np.arange(mesh.bone_ids.shape[0]), slot]
    loc = rig.node_to_local[dom]
    safe = np.clip(loc, 0, len(names) - 1)
    is_side = np.array([f" {side} " in names[i] for i in safe])
    is_foot = np.array([("foot" in names[i] or "toe" in names[i]) for i in safe])
    sel = is_side & is_foot
    if not sel.any():
        return None
    return float(positions[sel][:, 0].mean())


def pose_metrics(scene, t: float) -> dict:
    """一帧的姿态指标：关节撕裂、两脚间距（几何）、脚底离地、骨链横向。

    地面取世界 Y=0（游戏地板），不是模型最低点——后者会让"离地高度"恒为 0。
    """
    out: dict = {"joint_tear_cm": [], "feet_gap_cm": None, "ground_gap_cm": None,
                 "lateral_chain": None, "bone_world": {}}
    ground = 0.0
    lowest = float("inf")
    lx: list[float] = []
    rx: list[float] = []

    for rig, mesh_list in ((r, r.asset.meshes) for r in scene.rigs):
        world = rig.world_matrices(t)
        index = {}
        for i, nid in enumerate(rig.bone_ids):
            index[norm(rig.asset.nodes[nid].name)] = i

        # 关节撕裂（逐网格，按顶点数加权平均）
        num = den = 0.0
        for mesh in mesh_list:
            if mesh.vertex_count == 0:
                continue
            mats = rig.skin_matrices(t)
            pos = rig.skin_positions(mesh, mats)
            val = joint_tear(rig, mesh, pos, world)
            if np.isfinite(val):
                num += val * mesh.vertex_count
                den += mesh.vertex_count
            if pos.size:
                lowest = min(lowest, float(pos[:, 1].min()))
            for side, bucket in (("l", lx), ("r", rx)):
                c = _foot_centers(rig, mesh, pos, side)
                if c is not None:
                    bucket.append(c)
        if den:
            out["joint_tear_cm"].append(num / den)

        for key, name in (("l_foot", "bip01 l foot"), ("r_foot", "bip01 r foot"),
                          ("l_upleg", "bip01 l thigh"), ("l_lowleg", "bip01 l calf"),
                          ("r_upleg", "bip01 r thigh"), ("r_lowleg", "bip01 r calf")):
            if name in index:
                out["bone_world"][key] = world[index[name]][:3, 3].tolist()

    bw = out["bone_world"]
    if lx and rx:
        out["feet_gap_cm"] = float(abs(float(np.mean(lx)) - float(np.mean(rx))))
    if np.isfinite(lowest):
        out["ground_gap_cm"] = float(lowest - ground)
    if all(k in bw for k in ("l_upleg", "l_lowleg", "l_foot")):
        chain = [abs(bw["l_upleg"][0]), abs(bw["l_lowleg"][0]), abs(bw["l_foot"][0])]
        out["lateral_chain"] = [round(v, 2) for v in chain]
        out["lateral_monotonic"] = bool(chain[0] < chain[1] < chain[2])

    tears = [v for v in out["joint_tear_cm"] if np.isfinite(v)]
    out["joint_tear_cm"] = round(float(np.mean(tears)), 3) if tears else None
    if out["feet_gap_cm"] is not None:
        out["feet_gap_cm"] = round(out["feet_gap_cm"], 2)
    if out["ground_gap_cm"] is not None:
        out["ground_gap_cm"] = round(out["ground_gap_cm"], 2)
    return out


def sample_animation(scene, anim, samples: int = 6) -> dict:
    """一条动画采样若干帧，返回逐帧指标与汇总。"""
    dur = max(anim.duration, 1e-6)
    times = [dur * (i + 0.5) / samples for i in range(samples)]
    frames = []
    for t in times:
        m = pose_metrics(scene, t)
        m["t"] = round(t, 3)
        frames.append(m)

    def agg(key):
        vals = [f[key] for f in frames if f.get(key) is not None]
        return round(float(np.mean(vals)), 3) if vals else None

    lateral = [f["lateral_chain"] for f in frames if f.get("lateral_chain")]
    return {
        "samples": len(frames),
        "joint_tear_cm": agg("joint_tear_cm"),
        "joint_tear_max_cm": round(max((f["joint_tear_cm"] for f in frames
                                        if f.get("joint_tear_cm") is not None), default=float("nan")), 3),
        "feet_gap_cm": agg("feet_gap_cm"),
        "feet_gap_min_cm": round(min((f["feet_gap_cm"] for f in frames
                                      if f.get("feet_gap_cm") is not None), default=float("nan")), 2),
        "ground_gap_cm": agg("ground_gap_cm"),
        "ground_gap_min_cm": round(min((f["ground_gap_cm"] for f in frames
                                        if f.get("ground_gap_cm") is not None), default=float("nan")), 2),
        "lateral_chain_ok": (bool(np.mean([f.get("lateral_monotonic", False) for f in frames]) > 0.6)
                             if lateral else None),
        "frames": frames,
    }


# ---------------------------------------------------------------------------
# 对比与判定
# ---------------------------------------------------------------------------


def ratio(mod, vanilla):
    if mod is None or vanilla in (None, 0):
        return None
    return round(float(mod) / float(vanilla), 3)


def compare(mod_m: dict, van_m: dict) -> tuple[dict, list[str]]:
    """把 mod 与 vanilla 的指标并排，给出比值和告警。"""
    flags: list[str] = []
    out: dict = {}

    def put(key, label, lo=None, hi=None, fmt="{:.2f}"):
        m, v = mod_m.get(key), van_m.get(key)
        r = ratio(m, v)
        out[key] = {"mod": m, "vanilla": v, "ratio": r}
        if r is None:
            return
        if lo is not None and r < lo:
            flags.append(f"{label} 只有 vanilla 的 {r:.0%}（下限 {lo:.0%}）：{fmt.format(m)} vs {fmt.format(v)}")
        if hi is not None and r > hi:
            flags.append(f"{label} 是 vanilla 的 {r:.0%}（上限 {hi:.0%}）：{fmt.format(m)} vs {fmt.format(v)}")

    put("joint_tear_cm", "关节撕裂", hi=1.35)
    put("feet_gap_cm", "两脚间距", lo=0.85, hi=1.25)
    # 骨链单调性：骨架一致时两边会得到同样的结果，所以只有"mod 不单调而 vanilla 单调"
    # 才是 mod 的问题；两边都不单调属于这条动画本身的特征，不该报。
    m_chain, v_chain = mod_m.get("lateral_chain_ok"), van_m.get("lateral_chain_ok")
    if m_chain is False and v_chain is not False:
        flags.append("骨链横向不单调外撇（大腿→小腿→脚踝应逐级增大），"
                     "静止看不出、一动就是猫步")
    out["lateral_chain_ok"] = {"mod": m_chain, "vanilla": v_chain}

    g, gv = mod_m.get("ground_gap_min_cm"), van_m.get("ground_gap_min_cm")
    out["ground_gap_min_cm"] = {"mod": g, "vanilla": gv}
    if g is not None and gv is not None and abs(g - gv) > 3.0:
        flags.append(f"脚底离地比 vanilla 差 {g - gv:+.1f} cm"
                     f"（mod {g:+.1f} / vanilla {gv:+.1f}，>3 cm 说明悬空或陷地）")
    return out, flags


def verdict(flags: list[str], hard: list[str]) -> str:
    if hard:
        return "fail"
    if flags:
        return "warn"
    return "ok"
