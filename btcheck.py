#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btcheck —— 环境自检

在正式开跑之前先跑一次，把「装完之后才发现跑不起来」的几种情况提前查出来：
Python 版本够不够、SQLite 带没带 FTS5、端口能不能绑、目录能不能写、控制台编码对不对。

用法：
    py -3.11 btcheck.py          （Windows 上双击 check.bat 也行）
"""

import ast
import builtins
import glob
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcompat import BUILD, IS_WINDOWS, bind_udp, py_cmd, setup_console
from btprune import pad

MIN_PY = (3, 8)
# 无正文 FTS（content='' + contentless_delete=1）是 SQLite 3.43 引进的，
# 而那正是 btindex.py 建库用的表结构——不够就一条种子也存不进去。
# 注意这跟 Python 版本不是一回事：同一个 3.11，小版本不同带的 SQLite 也不同。
MIN_SQLITE = (3, 43)
PORTS = range(6881, 6889)

results = []


def guard(name, fn, ok_msg, fail_prefix="", fatal=False):
    """
    跑一项检查，自己出意外也只算这一项失败，不能把整个自检带崩。
    自检工具崩掉比检查不通过更糟——用户连总结都看不到，只剩一段堆栈。
    """
    try:
        problems = fn()
    except Exception as e:
        return check(name, False, "检查本身出错：%s: %s" % (type(e).__name__, e),
                     fatal=False)
    if not problems:
        return check(name, True, ok_msg)
    detail = problems if isinstance(problems, str) else "；".join(
        (p if isinstance(p, str) else str(p)) for p in problems[:2])
    return check(name, False, fail_prefix + detail, fatal=fatal)


def check(name, ok, detail="", fatal=False, hint=""):
    results.append((name, ok, detail, fatal))
    mark = "OK  " if ok else ("失败" if fatal else "注意")
    print("  [%s] %s %s" % (mark, pad(name, 22), detail))
    # 提示只在没过的时候打。过了还印一行「怎么修」纯属噪音，
    # 而真出问题的人恰恰最需要下一步往哪走。
    if not ok and hint:
        print("         %s" % hint)
    return ok


def run_child(argv, cwd, timeout=30):
    """
    跑一个子进程并读它的输出。

    必须显式指定 encoding="utf-8"：Windows 上 text=True 会用系统区域编码
    （中文版是 GBK）去解码，而我们的脚本都把自己的输出设成了 UTF-8，
    两边对不上就在读取线程里抛 UnicodeDecodeError，
    结果 r.stdout 变成 None，外面一拼接就是 TypeError。
    """
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                       cwd=cwd, encoding="utf-8", errors="replace")
    return r, (r.stdout or "") + (r.stderr or "")


BUILTIN_NAMES = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "self", "cls"}


def _parse_case_count(here):
    """用例有多少条，只为了在 OK 那行显示个数。取不到就当 0，不让它影响自检。"""
    sys.path.insert(0, here)
    try:
        import btparse
        return len(btparse.CASES)
    except Exception:
        return 0


def _query_case_count(here):
    """同上，只为了显示个数。"""
    sys.path.insert(0, here)
    try:
        import btindex
        return (len(btindex.QUERY_CASES) + len(btindex.EXPAND_CASES)
                + len(btindex.NAME_CASES))
    except Exception:
        return 0


def check_query_rules(here):
    """
    分词和查询构造的用例。

    入库展开和查询展开是同一套规则的两头，错开一点就是「库里明明有、怎么都
    搜不出来」——而且不报任何错，只是结果少了。这一项和「名字解析规则」是一对：
    那边守的是「这条是什么」，这边守的是「这个词能不能搜到」。
    """
    sys.path.insert(0, here)
    try:
        import btindex
    except ImportError as e:
        return ["btindex 导不进来：%s" % e]
    return ["%s(%r)：期望 %r，实际 %r" % (where, src[:30], want, got)
            for where, src, want, got in btindex.selftest()]


def _visible_names(tree):
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                names.add(a.asname or a.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.Global):
            names.update(n.names)
    return names


def scan_undefined(here):
    """
    静态找「用了但没定义」的名字。
    这类问题只在真正跑到那一行时才会炸——比如某个函数里用了 dhtsniff.DHTSniffer
    但模块顶上只 from dhtsniff import 了几个函数，--help 完全看不出来，
    非得等爬虫启动才报 NameError。所以要静态扫。
    """
    bad = []
    for f in sorted(glob.glob(os.path.join(here, "*.py"))):
        try:
            with open(f, encoding="utf-8") as fp:
                tree = ast.parse(fp.read(), filename=f)
        except SyntaxError as e:
            bad.append((os.path.basename(f), e.lineno, "语法错误: %s" % e.msg))
            continue
        known = _visible_names(tree) | BUILTIN_NAMES
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known:
                bad.append((os.path.basename(f), n.lineno, n.id))
    return sorted(set(bad))


def scan_bat_flags(here):
    """
    交叉校验每个 .bat 传给 .py 的参数，是否真的存在于那个脚本里。
    脚本换了个版本、参数改了名，.bat 却还是老写法——这种事只有实际运行才会暴露，
    而且报错是 argparse 的 usage 一大堆，不容易一眼看懂。
    """
    # 起子进程读 --help 是这项检查里唯一的开销，而 14 个 .bat 里同一个脚本
    # 会被提到好几次（btweb、btmaint、btimport、btmigrate 各两次）。
    # 同一个 (脚本, 子命令) 的答案不会变，存下来就行
    seen = {}

    def flags(script, sub=None):
        key = (script, sub)
        if key in seen:
            return seen[key]
        cmd = [sys.executable, script] + ([sub] if sub else []) + ["--help"]
        try:
            _, out = run_child(cmd, here)
            got = set(re.findall(r"(--[a-z0-9][a-z0-9-]*)", out))
        except (OSError, subprocess.SubprocessError):
            got = set()
        seen[key] = got
        return got

    SUBS = ("search", "scan", "check", "analyze", "verify", "prune",
            "vacuum", "report", "import")
    problems = []
    for bat in sorted(glob.glob(os.path.join(here, "*.bat"))):
        try:
            with open(bat, encoding="ascii") as fp:
                text = fp.read()
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.replace("\r\n", "\n").split("\n"):
            m = re.search(r"%PY%\s+(\S+\.py)\s*(.*)", line)
            if not m:
                continue
            script, rest = m.group(1), m.group(2)
            if not os.path.exists(os.path.join(here, script)):
                problems.append((os.path.basename(bat), script, ["文件不存在"]))
                continue
            avail = flags(script)
            for cand in SUBS:
                if re.search(r"\b%s\b" % cand, rest):
                    avail |= flags(script, cand)
                    break
            missing = sorted(set(re.findall(r"(--[a-z0-9][a-z0-9-]*)", rest)) - avail)
            if missing:
                problems.append((os.path.basename(bat), script, missing))
    return problems


def check_parse_rules(here):
    """
    跑一遍名字解析的用例。

    这些规则是「名字长什么样」的经验，没有编译器帮着看，改一条正则很容易
    捎带打翻另一条——上一版里「合集」被当成剧集信号，结果音乐专辑合集
    一路被判成剧集，中间没有任何一步报错。用例跑得很快（几十条，毫秒级），
    放进自检里，改完规则至少有人吭声。
    """
    sys.path.insert(0, here)
    try:
        import btparse
    except ImportError as e:
        return ["btparse 导不进来：%s" % e]
    return ["%s 的 %s：期望 %r，实际 %r" % (name[:40], key, want, got)
            for name, key, want, got in btparse.selftest()]


def check_inline_js(here):
    """
    扫出网页里 JS 的「字符串字面量跨行」错误。

    起因：btweb 的 SCRIPT 如果用普通三引号字符串写，JS 里的 \\n 会被 Python
    先解释成真换行，而 JS 的单双引号字符串不允许跨行 —— 整个 app.js 直接语法错误，
    页面上所有按钮、勾选框全部失效，而且浏览器不报到后端，只看服务端日志什么都看不出来。
    所以用原始字符串写，并在这里守着。
    """
    bad = []
    try:
        sys.path.insert(0, here)
        import importlib
        mod = importlib.import_module("btweb")
        js = getattr(mod, "SCRIPT", "") or ""
    except Exception as e:
        return ["读不到网页脚本：%s" % e]

    for lineno, line in enumerate(js.split("\n"), 1):
        quote, esc_next, i = None, False, 0
        while i < len(line):
            ch = line[i]
            if esc_next:
                esc_next = False
            elif ch == "\\":
                esc_next = True
            elif quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "`":
                quote = None           # 模板字符串允许跨行，不追踪
                break
            elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                break                  # 行注释，后面不看
            i += 1
        if quote:
            bad.append("第 %d 行有没闭合的 %s 字符串：%s" % (lineno, quote, line.strip()[:40]))
    return bad


def check_cli_flags(here):
    """
    确认常用选项写在子命令后面也能被接受。

    argparse 默认只认「选项在子命令前面」，但人会自然地写在后面
    （btimport.py torznab 地址 --limit 5000）。这类问题只有实际敲一次才会暴露，
    报错还是一大段 usage，不容易一眼看懂，所以放进自检里守着。
    """
    probes = [
        ("btimport.py", ["probe", "ia", "--timeout", "5", "--limit", "1",
                         "--delay", "0", "--workers", "1", "--proxy", ""]),
        ("btimport.py", ["folder", here, "--db", os.path.join(here, "_clitest.db"),
                         "-q", "--limit", "1"]),
    ]
    bad = []
    for script, argv in probes:
        if not os.path.exists(os.path.join(here, script)):
            continue
        try:
            _, out = run_child([sys.executable, script] + argv, here)
        except subprocess.TimeoutExpired:
            continue            # 进到联网阶段就说明参数没问题
        except (OSError, subprocess.SubprocessError):
            continue
        if "unrecognized arguments" in out:
            line = [l for l in out.split("\n") if "unrecognized" in l]
            bad.append("%s %s -> %s" % (script, argv[0], line[0][:60] if line else ""))
    for junk in ("_clitest.db", "_clitest.db-wal", "_clitest.db-shm"):
        try:
            os.remove(os.path.join(here, junk))
        except OSError:
            pass
    return bad


USAGE = """环境自检：把这套工具跑起来要用到的东西逐项试一遍。

    %s btcheck.py            跑全部检查
    %s btcheck.py --help     只看这段说明

不接受别的参数。检查项是固定的，没有可配的东西。"""


def main():
    setup_console()
    # 必须先认 --help，而且要立刻返回。
    #
    # 这个脚本自己就写在 check.bat 里，而 scan_bat_flags 的做法是对每个
    # .bat 里提到的脚本跑一次 `--help` 去收集它支持的参数——于是自检会起一个
    # 子进程递归地跑自己，跑满 30 秒被 timeout 杀掉才算完。整个自检 36 秒里
    # 有 30 秒耗在这一件事上，而且全程不显示任何进展，看着就像卡死了。
    if any(a in ("-h", "--help", "/?") for a in sys.argv[1:]):
        print(USAGE % (py_cmd(), py_cmd()))
        return
    if sys.argv[1:]:
        print("btcheck.py 不接受参数：%s" % " ".join(sys.argv[1:]))
        print(USAGE % (py_cmd(), py_cmd()))
        sys.exit(2)

    print("环境自检   构建 %s" % BUILD)
    print("=" * 66)

    # --- Python ---
    v = sys.version_info
    check("Python 版本", v[:2] >= MIN_PY,
          "%d.%d.%d  （最低要求 %d.%d）" % (v[0], v[1], v[2], MIN_PY[0], MIN_PY[1]),
          fatal=True)
    check("解释器位置", True, sys.executable)
    check("本机调用方式", True, "%s 脚本名.py" % py_cmd())

    # --- SQLite 与 FTS5 ---
    # FTS5 是整个索引的地基，没有它这套东西一行都跑不起来。
    # 官方 Windows 安装包是带的（sqlite3.vcxproj 里定义了 SQLITE_ENABLE_FTS5），
    # 但用别的渠道装的 Python 不一定，所以这里实际建一张表试试。
    #
    # 版本号这一项以前是写死 True 的，只把数字打出来给人看，从来没判断过。
    # 那样有个很坑的后果：下面的 FTS5 探测建的是一张普通 fts5 表，多老的
    # SQLite 都能建，于是自检 15 项全绿，然后第一次真用就死在建库那一步，
    # 报 unrecognized option: "contentless_delete"。全绿之后紧跟着一个
    # 打不开的库，是最难自己查出原因的一种失败。现在真的判了。
    ok_ver = sqlite3.sqlite_version_info >= MIN_SQLITE
    check("SQLite 版本", ok_ver,
          "%s  （最低要求 %d.%d，无正文 FTS 要这一版）"
          % (sqlite3.sqlite_version, MIN_SQLITE[0], MIN_SQLITE[1]),
          fatal=True,
          hint="别按 Python 版本推，同一个 3.11 小版本不同带的 SQLite 也不同。"
               "换 python.org 上新一点的安装包（3.12 及以上肯定够）再跑一遍。")
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(body, tokenize='unicode61')")
        conn.execute("INSERT INTO t VALUES ('复仇 仇者 者联 联盟 avengers')")
        hit = conn.execute("SELECT count(*) FROM t WHERE t MATCH '\"复仇\"'").fetchone()[0]
        check("FTS5 全文索引", hit == 1, "可用，中文二元组检索正常", fatal=True)
    except sqlite3.Error as e:
        check("FTS5 全文索引", False,
              "不可用：%s —— 换官方 python.org 的安装包" % e, fatal=True)
    finally:
        conn.close()

    # 光看版本号不够。真按 btindex 的 SCHEMA 建一张无正文表，
    # 建得起来才算数——版本够但编译选项缺了同样会在这里露出来。
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5"
                     "(body, tokenize='unicode61', content='', contentless_delete=1)")
        conn.execute("INSERT INTO t(rowid, body) VALUES (1, '复仇 仇者')")
        conn.execute("DELETE FROM t WHERE rowid=1")     # 无正文表能删，靠的就是这个选项
        check("无正文 FTS", True, "可建可删，索引能省掉约 47% 磁盘", fatal=True)
    except sqlite3.Error as e:
        check("无正文 FTS", False, "建不了：%s" % e, fatal=True,
              hint="这正是 btindex.py 建库时要用的表结构，缺了它一条种子也存不进去。")
    finally:
        conn.close()

    # --- 目录可写 ---
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        fd, tmp = tempfile.mkstemp(dir=here, suffix=".wtest")
        os.close(fd)
        os.remove(tmp)
        writable = True
        detail = here
    except OSError as e:
        writable = False
        detail = "%s 写不了：%s（别放在 Program Files 里）" % (here, e)
    check("工具目录可写", writable, detail, fatal=True)

    # --- 端口 ---
    # 爬虫要监听这些 UDP 端口。绑不上通常是被别的程序占了，
    # 绑得上也不代表外面能打进来——那是防火墙和 NAT 的事，见下面的提示。
    busy = []
    for p in PORTS:
        try:
            s = bind_udp(p)
            s.close()
        except OSError:
            busy.append(p)
    check("UDP 6881-6888", not busy,
          "全部可绑定" if not busy else "被占用：%s（换 --ports 或关掉占用的程序）"
          % ",".join(map(str, busy)))

    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 8080))
        s.close()
        web_ok, web_detail = True, "8080 可用"
    except OSError:
        web_ok, web_detail = False, "8080 被占用，启动网页时加 --port 换一个"
    check("网页端口", web_ok, web_detail)

    # --- 控制台编码 ---
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    check("输出编码", "utf-8" in enc or "utf8" in enc,
          "%s（重定向到日志时必须是 UTF-8，否则中文符号会让任务崩掉）" % enc)

    # --- 已有索引 ---
    db = os.path.join(here, "bt.db")
    if os.path.exists(db):
        try:
            c2 = sqlite3.connect(db)
            n, size = c2.execute(
                "SELECT count(*), COALESCE(SUM(size),0) FROM torrents").fetchone()
            detail = "bt.db 里有 %s 条种子" % format(n, ",")
            try:
                cols = {r[1] for r in c2.execute("PRAGMA table_info(torrents_fts)")}
                if not ("name" in cols and "files" in cols):
                    detail += "（全文索引还是老的单列结构，跑 migrate-fts.bat 能让排序更准）"
            except sqlite3.Error:
                pass
            check("已有索引", True, detail)
            c2.close()
        except sqlite3.Error as e:
            check("已有索引", False, "bt.db 读不了：%s" % e)
    else:
        check("已有索引", True, "还没有 bt.db，跑一次爬虫就会自动建")

    # --- 工具自身一致性 ---
    guard("代码完整性",
          lambda: ["%s 第%s行 %s" % b for b in scan_undefined(here)],
          "全部脚本没有未定义引用", fatal=True)

    guard("网页脚本", lambda: check_inline_js(here),
          "字符串字面量都闭合", fatal=True)

    guard("名字解析规则", lambda: check_parse_rules(here),
          "%d 条用例全过" % _parse_case_count(here))

    guard("分词与查询构造", lambda: check_query_rules(here),
          "%d 条用例全过" % _query_case_count(here))

    guard("命令行选项位置", lambda: check_cli_flags(here),
          "选项写在子命令前后都接受")

    guard("启动脚本参数",
          lambda: ["%s 用了 %s 但 %s 不支持" % (b, ",".join(m), s2)
                   for b, s2, m in scan_bat_flags(here)],
          "与各脚本的参数一致", fatal=True)

    # --- 小结 ---
    print("=" * 66)
    fatal = [r for r in results if not r[1] and r[3]]
    warn = [r for r in results if not r[1] and not r[3]]
    if fatal:
        print("有 %d 项必须先解决，否则跑不起来：" % len(fatal))
        for name, _, detail, _ in fatal:
            print("  * %s —— %s" % (name, detail))
        return 1

    print("环境没问题，可以开始了。")
    if warn:
        print("\n%d 项提醒（不影响启动，但会影响效果）：" % len(warn))
        for name, _, detail, _ in warn:
            print("  * %s —— %s" % (name, detail))

    print("\n下一步：")
    if IS_WINDOWS:
        print("  1. 右键 install-firewall.bat 用管理员身份运行（放行 UDP 入站）")
        print("  2. 双击 crawler.bat 开始爬，让它跑几个小时")
        print("  3. 双击 web-admin.bat 搜索（只给别人搜的话用 web-user.bat）")
    else:
        print("  %s dhtmeta.py --sniff --db bt.db --with-lookup" % py_cmd())
        print("  %s btweb.py --db bt.db" % py_cmd())
    print("\n注意：端口能绑上只说明本机没被占用。爬虫要收到别人的查询，")
    print("还需要防火墙放行 UDP 入站，家用路由器后面另需端口转发。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
