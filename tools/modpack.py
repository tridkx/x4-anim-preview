# -*- coding: utf-8 -*-
"""把 mod 的三种形态统一成"一个目录"：散装目录 / ``.cat``+``.dat`` 包 / ``.zip`` 发布包。

预览器和检查工具只认**散装的目录**：按文件名找 ``.xac``、读 ``libraries/*.xml``
判断多套模型、按材质名找 ``.dds.gz`` 贴图。而 X4 的发布形态是
``<mod>/ext_01.cat``（文本索引：``名字 大小 时间 md5``）+ ``ext_01.dat``
（把所有文件按索引顺序拼在一起），玩家拿到的又是一个 ``.zip``，里面还裹着这一对。
所以把 ``.cat`` 或 ``.zip`` 指给预览器，它什么都找不到。

这里做的是"就地摊开"：解到 ``<工程根>/work/unpacked/<包名>/``，之后一切照普通
目录走（多套模型、贴图、材质规则全都能用）。同一个包第二次打开直接复用缓存，
源文件更新过才重新解。
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

import x4game

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 解包缓存放哪：工程内的 work/ 下（跟着工程走，gitignore 已忽略）
UNPACK_ROOT = PROJECT_ROOT / "work" / "unpacked"

#: 能被"摊开"的包后缀
ARCHIVE_SUFFIXES = (".cat", ".zip")

#: 预览真正会读的文件：``.xac`` 网格、``.xml``（macro / 材质规则）、贴图。
#: 整包照抄的话，官方 DLC 一个 ext_01.cat 就是 2 GB，其中大半是预览用不上的东西
#: （视频、音频、签名），所以只挑这几类出来。
WANTED_SUFFIXES = (".xac", ".xml", ".gz", ".dds")

#: 摊开的上限。官方 DLC 里光"贴图"就有 200~900 MB（tera 的 ext_01.cat 是 2 GB），
#: 一键解到磁盘上不合适。超过上限时**只摊网格与 xml**（那部分几十 MB），
#: 贴图跳过并说明原因——看姿态够用了，只是模型没颜色。
MAX_UNPACK_BYTES = 300 * 1024 * 1024

#: 贴图后缀（体积大头，超限时优先放弃它们）
TEXTURE_SUFFIXES = (".gz", ".dds")

#: 记录"这个目录是从哪个包、哪个版本解出来的"，用来判断缓存还能不能用
STAMP_NAME = ".unpacked_from"


def is_signature_cat(path) -> bool:
    """``ext_01_sig.cat`` 是签名清单（一堆 md5），不是资产包——别去摊它。"""
    return Path(path).stem.lower().endswith("_sig")


def asset_cats(root: Path, deep: bool = False) -> list[Path]:
    """目录里真正的资产包（``.cat`` 且有同名 ``.dat``，排除 ``_sig``）。

    ``deep=True`` 递归找：发布包解开后 ``.cat`` 在 ``<mod包名>/`` 子目录里，
    只看顶层会漏（漏了就是"zip 打开后一个模型都没有"）。
    """
    root = Path(root)
    try:
        cats = sorted(root.rglob("*.cat") if deep else root.glob("*.cat"))
    except OSError:
        return []
    return [c for c in cats
            if not is_signature_cat(c) and c.with_suffix(".dat").is_file()]


def wanted(name: str) -> bool:
    return name.lower().endswith(WANTED_SUFFIXES)


def archive_kind(path) -> str | None:
    """``"cat"`` / ``"zip"`` / ``None``（不是包）。"""
    p = Path(path)
    if not p.is_file():
        return None
    suffix = p.suffix.lower()
    if suffix == ".cat":
        return "cat"
    if suffix == ".zip":
        return "zip"
    return None


def _has_xac(root: Path) -> bool:
    for _ in root.rglob("*.xac"):
        return True
    return False


def _stamp(path: Path) -> str:
    st = path.stat()
    return f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}"


def _cache_ok(dest: Path, src: Path) -> bool:
    """缓存目录还在、且是用**当前这个版本**的包解出来的吗。"""
    stamp_file = dest / STAMP_NAME
    if not stamp_file.is_file():
        return False
    try:
        return stamp_file.read_text(encoding="utf-8").strip() == _stamp(src)
    except OSError:
        return False


def _write_stamp(dest: Path, src: Path):
    try:
        (dest / STAMP_NAME).write_text(_stamp(src), encoding="utf-8")
    except OSError:
        pass


def _cache_dir(src: Path, dest_root: Path, base: str) -> Path:
    """缓存目录：名字看得懂，又不会互相踩。

    mod 的包十有八九就叫 ``ext_01.cat``，只按包名当目录名的话，两个不同 mod
    会解到同一个目录里互相覆盖（实测过：先开 A 再开 B，A 的内容被 B 顶掉）。
    所以用"所在目录名_包名"，万一连这个都撞（不同盘的同名 mod），再加 6 位路径哈希。
    """
    dest = dest_root / base
    if dest.is_dir() and not _cache_ok(dest, src):
        recorded = ""
        try:
            recorded = (dest / STAMP_NAME).read_text(encoding="utf-8").split("|")[0]
        except OSError:
            pass
        if recorded and recorded != str(src.resolve()):
            digest = hashlib.md5(str(src.resolve()).encode("utf-8")).hexdigest()[:6]
            dest = dest_root / f"{base}-{digest}"
    return dest


def cat_entries(cat_path: Path):
    """``[(名字, 大小, 偏移), …]``——.cat 索引 + 在 .dat 里的累加偏移。"""
    out = []
    offset = 0
    for name, size, _mtime, _md5 in x4game.parse_cat(Path(cat_path)):
        out.append((name, size, offset))
        offset += size
    return out


def extract_cat(cat_path: Path, dest_root: Path, progress=None,
                max_bytes: int | None = MAX_UNPACK_BYTES) -> int:
    """把一对 ``.cat``/``.dat`` 摊到 ``dest_root`` 下，返回写出的文件数。

    只写预览用得上的那几类（见 :data:`WANTED_SUFFIXES`），并且**先网格后贴图**：
    网格 + xml 一起超过 ``max_bytes`` 就直接报错；只有贴图超限时跳过贴图照常摊开
    （官方 DLC 就属于后者：网格 50~110 MB，贴图 200~900 MB）。
    """
    cat_path = Path(cat_path)
    dat_path = cat_path.with_suffix(".dat")
    if not dat_path.is_file():
        raise FileNotFoundError(f"缺少数据文件 {dat_path.name}（.cat 只是索引）")
    all_entries = cat_entries(cat_path)
    core, tex = [], []
    for name, size, offset in all_entries:
        if not wanted(name):
            continue
        (tex if name.lower().endswith(TEXTURE_SUFFIXES) else core).append((name, size, offset))
    core_sz = sum(s for _n, s, _o in core)
    tex_sz = sum(s for _n, s, _o in tex)
    if max_bytes and core_sz > max_bytes:
        raise ValueError(
            f"{cat_path.name} 里光是网格与 xml 就有 {core_sz / 1e6:.0f} MB，"
            f"超过上限 {max_bytes / 1e6:.0f} MB ——这种大包请自己解包后指向目录")
    note = ""
    if max_bytes and core_sz + tex_sz > max_bytes:
        note = f"，贴图 {tex_sz / 1e6:.0f} MB 超上限先跳过（模型会没颜色）"
        tex = []
    dest_root.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(dat_path, "rb") as fh:
        for name, size, offset in sorted(core + tex, key=lambda e: e[2]):   # 按偏移顺读
            target = dest_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            fh.seek(offset)
            target.write_bytes(fh.read(size))
            written += 1
    if progress:
        skipped = len(all_entries) - written
        extra = f"，跳过 {skipped} 个用不上的" if skipped else ""
        progress(f"解包 {cat_path.name} -> {dest_root}（{written} 个文件{extra}{note}）")
    return written


def extract_zip(zip_path: Path, dest_root: Path, progress=None) -> int:
    """解开 ``.zip``（跳过越界的条目），返回写出的文件数。"""
    zip_path = Path(zip_path)
    dest_root.mkdir(parents=True, exist_ok=True)
    written = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                continue                      # 不信任压缩包里的相对路径
            target = dest_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written += 1
    if progress:
        progress(f"解压 {zip_path.name} -> {dest_root}（{written} 个文件）")
    return written


def _unpack_into(src: Path, dest: Path, progress=None, force: bool = False) -> Path:
    """把 ``src``（.cat 或 .zip）摊到 ``dest``，必要时连里面的 .cat 一起摊。"""
    kind = archive_kind(src)
    if _cache_ok(dest, src) and not force:
        return dest
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    if kind == "cat":
        extract_cat(src, dest, progress=progress)
    else:
        extract_zip(src, dest, progress=progress)
        # 发布包里裹着的 ext_01.cat/.dat（assets 全在 .dat 里），接着摊开。
        # 它可能在 <mod包名>/ 子目录里，所以要递归找。
        for cat in asset_cats(dest, deep=True):
            if not _has_xac(dest):
                extract_cat(cat, dest, progress=progress)
    _write_stamp(dest, src)
    return dest


def unpack(path, dest_root: Path | None = None, progress=None, force: bool = False) -> Path:
    """把"用户给的 mod 路径"变成一个可以预览的目录。

    三种情况：

    * 已经是散装目录（里面有 ``.xac``）—— 原样返回，什么都不做；
    * ``.cat`` / ``.zip`` —— 摊到 ``<工程根>/work/unpacked/<包名>/`` 再返回；
    * 目录里只有 ``ext_01.cat/.dat``（打包但没散装）—— 也摊开，但摊到缓存目录里，
      **不动用户的目录**。

    摊过一次就记下源文件的大小与时间戳，下次直接复用；源包更新了会自动重解。
    """
    src = Path(path)
    dest_root = Path(dest_root) if dest_root else UNPACK_ROOT
    if src.is_dir():
        if _has_xac(src):
            return src
        cats = asset_cats(src)
        if not cats:
            return src                       # 交给上层报"没找到 .xac"
        if len(cats) == 1:
            return _unpack_into(cats[0], _cache_dir(cats[0], dest_root,
                                                     f"{src.name}_{cats[0].stem}"),
                                progress=progress, force=force)
        dest = _cache_dir(cats[0], dest_root, src.name)
        if not force and all(_cache_ok(dest, c) for c in cats):
            return dest
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        for c in cats:
            extract_cat(c, dest, progress=progress)
        _write_stamp(dest, cats[0])
        return dest
    kind = archive_kind(src)
    if kind is None:
        hint = "（.dat 只是数据，要跟同名 .cat 一起给）" if src.suffix.lower() == ".dat" else ""
        raise ValueError(f"{src} 既不是目录，也不是 .cat / .zip 包{hint}")
    base = src.stem if kind == "zip" else f"{src.parent.name}_{src.stem}"
    return _unpack_into(src, _cache_dir(src, dest_root, base), progress=progress, force=force)


if __name__ == "__main__":
    import sys

    import console  # noqa: F401

    if len(sys.argv) < 2:
        raise SystemExit("用法: python tools/modpack.py <mod目录 | .cat | .zip>")
    target = Path(sys.argv[1])
    out = unpack(target, progress=print)
    print(f"目录: {out}")
    print(f"  .xac {len(list(out.rglob('*.xac')))} 个")