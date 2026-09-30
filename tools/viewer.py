# -*- coding: utf-8 -*-
"""X4 角色动画预览器 · 命令行版。

图形界面版见 `studio.py`（推荐日常使用，可在窗口里选 mod 和动画）；
这个入口适合脚本化、批处理，或者只想快速看一眼。

用法::

    python tools/viewer.py --mod <mod目录>          # 与 vanilla 并排播放
    python tools/viewer.py --vanilla                # 只看 vanilla 基准
    python tools/viewer.py --body a.xac --head b.xac
    python tools/viewer.py --mod <dir> --all-variants        # mod 里每套模型各占一格
    python tools/viewer.py --mod <dir> --variant yue_b       # 只看其中一套
    python tools/viewer.py --mod <dir> --list-variants       # 列出这个 mod 有几套模型
    python tools/viewer.py --mod <dir> --anim anim_stand_idle_05 \
        --shot out.png --frames 0,15,30,45          # 不开窗口，直接出对比图

窗口快捷键：空格 播放/暂停 · ←→ 换动画 · , . 逐帧 · ↑↓ 速度 · T 姿态模式 ·
B 骨骼 · N 网格 · R 重置相机 · 鼠标拖动旋转 · 滚轮缩放 · Q 退出。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import console  # noqa: F401  (设置 UTF-8 控制台)
import animations as anims_mod
import glview
import scene as scene_mod
import x4game
import xsm


def mod_textures(mod_dir: Path, args):
    rules = scene_mod.MaterialRules([mod_dir])
    tex = None
    if not args.no_textures:
        ts = scene_mod.TextureSet([mod_dir])
        tex = ts if len(ts) else None
    return tex, rules


def add_mod_scenes(scenes: list, mod_dir: Path, args, label_prefix: str = "",
                   only_variant: str | None = None):
    """把一个 mod 目录加进场景。里面有**多套模型**时默认只加第一套。

    ``--all-variants`` 会把每套各加一格（并排看两套装扮的差别），
    ``--variant`` 则指名一套。返回实际加进去的套数。
    """
    variants = scene_mod.mod_variants(mod_dir)
    tex, rules = mod_textures(mod_dir, args)
    if not variants:                     # 认不出槽位：退回旧行为（前两件）
        src = scene_mod.load_mod_sources(mod_dir)
        if src:
            scenes.append(scene_mod.Scene(label_prefix or "mod", src, delta=args.delta,
                                          textures=tex, material_rules=rules))
            return 1
        return 0

    if len(variants) > 1:
        print(f"[mod] {mod_dir.name} 里有 {len(variants)} 套模型：" +
              "、".join(v.name() for v in variants))
    picked = variants
    if only_variant:
        hit = [v for v in variants if only_variant in (v.key, v.label, v.name())]
        if not hit:
            raise SystemExit(f"没有叫 {only_variant!r} 的模型套；可选："
                             + "、".join(v.name() for v in variants))
        picked = hit
    elif not args.all_variants:
        picked = variants[:1]
        if len(variants) > 1:
            print(f"[mod] 默认只加载第一套（{variants[0].name()}）；"
                  f"看别的套用 --variant <名字> 或 --all-variants")

    n = 0
    for v in picked:
        src = scene_mod.load_mod_sources(mod_dir, variant=v.key)
        if not src:
            continue
        scenes.append(scene_mod.Scene(f"{label_prefix}{v.name()}", src, delta=args.delta,
                                      textures=tex, material_rules=rules))
        n += 1
    return n


def build_scenes(args, game) -> list[scene_mod.Scene]:
    scenes: list[scene_mod.Scene] = []
    mod_sources: list[tuple[str, bytes]] = []

    if args.body or args.head:
        for p in list(args.head or []) + list(args.body or []):
            got = scene_mod.resolve_asset(p, game)
            if got:
                mod_sources.append(got)
            else:
                print(f"[warn] 找不到资产 {p}")
        if mod_sources:
            scenes.append(scene_mod.Scene("mod", mod_sources, delta=args.delta))
    elif args.mod:
        mod_dir = Path(args.mod)
        if not mod_dir.exists():
            raise SystemExit(f"mod 目录不存在: {mod_dir}")
        if not add_mod_scenes(scenes, mod_dir, args, only_variant=args.variant):
            raise SystemExit(f"{mod_dir} 下没找到 .xac")

    if args.compare_mod:
        # 与另一个 mod 对比：迭代时看"上一版 vs 这一版"比看 vanilla 更有用
        other = Path(args.compare_mod)
        if not other.is_dir():
            raise SystemExit(f"对比 mod 目录不存在: {other}")
        add_mod_scenes(scenes, other, args, label_prefix=f"{other.name}·")
    elif args.vanilla or scenes:
        van = []
        for ref in (args.vanilla_head or scene_mod.DEFAULT_HEAD,
                    args.vanilla_body or scene_mod.DEFAULT_BODY):
            got = scene_mod.resolve_asset(ref, game)
            if got:
                van.append(got)
        if van:
            scenes.append(scene_mod.Scene("vanilla", van, delta=args.delta))
    if not scenes:
        raise SystemExit("没有加载到任何资产，用 --mod / --body / --vanilla 指定")
    return scenes


def run_window(scenes, anim_list, game, args) -> int:
    state = glview.ViewState()
    idx = {"i": args.start % max(len(anim_list), 1)}
    delta = {"on": args.delta}

    def load(i: int) -> bool:
        ref = anim_list[i % len(anim_list)]
        raw = game.read(ref.path)
        if raw is None:
            print(f"[warn] 缺少动画 {ref.path}")
            return False
        anim = xsm.parse_xsm(raw, ref.path)
        for s in scenes:
            s.set_anim(anim, ref.name)
        state.time = 0.0
        print(f"[anim] {ref.name}  <- {Path(ref.path).stem}  ({anim.duration:.2f}s, "
              f"{anim.frame_rate():.0f}fps, 匹配 {scenes[0].matched():.0%})")
        return True

    def hud():
        ref = anim_list[idx["i"] % len(anim_list)]
        dur = scenes[0].duration
        lines = [
            f"动画 {idx['i']+1}/{len(anim_list)}: {ref.name}   ({Path(ref.path).stem})",
            f"时间 {state.time:6.2f}s / {dur:5.2f}s   速度 x{state.speed:.2f}   "
            f"{'播放' if state.playing else '暂停'}   模式 "
            f"{'delta(实验)' if delta['on'] else 'direct'}",
        ]
        for s in scenes:
            lines.append(f"[{s.label}] {s.info()}")
        lines.append("空格 播放/暂停  ←→ 换动画  ,. 逐帧  ↑↓ 速度  R 重置相机  "
                     "拖动旋转  滚轮缩放  B 骨骼  N 网格  T 姿态模式  Q 退出")
        return lines

    def on_key(symbol) -> bool:
        from pyglet.window import key

        if symbol == key.RIGHT:
            idx["i"] = (idx["i"] + 1) % len(anim_list)
            load(idx["i"])
            return True
        if symbol == key.LEFT:
            idx["i"] = (idx["i"] - 1) % len(anim_list)
            load(idx["i"])
            return True
        if symbol == key.UP:
            state.speed = min(4.0, state.speed * 1.25)
            return True
        if symbol == key.DOWN:
            state.speed = max(0.05, state.speed / 1.25)
            return True
        if symbol == key.T:
            delta["on"] = not delta["on"]
            for s in scenes:
                s.delta = delta["on"]
                if s.anim is not None:
                    s.set_anim(s.anim, s.anim_name)
            return True
        return False

    preview = glview.PreviewWindow(
        scenes, state, width=args.width, height=args.height,
        hud_lines=hud, on_key=on_key,
    )
    if not load(idx["i"]):
        preview.close()
        return 1

    target_dt = 1.0 / 60.0
    frames = 0
    while not preview.closed:
        t0 = time.perf_counter()
        if not preview.pump():
            break
        frames += 1
        if args.window_shot and frames >= 120:
            preview.screenshot(args.window_shot)
            print(f"[shot] {args.window_shot}")
            break
        rest = target_dt - (time.perf_counter() - t0)
        if rest > 0:
            time.sleep(rest)
    preview.close()
    return 0


def run_shots(scenes, anim_list, game, out: str, frames: list[float], args) -> int:
    import render as soft

    images = []
    for ai in (args.anims or [0]):
        ref = anim_list[ai % len(anim_list)]
        raw = game.read(ref.path)
        if raw is None:
            continue
        anim = xsm.parse_xsm(raw, ref.path)
        for s in scenes:
            s.set_anim(anim, ref.name)
        for s in scenes:
            w = max(args.width, 64)      # --width 是**每格**宽度，不是整张图的总宽
            tex = None if args.no_textures else s.part_textures()
            angles = ([args.azimuth] if args.views <= 0
                      else [360.0 * k / args.views for k in range(args.views)])
            for az in angles:
                cam = soft.Camera(
                    target=s.center, distance=max(s.height, 60.0) * 2.0,
                    azimuth=az, elevation=args.elevation, width=w, height=args.height,
                )
                ts = frames if args.views <= 0 else [max(anim.duration, 1e-6) * 0.5]
                for t in ts:
                    t = min(t, max(anim.duration - 1e-3, 0.0))
                    img = soft.render(
                        s.pose(t), cam, floor_y=0.0, textures=tex,
                        bones=None if args.no_bones else s.bone_segments(t),
                    )
                    tag = f"{s.label} | {ref.name} | t={t:.2f}s"
                    if args.views > 0:
                        tag += f" | az={az:.0f}°"
                    images.append(soft.add_label(img, tag))
    if not images:
        raise SystemExit("没有渲染出任何图片")
    # 一行排开最清楚；格数太多时才折行，且尽量折成整齐的矩形
    n = len(images)
    if n <= 12:
        cols = n
    else:
        cols = 6 if n % 6 == 0 else 8
    sheet = soft.contact_sheet(images, columns=cols)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"写出 {out}  {sheet.size}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="X4 角色动画预览器（命令行版）")
    ap.add_argument("--mod", help="mod 目录（自动找 head/torso 的 .xac）")
    ap.add_argument("--variant", help="只用 mod 里的某一套模型（名字见 --list-variants）")
    ap.add_argument("--all-variants", action="store_true",
                    help="mod 里每套模型各占一格并排显示（默认只用第一套）")
    ap.add_argument("--list-variants", action="store_true",
                    help="列出 mod 目录里的模型套，然后退出（不开窗口）")
    ap.add_argument("--compare-mod", help="与另一个 mod 并排对比（不给则对比 vanilla）")
    ap.add_argument("--body", nargs="*", help="躯干 .xac（磁盘路径或游戏包内路径）")
    ap.add_argument("--head", nargs="*", help="头部 .xac")
    ap.add_argument("--vanilla", action="store_true", help="只加载 vanilla 做基准")
    ap.add_argument("--vanilla-body", default=None)
    ap.add_argument("--vanilla-head", default=None)
    ap.add_argument("--component", default="character_argon_female_01",
                    help="取哪套动画（python tools/animations.py 可列出全部）")
    ap.add_argument("--anim", default=None, help="只播某条动画（逻辑名或资产名）")
    ap.add_argument("--anims", nargs="*", type=int, help="出图模式下播哪几条（序号）")
    ap.add_argument("--delta", action="store_true",
                    help="实验性：相对 .xsm 绑定姿态的增量（实测会让不同资产姿态不一致）")
    ap.add_argument("--shot", help="不开窗口，用软光栅渲染成 PNG")
    ap.add_argument("--window-shot", help="开窗口跑约 2 秒后截图再退出（验证 OpenGL 路径）")
    ap.add_argument("--frames", default="0,15,30,45", help="出图模式的帧号（15fps 计）")
    ap.add_argument("--azimuth", type=float, default=200.0)
    ap.add_argument("--elevation", type=float, default=6.0,
                    help="仰角（度）。负值从下往上看，查腋下/裙摆内侧时有用")
    ap.add_argument("--views", type=int, default=0,
                    help="环绕 N 个角度出图（0 = 只用 --azimuth 一个角度）")
    ap.add_argument("--width", type=int, default=1100,
                    help="窗口宽度；出图模式下是**每格**宽度")
    ap.add_argument("--height", type=int, default=760)
    ap.add_argument("--start", type=int, default=0, help="从第几条动画开始")
    ap.add_argument("--no-textures", action="store_true",
                    help="出图时不给 mod 侧上贴图（默认上，仅当 --mod 是磁盘目录）")
    ap.add_argument("--no-bones", action="store_true", help="出图时不叠加绿色骨骼线")
    args = ap.parse_args(argv)

    if args.list_variants:
        if not args.mod:
            raise SystemExit("--list-variants 要配 --mod <目录>")
        variants = scene_mod.mod_variants(Path(args.mod))
        print(f"{args.mod}: {len(variants)} 套模型")
        for v in variants:
            print(f"  {v.display():<34} key={v.key}")
            for p in v.files:
                print(f"      {p}")
        return 0

    game = x4game.GameArchive()
    scenes = build_scenes(args, game)

    anim_list = anims_mod.component_animations(args.component, game)
    if not anim_list:
        anim_list = anims_mod.suggest(game)
    if args.anim:
        hit = [a for a in anim_list if args.anim in (a.name, Path(a.path).stem)]
        if not hit:
            stem = args.anim[:-4] if args.anim.endswith(".xsm") else args.anim
            if "/" not in stem:
                stem = f"assets/characters/animations/{stem}.xsm"
            hit = [anims_mod.AnimRef(Path(stem).stem, stem)]
        anim_list = hit
    if not anim_list:
        raise SystemExit("没有可用动画")
    print(f"动画清单 {len(anim_list)} 条（component={args.component}）")

    if args.shot:
        frames = [float(x) / 15.0 for x in args.frames.split(",") if x.strip()]
        return run_shots(scenes, anim_list, game, args.shot, frames, args)
    return run_window(scenes, anim_list, game, args)


if __name__ == "__main__":
    sys.exit(main())
