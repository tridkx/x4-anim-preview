# -*- coding: utf-8 -*-
"""X4 动画预览器 · 图形界面版。

左边是控制面板（选 mod、选动画、播放控制），右边是独立的三维预览窗口。
不用敲命令行参数：启动后自动列出机器上的 mod 和该角色的全部动画。

    python tools/studio.py            # 或双击 启动预览器.bat

主循环由 tkinter 的 ``after`` 驱动，每帧手动步进 pyglet 窗口
（见 `glview.PreviewWindow.pump`），所以两个窗口共享一个线程，不需要锁。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import console  # noqa: F401  (设置 UTF-8 控制台)
import animations as anims_mod
import glview
import scene as scene_mod
import x4game
import xsm

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
UI_FONT = ("Microsoft YaHei UI", 9)
UI_FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
MONO_FONT = ("Consolas", 9)


# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_config(cfg: dict):
    try:
        old = load_config()
        old.update(cfg)
        CONFIG_PATH.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------


class StudioApp:
    def __init__(self, root: tk.Tk, game: x4game.GameArchive, cfg: dict):
        self.root = root
        self.game = game
        self.cfg = cfg
        self.state = glview.ViewState()
        self.scenes: list[scene_mod.Scene] = []
        self.preview: glview.PreviewWindow | None = None
        self.anim_refs: list[anims_mod.AnimRef] = []
        self.shown: list[anims_mod.AnimRef] = []
        self.anim_cache: dict[str, xsm.Xsm] = {}
        self._updating_scale = False
        self._status = "就绪"
        self._closing = False

        self.component_var = tk.StringVar(value=cfg.get("component", "character_argon_female_01"))
        self.mod_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.compare_var = tk.BooleanVar(value=bool(cfg.get("compare", True)))
        self.bones_var = tk.BooleanVar(value=False)
        self.mesh_var = tk.BooleanVar(value=True)
        self.loop_var = tk.BooleanVar(value=True)
        self.autoplay_var = tk.BooleanVar(value=False)
        self.speed_var = tk.DoubleVar(value=1.0)
        self.time_var = tk.DoubleVar(value=0.0)

        self._build_ui()
        self._load_animations()
        self._load_mods()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(30, self._tick)

    # -- 界面 -------------------------------------------------------------
    def _build_ui(self):
        r = self.root
        r.title("X4 动画预览器")
        sh = r.winfo_screenheight()
        default = f"356x{max(560, min(880, sh - 120))}"
        r.geometry(self.cfg.get("studio_geometry") or default)
        r.minsize(330, 560)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=UI_FONT)
        style.configure("TLabelframe.Label", font=UI_FONT_BOLD)
        style.configure("Hint.TLabel", foreground="#666")

        outer = ttk.Frame(r, padding=8)
        outer.pack(fill="both", expand=True)
        self.blocks = {}

        self.blocks["assets"] = self._build_assets(outer)
        self.blocks["playback"] = self._build_playback(outer)  # 放列表之前，空间不足也不会被挤掉
        self.blocks["anims"] = self._build_anims(outer)
        self.blocks["status"] = self._build_status(outer)

    def _build_assets(self, parent):
        box = ttk.LabelFrame(parent, text="1. 选择 mod", padding=8)
        box.pack(fill="x", pady=(0, 6))

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.mod_combo = ttk.Combobox(row, textvariable=self.mod_var, state="readonly")
        self.mod_combo.pack(side="left", fill="x", expand=True)
        self.mod_combo.bind("<<ComboboxSelected>>", lambda e: self.on_mod_change())
        ttk.Button(row, text="浏览…", width=7, command=self.on_browse).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="重扫", width=6, command=self._load_mods).pack(side="left", padx=(4, 0))

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(row2, text="与 vanilla 并排对比", variable=self.compare_var,
                        command=self.rebuild).pack(side="left")

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=(4, 0))
        ttk.Label(row3, text="动画集").pack(side="left")
        comps = ["character_argon_female_01", "character_argon_male_01",
                 "character_player_argon_female_01", "character_split_female_01",
                 "character_teladi_01", "character_paranid_01"]
        comp = ttk.Combobox(row3, textvariable=self.component_var, values=comps,
                            state="readonly")
        comp.pack(side="left", fill="x", expand=True, padx=(4, 0))
        comp.bind("<<ComboboxSelected>>", lambda e: self._load_animations(reload_anims=True))

        self.asset_hint = ttk.Label(box, text="", style="Hint.TLabel", wraplength=330,
                                    justify="left")
        self.asset_hint.pack(fill="x", pady=(6, 0))
        return box

    def _build_anims(self, parent):
        box = ttk.LabelFrame(parent, text="3. 选择动画", padding=8)
        box.pack(fill="both", expand=True, pady=(0, 6))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="搜索").pack(side="left")
        entry = ttk.Entry(row, textvariable=self.search_var)
        entry.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.search_var.trace_add("write", lambda *_: self._refill_anim_list())

        wrap = ttk.Frame(box)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.anim_list = tk.Listbox(wrap, font=MONO_FONT, activestyle="none",
                                    exportselection=False, height=8)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.anim_list.yview)
        self.anim_list.configure(yscrollcommand=sb.set)
        self.anim_list.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.anim_list.bind("<<ListboxSelect>>", lambda e: self.on_pick_anim())

        nav = ttk.Frame(box)
        nav.pack(fill="x", pady=(6, 0))
        ttk.Button(nav, text="◀ 上一条", command=lambda: self.step_anim(-1)).pack(side="left")
        ttk.Button(nav, text="下一条 ▶", command=lambda: self.step_anim(1)).pack(side="left", padx=4)
        return box

    def _build_playback(self, parent):
        box = ttk.LabelFrame(parent, text="2. 播放控制", padding=8)
        box.pack(fill="x", pady=(0, 6))

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.play_btn = ttk.Button(row, text="⏸ 暂停", width=9, command=self.toggle_play)
        self.play_btn.pack(side="left")
        ttk.Button(row, text="◀|", width=3, command=lambda: self.nudge(-1)).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="|▶", width=3, command=lambda: self.nudge(1)).pack(side="left", padx=2)
        ttk.Checkbutton(row, text="循环", variable=self.loop_var,
                        command=self.sync_state).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(row, text="轮播", variable=self.autoplay_var).pack(side="left", padx=(6, 0))

        trow = ttk.Frame(box)
        trow.pack(fill="x", pady=(6, 0))
        self.time_label = ttk.Label(trow, text="0.00 / 0.00s", font=MONO_FONT, width=15)
        self.time_label.pack(side="right")
        self.seek = ttk.Scale(trow, from_=0.0, to=1.0, variable=self.time_var,
                              command=self.on_seek)
        self.seek.pack(side="left", fill="x", expand=True)

        srow = ttk.Frame(box)
        srow.pack(fill="x", pady=(4, 0))
        ttk.Label(srow, text="速度").pack(side="left")
        ttk.Scale(srow, from_=0.1, to=3.0, variable=self.speed_var,
                  command=lambda v: self.sync_state()).pack(side="left", fill="x", expand=True,
                                                            padx=(4, 6))
        self.speed_label = ttk.Label(srow, text="x1.00", font=MONO_FONT, width=6)
        self.speed_label.pack(side="left")

        vrow = ttk.Frame(box)
        vrow.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(vrow, text="网格", variable=self.mesh_var,
                        command=self.sync_state).pack(side="left")
        ttk.Checkbutton(vrow, text="骨骼", variable=self.bones_var,
                        command=self.sync_state).pack(side="left", padx=(6, 0))
        ttk.Button(vrow, text="重置相机", width=9, command=self.reset_camera).pack(side="left", padx=(8, 0))
        ttk.Button(vrow, text="截图", width=6, command=self.screenshot).pack(side="left", padx=4)
        return box

    def _build_status(self, parent):
        box = ttk.Frame(parent)
        box.pack(fill="x")
        self.status = ttk.Label(box, text="", font=UI_FONT, wraplength=330, justify="left")
        self.status.pack(fill="x")
        ttk.Label(box, text="快捷键：空格 播放/暂停 · ←→ 换动画 · , . 逐帧 · B 骨骼 · "
                            "N 网格 · R 重置相机 · 鼠标拖动旋转 · 滚轮缩放",
                  style="Hint.TLabel", wraplength=330, justify="left").pack(fill="x", pady=(4, 0))
        return box

    # -- 数据加载 ---------------------------------------------------------
    def _load_animations(self, reload_anims: bool = False):
        comp = self.component_var.get()
        self.anim_refs = anims_mod.component_animations(comp, self.game)
        if not self.anim_refs:
            self.anim_refs = anims_mod.suggest(self.game)
        if reload_anims:
            self.anim_cache.clear()
        self._refill_anim_list()
        save_config({"component": comp})
        self._set_status(f"动画集 {comp}：{len(self.anim_refs)} 条")

    def _refill_anim_list(self):
        needle = self.search_var.get().strip().lower()
        self.shown = [a for a in self.anim_refs
                      if not needle or needle in a.name.lower() or needle in a.path.lower()]
        self.anim_list.delete(0, tk.END)
        for a in self.shown:
            self.anim_list.insert(tk.END, f"{a.name}")
        if self.shown:
            self.anim_list.selection_clear(0, tk.END)
            self.anim_list.selection_set(0)
            self.anim_list.see(0)

    def _load_mods(self):
        dirs = scene_mod.discover_mod_dirs(self.game)
        self.mod_dirs = dirs
        labels = ["（只用 vanilla 基准）"] + [label for label, _ in dirs]
        self.mod_combo.configure(values=labels)
        if not self.mod_var.get() or self.mod_var.get() not in labels:
            want = self.cfg.get("last_mod")
            pick = None
            for label, path in dirs:
                if want and str(path) == want:
                    pick = label
            self.mod_var.set(pick or (labels[1] if len(labels) > 1 else labels[0]))
        self._set_status(f"发现 {len(dirs)} 个 mod 目录")
        self.rebuild()

    def current_mod_dir(self) -> Path | None:
        label = self.mod_var.get()
        for lab, path in getattr(self, "mod_dirs", []):
            if lab == label:
                return path
        return None

    def _vanilla_sources(self):
        out = []
        for ref in (scene_mod.DEFAULT_HEAD, scene_mod.DEFAULT_BODY):
            got = scene_mod.resolve_asset(ref, self.game)
            if got:
                out.append(got)
        return out

    def rebuild(self):
        """按当前选择重建场景并（必要时）创建预览窗口。"""
        sources: list[tuple[str, bytes]] = []
        mod_dir = self.current_mod_dir()
        if mod_dir is not None:
            heads, torsos = scene_mod.guess_mod_parts(mod_dir)
            picked = (heads[:1] + torsos[:1])
            if not picked:
                options = scene_mod.mod_asset_options(mod_dir)
                picked = [p for p, _ in options[:2]]
            for p in picked:
                sources.append((p.name, p.read_bytes()))
            save_config({"last_mod": str(mod_dir)})

        new_scenes: list[scene_mod.Scene] = []
        try:
            if sources:
                new_scenes.append(scene_mod.Scene("mod", sources))
            if self.compare_var.get() or not new_scenes:
                van = self._vanilla_sources()
                if van:
                    new_scenes.append(scene_mod.Scene("vanilla", van))
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))
            return

        if not new_scenes:
            self._set_status("没有可加载的资产")
            return

        self.scenes = new_scenes
        if self.preview is None or self.preview.closed:
            self._open_preview()
        else:
            self.preview.set_scenes(new_scenes)

        # 场景换了要重新套动画
        if self.shown:
            self.on_pick_anim(force=True)

        desc = " + ".join(f"{s.label}({s.info()})" for s in self.scenes)
        hint = mod_dir.name if mod_dir else "仅 vanilla"
        self.asset_hint.configure(text=f"{hint}\n{desc}")
        self._set_status("场景已加载")

    def _open_preview(self):
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + self.root.winfo_width() + 8
        y = self.root.winfo_rooty()
        geo = self.cfg.get("preview_geometry", "1000x820")
        try:
            w, h = (int(v) for v in geo.split("x"))
        except ValueError:
            w, h = 1000, 820
        self.preview = glview.PreviewWindow(
            self.scenes, self.state,
            width=w, height=h, location=(x, y),
            hud_lines=self._hud_lines, on_key=self._on_preview_key,
            on_close=self._on_preview_closed,
        )

    # -- 动画 -------------------------------------------------------------
    def _load_anim(self, ref: anims_mod.AnimRef) -> xsm.Xsm | None:
        cached = self.anim_cache.get(ref.path)
        if cached is not None:
            return cached
        raw = self.game.read(ref.path)
        if raw is None:
            self._set_status(f"缺少动画资产：{ref.path}")
            return None
        anim = xsm.parse_xsm(raw, ref.path)
        if len(self.anim_cache) > 12:
            self.anim_cache.clear()
        self.anim_cache[ref.path] = anim
        return anim

    def current_ref(self) -> anims_mod.AnimRef | None:
        sel = self.anim_list.curselection()
        if not sel or not self.shown:
            return None
        idx = min(sel[0], len(self.shown) - 1)
        return self.shown[idx]

    def on_pick_anim(self, force: bool = False):
        ref = self.current_ref()
        if ref is None or not self.scenes:
            return
        if not force and getattr(self, "_last_ref", None) == ref.path:
            return
        self._last_ref = ref.path
        anim = self._load_anim(ref)
        if anim is None:
            return
        for s in self.scenes:
            s.set_anim(anim, ref.name)
        self.state.time = 0.0
        dur = max(anim.duration, 0.01)
        self.seek.configure(to=dur)
        self.time_var.set(0.0)
        self._set_status(f"{ref.name}  ·  {anim.duration:.2f}s  ·  "
                         f"{anim.frame_rate():.0f}fps  ·  匹配 {self.scenes[0].matched():.0%}")
        save_config({"last_anim": ref.name})

    def step_anim(self, delta: int):
        if not self.shown:
            return
        sel = self.anim_list.curselection()
        idx = (sel[0] if sel else 0) + delta
        idx = max(0, min(idx, len(self.shown) - 1))
        self.anim_list.selection_clear(0, tk.END)
        self.anim_list.selection_set(idx)
        self.anim_list.see(idx)
        self.on_pick_anim()

    # -- 播放 -------------------------------------------------------------
    def toggle_play(self):
        self.state.playing = not self.state.playing
        self._sync_play_button()

    def _sync_play_button(self):
        self.play_btn.configure(text="⏸ 暂停" if self.state.playing else "▶ 播放")

    def nudge(self, frames: int):
        self.state.playing = False
        self._sync_play_button()
        self.state.time = max(0.0, min(self.state.time + frames / 15.0,
                                       self.preview.duration if self.preview else 0.0))

    def on_seek(self, value):
        if self._updating_scale:
            return
        self.state.time = float(value)

    def sync_state(self):
        self.state.loop = self.loop_var.get()
        self.state.show_mesh = self.mesh_var.get()
        self.state.show_bones = self.bones_var.get()
        self.state.speed = float(self.speed_var.get())

    def reset_camera(self):
        if self.preview:
            self.preview.reset_cameras()

    def screenshot(self):
        if not self.preview:
            return
        out = PROJECT_ROOT / "work" / "shots" / time.strftime("shot_%Y%m%d_%H%M%S.png")
        try:
            self.preview.screenshot(str(out))
            self._set_status(f"已截图 {out}")
        except Exception as exc:
            self._set_status(f"截图失败：{exc}")

    def on_browse(self):
        d = filedialog.askdirectory(title="选择 mod 目录（含 .xac）")
        if not d:
            return
        label = f"[手动] {Path(d).name}"
        self.mod_dirs = list(getattr(self, "mod_dirs", [])) + [(label, Path(d))]
        self.mod_combo.configure(values=["（只用 vanilla 基准）"] + [l for l, _ in self.mod_dirs])
        self.mod_var.set(label)
        self.on_mod_change()

    def on_mod_change(self):
        self.rebuild()

    # -- 预览回调 ---------------------------------------------------------
    def _hud_lines(self):
        ref = self.current_ref()
        lines = []
        if ref:
            lines.append(f"{ref.name}   ({Path(ref.path).stem})")
        dur = self.preview.duration if self.preview else 0.0
        lines.append(f"t={self.state.time:5.2f}s / {dur:5.2f}s   x{self.state.speed:.2f}   "
                     f"{'播放' if self.state.playing else '暂停'}")
        for s in self.scenes:
            lines.append(f"[{s.label}] {len(s.assets)} 网格 {s.vertex_count} 顶点 "
                         f"匹配 {s.matched():.0%}")
        return lines

    def _on_preview_key(self, symbol) -> bool:
        from pyglet.window import key

        if symbol == key.RIGHT:
            self.step_anim(1)
            return True
        if symbol == key.LEFT:
            self.step_anim(-1)
            return True
        if symbol == key.UP:
            self.speed_var.set(min(3.0, float(self.speed_var.get()) * 1.25))
            self.sync_state()
            return True
        if symbol == key.DOWN:
            self.speed_var.set(max(0.1, float(self.speed_var.get()) / 1.25))
            self.sync_state()
            return True
        if symbol == key.SPACE:
            self.toggle_play()
            return True
        return False

    def _on_preview_closed(self):
        self._closing = True
        self.root.after(10, self.root.destroy)

    def on_close(self):
        self._closing = True
        save_config({
            "studio_geometry": self.root.geometry(),
            "preview_geometry": (f"{self.preview.window.width}x{self.preview.window.height}"
                                 if self.preview and not self.preview.closed else None) or
                                self.cfg.get("preview_geometry", "1000x820"),
            "compare": self.compare_var.get(),
            "component": self.component_var.get(),
        })
        if self.preview and not self.preview.closed:
            self.preview.close()
        self.root.destroy()

    # -- 主循环 -----------------------------------------------------------
    def _tick(self):
        if self._closing:
            return
        if self.preview is not None and not self.preview.closed:
            alive = self.preview.pump()
            if not alive:
                self._closing = True
                self.root.after(10, self.root.destroy)
                return
            dur = self.preview.duration
            if self.state.playing and self.autoplay_var.get() and dur > 0 \
                    and self.state.time >= dur - 1e-3:
                self.step_anim(1)
            self._updating_scale = True
            self.time_var.set(self.state.time)
            self._updating_scale = False
            self.seek.configure(to=max(dur, 0.01))
            self.time_label.configure(text=f"{self.state.time:5.2f} / {dur:5.2f}s")
            self.speed_label.configure(text=f"x{self.state.speed:.2f}")
            self._sync_play_button()
        self.root.after(16, self._tick)

    def _set_status(self, text: str):
        self._status = text
        if hasattr(self, "status"):
            self.status.configure(text=text)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description="X4 动画预览器（图形界面）")
    ap.add_argument("--selftest", metavar="OUT.png",
                    help="启动后自动截图并退出（用来验证界面与渲染链路）")
    ap.add_argument("--mod", help="启动时直接选中该 mod 目录")
    args = ap.parse_args(argv)

    try:
        game = x4game.GameArchive()
    except Exception as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("找不到 X4", str(exc))
        return 2

    cfg = load_config()
    if args.mod:
        cfg["last_mod"] = str(Path(args.mod).resolve())

    root = tk.Tk()
    app = StudioApp(root, game, cfg)

    if args.selftest:
        out = Path(args.selftest).resolve()

        def exercise():
            """把主要交互路径跑一遍，确认不会崩。"""
            steps = []
            labels = list(app.mod_combo.cget("values"))
            if len(labels) > 2:
                app.mod_var.set(labels[2])
                app.on_mod_change()
                steps.append(f"切到 {labels[2]}")
            for i in (1, 5, 12):
                if len(app.shown) > i:
                    app.anim_list.selection_clear(0, tk.END)
                    app.anim_list.selection_set(i)
                    app.on_pick_anim()
                    steps.append(f"动画 {app.shown[i].name}")
            app.bones_var.set(True)
            app.sync_state()
            steps.append("开骨骼显示")
            app.state.playing = True
            for _ in range(6):
                app.preview.pump()
            app.state.playing = False
            app.state.time = 0.5
            return steps

        def shoot():
            try:
                root.update_idletasks()
                try:
                    steps = exercise()
                    print("[selftest] 交互: " + " -> ".join(steps))
                except Exception as exc:
                    import traceback
                    traceback.print_exc()
                    print(f"[selftest] 交互失败: {exc}")
                if app.preview is None:
                    print("[selftest] 没有创建预览窗口")
                else:
                    app.state.playing = False
                    app.state.time = 0.6
                    app.preview.screenshot(str(out))
                    print(f"[selftest] 写出 {out}")
                    r = app.root
                    r.update_idletasks()
                    win_h = r.winfo_height()
                    print(f"[selftest] 面板窗口 {r.winfo_width()}x{win_h}")
                    overflow = False
                    for name, w in app.blocks.items():
                        y, hh = w.winfo_y(), w.winfo_height()
                        flag = "" if y + hh <= win_h + 2 else "  <== 超出窗口!"
                        overflow = overflow or bool(flag)
                        print(f"[selftest]   {name:9s} y={y:5d} h={hh:5d} 底={y+hh:5d}{flag}")
                    print(f"[selftest] 布局{'有问题' if overflow else '完整可见 ✓'}")
                    try:
                        from PIL import ImageGrab
                        r.lift()
                        r.attributes("-topmost", True)
                        r.update()
                        time.sleep(0.5)
                        r.update()
                        box = (r.winfo_rootx(), r.winfo_rooty(),
                               r.winfo_rootx() + r.winfo_width(),
                               r.winfo_rooty() + r.winfo_height())
                        panel = out.with_name(out.stem + "_panel.png")
                        ImageGrab.grab(bbox=box).save(panel)
                        print(f"[selftest] 面板截图 {panel}  {box}")
                    except Exception as exc:
                        print(f"[selftest] 面板截图失败: {exc}")
                    print(f"[selftest] 场景: " +
                          " + ".join(f"{s.label}({s.info()})" for s in app.scenes))
                    print(f"[selftest] 动画: " +
                          (app.current_ref().name if app.current_ref() else "(无)"))
            except Exception as exc:
                import traceback
                traceback.print_exc()
                print(f"[selftest] 失败: {exc}")
            finally:
                app.on_close()

        root.after(1500, shoot)

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
