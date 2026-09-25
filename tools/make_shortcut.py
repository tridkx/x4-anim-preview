# -*- coding: utf-8 -*-
"""生成"双击即用"的 Windows 快捷方式（.lnk）。

为什么不直接双击 .bat / .py：

- ``.bat`` 必须是 CRLF 行尾，而且它的文件关联容易被别的软件抢走；
- ``.py`` 的关联在很多机器上被 Visual Studio 接管，双击会打开 IDE 而不是运行脚本。

快捷方式直接指向 ``pythonw.exe``，绕开所有文件关联问题，也不会弹出黑色控制台窗口。

用法::

    python tools/make_shortcut.py              # 在项目根生成
    python tools/make_shortcut.py --desktop    # 同时放到桌面
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import console  # noqa: F401  (设置 UTF-8 控制台)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STUDIO = PROJECT_ROOT / "tools" / "studio.py"


def desktop_dir() -> Path:
    """取真实的桌面路径（可能被 OneDrive 重定向，所以问系统而不是拼路径）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        p = Path(out.stdout.strip())
        if p.is_dir():
            return p
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / "Desktop"


def python_launcher() -> Path:
    """优先用 pythonw.exe（无控制台窗口）。"""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def make_shortcut(dest: Path, target: Path, args: str, workdir: Path, desc: str) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{dest}'); "
        f"$sc.TargetPath = '{target}'; "
        f"$sc.Arguments = '{args}'; "
        f"$sc.WorkingDirectory = '{workdir}'; "
        f"$sc.Description = '{desc}'; "
        "$sc.IconLocation = '%%SystemRoot%%\\System32\\shell32.dll,137'; "
        "$sc.Save()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=60, check=True)
        return dest.is_file()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  生成失败: {exc}")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成 X4 动画预览器的快捷方式")
    ap.add_argument("--desktop", action="store_true", help="同时在桌面放一个")
    ap.add_argument("--name", default="X4 动画预览器", help="快捷方式名字")
    args = ap.parse_args(argv)

    target = python_launcher()
    if not STUDIO.is_file():
        print(f"找不到 {STUDIO}")
        return 1

    print(f"python : {target}")
    print(f"脚本   : {STUDIO}")

    places = [PROJECT_ROOT / f"{args.name}.lnk"]
    if args.desktop:
        places.append(desktop_dir() / f"{args.name}.lnk")

    ok = True
    for dest in places:
        made = make_shortcut(dest, target, f'"{STUDIO}"', PROJECT_ROOT,
                             "X4 角色动画预览器")
        print(f"  {'✓' if made else '✗'} {dest}")
        ok = ok and made

    if ok:
        print("\n双击快捷方式即可启动，不依赖 .bat / .py 的文件关联。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
