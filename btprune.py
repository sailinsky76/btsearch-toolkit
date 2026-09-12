#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btprune —— 索引维护：体检、清死种、修一致性、回收空间

先说清楚这个工具存在的两个理由。

理由一：这版索引的 FTS 表没有触发器。
btindex 的 torrents_fts 是独立表，只在 upsert 里手动同步。也就是说，
只要有人写了一句朴素的 DELETE FROM torrents，FTS 里就会留下孤儿行。
后果不是「索引变大一点」那么轻——SQLite 会回收被删掉的最大 rowid，
新种子拿到同一个 rowid 往 FTS 一插，直接 IntegrityError，爬虫当场挂掉。
所以任何删除都必须先删 FTS 再删主表，本工具的 prune 就是这么做的。

理由二：「死种」在这里只是个推测，不是事实。
库里的 last_seen 反映的是「你的节点听到它的最后时间」，不是「它在全网的最后活跃时间」。
你的嗅探节点只覆盖 DHT 键空间的一小片，一个种子活得好好的、但恰好没往你这片announce，
在库里看起来就跟死了一样。所以按时间清理一定有误杀，
要真正确认死活得去 DHT 发 get_peers 问一圈——那是下一个工具的事。

结论：清理策略要保守。默认建议只删「只见过一次、而且很久没再出现」的，
这类基本是一次性垃圾；磁盘不紧张的话，根本不用急着删。

用法：
    python3 btprune.py analyze                     # 只看不动，先体检
    python3 btprune.py verify --fix                # 修 FTS 一致性
    python3 btprune.py prune --older-than 180d --max-hits 1        # 先看会删什么
    python3 btprune.py prune --older-than 180d --max-hits 1 --yes  # 真删
    python3 btprune.py vacuum                      # 回收磁盘

依赖：无，标准库足够。
"""

import argparse
import os
import re
import sqlite3
import sys
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcompat import db_uri, py_cmd, setup_console
from btindex import DB_DEFAULT, expand_text, human, parse_size

BAR = "▇"


def dwidth(text) -> int:
    """终端显示宽度。中文是双宽，用 len() 排版会歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(text))


def pad(text, width, right=False) -> str:
    text = str(text)
    space = " " * max(width - dwidth(text), 0)
    return space + text if right else text + space


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def parse_duration(text) -> int:
    """'90d' / '12w' / '6mo' / '1y' / 裸数字（当天算）-> 秒数。"""
    s = str(text).strip().lower()
    if not s:
        raise ValueError("时长不能为空")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(mo|[hdwy])?", s)
    if not m:
        raise ValueError("看不懂的时长: %r（试试 90d、12w、6mo、1y）" % text)
    unit = {"h": 3600, "d": 86400, "w": 604800,
            "mo": 2592000, "y": 31536000}[m.group(2) or "d"]
    return int(float(m.group(1)) * unit)


def fmt_ago(ts) -> str:
    if not ts:
        return "-"
    d = max(int(time.time()) - int(ts), 0)
    for limit, div, unit in ((3600, 60, "分钟"), (86400, 3600, "小时"),
                             (2592000, 86400, "天"), (31536000, 2592000, "个月")):
        if d < limit:
            return "%d %s前" % (max(d // div, 1), unit)
    return "%d 年前" % max(d // 31536000, 1)


def checkpoint(conn):
    """把 WAL 里的改动落回主库。量文件大小之前必须做，否则数据还散在 wal 里，前后对比会失真。"""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


def db_size(path) -> int:
    """只算主库和 WAL。-shm 是连接打开期间的固定 32 KB 开销，关掉就没了，算进去会误导。"""
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            pass
    return total


def open_db(path, readonly=False):
    if not os.path.exists(path):
        sys.exit("找不到 %s" % path)
    if readonly:
        conn = sqlite3.connect(db_uri(path, readonly=True), uri=True, timeout=30)
    else:
        conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def confirm_or_die(args, what):
    if args.yes:
        return
    print("\n这是预演，什么都没动。确认无误后加 --yes 真正执行：")
    print("  %s --yes" % what)
    sys.exit(0)


def backup_db(src_path, dest_path):
    """用 SQLite 的备份 API，比 cp 安全：它能在 WAL 模式下拿到一致快照。"""
    src = sqlite3.connect(src_path, timeout=30)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    print("已备份到 %s（%s）" % (dest_path, human(db_size(dest_path))))


# --------------------------------------------------------------------------
# 一致性检查
# --------------------------------------------------------------------------

def check_consistency(conn) -> dict:
    """
    三种病：
      orphan  —— FTS 里有、主表里没有。就是它会导致后续插入撞 rowid 而崩。
      missing —— 主表里有、FTS 里没有。这些种子搜不出来，等于白爬。
      dup     —— 同一个 rowid 在 FTS 里出现多次，会让搜索结果重复。
    """
    orphan = conn.execute(
        "SELECT count(*) FROM torrents_fts "
        "WHERE rowid NOT IN (SELECT rowid FROM torrents)").fetchone()[0]
    missing = conn.execute(
        "SELECT count(*) FROM torrents t WHERE NOT EXISTS "
        "(SELECT 1 FROM torrents_fts f WHERE f.rowid = t.rowid)").fetchone()[0]
    dup = conn.execute(
        "SELECT COALESCE(SUM(n-1),0) FROM "
        "(SELECT rowid, count(*) n FROM torrents_fts GROUP BY rowid HAVING n>1)"
    ).fetchone()[0]
    return {"orphan": orphan, "missing": missing, "dup": dup}


def fix_consistency(conn) -> dict:
    fixed = {"orphan": 0, "missing": 0}
    with conn:
        cur = conn.execute(
            "DELETE FROM torrents_fts "
            "WHERE rowid NOT IN (SELECT rowid FROM torrents)")
        fixed["orphan"] = cur.rowcount

        rows = conn.execute(
            "SELECT t.rowid, t.name, t.filelist FROM torrents t WHERE NOT EXISTS "
            "(SELECT 1 FROM torrents_fts f WHERE f.rowid = t.rowid)").fetchall()
        for r in rows:
            # 索引正文的拼法必须和 btindex.upsert 里完全一致，
            # 否则补回去的这些条目搜出来的结果会和别的不一样
            body = "%s %s %s %s" % (r["name"], expand_text(r["name"]),
                                    r["filelist"], expand_text(r["filelist"]))
            conn.execute("INSERT INTO torrents_fts(rowid, body) VALUES (?,?)",
                         (r["rowid"], body))
        fixed["missing"] = len(rows)
    return fixed


# --------------------------------------------------------------------------
# 过滤条件
# --------------------------------------------------------------------------

def build_filter(args):
    """把命令行条件拼成 WHERE。返回 (sql, 参数, 人话描述)。"""
    where, params, desc = [], [], []
    if args.older_than:
        cutoff = int(time.time()) - parse_duration(args.older_than)
        where.append("last_seen < ?")
        params.append(cutoff)
        desc.append("最后一次出现早于 %s" % time.strftime(
            "%Y-%m-%d", time.localtime(cutoff)))
    if args.max_hits is not None:
        where.append("hits <= ?")
        params.append(args.max_hits)
        desc.append("被 announce 不超过 %d 次" % args.max_hits)
    if args.max_size:
        n = parse_size(args.max_size)
        where.append("size <= ?")
        params.append(n)
        desc.append("体积不超过 %s" % human(n))
    if args.min_size:
        n = parse_size(args.min_size)
        where.append("size >= ?")
        params.append(n)
        desc.append("体积不小于 %s" % human(n))
    if args.source:
        where.append("source = ?")
        params.append(args.source)
        desc.append("来源是 %s" % args.source)
    if args.nameless:
        where.append("(name = '(无名)' OR trim(name) = '')")
        desc.append("没有名字")
    if args.max_peers is not None:
        # 这两个条件要 btpeers 扫过才有意义，所以下面会先检查列在不在
        where.append("peers >= 0 AND peers <= ?")
        params.append(args.max_peers)
        desc.append("实测 peer 数不超过 %d" % args.max_peers)
    if args.checked:
        where.append("checked_at > 0")
        desc.append("已经实测过")
    return " AND ".join(where), params, desc


def needs_peer_columns(args) -> bool:
    return args.max_peers is not None or args.checked


def assert_peer_columns(conn, args):
    if not needs_peer_columns(args):
        return
    have = {r[1] for r in conn.execute("PRAGMA table_info(torrents)")}
    if "peers" not in have or "checked_at" not in have:
        sys.exit("索引里还没有实测数据。先跑一次：\n"
                 "  %s btpeers.py scan --db %s --limit 500" % (py_cmd(), args.db))


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

AGE_BUCKETS = [(7, "7 天内"), (30, "7–30 天"), (90, "30–90 天"),
               (180, "90–180 天"), (365, "180–365 天"), (None, "一年以上")]


def cmd_analyze(args):
    conn = open_db(args.db, readonly=True)
    now = int(time.time())

    total, total_size = conn.execute(
        "SELECT count(*), COALESCE(SUM(size),0) FROM torrents").fetchone()
    print("索引体检  %s" % args.db)
    print("=" * 60)
    if not total:
        print("库是空的，先让爬虫跑一会儿。")
        return
    print("种子 %s 条    内容总量 %s    数据库占用 %s"
          % (format(total, ","), human(total_size), human(db_size(args.db))))

    # 年龄分布：这是判断该不该清理的主要依据
    print("\n按最后一次出现的时间分布")
    prev = 0
    rows = []
    for days, label in AGE_BUCKETS:
        if days is None:
            n, sz = conn.execute(
                "SELECT count(*), COALESCE(SUM(size),0) FROM torrents "
                "WHERE last_seen < ?", (now - prev * 86400,)).fetchone()
        else:
            n, sz = conn.execute(
                "SELECT count(*), COALESCE(SUM(size),0) FROM torrents "
                "WHERE last_seen >= ? AND last_seen < ?",
                (now - days * 86400, now - prev * 86400)).fetchone()
            prev = days
        rows.append((label, n, sz))
    widest = max((n for _, n, _ in rows), default=1) or 1
    for label, n, sz in rows:
        bar = BAR * max(int(n / widest * 26), 1 if n else 0)
        print("  %s %s 条  %s %s"
              % (pad(label, 11), pad(format(n, ","), 7, right=True),
                 pad(bar, 26), human(sz)))

    # 热度分布：只见过一次的那批是清理的首要目标
    once = conn.execute("SELECT count(*) FROM torrents WHERE hits <= 1").fetchone()[0]
    few = conn.execute("SELECT count(*) FROM torrents WHERE hits BETWEEN 2 AND 5").fetchone()[0]
    print("\n热度分布")
    for label, n in (("只见过 1 次", once), ("见过 2–5 次", few),
                     ("见过 5 次以上", total - once - few)):
        print("  %s %s 条  (%.1f%%)"
              % (pad(label, 14), pad(format(n, ","), 7, right=True), n * 100.0 / total))

    # 一致性
    c = check_consistency(conn)
    print("\nFTS 一致性")
    if not any(c.values()):
        print("  正常。")
    else:
        if c["orphan"]:
            print("  孤儿条目 %d 条 —— 这会让爬虫下次插入时崩掉，跑 verify --fix 修" % c["orphan"])
        if c["missing"]:
            print("  缺索引 %d 条 —— 这些种子搜不出来，跑 verify --fix 补" % c["missing"])
        if c["dup"]:
            print("  重复条目 %d 条 —— 搜索结果会重复" % c["dup"])

    # 给个保守建议，而不是替用户做决定
    # 按来源分开给建议。last_seen 和 hits 只对爬虫抓的条目有意义：
    # 导入来的条目，last_seen 是「上次跑导入的时间」，hits 恒为 1，
    # 拿这两个当死活判据，会把整批导入的资源误删掉。
    cutoff = now - 180 * 86400
    dht_n = conn.execute(
        "SELECT count(*) FROM torrents WHERE source = 'dht' "
        "AND last_seen < ? AND hits <= 1", (cutoff,)).fetchone()[0]
    other = conn.execute(
        "SELECT count(*) FROM torrents WHERE source IS NULL OR source != 'dht'"
    ).fetchone()[0]

    # 有多少条能显示预览图。不报的话只能一个个点详情页去猜有没有按钮
    have = {r[1] for r in conn.execute("PRAGMA table_info(torrents)")}
    if "cover" in have:
        withcover = conn.execute(
            "SELECT count(*) FROM torrents WHERE cover != ''").fetchone()[0]
        print("\n预览图")
        print("  %s 条有封面地址（占 %.1f%%），详情页上会出现「显示预览图」。"
              % (format(withcover, ","), withcover * 100.0 / max(total, 1)))
        if not withcover:
            print("  一条都没有：你接的索引站没在 Torznab 里提供 coverurl。"
                  "多数中文站和动漫站都不提供。")
    else:
        print("\n预览图")
        print("  索引里还没有封面字段 —— 说明所有条目都是在这个功能之前导入的。")
        print("  重新导入一次会给老条目回填（不会产生重复），"
              "前提是那个站确实提供封面地址。")

    print("\n清理建议")
    print("  爬虫抓的条目里，「只见过 1 次且 180 天没再出现」的有 %s 条。"
          % format(dht_n, ","))
    if dht_n:
        print("  这类多半是一次性垃圾，是最安全的清理对象：")
        print("    %s btprune.py --db %s prune --source dht --older-than 180d --max-hits 1"
              % (py_cmd(), args.db))
    if other:
        print("\n  另有 %s 条是导入来的（Jackett / 档案馆 / 本地种子）。" % format(other, ","))
        print("  这批不能按时间和热度清 —— 对它们来说 last_seen 只是"
              "「上次跑导入的时间」，")
        print("  hits 恒为 1，跟种子死活毫无关系。想清理只能先实测：")
        print("    %s btpeers.py --db %s scan --limit 500" % (py_cmd(), args.db))
        print("    %s btprune.py --db %s prune --max-peers 0 --checked" % (py_cmd(), args.db))

    print("\n提醒：last_seen 只代表你的节点最后一次听到它，不代表它在全网真的死了。")
    conn.close()


def cmd_verify(args):
    conn = open_db(args.db, readonly=not args.fix)
    c = check_consistency(conn)
    print("孤儿 FTS 条目 : %d" % c["orphan"])
    print("缺失 FTS 条目 : %d" % c["missing"])
    print("重复 FTS 条目 : %d" % c["dup"])
    if not any(c.values()):
        print("\n一致性正常，不用修。")
        return
    if not args.fix:
        print("\n加 --fix 修复。")
        return
    if args.backup:
        backup_db(args.db, args.backup)
    fixed = fix_consistency(conn)
    print("\n已删除孤儿 %d 条，补回索引 %d 条。" % (fixed["orphan"], fixed["missing"]))
    if c["dup"]:
        print("重复条目本工具不自动处理，因为无法判断该留哪一条；"
              "确认后可以用 verify --fix 先删孤儿，再重建整表。")
    conn.close()


def cmd_prune(args):
    where, params, desc = build_filter(args)
    if not where:
        sys.exit("至少要给一个条件，否则就是清空整个库了。"
                 "常用：--older-than 180d --max-hits 1")

    conn = open_db(args.db, readonly=not args.yes)
    assert_peer_columns(conn, args)
    total = conn.execute("SELECT count(*) FROM torrents").fetchone()[0]
    n, sz = conn.execute(
        "SELECT count(*), COALESCE(SUM(size),0) FROM torrents WHERE %s" % where,
        params).fetchone()

    print("条件：%s" % "，且".join(desc))
    print("命中 %s 条 / 全库 %s 条（%.1f%%），对应内容 %s"
          % (format(n, ","), format(total, ","),
             n * 100.0 / total if total else 0, human(sz)))
    if not n:
        print("没有匹配的，不用清。")
        return

    # 删之前先看看长什么样。删除不可逆，这一眼很值
    print("\n随便抽几条给你过目：")
    for r in conn.execute(
            "SELECT name, size, hits, last_seen FROM torrents WHERE %s "
            "ORDER BY RANDOM() LIMIT 8" % where, params):
        print("  %s %s  %3d 次  %s"
              % (pad(r["name"][:44], 46), pad(human(r["size"]), 9, right=True),
                 r["hits"], fmt_ago(r["last_seen"])))

    if n * 100.0 / max(total, 1) > 50:
        print("\n注意：这一刀下去超过半个库。确认条件是不是写宽了。")

    # 用时间/热度当判据、却没限定来源时，检查会不会误伤导入的条目
    if (args.older_than or args.max_hits is not None) and not args.source:
        hit_other = conn.execute(
            "SELECT count(*) FROM torrents WHERE (source IS NULL OR source != 'dht') "
            "AND %s" % where, params).fetchone()[0]
        if hit_other:
            print("\n⚠ 其中 %s 条是导入来的（非爬虫抓取）。" % format(hit_other, ","))
            print("  对这批条目，last_seen 只是「上次跑导入的时间」，hits 恒为 1，")
            print("  按时间或热度清理等于按「我多久没跑导入」删资源，和种子死活无关。")
            print("  要么加 --source dht 只清爬虫抓的，要么先跑 btpeers scan 实测再按"
                  " --max-peers 0 --checked 清。")

    # 被 btmaint 之类的程序调用时 sys.argv 里没有 "prune"，这里要容错
    try:
        tail = " ".join(sys.argv[sys.argv.index("prune") + 1:])
    except ValueError:
        tail = "<你的条件>"
    confirm_or_die(args, "%s btprune.py --db %s prune %s" % (py_cmd(), args.db, tail))

    if args.backup:
        backup_db(args.db, args.backup)

    before = db_size(args.db)
    with conn:
        # 顺序不能反：先按主表的条件把 FTS 行删掉，再删主表。
        # 反过来的话第二步就找不到该删哪些 FTS 行了，孤儿就此产生。
        conn.execute("DELETE FROM torrents_fts WHERE rowid IN "
                     "(SELECT rowid FROM torrents WHERE %s)" % where, params)
        cur = conn.execute("DELETE FROM torrents WHERE %s" % where, params)
        removed = cur.rowcount

    left = conn.execute("SELECT count(*) FROM torrents").fetchone()[0]
    c = check_consistency(conn)
    print("\n已删除 %s 条，剩余 %s 条。" % (format(removed, ","), format(left, ",")))
    print("一致性复查：孤儿 %d，缺失 %d %s"
          % (c["orphan"], c["missing"], "✓" if not (c["orphan"] or c["missing"]) else "✗"))
    print("数据库还是 %s —— 删除只是标记空闲页，要真的还给磁盘得跑 vacuum。"
          % human(before))
    conn.close()


def cmd_reset(args):
    """
    清空索引。

    为什么要专门做一个命令，而不是让人自己写 DELETE：
    这版的 FTS 表没有触发器，只在 upsert 里手动同步。一句朴素的
    DELETE FROM torrents 会在 torrents_fts 里留下孤儿行；全表清空之后
    rowid 从 1 重新开始，下一个新种子插进来就撞上残留条目，
    直接 IntegrityError 把爬虫打挂。所以必须两张表一起清。
    """
    conn = open_db(args.db, readonly=not args.yes)
    where, params = ("source = ?", [args.source]) if args.source else ("1=1", [])
    n, sz = conn.execute(
        "SELECT count(*), COALESCE(SUM(size),0) FROM torrents WHERE %s" % where,
        params).fetchone()
    total = conn.execute("SELECT count(*) FROM torrents").fetchone()[0]

    scope = ("来源 %s" % args.source) if args.source else "整个索引"
    print("要清空的范围：%s" % scope)
    print("涉及 %s 条 / 全库 %s 条，对应内容 %s"
          % (format(n, ","), format(total, ","), human(sz)))
    if not n:
        print("本来就是空的，不用清。")
        return

    if not args.source:
        print("\n这会删掉所有条目，包括爬虫辛苦攒下来的那部分。"
              "只想清掉某一批的话用 --source，比如 --source torznab。")
        for row in conn.execute(
                "SELECT source, count(*) n FROM torrents GROUP BY source ORDER BY n DESC"):
            print("  来源 %-10s %s 条" % (row["source"] or "(未标)", format(row["n"], ",")))

    if not args.yes:
        print("\n这是预演，什么都没动。确认后加 --yes：")
        print("  %s btprune.py --db %s reset%s --yes"
              % (py_cmd(), args.db, (" --source " + args.source) if args.source else ""))
        return

    if args.backup:
        backup_db(args.db, args.backup)

    checkpoint(conn)
    before = db_size(args.db)
    with conn:
        if args.source:
            # 先按条件删 FTS，再删主表。顺序反了就找不到该删哪些 FTS 行
            conn.execute("DELETE FROM torrents_fts WHERE rowid IN "
                         "(SELECT rowid FROM torrents WHERE source = ?)", [args.source])
            conn.execute("DELETE FROM torrents WHERE source = ?", [args.source])
        else:
            conn.execute("DELETE FROM torrents_fts")
            conn.execute("DELETE FROM torrents")

    left = conn.execute("SELECT count(*) FROM torrents").fetchone()[0]
    c = check_consistency(conn)
    print("\n已清空，剩余 %s 条。" % format(left, ","))
    print("一致性复查：孤儿 %d，缺失 %d %s"
          % (c["orphan"], c["missing"], "✓" if not (c["orphan"] or c["missing"]) else "✗"))

    ok = False
    if not args.no_vacuum:
        print("整理磁盘…")
        try:
            conn.execute("VACUUM")
            checkpoint(conn)      # VACUUM 的改动也走 WAL，要归档才落到主库
            ok = True
        except sqlite3.OperationalError as e:
            print("整理失败（%s）。爬虫或网页还开着的话先关掉，再单独跑 vacuum。" % e)
    conn.close()                  # 关掉再量，剩下的才是真实占用
    if ok:
        print("%s -> %s" % (human(before), human(db_size(args.db))))


def cmd_vacuum(args):
    conn = open_db(args.db)
    checkpoint(conn)
    before = db_size(args.db)
    print("整理中，大库可能要几分钟，期间别让爬虫写入。")
    try:
        # optimize 让 FTS 把碎片化的小段合并成大段，搜索会快一些
        with conn:
            conn.execute("INSERT INTO torrents_fts(torrents_fts) VALUES('optimize')")
        conn.execute("VACUUM")
        checkpoint(conn)
    except sqlite3.OperationalError as e:
        conn.close()
        sys.exit("整理失败：%s\nVACUUM 需要独占访问，先把爬虫和 web 停掉再试。" % e)
    conn.close()          # 关掉再量
    after = db_size(args.db)
    saved = before - after
    print("%s -> %s，%s %s"
          % (human(before), human(after),
             "回收了" if saved > 0 else "变化", human(abs(saved)) if saved else "不大"))


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description="种子索引维护：体检、清死种、修一致性、回收空间",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""建议的顺序：
  1. analyze            先看清楚库里是什么情况
  2. verify --fix       把 FTS 一致性修好（孤儿条目会让爬虫崩）
  3. prune ...          不加 --yes 是预演，看清楚再加
     reset               清空整个索引，或 --source 只清某一批
     想按真实做种情况清理，先跑 btpeers.py scan，再用 --max-peers 0 --checked
  4. vacuum             把空间真正还给磁盘
""")
    ap.add_argument("--db", default=DB_DEFAULT, help="索引路径（默认 %s）" % DB_DEFAULT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("analyze", help="体检，只读不改")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("verify", help="检查并修复 FTS 一致性")
    p.add_argument("--fix", action="store_true", help="真的动手修")
    p.add_argument("--backup", help="修之前先备份到这个路径")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("prune", help="按条件删除，默认只预演")
    p.add_argument("--older-than", help="最后一次出现早于多久，如 180d、6mo、1y")
    p.add_argument("--max-hits", type=int, help="被 announce 次数不超过这个值")
    p.add_argument("--max-size", help="体积上限，如 10MB")
    p.add_argument("--min-size", help="体积下限")
    p.add_argument("--source", help="只清指定来源")
    p.add_argument("--nameless", action="store_true", help="只清没有名字的")
    p.add_argument("--max-peers", type=int,
                   help="实测 peer 数不超过这个值（需要先跑 btpeers scan）")
    p.add_argument("--checked", action="store_true",
                   help="只清已经实测过的，避免误伤没查过的")
    p.add_argument("--backup", help="删之前先备份到这个路径")
    p.add_argument("--yes", action="store_true", help="不加这个就只是预演")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("reset", help="清空索引（可按来源）")
    p.add_argument("--source", help="只清这一个来源，如 torznab / ia / dht / folder")
    p.add_argument("--backup", help="清之前先备份到这个路径")
    p.add_argument("--no-vacuum", action="store_true", help="清完不整理磁盘")
    p.add_argument("--yes", action="store_true", help="不加这个就只是预演")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("vacuum", help="回收磁盘空间并优化 FTS")
    p.set_defaults(func=cmd_vacuum)

    if ap.epilog:
        ap.epilog = ap.epilog.replace("python3 ", py_cmd() + " ")
    args = ap.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        sys.exit("参数有问题：%s" % e)
    except sqlite3.OperationalError as e:
        sys.exit("数据库出错：%s" % e)
    except BrokenPipeError:
        # 接 head/less 时对方提前关管道，正常退出即可，别吐 traceback
        try:
            sys.stdout.close()
        except OSError:
            pass
        os._exit(0)


if __name__ == "__main__":
    main()
