# -*- coding: utf-8 -*-
"""角色动画清单：从 ``libraries/character_components.xml`` 里读某个组件能用的动画。

X4 的 NPC 用共享 component（比如 ``character_argon_female_01``）持有骨架和
整套动画，macro 只挑 head/torso/props 三个网格。所以"这个角色会播哪些动画"
要去 component 的 ``<animations>`` 里找，而不是看 mesh 资产。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from x4game import GameArchive

COMPONENTS_XML = "libraries/character_components.xml"

_COMPONENT_RE = r'<component\s+name="{name}"[^>]*>(.*?)</component>'
_ANIM_RE = re.compile(r'<animation\s+([^>]*?)/?>', re.S)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')

#: 常用 component：决定换成哪个种族的骨架 / 动画集
COMPONENT_BY_RACE = {
    "argon": "character_argon_female_01",
    "argon_male": "character_argon_male_01",
    "terran": "character_terran_female_01",
    "paranid": "character_paranid_female_01",
    "teladi": "character_teladi_female_01",
    "boron": "character_boron_female_01",
    "split": "character_split_female_01",
}


@dataclass
class AnimRef:
    name: str  # 逻辑名，例如 anim_stand_idle_01
    path: str  # 资产路径，例如 assets/characters/animations/anim_a-a_ar_fe_generic_01.xsm
    comment: str = ""


def component_names(game: GameArchive | None = None) -> list[str]:
    game = game or GameArchive()
    text = game.read_text(COMPONENTS_XML) or ""
    return re.findall(r'<component\s+name="([^"]+)"', text)


def component_animations(
    component: str = "character_argon_female_01",
    game: GameArchive | None = None,
    dedupe: bool = True,
) -> list[AnimRef]:
    """返回该 component 声明的动画列表（按 xml 顺序）。"""
    game = game or GameArchive()
    text = game.read_text(COMPONENTS_XML)
    if not text:
        return []
    m = re.search(_COMPONENT_RE.format(name=re.escape(component)), text, re.S)
    if not m:
        return []
    body = m.group(1)
    out: list[AnimRef] = []
    seen: set[str] = set()
    for attrs in _ANIM_RE.findall(body):
        d = dict(_ATTR_RE.findall(attrs))
        ref = d.get("ref")
        if not ref or ref.startswith("$"):
            continue
        path = ref if ref.lower().endswith((".xsm", ".xac")) else ref + ".xsm"
        key = d.get("name", path)
        if dedupe and key in seen:
            continue
        seen.add(key)
        out.append(AnimRef(key, path, d.get("comment", "")))
    return out


def all_animations(game: GameArchive | None = None) -> list[str]:
    """游戏里全部 ``.xsm`` 资产路径。"""
    game = game or GameArchive()
    out = [e["name"] for k, e in game.index.items()
           if k.startswith("assets/characters/animations/") and k.endswith(".xsm")]
    out.sort()
    return out


def suggest(game: GameArchive | None = None, limit: int = 400) -> list[AnimRef]:
    """挑一批"值得先看"的动画：站立/坐姿/工具/搬运/谈话。

    用于没有 component（比如自己拼的 mod 资产）时的默认清单。
    """
    game = game or GameArchive()
    wanted = [
        "anim_a-a_ar_fe_generic_01", "anim_a-a_ar_fe_generic_02", "anim_a-a_ar_fe_generic_03",
        "anim_a-a_ar_fe_generic_04", "anim_a-a_ar_fe_generic_05", "anim_a-a_ar_fe_generic_06",
        "anim_a-b_ar_fe", "anim_b-a_ar_fe", "anim_a-a_ar_carry_01", "anim_a-a_ar_carry_02",
        "anim_a_carry-a_ar_01", "anim_a_carry-a_twohandtool_ar_01",
        "anim_a_carry-a_onehandtool_ar_01", "anim_a_twohandtool_ar_01",
        "anim_a_onehandtool_ar_01", "anim_a-a_ar_fe",
        "anim_a_terminal-a_terminal_ar_fe_generic_01",
        "anim_tran_a-a_terminal_ar_fe_generic_01",
    ]
    out: list[AnimRef] = []
    for stem in wanted[:limit]:
        path = f"assets/characters/animations/{stem}.xsm"
        if game.has(path):
            out.append(AnimRef(stem, path))
    if out:
        return out
    return [AnimRef(Path(p).stem, p) for p in all_animations(game)[:limit]]


if __name__ == "__main__":
    import sys

    comp = sys.argv[1] if len(sys.argv) > 1 else "character_argon_female_01"
    g = GameArchive()
    anims = component_animations(comp, g)
    print(f"{comp}: {len(anims)} 条动画")
    for a in anims[:40]:
        print(f"  {a.name:<38} {a.path}")
    print("游戏内 .xsm 总数:", len(all_animations(g)))
