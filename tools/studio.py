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
from tkinter import messagebox, ttk

import console  # noqa: F401  (设置 UTF-8 控制台)
import animations as anims_mod
import modpack
import scene as scene_mod
import glview
import x4game
import xsm

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
UI_FONT = ("Microsoft YaHei UI", 9)
UI_FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
MONO_FONT = ("Consolas", 9)

#: 预览窗口最多分几屏。模型多的时候全选会切成十几个窄条，反而看不清
MAX_PANES = 4


def _elide(text: str, width: int) -> str:
    """太长就中间省略——头尾都要留：名字在头，区分用的目录提示常在尾。"""
    if len(text) <= width:
        return text
    head = max(6, width // 2 - 2)
    return text[:head] + "…" + text[-(width - head - 1):]


def grab_widget(path, widget) -> tuple[int, int, int, int]:
    """给一个 tkinter 控件截图（自检和文档出图用）。

    **不要**拿 ``winfo_rootx/rooty`` 去喂 ``ImageGrab.grab(bbox=…)``：实测截出来
    的框整体偏几十像素，右边和下面各少一块。原因是两条坐标系不是一套：

    * tkinter / GetWindowRect 给的是**进程虚拟坐标**（这台机器上 1707×1067，
      显示器 150% 缩放）；
    * ``ImageGrab.grab()`` 拿到的是**物理像素**（2560×1600）。

    所以整屏截图后按比例换算再裁剪，返回裁剪框（图像像素）。
    """
    from PIL import ImageGrab

    widget.update_idletasks()
    try:
        import ctypes
        from ctypes import wintypes

        class _Rect(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        rc = _Rect()
        ctypes.windll.user32.GetWindowRect(widget.winfo_id(), ctypes.byref(rc))
        left, top, right, bottom = rc.left, rc.top, rc.right, rc.bottom
        vw = ctypes.windll.user32.GetSystemMetrics(0)
        vh = ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:                      # 拿不到就走 tkinter 自己的坐标
        left, top = widget.winfo_rootx(), widget.winfo_rooty()
        right = left + widget.winfo_width()
        bottom = top + widget.winfo_height()
        vw = vh = 0

    img = ImageGrab.grab()
    if vw > 0 and vh > 0 and (img.width != vw or img.height != vh):
        sx, sy = img.width / vw, img.height / vh
        box = (round(left * sx), round(top * sy), round(right * sx), round(bottom * sy))
    else:
        box = (left, top, right, bottom)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.crop(box).save(str(path))
    return box


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
        #: 当前 mod 里的全部模型（一个 mod 可以有好几套：多套装扮、多个角色…）
        self.variants: list[scene_mod.ModelVariant] = []
        #: 选中的那几套（key 集合）——选多套就并排显示
        self.picked: set[str] = set()
        self._file_cache: dict[Path, bytes] = {}
        self._materials_cache: dict[Path, tuple] = {}
        #: 下拉框条目的原始路径 -> 能扫描的目录（打包安装的要先摊开）
        self._dir_cache: dict[Path, Path] = {}
        self._updating_scale = False
        self._status = "就绪"
        self._closing = False
        #: 模态对话框（选目录等）期间为 True——此时**不能**再驱动 pyglet。
        #: tkinter 的原生模态框有自己的消息循环，我们再往里塞 dispatch_events
        #: 会两边抢消息，表现就是界面卡死。
        self._modal = False

        self.component_var = tk.StringVar(value=cfg.get("component", "character_argon_female_01"))
        self.mod_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.compare_var = tk.BooleanVar(value=bool(cfg.get("compare", True)))
        self.compare_target_var = tk.StringVar(value=cfg.get("compare_target", "vanilla"))
        self.bones_var = tk.BooleanVar(value=False)
        self.mesh_var = tk.BooleanVar(value=True)
        self.textures_var = tk.BooleanVar(value=bool(cfg.get("textures", True)))
        self.loop_var = tk.BooleanVar(value=True)
        self.autoplay_var = tk.BooleanVar(value=False)
        self.drag_invert_var = tk.BooleanVar(value=bool(cfg.get("drag_invert", False)))
        self.speed_var = tk.DoubleVar(value=1.0)
        self.time_var = tk.DoubleVar(value=0.0)

        self._build_ui()
        self.sync_state()          # 把 config 里存的开关同步进 ViewState
        self._load_animations()
        self._load_mods()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(30, self._tick)

    # -- 界面 -------------------------------------------------------------
    def _build_ui(self):
        r = self.root
        r.title("X4 动画预览器")
        sh = r.winfo_screenheight()
        default = f"356x{max(620, min(880, sh - 120))}"
        r.geometry(self.cfg.get("studio_geometry") or default)
        r.minsize(340, 620)

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
        self.blocks["status"] = self._build_status(outer)      # 同上：贴底先占位
        self.blocks["anims"] = self._build_anims(outer)

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
        ttk.Button(row, text="重载", width=6,
                   command=self._reload_assets).pack(side="left", padx=(4, 0))

        # 模型列表：一个 mod 里可能有好几套模型（`build_all.py --outfits a,b` 的
        # A/B 套装就是两套 head+torso），早先只认第一套、其余的整套都看不到。
        # 点一下切换显示；同时选几套就分屏并排。
        mrow = ttk.Frame(box)
        mrow.pack(fill="x", pady=(6, 0))
        ttk.Label(mrow, text="模型（点选，可多选）").pack(side="left")
        self.variant_count = ttk.Label(mrow, text="", style="Hint.TLabel")
        self.variant_count.pack(side="left", padx=(4, 0))
        ttk.Button(mrow, text="全选", width=6,
                   command=lambda: self.pick_variants(all=True)).pack(side="right")
        ttk.Button(mrow, text="清空", width=6,
                   command=lambda: self.pick_variants(all=False)).pack(side="right", padx=(0, 4))

        mwrap = ttk.Frame(box)
        mwrap.pack(fill="x", pady=(2, 0))
        self.variant_list = tk.Listbox(mwrap, font=MONO_FONT, activestyle="none",
                                       exportselection=False, height=3, selectmode="browse")
        vsb = ttk.Scrollbar(mwrap, orient="vertical", command=self.variant_list.yview)
        self.variant_list.configure(yscrollcommand=vsb.set)
        self.variant_list.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.variant_list.bind("<Button-1>", self.on_variant_click)
        self.variant_list.bind("<space>", self.on_variant_key)
        self.variant_list.bind("<Return>", self.on_variant_key)

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(row2, text="并排对比", variable=self.compare_var,
                        command=self.rebuild).pack(side="left")
        ttk.Label(row2, text="对象").pack(side="left", padx=(6, 2))
        self.compare_combo = ttk.Combobox(row2, textvariable=self.compare_target_var,
                                          values=["vanilla"], state="readonly", width=18)
        self.compare_combo.pack(side="left", fill="x", expand=True)
        self.compare_combo.bind("<<ComboboxSelected>>",
                                lambda e: (save_config({"compare_target": self.compare_target_var.get()}),
                                           self.rebuild()))

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
        ttk.Checkbutton(vrow, text="贴图", variable=self.textures_var,
                        command=self.sync_state).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(vrow, text="骨骼", variable=self.bones_var,
                        command=self.sync_state).pack(side="left", padx=(6, 0))
        ttk.Button(vrow, text="重置相机", width=9, command=self.reset_camera).pack(side="left", padx=(6, 0))
        ttk.Button(vrow, text="截图", width=6, command=self.screenshot).pack(side="left", padx=4)
        return box

    def _build_status(self, parent):
        box = ttk.Frame(parent)
        # side="bottom" + 排在动画列表**之前** pack：窗口矮的时候先给状态栏留位置，
        # 让可以伸缩的动画列表去吃剩下的空间（否则状态栏会被挤成 1px 看不见）
        box.pack(side="bottom", fill="x")
        self.status = ttk.Label(box, text="", font=UI_FONT, wraplength=330, justify="left")
        self.status.pack(fill="x")
        opt = ttk.Frame(box)
        opt.pack(fill="x", pady=(2, 0))
        ttk.Checkbutton(opt, text="拖动反向", variable=self.drag_invert_var,
                        command=self.sync_state).pack(side="left")
        ttk.Label(opt, text="（默认：模型跟着鼠标转）",
                  style="Hint.TLabel").pack(side="left", padx=(4, 0))
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
        targets = ["vanilla"] + [label for label, _ in dirs]
        self.compare_combo.configure(values=targets)
        if self.compare_target_var.get() not in targets:
            self.compare_target_var.set("vanilla")
        if not self.mod_var.get() or self.mod_var.get() not in labels:
            want = self.cfg.get("last_mod")
            pick = None
            for label, path in dirs:
                if want and str(path) == want:
                    pick = label
            if pick is None and want and (Path(want).is_dir() or modpack.archive_kind(want)):
                # config 里记的路径不在自动发现的结果里（名字不像 mod 目录、是手动
                # 浏览进来的、或者是 .cat/.zip 包）：补一条，免得 --mod / 上次的选择失效
                resolved = self._resolve_mod_path(want)
                if resolved is not None:
                    name = Path(want).name
                    pick = f"[包] {name}" if modpack.archive_kind(want) else f"[手动] {name}"
                    self._dir_cache[Path(want)] = resolved
                    self.mod_dirs = list(self.mod_dirs) + [(pick, Path(want))]
                    labels = labels + [pick]
                    self.mod_combo.configure(values=labels)
                    self.compare_combo.configure(
                        values=["vanilla"] + [lab for lab, _ in self.mod_dirs])
            self.mod_var.set(pick or (labels[1] if len(labels) > 1 else labels[0]))
        self._set_status(f"发现 {len(dirs)} 个 mod 目录")
        self.rebuild()

    def current_mod_dir(self) -> Path | None:
        label = self.mod_var.get()
        for lab, path in getattr(self, "mod_dirs", []):
            if lab == label:
                return self._resolved_mod_dir(path)
        return None

    def _resolved_mod_dir(self, path: Path) -> Path | None:
        """下拉框里的条目可能是散装目录，也可能是**打包安装**的 mod——
        后者要摊开才能扫（见 :mod:`modpack`）。结果记住，别每次重建都问一遍。"""
        if path in self._dir_cache:
            return self._dir_cache[path]
        out = self._resolve_mod_path(path) or path
        self._dir_cache[path] = out
        return out

    # -- 模型（一个 mod 里可能有好几套） -----------------------------------
    def _load_variants(self):
        """扫描当前 mod 里的全部模型，恢复/决定选哪几套。"""
        mod_dir = self.current_mod_dir()
        self.variants = scene_mod.mod_variants(mod_dir) if mod_dir is not None else []
        self._file_cache.clear()
        keys = [v.key for v in self.variants]
        saved = (self.cfg.get("variant_pick") or {}).get(str(mod_dir)) if mod_dir else None
        if saved:
            self.picked = {k for k in saved if k in keys}
        elif keys:
            # 模型不多就默认全显示——一眼看到 mod 里到底有几套；多了只放第一套，
            # 免得分成十几个窄条（要全看可以点"全选"）
            room = MAX_PANES - (1 if self.compare_var.get() else 0)
            self.picked = set(keys[:room]) if len(keys) <= room else {keys[0]}
        else:
            self.picked = set()
        self._refill_variant_list()

    def selected_variants(self) -> list[scene_mod.ModelVariant]:
        return [v for v in self.variants if v.key in self.picked]

    def _refill_variant_list(self, keep: int | None = None):
        self.variant_list.delete(0, tk.END)
        for v in self.variants:
            mark = "x" if v.key in self.picked else " "
            self.variant_list.insert(tk.END, f"[{mark}] {_elide(v.display(), 42)}")
        total = len(self.variants)
        self.variant_count.configure(
            text=f"{len(self.picked)}/{total}" if total else "（没有 .xac）")
        if keep is not None and 0 <= keep < total:
            self.variant_list.selection_clear(0, tk.END)
            self.variant_list.selection_set(keep)
            self.variant_list.activate(keep)

    def on_variant_click(self, event):
        idx = self.variant_list.nearest(event.y)
        if not (0 <= idx < len(self.variants)):
            return
        bbox = self.variant_list.bbox(idx)      # 点在列表下方的空白处不算
        if bbox is None or not (bbox[1] <= event.y <= bbox[1] + bbox[3]):
            return
        self.toggle_variant(self.variants[idx].key, idx)

    def on_variant_key(self, event):
        sel = self.variant_list.curselection()
        if sel and sel[0] < len(self.variants):
            self.toggle_variant(self.variants[sel[0]].key, sel[0])
        return "break"

    def toggle_variant(self, key: str, idx: int | None = None):
        if key in self.picked:
            self.picked.discard(key)
        else:
            self.picked.add(key)
        self._refill_variant_list(keep=idx)
        self._rebuild_now()

    def pick_variants(self, all: bool):
        self.picked = {v.key for v in self.variants} if all else set()
        self._refill_variant_list(keep=0 if self.variants else None)
        self._rebuild_now()

    def _variant_sources(self, v: scene_mod.ModelVariant) -> list[tuple[str, bytes]]:
        """读该套模型的字节（按文件缓存，来回切换不用反复读盘）。"""
        out: list[tuple[str, bytes]] = []
        for p in v.files:
            raw = self._file_cache.get(p)
            if raw is None:
                try:
                    raw = p.read_bytes()
                except OSError as exc:
                    self._set_status(f"读不了 {p.name}：{exc}")
                    continue
                self._file_cache[p] = raw
            out.append((p.name, raw))
        return out

    def _mod_materials(self, mod_dir: Path):
        """该 mod 的贴图表与材质规则（一次扫描，多个模型共用）。"""
        got = self._materials_cache.get(mod_dir)
        if got is None:
            ts = scene_mod.TextureSet([mod_dir], warn=self._set_status)
            got = (ts if len(ts) else None, scene_mod.MaterialRules([mod_dir]))
            self._materials_cache[mod_dir] = got
        return got

    def _remember(self, values: dict):
        save_config(values)
        self.cfg.update(values)

    def _vanilla_sources(self):
        out = []
        for ref in (scene_mod.DEFAULT_HEAD, scene_mod.DEFAULT_BODY):
            got = scene_mod.resolve_asset(ref, self.game)
            if got:
                out.append(got)
        return out

    def _pick_directory(self, initial: str) -> str | None:
        """自己实现的 mod 选择器（目录，或 .cat / .zip 包）。

        **不要用 ``filedialog``**：实测连"只开 tkinter、没有 pyglet"的裸环境里
        ``askdirectory`` 都会卡住不返回（对话框根本不弹），和 pyglet 无关。
        这里全部用普通 tkinter 控件搭，不碰任何原生对话框。
        """
        top = tk.Toplevel(self.root)
        top.title("选择 mod（目录 / .cat / .zip）")
        top.transient(self.root)
        top.geometry("580x480")
        top.minsize(460, 340)
        result: dict = {"path": None}
        cur = tk.StringVar(value=initial)

        ttk.Label(top, text="路径：目录，或 .cat / .zip 包（可直接粘贴，回车进入）",
                  font=UI_FONT).pack(anchor="w", padx=10, pady=(10, 2))
        entry = ttk.Entry(top, textvariable=cur, font=MONO_FONT)
        entry.pack(fill="x", padx=10)

        info = ttk.Label(top, text="", style="Hint.TLabel", font=UI_FONT)
        info.pack(anchor="w", padx=10, pady=(4, 0))

        mid = ttk.Frame(top)
        mid.pack(fill="both", expand=True, padx=10, pady=6)
        listing = tk.Listbox(mid, font=MONO_FONT, activestyle="none", exportselection=False)
        sb = ttk.Scrollbar(mid, orient="vertical", command=listing.yview)
        listing.configure(yscrollcommand=sb.set)
        listing.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        def shorten(text: str, width: int = 54) -> str:
            return text if len(text) <= width else "…" + text[-(width - 1):]

        def refresh(*_):
            path = Path(cur.get().strip().strip('"'))
            listing.delete(0, tk.END)
            if modpack.archive_kind(path):
                info.configure(text=f"√ 包：{path.name}    确定后自动解包再预览")
                return
            if not path.is_dir():
                info.configure(text=f"× 不是目录，也不是 .cat/.zip 包：{path}")
                return
            try:
                subs = sorted((d for d in path.iterdir() if d.is_dir()),
                              key=lambda d: d.name.lower())
                packs = sorted((f for f in path.iterdir()
                                if modpack.archive_kind(f)), key=lambda f: f.name.lower())
            except OSError as exc:
                info.configure(text=f"× 读不了：{exc}")
                return
            listing.insert(tk.END, "..")
            for d in subs:
                listing.insert(tk.END, d.name + "/")
            for f in packs:
                listing.insert(tk.END, f.name)
            # 必须有界！对主目录做无界 rglob 会跑几分钟，表现就是卡死
            n_xac = sum(1 for _ in scene_mod.iter_files(path, (".xac",),
                                                        max_depth=6, budget_s=0.4))
            # 打包形态：目录里只有 ext_01.cat/.dat，确定后会自动摊开
            cats = [c.name for c in modpack.asset_cats(path)] if not n_xac else []
            if cats:
                info.configure(text=f"√ {shorten(str(path))}    只有 {', '.join(cats)}"
                                    f"（打包形态，确定后自动解包）")
            else:
                info.configure(text=f"√ {shorten(str(path))}    子目录 {len(subs)} 个，"
                                    f".xac {n_xac} 个，包 {len(packs)} 个")

        def enter(_event=None):
            sel = listing.curselection()
            path = Path(cur.get().strip().strip('"'))
            if sel:
                name = listing.get(sel[0])
                path = (path.parent if name == ".." else path / name.rstrip("/"))
            cur.set(str(path))
            refresh()

        def confirm(_event=None):
            path = Path(cur.get().strip().strip('"'))
            if path.is_dir() or modpack.archive_kind(path):
                result["path"] = str(path)
                top.destroy()
            else:
                info.configure(text=f"× 不是目录，也不是 .cat/.zip 包：{path}")

        listing.bind("<Double-Button-1>", enter)
        listing.bind("<Return>", enter)
        entry.bind("<Return>", lambda e: (refresh(), None)[1])
        top.bind("<Escape>", lambda e: top.destroy())

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        for label, cmd in (("上一级", lambda: (cur.set(str(Path(cur.get()).parent)), refresh())),
                           ("主目录", lambda: (cur.set(str(Path.home())), refresh())),
                           ("取消", top.destroy)):
            ttk.Button(btns, text=label, width=8, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="确定", width=8, command=confirm).pack(side="right")

        refresh()
        entry.focus_set()
        self._modal = True
        try:
            top.wait_window()          # tkinter 自己的模态，不是原生对话框
        finally:
            self._modal = False
        return result["path"]

    def _modal_call(self, fn, *args, **kwargs):
        """所有会弹出**模态**对话框的调用都要走这里。

        tkinter 的原生模态框（选目录、消息框）自带消息循环，而我们的主循环
        每 16ms 会去驱动一次 pyglet 窗口。两边同时抢消息就会卡死——
        所以模态期间必须停掉 pyglet 那一侧。
        """
        self._modal = True
        try:
            self.root.update_idletasks()
            return fn(*args, **kwargs)
        finally:
            self._modal = False

    def rebuild(self):
        """按当前选择重建场景（会重扫 mod 里的模型）。"""
        self._load_variants()
        self._rebuild_now()

    def _rebuild_now(self):
        # 让用户知道在处理（选了很大的目录时扫描要一两秒）
        self._set_status("加载中…")
        self.root.update_idletasks()
        mod_dir = self.current_mod_dir()
        picked = self.selected_variants()
        mod_scenes: list[scene_mod.Scene] = []
        new_scenes: list[scene_mod.Scene] = []
        try:
            if mod_dir is not None and picked:
                tex, rules = self._mod_materials(mod_dir)
                for v in picked:
                    scene = self._make_scene(v.name(), v, tex, rules)
                    if scene is not None:
                        mod_scenes.append(scene)
                new_scenes.extend(mod_scenes)
                self._remember({"last_mod": str(mod_dir), "variant_pick": {
                    **(self.cfg.get("variant_pick") or {}),
                    str(mod_dir): [v.key for v in picked]}})
            if self.compare_var.get() or not new_scenes:
                new_scenes.extend(self._compare_scenes(picked))
        except Exception as exc:
            self._set_status(f"加载失败：{exc}")
            print(f"[error] 加载失败: {exc}")
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

        self._update_asset_hint(mod_dir, mod_scenes, picked)
        self._set_status("场景已加载")

    def _make_scene(self, label: str, v: scene_mod.ModelVariant, tex, rules):
        """建一个场景；某一套坏了只跳过它，不让它把整个预览拖没。"""
        try:
            sources = self._variant_sources(v)
            if not sources:
                return None
            return scene_mod.Scene(label, sources, textures=tex, material_rules=rules)
        except Exception as exc:
            self._set_status(f"{v.name()} 加载失败：{exc}")
            print(f"[error] {v.name()} 加载失败: {exc}")
            return None

    def _compare_scenes(self, want: list[scene_mod.ModelVariant]) -> list[scene_mod.Scene]:
        """对比侧的场景：另一个 mod（同名模型对同名模型）或 vanilla。"""
        out: list[scene_mod.Scene] = []
        target = self.compare_target_var.get()
        other_dir = None
        for lab, path in getattr(self, "mod_dirs", []):
            if lab == target:
                other_dir = path
        if other_dir is None:
            van = self._vanilla_sources()
            if van:
                out.append(scene_mod.Scene("vanilla", van))
            return out

        # 与另一个 mod 对比：迭代时"上一版 vs 这一版"比对着 vanilla 更有用。
        # 两边套数一致时按套对上（这一版的 A 对上一版的 A），对不上就退回第一套。
        otex, orules = self._mod_materials(other_dir)
        others = scene_mod.mod_variants(other_dir)
        for v in scene_mod.match_variants(want, others):
            scene = self._make_scene(f"{other_dir.name}·{v.name()}", v, otex, orules)
            if scene is not None:
                out.append(scene)
        return out

    def _update_asset_hint(self, mod_dir, mod_scenes, picked):
        lines = [mod_dir.name] if mod_dir is not None else ["仅 vanilla"]
        if not picked and self.variants:
            lines.append("没有选中的模型：在上面的列表里点一下（可多选并排）")
        elif picked and len(self.variants) > 1:
            names = "、".join(v.name() for v in picked)
            lines.append(f"显示 {len(picked)}/{len(self.variants)} 套：{_elide(names, 60)}")
        if mod_scenes:
            verts = sum(s.vertex_count for s in mod_scenes)
            match = sum(s.matched() for s in mod_scenes) / len(mod_scenes)
            hit = sum(s.texture_stats()[0] for s in mod_scenes)
            total = sum(s.texture_stats()[1] for s in mod_scenes)
            txt = f"{len(mod_scenes)} 套模型 / {verts} 顶点 / 动画匹配 {match:.0%}"
            if total:
                txt += f" / 贴图 {hit}/{total}"
            lines.append(txt)
        self.asset_hint.configure(text="\n".join(lines))

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
        self.state.show_textures = self.textures_var.get()
        self.state.speed = float(self.speed_var.get())
        self.state.drag_invert = self.drag_invert_var.get()

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
        start = self.cfg.get("last_browse") or str(self.current_mod_dir() or Path.home())
        d = self._pick_directory(start)
        if not d:
            return
        save_config({"last_browse": d})
        mod_dir = self._resolve_mod_path(d)
        if mod_dir is None:
            return
        is_pack = modpack.archive_kind(Path(d)) is not None
        label = (f"[包] {Path(d).name}" if is_pack else f"[手动] {Path(d).name}")
        self._dir_cache[Path(d)] = mod_dir
        self.mod_dirs = list(getattr(self, "mod_dirs", [])) + [(label, Path(d))]
        labels = ["（只用 vanilla 基准）"] + [l for l, _ in self.mod_dirs]
        self.mod_combo.configure(values=labels)
        self.compare_combo.configure(values=["vanilla"] + [l for l, _ in self.mod_dirs])
        self.mod_var.set(label)
        self.on_mod_change()

    def _resolve_mod_path(self, raw) -> Path | None:
        """把用户给的路径变成能预览的目录。

        可能是散装目录，也可能是 ``.cat`` / ``.zip`` 包（X4 的发布形态：assets
        全在 ``ext_01.dat`` 里，``.cat`` 只是索引）——是包就地摊开再用。
        """
        try:
            out = modpack.unpack(raw, progress=self._set_status)
        except Exception as exc:
            self._set_status(f"打不开 {Path(str(raw)).name}：{exc}")
            print(f"[error] 解包失败 {raw}: {exc}")
            return None
        return out

    def on_mod_change(self):
        self.rebuild()

    def _reload_assets(self):
        """重新读磁盘上的 .xac / 贴图（改完 mod 不用切来切去）。"""
        self.anim_cache.clear()
        self._materials_cache.clear()
        self._dir_cache.clear()          # 打包的 mod 重建过就重新摊
        self.rebuild()
        self._set_status("已重新加载资产")

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
            "textures": self.textures_var.get(),
            "compare_target": self.compare_target_var.get(),
            "drag_invert": self.drag_invert_var.get(),
        })
        if self.preview and not self.preview.closed:
            self.preview.close()
        self.root.destroy()

    # -- 主循环 -----------------------------------------------------------
    def _should_pump(self) -> bool:
        """模态框开着时不驱动 pyglet（见 _modal_call 的说明）。"""
        return not self._modal and self.preview is not None and not self.preview.closed

    def _tick(self):
        if self._closing:
            return
        if not self._should_pump():
            # 模态框开着（或没有预览窗）时只维持 tkinter 自己转
            self.root.after(50 if self._modal else 16, self._tick)
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
    ap.add_argument("--shot-dir", help="自检时额外把控制面板/选择器截图放到这里")
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
    app.shot_dir = args.shot_dir

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
            steps.append(f"模型 {len(app.variants)} 套，选中 {len(app.picked)}"
                         + ("（" + "、".join(v.name() for v in app.selected_variants()) + "）"
                            if app.variants else ""))
            # 一个 mod 里有多套模型时：切换选择 -> 场景数要跟着变
            if len(app.variants) > 1:
                first = app.variants[0].key
                app.toggle_variant(first, 0)
                after_off = len(app.scenes)
                app.toggle_variant(first, 0)
                after_on = len(app.scenes)
                steps.append(f"切模型：取消后 {after_off} 屏 / 选回后 {after_on} 屏")
                if after_on <= after_off:
                    steps.append("  <== 多选没有变成多屏!")
            else:
                app.pick_variants(all=True)
                steps.append(f"全选 -> {len(app.scenes)} 屏")
            # 拖动方向：往右拖，方位角要**变小**（模型跟着鼠标走）
            def _fake_drag(dx, dy):
                """模拟一次鼠标拖动。

                注意 pyglet 的窗口在正常状态下会把外部调用的 dispatch_event
                塞进事件队列（见 BaseWindow.dispatch_event），这里临时打开直通
                开关，等价于主循环 pump 里那次派发。
                """
                win = app.preview.window
                app.preview._dragging = True
                old = win._allow_dispatch_event
                win._allow_dispatch_event = True
                try:
                    win.dispatch_event("on_mouse_drag", 200, 200, dx, dy, 1, 0)
                finally:
                    win._allow_dispatch_event = old
                    app.preview._dragging = False

            cam = app.preview.camera
            before = cam.azimuth
            _fake_drag(60, 0)
            steps.append(f"向右拖 60px：方位角 {before:.1f}° -> {cam.azimuth:.1f}°"
                         + ("（反了!）" if cam.azimuth >= before else "（模型跟着鼠标 ✓）"))
            before = cam.elevation
            _fake_drag(0, 60)
            steps.append(f"向上拖 60px：仰角 {before:.1f}° -> {cam.elevation:.1f}°"
                         + ("（反了!）" if cam.elevation >= before else "（模型跟着鼠标 ✓）"))
            app.drag_invert_var.set(True)
            app.sync_state()
            before = cam.azimuth
            _fake_drag(60, 0)
            steps.append(f"勾上'拖动反向'：方位角 {before:.1f}° -> {cam.azimuth:.1f}°"
                         + ("（没生效!）" if cam.azimuth <= before else "（相机跟着鼠标 ✓）"))
            app.drag_invert_var.set(False)
            app.sync_state()
            app.reset_camera()
            for i in (1, 5, 12):
                if len(app.shown) > i:
                    app.anim_list.selection_clear(0, tk.END)
                    app.anim_list.selection_set(i)
                    app.on_pick_anim()
                    steps.append(f"动画 {app.shown[i].name}")
            targets = list(app.compare_combo.cget("values"))
            if len(targets) > 1:
                app.compare_target_var.set(targets[1])
                app.rebuild()
                steps.append(f"对比对象 -> {targets[1]}")
            # 走一遍完整的"浏览"路径：把对话框换成一个假返回值，
            # 验证 选目录 -> 更新下拉 -> 重建场景 这条链没问题
            target = None
            for lab, path in getattr(app, "mod_dirs", []):
                if "lumine" in lab.lower() and "terran" in lab.lower():
                    target = path
            if target is not None:
                real = app._pick_directory
                app._pick_directory = lambda initial: str(target)
                try:
                    app.on_browse()
                    steps.append(f"浏览 -> {target.name}")
                finally:
                    app._pick_directory = real

                # 真实构造一次自绘选择器：截图后自己关掉，验证它不会卡住
                def _snap_picker():
                    tops = [w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel)]
                    if tops and app.shot_dir:
                        try:
                            box = grab_widget(Path(app.shot_dir) / "dirpicker.png", tops[0])
                            print(f"[selftest] 选择器截图 {box}")
                        except Exception as exc:
                            print(f"[selftest] 选择器截图失败: {exc}")
                    for w in tops:
                        w.destroy()

                app.root.after(700, _snap_picker)
                got = app._pick_directory(str(target))
                steps.append(f"自绘选择器可开可关(返回 {got!r})")

                # 点"主目录"曾经会卡死：refresh 里对主目录做了无界 rglob。
                # 现在扫描有界，这条路径必须能在 1 秒内走完。
                import time as _t
                t0 = _t.perf_counter()
                app.root.after(800, lambda: [w.destroy() for w in app.root.winfo_children()
                                             if isinstance(w, tk.Toplevel)])
                app._pick_directory(str(Path.home()))
                dt = _t.perf_counter() - t0
                steps.append(f"主目录选择器 {dt:.2f}s{'  <== 太慢!' if dt > 1.5 else ''}")

            # 验证模态期间不会去 pump pyglet（"浏览"卡死就是这个原因）。
            # 注意别直接调 _tick——它会再注册一个 after 回调，攒起来会互相打架。
            app._modal = True
            modal_ok = not app._should_pump()
            app._modal = False
            steps.append(f"模态期间跳过 pump: {modal_ok}")
            if args.mod:
                # 上面几步把 mod 换走了，截图前切回 --mod 指定的那个
                want = str(Path(args.mod).resolve())
                for lab, path in getattr(app, "mod_dirs", []):
                    if str(path) == want:
                        app.mod_var.set(lab)
                        app.on_mod_change()
                        steps.append(f"回到 {lab}（模型 {len(app.variants)} 套 / "
                                     f"{len(app.scenes)} 屏）")
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
                    print("[selftest] 布局" + ("有问题" if overflow else "完整可见 ✓"))
                    try:
                        r.lift()
                        r.attributes("-topmost", True)
                        r.update()
                        time.sleep(0.5)
                        r.update()
                        panel = out.with_name(out.stem + "_panel.png")
                        box = grab_widget(panel, r)
                        print(f"[selftest] 面板截图 {panel}  {box}")
                    except Exception as exc:
                        print(f"[selftest] 面板截图失败: {exc}")
                    print("[selftest] 场景: "
                          + " + ".join(f"{s.label}({s.info()})" for s in app.scenes))
                    print("[selftest] 动画: "
                          + (app.current_ref().name if app.current_ref() else "(无)"))
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
