# -*- coding: utf-8 -*-
"""OpenGL 预览视图层：轨道相机、网格/骨架绘制、可手动步进的预览窗口。

被 `viewer.py`（命令行模式）和 `studio.py`（图形界面）共用。

窗口用 :meth:`PreviewWindow.pump` **手动步进**，因此可以挂在别的主循环
（tkinter 的 ``after``）下面，不需要 ``pyglet.app.run()``。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pyglet
from pyglet.gl import *
from pyglet.window import key, mouse

#: 不同网格的默认底色（按网格序号轮换）
COLORS = [
    (0.86, 0.83, 0.79),
    (0.76, 0.80, 0.88),
    (0.84, 0.78, 0.84),
    (0.78, 0.86, 0.80),
    (0.88, 0.82, 0.72),
    (0.78, 0.78, 0.78),
]


def as_gl(arr: np.ndarray, dtype=np.float32):
    """返回 ``(持有内存的数组, 首地址)``。

    必须保留数组引用直到绘制调用结束，否则地址失效会直接段错误。
    """
    a = np.ascontiguousarray(arr, dtype=dtype)
    return a, a.ctypes.data


# ---------------------------------------------------------------------------
# 视图状态
# ---------------------------------------------------------------------------


@dataclass
class ViewState:
    """UI 与渲染层共享的可变状态（两边都在主线程，无需加锁）。"""

    time: float = 0.0
    playing: bool = True
    speed: float = 1.0
    show_mesh: bool = True
    show_bones: bool = False
    loop: bool = True

    def advance(self, dt: float, duration: float):
        if not self.playing or duration <= 0:
            return
        t = self.time + dt * self.speed
        if t > duration:
            t = (t % duration) if self.loop else duration
        self.time = max(0.0, t)


# ---------------------------------------------------------------------------
# 相机
# ---------------------------------------------------------------------------


class OrbitCamera:
    """绕目标点的轨道相机（X4 是 Y 轴向上）。"""

    def __init__(self, target, distance: float, azimuth: float = 200.0, elevation: float = 6.0):
        self.target = np.asarray(target, dtype=np.float64).copy()
        self.home_target = self.target.copy()
        self.distance = float(distance)
        self.home_distance = float(distance)
        self.azimuth = azimuth
        self.elevation = elevation
        self.home_azimuth = azimuth
        self.home_elevation = elevation
        self.fov = 30.0

    @classmethod
    def for_scenes(cls, scenes):
        """所有场景共用一个取景，分屏对比时比例才可比。"""
        lo = np.min(np.stack([s.lo for s in scenes]), axis=0)
        hi = np.max(np.stack([s.hi for s in scenes]), axis=0)
        center = (lo + hi) / 2.0
        height = float(hi[1] - lo[1])
        return cls(center, max(height, 60.0) * 2.1)

    def reset(self):
        self.target = self.home_target.copy()
        self.distance = self.home_distance
        self.azimuth = self.home_azimuth
        self.elevation = self.home_elevation

    def eye(self) -> np.ndarray:
        a = math.radians(self.azimuth)
        e = math.radians(self.elevation)
        d = np.array([math.sin(a) * math.cos(e), math.sin(e), math.cos(a) * math.cos(e)])
        return self.target + d * self.distance

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
        e = self.eye()
        from pyglet.gl import gluLookAt

        gluLookAt(e[0], e[1], e[2], self.target[0], self.target[1], self.target[2], 0.0, 1.0, 0.0)


# ---------------------------------------------------------------------------
# 绘制
# ---------------------------------------------------------------------------


def setup_light():
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


def draw_floor(lo_y: float, extent: float = 400.0, step: float = 50.0):
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
    n = int(extent // step)
    for i in range(-n, n + 1):
        glVertex3f(i * step, lo_y, -extent)
        glVertex3f(i * step, lo_y, extent)
        glVertex3f(-extent, lo_y, i * step)
        glVertex3f(extent, lo_y, i * step)
    glEnd()


class ScenePainter:
    """把一个 Scene 的蒙皮结果画到当前 OpenGL 上下文。"""

    def __init__(self, scene):
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
                glColor3f(*COLORS[i % len(COLORS)])
                _p, ptr = as_gl(pos)
                glEnableClientState(GL_VERTEX_ARRAY)
                glVertexPointer(3, GL_FLOAT, 0, ptr)
                if nrm is not None:
                    _n, nptr = as_gl(nrm)
                    glEnableClientState(GL_NORMAL_ARRAY)
                    glNormalPointer(GL_FLOAT, 0, nptr)
                else:
                    glDisableClientState(GL_NORMAL_ARRAY)
                    glNormal3f(0.0, 1.0, 0.0)
                idx, iptr = as_gl(mesh.faces.reshape(-1), np.uint32)
                glDrawElements(GL_TRIANGLES, int(idx.size), GL_UNSIGNED_INT, iptr)
                glDisableClientState(GL_VERTEX_ARRAY)
                glDisableClientState(GL_NORMAL_ARRAY)
            glDisable(GL_LIGHTING)
        if show_bones:
            # 关掉深度测试：骨骼是辅助信息，被网格挡住就没法核对骨架了
            glDisable(GL_LIGHTING)
            glDisable(GL_DEPTH_TEST)
            glColor3f(0.30, 1.00, 0.45)
            glLineWidth(2.2)
            glBegin(GL_LINES)
            for a, b in self.scene.bone_segments(t):
                glVertex3f(float(a[0]), float(a[1]), float(a[2]))
                glVertex3f(float(b[0]), float(b[1]), float(b[2]))
            glEnd()
            glEnable(GL_DEPTH_TEST)
            glLineWidth(1.0)


# ---------------------------------------------------------------------------
# 可手动步进的预览窗口
# ---------------------------------------------------------------------------


class PreviewWindow:
    """pyglet 窗口 + 渲染循环，用 :meth:`pump` 手动驱动。

    :param scenes:   Scene 列表（多于一个时分屏并排）
    :param state:    :class:`ViewState`，由外部（UI）读写
    :param hud_lines: 可调用对象，返回 HUD 文本行列表
    :param on_key:   可调用对象 ``(symbol) -> bool``，返回 True 表示已处理
    """

    def __init__(self, scenes, state: ViewState, caption: str = "X4 动画预览器",
                 width: int = 1000, height: int = 760, location=None,
                 hud_lines=None, on_key=None, on_close=None):
        self.scenes = scenes
        self.state = state
        self.painters = [ScenePainter(s) for s in scenes]
        self.cameras = [OrbitCamera.for_scenes(scenes) for _ in scenes] if len(scenes) > 1 \
            else [OrbitCamera.for_scenes(scenes)]
        self.hud_lines = hud_lines
        self.on_key = on_key
        self.on_close = on_close
        self._closed = False
        self._dragging = False

        self.window = pyglet.window.Window(
            width=width, height=height, caption=caption, resizable=True,
        )
        if location:
            self.window.set_location(*location)
        glClearColor(0.13, 0.14, 0.16, 1.0)
        setup_light()

        self.hud = pyglet.text.Label(
            "", font_name="Consolas", font_size=10, x=8, y=0,
            color=(225, 228, 235, 255), multiline=True, width=max(width - 20, 300),
            anchor_y="top",
        )
        self._install_events()

    # -- 属性 -------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def duration(self) -> float:
        return self.scenes[0].duration if self.scenes else 0.0

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self.window.close()
            except Exception:
                pass
            if self.on_close:
                self.on_close()

    def set_scenes(self, scenes):
        """替换显示的资产（动画时长变了要同步）。"""
        self.scenes = scenes
        self.painters = [ScenePainter(s) for s in scenes]
        self.cameras = [OrbitCamera.for_scenes(scenes) for _ in scenes]
        self.state.time = 0.0

    @property
    def camera(self) -> OrbitCamera:
        return self.cameras[0]

    def reset_cameras(self):
        for cam in self.cameras:
            cam.reset()

    # -- 事件 -------------------------------------------------------------
    def _install_events(self):
        win = self.window

        @win.event
        def on_draw():
            self._draw()

        @win.event
        def on_mouse_press(x, y, button, modifiers):
            if button == mouse.LEFT:
                self._dragging = True

        @win.event
        def on_mouse_release(x, y, button, modifiers):
            self._dragging = False

        @win.event
        def on_mouse_drag(x, y, dx, dy, buttons, modifiers):
            if not self._dragging:
                return
            for cam in self.cameras:
                cam.azimuth += dx * 0.4
                cam.elevation = max(-85.0, min(85.0, cam.elevation + dy * 0.3))

        @win.event
        def on_mouse_scroll(x, y, sx, sy):
            for cam in self.cameras:
                cam.distance = max(20.0, cam.distance * (0.9 ** sy))

        @win.event
        def on_key_press(symbol, modifiers):
            if symbol in (key.ESCAPE, key.Q) and not self._wants_esc():
                self.close()
                return
            if self.on_key and self.on_key(symbol):
                return
            if symbol == key.SPACE:
                self.state.playing = not self.state.playing
            elif symbol == key.PERIOD:
                self.state.time = min(self.duration, self.state.time + 1.0 / 15.0)
            elif symbol == key.COMMA:
                self.state.time = max(0.0, self.state.time - 1.0 / 15.0)
            elif symbol == key.B:
                self.state.show_bones = not self.state.show_bones
            elif symbol == key.N:
                self.state.show_mesh = not self.state.show_mesh
            elif symbol == key.R:
                self.reset_cameras()

        @win.event
        def on_close():
            self._closed = True
            if self.on_close:
                self.on_close()

        @win.event
        def on_resize(w, h):
            glViewport(0, 0, w, h)

    def _wants_esc(self) -> bool:
        return self.on_key is not None and getattr(self, "_esc_reserved", False)

    # -- 渲染 -------------------------------------------------------------
    def _draw(self):
        self.window.clear()
        w, h = self.window.get_size()
        n = max(len(self.scenes), 1)
        for i, (painter, cam) in enumerate(zip(self.painters, self.cameras)):
            vw = w // n
            glViewport(i * vw, 0, vw, h)
            cam.apply(vw, h)
            draw_floor(float(self.scenes[i].lo[1]))
            painter.draw(self.state.time, self.state.show_mesh, self.state.show_bones)

        glViewport(0, 0, w, h)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0, w, 0, h, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glLoadIdentity()
        glDisable(GL_DEPTH_TEST)
        if self.hud_lines:
            self.hud.width = max(w - 20, 300)
            self.hud.y = h - 6
            self.hud.text = "\n".join(self.hud_lines() or [])
            # 半透明底条，免得文字压在模型上看不清
            bar = self.hud.content_height + 8
            glEnable(GL_BLEND)
            glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
            glColor4f(0.06, 0.07, 0.09, 0.78)
            glBegin(GL_QUADS)
            glVertex2f(0, h)
            glVertex2f(w, h)
            glVertex2f(w, h - bar)
            glVertex2f(0, h - bar)
            glEnd()
            glDisable(GL_BLEND)
            self.hud.draw()

    def pump(self) -> bool:
        """推进一帧：处理窗口事件 + 渲染。返回 False 表示窗口已关闭。"""
        if self._closed:
            return False
        dt = pyglet.clock.tick()
        try:
            self.window.dispatch_events()
        except Exception:
            self.close()
            return False
        if self._closed:
            return False
        self.state.advance(dt, self.duration)
        self.window.switch_to()
        self.window.dispatch_event("on_draw")
        self.window.flip()
        return True

    def screenshot(self, path: str):
        from pathlib import Path

        self.window.switch_to()
        self.window.dispatch_event("on_draw")
        self.window.flip()
        buf = pyglet.image.get_buffer_manager().get_color_buffer()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        buf.save(path)
        return path
