# -*- coding: utf-8 -*-
"""场景层：定位资产、组装可摆姿态的角色、自动发现可用的 mod 目录。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

import numpy as np

import ddstex
import modpack
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


#: 递归扫描的默认上限。mod 目录实测最深 5~6 层、几十个文件；
#: 而一旦用户把路径指到主目录，无界 rglob 会跑几分钟（几十万文件），
#: 界面就直接卡死了。深度和时间双保险，宁可漏扫也不能卡。
SCAN_MAX_DEPTH = 8
SCAN_BUDGET_S = 2.0


def iter_files(root: Path, suffixes: tuple[str, ...], max_depth: int = SCAN_MAX_DEPTH,
               budget_s: float = SCAN_BUDGET_S, want_dirs: bool = False):
    """有界递归查找。``suffixes`` 传空元组表示要全部文件。

    :param want_dirs: 为 True 时连目录一起产出（用于找 ``textures`` 这类目录名）
    """
    import time as _time

    deadline = _time.perf_counter() + budget_s
    stack: list[tuple[Path, int]] = [(Path(root), 0)]
    while stack:
        d, depth = stack.pop()
        if depth > max_depth or _time.perf_counter() > deadline:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if _time.perf_counter() > deadline:   # 单个大目录的 scandir 也可能很慢
                return
            try:
                if e.is_dir(follow_symlinks=False):
                    if want_dirs and (not suffixes or e.name.lower().endswith(suffixes)):
                        yield Path(e.path)
                    if depth < max_depth:
                        stack.append((Path(e.path), depth + 1))
                elif not want_dirs:
                    low = e.name.lower()
                    if not suffixes or low.endswith(suffixes):
                        yield Path(e.path)
            except OSError:
                continue


def resolve_asset(path: str | os.PathLike, game: x4game.GameArchive):
    """既接受磁盘路径，也接受游戏包内路径 / 目录。返回 ``(显示名, 字节)``。"""
    p = Path(path)
    if p.is_file():
        return p.name, p.read_bytes()
    if p.is_dir():
        cands = sorted(iter_files(p, (".xac",)))
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


def mod_asset_options(mod_dir: Path):
    """返回该目录下所有 .xac 及其槽位，供界面列出。"""
    out = []
    for p in sorted(iter_files(Path(mod_dir), (".xac",))):
        out.append((p, classify(p.name) or "other"))
    return out


# ---------------------------------------------------------------------------
# mod 里的"多套模型"
# ---------------------------------------------------------------------------
#
# 一个 mod 目录里往往不止一套模型：`python tools/build_all.py --outfits a,b`
# 会同时产出 A/B 两套装扮（各一套 head + torso），有些包干脆是几个角色放一起。
# 只认"第一个 head + 第一个 torso"就会把其余的整套吞掉——这正是预览器只能看到
# 第一套模型的原因。判断"哪些文件属于同一套"有两个依据，按可靠程度排序：
#
# 1. **macro 的 ``<models>``**（最准）：游戏加载的就是 macro 里列的那几件，
#    每一条 macro 就是一套模型。
# 2. **文件名前缀**（退化）：``yue_a_head`` + ``yue_a_body`` 归成 ``yue_a``；
#    但只有在"模型目录"（上两级目录，即 heads/ 与 bodies/ 的共同父目录）
#    也一致时才归并——否则 ``dist/x4_rose_argon_add/.../rose_head`` 会和
#    ``dist/x4_rose_argon_replace/.../rose_head`` 混成一套。

#: macro 里 model 的槽位顺序（只影响列出顺序）
SLOT_ORDER = ("head", "torso", "props", "props2")

#: 槽位 -> 文件名关键词。head 优先于 torso，和 :func:`classify` 保持一致
SLOT_KEYS = (("head", HEAD_KEYS), ("torso", TORSO_KEYS))

_MACRO_BLOCK_RE = re.compile(r"<macro\b([^>]*?)(?:/>|>(.*?)</macro>)", re.S | re.I)
_MODEL_TAG_RE = re.compile(r"<model\b([^>]*?)/?>", re.S | re.I)
_XML_ATTR_RE = re.compile(r'([A-Za-z_][\w.-]*)\s*=\s*"([^"]*)"')

#: ref 里这些值表示"这个槽位空着"，不是资产
_EMPTY_REFS = ("", "none", "null", "-")


@dataclass
class ModelVariant:
    """mod 里的**一套**模型（head + torso + …）。

    ``key`` 用来在 config.json 里记住用户选了哪几套，``label`` 是给人看的名字，
    ``hint`` 只在几套重名时才填（比如一个目录里塞了好几个 mod），指向它在哪个子目录。
    """

    key: str
    label: str
    parts: dict[str, Path]
    origin: str = "macro"          # macro / group / others
    hint: str = ""

    def slot_names(self) -> list[str]:
        known = [s for s in SLOT_ORDER if s in self.parts]
        return known + sorted(s for s in self.parts if s not in SLOT_ORDER)

    @property
    def files(self) -> list[Path]:
        return [self.parts[s] for s in self.slot_names()]

    def slot_summary(self) -> str:
        known = [s for s in SLOT_ORDER if s in self.parts]
        n_other = len(self.parts) - len(known)
        if n_other and not known:
            return f"{n_other} 件"
        return "+".join(known + ([f"其他×{n_other}"] if n_other else []))

    def name(self) -> str:
        """短名字（重名时带上目录提示），用于 HUD 和场景标签。"""
        return f"{self.label} · {self.hint}" if self.hint else self.label

    def display(self) -> str:
        return f"{self.name()}   {self.slot_summary()}"


def split_slot(stem: str) -> tuple[str | None, str]:
    """把文件名拆成 ``(槽位, 前缀)``：``yue_a_head`` -> ``("head", "yue_a")``。

    关键词取**最后一次**出现的位置：``char_arg_f_dyn_blend_head`` 的关键词在尾部，
    前缀才是 ``char_arg_f_dyn_blend``。
    """
    low = stem.lower()
    for slot, keys in SLOT_KEYS:
        hit = max((low.rfind(k) for k in keys), default=-1)
        if hit >= 0:
            return slot, stem[:hit].rstrip("_-.") or stem
    return None, stem


def _model_dir(path: Path) -> str:
    """文件的"模型目录"：上两级。

    ``…/yueqingshu/heads/yue_a_head.xac`` 与 ``…/yueqingshu/bodies/yue_a_body.xac``
    上两级都是 ``…/yueqingshu``，所以算同一套；而 ``dist/<modA>/…`` 与
    ``dist/<modB>/…`` 不会互相归并。
    """
    return path.parent.parent.as_posix().lower()


def _dir_hint(path: Path, mod_dir: Path) -> str:
    """文件所在目录（相对 mod 根），重名时用来区分是哪一套。"""
    try:
        return "/".join(path.relative_to(mod_dir).parts[:-1]) or mod_dir.name
    except ValueError:
        return path.parent.name


def _unique_hints(dirs: list[str]) -> list[str]:
    """给一组目录算出"最短还能互相区分"的前缀。

    ``dist/x4_lumine_argon_add/…`` 与 ``dist/x4_lumine_terran_add/…`` 只留
    ``x4_lumine_argon_add`` / ``x4_lumine_terran_add``；差别在更深处时（CC 包那种
    ``a/yue_a_head/…`` 与 ``a/yue_a_body/…``）就多留几段。
    """
    out = []
    for i, s in enumerate(dirs):
        need = 1
        for j, t in enumerate(dirs):
            if i == j:
                continue
            n = 0
            while n < min(len(s), len(t)) and s[n] == t[n]:
                n += 1
            need = max(need, n + 1)
        cut = s.find("/", need - 1)
        out.append(s[:cut] if cut > 0 else s)
    return out


def _merge_halves(variants: list[ModelVariant]) -> list[ModelVariant]:
    """把"只有一半"的两套合成一套。

    CC 打包产物是每个资产一个目录（``a/yue_a_head/…`` 与 ``a/yue_a_body/…``），
    按目录规则看它们是两套。这里只在**两个都只有一件、且槽位互补**时合并，
    不会碰正常的成套资产。
    """
    out: list[ModelVariant] = []
    for v in variants:
        mate = None
        for w in out:
            if w.label != v.label or len(w.parts) != 1 or len(v.parts) != 1:
                continue
            if set(w.parts) != set(v.parts):
                mate = w
                break
        if mate is None:
            out.append(v)
            continue
        mate.parts.update(v.parts)
    return out


def resolve_model_ref(ref: str, mod_dir: Path, by_rel: dict, by_name: dict,
                      prefer_dir: Path | None = None) -> Path | None:
    """把 macro 里的 ``ref`` 落到磁盘上的 .xac。

    ref 写的是**打包后**的路径（``extensions/x4_yueqingshu_mod/assets/…``），
    而 mod 工程目录名常常不是那个名字（``x4_yue_argon_add``），所以依次尝试：
    原路径 -> 去掉 ``extensions/<包名>/`` -> 相对路径尾部匹配 -> 只按文件名。
    尾部匹配可能命中同一资产的多个副本（一个 mod 复制到了好几个目录），这时优先
    取 **macro 自己所在的那份**（``prefer_dir``），否则取路径最短的。
    """
    r = (ref or "").strip().replace("\\", "/").strip("/")
    if r.lower() in _EMPTY_REFS:
        return None
    cands = [r]
    if r.lower().startswith("extensions/"):
        bits = r.split("/", 2)
        if len(bits) == 3:
            cands.append(bits[2])
    for c in cands:
        rel = c if c.lower().endswith(".xac") else c + ".xac"
        direct = mod_dir / rel
        if direct.is_file():
            return direct
        if rel.lower() in by_rel:
            return by_rel[rel.lower()]
        tail = "/" + rel.lower()
        hits = [(key, path) for key, path in by_rel.items() if key.endswith(tail)]
        if hits:
            if prefer_dir is not None:
                for _key, path in hits:
                    try:
                        if path.is_relative_to(prefer_dir):
                            return path
                    except (OSError, ValueError):
                        pass
            return min(hits, key=lambda kv: len(kv[0]))[1]
    # 路径都对不上（工程目录被改过名、ref 只写了一半）时最后才按文件名兜底：
    # 同一份资产被复制到多个目录时可能选错，但总比整块模型消失强
    for c in cands:
        stem = Path(c if c.lower().endswith(".xac") else c + ".xac").stem.lower()
        if stem in by_name:
            return by_name[stem]
    return None


def iter_macro_models(mod_dir: Path):
    """产出 ``(xml 路径, macro 名, [(槽位, ref), …])``——mod 里每条 macro 的模型清单。

    只读 ``character_macros.xml``（找不到才退化成扫 mod 里所有 .xml）：mod 的
    xml 也可能是 ``content.xml`` 这种，全读一遍既慢又容易误命中。
    """
    files = list(iter_files(mod_dir, ("character_macros.xml",), max_depth=6))
    if not files:
        files = list(iter_files(mod_dir, (".xml",), max_depth=6))
    for f in sorted(files):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "<macro" not in text.lower():
            continue
        for m in _MACRO_BLOCK_RE.finditer(text):
            name = dict(_XML_ATTR_RE.findall(m.group(1) or "")).get("name") or f.stem
            parts: list[tuple[str, str]] = []
            for tag in _MODEL_TAG_RE.finditer(m.group(2) or ""):
                a = dict(_XML_ATTR_RE.findall(tag.group(1)))
                ref = (a.get("ref") or "").strip()
                if ref.lower() not in _EMPTY_REFS:
                    parts.append(((a.get("type") or "?").lower(), ref))
            if parts:
                yield f, name, parts


def macro_label(name: str) -> str:
    """``character_argon_female_yue_a_macro`` -> ``yue_a``。"""
    s = name
    if s.lower().endswith("_macro"):
        s = s[:-len("_macro")]
    if s.lower().startswith("character_"):
        s = s[len("character_"):]
    for race in ("argon", "terran", "teladi", "paranid", "split", "boron"):
        if s.lower().startswith(race):
            s = s[len(race):].lstrip("_")
            break
    for sex in ("female", "male"):
        if s.lower().startswith(sex):
            s = s[len(sex):].lstrip("_")
            break
    return s or name


def macro_model_variants(mod_dir: Path, files: list[Path]) -> list[ModelVariant]:
    """按 macro 的 ``<models>`` 分组，每组一套模型。"""
    by_rel: dict[str, Path] = {}
    by_name: dict[str, Path] = {}
    for p in files:
        by_name.setdefault(p.stem.lower(), p)
        try:
            rel = p.relative_to(mod_dir).as_posix().lower()
        except ValueError:
            rel = p.name.lower()
        by_rel.setdefault(rel, p)

    out: list[ModelVariant] = []
    seen: set[tuple] = set()
    for xml_path, name, parts in iter_macro_models(mod_dir):
        got: dict[str, Path] = {}
        for slot, ref in parts:
            p = resolve_model_ref(ref, mod_dir, by_rel, by_name,
                                  prefer_dir=xml_path.parent.parent)
            if p is not None:
                got.setdefault(slot, p)
        if not got:
            continue
        fingerprint = tuple(sorted(str(p) for p in got.values()))
        if fingerprint in seen:      # 同一个资产被多条 macro 引用（add + replace）
            continue
        seen.add(fingerprint)
        out.append(ModelVariant(key=name, label=macro_label(name), parts=got))
    return out


def group_model_variants(files: list[Path]) -> list[ModelVariant]:
    """没有 macro 信息时，按文件名前缀把 head / torso 凑成一套。"""
    groups: list[dict] = []
    others: list[Path] = []
    for p in sorted(files):
        slot, prefix = split_slot(p.stem)
        if slot is None:
            others.append(p)
            continue
        target = None
        for g in groups:
            a, b = g["prefix"].lower(), prefix.lower()
            if not (a == b or a.startswith(b) or b.startswith(a)):
                continue
            if _model_dir(g["path"]) != _model_dir(p):
                continue             # 前缀相同但在不同模型目录：不是同一套
            target = g
            if len(prefix) < len(g["prefix"]):
                g["prefix"] = prefix  # 收短成更一般的前缀（重叠取公共部分）
            break
        if target is None:
            target = {"prefix": prefix, "path": p, "parts": {}, "extra": 0}
            groups.append(target)
        if slot in target["parts"]:
            # 同一前缀下同槽位的第二件（比如两种发型）：另起一套，
            # 既不叠着渲染，也不至于在列表里看不到
            target["extra"] += 1
            groups.append({"prefix": f"{target['prefix']} #{target['extra'] + 1}",
                           "path": p, "parts": {slot: p}, "extra": 0})
        else:
            target["parts"][slot] = p

    out = [ModelVariant(key=f"group:{g['prefix']}", label=g["prefix"], parts=g["parts"],
                        origin="group")
           for g in groups if g["parts"]]
    if others:
        # 认不出槽位的资产（比如自己拼的探针网格）打包成一套，
        # 这样它们至少还能被看到，而不是从列表里消失
        out.append(ModelVariant(key="group:*others", label="其他",
                                parts={f"other{i}": p for i, p in enumerate(others)},
                                origin="others"))
    return out


def mod_variants(mod_dir: Path) -> list[ModelVariant]:
    """列出 mod 目录里能预览的**全部**模型（多套时全列出来）。

    优先用 macro 的 ``<models>``（和游戏真正加载的一致）；macro 认不出来时按
    文件名前缀分组。macro 没引用到的 .xac 也补在后面，免得被藏起来。
    """
    mod_dir = Path(mod_dir)
    if not mod_dir.is_dir():
        return []
    files = sorted(iter_files(mod_dir, (".xac",)))
    if not files:
        return []
    out = macro_model_variants(mod_dir, files)
    if out:
        used = {p.resolve() for v in out for p in v.files}
        extra = [p for p in files if p.resolve() not in used]
        if extra:
            out = out + group_model_variants(extra)
    else:
        out = group_model_variants(files)
    return _tidy_variants(_merge_halves(out), mod_dir)


def _tidy_variants(variants: list[ModelVariant], mod_dir: Path) -> list[ModelVariant]:
    """重名的加目录提示、key 去重，保证列表里能分辨、config 里能对上。"""
    by_label: dict[str, list[ModelVariant]] = {}
    for v in variants:
        by_label.setdefault(v.label, []).append(v)
    for group in by_label.values():
        if len(group) < 2:
            continue
        hints = _unique_hints([_dir_hint(v.files[0], mod_dir) for v in group])
        for v, h in zip(group, hints):
            v.hint = h
    used: set[str] = set()
    for i, v in enumerate(variants):
        if v.key in used:
            v.key = f"{v.key}#{i}"
        used.add(v.key)
    return variants


def match_variants(want: list[ModelVariant], others: list[ModelVariant]) -> list[ModelVariant]:
    """在另一个 mod 里找出与 ``want`` 对应的那几套，用来并排对比。

    迭代时最有用的是"这一版的 A 对上一版的 A"：先按 macro 名/key 对，再按显示名、
    再按不带目录提示的短名，全对不上就退回对方的第一套。
    """
    if not others:
        return []
    keys = {v.key for v in want}
    if keys:
        hit = [v for v in others if v.key in keys]
        if hit:
            return hit
    names = {v.name() for v in want}
    if names:
        hit = [v for v in others if v.name() in names]
        if hit:
            return hit
    labels = {v.label for v in want}
    if labels:
        hit = [v for v in others if v.label in labels]
        if hit:
            return hit
    return others[:1]


def load_mod_sources(mod_dir: Path, limit: int | None = None, variant: str | None = None):
    """挑出要加载的资产，返回 ``[(文件名, 字节)]``。

    默认是第一套模型（head + torso + …），``variant`` 可以指名另一套
    （给 :class:`ModelVariant` 的 ``key`` 或 ``label``）。认不出任何槽位时
    退化成前 ``limit``（默认 2）个 .xac。
    """
    mod_dir = Path(mod_dir)
    variants = mod_variants(mod_dir)
    if variants:
        pick = variants[0]
        if variant:
            for v in variants:
                if variant in (v.key, v.label):
                    pick = v
                    break
        files = pick.files
        if limit:
            files = files[:limit]
        return [(p.name, p.read_bytes()) for p in files if p.is_file()]
    picked = [p for p, _ in mod_asset_options(mod_dir)[:limit or 2]]
    return [(p.name, p.read_bytes()) for p in picked]


def has_pack(path: Path) -> bool:
    """目录里是不是"打包但没散装"的 mod（``ext_01.cat`` + ``ext_01.dat``）。

    游戏 ``extensions/`` 下装好的 mod 大多是这个形态，assets 全在 ``.dat`` 里，
    按"有没有 .xac"去找会把它们整个漏掉（实测：装了 mod 却不在列表里）。
    签名包 ``ext_01_sig.cat`` 不算——那里面只有一堆 md5。
    """
    return bool(modpack.asset_cats(path))


def discover_mod_dirs(extra_roots: list[Path] | None = None) -> list[tuple[str, Path]]:
    """找出**工作区**里"可能是 mod"的目录，返回 ``[(标签, 路径)]``。

    只扫工作区根（本工具所在目录的上一级，例如 ``D:\\dsh-x4``）下的各工程输出：
    那里是你要看的东西，数量有限、启动快。

    **不扫游戏的 ``extensions/``**：那里面"人多眼杂"——官方 DLC、工具类 mod、
    别人做的 mod 全在一起，几十个条目里绝大多数跟人物动作无关，既拖慢启动又刷屏。
    要预览游戏里装好的 mod，用「浏览…」自己指过去就行；确实想让它常驻列表，
    把那个目录写进 ``config.json`` 的 ``mod_roots``（见 ``config.example.json``）。

    打包安装（只有 ``.cat``/``.dat``）的也列出来，标签带"包"字——选它时会自动摊开
    （见 :mod:`modpack`）。
    """
    found: dict[Path, str] = {}

    def add(path: Path, label: str):
        try:
            path = path.resolve()
        except OSError:
            return
        if not path.is_dir() or path in found:
            return
        loose = any(iter_files(path, (".xac",), max_depth=6, budget_s=0.5))
        packed = has_pack(path)
        if not loose and not packed:
            return
        # 只有"打包且没散装"的才标"·包"：工程输出目录通常既有 assets 又有
        # ext_01.cat，那种是散装目录，读的时候根本不会去解包
        found[path] = f"{label}·包" if packed and not loose else label

    roots = [PROJECT_ROOT.parent]              # 工作区根（D:\dsh-x4）
    roots.extend(extra_roots or [])
    for root in roots:
        if not root.is_dir():
            continue
        if root.resolve() == PROJECT_ROOT.parent.resolve():
            # 工作区：各工程的输出目录埋在好几层里，递归找名字像 mod 的目录
            for sub in sorted(root.iterdir()):
                if not sub.is_dir() or sub.name.startswith("."):
                    continue
                for cand in sorted(iter_files(sub, (), max_depth=5, budget_s=1.5,
                                              want_dirs=True)):
                    low = cand.name.lower()
                    if any(k in low for k in ("x4_", "x4-", "_mod", "mod_")):
                        rel = cand.relative_to(sub).as_posix()
                        rel = rel if len(rel) <= 34 else "…" + rel[-33:]
                        add(cand, f"[{sub.name}] {rel}")
        else:
            # 额外根（config 的 mod_roots）：当成"装 mod 的目录"，每个子目录就是一个 mod
            for sub in sorted(root.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    add(sub, f"[{root.name}] {sub.name}")

    return [(label, path) for path, label in sorted(found.items(), key=lambda kv: kv[1])]


# ---------------------------------------------------------------------------
# 材质规则（复刻游戏的渲染方式）
# ---------------------------------------------------------------------------

#: 默认的 alpha 裁剪阈值。取自游戏 shader 的硬编码值：
#: ``shadergl/glsl/p1/high/hair.frag.glsl`` 里写着 ``if (ColorBaseDiffuse.a < 0.5f) discard;``
ALPHA_TEST_DEFAULT = 0.5

#: 需要 alpha 裁剪的 shader。``p1_character`` 的 frag shader 里**没有任何 discard**，
#: 所以皮肤/衣服这类材质不该裁剪——之前对全部材质一律裁剪是错的。
ALPHA_TEST_SHADERS = {"p1_hair", "p1_hair_paint"}

#: 这些 blendmode 走 1-bit alpha（引擎侧设 alpha test），阈值沿用 0.5
ALPHA_TEST_BLENDMODES = {"ALPHA1", "ALPHA1NOG", "ALPHA8", "ALPHA8_SINGLE", "PREALPHA8",
                         "ALPHA8_ANARK", "ALPHA8_OVERLAY", "ALPHA8_GBLEND"}


@dataclass(frozen=True)
class MaterialRule:
    """一个材质该怎么画。"""

    shader: str = ""
    blendmode: str = "NONE"

    @property
    def two_sided(self) -> bool:
        return self.blendmode in ("TWOSIDED", "HAIR") or self.shader in ALPHA_TEST_SHADERS

    @property
    def alpha_test(self) -> float | None:
        """返回裁剪阈值；``None`` 表示不做 alpha 裁剪（不透明材质）。"""
        if self.shader in ALPHA_TEST_SHADERS:
            return ALPHA_TEST_DEFAULT
        if self.blendmode in ALPHA_TEST_BLENDMODES:
            return ALPHA_TEST_DEFAULT
        return None


class MaterialRules:
    """从 mod 的 ``libraries/material_library.xml`` 读每个材质怎么渲染。

    实测同一套资产里既有 ``p1_hair``（alpha<0.5 裁剪）也有 ``p1_character``
    （完全不裁剪），还有大量 ``TWOSIDED``。一律按同一个阈值裁剪，
    半透明的发丝边缘会被留下来，看起来就是"白棕相间"的噪点。
    """

    def __init__(self, roots: list[Path]):
        self.rules: dict[str, MaterialRule] = {}
        for root in roots:
            root = Path(root)
            if not root.is_dir():
                continue
            for f in sorted(iter_files(root, ("material_library.xml",), max_depth=5)):
                self._parse(f)

    def _parse(self, path: Path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for m in re.finditer(r'<material\s+name="([^"]+)"([^>]*)>', text):
            name, attrs = m.group(1), m.group(2)
            sh = re.search(r'shader="([^"]+)"', attrs)
            bm = re.search(r'blendmode="([^"]+)"', attrs)
            self.rules[name.lower()] = MaterialRule(
                shader=sh.group(1) if sh else "", blendmode=bm.group(1) if bm else "NONE")

    def __len__(self):
        return len(self.rules)

    def get(self, material: str) -> MaterialRule:
        """材质名可能是 ``boru.hair``，而 xml 里写的是 ``hair``。"""
        key = material.strip().lower()
        rule = self.rules.get(key)
        if rule is None and "." in key:
            rule = self.rules.get(key.rsplit(".", 1)[-1])
        return rule or MaterialRule()


@dataclass
class SurfaceTex:
    """一个材质最终要怎么画：贴图 + 裁剪规则。"""

    image: "object" = None
    alpha_test: float | None = None
    two_sided: bool = False


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
            for f in sorted(iter_files(root, (), max_depth=6)):
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

    def __init__(self, label: str, sources: list[tuple[str, bytes]], delta: bool = False,
                 textures: "TextureSet | None" = None,
                 material_rules: "MaterialRules | None" = None):
        self.label = label
        self.paths: list[str] = []
        self.assets: list[xac.Asset] = []
        self.rigs: list[rig_mod.Rig] = []
        self.delta = delta
        self.texture_set = textures
        self.material_rules = material_rules
        self._part_tex: list | None = None
        self.anim: xsm.Xsm | None = None
        self.anim_name = "-"
        self.kept_sources: list[tuple[str, bytes]] = []
        for name, raw in sources:
            asset = xac.load_xac(name, raw)
            if not asset.meshes:
                continue
            self.paths.append(name)
            self.kept_sources.append((name, raw))
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

    def part_textures(self) -> list | None:
        """与 :meth:`pose` 返回的 parts **一一对应**的贴图表。

        每个 part 一张表（``{material_id: RGBA}``），因为 head 和 torso 是两套独立
        的材质表，共用一个 dict 会互相查不到——这正是之前"衣服没贴图"的成因。
        没有贴图时返回 ``None``。
        """
        if self.texture_set is None or not len(self.texture_set):
            return None
        if self._part_tex is None:
            out = []
            for asset in self.assets:
                mapping = self.texture_set.for_asset(asset)
                rules = self.material_rules
                surfaces = {}
                for mid, img in mapping.items():
                    name = asset.materials[mid] if mid < len(asset.materials) else ""
                    rule = rules.get(name) if rules is not None else MaterialRule()
                    surfaces[mid] = SurfaceTex(image=img, alpha_test=rule.alpha_test,
                                               two_sided=rule.two_sided)
                for mesh in asset.meshes:
                    if mesh.vertex_count == 0:
                        continue
                    out.append(surfaces)
            self._part_tex = out
        return self._part_tex

    def texture_stats(self) -> tuple[int, int]:
        """``(有贴图的 part 数, 总 part 数)``。"""
        tex = self.part_textures()
        if tex is None:
            return 0, 0
        return sum(1 for m in tex if m), len(tex)

    def info(self) -> str:
        return (f"{len(self.assets)} 个网格 / {self.vertex_count} 顶点 / "
                f"动画匹配 {self.matched():.0%}")
