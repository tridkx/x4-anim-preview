# -*- coding: utf-8 -*-
"""离线核对："mod 里有多套模型"能不能全部认出来，以及打包形态能不能摊开。

对着磁盘上现有的 mod 目录跑，不需要游戏、不开窗口::

    python examples/variants_check.py [mod目录 …]

不给参数时自动发现机器上的 mod 目录，逐个列出识别到的模型套数。
还会临时搭一个"两套装扮 + 一条 macro"的假 mod，验证 macro 分组这条路径，
以及把假 mod 打成 ``.cat``/``.dat`` 后能不能照样认全（含贴图超限只摊网格）。
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import console  # noqa: F401  (设置 UTF-8 控制台)
import modpack
import scene as scene_mod

FAKE_MACRO = """<?xml version="1.0" encoding="utf-8"?>
<diff>
  <add sel="/macros">
    <macro name="character_argon_female_yue_a_macro" class="npc"
           ref="character_argon_female_cau_base_01_macro">
      <component ref="character_argon_female_01" />
      <properties>
        <models>
          <model type="head"  ref="extensions/x4_yueqingshu_mod/assets/characters/argon/yueqingshu/heads/yue_a_head" />
          <model type="torso" ref="extensions/x4_yueqingshu_mod/assets/characters/argon/yueqingshu/bodies/yue_a_body" />
          <model type="props" ref="none" />
        </models>
      </properties>
    </macro>
    <macro name="character_argon_female_yue_b_macro" class="npc"
           ref="character_argon_female_cau_base_01_macro">
      <component ref="character_argon_female_01" />
      <properties>
        <models>
          <model type="head"  ref="extensions/x4_yueqingshu_mod/assets/characters/argon/yueqingshu/heads/yue_b_head" />
          <model type="torso" ref="extensions/x4_yueqingshu_mod/assets/characters/argon/yueqingshu/bodies/yue_b_body" />
          <model type="props" ref="none" />
        </models>
      </properties>
    </macro>
  </add>
  <replace sel="/macros/macro[@name='character_player_custom_f_cau_macro']/properties/models/model[@type='head']/@ref">extensions/x4_yueqingshu_mod/assets/characters/argon/yueqingshu/heads/yue_a_head</replace>
</diff>
"""


def donor_files() -> list[Path]:
    """找两件现成的 .xac 当素材（内容无所谓，这里只验证分组逻辑）。"""
    root = Path(__file__).resolve().parent.parent
    for cand in (root / "work" / "vanilla", root.parent / "work" / "vanilla",
                 root.parent / "shared" / "x4root" / "assets" / "characters" / "argon"):
        xs = sorted(cand.rglob("*.xac")) if cand.is_dir() else []
        if len(xs) >= 2:
            return [xs[0], xs[-1]]
    # 兜底：从工程上一级有界地扫一遍，随便找两件
    xs = sorted(scene_mod.iter_files(root.parent, (".xac",), max_depth=6, budget_s=4.0))
    if len(xs) >= 2:
        return [xs[0], xs[-1]]
    raise SystemExit("找不到任何 .xac 当测试素材")


def fake_mod(root: Path) -> Path:
    """搭一个两套装扮的假 mod（macro 里 ref 指向的是打包后的目录名）。"""
    donor = donor_files()
    mod = root / "x4_fake_argon_add"
    bodies = mod / "assets" / "characters" / "argon" / "fake" / "bodies"
    heads = mod / "assets" / "characters" / "argon" / "fake" / "heads"
    bodies.mkdir(parents=True)
    heads.mkdir(parents=True)
    for tag in ("a", "b"):
        shutil.copy(donor[0], bodies / f"fake_{tag}_body.xac")
        shutil.copy(donor[-1], heads / f"fake_{tag}_head.xac")
    lib = mod / "libraries"
    lib.mkdir()
    (lib / "character_macros.xml").write_text(
        FAKE_MACRO.replace("yueqingshu", "fake").replace("yue_", "fake_"),
        encoding="utf-8")
    # 一张"贴图"，用来验证超限时先放弃贴图那一条分支
    tex = mod / "assets" / "characters" / "argon" / "fake" / "textures"
    tex.mkdir(parents=True, exist_ok=True)
    (tex / "fake_head_d_diff.gz").write_bytes(b"\x1f\x8b" + b"0" * 4096)
    return mod


def pack_dir(src: Path, out_dir: Path) -> Path:
    """把一个目录打成 X4 的包：``ext_01.cat``（索引）+ ``ext_01.dat``（顺序拼接）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    cat, dat = out_dir / "ext_01.cat", out_dir / "ext_01.dat"
    lines, blob = [], bytearray()
    for p in sorted(x for x in src.rglob("*") if x.is_file()):
        data = p.read_bytes()
        rel = p.relative_to(src).as_posix()
        lines.append(f"{rel} {len(data)} {int(p.stat().st_mtime)} "
                     f"{hashlib.md5(data).hexdigest()}")
        blob += data
    cat.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    dat.write_bytes(bytes(blob))
    return cat


def check_packs(root: Path, fake: Path) -> None:
    """打包形态：摊开、认全两套、跳过签名包、超限先放弃贴图。"""
    packed = root / "packed"
    cat = pack_dir(fake, packed)
    # 签名包（ext_01_sig.cat）不是资产，不该被当成包
    shutil.copy(cat, packed / "ext_01_sig.cat")
    shutil.copy(cat.with_suffix(".dat"), packed / "ext_01_sig.dat")
    assert [c.name for c in modpack.asset_cats(packed)] == ["ext_01.cat"], \
        modpack.asset_cats(packed)

    out = modpack.unpack(cat, dest_root=root / "unpacked")
    labels = [v.label for v in scene_mod.mod_variants(out)]
    assert labels == ["fake_a", "fake_b"], labels
    assert (out / "assets/characters/argon/fake/heads/fake_a_head.xac").is_file()
    print("[ok] .cat/.dat 摊开后仍能认全两套（签名包已跳过）")

    # 同一个包，把上限压到"只够网格"：贴图要被跳过，网格照旧
    tight = root / "tight"
    core = sum(s for n, s, _o in modpack.cat_entries(cat)
               if modpack.wanted(n) and not n.lower().endswith(modpack.TEXTURE_SUFFIXES))
    modpack.extract_cat(cat, tight, max_bytes=core + 1)
    assert list(tight.rglob("*.xac")), "网格没摊出来"
    assert not list(tight.rglob("*.gz")), "贴图超限时应当跳过"
    print("[ok] 超过上限时只摊网格与 xml，贴图跳过")

    try:
        modpack.extract_cat(cat, root / "never", max_bytes=1)
        raise AssertionError("上限太小却没有报错")
    except ValueError as exc:
        assert "超过上限" in str(exc)
    print("[ok] 连网格都超限时明确报错，而不是默默写盘")


def report(mod_dir: Path) -> int:
    variants = scene_mod.mod_variants(mod_dir)
    print(f"\n{mod_dir}")
    print(f"  识别到 {len(variants)} 套模型")
    for v in variants:
        names = ", ".join(p.name for p in v.files)
        print(f"    [{v.origin:6s}] {v.display():<28} {names}")
    if not variants:
        print("    （没找到 .xac）")
        return 0
    covered = {p for v in variants for p in v.files}
    all_xac = set(scene_mod.iter_files(mod_dir, (".xac",)))
    missing = all_xac - covered
    if missing:
        print(f"    !! 有 {len(missing)} 个 .xac 没被任何一套模型覆盖: "
              + ", ".join(sorted(p.name for p in missing)))
        return 1
    return 0


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    bad = 0
    if args:
        for a in args:
            bad += report(Path(a))
        return 1 if bad else 0

    tmp = Path(tempfile.mkdtemp(prefix="x4var_"))
    try:
        fake = fake_mod(tmp)
        bad += report(fake)
        # macro 分组：应当正好是两套，且各自 head+torso 齐全
        variants = scene_mod.mod_variants(fake)
        assert len(variants) == 2, variants
        assert [v.label for v in variants] == ["fake_a", "fake_b"], [v.label for v in variants]
        for v in variants:
            assert v.slot_summary() == "head+torso", v.slot_summary()
        print("\n[ok] macro 分组：两套装扮各自成套")

        # 没有 macro 时退化成按文件名前缀分组
        for extra in (fake / "libraries").glob("*.xml"):
            extra.unlink()
        variants = scene_mod.mod_variants(fake)
        assert [v.label for v in variants] == ["fake_a", "fake_b"], [v.label for v in variants]
        print("[ok] 无 macro 时按前缀分组：fake_a / fake_b 仍然分开")

        # 并排对比时"这一版的 A 对上一版的 A"
        a, b = variants
        other = scene_mod.ModelVariant("character_argon_female_fake_b_macro", "fake_b",
                                       {"head": b.parts["head"]})
        assert scene_mod.match_variants([b], [a, other]) == [other]
        assert scene_mod.match_variants([a], [other]) == [other]      # 对不上就退回第一套
        assert scene_mod.match_variants([a, b], [a, b]) == [a, b]
        print("[ok] 对比配对：按名字对上、对不上退回第一套")

        # --variant 取值要认 key / label
        got = scene_mod.load_mod_sources(fake, variant="fake_b")
        assert got and all("fake_b" in name for name, _ in got), got
        print("[ok] load_mod_sources(variant=…) 取到指定那一套")

        check_packs(tmp, fake)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    try:
        import x4game
        game = x4game.GameArchive()
    except Exception as exc:                      # 没装游戏也能跑（只少了自动发现）
        print(f"\n[skip] 自动发现 mod 目录: {exc}")
        return 1 if bad else 0
    for _label, path in scene_mod.discover_mod_dirs(game):
        bad += report(path)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())