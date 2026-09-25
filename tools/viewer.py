# -*- coding: utf-8 -*-
"""X4 角色动画预览器：在游戏外播放 vanilla 动画来检查替换模型的动作。

用法::

    # 看一个 mod 资产（自动带上 vanilla 做左右对比）
    python tools/viewer.py --mod ../work/x4_rose_mod

    # 直接指定资产
    python tools/viewer.py --body <torso.xac> --head <head.xac>

    # 只跑 vanilla，确认工具本身是对的
    python tools/viewer.py --vanilla

    # 无窗口批量出图（CI / 不想开窗口时）
    python tools/viewer.py --mod ... --shot out.png --frames 0,20,40

操作::

    空格      播放 / 暂停
    ← →       上一个 / 下一个动画
    , .       逐帧后退 / 前进
    ↑ ↓       播放速度
    T         切换姿态模式（直接套用 / 相对绑定姿态的增量）
    N         切换显示蒙皮网格 / 只显示骨架（自动蒙皮结果）
    B         切换骨骼线框
    R         重置相机
    鼠标拖动   旋转   滚轮缩放
    ESC / Q   退出

X4 的 NPC 是"换网格、留骨架"：动画挂在共享 component 上，所以这里直接拿
vanilla 的 ``.xsm`` 动画去驱动 mod 的 ``.xac`` 网格——正是游戏里发生的事。
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import animations as anims_mod
import x4game
import xac
import rig as rig_mod
import xsm

try:
    import pyglet
    from pyglet.gl import *
    from pyglet.window import mouse
    HAVE_PYGLET = True
except Exception:  # pragma: no cover - 无 GUI 环境
    HAVE_PYGLET = False


# ---------------------------------------------------------------------------
# 资产定位
# ---------------------------------------------------------------------------

DEFAULT_BODY = "assets/characters/argon/bodies/char_arg_f_jacket_leggings_civ_01.xac"
DEFAULT_HEAD = "assets/characters/argon/heads/char_arg_f_dyn_blend_head.xac"


def _resolve(path: str, game: x4game.GameArchive) -> tuple[str, bytes] | None:
    """既接受磁盘路径，也接受游戏包内路径。"""
    p = Path(path)
    if p.is_file():
        return str(p), p.read_bytes()
    if p.is_dir():
        cands = sorted(p.rglob("*.xac"))
        if cands:
            return str(cands[0]), cands[0].read_bytes()
    raw = game.read(path)
    if raw is not None:
        return path, raw
    if game.has(path + ".xac"):
        return path + ".xac", game.read(path + ".xac")
    return None


def guess_mod_parts(mod_dir: Path) -> tuple[list[str], list[str]]:
    """从 mod 目录里挑出 head / torso 的 .xac（按文件名关键词）。"""
    heads, torsos = [], []
    for p in sorted(mod_dir.rglob("*.xac")):
        low = p.name.lower()
        if "head" in low:
            heads.append(str(p))
        elif any(k in low for k in ("body", "torso", "jacket", "suit", "cloth")):
            torsos.append(str(p))
    return heads, torsos


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------


class Scene:
    """一组资产 + 一个动画，能按时间给出蒙皮后的顶点。"""

    def __init__(self, label: str, sources: list[tuple[str, bytes]], delta: bool = True):
        self.label = label
        self.assets: list[xac.Asset] = []
        self.rigs: list[rig_mod.Rig] = []
        for name, raw in sources:
            asset = xac.load_xac(name, raw)
            if not asset.meshes:
                continue
            self.assets.append(asset)
            self.rigs.append(rig_mod.Rig.build(asset, None, delta=delta))
        if not self.assets:
            raise SystemExit(f"{label}: 没有可用的网格")
        self.delta = delta
        self.anim: xsm.Xsm | None = None
        self.anim_name = "-"
        # 取景：把所有网格顶点合起来算包围盒
        pts = np.concatenate([m.positions for a in self.assets for m in a.meshes])
        self.lo = pts.min(axis=0)
        self.hi = pts.max(axis=0)
        self.center = (self.lo + self.hi) / 2.0
        self.height = float(self.hi[1] - self.lo[1])
        self._cache_t = None
        self._cache = None

    # -- 动画 -------------------------------------------------------------
    def set_anim(self, anim: xsm.Xsm | None, name: str = "-"):
        self.anim = anim
        self.anim_name = name
        self._cache_t = None
        for asset, r in zip(self.assets, self.rigs):
            self.rigs[self.assets.index(asset)] = rig_mod.Rig.build(asset, anim, delta=self.delta)

    @property
    def duration(self) -> float:
        return self.anim.duration if self.anim is not None else 0.0

    def matched(self) -> float:
        vals = [r.matched_fraction() for r in self.rigs]
        return float(np.mean(vals)) if vals else 0.0

    # -- 姿态 -------------------------------------------------------------
    def pose(self, t: float):
        key = round(t, 6)
        if self._cache_t == key and self._cache is not None:
            return self._cache
        parts = []
        for r in self.rigs:
            parts.extend(r.pose_all(t))
        self._cache_t = key
        self._cache = parts
        return parts

    def bone_segments(self, t: float):
        """骨骼线段（世界坐标），用来画骨架。"""
        segs = []
        for r in self.rigs:
            world = r.world_matrices(t)
            parents = r.parents
            for i in range(len(r)):
                p = parents[i]
                if p < 0:
                    continue
                a = world[i][:3, 3]
                b = world[p][:3, 3]
                segs.append((a, b))
        return segs


# ---------------------------------------------------------------------------
# OpenGL 绘制（pyglet 1.5 固定管线；够用且不依赖 shader 编译）
# ---------------------------------------------------------------------------

COLORS = [
    (0.86, 0.83, 0.79),
    (0.76, 0.80, 0.88),
    (0.84, 0.78, 0.84),
    (0.78, 0.86, 0.80),
    (0.88, 0.82, 0.72),
    (0.78, 0.78, 0.78),
]


def _as_gl(arr: np.ndarray, dtype=np.float32):
    """返回 (持有内存的数组, 首地址)。必须保留数组引用，否则地址会失效。"""
    a = np.ascontiguousarray(arr, dtype=dtype)
    return a, a.ctypes.data


class GLScenePainter:
    """把 Scene 的蒙皮结果画到当前 OpenGL 上下文。"""

    def __init__(self, scene: Scene):
        self.scene = scene

    def draw(self, t: float, show_mesh: bool = True, show_bones: bool = False):
        parts = self.scene.pose(t)
        if show_mesh:
            glEnable(GL_LIGHTING)
            glEnable(GL_DEPTH_TEST)
            glEnable(GL_COLOR_MATERIAL)
            glColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
            for i, (mesh, pos, nrm) in enumerate(parts):
                if mesh.faces.size == 0:
                    continue
                c = COLORS[i % len(COLORS)]
                glColor3f(*c)
                p, ptr = _as_gl(pos)
                glEnableClientState(GL_VERTEX_ARRAY)
                glVertexPointer(3, GL_FLOAT, 0, ptr)
                if nrm is not None:
                    n, nptr = _as_gl(nrm)
                    glEnableClientState(GL_NORMAL_ARRAY)
                    glNormalPointer(GL_FLOAT, 0, nptr)
                else:
                    glDisableClientState(GL_NORMAL_ARRAY)
                    glNormal3f(0.0, 1.0, 0.0)
                idx, iptr = _as_gl(mesh.faces.reshape(-1), np.uint32)
                glDrawElements(GL_TRIANGLES, int(idx.size), GL_UNSIGNED_INT, iptr)
                glDisableClientState(GL_VERTEX_ARRAY)
                glDisableClientState(GL_NORMAL_ARRAY)
            glDisable(GL_LIGHTING)
        if show_bones:
            glDisable(GL_LIGHTING)
            glColor3f(0.25, 0.85, 0.45)
            glLineWidth(1.6)
            glBegin(GL_LINES)
            for a, b in self.scene.bone_segments(t):
                glVertex3f(float(a[0]), float(a[1]), float(a[2]))
                glVertex3f(float(b[0]), float(b[1]), float(b[2]))
            glEnd()


def _setup_light():
    glEnable(GL_LIGHT0)
    glEnable(GL_LIGHT1)
    glLightfv(GL_LIGHT0, GL_POSITION, (GLfloat * 4)(0.4, 0.85, 0.6, 0.0))
    glLightfv(GL_LIGHT0, GL_DIFFUSE, (GLfloat * 4)(0.85, 0.85, 0.85, 1.0))
    glLightfv(GL_LIGHT0, GL_AMBIENT, (GLfloat * 4)(0.10, 0.10, 0.11, 1.0))
    glLightfv(GL_LIGHT1, GL_POSITION, (GLfloat * 4)(-0.6, 0.3, -0.7, 0.0))
    glLightfv(GL_LIGHT1, GL_DIFFUSE, (GLfloat * 4)(0.30, 0.32, 0.38, 1.0))
    glLightModelfv(GL_LIGHT_MODEL_AMBIENT, (GLfloat * 4)(0.30, 0.30, 0.33, 1.0))
    glEnable(GL_NORMALIZE)
    glShadeModel(GL_SMOOTH)


def _draw_floor(lo_y: float, extent: float = 400.0):
    glDisable(GL_LIGHTING)
    glColor3f(0.20, 0.21, 0.24)
    glBegin(GL_QUADS)
    glVertex3f(-extent, lo_y, -extent)
    glVertex3f(extent, lo_y, -extent)
    glVertex3f(extent, lo_y, extent)
    glVertex3f(-extent, lo_y, extent)
    glEnd()
    glColor3f(0.30, 0.31, 0.36)
    glBegin(GL_LINES)
    step = 50.0
    n = int(extent // step)
    for i in range(-n, n + 1):
        glVertex3f(i * step, lo_y, -extent)
        glVertex3f(i * step, lo_y, extent)
        glVertex3f(-extent, lo_y, i * step)
        glVertex3f(extent, lo_y, i * step)
    glEnd()


class OrbitCamera:
    def __init__(self, scene: Scene):
        self.scene = scene
        self.azimuth = 200.0
        self.elevation = 6.0
        self.distance = max(scene.height, 60.0) * 2.1
        self.target = scene.center.copy()
        self.fov = 30.0

    def reset(self):
        self.azimuth = 200.0
        self.elevation = 6.0
        self.distance = max(self.scene.height, 60.0) * 2.1
        self.target = self.scene.center.copy()

    def apply(self, width: int, height: int):
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        aspect = max(width, 1) / max(height, 1)
        near = max(self.distance * 0.02, 1.0)
        far = self.distance * 12.0 + 2000.0
        top = near * math.tan(math.radians(self.fov) * 0.5)
        glFrustum(-top * aspect, top * aspect, -top, top, near, far)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        a = math.radians(self.azimuth)
        e = math.radians(self.elevation)
        eye = self.target + np.array(
            [math.sin(a) * math.cos(e), math.sin(e), math.cos(a) * math.cos(e)]
        ) * self.distance
        t = self.target
        from pyglet.gl import gluLookAt

        gluLookAt(eye[0], eye[1], eye[2], t[0], t[1], t[2], 0.0, 1.0, 0.0)


# ---------------------------------------------------------------------------
# 窗口
# ---------------------------------------------------------------------------


def run_viewer(scenes: list[Scene], anim_list: list[anims_mod.AnimRef], game: x4game.GameArchive,
               start: int = 0, args=None, window_shot: str | None = None,
               shot_delay: float = 1.5):
    if not HAVE_PYGLET:
        raise SystemExit("没有可用的 pyglet，无法开窗口。可以改用 --shot 出图模式。")

    painters = [GLScenePainter(s) for s in scenes]
    cameras = [OrbitCamera(s) for s in scenes]
    # 分屏对比时统一取景：否则两边各按自己的包围盒取景，比例对不上
    if len(scenes) > 1:
        lo = np.min(np.stack([s.lo for s in scenes]), axis=0)
        hi = np.max(np.stack([s.hi for s in scenes]), axis=0)
        shared_center = (lo + hi) / 2.0
        shared_height = float(hi[1] - lo[1])
        for cam in cameras:
            cam.target = shared_center.copy()
            cam.distance = max(shared_height, 60.0) * 2.1

    state = {
        "playing": True,
        "t": 0.0,
        "speed": 1.0,
        "index": start,
        "show_mesh": True,
        "show_bones": False,
        "delta": bool(getattr(args, "delta", False)),
    }

    def load_anim(i: int):
        ref = anim_list[i % len(anim_list)]
        raw = game.read(ref.path)
        if raw is None:
            print(f"[warn] 缺少动画 {ref.path}")
            return False
        a = xsm.parse_xsm(raw, ref.path)
        for s in scenes:
            s.set_anim(a, ref.name)
        state["t"] = 0.0
        fname = Path(ref.path).stem
        print(f"[anim] {ref.name}  <- {fname}  ({a.duration:.2f}s, {a.frame_rate():.0f}fps, "
              f"匹配 {scenes[0].matched():.0%})")
        return True

    window = pyglet.window.Window(
        width=args.width if args else 1100,
        height=args.height if args else 760,
        caption="X4 动画预览器",
        resizable=True,
    )
    pyglet.gl.glClearColor(0.13, 0.14, 0.16, 1.0)
    _setup_light()

    hud = pyglet.text.Label(
        "", font_name="Consolas", font_size=10, x=8, y=0,
        color=(225, 228, 235, 255), multiline=True, width=1000,
        anchor_y="top",
    )

    dragging = [False, 0, 0]

    @window.event
    def on_draw():
        window.clear()
        w, h = window.get_size()
        n = len(scenes)
        for i, (p, cam) in enumerate(zip(painters, cameras)):
            vw = w // n
            glViewport(i * vw, 0, vw, h)
            cam.apply(vw, h)
            _draw_floor(float(scenes[i].lo[1]))
            p.draw(state["t"], state["show_mesh"], state["show_bones"])

        glViewport(0, 0, w, h)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0, w, 0, h, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        glDisable(GL_DEPTH_TEST)
        hud.y = h - 6
        ref = anim_list[state["index"] % len(anim_list)]
        lines = [
            f"动画 {state['index']+1}/{len(anim_list)}: {ref.name}   ({Path(ref.path).stem})",
            f"时间 {state['t']:6.2f}s / {scenes[0].duration:5.2f}s   速度 x{state['speed']:.2f}   "
            f"{'播放' if state['playing'] else '暂停'}   模式 {'delta' if state['delta'] else 'direct'}",
        ]
        for s in scenes:
            lines.append(f"[{s.label}] 网格 {len(s.assets)}  顶点 "
                         f"{sum(m.vertex_count for a in s.assets for m in a.meshes)}  "
                         f"动画匹配 {s.matched():.0%}")
        lines.append("空格 播放/暂停  ←→ 换动画  ,. 逐帧  ↑↓ 速度  R 重置相机  "
                     "拖动旋转  滚轮缩放  B 骨骼  N 只看骨架  T 姿态模式  Q 退出")
        hud.text = "\n".join(lines)
        hud.draw()

    @window.event
    def on_resize(w, h):
        glViewport(0, 0, w, h)

    @window.event
    def on_mouse_press(x, y, button, modifiers):
        if button == mouse.LEFT:
            dragging[0] = True
            dragging[1], dragging[2] = x, y

    @window.event
    def on_mouse_release(x, y, button, modifiers):
        dragging[0] = False

    @window.event
    def on_mouse_drag(x, y, dx, dy, buttons, modifiers):
        if not dragging[0]:
            return
        for cam in cameras:
            cam.azimuth += dx * 0.4
            cam.elevation = max(-85.0, min(85.0, cam.elevation + dy * 0.3))

    @window.event
    def on_mouse_scroll(x, y, sx, sy):
        for cam in cameras:
            cam.distance = max(20.0, cam.distance * (0.9 ** sy))

    @window.event
    def on_key_press(symbol, modifiers):
        from pyglet.window import key

        if symbol in (key.ESCAPE, key.Q):
            window.close()
        elif symbol == key.SPACE:
            state["playing"] = not state["playing"]
        elif symbol == key.RIGHT:
            state["index"] = (state["index"] + 1) % len(anim_list)
            load_anim(state["index"])
        elif symbol == key.LEFT:
            state["index"] = (state["index"] - 1) % len(anim_list)
            load_anim(state["index"])
        elif symbol == key.PERIOD:
            state["t"] += 1.0 / 15.0
        elif symbol == key.COMMA:
            state["t"] = max(0.0, state["t"] - 1.0 / 15.0)
        elif symbol == key.UP:
            state["speed"] = min(4.0, state["speed"] * 1.25)
        elif symbol == key.DOWN:
            state["speed"] = max(0.05, state["speed"] / 1.25)
        elif symbol == key.T:
            state["delta"] = not state["delta"]
            for s in scenes:
                s.delta = state["delta"]
                if s.anim is not None:
                    s.set_anim(s.anim, s.anim_name)
        elif symbol == key.B:
            state["show_bones"] = not state["show_bones"]
        elif symbol == key.N:
            state["show_mesh"] = not state["show_mesh"]
        elif symbol == key.R:
            for cam in cameras:
                cam.reset()

    def tick(dt):
        if not state["playing"]:
            return
        dur = scenes[0].duration
        if dur <= 0:
            return
        state["t"] = (state["t"] + dt * state["speed"]) % dur

    load_anim(state["index"])
    pyglet.clock.schedule_interval(tick, 1.0 / 60.0)

    if window_shot:
        def _grab(_dt):
            try:
                buf = pyglet.image.get_buffer_manager().get_color_buffer()
                Path(window_shot).parent.mkdir(parents=True, exist_ok=True)
                buf.save(window_shot)
                print(f"[shot] {window_shot}")
            except Exception as exc:  # pragma: no cover
                print(f"[shot] 失败: {exc}")
            finally:
                window.close()

        pyglet.clock.schedule_once(_grab, shot_delay)

    pyglet.app.run()


def run_shots(scenes: list[Scene], anim_list: list[anims_mod.AnimRef], game: x4game.GameArchive,
              out: str, frames: list[float], args):
    """无窗口批量出图：每个动画渲染指定帧，拼成对比图。"""
    import render as soft

    painters = [GLScenePainter(s) for s in scenes]
    images = []
    anim_indices = args.anims if getattr(args, "anims", None) else [0]
    for ai in anim_indices:
        ref = anim_list[ai % len(anim_list)]
        raw = game.read(ref.path)
        if raw is None:
            continue
        a = xsm.parse_xsm(raw, ref.path)
        for s in scenes:
            s.set_anim(a, ref.name)
        for s, p in zip(scenes, painters):
            cam = soft.Camera(
                target=s.center,
                distance=max(s.height, 60.0) * 2.0,
                azimuth=getattr(args, "azimuth", 200.0),
                elevation=6.0,
                width=args.width // max(len(scenes), 1),
                height=args.height,
            )
            for t in frames:
                t = min(t, max(a.duration - 1e-3, 0.0))
                img = soft.render(s.pose(t), cam, floor_y=float(s.lo[1]))
                img = soft.add_label(img, f"{s.label} | {ref.name} | t={t:.2f}s")
                images.append(img)
    if not images:
        raise SystemExit("没有渲染出任何图片")
    cols = len(frames) * len(scenes)
    sheet = soft.contact_sheet(images, columns=min(cols, 8))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"写出 {out}  {sheet.size}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_scenes(args, game) -> list[Scene]:
    scenes: list[Scene] = []

    mod_sources: list[tuple[str, bytes]] = []
    if args.body or args.head:
        for p in (args.head or []):
            r = _resolve(p, game)
            if r:
                mod_sources.append(r)
        for p in (args.body or []):
            r = _resolve(p, game)
            if r:
                mod_sources.append(r)
    elif args.mod:
        mod_dir = Path(args.mod)
        if not mod_dir.exists():
            raise SystemExit(f"mod 目录不存在: {mod_dir}")
        heads, torsos = guess_mod_parts(mod_dir)
        for p in (heads[:1] + torsos[:1]):
            mod_sources.append((p, Path(p).read_bytes()))
        if not mod_sources:
            raise SystemExit(f"{mod_dir} 下没找到 head/torso 的 .xac")

    if mod_sources:
        scenes.append(Scene("mod", mod_sources, delta=args.delta))
    if args.vanilla or mod_sources:
        van = []
        for p in ([args.vanilla_head] if args.vanilla_head else [DEFAULT_HEAD]):
            r = _resolve(p, game)
            if r:
                van.append(r)
        for p in ([args.vanilla_body] if args.vanilla_body else [DEFAULT_BODY]):
            r = _resolve(p, game)
            if r:
                van.append(r)
        if van:
            scenes.append(Scene("vanilla", van, delta=args.delta))
    if not scenes:
        raise SystemExit("没有加载到任何资产，用 --mod / --body / --vanilla 指定")
    return scenes


def main(argv=None):
    ap = argparse.ArgumentParser(description="X4 角色动画预览器")
    ap.add_argument("--mod", help="mod 目录（自动找 head/torso 的 .xac）")
    ap.add_argument("--body", nargs="*", help="躯干 .xac（磁盘路径或游戏包内路径）")
    ap.add_argument("--head", nargs="*", help="头部 .xac")
    ap.add_argument("--vanilla", action="store_true", help="只加载 vanilla 做基准")
    ap.add_argument("--vanilla-body", default=None)
    ap.add_argument("--vanilla-head", default=None)
    ap.add_argument("--component", default="character_argon_female_01",
                    help="取哪套动画（见 tools/animations.py）")
    ap.add_argument("--anim", default=None, help="只播某条动画（逻辑名或资产名）")
    ap.add_argument("--anims", nargs="*", type=int, help="出图模式下播哪几条（序号）")
    ap.add_argument("--delta", action="store_true",
                    help="实验性：用相对 .xsm 绑定姿态的增量（默认关闭，实测会让不同资产姿态不一致）")
    ap.add_argument("--shot", help="不开窗口，用软光栅渲染成 PNG")
    ap.add_argument("--window-shot", help="开窗口跑几秒后截图再退出（验证 OpenGL 路径）")
    ap.add_argument("--frames", default="0,15,30,45", help="出图模式的帧号（15fps 计）")
    ap.add_argument("--azimuth", type=float, default=200.0)
    ap.add_argument("--width", type=int, default=1100)
    ap.add_argument("--height", type=int, default=760)
    ap.add_argument("--start", type=int, default=0, help="从第几条动画开始")
    args = ap.parse_args(argv)

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
        run_shots(scenes, anim_list, game, args.shot, frames, args)
    else:
        run_viewer(scenes, anim_list, game, start=args.start, args=args,
                   window_shot=args.window_shot)


if __name__ == "__main__":
    main()
