#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btmaint —— 定期维护：一条命令跑完体检、实测、清理、整理

这是给 Windows 计划任务（或 Linux cron）用的入口。它把前面几个工具串成一条流水线，
按固定顺序执行，全程记日志，并且保证不会有两个实例同时动同一个数据库。

默认执行的步骤（都是非破坏性的）：
    1. 体检      —— 看看库里现在什么情况
    2. 修一致性  —— FTS 孤儿条目会让爬虫崩，必须定期清
    3. 实测做种  —— 抽一批种子去 DHT 查真实 peer 数，写回索引

删除和整理默认都不做，必须显式打开：
    --prune-dead     删掉「实测没人在传、而且很久没再出现」的
    --prune-old N    删掉「只见过一次、N 天没再出现」的
    --vacuum         把删掉的空间还给磁盘

为什么删除要单独开：查到 0 个 peer 只代表此刻 DHT 里没人宣告，
单次结果不足以判死。所以 --prune-dead 还额外要求「last_seen 也已经很旧」，
两个条件都满足才动手，比只看一次实测结果稳妥得多。

用法：
    python btmaint.py --db bt.db                      # 每天跑，只体检和实测
    python btmaint.py --db bt.db --prune-dead --vacuum-weekday 6   # 周日顺便清理整理
    Windows 上用 maint.bat 包一层，然后交给计划任务。

依赖：无，标准库足够。
"""

import argparse
import os
import sys
import time
import traceback
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcompat import setup_console
from btprune import pad
from btindex import DB_DEFAULT, human
import btparse
import btpeers
import btprune


# --------------------------------------------------------------------------
# 日志：同时写控制台和文件，按大小轮转
# --------------------------------------------------------------------------

class Tee:
    """把输出同时送到控制台和日志文件。计划任务看日志，人看屏幕。"""

    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, text):
        for s in self.streams:
            try:
                s.write(text)
            except (ValueError, OSError):
                pass
        return len(text)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except (ValueError, OSError):
                pass

    def isatty(self):
        return False


def rotate_log(path, max_bytes=5 * 1024 * 1024, keep=5):
    """日志超过大小就轮转。挂着跑几个月不至于把磁盘写满。"""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return
    except OSError:
        return
    for i in range(keep - 1, 0, -1):
        src, dst = "%s.%d" % (path, i), "%s.%d" % (path, i + 1)
        if os.path.exists(src):
            try:
                if os.path.exists(dst):
                    os.remove(dst)          # Windows 上 rename 不会覆盖，得先删
                os.replace(src, dst)
            except OSError:
                pass
    try:
        if os.path.exists("%s.1" % path):
            os.remove("%s.1" % path)
        os.replace(path, "%s.1" % path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 单实例锁
# --------------------------------------------------------------------------

class AlreadyRunning(Exception):
    pass


class SingleInstance:
    """
    防止两次计划任务撞在一起。上一次跑得慢、下一次又到点了，
    两个进程同时删同一个库不会立刻报错，但会互相拖成死锁或超时。

    用「原子创建文件」实现，跨平台可靠。锁文件太旧（比如上次是断电挂的）会自动接管。
    """

    def __init__(self, path, stale_hours=12):
        self.path = path
        self.stale = stale_hours * 3600
        self.acquired = False

    def __enter__(self):
        for attempt in (1, 2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as fp:
                    fp.write("%d\n%d\n" % (os.getpid(), int(time.time())))
                self.acquired = True
                return self
            except FileExistsError:
                if attempt == 2:
                    break
                try:
                    age = time.time() - os.path.getmtime(self.path)
                except OSError:
                    age = 0
                if age > self.stale:
                    print("发现 %.1f 小时前的残留锁，判定为上次异常退出，接管。" % (age / 3600))
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    continue
                raise AlreadyRunning(
                    "已经有一个维护任务在跑（锁文件 %s，%.0f 分钟前建的）。"
                    % (self.path, age / 60))
        raise AlreadyRunning("拿不到锁：%s" % self.path)

    def __exit__(self, *exc):
        if self.acquired:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


# --------------------------------------------------------------------------
# 步骤执行
# --------------------------------------------------------------------------

class Runner:
    def __init__(self):
        self.results = []

    def step(self, name, func):
        """
        一个步骤失败不能带倒后面的。子命令里有 sys.exit()，所以 SystemExit 也要接住——
        比如爬虫正在跑的时候 vacuum 会失败退出，但实测那步的结果不该因此丢掉。
        """
        print("\n" + "=" * 68)
        print("▶ %s" % name)
        print("=" * 68)
        t0 = time.time()
        try:
            func()
            ok, detail = True, ""
        except SystemExit as e:
            code = e.code
            ok = (code in (0, None))
            detail = "" if ok else str(code)
            if not ok:
                print("这一步提前结束：%s" % detail)
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
            print("这一步出错：%s" % detail)
            traceback.print_exc(file=sys.stdout)
        self.results.append((name, ok, round(time.time() - t0, 1), detail))

    def summary(self):
        print("\n" + "=" * 68)
        print("本次维护小结")
        print("=" * 68)
        for name, ok, secs, detail in self.results:
            print("  %s %s %6.1f 秒  %s"
                  % (pad("完成" if ok else "未完成", 7), pad(name, 24), secs, detail))
        failed = [r for r in self.results if not r[1]]
        return 1 if failed else 0


# --------------------------------------------------------------------------
# 各步骤
# --------------------------------------------------------------------------

def step_overview(args):
    def run():
        conn = btprune.open_db(args.db, readonly=True)
        total, size = conn.execute(
            "SELECT count(*), COALESCE(SUM(size),0) FROM torrents").fetchone()
        print("种子 %s 条，内容总量 %s，数据库 %s"
              % (format(total, ","), human(size), human(btprune.db_size(args.db))))
        have = {r[1] for r in conn.execute("PRAGMA table_info(torrents)")}
        if "checked_at" in have:
            checked, dead = conn.execute(
                "SELECT SUM(checked_at>0), SUM(checked_at>0 AND peers=0) FROM torrents"
            ).fetchone()
            print("已实测 %s 条，其中没人在传 %s 条"
                  % (format(checked or 0, ","), format(dead or 0, ",")))
        else:
            print("还没实测过做种情况（本次会开始实测）")
        conn.close()
    return run


def step_parse(args):
    """
    把还没解析过名字的条目补上。

    平时这一步什么也不做——新条目入库时就解析好了。它存在是为了两种情况：
    一是从旧版升上来、库里堆着一批没解析过的；
    二是解析规则改过（PARSE_VERSION 变了），全库需要按新规则重来一遍。
    两种情况都不用人操心，每周维护跑到就顺手补了。
    """
    def run():
        r = btparse.backfill(args.db, progress=None)
        if r["added"]:
            print("已给索引补上列：%s" % "、".join(r["added"]))
        if not r["todo"]:
            print("都解析过了，跳过。")
            return
        print("解析了 %s 条，其中 %s 条的值有变化。"
              % (format(r["done"], ","), format(r["changed"], ",")))
    return run


def step_verify(args):
    def run():
        btprune.cmd_verify(SimpleNamespace(db=args.db, fix=True, backup=None))
    return run


def step_scan(args):
    def run():
        btpeers.cmd_scan(SimpleNamespace(
            db=args.db, limit=args.scan, workers=args.scan_workers,
            timeout=args.scan_timeout, recheck=args.recheck, order=args.scan_order))
    return run


def step_prune_dead(args):
    def run():
        # 两个条件同时要求：实测没人 + 本来就很久没再出现。
        # 只凭单次实测为 0 就删，误伤率会很高。
        btprune.cmd_prune(SimpleNamespace(
            db=args.db, older_than="%dd" % args.prune_dead_after,
            max_hits=None, max_size=None, min_size=None, source=None,
            nameless=False, max_peers=0, checked=True,
            backup=args.backup, yes=not args.dry_run))
    return run


def step_prune_old(args):
    def run():
        btprune.cmd_prune(SimpleNamespace(
            db=args.db, older_than="%dd" % args.prune_old, max_hits=1,
            max_size=None, min_size=None, source=None, nameless=False,
            max_peers=None, checked=False,
            backup=args.backup, yes=not args.dry_run))
    return run


def step_vacuum(args):
    def run():
        btprune.cmd_vacuum(SimpleNamespace(db=args.db))
    return run


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description="定期维护：体检、修一致性、实测做种、按需清理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""典型排法：
  每天  python btmaint.py --db bt.db --scan 300
  每周  python btmaint.py --db bt.db --scan 800 --prune-dead --vacuum

删除类操作默认都关着，要显式打开。加 --dry-run 可以先看会删什么。
""")
    ap.add_argument("--db", default=DB_DEFAULT, help="索引路径（默认 %s）" % DB_DEFAULT)

    ap.add_argument("--scan", type=int, default=300,
                    help="本次实测多少个种子的做种情况，0 表示跳过（默认 300）")
    ap.add_argument("--scan-workers", type=int, default=16, help="实测并发数")
    ap.add_argument("--scan-timeout", type=float, default=12, help="单个查找超时秒数")
    ap.add_argument("--scan-order", default="oldest",
                    choices=["oldest", "newest", "hot", "random"], help="先测哪些")
    ap.add_argument("--recheck", type=int, default=14,
                    help="多少天内测过的就跳过（默认 14）")
    ap.add_argument("--no-verify", action="store_true", help="跳过一致性检查")
    ap.add_argument("--no-parse", action="store_true",
                    help="跳过「把没解析过的条目补上分类和清晰度」这一步")

    ap.add_argument("--prune-dead", action="store_true",
                    help="删掉「实测没人在传」且「很久没再出现」的")
    ap.add_argument("--prune-dead-after", type=int, default=30,
                    help="配合 --prune-dead：还要求多少天没再出现（默认 30）")
    ap.add_argument("--prune-old", type=int, metavar="DAYS",
                    help="删掉「只见过一次且 N 天没再出现」的")
    ap.add_argument("--backup", help="任何删除前先把库备份到这个路径")
    ap.add_argument("--dry-run", action="store_true", help="删除只预演，不真删")

    ap.add_argument("--vacuum", action="store_true", help="本次整理磁盘")
    ap.add_argument("--vacuum-weekday", type=int, metavar="N",
                    help="只在星期几整理，0=周一 … 6=周日")

    ap.add_argument("--log", default="", help="日志文件路径，不给就只打屏幕")
    ap.add_argument("--log-keep", type=int, default=5, help="日志保留几个轮转文件")
    ap.add_argument("--lock", default="", help="锁文件路径，默认放在数据库旁边")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit("找不到 %s。先跑爬虫把索引建起来。" % args.db)

    log_fp = None
    if args.log:
        d = os.path.dirname(os.path.abspath(args.log))
        if d:
            os.makedirs(d, exist_ok=True)
        rotate_log(args.log, keep=args.log_keep)
        log_fp = open(args.log, "a", encoding="utf-8")
        sys.stdout = Tee(sys.stdout, log_fp)
        sys.stderr = sys.stdout

    lock_path = args.lock or (os.path.abspath(args.db) + ".maintlock")
    started = time.time()
    print("\n" + "#" * 68)
    print("# 维护开始  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("# 数据库 %s" % os.path.abspath(args.db))
    print("#" * 68)

    code = 0
    try:
        with SingleInstance(lock_path):
            r = Runner()
            r.step("体检", step_overview(args))
            if not args.no_verify:
                r.step("修复索引一致性", step_verify(args))
            if not args.no_parse:
                # 放在实测前面：解析不联网、跑得快，而实测那一步动辄几分钟，
                # 排在后面万一被掐了，前面这步已经落盘了
                r.step("解析名字", step_parse(args))
            if args.scan > 0:
                r.step("实测做种情况", step_scan(args))
            if args.prune_dead:
                r.step("清理确认死掉的", step_prune_dead(args))
            if args.prune_old:
                r.step("清理陈旧一次性条目", step_prune_old(args))

            do_vac = args.vacuum or (
                args.vacuum_weekday is not None
                and time.localtime().tm_wday == args.vacuum_weekday)
            if do_vac:
                r.step("整理磁盘空间", step_vacuum(args))
            code = r.summary()
    except AlreadyRunning as e:
        print("\n跳过本次：%s" % e)
        code = 2
    except KeyboardInterrupt:
        print("\n被手动中断。")
        code = 130

    print("\n维护结束，总耗时 %.1f 秒，退出码 %d" % (time.time() - started, code))
    if log_fp:
        try:
            log_fp.close()
        except OSError:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
