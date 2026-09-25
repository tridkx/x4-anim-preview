# -*- coding: utf-8 -*-
"""骨架一致性校验：mod 的 `.xac` 骨架是否真的等于 vanilla。

X4 的 NPC 是"换网格、留骨架"：动画、look-at、挂点全挂在共享 component 上，
所以替换资产必须**带着逐字节相同的骨架**，否则实机就会出现"只有这个 mod
的角色动作怪"这类问题。

判据（按骨名配对，不按出现顺序——原版躯干里常见同一套骨重复多次）::

    共同骨名数 / mod 骨名数        覆盖率
    共同骨的 bind 位置最大偏差     < 1e-4 cm 才算一致
    共同骨的 bind 旋转最大偏差     < 0.01°   才算一致
    mod 多出来的骨                  只提示，不算错（引擎忽略多余的骨）

用法::

    python tools/skeleton_check.py --mod <mod目录或 .xac>
    python tools/skeleton_check.py --body <mod躯干.xac> --head <mod头.xac>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import console  # noqa: F401  (设置 UTF-8 控制台)
import x4game
import xac

#: macro 里 argon 女性实际引用的槽位资产（见 libraries/character_macros.xml）
SLOT_REFERENCE = {
    "head": "assets/characters/argon/heads/char_arg_f_dyn_blend_head.xac",
    "torso": "assets/characters/argon/bodies/char_arg_f_jacket_leggings_civ_01.xac",
}


def quat_angle_deg(q1, q2) -> float:
    a = np.asarray(q1, dtype=np.float64)
    b = np.asarray(q2, dtype=np.float64)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    d = abs(float(np.dot(a / na, b / nb)))
    return float(np.degrees(2.0 * np.arccos(min(d, 1.0))))


def compare(reference: xac.Asset, target: xac.Asset, label: str) -> dict:
    ref_by_name = {}
    for n in reference.nodes:
        if n.name.startswith("Bip01") or n.name.endswith(("Helper", "LookAt", "Nub")):
            ref_by_name.setdefault(n.name, n)
    tgt = [n for n in target.nodes if n.name in ref_by_name]
    missing = sorted(set(ref_by_name) - {n.name for n in target.nodes})

    if not tgt:
        print(f"  [{label}] 没有任何同名骨骼 —— 骨架完全对不上")
        return {"ok": False, "coverage": 0.0}

    pos = np.array([np.linalg.norm(np.asarray(n.position) - np.asarray(ref_by_name[n.name].position))
                    for n in tgt])
    rot = np.array([quat_angle_deg(n.rotation, ref_by_name[n.name].rotation) for n in tgt])
    scl = np.array([np.linalg.norm(np.asarray(n.scale) - np.asarray(ref_by_name[n.name].scale))
                    for n in tgt])
    parents = sum(1 for n in tgt if n.parent_id != ref_by_name[n.name].parent_id)

    i = int(np.argmax(pos))
    j = int(np.argmax(rot))
    ok = bool(pos.max() < 1e-4 and rot.max() < 0.01 and scl.max() < 1e-4 and parents == 0)

    print(f"  [{label}]")
    print(f"    共同骨 {len(tgt)} / 参考骨 {len(ref_by_name)}"
          + (f"，缺失 {len(missing)}: {missing[:6]}" if missing else ""))
    print(f"    bind 位置最大偏差 {pos.max():.5f} cm   (平均 {pos.mean():.5f})"
          + (f"  <- {tgt[i].name}" if pos.max() > 1e-4 else ""))
    print(f"    bind 旋转最大偏差 {rot.max():.5f}°    (平均 {rot.mean():.5f})"
          + (f"  <- {tgt[j].name}" if rot.max() > 0.01 else ""))
    print(f"    bind 缩放最大偏差 {scl.max():.5f}     父节点不一致 {parents} 根")
    print(f"    结论: {'一致 ✓' if ok else '不一致 ✗ —— 动画/挂点可能与实机不符'}")
    return {"ok": ok, "coverage": len(tgt) / max(len(ref_by_name), 1)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="X4 骨架一致性校验")
    ap.add_argument("--mod", help="mod 目录或单个 .xac")
    ap.add_argument("--body", nargs="*", help="mod 躯干 .xac")
    ap.add_argument("--head", nargs="*", help="mod 头部 .xac")
    args = ap.parse_args(argv)

    game = x4game.GameArchive()
    targets: list[tuple[str, Path]] = []
    if args.mod:
        p = Path(args.mod)
        if p.is_file():
            targets.append((p.name, p))
        else:
            for f in sorted(p.rglob("*.xac")):
                low = f.name.lower()
                slot = "head" if "head" in low else ("torso" if "body" in low or "jacket" in low else None)
                if slot:
                    targets.append((slot, f))
    for kind, lst in (("head", args.head), ("torso", args.body)):
        for x in (lst or []):
            targets.append((kind, Path(x)))

    if not targets:
        raise SystemExit("用 --mod 或 --body/--head 指定要比对的资产")

    all_ok = True
    for slot, path in targets:
        if slot not in SLOT_REFERENCE:
            print(f"  [{path.name}] 认不出槽位（文件名里要有 head/body/jacket），跳过")
            continue
        ref_path = SLOT_REFERENCE[slot]
        raw = game.read(ref_path)
        if raw is None:
            print(f"  参考资产缺失: {ref_path}")
            continue
        print(f"== {path.name}  (对照 vanilla {slot}: {Path(ref_path).name})")
        res = compare(xac.load_xac(ref_path, raw), xac.load_xac(str(path)), slot)
        all_ok = all_ok and res.get("ok", False)

    print("\n总判定:", "全部一致 ✓" if all_ok else "存在不一致 ✗")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
