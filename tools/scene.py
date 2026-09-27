# -*- coding: utf-8 -*-
"""场景层：定位资产、组装可摆姿态的角色、自动发现可用的 mod 目录。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import ddstex
import rig as rig_mod
import x4game
import xac
import xsm

#: macro 里 argon 女性实际引用的槽位（见 libraries/character_macros.xml）
DEFAULT_HEAD = "assets/characters/argon/heads/char_arg_f_dyn_blend_head.xac"
DEFAULT_BODY = "assets/characters/argon/bodies/char_arg_f_jacket_leggings_civ_01.xac"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

HEAD_KEYS = ("head", "face", "hair")
TORSO_KEYS = ("body", "torso", "jacket", "suit", "cloth", "shirt", "armor", "outfit")


def resolve_asset(path: str | os.PathLike, game: x4game.GameArchive):
    """既接受磁盘路径，也接受游戏包内路径 / 目录。返回 ``(显示名, 字节)``。"""
    p = Path(path)
    if p.is_file():
        return p.name, p.read_bytes()
    if p.is_dir():
        cands = sorted(p.rglob("*.xac"))
        if cands:
            return cands[0].name, cands[0].read_bytes()
        return None
    key = str(path)
    raw = game.read(key)
    if raw is None and not key.lower().endswith(".xac"):
        key = key + ".xac"
        raw = game.read(key)
    if raw is None:
        return None
    return Path(key).name, raw


def classify(name: str) -> str | None:
    """按文件名猜槽位：head / torso。"""
    low = name.lower()
    if any(k in low for k in HEAD_KEYS):
        return "head"
    if any(k in low for k in TORSO_KEYS):
        return "torso"
    return None


def guess_mod_parts(mod_dir: Path):
    """从目录里挑出 head / torso 的 .xac。"""
    heads, torsos = [], []
    for p in sorted(Path(mod_dir).rglob("*.xac")):
        slot = classify(p.name)
        if slot == "head":
            heads.append(p)
        elif slot == "torso":
            torsos.append(p)
    return heads, torsos


def mod_asset_options(mod_dir: Path):
    """返回该目录下所有 .xac 及其槽位，供界面列出。"""
    out = []
    for p in sorted(Path(mod_dir).rglob("*.xac")):
        out.append((p, classify(p.name) or "other"))
    return out


def discover_mod_dirs(game: x4game.GameArchive | None = None,
                      extra_roots: list[Path] | None = None) -> list[tuple[str, Path]]:
    """找出机器上"可能是 mod"的目录，返回 ``[(标签, 路径)]``。

    覆盖三类：游戏自己的 ``extensions/``、本工具所在工作区里的 mod 工程输出，
    以及用户额外指定的根目录。同名只留一个。
    """
    found: dict[Path, str] = {}

    def add(path: Path, label: str):
        try:
            path = path.resolve()
        except OSError:
            return
        if not path.is_dir() or path in found:
            return
        if not any(path.rglob("*.xac")):
            return
        found[path] = label

    roots: list[Path] = []
    if game is not None:
        roots.append(game.root / "extensions")
    roots.append(PROJECT_ROOT.parent)          # 工作区根（D:\dsh-x4）
    roots.extend(extra_roots or [])

    for root in roots:
        if not root.is_dir():
            continue
        if root.name == "extensions":
            for sub in sorted(root.iterdir()):
                if sub.is_dir():
                    add(sub, f"[游戏] {sub.name}")
            continue
        # 工作区：递归找含 .xac 的目录，但只认名字里带 x4/mod/角色名的
        for sub in sorted(root.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            for cand in sorted(sub.rglob("*")):
                if not cand.is_dir():
                    continue
                low = cand.name.lower()
                if any(k in low for k in ("x4_", "x4-", "_mod", "mod_")):
                    rel = cand.relative_to(sub).as_posix()
                    rel = rel if len(rel) <= 34 else "…" + rel[-33:]
                    add(cand, f"[{sub.name}] {rel}")

    return [(label, path) for path, label in sorted(found.items(), key=lambda kv: kv[1])]


# ---------------------------------------------------------------------------
# 贴图
# ---------------------------------------------------------------------------


class TextureSet:
    """mod 目录里的贴图：``材质名 -> (H,W,4) uint8``。

    实测的命名约定是「材质名点换下划线 + 用途后缀」：

        material ``lumine.cloth``  ->  ``textures/lumine_cloth_diff.gz``

    只加载 diffuse（albedo）。normal / smooth 对"动作对不对"没有帮助，
    而且 vanilla 侧本来就不上贴图（它只是姿态基准）。
    """

    SUFFIXES = ("_diff", "_albedo", "_col", "_basecolor")

    def __init__(self, roots: list[Path], warn=None):
        self.files: dict[str, Path] = {}
        self.cache: dict[str, np.ndarray | None] = {}
        self.warn = warn
        for root in roots:
            root = Path(root)
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*")):
                if not f.is_file():
                    continue
                # X4 的贴图是 "xxx_diff.gz"（不带 .dds 后缀），裸 dds 也一并支持
                name = f.name.lower()
                if name.endswith(".dds.gz"):
                    stem = name[:-7]
                elif name.endswith(".dds"):
                    stem = name[:-4]
                elif name.endswith(".gz"):
                    stem = name[:-3]
                else:
                    continue
                for suf in self.SUFFIXES:
                    if stem.endswith(suf):
                        # 两个键都建：layer 里给的是完整名（lumine_face_diff），
                        # 而按材质名猜时要的是去后缀的名字（lumine_face）
                        self.files.setdefault(stem, f)
                        self.files.setdefault(stem[: -len(suf)], f)
                        break
                else:
                    self.files.setdefault(stem, f)

    def __len__(self):
        return len(self.files)

    @staticmethod
    def _key(material: str) -> str:
        return material.strip().lower().replace(".", "_")

    def get(self, material: str):
        key = self._key(material)
        if key in self.cache:
            return self.cache[key]
        path = self.files.get(key)
        if path is None:                      # 退化：材质名本身就是贴图名
            path = self.files.get(key.rsplit("_", 1)[0])
        img = None
        if path is not None:
            try:
                img = ddstex.load_texture(path)
            except Exception as exc:
                if self.warn:
                    self.warn(f"贴图 {path.name} 解码失败: {exc}")
        self.cache[key] = img
        return img

    def by_layer(self, layer: str):
        """按 xac 材质 layer 里记的贴图名精确取图（优先路径）。"""
        key = layer.strip().lower()
        if key in self.cache:
            return self.cache[key]
        path = self.files.get(key)
        img = None
        if path is not None:
            try:
                img = ddstex.load_texture(path)
            except Exception as exc:
                if self.warn:
                    self.warn(f"贴图 {path.name} 解码失败: {exc}")
        self.cache[key] = img
        return img

    def for_asset(self, asset) -> dict:
        """返回 ``{material_id: ndarray}``，供软光栅按材质取样。

        先用材质 layer 里记的贴图名（准），没有再退回按材质名猜。
        """
        out = {}
        layers = getattr(asset, "material_layers", [])
        for i, name in enumerate(asset.materials):
            img = None
            for layer in (layers[i] if i < len(layers) else []):
                img = self.by_layer(layer)
                if img is not None:
                    break
            if img is None:
                img = self.get(name)
            if img is not None:
                out[i] = img
        return out


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------


class Scene:
    """一组资产 + 一条动画，按时间给出蒙皮后的顶点。"""

    def __init__(self, label: str, sources: list[tuple[str, bytes]], delta: bool = False):
        self.label = label
        self.paths: list[str] = []
        self.assets: list[xac.Asset] = []
        self.rigs: list[rig_mod.Rig] = []
        self.delta = delta
        self.anim: xsm.Xsm | None = None
        self.anim_name = "-"
        for name, raw in sources:
            asset = xac.load_xac(name, raw)
            if not asset.meshes:
                continue
            self.paths.append(name)
            self.assets.append(asset)
            self.rigs.append(rig_mod.Rig.build(asset, None, delta=delta))
        if not self.assets:
            raise ValueError(f"{label}: 没有可用的网格")

        pts = np.concatenate([m.positions for a in self.assets for m in a.meshes])
        self.lo = pts.min(axis=0)
        self.hi = pts.max(axis=0)
        self.center = (self.lo + self.hi) / 2.0
        self.height = float(self.hi[1] - self.lo[1])
        self.vertex_count = int(sum(m.vertex_count for a in self.assets for m in a.meshes))
        self._cache_t = None
        self._cache = None

    # -- 动画 -------------------------------------------------------------
    def set_anim(self, anim: xsm.Xsm | None, name: str = "-"):
        self.anim = anim
        self.anim_name = name
        self._cache_t = None
        self.rigs = [rig_mod.Rig.build(a, anim, delta=self.delta) for a in self.assets]

    @property
    def duration(self) -> float:
        return self.anim.duration if self.anim is not None else 0.0

    def matched(self) -> float:
        vals = [r.matched_fraction() for r in self.rigs]
        return float(np.mean(vals)) if vals else 0.0

    def missing_bones(self) -> list[str]:
        out: list[str] = []
        for r in self.rigs:
            out.extend(r.missing)
        return out

    # -- 姿态 -------------------------------------------------------------
    def pose(self, t: float):
        key = round(t, 6)
        if self._cache_t == key and self._cache is not None:
            return self._cache
        parts: list = []
        for r in self.rigs:
            parts.extend(r.pose_all(t))
        self._cache_t = key
        self._cache = parts
        return parts

    def bone_segments(self, t: float):
        segs = []
        for r in self.rigs:
            world = r.world_matrices(t)
            parents = r.parents
            for i in range(len(r)):
                p = parents[i]
                if p < 0:
                    continue
                segs.append((world[i][:3, 3], world[p][:3, 3]))
        return segs

    def info(self) -> str:
        return (f"{len(self.assets)} 个网格 / {self.vertex_count} 顶点 / "
                f"动画匹配 {self.matched():.0%}")
