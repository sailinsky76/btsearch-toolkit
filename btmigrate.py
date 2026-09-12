#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btmigrate —— 把已有的库迁到无正文（contentless）FTS

要解决的问题：老 schema 里的 torrents_fts 是带正文的 FTS5 表，
它会把喂进去的 body 原样再存一份。而这份副本从来没人读——
全文所有 FTS 访问不是 MATCH、rowid 就是 bm25()，显示用的字段一律走主表。

实测 600 万条的库拆开看：

    torrents_fts_content   2.30 GB   <- 这张表就是那份没人读的副本
    torrents               1.45 GB
    torrents_fts_data      0.51 GB   <- 真正的倒排索引只有这么大
    其它索引               0.60 GB

也就是说整个库有 47% 是白占的。一亿条的库按这个比例算，
81 GB 里有 38 GB 是这份副本。

body 完全可以从 name 和 filelist 重算（就是 btindex.expand_text 那套），
所以迁移做的事很简单：另建一张无正文表，把全库重新喂一遍，然后换掉旧表。

    py -3.11 btmigrate.py --db bt.db            # 看看能省多少，不动手
    py -3.11 btmigrate.py --db bt.db --go       # 真的迁
    py -3.11 btmigrate.py --db bt.db --go       # 中断了？再跑一次，接着上次的来

几件要先说清楚的事：

  · 这是一次性操作。新建的库已经是无正文的，不用跑这个。
  · 跑之前把爬虫和网页停掉。迁移要独占写。
  · 可以中断。Ctrl-C 或者断电都不要紧，重新跑会从断的地方接着来，
    旧表在最后一刻之前一直原封不动，搜索期间照常可用。
  · 迁移过程中库文件会先涨一点（新旧两张索引并存），换完旧表的空间
    变成空闲页。文件不会自己缩回去——但爬虫接着往里写就会复用这些页，
    库是在长的就别管它。真想立刻还给磁盘，跑 btprune.py vacuum，
    注意那一步要额外一份等同于最终大小的临时空间。
"""

import argparse
import os
import sqlite3
import sys
import time

from btindex import MIN_SQLITE, expand_text, fts_two_col

BATCH = 20000
NEW = "torrents_fts_new"


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.2f %s" % (n, unit)).replace(".00 ", " ")
        n /= 1024.0


def table_sql(conn, name):
    r = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
    return r[0] if r else ""


def is_contentless(conn, name="torrents_fts"):
    sql = (table_sql(conn, name) or "").replace(" ", "").replace("'", "").replace('"', "")
    return "content=" in sql and "content=torrents" not in sql


def needs_migration(conn):
    """
    返回这个库还差哪几项。两项互相独立，可能只差一项，也可能两项都差：

      contentless  FTS 还带着一份没人读的正文副本（老结构，费磁盘）
      twocol       FTS 还是单列 body，名字和文件列表混在一起（排序分不出轻重）

    两项都靠「另建一张表、全库重喂一遍、换掉旧表」来解决，所以一趟做完，
    不用迁两次。
    """
    todo = []
    if not is_contentless(conn):
        todo.append("contentless")
    if not fts_two_col(conn):
        todo.append("twocol")
    return todo


def part_sizes(conn):
    """按影子表分开量。dbstat 要 SQLite 编译时带 DBSTAT 虚表，主流构建都带。"""
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
    except sqlite3.Error:
        return {}


def report(conn, path):
    n = conn.execute("SELECT COUNT(*) FROM torrents").fetchone()[0]
    on_disk = sum(os.path.getsize(path + s) for s in ("", "-wal", "-shm")
                  if os.path.exists(path + s))
    print("库里 %s 条，占 %s" % (format(n, ","), human(on_disk)))
    parts = part_sizes(conn)
    if parts:
        print("\n分表占用：")
        for name, sz in sorted(parts.items(), key=lambda kv: -kv[1])[:8]:
            mark = "   <- 这份没人读" if name == "torrents_fts_content" else ""
            print("  %-30s %10s%s" % (name, human(sz), mark))
    waste = parts.get("torrents_fts_content", 0)
    if waste:
        print("\n迁完能省下 %s（占当前库的 %.0f%%）"
              % (human(waste), waste * 100.0 / max(on_disk, 1)))
    return n, waste


def migrate(path, go=False, quiet=False):
    conn = sqlite3.connect(path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-200000")     # 200 MB 页缓存，重建时值回票价

    todo = needs_migration(conn)
    if not todo:
        print("这个库的全文索引已经是最新结构了（无正文 + 名字/文件列表分列），不用迁。")
        conn.close()
        return 0
    print("这个库要迁的是：%s\n" % "、".join(
        {"contentless": "去掉没人读的正文副本（省磁盘）",
         "twocol": "把名字和文件列表分成两列（搜索排序才分得出轻重）"}[t]
        for t in todo))

    total, waste = report(conn, path)
    if not go:
        print("\n这只是估算。真要动手加 --go。"
              "\n动手前把爬虫和网页停掉——迁移要独占写。")
        conn.close()
        return 0

    # 续跑：新表已经建到哪一条了
    done_to = 0
    if table_sql(conn, NEW) and not fts_two_col(conn, NEW):
        # 上次是用更早的版本迁到一半的，那张半成品是单列的，接不上。
        # 扔掉重来——它还没被任何人用过（换表是最后一步），扔掉不损失什么
        print("\n发现上次留下的半成品新表是旧结构（单列），丢掉重建。")
        conn.execute("DROP TABLE %s" % NEW)
        conn.commit()
    if table_sql(conn, NEW):
        done_to = conn.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM %s" % NEW).fetchone()[0]
        print("\n发现上次没迁完的新表，从 rowid %s 接着来。" % format(done_to, ","))
    else:
        conn.execute(
            "CREATE VIRTUAL TABLE %s USING fts5(name, files, tokenize='unicode61', "
            "content='', contentless_delete=1)" % NEW)
        conn.commit()
        print("\n新表建好了。旧表先留着，搜索这期间照常能用。")

    remaining = conn.execute(
        "SELECT COUNT(*) FROM torrents WHERE rowid > ?", (done_to,)).fetchone()[0]
    print("要重建 %s 条。可以随时 Ctrl-C，下次接着跑。\n" % format(remaining, ","))

    t0 = time.time()
    moved = 0
    while True:
        # 按 rowid 顺序取，一来是顺序读快，二来 MAX(rowid) 才能当断点用
        rows = conn.execute(
            "SELECT rowid, name, filelist FROM torrents "
            "WHERE rowid > ? ORDER BY rowid LIMIT ?", (done_to, BATCH)).fetchall()
        if not rows:
            break
        payload = []
        for r in rows:
            name, fl = r["name"] or "", r["filelist"] or ""
            # 拼法必须和 btindex.fts_write 里一模一样，差一个空格，
            # 迁完的库搜出来的东西就和迁之前不一样了
            payload.append((r["rowid"], "%s %s" % (name, expand_text(name)),
                            "%s %s" % (fl, expand_text(fl))))
        with conn:
            conn.executemany(
                "INSERT INTO %s(rowid, name, files) VALUES (?,?,?)" % NEW, payload)
        done_to = rows[-1]["rowid"]
        moved += len(rows)
        if not quiet:
            el = time.time() - t0
            rate = moved / el if el else 0
            left = (remaining - moved) / rate if rate else 0
            print("\r  %s / %s 条  %.0f 条/秒  预计还要 %s      "
                  % (format(moved, ","), format(remaining, ","), rate,
                     fmt_eta(left)), end="", flush=True)
    if not quiet:
        print()

    # 换表之前先数一遍。少了就说明中间漏了，这时候宁可不换——
    # 旧表还在，搜索不受影响，重跑一次就是了
    n_new = conn.execute("SELECT COUNT(*) FROM %s" % NEW).fetchone()[0]
    n_old = conn.execute("SELECT COUNT(*) FROM torrents").fetchone()[0]
    if n_new != n_old:
        conn.close()
        sys.exit("\n对不上：主表 %s 条，新索引 %s 条。旧表原样没动，"
                 "搜索照常。再跑一次这个脚本接着补。"
                 % (format(n_old, ","), format(n_new, ",")))

    print("新索引 %s 条，和主表对上了。正在换表…" % format(n_new, ","))
    with conn:
        conn.execute("DROP TABLE torrents_fts")
        conn.execute("ALTER TABLE %s RENAME TO torrents_fts" % NEW)
    # 改名会连影子表一起改（_data/_idx/_docsize/_config），实测过
    conn.execute("INSERT INTO torrents_fts(torrents_fts) VALUES('optimize')")
    conn.commit()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass

    after = sum(os.path.getsize(path + s) for s in ("", "-wal", "-shm")
                if os.path.exists(path + s))
    print("\n迁完了，用时 %s。" % fmt_eta(time.time() - t0))
    print("库现在 %s。" % human(after))
    print("腾出来的是空闲页，文件不会自己缩——爬虫接着写就会复用。"
          "\n想立刻还给磁盘就跑：btprune.py vacuum"
          "（那一步要额外一份等同最终大小的临时空间，先看看盘够不够）。")
    conn.close()
    return 0


def fmt_eta(sec):
    sec = int(max(sec, 0))
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 小时 %d 分" % (sec // 3600, (sec % 3600) // 60)


def main():
    try:
        from btcompat import setup_console
        setup_console()
    except ImportError:
        pass
    ap = argparse.ArgumentParser(
        description="把已有的库迁到无正文 FTS，省掉那份没人读的正文副本")
    ap.add_argument("--db", default="bt.db")
    ap.add_argument("--go", action="store_true", help="真的动手，不加就只估算")
    ap.add_argument("--quiet", action="store_true", help="不打进度")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        sys.exit("找不到 %s" % args.db)
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        # 原来这里写的是「不迁也能用，只是多占那 47%」。那句现在不成立了：
        # btindex.py 的 SCHEMA 本身就要无正文表，版本不够连新库都建不出来，
        # 不只是迁不了。别让人以为忍着多占点地方就能凑合用。
        sys.exit("你这套 Python 带的 SQLite 是 %s，无正文 FTS 要 %d.%d 以上。"
                 "\n不只是迁不了——这套索引本来就要这个表结构，版本不够整个工具都跑不起来。"
                 "\n换 python.org 上新一点的安装包（3.12 及以上肯定够），再跑一遍 check.bat。"
                 % (sqlite3.sqlite_version, MIN_SQLITE[0], MIN_SQLITE[1]))
    try:
        sys.exit(migrate(args.db, args.go, args.quiet))
    except KeyboardInterrupt:
        # 上面明说了「可以随时 Ctrl-C」，那按下去就不该甩一脸 traceback。
        # 已经写进新表的批次都提交过了，旧表一直没动，这会儿是干净的中间态
        print("\n\n停下了。已经迁好的部分留着了，旧索引原样没动，搜索照常能用。"
              "\n想接着迁，再跑一遍同样的命令就行。")
        sys.exit(130)


if __name__ == "__main__":
    main()
