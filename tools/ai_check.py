# -*- coding: utf-8 -*-
"""给 AI（或 CI）用的成果检查入口：一条命令产出「对比图 + 结构化判据」。

人和 AI 看的东西不一样：GUI 是给人看的，AI 看不见窗口；只丢一张图，
AI 还得自己猜哪里有问题。这个脚本把"这版 mod 的动作对不对"翻译成
**可以比较的数字**（全部相对 vanilla 基线），再附上对比图。

用法::

    python tools/ai_check.py --mod <mod目录> --out report/
    python tools/ai_check.py --mod <mod目录> --out report/ --anims 5 --samples 8

产物::

    report/report.json          全部指标、比值、告警、结论（AI 读这个）
    report/summary.txt          同样内容的人类可读版
    report/shots/<动画>.png     每个动画一张：左 mod 三帧 / 右 vanilla 三帧
    report/shots/index.json     图片清单与每格的含义

退出码：0 = ok，1 = warn（有告警），2 = fail（骨架不一致等硬伤）。

设计约定：

* **一切与 vanilla 比**——绝对值没有意义（实测"只推到 vanilla 的 77% 仍然
  看得出猫步"，所以判据必须是比值）。
* 每个动画**采样若干帧**再聚合，避免只看第一帧（第一帧常常接近绑定姿态，
  什么问题都看不出来）。
* 输出里凡是能判定的都带 ``status``，AI 不必自己发明阈值。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import console  # noqa: F401  (设置 UTF-8 控制台)
import animations as anims_mod
import metrics as M
import render as soft
import scene as scene_mod
import skeleton_check as skel
import x4game
import xac
import xsm

#: 优先挑这些动画做检查——覆盖站立 / 坐姿 / 大臂展 / 负重，姿态差异最大
PREFERRED = [
    ("idle", ["anim_stand_idle_05", "anim_stand_idle_01", "anim_stand_conversation_01"]),
    ("sit", ["anim_sit_idle_01", "tran_stand_sit_01", "tran_sit_stand_01"]),
    ("tool", ["anim_2htooluse_01", "anim_1htooluse_01", "anim_standterminal_busyfront_01"]),
    ("carry", ["anim_carry_01", "anim_stand_carry_01", "anim_a-a_ar_carry_01"]),
    ("talk", ["anim_stand_conversation_01", "anim_standcaptain_01"]),
]


def pick_animations(refs: list[anims_mod.AnimRef], n: int) -> list[anims_mod.AnimRef]:
    """按类别挑 n 条代表性动画，尽量覆盖不同姿态。"""
    by_name = {a.name: a for a in refs}
    chosen: list[anims_mod.AnimRef] = []
    for _label, names in PREFERRED:
        for nm in names:
            if nm in by_name and by_name[nm] not in chosen:
                chosen.append(by_name[nm])
                break
        if len(chosen) >= n:
            break
    for a in refs:
        if len(chosen) >= n:
            break
        if a not in chosen:
            chosen.append(a)
    return chosen[:n]


def static_checks(mod_assets: dict, game) -> tuple[dict, list[str], list[str]]:
    """骨架一致性 + 顶点预算 + 法线 + 绕序。返回 (结果, 告警, 硬伤)。"""
    flags: list[str] = []
    hard: list[str] = []
    out: dict = {"skeleton": {}, "budget": {}, "normals": {}, "winding": {}}

    for slot, asset in mod_assets.items():
        ref_path = skel.SLOT_REFERENCE.get(slot)
        if not ref_path:
            continue
        raw = game.read(ref_path)
        if raw is None:
            continue
        res = skel.compare(xac.load_xac(ref_path, raw), asset, slot)
        out["skeleton"][slot] = res
        if not res.get("ok"):
            hard.append(f"{slot} 骨架与 vanilla 不一致"
                        + (f"（位置最大偏差 {res['max_pos_diff_cm']} cm）"
                           if res.get("max_pos_diff_cm") else "（同名骨缺失）"))

        ref_static = M.mesh_static(xac.load_xac(ref_path, raw))
        mod_static = M.mesh_static(asset)
        r = M.ratio(mod_static["verts"], ref_static["verts"])
        out["budget"][slot] = {"mod_verts": mod_static["verts"], "vanilla_verts": ref_static["verts"],
                               "ratio": r, "tris": mod_static["tris"]}
        if r is not None:
            if r > 15:
                hard.append(f"{slot} 顶点数是 vanilla 的 {r:.1f}×（>15× 实测会让空间站频闪）")
            elif r > 6:
                flags.append(f"{slot} 顶点数是 vanilla 的 {r:.1f}×（>6× 起要留意频闪）")

        out["normals"][slot] = {"mod": mod_static["normal_flatness"],
                                "vanilla": ref_static["normal_flatness"],
                                "note": "mean|dot(面法线,顶点法线)|，接近 1 = 平面着色"}
        mf, vf = mod_static["normal_flatness"], ref_static["normal_flatness"]
        if mf is not None and vf is not None and mf > 0.9 and vf < 0.8:
            flags.append(f"{slot} 法线几乎全平面（mean|dot|={mf:.2f}，vanilla {vf:.2f}）"
                         f"——多半是导出时没开平滑着色，实机是满脸面片")

        out["winding"][slot] = {"mod": mod_static["flipped_face_ratio"],
                                "vanilla": ref_static["flipped_face_ratio"]}
        mw, vw = mod_static["flipped_face_ratio"], ref_static["flipped_face_ratio"]
        if mw is not None and mw > 0.1:
            hard.append(f"{slot} 有 {mw:.0%} 的面法线与顶点法线反向"
                        f"（vanilla {(vw or 0):.1%}）——绕序翻转，"
                        f"实机表现为脸消失/衣服透明/看到内壳")
    return out, flags, hard


def load_mod_assets(mod_dir: Path) -> dict:
    """按槽位加载 mod 资产。"""
    out: dict = {}
    for path, slot in scene_mod.mod_asset_options(mod_dir):
        if slot in ("head", "torso") and slot not in out:
            out[slot] = (path.name, path.read_bytes())
    if not out:  # 认不出槽位就退化成前两个
        opts = scene_mod.mod_asset_options(mod_dir)[:2]
        for path, _slot in opts:
            out[path.stem] = (path.name, path.read_bytes())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="X4 mod 动作检查（AI/CI 用）")
    ap.add_argument("--mod", required=True, help="mod 目录（含 head/torso 的 .xac）")
    ap.add_argument("--out", default="report", help="输出目录")
    ap.add_argument("--anims", type=int, default=3, help="检查几条代表性动画")
    ap.add_argument("--samples", type=int, default=6, help="每条动画采样多少帧")
    ap.add_argument("--shots", type=int, default=3, help="每条动画出几格对比图")
    ap.add_argument("--component", default="character_argon_female_01")
    ap.add_argument("--no-textures", action="store_true",
                    help="不给 mod 侧上贴图（默认上；vanilla 侧始终是纯色）")
    ap.add_argument("--no-bones", action="store_true", help="不叠加绿色骨骼线")
    ap.add_argument("--width", type=int, default=330)
    ap.add_argument("--height", type=int, default=540)
    ap.add_argument("--azimuth", type=float, default=200.0,
                    help="时间序列那张图用的方位角（度）")
    ap.add_argument("--elevation", type=float, default=6.0,
                    help="仰角（度）。负值从下往上看，查腋下/裙摆内侧时有用")
    ap.add_argument("--views", type=int, default=4,
                    help="额外输出一张环绕图，绕 N 个等分角度（0 = 关闭）。"
                         "单角度会漏掉背面、侧面、腋下这类问题")
    args = ap.parse_args(argv)

    t_start = time.time()
    game = x4game.GameArchive()
    mod_dir = Path(args.mod).resolve()
    if not mod_dir.is_dir():
        print(f"mod 目录不存在: {mod_dir}")
        return 2

    out_dir = Path(args.out)
    shots_dir = out_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)

    # -- 1. 资产 ----------------------------------------------------------
    mod_sources = load_mod_assets(mod_dir)
    if not mod_sources:
        print(f"{mod_dir} 下没找到 .xac")
        return 2
    print("mod 资产: " + ", ".join(f"{k}={v[0]}" for k, v in mod_sources.items()))

    # 贴图只给 mod 侧（vanilla 只是姿态基准，上贴图没意义还慢一倍）
    texset = None
    rules = scene_mod.MaterialRules([mod_dir])
    if not args.no_textures:
        ts = scene_mod.TextureSet([mod_dir], warn=print)
        texset = ts if len(ts) else None
    mod_scene = scene_mod.Scene("mod", list(mod_sources.values()), textures=texset,
                                material_rules=rules)
    if texset is not None:
        hit, total = mod_scene.texture_stats()
        print(f"贴图: {len(texset)} 张可用，{hit}/{total} 个网格有贴图")
    # 用"实际加载成功的那几个"配对槽位：Scene 会跳过没有网格的资产，
    # 直接按 mod_sources 的下标去索引 assets 会错位
    kept = {name for name, _ in mod_scene.kept_sources}
    mod_assets = {slot: asset for slot, (name, _raw), asset
                  in zip(mod_sources, mod_scene.kept_sources, mod_scene.assets)
                  if name in kept}
    van_sources = []
    for ref in (scene_mod.DEFAULT_HEAD, scene_mod.DEFAULT_BODY):
        got = scene_mod.resolve_asset(ref, game)
        if got:
            van_sources.append(got)
    van_scene = scene_mod.Scene("vanilla", van_sources)

    report: dict = {
        "tool": "x4-anim-preview/ai_check",
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mod_dir": str(mod_dir),
        "mod_assets": {k: v[0] for k, v in mod_sources.items()},
        "elapsed_s": None,
    }

    # -- 2. 静态检查 ------------------------------------------------------
    print("\n[1/3] 静态检查（骨架 / 顶点预算 / 法线 / 绕序）")
    static, flags, hard = static_checks(mod_assets, game)
    report["static"] = static
    report["flags"] = list(flags)
    report["hard_failures"] = list(hard)
    for slot, res in static["skeleton"].items():
        print(f"  骨架[{slot}]: {'一致' if res.get('ok') else '不一致'}  "
              f"共同骨 {res.get('shared_bones')}/{res.get('reference_bones')}  "
              f"位置偏差 {res.get('max_pos_diff_cm')} cm")
    for slot, b in static["budget"].items():
        print(f"  顶点[{slot}]: {b['mod_verts']} vs vanilla {b['vanilla_verts']} "
              f"= {b['ratio']}×")

    # -- 3. 动画检查 ------------------------------------------------------
    refs = anims_mod.component_animations(args.component, game)
    if not refs:
        refs = anims_mod.suggest(game)
    picked = pick_animations(refs, args.anims)
    print(f"\n[2/3] 动画检查（{len(picked)} 条 × {args.samples} 帧采样）")

    anim_reports = []
    shot_index = []
    for ref in picked:
        raw = game.read(ref.path)
        if raw is None:
            continue
        anim = xsm.parse_xsm(raw, ref.path)
        for s in (mod_scene, van_scene):
            s.set_anim(anim, ref.name)

        mod_m = M.sample_animation(mod_scene, anim, args.samples)
        van_m = M.sample_animation(van_scene, anim, args.samples)
        cmp_metrics, aflags = M.compare(mod_m, van_m)
        entry = {
            "name": ref.name,
            "asset": Path(ref.path).stem,
            "path": ref.path,
            "duration_s": round(anim.duration, 3),
            "frame_rate": anim.frame_rate(),
            "matched": round(mod_scene.matched(), 4),
            "metrics": cmp_metrics,
            "flags": aflags,
        }
        if mod_scene.matched() < 0.5:
            entry["flags"].append(
                f"只有 {mod_scene.matched():.0%} 的动画节点能对上骨架——动画可能驱动不了这个资产")
        anim_reports.append(entry)
        flags.extend(f"[{ref.name}] {f}" for f in aflags)

        print(f"  {ref.name:<34} 撕裂 {mod_m['joint_tear_cm']} vs {van_m['joint_tear_cm']} cm"
              f" | 两脚间距 {mod_m['feet_gap_cm']} vs {van_m['feet_gap_cm']} cm"
              f"{'  ⚠ ' + str(len(aflags)) + ' 条告警' if aflags else ''}")

        # -- 出图 ---------------------------------------------------------
        dur = max(anim.duration, 1e-6)
        times = [dur * (i + 0.5) / max(args.shots, 1) for i in range(args.shots)]

        def shoot(t, azimuth, elevation):
            out = []
            for s in (mod_scene, van_scene):
                cam = soft.Camera(target=s.center, distance=max(s.height, 60.0) * 2.0,
                                  azimuth=azimuth, elevation=elevation,
                                  width=args.width, height=args.height)
                # 地板固定在世界 Y=0（和指标口径一致）：用各场景自己的最低点会让
                # 两边地板不在同一高度，"谁陷进地板"就看不出来了
                out.append(soft.render(
                    s.pose(t), cam, floor_y=0.0,
                    textures=None if args.no_textures else s.part_textures(),
                    bones=None if args.no_bones else s.bone_segments(t),
                ))
            return out

        cells = []
        for t in times:
            imgs = shoot(t, args.azimuth, args.elevation)
            for s, img in zip((mod_scene, van_scene), imgs):
                cells.append(soft.add_label(img, f"{s.label} | t={t:.2f}s"))
        # 交错排列：mod0 vanilla0 mod1 vanilla1 ...
        sheet = soft.contact_sheet(cells, columns=len(cells))
        shot_path = shots_dir / f"{ref.name}.png"
        sheet.save(shot_path)
        shot_index.append({
            "file": f"shots/{ref.name}.png",
            "animation": ref.name,
            "view": f"azimuth={args.azimuth:g} elevation={args.elevation:g}",
            "layout": "从左到右依次为 mod(t0) vanilla(t0) mod(t1) vanilla(t1) ...",
            "frames_s": [round(t, 3) for t in times],
        })

        if args.views > 0:
            # 环绕一圈，同一时刻。单看一个角度很容易漏掉背面/侧面的问题
            t_mid = dur * 0.5
            ring = []
            angles = [360.0 * k / args.views for k in range(args.views)]
            for az in angles:
                for s, img in zip((mod_scene, van_scene),
                                  shoot(t_mid, az, args.elevation)):
                    ring.append(soft.add_label(img, f"{s.label} | az={az:.0f}°"))
            ring_path = shots_dir / f"{ref.name}_views.png"
            soft.contact_sheet(ring, columns=len(ring)).save(ring_path)
            shot_index.append({
                "file": f"shots/{ref.name}_views.png",
                "animation": ref.name,
                "view": f"环绕 {args.views} 个角度，elevation={args.elevation:g}",
                "layout": "每个角度两张：先 mod 后 vanilla，角度依次为 "
                          + ", ".join(f"{a:.0f}°" for a in angles),
                "frames_s": [round(t_mid, 3)],
            })

    report["animations"] = anim_reports
    report["shots"] = shot_index
    status = M.verdict(flags, hard)
    report["status"] = status
    report["elapsed_s"] = round(time.time() - t_start, 1)
    report["how_to_read"] = (
        "status: ok/warn/fail。hard_failures 是必须修的硬伤（骨架不一致、绕序翻转、"
        "顶点爆炸）。flags 是疑似问题，每条都给了 mod/vanilla 的比值。"
        "animations[].metrics 里 ratio = mod/vanilla，越接近 1 越好。"
        "shots/ 里每张图从左到右是 mod、vanilla 交替的同一时刻，可直接目视对比。"
    )

    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- 4. 人类可读摘要 --------------------------------------------------
    lines = [f"X4 mod 动作检查  {report['generated']}",
             f"mod: {mod_dir}", f"结论: {status.upper()}", ""]
    if hard:
        lines.append("硬伤（必须修）:")
        lines += [f"  ✗ {h}" for h in hard]
        lines.append("")
    if flags:
        lines.append("告警:")
        lines += [f"  ! {f}" for f in flags]
        lines.append("")
    if not hard and not flags:
        lines.append("没有发现问题：骨架一致、指标与 vanilla 相当。")
        lines.append("")
    lines.append("各动画指标（mod vs vanilla）:")
    for a in anim_reports:
        lines.append(f"  {a['name']}")
        for key, v in a["metrics"].items():
            if isinstance(v, dict) and v.get("ratio") is not None:
                lines.append(f"    {key:<18} {v['mod']} vs {v['vanilla']}  ({v['ratio']}×)")
    lines.append("")
    lines.append("对比图: " + ", ".join(s["file"] for s in shot_index))
    (out_dir / "summary.txt").write_text("\n".join(lines), encoding="utf-8")

    print(f"\n[3/3] 结论: {status.upper()}")
    for h in hard:
        print(f"  ✗ {h}")
    for f in flags:
        print(f"  ! {f}")
    print(f"\n写出 {out_dir/'report.json'}  {out_dir/'summary.txt'}  "
          f"{len(shot_index)} 张对比图  ({report['elapsed_s']}s)")
    return {"ok": 0, "warn": 1, "fail": 2}[status]


if __name__ == "__main__":
    sys.exit(main())
