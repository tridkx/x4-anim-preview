# -*- coding: utf-8 -*-
"""控制台编码：把 stdout/stderr 固定成 UTF-8。

Windows 中文环境默认代码页是 GBK，脚本里出现 ``✓`` 之类的字符会直接
``UnicodeEncodeError`` 崩掉（不只是显示乱码）。所有命令行入口先调一次
:func:`setup_console` 就能避免。``pythonw.exe`` 下没有控制台、``sys.stdout``
是 ``None``，这里一并兜住。
"""

from __future__ import annotations

import sys


def setup_console():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


setup_console()
