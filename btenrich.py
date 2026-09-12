#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btenrich —— 给库里缺文件列表的条目补全元数据

为什么会缺：Torznab 协议本身只给标题、体积和磁力链，**没有文件列表这个字段**。
所以从 Jackett 导进来的条目，除非当时开了 --fetch-torrents 把种子下下来解析，
否则库里就只有一个名字。爬虫和本地种子文件导入的条目不受影响，它们拿的是完整元数据。

补全的办法和爬虫一样：infohash 在手，就能去 DHT 问谁有这个种子，
连上去按 BEP 9 把 info 字典要回来，里面有真实名字和完整文件列表。

好处不只是多了文件列表：
  * 种子里的真名通常比索引站的标题准确（标题常被站点改过）
  * 体积是精确值
  * 文件名进全文索引后，合集类种子的内部文件也能搜到

代价是慢：每条要先做一次 DHT 查找，再连 peer 抓元数据，成功率大概一两成，
和爬虫是一个量级。所以按 --limit 分批跑，跑不完下次接着来。

用法：
    py -3.11 btenrich.py --db bt.db --limit 200
    py -3.11 btenrich.py --db bt.db --source torznab --limit 500

依赖：无，标准库足够。
"""

import argparse
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcompat import BUILD, clip, pad, py_cmd, setup_console
from btindex import DB_DEFAULT, Index, human
import btpeers
from dhtmeta import MetaError, describe, fetch_metadata


def ensure_columns(conn):
    """
    加一列记录「试过没有」。不记的话每次都从同一批失败的条目重头试，
    永远轮不到后面的。加列是向后兼容的，其余代码用的都是具名列。
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(torrents)")}
    if "meta_tried" not in have:
        conn.execute("ALTER TABLE torrents ADD COLUMN meta_tried INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_tried ON torrents(meta_tried)")
        conn.commit()
        return True
    return False


def pending(conn, limit, source="", retry_after_days=30):
    """挑出缺文件列表的条目。最近试过的先跳过，给没试过的让路。"""
    cutoff = int(time.time()) - retry_after_days * 86400
    where = ["(filelist IS NULL OR filelist = '')", "meta_tried < ?"]
    params = [cutoff]
    if source:
        where.append("source = ?")
        params.append(source)
    sql = ("SELECT infohash, name, source FROM torrents WHERE %s "
           "ORDER BY meta_tried ASC, hits DESC LIMIT ?" % " AND ".join(where))
    return [dict(r) for r in conn.execute(sql, params + [limit])]


def enrich_one(infohash, lookup_timeout, fetch_timeout, try_peers):
    """
    查 peer -> 抓元数据。返回 (名字, 体积, 文件数, 文件列表) 或抛异常。
    """
    ih = bytes.fromhex(infohash)
    res = btpeers.PeerLookup(timeout=lookup_timeout).run(ih)
    peers = sorted(res["peers"])
    if not peers:
        raise MetaError("DHT 里没人宣告持有它")

    last = None
    for peer in peers[:try_peers]:
        try:
            _, info = fetch_metadata(ih, peer, timeout=fetch_timeout)
            d = describe(info)
            return d["name"], d["total_size"], d["count"], [p for p, _ in d["files"]]
        except (MetaError, OSError, ValueError) as e:
            last = e
    raise MetaError("试了 %d 个 peer 都没拿到：%s" % (len(peers[:try_peers]), last))


def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description="给缺文件列表的条目从 DHT 补全元数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""说明：
  Torznab 协议不提供文件列表，所以 Jackett 导入的条目默认只有名字。
  这个工具用 infohash 去 DHT 把真实元数据取回来补上。

  成功率一两成属正常 —— 很多种子此刻 DHT 里没人在传，或者持有者在 NAT 后面连不上。
  失败的会记下来，30 天内不再重试，下次自动轮到别的条目。
""")
    ap.add_argument("--db", default=DB_DEFAULT, help="索引路径（默认 %s）" % DB_DEFAULT)
    ap.add_argument("--limit", type=int, default=200, help="这次补多少条")
    ap.add_argument("--source", default="", help="只补某个来源，如 torznab")
    ap.add_argument("--workers", type=int, default=10, help="并发数")
    ap.add_argument("--lookup-timeout", type=float, default=10, help="单次 DHT 查找超时")
    ap.add_argument("--timeout", type=float, default=8, help="单个 peer 抓取超时")
    ap.add_argument("--try-peers", type=int, default=6, help="每条最多试几个 peer")
    ap.add_argument("--retry-after", type=int, default=30, help="失败后隔多少天再试")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit("找不到 %s" % args.db)

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    if ensure_columns(conn):
        print("已给索引补上 meta_tried 列")

    total_missing = conn.execute(
        "SELECT count(*) FROM torrents WHERE filelist IS NULL OR filelist = ''"
    ).fetchone()[0]
    rows = pending(conn, args.limit, args.source, args.retry_after)
    conn.close()

    print("[构建 %s] 缺文件列表的共 %s 条，这次处理 %d 条"
          % (BUILD, format(total_missing, ","), len(rows)))
    if not rows:
        print("没有需要补的。都补过了，或者都在 %d 天的重试间隔内。" % args.retry_after)
        return
    print("-" * 70)

    idx = Index(args.db)
    lock = threading.Lock()
    stat = {"ok": 0, "fail": 0, "done": 0}
    last_commit = [time.time()]
    reasons = {}
    now = int(time.time())

    def work(row):
        ih = row["infohash"]
        try:
            name, size, nfiles, files = enrich_one(
                ih, args.lookup_timeout, args.timeout, args.try_peers)
            with lock:
                # upsert 在记录已存在时会更新名字/体积/文件列表并重建 FTS 行，
                # 所以直接复用它，不用自己写更新逻辑。source 原样保留。
                idx.upsert(ih, name, size, nfiles, files, source=row["source"] or "dht")
                idx.db.execute("UPDATE torrents SET meta_tried=? WHERE infohash=?", (now, ih))
                stat["ok"] += 1
                print("  %s  %s  %d 个文件"
                      % (pad(human(size), 9, right=True), clip(name, 46), nfiles))
        except Exception as e:
            why = str(e)[:56]
            with lock:
                stat["fail"] += 1
                reasons[why] = reasons.get(why, 0) + 1
                idx.db.execute("UPDATE torrents SET meta_tried=? WHERE infohash=?", (now, ih))
        finally:
            with lock:
                stat["done"] += 1
                # 提交要勤：没提交的改动别人读不到（网页那边是独立的只读连接），
                # 而且中途点「停止」是直接杀进程，未提交的成果全丢。
                # 按条数或时间任一满足就提交，慢任务也不会攒太久。
                if stat["done"] % 10 == 0 or time.time() - last_commit[0] > 3:
                    idx.commit()
                    last_commit[0] = time.time()
                if stat["done"] % 50 == 0:
                    print("  … 已处理 %d/%d，成功 %d"
                          % (stat["done"], len(rows), stat["ok"]), file=sys.stderr)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(work, rows))
    except KeyboardInterrupt:
        print("\n手动中断，已补上的会保留。")
    finally:
        idx.commit()
        idx.close()

    print("-" * 70)
    print("补上 %d 条，失败 %d 条%s"
          % (stat["ok"], stat["fail"],
             ("（成功率 %.0f%%）" % (stat["ok"] * 100.0 / len(rows))) if rows else ""))
    if reasons:
        print("\n失败原因：")
        for why, cnt in sorted(reasons.items(), key=lambda kv: -kv[1])[:6]:
            print("  %5d 次  %s" % (cnt, why))
        print("\n这个成功率是正常的：很多种子此刻 DHT 里没人在传，"
              "或者持有者在 NAT 后面连不上。")
    left = max(total_missing - stat["ok"], 0)
    if left:
        print("\n还有 %s 条缺文件列表，再跑一次会自动接着补："
              % format(left, ","))
        print("  %s btenrich.py --db %s --limit %d" % (py_cmd(), args.db, args.limit))


if __name__ == "__main__":
    main()
