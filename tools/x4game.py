# -*- coding: utf-8 -*-
"""定位 X4 游戏目录并从 .cat/.dat 包里随机读取资产。

路径策略（可移植）：按顺序尝试
  1. 环境变量 ``X4_GAME_DIR``
  2. 项目根下的 ``config.json`` 里的 ``game_dir``（相对项目根解析）
  3. 一组常见安装位置候选
找不到时抛 ``GameNotFound``，让调用方提示用户配置，而不是写死盘符。
"""

from __future__ import annotations

import glob
import json
import os
import struct
from pathlib import Path

# 项目根 = 本文件所在目录的上一级（tools/ 的父目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 常见安装位置候选。仅作为最后手段，不假设一定存在。
CANDIDATE_GLOBS = [
    "C:/Program Files (x86)/Steam/steamapps/common/X4 Foundations",
    "C:/Program Files/Steam/steamapps/common/X4 Foundations",
    "D:/SteamLibrary/steamapps/common/X4 Foundations",
    "E:/SteamLibrary/steamapps/common/X4 Foundations",
    "F:/SteamLibrary/steamapps/common/X4 Foundations",
    "D:/Steam/steamapps/common/X4 Foundations",
    "E:/Steam/steamapps/common/X4 Foundations",
    "C:/GOG Games/X4 Foundations",
    "D:/GOG Games/X4 Foundations",
    "~/GOG Games/X4 Foundations",
]


class GameNotFound(RuntimeError):
    pass


def _looks_like_game(path: Path) -> bool:
    return (path / "01.cat").is_file() and (path / "01.dat").is_file()


def find_game_root(explicit: str | os.PathLike | None = None) -> Path:
    """返回 X4 安装根目录（含 01.cat 的那一层）。"""
    tried: list[str] = []

    if explicit:
        p = Path(explicit).expanduser()
        if _looks_like_game(p):
            return p.resolve()
        tried.append(str(p))

    env = os.environ.get("X4_GAME_DIR")
    if env:
        p = Path(env).expanduser()
        if _looks_like_game(p):
            return p.resolve()
        tried.append(str(p))

    cfg = PROJECT_ROOT / "config.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            raw = data.get("game_dir")
            if raw:
                p = Path(raw).expanduser()
                if not p.is_absolute():
                    p = PROJECT_ROOT / p
                if _looks_like_game(p):
                    return p.resolve()
                tried.append(str(p))
        except (OSError, ValueError):
            pass

    for pattern in CANDIDATE_GLOBS:
        for hit in glob.glob(os.path.expanduser(pattern)):
            p = Path(hit)
            if _looks_like_game(p):
                return p.resolve()
            tried.append(str(p))

    raise GameNotFound(
        "找不到 X4 安装目录（需要含 01.cat / 01.dat 的那一层）。\n"
        "请设置环境变量 X4_GAME_DIR，或在项目根写 config.json：\n"
        '  {"game_dir": "D:/SteamLibrary/steamapps/common/X4 Foundations"}\n'
        "已尝试：\n  " + "\n  ".join(tried)
    )


def _parse_cat(cat_path: Path):
    """一个 .cat 索引行：``name size mtime md5``。"""
    out = []
    with open(cat_path, "rb") as fh:
        for line in fh.read().split(b"\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.rsplit(b" ", 3)
            if len(parts) != 4:
                continue
            try:
                out.append(
                    (
                        parts[0].decode("utf-8", "replace"),
                        int(parts[1]),
                        int(parts[2]),
                        parts[3].decode("ascii", "replace"),
                    )
                )
            except ValueError:
                continue
    return out


class GameArchive:
    """所有 ``0N.cat`` 的合并索引；后加载的覆盖先加载的（X4 的规则）。"""

    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(root) if root else find_game_root()
        self.index: dict[str, dict] = {}
        self._build()

    def _build(self):
        cats = sorted(glob.glob(str(self.root / "0[0-9].cat")))
        if not cats:
            raise GameNotFound(f"{self.root} 下没有 0N.cat")
        for cat in cats:
            cat_path = Path(cat)
            dat_path = cat_path.with_suffix(".dat")
            offset = 0
            for name, size, mtime, md5 in _parse_cat(cat_path):
                self.index[name.lower()] = {
                    "name": name,
                    "size": size,
                    "mtime": mtime,
                    "md5": md5,
                    "dat": dat_path,
                    "off": offset,
                    "cat": cat_path.name,
                }
                offset += size

    def __len__(self):
        return len(self.index)

    def has(self, path: str) -> bool:
        return path.lower() in self.index

    def read(self, path: str) -> bytes | None:
        entry = self.index.get(path.lower())
        if entry is None:
            return None
        with open(entry["dat"], "rb") as fh:
            fh.seek(entry["off"])
            return fh.read(entry["size"])

    def read_text(self, path: str) -> str | None:
        raw = self.read(path)
        return None if raw is None else raw.decode("utf-8", "replace")

    def exists(self, path: str) -> bool:
        return self.has(path)

    def find(self, pattern: str, limit: int = 200):
        """按子串过滤资产路径（大小写不敏感）。"""
        needle = pattern.lower()
        hits = [e["name"] for k, e in self.index.items() if needle in k]
        hits.sort()
        return hits[:limit]


def write_extracted(data: bytes, out_path: Path) -> Path:
    """把从包里读出的字节落盘（缓存用）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path


if __name__ == "__main__":
    game = GameArchive()
    print(f"游戏目录: {game.root}")
    print(f"索引条目: {len(game)}")
    for probe in (
        "libraries/character_components.xml",
        "assets/characters/animations/anim_a-a_ar_fe_generic_01.xsm",
    ):
        entry = game.index.get(probe.lower())
        print(f"  {probe} -> {entry['cat']}#{entry['off']} ({entry['size']} B)" if entry else f"  {probe} -> 缺失")
