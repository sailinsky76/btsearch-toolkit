#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btcompat —— 跨平台兼容层（主要是给 Windows 擦屁股）

这套工具原本在 Linux 上写的，搬到 Windows 有三个地方会真出问题，都在这里统一处理。

一、控制台编码
    中文版 Windows 的默认代码页是 GBK(cp936)。程序输出到控制台时 Python 会走
    Unicode 控制台 API，不会崩；但一旦输出被重定向到文件或管道——计划任务恰恰就是
    这么干的——Python 改用 GBK 编码，遇到 ✓ ✗ ⚠ 这类字符直接 UnicodeEncodeError，
    整个任务当场挂掉，而且日志里只留半行。
    setup_console() 把标准输出改成 UTF-8，并顺手把控制台代码页也设成 UTF-8。

二、SQLite 的 URI 文件名
    只读打开数据库要用 "file:路径?mode=ro" 这种 URI。Windows 路径是
    C:\\Users\\me\\bt.db，反斜杠和盘符冒号在 URI 里都不合法，直接拼会打不开。
    db_uri() 用 pathlib 的 as_uri() 生成，两个平台都对。

三、SO_REUSEADDR 的语义差异
    Linux 上它表示「允许复用 TIME_WAIT 状态的端口」，是常规做法。
    Windows 上它表示「允许另一个进程抢占已被占用的端口」——两个嗅探实例会同时
    绑上同一个端口，流量随机分给其中一个，症状是收获莫名其妙减半，还极难排查。
    所以 Windows 上不能开它，要开的是 SO_EXCLUSIVEADDRUSE。

用法：每个可执行脚本在 main() 开头调一次 setup_console() 就行。
"""

import os
import socket
import sys

BUILD = "2026-09-13-a"          # 版本戳，用来确认跑的是哪一版
IS_WINDOWS = os.name == "nt"


def setup_console():
    """把标准输出/错误改成 UTF-8。重定向到文件时这一步是必须的。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            # errors="replace" 是兜底：万一还有编不出来的字符，
            # 显示成问号也比整个任务崩掉强
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass       # 设不成就算了，上面的 reconfigure 已经保住了不崩


def dwidth(text) -> int:
    """终端显示宽度。中文是双宽字符，用 len() 排版一定会歪。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def pad(text, width, right=False) -> str:
    """按显示宽度补空格，中英混排的表格才对得齐。"""
    text = str(text)
    space = " " * max(width - dwidth(text), 0)
    return space + text if right else text + space


def clip(text, width) -> str:
    """按显示宽度截断，超出的用省略号。"""
    text = str(text)
    if dwidth(text) <= width:
        return text
    out = ""
    for ch in text:
        if dwidth(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


def py_cmd() -> str:
    """
    提示信息里该写哪种 Python 调用方式。
    Windows 上很多人没把 python 加进 PATH（官方安装包默认就不加），
    但 py 启动器一定有，所以直接给出带版本号的形式，复制粘贴就能用。
    """
    if IS_WINDOWS:
        return "py -%d.%d" % sys.version_info[:2]
    return "python3"


def db_uri(path, readonly=False) -> str:
    """
    生成 SQLite 能认的 URI 文件名。
    Linux: file:///home/me/bt.db
    Windows: file:///C:/Users/me/bt.db
    pathlib 的 as_uri() 会处理盘符、反斜杠和百分号转义，比手工拼字符串可靠。
    """
    from pathlib import Path
    uri = Path(path).absolute().as_uri()
    return uri + "?mode=ro" if readonly else uri


def bind_udp(port, host="0.0.0.0", rcvbuf=4 * 1024 * 1024):
    """
    绑一个 UDP 端口，端口复用选项按平台区别对待（原因见文件头第三条）。
    绑不上会抛 OSError，交给调用方决定怎么办。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if IS_WINDOWS:
        # 明确要求独占，防止另一个实例悄悄绑上同一个端口分走流量
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except (AttributeError, OSError):
            pass
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    if rcvbuf:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        except OSError:
            pass
    sock.bind((host, port))
    return sock


def safe_exit_on_broken_pipe(func):
    """
    装饰 main()：接 head/less 时对方提前关掉管道，正常退出即可，别吐 traceback。
    Windows 上管道断开报的是 OSError(EINVAL)，一并接住。
    """
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except BrokenPipeError:
            try:
                sys.stdout.close()
            except OSError:
                pass
            os._exit(0)
    return wrapper
