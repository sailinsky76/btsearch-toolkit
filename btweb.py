#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btweb —— 给本地种子索引配一个网页界面

设计上有三条硬约束，都是因为索引里的名字全是陌生人写的：

  1. 只读打开数据库。连接用 file:...?mode=ro 开，这个进程在数据库层面就没有写权限，
     哪怕代码写错了也改不动你辛苦爬来的库。
  2. 所有从库里取出的文本都过 html.escape。种子名里塞 <script> 是真实存在的事。
  3. 响应头带严格的 CSP，页面里没有任何内联脚本和内联样式。
     万一哪处转义漏了，浏览器也不会执行注入进来的脚本——纵深防御。

默认只监听 127.0.0.1。要对外开放请自己想清楚再改 --host（见启动时的提示）。

用法：
    python3 btweb.py --db bt.db
    然后浏览器打开 http://127.0.0.1:8080

依赖：无，标准库足够。
"""

import argparse
import html
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcompat import BUILD, db_uri, py_cmd, setup_console
from btindex import DB_DEFAULT, Index, build_match, human, magnet, parse_size

PER_PAGE = 25
MAX_LIMIT = 100
MAX_QUERY = 200
MAX_PAGE = 400

# ── 下面几个数只在库大起来以后才有意义，但必须提前定好 ────────────────────
# 库到千万级以后，「扫一遍全表」这个动作就不能再出现在页面的必经路上了：
# 实测 600 万条（4.9 GB）时 COUNT+SUM 一次 2.5 秒、GROUP BY source 一次 2.7 秒，
# 而且是线性涨的——1 亿条就是一分多钟，每开一个页面都要付。
STATS_TTL = 90          # 顶栏统计数字缓存多久。数字晚一分半无所谓，卡一分钟有所谓
STATS_CACHE_FROM = 100000   # 超过这个条数才启用缓存，以下实算——见 get_stats
COUNT_CAP = 50000       # 「共 N 条匹配」最多数到这儿，再多就显示 50,000+
LIKE_WINDOW = 500000    # 单字查询退回 LIKE 时，只扫最近入库的这么多条
QUERY_DEADLINE = 8      # 网页上任何一条查询的墙钟上限，超时就报错而不是把页面吊死
DELETE_BATCH = 20000    # 按条件批量删除时每批多少行，避免一个事务跑几小时

HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")

# 和 btindex 里的排序保持一致。这里没有复用它的 search()，
# 是因为那个方法只能在可写连接上跑，而且不支持翻页；
# 但查询表达式一定要用它的 build_match——入库展开和查询展开必须同一套规则。
ORDER_SQL = {
    "relevance": "bm25(torrents_fts), t.hits DESC",
    "hits":      "t.hits DESC, bm25(torrents_fts)",
    "size":      "t.size DESC",
    "date":      "t.last_seen DESC",
}
ORDER_LABEL = [("relevance", "相关度"), ("hits", "热度"),
               ("size", "体积"), ("date", "最近出现")]
SIZE_LABEL = [("", "不限"), ("100MB", "100 MB 以上"), ("1GB", "1 GB 以上"),
              ("5GB", "5 GB 以上"), ("20GB", "20 GB 以上")]


# --------------------------------------------------------------------------
# 只读数据访问
# --------------------------------------------------------------------------

_local = threading.local()
DB_PATH = [DB_DEFAULT]      # 启动时定下来，后台线程要靠它自己开连接


def conn_for(path):
    """每个线程一个连接。sqlite3 的连接对象不是线程安全的，共用会随机炸。"""
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(db_uri(path, readonly=True), uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            # 库大到几百万条时，内存映射读比常规 I/O 快一到两成，白捡的。
            # 调 cache_size 反而没用——实测首次查询更慢，因为要先填页缓存。
            c.execute("PRAGMA mmap_size=1073741824")
        except sqlite3.Error:
            pass
        _local.conn = c
    return c


class _Deadline:
    """
    给一条查询套上墙钟上限。

    SQLite 没有语句超时，只有 progress handler：每跑若干步虚拟机指令回调一次，
    返回非 0 就中断，抛 OperationalError。没有这道闸，一条扫全表的查询会把
    那个工作线程按住几分钟，而用户那头只看到浏览器一直转圈——刷新一次
    再来一条，线程就这么堆起来。宁可明说「这个搜法太慢」，也别装作在算。
    """

    def __init__(self, conn, seconds=QUERY_DEADLINE):
        self.conn, self.end = conn, time.time() + seconds

    def __enter__(self):
        try:                     # 每两万步查一次表，开销可以忽略
            self.conn.set_progress_handler(
                lambda: 1 if time.time() > self.end else 0, 20000)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        try:
            self.conn.set_progress_handler(None, 0)
        except Exception:
            pass
        return False


# 算一次管一阵子的小缓存。键 -> [值, 算好的时刻, 是否有人正在后台重算]
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def cached(key, ttl, fn):
    """
    过期不等新值：手上有旧值就先把旧值交出去，新的丢给后台线程慢慢算。

    这是整份代码里对付大库最要紧的一招。顶栏那行「库里 N 条 / 内容总量」
    和来源下拉，每开一个页面都要各扫一遍全表；库小的时候几十毫秒看不出来，
    千万级以上就变成页面在等数据库。而这两个数字晚一分半钟根本没人在乎。
    """
    now = time.time()
    with _CACHE_LOCK:
        ent = _CACHE.get(key)
        if ent and now - ent[1] < ttl:
            return ent[0]
        if ent is None:
            _CACHE[key] = ent = [None, 0.0, False]
        if ent[2]:                       # 已经有人在算了，别再开一个
            return ent[0]
        ent[2] = True

    if ent[0] is None:
        # 第一次，没有旧值可交，只能当场算。启动时的预热就是为了把这一次
        # 挪到用户切去浏览器的那几秒里
        try:
            ent[0], ent[1] = fn(), time.time()
        except Exception:
            pass
        finally:
            ent[2] = False
        return ent[0]

    def refresh():
        try:
            v = fn()
            with _CACHE_LOCK:
                ent[0], ent[1] = v, time.time()
        except Exception:
            pass
        finally:
            ent[2] = False

    threading.Thread(target=refresh, daemon=True).start()
    return ent[0]


def do_search(conn, query, page=1, sort="relevance", min_size=0, per_page=PER_PAGE,
              source=""):
    """
    多取一条用来判断有没有下一页，比 COUNT(*) 便宜太多。

    没给关键词时走「浏览全部」：直接查主表，不经过 FTS。
    相关度排序在没有查询词时没有意义，自动退成按最近出现排。
    """
    # 单字查询 FTS 够不着，和 btindex.search 走同一套 LIKE 回退，
    # 否则会出现命令行搜得到、网页搜不到的割裂
    single_char = bool(query) and Index.needs_like(query)
    match = "" if single_char else (build_match(query) if query else "")
    where, params = [], []
    if min_size:
        where.append("t.size >= ?")
        params.append(min_size)
    if source:
        where.append("t.source = ?")
        params.append(source)

    if match:
        where.insert(0, "torrents_fts MATCH ?")
        params.insert(0, match)
        order = ORDER_SQL.get(sort, ORDER_SQL["relevance"])
        sql = ("SELECT t.* FROM torrents_fts "
               "JOIN torrents t ON t.rowid = torrents_fts.rowid "
               "WHERE %s ORDER BY %s LIMIT ? OFFSET ?"
               % (" AND ".join(where), order))
    else:
        if single_char:
            clause, lp = Index.like_clause(query)
            if clause:
                where.insert(0, clause)
                params[:0] = lp
                # LIKE 没有任何索引可用，是实打实的逐行比对。600 万条的库里
                # 搜一个「猫」要 15.7 秒（命中 0 条时最惨，得扫到底才知道没有），
                # 而这个时间是跟着库线性涨的。所以给它划一扇窗：只看最近入库的
                # 这么多条。rowid 是自增的，rowid > MAX-N 正好就是「最近 N 条」，
                # 而且是顺序读，代价固定，多大的库都一样快。
                # 代价是单字搜索从此只覆盖库的一部分——这件事必须在结果页上
                # 明说，不能让人以为搜的是全库。
                where.insert(0, "t.rowid > (SELECT MAX(rowid) FROM torrents) - %d"
                                % LIKE_WINDOW)
        order = ORDER_SQL.get(sort, ORDER_SQL["date"])
        if "bm25" in order:            # 没有 FTS 参与时相关度无从谈起
            order = ORDER_SQL["date"]
        if single_char:
            # 这个加号不是笔误，少了它上面那扇窗等于没开。
            # ORDER BY t.hits DESC 会诱使 SQLite 走 idx_hits 从头扫整个索引、
            # 逐行回表验 rowid 范围——查询计划里是 SCAN t USING INDEX idx_hits，
            # 窗口条件退化成一个过滤器，该扫多少还是扫多少。
            # 一元加号让这个表达式不再「是某个索引的列」，SQLite 于是改走
            # SEARCH t USING INTEGER PRIMARY KEY (rowid>?)，老老实实只读窗口内
            # 那一段，排序丢给临时 B 树。实测 1.21 秒变 0.26 秒，
            # 而且这回是真的不随库长大了。
            order = "+" + order.replace("t.", "").replace(", ", ", +")
        sql = ("SELECT t.* FROM torrents t %s ORDER BY %s LIMIT ? OFFSET ?"
               % (("WHERE " + " AND ".join(where)) if where else "", order))
    params += [per_page + 1, (page - 1) * per_page]
    try:
        with _Deadline(conn):
            rows = [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError as e:
        if "interrupt" in str(e).lower():
            raise ValueError("这个搜法在当前库的规模下太慢了（超过 %d 秒），"
                             "换个更具体的词试试" % QUERY_DEADLINE)
        raise ValueError("查询无法执行: %s" % e)
    return rows[:per_page], len(rows) > per_page


def get_one(conn, infohash):
    if not HEX40.match(infohash or ""):
        return None
    r = conn.execute("SELECT * FROM torrents WHERE infohash=?",
                     (infohash.lower(),)).fetchone()
    return dict(r) if r else None


TASKS = [None]                     # 启动时装上 TaskManager，--no-tasks 时保持 None
CSRF = [os.urandom(16).hex()]      # 每次启动随机生成，跨站页面拿不到
ALLOW_DELETE = [True]
# 上一次真正看过的那份列表（形如 "?q=&sort=size"）。空手回到 / 时拿它把人送回原处：
# 从列表切去任务面板再点站名回来，不该落到一张空提示卡上。
# 只在内存里，重启就清空，所以 web.bat 每次开起来仍然是落地页。
# 存的是重新拼过的参数串而不是原始查询串，写进 Location 头才不会夹带东西。
LAST_VIEW = [""]


def delete_by_hashes(path, hashes):
    """
    按 infohash 删除。只在真的要删时才开一个可写连接，读路径始终是只读的。

    两张表必须一起删：FTS 表没有触发器，只删主表会留下孤儿行，
    等 rowid 被回收后新种子插进来就撞上残留条目，爬虫会 IntegrityError 崩掉。
    """
    clean = [h.lower() for h in hashes if HEX40.match(h or "")]
    if not clean:
        return 0
    conn = sqlite3.connect(path, timeout=20)
    try:
        with conn:
            marks = ",".join("?" * len(clean))
            conn.execute("DELETE FROM torrents_fts WHERE rowid IN "
                         "(SELECT rowid FROM torrents WHERE infohash IN (%s))" % marks,
                         clean)
            cur = conn.execute("DELETE FROM torrents WHERE infohash IN (%s)" % marks,
                               clean)
            n = cur.rowcount
        _CACHE.clear()              # 条数变了，顶栏的缓存作废
        return n
    finally:
        conn.close()


def delete_by_filter(path, query, min_size=0, source=""):
    """
    按当前筛选条件删除。「全选删除全部」走的是这条——
    几万条逐个勾选不现实，也不该把上万个 infohash 塞进请求体。

    这里有个必须先把 rowid 固化下来的理由：筛选条件里含关键词时会查 torrents_fts，
    而第一步正是删 FTS 行。如果直接用同一个条件删两次，第一步就把第二步的依据毁了，
    结果是 FTS 没了、主表还在——数据变成搜不到的僵尸行，而且返回的删除数是 0，
    表面上看像什么都没发生。所以先把命中的 rowid 落到临时表，再照着它删两张表。
    """
    where, params = [], []
    match = build_match(query) if query else ""
    if match:
        where.append("rowid IN (SELECT rowid FROM torrents_fts "
                     "WHERE torrents_fts MATCH ?)")
        params.append(match)
    if min_size:
        where.append("size >= ?")
        params.append(min_size)
    if source:
        where.append("source = ?")
        params.append(source)
    cond = (" WHERE " + " AND ".join(where)) if where else ""

    conn = sqlite3.connect(path, timeout=20)
    try:
        # 分批。老写法是一个事务干完：先把命中的 rowid 全灌进临时表，再删两张表。
        # 在小库上没问题，库大起来就是另一回事了——「浏览全部」下点删除全部，
        # 命中的就是整张表，一亿个 rowid 先落一遍临时文件，然后一个事务里删
        # 一亿行主表加一亿行 FTS，WAL 会涨到和库同量级，中途断电或者按了 Ctrl-C
        # 就是几十 GB 的回滚。而且整个过程里库是锁着的，网页那头什么也干不了。
        # 改成每 DELETE_BATCH 行提交一次：随时可以中断，中断了也是删了一半，
        # 不会留下半个事务；WAL 始终是小的。
        total = 0
        while True:
            with conn:
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS _del(rid INTEGER PRIMARY KEY)")
                conn.execute("DELETE FROM _del")
                conn.execute(
                    "INSERT INTO _del(rid) SELECT rowid FROM torrents%s LIMIT %d"
                    % (cond, DELETE_BATCH), params)
                got = conn.execute("SELECT count(*) FROM _del").fetchone()[0]
                if got:
                    # FTS 必须先删。第一步删的正是筛选条件要读的那张表，
                    # 所以 rowid 得先固化到 _del 里，否则第一步会把第二步的
                    # 依据毁掉，结果是主表留下搜不到的僵尸行
                    conn.execute("DELETE FROM torrents_fts "
                                 "WHERE rowid IN (SELECT rid FROM _del)")
                    cur = conn.execute("DELETE FROM torrents "
                                       "WHERE rowid IN (SELECT rid FROM _del)")
                    total += cur.rowcount
            if got < DELETE_BATCH:
                break
        try:
            conn.execute("DROP TABLE IF EXISTS _del")
        except sqlite3.Error:
            pass
        _CACHE.clear()              # 删完统计数字就不作数了，下次重算
        return total
    finally:
        conn.close()


def count_by_filter(conn, query, min_size=0, source=""):
    where, params = [], []
    match = build_match(query) if query else ""
    if match:
        where.append("rowid IN (SELECT rowid FROM torrents_fts "
                     "WHERE torrents_fts MATCH ?)")
        params.append(match)
    if min_size:
        where.append("size >= ?")
        params.append(min_size)
    if source:
        where.append("source = ?")
        params.append(source)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    # 数到上限就收手。这个数只用来写「删除当前筛选的全部 N 条」这句话，
    # 而为了这句话去数穿整张表是不划算的：600 万条的库里搜 1080p 命中 45 万条，
    # 老写法要把这 45 万行全数一遍。封顶之后最多数 5 万行就停，
    # 而且显示成「50,000+」反倒让人对这个删除按钮更警惕一点。
    sql = ("SELECT count(*) FROM (SELECT 1 FROM torrents%s LIMIT %d)"
           % (cond, COUNT_CAP + 1))
    with _Deadline(conn):
        try:
            n = conn.execute(sql, params).fetchone()[0]
        except sqlite3.OperationalError:
            return 0, False
    return (COUNT_CAP, True) if n > COUNT_CAP else (n, False)


def _safe_size(text):
    try:
        return parse_size(text) if text else 0
    except (ValueError, TypeError):
        return 0


def _list_sources_raw(conn):
    try:
        return [(r["source"] or "", r["n"]) for r in conn.execute(
            "SELECT source, count(*) n FROM torrents GROUP BY source ORDER BY n DESC")]
    except sqlite3.Error:
        return []


def list_sources(conn):
    """来源下拉。没有 idx_source 时这是一次全表扫 + 两棵临时 B 树。"""
    known = (_CACHE.get("stats") or [None])[0]
    if (known or {}).get("count", 0) > STATS_CACHE_FROM:
        return cached("sources", STATS_TTL,
                      lambda: with_own_conn(_list_sources_raw)) or []
    try:                               # 小库实算，理由同 get_stats
        return _list_sources_raw(conn) if conn is not None \
            else with_own_conn(_list_sources_raw)
    except sqlite3.Error:
        return []


def _stats_raw(conn):
    """
    三个聚合分开查，不要写成一条。

    合在一条里 SQLite 只能 SCAN 主表（1.45 GB / 600 万条，2.5 秒）；
    拆开之后每条都走各自的覆盖索引，只读索引不碰主表：
    COUNT 走 idx_hits 0.26 秒、SUM 走 idx_size 0.61 秒、MAX 走 idx_last_seen
    直接跳到末端 0.00 秒。同样的数据，三次查询反而比一次快三倍。
    """
    n = conn.execute("SELECT COUNT(*) FROM torrents").fetchone()[0]
    total = conn.execute(
        "SELECT COALESCE(SUM(size),0) FROM torrents").fetchone()[0] if n else 0
    newest = conn.execute("SELECT MAX(last_seen) FROM torrents").fetchone()[0]
    return {"count": n, "total_size": total, "newest": newest}


def get_stats(conn, path):
    # 库的体积每次都实地量——getsize 是常数时间，而爬虫跑着的时候
    # 用户正想看这个数在涨
    on_disk = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            on_disk += os.path.getsize(path + suffix)
        except OSError:
            pass
    # 缓存只为大库存在。它要挡的是「扫全表太贵」，而那只在库大时成立——
    # 十万条以内扫一遍几十毫秒，套上缓存反而会出怪事：过期那一下先交旧值、
    # 后台再刷，于是页面一边列着刚导进来的 80 条，顶栏一边写「库里 0 条」。
    # 所以小库干脆不缓存，每次实算；大库才用缓存换响应。
    # 门槛之上首次仍要付一次全表扫，启动时的 warm_cache 就是去把它垫掉的。
    known = (_CACHE.get("stats") or [None])[0]
    if (known or {}).get("count", 0) > STATS_CACHE_FROM:
        s = cached("stats", STATS_TTL, lambda: with_own_conn(_stats_raw))
    else:
        try:
            s = with_own_conn(_stats_raw)
            with _CACHE_LOCK:          # 存一份，好让下次知道库有多大
                _CACHE["stats"] = [s, time.time(), False]
        except Exception:
            s = (known or None)
    if s is None:                     # 第一次还没算出来（或者算失败了）
        s = {"count": 0, "total_size": 0, "newest": None}
    out = dict(s)
    out["db_size"] = on_disk
    return out


def has_any_rows(conn):
    """
    库里到底有没有东西——不看缓存，直接问。

    专门给「索引还是空的」那张引导页用。那张页面一旦错报，人就会以为
    刚跑完的导入没生效，而这恰好是第一次用的人最容易撞上的时刻。
    LIMIT 1 是常数时间，一亿条的库上也是一瞬，所以这道确认不怕反复做。
    """
    try:
        return conn.execute("SELECT 1 FROM torrents LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def with_own_conn(fn):
    """
    开一条一次性连接跑 fn，跑完就关。

    缓存过期后是后台线程去重算的，不能借用 conn_for —— 那是按线程缓存的，
    每来一个临时线程就挂一条连接上去，线程退了连接还留着，日积月累就是
    几百个打开的库句柄。这里用完即弃，干净。
    """
    conn = sqlite3.connect(db_uri(DB_PATH[0], readonly=True), uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        return fn(conn)
    finally:
        conn.close()


def warm_cache():
    """
    启动时先把统计算一遍。

    这一次扫全表躲不掉，但可以挪个位置：挪到用户从命令行窗口切去浏览器
    的那几秒里，而不是挪到他打开首页之后干等的那一分钟里。
    """
    def run():
        try:
            # 两个函数都不再用传进去的连接（内部走 with_own_conn），
            # 所以这里给 None，免得在这条一次性线程上挂一条 thread-local 连接
            get_stats(None, DB_PATH[0])
            list_sources(None)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------
# 展示用小工具
# --------------------------------------------------------------------------

def ago(ts):
    if not ts:
        return ""
    d = max(int(time.time()) - int(ts), 0)
    for limit, div, unit in ((3600, 60, "分钟"), (86400, 3600, "小时"),
                             (2592000, 86400, "天"), (31536000, 2592000, "个月")):
        if d < limit:
            return "%d %s前" % (max(d // div, 1), unit)
    return "%d 年前" % max(d // 31536000, 1)


def heat_width(hits):
    """热度条宽度。announce 次数长尾极重，必须走对数，不然一条独大其余全是零。"""
    return min(100, int(math.log(max(int(hits), 1)) / math.log(500) * 100))


def esc(v):
    return html.escape(str(v if v is not None else ""), quote=True)


# --------------------------------------------------------------------------
# 目录浏览：给「导入本地种子」那块面板选文件夹用
# --------------------------------------------------------------------------
#
# 为什么要让服务端来列目录：网页拿不到本地绝对路径。<input type="file"> 出于
# 安全考虑只交文件名和相对路径，拿不到 D:\torrents 这种东西，而 btimport
# 要的恰恰是绝对路径。好在这个服务本来就跑在用户自己机器上，由它来列目录
# 是最直接的办法。
#
# 这确实把文件系统的目录名暴露给了能访问这个页面的人，所以：
#   · 只在任务面板开着时才提供（--no-tasks 会一起关掉）——
#     能起任务的人本来就能扫任意目录，不算新增攻击面
#   · 只回目录名和 .torrent 计数，不回文件名、不读任何文件内容
#   · 绑到 0.0.0.0 的那条警告同样适用，启动时已经喊过了

WALK_BUDGET = 20000        # 数 .torrent 时最多看这么多个条目，防止在 C:\ 上卡死
WALK_SECONDS = 0.6         # 再加一道墙钟。每点一次目录都要走一遍，不能让它磨蹭


def list_drives():
    """Windows 上枚举盘符。GetLogicalDrives 是位图，比逐个 exists 探快得多。"""
    if os.name != "nt":
        return []
    try:
        import ctypes
        bits = ctypes.windll.kernel32.GetLogicalDrives()
        return ["%s:\\" % chr(65 + i) for i in range(26) if bits >> i & 1]
    except Exception:
        import string
        return ["%s:\\" % c for c in string.ascii_uppercase
                if os.path.exists("%s:\\" % c)]


def count_torrents(path):
    """
    这个目录（含子目录）里大概有多少个 .torrent。

    给的是「大概」：走满预算就停，返回 (数量, 是否还没数完)。
    面板上写成「至少 N 个」。这个数的用处是让人确认自己站对了地方，
    不是统计报表，不值得为了精确在 C:\\ 上转几分钟。
    """
    n, seen, t0 = 0, 0, time.time()
    try:
        for root, dirs, names in os.walk(path):
            for f in names:
                seen += 1
                if f.lower().endswith(".torrent"):
                    n += 1
            if seen >= WALK_BUDGET or time.time() - t0 > WALK_SECONDS:
                return n, True
    except (OSError, ValueError):
        pass
    return n, False


def browse_dir(path):
    """列出一个目录下的子目录。path 为空时给盘符列表（Windows）或根。"""
    if not path:
        drives = list_drives()
        if drives:
            return {"path": "", "parent": None, "is_root": True,
                    "entries": [{"name": d, "path": d} for d in drives],
                    "count": 0, "partial": False}
        path = os.path.abspath(os.sep)

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise ValueError("不是一个文件夹：%s" % path)

    entries = []
    try:
        for name in sorted(os.listdir(path), key=lambda s: s.lower()):
            full = os.path.join(path, name)
            try:
                if not os.path.isdir(full):
                    continue
                # 跳过链接，免得在符号链接环里转圈
                if os.path.islink(full):
                    continue
            except OSError:
                continue
            entries.append({"name": name, "path": full})
    except PermissionError:
        raise ValueError("没有权限读这个文件夹")
    except OSError as e:
        raise ValueError("读不了这个文件夹：%s" % e)

    parent = os.path.dirname(path.rstrip(os.sep)) or None
    # 到了盘符根（D:\）再往上就该回到盘符列表，而不是原地打转
    if parent == path or (os.name == "nt" and len(path.rstrip(os.sep)) <= 2):
        parent = ""
    # 盘符根（D:\）和文件系统根（/）不数。那一数就是把整块盘走一遍，
    # 走满预算也数不出个所以然，纯粹是每点一次就白磨一次磁盘
    at_root = (parent == "" or parent is None)
    n, partial = (0, False) if at_root else count_torrents(path)
    return {"path": path, "parent": parent, "is_root": False,
            "entries": entries[:500], "count": n, "partial": partial,
            "counted": not at_root}


def qs(**kw):
    clean = {k: v for k, v in kw.items() if v not in ("", None, 1)}
    return ("?" + urllib.parse.urlencode(clean)) if clean else "/"


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

STYLE = """
/* 这是一台编目仪器，不是落地页。
   一半内容是机器数据（40 位十六进制、字节数、滚动日志），所以等宽字体
   只用来承载数据、不做装饰；力气集中花在搜索框上，其余一律安静。
   配色取深青珐琅 + 暖中性纸，刻意避开千篇一律的靛蓝 SaaS 和奶油色调。

   分隔色只有两档，各自绑定所在的底色，不能混用：
   --hair 是白卡片（--surface）内部的分隔线，对白底 1.23；直接铺在纸底
          （--paper）上只有 1.05，等于没画，所以纸底上不许用它。
   --line 是边框和细标记，对白底 1.38，用来给卡片收边、画热度槽、
          画选择态的左轨、描禁用按钮。
   曾经为「纸底上的分隔线」加过第三档 --rule，结果列表搬进卡片之后
   纸底上已经没有分隔线了，那一档跟着退掉。要是哪天把列表改回平铺，
   得把 --rule（浅 #cbd1c3 / 深 #2e3837）加回来，--hair 在纸底上撑不住。 */
:root{
  --paper:#edeeea; --surface:#ffffff; --sunk:#f4f6f2; --hover:#eef1ec;
  --ink:#14181a; --muted:#5d6b6c; --faint:#8a9698;
  --line:#d9ddd4; --hair:#e6e9e1;
  --accent:#0f6e62; --accent-ink:#ffffff; --accent-soft:#e2eeea;
  --danger:#9b3324;
  --term-bg:#12191a; --term-ink:#c7d5d2;
  --r:8px;
}
@media (prefers-color-scheme:dark){
  :root{
    --paper:#0f1416; --surface:#171d1f; --sunk:#131a1b; --hover:#232b2c;
    --ink:#e6ebe9; --muted:#93a2a2; --faint:#6e7c7d;
    --line:#262f2f; --hair:#1f2827;
    --accent:#58c0ae; --accent-ink:#0b1614; --accent-soft:#14302c;
    --danger:#d4705e;
    --term-bg:#0b1112; --term-ink:#b9c9c6;
  }
}
*{box-sizing:border-box}
/* 滚动条槽常驻。任务面板比大多数搜索结果页长，于是一页有纵向滚动条、
   一页没有，而 .wrap 是 margin:0 auto 居中的——可用宽度差了一个滚动条，
   居中点就跟着挪半个滚动条宽（Windows 上约 8px），切页时整幅界面横晃一下。
   stable 让浏览器不管内容长短都把那条槽留出来，两页的左右边界就锁死了。
   必须写在 html 上：页面级的滚动容器是它，写 body 上不生效 */
html{-webkit-text-size-adjust:100%; scrollbar-gutter:stable}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font:14px/1.55 "Segoe UI Variable Text","Segoe UI",-apple-system,
       BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  font-variant-numeric:tabular-nums;
}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:2px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
.wrap{max-width:940px;margin:0 auto;padding:0 20px}

/* ── 顶部：搜索框就是主角，其余全部让位 ── */
header{
  background:var(--surface); border-bottom:1px solid var(--line);
  padding:20px 0 16px; position:sticky; top:0; z-index:5;
}
h1{margin:0 0 14px;font-size:13px;font-weight:600;letter-spacing:.02em;color:var(--muted)}
h1 a{color:var(--ink)}
a.nav{font-size:12.5px;font-weight:500;margin-left:12px;color:var(--accent)}
form{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=search]{
  flex:1 1 280px; min-width:0; height:46px; padding:0 16px;
  font-size:17px; font-family:inherit; color:var(--ink);
  background:var(--sunk); border:1.5px solid var(--line);
  border-radius:var(--r); transition:border-color .12s, background .12s;
}
input[type=search]:focus{
  outline:none; border-color:var(--accent); background:var(--surface);
  box-shadow:0 0 0 3px var(--accent-soft);
}
input[type=search]::placeholder{color:var(--faint)}
/* 排序/体积这一排要自己占一行，否则宽屏上会被挤到搜索框右边，
   和搜索按钮抢视觉重心 */
.controls{
  flex:0 0 100%; display:flex; gap:10px; align-items:center;
  margin-top:12px; flex-wrap:wrap;
}
.controls label{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted)}
a.browseall{font-size:12.5px;align-self:center;margin-left:2px}
/* 内边距常驻，激活时只填底色不改尺寸，否则整排控件会横向抖一下。
   标签在「编辑列表 / 退出编辑」之间换，都是四个字，宽度不变 */
button.editlist{
  font-size:12.5px; padding:3px 8px; border-radius:5px; margin-left:-4px;
  background:none; border:0; color:var(--accent); font-family:inherit; cursor:pointer;
}
button.editlist:hover{background:var(--accent-soft)}
/* 激活态用淡底加一圈内描边，不用实心强调色。旁边就是搜索按钮，
   那里已经有一块饱和的青了，这排再来一块会变成两个主按钮在抢。
   状态主要由文案承担，底色只是跟着点头。
   描边走 inset box-shadow 而不是 border，免得多出 2px 把整排推歪 */
button.editlist[aria-pressed="true"]{
  background:var(--accent-soft); box-shadow:inset 0 0 0 1px var(--accent);
}
.meta{
  color:var(--faint); font-size:11.5px; margin-top:12px;
  display:flex; gap:20px; flex-wrap:wrap;
}

/* ── 表单控件统一成一套 ── */
select,.ctl input[type=text],.ctl input[type=number],.mag input{
  height:34px; padding:0 10px; font-size:13px; font-family:inherit;
  color:var(--ink); background:var(--surface);
  border:1px solid var(--line); border-radius:6px;
}
select{padding-right:6px;cursor:pointer}
select:focus,.ctl input:focus,.mag input:focus{
  outline:none; border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft);
}
button{font-family:inherit}
button.go{
  height:46px; padding:0 22px; font-size:14.5px; font-weight:600; cursor:pointer;
  color:var(--accent-ink); background:var(--accent); border:0; border-radius:var(--r);
}
button.go:hover{filter:brightness(1.08)}
.ctl button.go{height:34px;padding:0 16px;font-size:13px;border-radius:6px}
button.ghost2{background:none;color:var(--accent);border:1px solid var(--accent)}
button.danger{
  height:34px; padding:0 14px; font-size:13px; font-weight:500; cursor:pointer;
  color:#fff; background:var(--danger); border:0; border-radius:6px;
}
button.danger.ghost{background:none;color:var(--danger);border:1px solid var(--danger)}
button.danger.ghost:hover{background:var(--danger);color:#fff}
/* 禁用态原本是 --hair 实心填充，而工具条铺在纸底上，整颗按钮跟着隐形，
   看起来像「删除选中」这个按钮压根不存在。改成描边灰，形状留住，份量退下去。
   写在 .ghost 之后，是因为两条选择器权重相同，靠顺序决胜 */
button.danger:disabled,button.danger.ghost:disabled{
  background:none; border:1px solid var(--line); color:var(--faint); cursor:default;
}
button.danger.ghost:disabled:hover{background:none;color:var(--faint)}

/* ── 结果：整份列表收进一张卡片，和空状态、详情、任务面板同一套语言 ── */
main{padding-top:6px}
/* 卡片自己不留内边距，行和工具条各自贴边铺满，分隔线才能横穿整幅宽度。
   overflow:hidden 是为了让首尾两行的悬停底色被圆角裁住，
   否则第一行悬停时会在卡片顶上顶出两个直角 */
.listcard{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--r);
  overflow:hidden; margin:18px 0 0;
}
ol.results{list-style:none;margin:0;padding:0}
ol.results li{
  border-bottom:1px solid var(--hair); border-left:3px solid transparent;
  padding:14px 16px 13px 13px;     /* 左边 13 + 3px 左轨 = 和工具条的 16 对齐 */
  transition:background .1s;
}
/* 末行不画线。卡片下边框已经把列表收住了，再来一道就是双线 */
ol.results li:last-child{border-bottom:0}
ol.results li:hover{background:var(--hover);border-left-color:var(--accent-soft)}
.row1{display:flex;gap:12px;align-items:baseline}
.name{flex:1;min-width:0;font-size:14.5px;font-weight:500;line-height:1.45;
  overflow-wrap:anywhere;word-break:normal}
.name a{color:var(--ink)}
.name a:hover{color:var(--accent)}
.size{
  color:var(--muted); white-space:nowrap; font-size:13px; font-weight:500;
  font-variant-numeric:tabular-nums;
}
.row2{
  display:flex; gap:14px; align-items:center; margin-top:7px;
  color:var(--faint); font-size:11.5px; flex-wrap:wrap;
}
.hash{
  font-family:ui-monospace,"Cascadia Code",SFMono-Regular,Consolas,monospace;
  font-size:11px; color:var(--faint); letter-spacing:.01em;
}
.heat{
  display:inline-block; width:44px; height:3px; background:var(--line);
  border-radius:2px; overflow:hidden; vertical-align:middle;
}
.heat i{display:block;height:100%;background:var(--accent)}
.copy{cursor:pointer;color:var(--accent);background:none;border:0;padding:0;font:inherit}
/* 勾选框默认不占位。display:none 而不是 visibility，
   这样标题的左缘在非选择状态下是齐的，列表扫读不被打断 */
input.pick{display:none;margin:0 2px 0 0;flex:none;accent-color:var(--accent);cursor:pointer}
body.selmode input.pick{display:inline-block}
/* 选择模式下不再给每行画左轨。勾选框冒出来、工具条展开成一排按钮，
   模式已经说得够清楚了；再加一道轨，还会跟悬停那道撞在一起，
   而且轨色比悬停的 accent-soft 还重，看起来像悬停把行变淡了 */
/* 工具条只在编辑模式下出现。平时列表上顶着一条只装了一个按钮的横条，
   份量和它承担的东西不匹配；入口挪去筛选那一排之后，这里剩下的全是
   真正要对选中项动手的操作，占一行才站得住。
   位置仍在卡片内、列表上方：它操作的就是下面这些行 */
.tools{
  display:none; gap:12px; align-items:center; flex-wrap:wrap;
  padding:10px 16px; border-bottom:1px solid var(--hair); font-size:13px;
  min-height:38px;
}
body.selmode .tools{display:flex}
.tools .all{color:var(--muted);display:flex;gap:7px;align-items:center;cursor:pointer}
#delmsg{color:var(--muted);font-size:12px}
.browsehead{color:var(--faint);font-size:11.5px;margin:14px 0 0}
.pager{display:flex;gap:18px;padding:22px 0 44px;font-size:13.5px}

/* ── 空状态是给方向的地方 ── */
.empty{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--r);
  padding:28px 30px; margin:26px 0;
}
/* 卡片跟着内容区对齐，但正文行长仍然限制在可读范围内 */
/* 不再给正文加行长上限。容器（.wrap 940px）本身已经把行长约束在
   60 来个汉字，对这种一两行的说明足够了；再加一层 max-width 只会让
   右边空出一截，看着像渲染坏了。
   顺带记一笔：行长如果真要限，中英混排得用 em 不能用 ch ——
   ch 是拉丁 "0" 的宽度，中文字宽约两倍，72ch 实际只放得下 40 个汉字。 */
.empty p{margin:0 0 10px;color:var(--muted);line-height:1.75}
.empty p:last-child{margin-bottom:0}
/* 行内代码就该是行内的。上一版这里写了 display:block，
   结果句子里的 .torrent 变成一整块黑框，把话劈成三段 */
.empty code{
  background:var(--sunk); border:1px solid var(--hair); border-radius:4px;
  padding:1px 6px; font-size:12px; color:var(--ink);
  font-family:ui-monospace,"Cascadia Code",SFMono-Regular,Consolas,monospace;
  overflow-wrap:anywhere;
}
/* 命令块换行显示，不要横向滚动条 —— 要复制的东西藏在滚动条后面最恼人 */
.empty code.block{
  display:block; background:var(--term-bg); color:var(--term-ink);
  border:0; border-radius:6px; padding:12px 14px; margin:12px 0;
  font-size:12.5px; line-height:1.75; white-space:pre-wrap;
}

/* ── 详情 ── */
.detail{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--r);
  padding:24px 26px; margin:24px 0;
}
.detail h2{margin:0 0 16px;font-size:18px;font-weight:600;line-height:1.4;
  overflow-wrap:anywhere}
dl{display:grid;grid-template-columns:auto 1fr;gap:9px 22px;margin:0 0 20px;font-size:13px}
dt{color:var(--faint)}
.nofiles{color:var(--muted);font-size:12.5px;line-height:1.7;margin:0}
.cover{margin:0 0 20px}
.covernote{color:var(--faint);font-size:11.5px;margin:8px 0 0;line-height:1.6}
#coverbox img{max-width:300px;max-height:420px;border-radius:6px;
  border:1px solid var(--line);display:block;margin-top:12px}
#coverbox .err{color:var(--muted);font-size:12.5px;margin-top:10px}
dd{margin:0;overflow-wrap:anywhere}
.mag{display:flex;gap:8px;align-items:center;margin-bottom:20px;flex-wrap:wrap}
.mag input{
  flex:1 1 320px; min-width:0; font-size:12px; background:var(--sunk);
  font-family:ui-monospace,"Cascadia Code",SFMono-Regular,Consolas,monospace;
}
.mag .copy{
  height:34px;padding:0 14px;border:1px solid var(--line);border-radius:6px;
  background:var(--surface);
}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:8px 0;border-top:1px solid var(--hair);overflow-wrap:anywhere}
td.sz{text-align:right;color:var(--faint);white-space:nowrap;padding-left:16px}

/* ── 任务面板 ── */
.panel{
  background:var(--surface); border:1px solid var(--line); border-radius:var(--r);
  padding:20px 22px 22px; margin:22px 0;
}
.panel h2{
  margin:0 0 7px; font-size:16px; font-weight:600; letter-spacing:.01em;
  padding-left:11px; border-left:3px solid var(--accent);
}
.panel .note{margin:0 0 14px;color:var(--muted);font-size:12.5px;line-height:1.7}
.panel code{
  background:var(--sunk); border:1px solid var(--hair); border-radius:4px;
  padding:1px 6px; font-size:11.5px; color:var(--ink);
  font-family:ui-monospace,"Cascadia Code",SFMono-Regular,Consolas,monospace;
  /* anywhere 只在放不下时才断，不会把 btimport.py 这种词从中间劈开 */
  overflow-wrap:anywhere; word-break:normal;
}
/* 命令块不受行长限制，撑满面板 —— 它是要被整条复制走的 */
.panel code.block{
  display:block; padding:10px 13px; margin:0 0 16px; font-size:12px; line-height:1.6;
  background:var(--term-bg); color:var(--term-ink); border-color:var(--term-bg);
}
.panel h2{scroll-margin-top:130px}
/* 四列网格。flex 布局下每个字段按自身内容定宽，结果就是七长八短对不齐；
   网格让所有输入框落在同一组列线上，跨列用 .half / .wide 表达 */
.ctl{
  display:grid; grid-template-columns:repeat(4,1fr);
  gap:14px 16px; align-items:end; margin-bottom:16px;
}
.ctl label{display:flex;flex-direction:column;gap:6px;font-size:11.5px;color:var(--faint);min-width:0}
.ctl label.half{grid-column:span 2}
.ctl label.wide{grid-column:1/-1}
.ctl label.chk{
  flex-direction:row; align-items:center; gap:8px; height:34px;
  cursor:pointer; color:var(--muted); font-size:12.5px;
}
.ctl input[type=checkbox]{accent-color:var(--accent);cursor:pointer;flex:none}
.ctl input[type=text],.ctl input[type=number],.ctl select{width:100%}
.ctl.actions{display:flex;gap:10px;margin-top:2px}
@media (max-width:760px){ .ctl{grid-template-columns:repeat(2,1fr)}
  .ctl label.half,.ctl label.wide{grid-column:1/-1} }
/* ── 文件夹选择器 ── */
/* 输入框和「浏览…」并排。用 flex 而不是让按钮浮在右边，
   因为 .ctl 是网格，label 宽度会随列跨度变，浮动对不齐 */
.pathrow{display:flex;gap:8px}
.pathrow input{flex:1;min-width:0}
.pathrow button{flex:none}
.picker{
  border:1px solid var(--line); border-radius:var(--r);
  background:var(--sunk); margin:-8px 0 16px; overflow:hidden;
}
.pickhead,.pickfoot{
  display:flex; gap:10px; align-items:center;
  padding:10px 12px; background:var(--surface);
}
.pickhead{border-bottom:1px solid var(--hair)}
.pickfoot{border-top:1px solid var(--hair)}
button.up{
  flex:none; font-size:12.5px; padding:4px 10px; border-radius:5px; cursor:pointer;
  background:none; border:1px solid var(--line); color:var(--ink); font-family:inherit;
}
button.up:hover{background:var(--hover)}
button.up:disabled{color:var(--faint);cursor:default}
/* 路径可能很长，让它从左边溢出——尾部（当前在哪）比盘符重要 */
.pkpath{
  flex:1; min-width:0; font-size:12px; color:var(--muted);
  font-family:ui-monospace,"Cascadia Code",SFMono-Regular,Consolas,monospace;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; direction:rtl;
  text-align:left;
}
.pklist{
  list-style:none; margin:0; padding:0;
  max-height:240px; overflow:auto; background:var(--sunk);
}
.pklist li{border-bottom:1px solid var(--hair)}
.pklist li:last-child{border-bottom:0}
.pklist button{
  display:block; width:100%; text-align:left; cursor:pointer;
  padding:8px 12px; font-size:13px; font-family:inherit;
  background:none; border:0; color:var(--ink);
}
.pklist button:hover{background:var(--hover)}
.pklist .empty-hint{padding:12px;color:var(--faint);font-size:12.5px}
.pkcount{flex:1;font-size:12px;color:var(--muted)}

/* 日志做成终端的样子：这些是机器输出，不该和界面文字混为一体 */
pre.log{
  background:var(--term-bg); color:var(--term-ink);
  border:1px solid var(--term-bg); border-radius:6px;
  padding:12px 14px; margin:0; max-height:300px; overflow:auto;
  font-size:12px; line-height:1.5; white-space:pre-wrap; overflow-wrap:anywhere;
  font-family:ui-monospace,"Cascadia Code",SFMono-Regular,Consolas,monospace;
}

@media (max-width:620px){
  .wrap{padding:0 14px}
  input[type=search]{height:42px;font-size:16px}
  button.go{height:42px}
  .row1{flex-wrap:wrap;gap:4px}
  .size{font-size:12.5px}
  dl{grid-template-columns:1fr;gap:2px 0}
  dt{margin-top:8px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""


# --------------------------------------------------------------------------
# 站点图标
# --------------------------------------------------------------------------

# 三条索引行，命中的那一行是实心的，末端坐着一个点。
# 用这个而不是放大镜：放大镜是所有搜索类产品的默认脸，而这东西本质上是
# 一份本地编目，不是一个搜索框。圆点压在中间那行的末端，读出来是
# 「在列表里命中了这一条」。
#
# 几何是照着 16 像素反推的，不是画好再缩：
#   · 三行拉到 y=16.5 / 29.5 / 42.5，行距 13。早先挤到行距 10，
#     圆点的底色环会啃掉上下两行的右端，缩小之后像线条缺了口。
#   · 圆点外面留 2.5 的底色环，让它和中间那行断开。不留环的话
#     两者连成一体，看着像个设置滑块。
#   · 上下两行 68% 白，中间行满白——尺寸缩到 16px 时，
#     亮度差比长度差更容易分辨。
# 颜色直接取 --accent 那支深青珐琅，和界面是同一支。
FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#0f6e62"/>
<g fill="#ffffff">
<rect x="14" y="16.5" width="36" height="5.5" rx="2.75" opacity=".68"/>
<rect x="14" y="29.5" width="24" height="5.5" rx="2.75"/>
<rect x="14" y="42.5" width="30" height="5.5" rx="2.75" opacity=".68"/>
</g>
<circle cx="44" cy="32.25" r="9.5" fill="#0f6e62"/>
<circle cx="44" cy="32.25" r="7" fill="#ffffff"/>
</svg>"""


SCRIPT = r"""
document.addEventListener('click', function(e){
  var b = e.target.closest('.copy'); if(!b) return;
  var text = b.dataset.magnet || (b.previousElementSibling && b.previousElementSibling.value);
  if(!text) return;
  var done = function(){ var o=b.textContent; b.textContent='已复制';
                         setTimeout(function(){ b.textContent=o; }, 1400); };
  if(navigator.clipboard){ navigator.clipboard.writeText(text).then(done, function(){}); }
  else { var t=document.createElement('textarea'); t.value=text; document.body.appendChild(t);
         t.select(); try{ document.execCommand('copy'); done(); }catch(_){}
         document.body.removeChild(t); }
});
(function(){
  var meta = function(n){ var m=document.querySelector('meta[name="'+n+'"]');
                          return m ? m.content : ''; };
  var picks = function(){ return Array.prototype.slice.call(
                            document.querySelectorAll('.pick')); };
  var chosen = function(){ return picks().filter(function(c){ return c.checked; }); };
  var msg = document.getElementById('delmsg');
  var delsel = document.getElementById('delsel');
  var delall = document.getElementById('delall');
  var pickall = document.getElementById('pickall');
  if (!delsel) return;

  function refresh(){
    var n = chosen().length;
    delsel.disabled = !n;
    delsel.textContent = n ? ('删除选中 ' + n + ' 条') : '删除选中';
  }
  document.addEventListener('change', function(e){
    if (e.target === pickall){
      picks().forEach(function(c){ c.checked = pickall.checked; });
    }
    if (e.target.classList && e.target.classList.contains('pick')) {
      if (!e.target.checked && pickall) pickall.checked = false;
    }
    refresh();
  });

  function post(body, done){
    body.token = meta('csrf');
    msg.textContent = '正在删除…';
    fetch('/api/delete', {method:'POST', headers:{'Content-Type':'application/json'},
                          body: JSON.stringify(body)})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.error){ msg.textContent = '失败：' + d.error; return; }
        msg.textContent = '已删除 ' + d.deleted + ' 条'
                          + (d.hint ? '。' + d.hint : '，正在刷新…');
        setTimeout(function(){ location.reload(); }, d.hint ? 6000 : 600);
      })
      .catch(function(e){ msg.textContent = '请求失败：' + e; });
  }

  // 进出都是页头上这一个按钮，标签在两种状态间换。两个词都是四个字，
  // 换文案不会把右边的东西挤动
  var toggle = document.getElementById('selmode');
  function setMode(on){
    document.body.classList.toggle('selmode', on);
    if (toggle){
      toggle.textContent = on ? '退出编辑' : '编辑列表';
      toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    if (!on){                       // 退出时清空，免得下次进来还残留上次的选中
      picks().forEach(function(c){ c.checked = false; });
      if (pickall) pickall.checked = false;
      msg.textContent = '';
      refresh();
    }
  }
  if (toggle) toggle.addEventListener('click', function(){
    setMode(!document.body.classList.contains('selmode'));
  });

  delsel.addEventListener('click', function(){
    var hs = chosen().map(function(c){ return c.value; });
    if (!hs.length) return;
    if (!confirm('确定删除选中的 ' + hs.length + ' 条？删除后无法撤销。')) return;
    post({mode:'hashes', hashes:hs});
  });

  if (delall) delall.addEventListener('click', function(){
    var what = delall.textContent.replace('删除','');
    if (!confirm('确定删除' + what + '？\n\n这会删掉符合当前筛选条件的所有条目，'
                 + '不只是本页。删除后无法撤销。')) return;
    if (!confirm('再确认一次：真的要删除' + what + '？')) return;
    post({mode:'filter', q: meta('filter-q'), min: meta('filter-min'),
          src: meta('filter-src')});
  });
  refresh();
})();

(function(){
  // 按「面板」渲染而不是按任务槽：本地扫描和网络导入共用一个槽，
  // 但服务端会记下任务是从哪块面板发起的，只往那一块的日志区写
  var PANELS = { crawler:'log_crawler', local:'log_local',
                 import:'log_import', maint:'log_maint' };
  if (!document.getElementById('log_crawler')) return;   // 不在任务面板上

  // ── 文件夹选择器 ────────────────────────────────────────────────
  // 服务端列目录，前端只负责走来走去。见 browse_dir 那里对「为什么不能用
  // <input type=file>」的说明。
  (function(){
    var box   = document.getElementById('l_picker');
    var list  = document.getElementById('pk_list');
    var pathEl= document.getElementById('pk_path');
    var cntEl = document.getElementById('pk_count');
    var upBtn = document.getElementById('pk_up');
    var input = document.getElementById('l_path');
    if (!box) return;
    var cur = null, parent = null;

    function go(path){
      list.innerHTML = '<li class="empty-hint">读取中…</li>';
      fetch('/api/browse?path=' + encodeURIComponent(path == null ? '' : path))
        .then(function(r){ return r.json(); })
        .then(function(d){
          if (d.error){ list.innerHTML = '<li class="empty-hint"></li>';
                        list.firstChild.textContent = d.error; return; }
          cur = d.path; parent = d.parent;
          pathEl.textContent = d.path || '选择一个盘符';
          upBtn.disabled = (d.parent === null);
          // 四种情况分开写。早先图省事拼字符串，count 为 0 又没数完时
          // 拼出来是「没找到 个 .torrent」，句子是断的
          cntEl.textContent =
            (d.is_root || !d.counted) ? '' :
            d.count ? ('这个文件夹下' + (d.partial ? '至少有 ' : '有 ')
                       + d.count + ' 个 .torrent')
            : d.partial ? '文件太多没数完，目前还没见到 .torrent'
            : '这个文件夹下没有 .torrent';
          list.innerHTML = '';
          if (!d.entries.length){
            var li = document.createElement('li');
            li.className = 'empty-hint';
            li.textContent = '没有子文件夹';
            list.appendChild(li); return;
          }
          d.entries.forEach(function(en){
            var li = document.createElement('li');
            var b  = document.createElement('button');
            b.type = 'button';
            // 用 textContent 不用 innerHTML：文件夹名是文件系统来的，
            // 里面完全可能有尖括号
            b.textContent = en.name;
            b.addEventListener('click', function(){ go(en.path); });
            li.appendChild(b); list.appendChild(li);
          });
        })
        .catch(function(){ list.innerHTML = '<li class="empty-hint">读取失败</li>'; });
    }

    document.getElementById('l_browse').addEventListener('click', function(){
      box.hidden = !box.hidden;
      // 已经填了路径就从那儿开始逛，省得每次从盘符点起
      if (!box.hidden && cur === null) go(input.value.trim() || null);
    });
    upBtn.addEventListener('click', function(){ if (parent !== null) go(parent); });
    document.getElementById('pk_close').addEventListener('click', function(){
      box.hidden = true; });
    document.getElementById('pk_pick').addEventListener('click', function(){
      if (cur){ input.value = cur; box.hidden = true; }
    });
  })();

  var meta = function(n){ var m=document.querySelector('meta[name="'+n+'"]');
                          return m ? m.content : ''; };
  var val  = function(id){ var e=document.getElementById(id); return e ? e.value : ''; };
  var chk  = function(id){ var e=document.getElementById(id); return !!(e && e.checked); };

  function collect(kind, which){
    if (kind === 'crawler')
      return {port:val('c_port'), workers:val('c_workers'), rate:val('c_rate'),
              lookup_workers:val('c_lw'), with_lookup:chk('c_lookup')};
    if (kind === 'import'){
      // 本地扫描和网络导入是两块独立的表单，别互相拿错字段
      if (which === 'folder') return {source:'folder', path:val('l_path'), limit:100000};
      var host = (val('i_host') || '').replace(/\/+$/, '');
      // Jackett 的索引站 id 一律小写，填成 Anibt 会报 Unknown indexer。
      // 在这儿顺手规范掉，省得让人对着堆栈猜。
      var indexer = (val('i_indexer') || 'all').trim().toLowerCase();
      var endpoint = host + '/api/v2.0/indexers/' + indexer
                   + '/results/torznab/api|' + val('i_apikey').trim();
      return {source:val('i_source'), limit:val('i_limit'), timeout:val('i_timeout'),
              endpoint:endpoint, query:val('i_query'), proxy:val('i_proxy'),
              pages:val('i_pages'), delay:val('i_delay'), fetch_torrents:chk('i_fetch')};
    }
    return {};
  }

  function call(path, body){
    body.token = meta('csrf');
    return fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify(body)})
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.error){ var el = logs[body.kind];
                      if (el) el.textContent = '启动失败：' + d.error; }
        poll();
      })
      .catch(function(e){ console.error(e); });
  }

  document.addEventListener('click', function(e){
    var t = e.target;
    if (t.dataset && t.dataset.start)
      call('/api/task/start', {kind:t.dataset.start,
                               params:collect(t.dataset.start, t.dataset.src)});
    else if (t.dataset && t.dataset.stop)
      call('/api/task/stop', {kind:t.dataset.stop});
    else if (t.dataset && t.dataset.maint)
      call('/api/task/start', {kind:'maint', params:{action:t.dataset.maint, limit:200}});
  });

  // 来源不同，要填的东西不同
  var src = document.getElementById('i_source');
  function syncSource(){
    var tz = document.getElementById('i_torznab');
    if (tz) tz.hidden = (src && src.value !== 'torznab');
  }
  if (src){ src.addEventListener('change', syncSource); syncSource(); }

  function poll(){
    fetch('/api/task/status').then(function(r){ return r.json(); }).then(function(d){
      var byPanel = {};
      Object.keys(d.tasks || {}).forEach(function(k){
        var t = d.tasks[k];
        byPanel[t.panel || k] = t;
      });
      Object.keys(PANELS).forEach(function(p){
        var el = document.getElementById(PANELS[p]); if (!el) return;
        var t = byPanel[p];
        if (!t){ if (el.textContent !== '未运行') el.textContent = '未运行'; return; }
        var head = (t.running ? '● 运行中' : '○ 已结束')
                 + '　' + t.desc + '　' + t.elapsed + ' 秒\n\n';
        var atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        el.textContent = head + t.lines.join('\n');
        if (atBottom) el.scrollTop = el.scrollHeight;    // 只在已经贴底时才跟着滚
      });
    }).catch(function(){});
  }
  poll();
  setInterval(poll, 2000);
})();

(function(){
  var btn = document.getElementById('showcover');
  if (!btn) return;
  btn.addEventListener('click', function(){
    var box = document.getElementById('coverbox');
    if (box.firstChild){                 // 再点一次收起来
      box.innerHTML = '';
      btn.textContent = '显示预览图';
      return;
    }
    var img = new Image();
    img.alt = '预览图';
    img.onerror = function(){
      box.innerHTML = '';
      var p = document.createElement('p');
      p.className = 'err';
      p.textContent = '图片取不到 —— 地址可能失效了，或者这个站在你的网络里不通。';
      box.appendChild(p);
    };
    img.src = btn.dataset.src;
    box.appendChild(img);
    btn.textContent = '收起预览图';
  });
})();

document.addEventListener('keydown', function(e){
  if(e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)){
    e.preventDefault(); var s=document.querySelector('input[type=search]'); if(s) s.focus();
  }
  if(e.key === 'Escape'){
    var c = document.getElementById('selmode');   // 现在是个开关，再点一次就是退出
    if(c && document.body.classList.contains('selmode')) c.click();
  }
});
"""


def page(title, body, q="", sort="relevance", minsz="", stats=None,
         sources=(), src="", editable=False):
    # 编辑入口只在「这一页真有行可编辑」时才出现。空库、无匹配、详情页、
    # 任务面板都不给，按了也没东西可选
    edit_btn = ('<button id="selmode" class="editlist" type="button" '
                'aria-pressed="false">编辑列表</button>') if editable else ""
    opts = lambda items, cur: "".join(
        '<option value="%s"%s>%s</option>'
        % (esc(v), " selected" if v == cur else "", esc(label))
        for v, label in items)

    bar = ""
    if stats:
        bits = ["库里 %s 条" % format(stats["count"], ",")]
        if stats["count"]:
            bits.append("内容总量 %s" % human(stats["total_size"]))
            bits.append("最近更新 %s" % ago(stats["newest"]))
        bits.append("数据库 %s" % human(stats["db_size"]))
        bar = '<div class="meta">%s</div>' % "".join(
            "<span>%s</span>" % esc(b) for b in bits)

    # 始终渲染来源筛选。库为空时 sources 是空的，以前整个下拉就不出现了，
    # 界面看起来像少了个筛选项。
    opts_src = '<option value="">全部来源</option>' + "".join(
        '<option value="%s"%s>%s（%s）</option>'
        % (esc(name), " selected" if name == src else "",
           esc(name or "未标"), format(n, ","))
        for name, n in sources)
    src_sel = '<label>来源 <select name="src">%s</select></label>' % opts_src

    return """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="csrf" content="%s">
<meta name="filter-q" content="%s">
<meta name="filter-min" content="%s">
<meta name="filter-src" content="%s">
<title>%s</title><link rel="stylesheet" href="/style.css?v=%s">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
</head><body>
<header><div class="wrap">
  <h1><a href="/">本地搜索</a> <a class="nav" href="/tasks">任务面板</a></h1>
  <form action="/" method="get" role="search">
    <input type="search" name="q" value="%s" placeholder="搜什么，中英文都行，按 / 聚焦"
           autocomplete="off" autofocus maxlength="%d">
    <button class="go" type="submit">搜索</button>
    <div class="controls">
      <label>排序 <select name="sort">%s</select></label>
      <label>体积 <select name="min">%s</select></label>
      %s
      <a class="browseall" href="/?q=">浏览全部</a>
      %s
    </div>
  </form>
  %s
</div></header>
<main class="wrap">%s</main>
<script src="/app.js"></script>
</body></html>""" % (esc(CSRF[0]), esc(q), esc(minsz), esc(src),      # 四个 meta
                     esc(title), BUILD, esc(q), MAX_QUERY,            # title、样式版本、搜索框
                     opts(ORDER_LABEL, sort), opts(SIZE_LABEL, minsz), src_sel,
                     edit_btn, bar, body)


def render_results(rows, q, page_no, has_next, sort, minsz, src="",
                   total_matching=0, count_capped=False, can_delete=True):
    items = []
    for r in rows:
        link = magnet(r["infohash"], r["name"])
        items.append(
            '<li><div class="row1">'
            '%s'
            '<span class="name"><a href="/t/%s">%s</a></span>'
            '<span class="size">%s</span></div>'
            '<div class="row2">'
            '<span>%d 个文件</span><span>%s</span>'
            '<span class="heat" title="被 announce %d 次"><i style="width:%d%%"></i></span>'
            '<span class="hash">%s</span>'
            '<a href="%s">打开磁力链</a>'
            '<button class="copy" data-magnet="%s" type="button">复制</button>'
            '</div></li>'
            % (('<input type="checkbox" class="pick" value="%s" aria-label="选中">'
                % esc(r["infohash"])) if can_delete else "",
               esc(r["infohash"]), esc(r["name"]), esc(human(r["size"])),
               r["nfiles"], esc(ago(r["last_seen"])),
               r["hits"], heat_width(r["hits"]),
               esc(r["infohash"][:16]), esc(link), esc(link)))

    pager = []
    if page_no > 1:
        pager.append('<a href="%s">← 上一页</a>'
                     % esc(qs(q=q, sort=sort, min=minsz, src=src, page=page_no - 1)))
    if has_next:
        pager.append('<a href="%s">下一页 →</a>'
                     % esc(qs(q=q, sort=sort, min=minsz, src=src, page=page_no + 1)))

    bar = ""
    if can_delete:
        # 计数封顶时说「5 万条以上」而不是报一个假的精确数。
        # 删除是不可逆的，这里宁可含糊也不能骗人
        if total_matching and count_capped:
            scope = "当前筛选的全部（%s 条以上）" % format(total_matching, ",")
        elif total_matching:
            scope = "当前筛选的全部 %s 条" % format(total_matching, ",")
        else:
            scope = "全部"
        # 删除是低频且不可逆的操作，不该常驻在每一行上。入口是筛选那一排的
        # 「编辑列表」，按下才展开这条工具条和每行的勾选框。
        # 退出走同一个按钮（标签换成「退出编辑」），所以这里不再放退出键：
        # 它常驻在吸顶的页头上，翻到列表多深都够得着。
        bar = ('<div class="tools">'
               '<label class="all"><input type="checkbox" id="pickall"> 全选本页</label>'
               '<button id="delsel" class="danger" type="button" disabled>删除选中</button>'
               '<button id="delall" class="danger ghost" type="button">删除%s</button>'
               '<span id="delmsg"></span></div>' % esc(scope))
    # 工具条和列表包在同一张卡片里，翻页条留在卡片外面：
    # 前两者是列表本身，翻页是离开这份列表的动作，不该被框进去
    return ('<div class="listcard">%s<ol class="results">%s</ol></div>'
            '<div class="pager">%s</div>'
            % (bar, "".join(items), "".join(pager)))


EMPTY_DB = """<div class="empty">
<p>索引还是空的。到<a href="/tasks">任务面板</a>去填：扫描本地已有的
   <code>.torrent</code>、从 Jackett 导入，或者启动 DHT 爬虫，点几下就行。</p>
<p>想用命令行的话：</p>
<code class="block">%s btimport.py folder D:\\torrents --db %s
%s dhtmeta.py --sniff --db %s --with-lookup</code>
<p>本地扫描是最快见效的一条，不需要联网。爬虫要公网可达的 UDP 端口，
   在 NAT 后面收获会少很多。</p>
</div>"""

# 空手进来（地址栏就是一个 /，没带任何查询串）时给的页面。
# 一上来就把整库铺开，既慢又没给人任何方向感；这里只留两条路。
LANDING = """<div class="empty">
<p>上面搜点什么，中英文都行。想先看看库里都有什么，点<a href="/?q=">浏览全部</a>，
   或者搜索框留空直接搜，两者是一回事。</p>
<p>往里加内容去<a href="/tasks">任务面板</a>，本地扫描、Jackett 导入、DHT 爬虫都在那儿。</p>
</div>"""


NO_MATCH = """<div class="empty">
<p>没搜到「%s」。</p>
<p>索引里目前 %s 条，只覆盖爬虫运行期间在网络上活跃过的种子。
换个词试试，或者让爬虫再多跑一阵。</p>
</div>"""


TASK_PAGE = """
<div class="panel">
  <h2>DHT 爬虫</h2>
  <p class="note">在网络上被动收集种子，抓到的名字和文件列表写进索引 <code>{{DB}}</code>。
     需要放行 UDP 入站才有好效果，开着不用管，越跑越多。</p>
  <div class="ctl">
    <label>端口 <input id="c_port" type="number" value="6881" min="1" max="65535"></label>
    <label>抓取线程 <input id="c_workers" type="number" value="40" min="1" max="200"></label>
    <label>查询线程 <input id="c_lw" type="number" value="30" min="1" max="200"></label>
    <label>find_node 速率 <input id="c_rate" type="number" value="30" min="1" max="500"></label>
    <label class="chk wide"><input id="c_lookup" type="checkbox" checked> 主动查询（覆盖率高一个数量级）</label>
  </div>
  <div class="ctl actions">
    <button class="go" data-start="crawler" type="button">启动</button>
    <button class="danger ghost" data-stop="crawler" type="button">停止</button>
  </div>
  <pre class="log" id="log_crawler">未运行</pre>
</div>

<div class="panel">
  <h2>导入本地种子文件</h2>
  <p class="note">扫描你电脑上已有的 <code>.torrent</code>，提取名字、体积和文件列表写进索引。
     <strong>只读取，不会改动、移动或删除任何文件</strong>，也完全不需要联网。</p>
  <div class="ctl">
    <label class="wide">要扫描的文件夹（会递归扫子目录）
      <span class="pathrow">
        <input id="l_path" type="text" placeholder="D:\\torrents">
        <button class="go ghost2" id="l_browse" type="button">浏览…</button>
      </span></label>
  </div>
  <div class="picker" id="l_picker" hidden>
    <div class="pickhead">
      <button class="up" id="pk_up" type="button">↑ 上一级</button>
      <span class="pkpath" id="pk_path">—</span>
    </div>
    <ul class="pklist" id="pk_list"></ul>
    <div class="pickfoot">
      <span class="pkcount" id="pk_count"></span>
      <button class="go" id="pk_pick" type="button">用这个文件夹</button>
      <button class="danger ghost" id="pk_close" type="button">取消</button>
    </div>
  </div>
  <div class="ctl actions">
    <button class="go" data-start="import" data-src="folder" type="button">开始扫描</button>
    <button class="danger ghost" data-stop="import" type="button">停止</button>
  </div>
  <pre class="log" id="log_local">未运行</pre>
</div>

<div class="panel">
  <h2>导入网络资源库</h2>
  <p class="note">从别人已经建好的索引站批量拉，比自己爬快得多。结果同样写进 <code>{{DB}}</code>。</p>
  <div class="ctl">
    <label class="half">来源
      <select id="i_source">
        <option value="torznab">Jackett / Prowlarr（接入面最广）</option>
        <option value="ia">互联网档案馆</option>
        <option value="academic">Academic Torrents</option>
      </select></label>
    <label>最多导入 <input id="i_limit" type="number" value="5000" min="1" max="500000"></label>
    <label>超时秒数 <input id="i_timeout" type="number" value="60" min="5" max="600"></label>
  </div>

  <div id="i_torznab">
    <div class="ctl">
      <label>Jackett 地址 <input id="i_host" type="text" value="http://127.0.0.1:9117"></label>
      <label>索引站 id（小写） <input id="i_indexer" type="text" value="all"></label>
      <label class="half">API Key（Jackett 页面顶部，点一下就能复制）
        <input id="i_apikey" type="text" placeholder="一长串十六进制"></label>
    </div>
    <p class="note">索引站 id 填 <code>all</code> 会搜所有已配置的站，但要等最慢的那个返回。
       改成单个站名（比如 <code>52bt</code>）快得多也稳得多。
       不知道哪些站能用，先在命令行测一遍：</p>
    <code class="block">btimport.py probe torznab "地址|APIKEY" --each</code>
    <div class="ctl">
      <label>翻页数 <input id="i_pages" type="number" value="5" min="1" max="200"></label>
      <label>每轮间隔秒 <input id="i_delay" type="number" value="2" min="0" max="60"></label>
      <label class="chk half"><input id="i_fetch" type="checkbox"> 下载种子算 infohash（站点只给 .torrent 时需要）</label>
    </div>
  </div>

  <div class="ctl">
    <label class="half">关键词（留空按内置词表扫。第一次用建议先填一个词试通）
      <input id="i_query" type="text"></label>
    <label class="half">代理（可留空）
      <input id="i_proxy" type="text" placeholder="http://127.0.0.1:7890"></label>
  </div>
  <div class="ctl actions">
    <button class="go" data-start="import" type="button">开始导入</button>
    <button class="danger ghost" data-stop="import" type="button">停止</button>
  </div>
  <pre class="log" id="log_import">未运行</pre>
</div>

<div class="panel">
  <h2>维护</h2>
  <p class="note">整理磁盘需要独占数据库，跑之前先停掉爬虫。
     「补全文件列表」是给 Jackett 导入的条目用的 —— Torznab 协议不提供文件列表，
     这一步用 infohash 去 DHT 把真实元数据取回来补上。</p>
  <div class="ctl actions">
    <button class="go ghost2" data-maint="analyze" type="button">体检</button>
    <button class="go ghost2" data-maint="verify" type="button">修复索引一致性</button>
    <button class="go ghost2" data-maint="vacuum" type="button">整理磁盘空间</button>
    <button class="go ghost2" data-maint="peers" type="button">实测做种情况</button>
    <button class="go ghost2" data-maint="enrich" type="button">补全文件列表</button>
    <button class="danger ghost" data-stop="maint" type="button">停止</button>
  </div>
  <pre class="log" id="log_maint">未运行</pre>
</div>
"""


def render_detail(r):
    files = [f for f in (r["filelist"] or "").split("\n") if f]
    link = magnet(r["infohash"], r["name"])
    rows = "".join('<tr><td>%s</td><td class="sz"></td></tr>' % esc(f) for f in files)
    cover = ""
    try:
        url = (r["cover"] or "").strip()
    except (IndexError, KeyError):
        url = ""            # 老库还没有 cover 列
    if url.startswith(("http://", "https://")):
        cover = ('<div class="cover"><button class="go ghost2" id="showcover" '
                 'type="button" data-src="%s">显示预览图</button>'
                 '<p class="covernote">图片来自索引站，点开才会去取 —— '
                 '这会让对方知道你的 IP。不点就不会有任何对外请求。</p>'
                 '<div id="coverbox"></div></div>' % esc(url))

    table = ("<table>%s</table>" % rows) if files else (
        '<p class="nofiles">这条没有文件列表。Torznab 协议只给标题、体积和磁力链，'
        '不提供文件清单，所以从 Jackett 导入的条目默认没有。'
        '到<a href="/tasks">任务面板</a>点「补全文件列表」，'
        '可以用 infohash 去 DHT 把真实元数据取回来。</p>')
    note = ""
    if files and len(files) < r["nfiles"]:
        note = ('<p style="color:var(--dim);font-size:12px;margin:10px 0 0">'
                '共 %d 个文件，索引只保留了前 %d 个。</p>' % (r["nfiles"], len(files)))
    return """<div class="detail">
<h2>%s</h2>
%s
<div class="mag"><input type="text" value="%s" readonly aria-label="磁力链">
<button class="copy" type="button">复制</button>
<a href="%s">打开</a></div>
<dl>
<dt>体积</dt><dd>%s</dd>
<dt>文件数</dt><dd>%d</dd>
<dt>被 announce</dt><dd>%d 次</dd>
<dt>首次见到</dt><dd>%s</dd>
<dt>最近见到</dt><dd>%s</dd>
<dt>来源</dt><dd>%s</dd>
<dt>infohash</dt><dd class="hash">%s</dd>
</dl>
%s%s</div>""" % (esc(r["name"]), cover, esc(link), esc(link), esc(human(r["size"])),
                 r["nfiles"], r["hits"], esc(ago(r["first_seen"])),
                 esc(ago(r["last_seen"])), esc(r["source"] or "未知"),
                 esc(r["infohash"]), table, note)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "btweb"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    db_path = DB_DEFAULT
    quiet = False

    # 页面里没有任何内联脚本和样式，所以能开这么严的策略。
    # 万一哪处转义漏了，注入进来的脚本也执行不了。
    # img-src 放开到 https:，是为了详情页那个「显示预览图」。
    # 默认不加载任何外部图片，只有用户点了按钮才会插入 <img>，
    # 所以不点就不会有对外请求；referrer 已经全局设成 no-referrer。
    CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
           "connect-src 'self'; img-src 'self' data: https:; form-action 'self'; "
           "base-uri 'none'; frame-ancestors 'none'")

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def reply(self, body, ctype="text/html; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", self.CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def redirect(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _same_origin(self):
        """
        挡跨站请求。浏览器发 fetch POST 时一定带 Origin，
        别的网页想偷偷调本机这个接口，Origin 就对不上。
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True                    # curl 之类没有 Origin，放行
        host = self.headers.get("Host") or ""
        return origin.split("//")[-1] == host

    def do_POST(self):
        if self.path.startswith("/api/task/"):
            return self.task_post()
        if self.path != "/api/delete":
            return self.reply_json({"error": "未知接口"}, 404)
        if not ALLOW_DELETE[0]:
            return self.reply_json({"error": "删除功能已关闭（启动时加了 --no-delete）"}, 403)
        if not self._same_origin():
            return self.reply_json({"error": "跨站请求被拒绝"}, 403)
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 4 << 20)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, OSError):
            return self.reply_json({"error": "请求体解析失败"}, 400)

        if payload.get("token") != CSRF[0]:
            return self.reply_json({"error": "令牌不对，刷新页面再试"}, 403)

        try:
            if payload.get("mode") == "filter":
                n = delete_by_filter(self.db_path, payload.get("q", "")[:MAX_QUERY],
                                     _safe_size(payload.get("min", "")),
                                     str(payload.get("src", ""))[:32])
            else:
                hashes = payload.get("hashes") or []
                if not isinstance(hashes, list) or len(hashes) > 5000:
                    return self.reply_json({"error": "选中数量不合法"}, 400)
                n = delete_by_hashes(self.db_path, hashes)
        except sqlite3.Error as e:
            return self.reply_json({"error": "数据库出错：%s" % e}, 500)
        if not self.quiet:
            sys.stderr.write("删除了 %d 条\n" % n)
        out = {"deleted": n}
        if n >= 200:
            # SQLite 删除只把页标记为空闲，不会把空间还给操作系统；
            # FTS5 还会为删除本身写入新的段，文件反而可能变大。
            out["hint"] = ("记录已删干净，但 SQLite 不会自动缩小文件。"
                           "想把空间还给磁盘，关掉本页后跑：%s btprune.py vacuum"
                           % py_cmd())
        return self.reply_json(out)

    def task_post(self):
        """启动/停止后台任务。和删除接口一样要过令牌和同源检查。"""
        if not TASKS[0]:
            return self.reply_json({"error": "任务面板已关闭（--no-tasks）"}, 403)
        if not self._same_origin():
            return self.reply_json({"error": "跨站请求被拒绝"}, 403)
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, OSError):
            return self.reply_json({"error": "请求体解析失败"}, 400)
        if body.get("token") != CSRF[0]:
            return self.reply_json({"error": "令牌不对，刷新页面再试"}, 403)

        kind = body.get("kind")
        if kind not in ("crawler", "import", "maint"):
            return self.reply_json({"error": "不认识的任务种类"}, 400)
        try:
            if self.path.endswith("/stop"):
                return self.reply_json({"stopped": TASKS[0].stop(kind)})
            task = TASKS[0].start(kind, body.get("params") or {})
            if not self.quiet:
                sys.stderr.write("启动任务：%s\n" % " ".join(task.argv[1:]))
            return self.reply_json({"started": True, "desc": task.desc})
        except (ValueError, RuntimeError) as e:
            return self.reply_json({"error": str(e)}, 400)
        except OSError as e:
            return self.reply_json({"error": "启动失败：%s" % e}, 500)

    def reply_json(self, obj, code=200):
        self.reply(json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8", code)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        args = urllib.parse.parse_qs(parsed.query)
        one = lambda k, d="": (args.get(k) or [d])[0]

        try:
            if route == "/style.css":
                return self.reply(STYLE, "text/css; charset=utf-8")
            if route == "/app.js":
                return self.reply(SCRIPT, "application/javascript; charset=utf-8")
            if route == "/favicon.svg":
                return self.reply(FAVICON, "image/svg+xml; charset=utf-8")
            if route == "/favicon.ico":
                # 页头已经用 <link rel="icon"> 指到 SVG 了，正常不会有人来要 .ico。
                # 但老浏览器和某些扩展会盲猜这个路径，返 204 比让它 404 干净——
                # 图标本来就是可有可无的东西，不值得在日志里留一行红字
                return self.reply(b"", "image/x-icon", code=204)
            if route == "/":
                # 判据是原始查询串在不在，不是解析结果。parse_qs 默认丢掉空值，
                # 「?q=」解析完是空字典，但它确实是一次提交，不能当成空手进来
                return self.page_search(one, bool(parsed.query))
            if route == "/api/search":
                return self.api_search(one)
            if route == "/tasks":
                if not TASKS[0]:
                    return self.reply(page("任务", '<div class="empty"><p>'
                        '任务面板已关闭（启动时加了 --no-tasks）。</p></div>'), code=403)
                try:
                    st = get_stats(conn_for(self.db_path), self.db_path)
                except sqlite3.Error:
                    st = None          # 库还没建好也不该挡住任务面板
                body = TASK_PAGE.replace("{{DB}}", esc(os.path.abspath(self.db_path)))
                return self.reply(page("任务 — 本地搜索", body, stats=st))
            if route == "/api/task/status":
                if not TASKS[0]:
                    return self.reply_json({"tasks": {}})
                return self.reply_json({"tasks": TASKS[0].status()})
            if route == "/api/browse":
                # 跟着任务面板一起开关。关了任务面板的人不想让这个页面碰机器，
                # 列目录也一样不该给
                if not TASKS[0]:
                    return self.reply_json({"error": "任务面板已关闭（--no-tasks）"}, 403)
                try:
                    return self.reply_json(browse_dir(one("path")))
                except ValueError as e:
                    return self.reply_json({"error": str(e)}, 400)
            if route.startswith("/t/"):
                return self.page_detail(route[3:])
            self.reply(page("找不到", '<div class="empty"><p>没有这个页面。'
                            '<a href="/">回到搜索</a></p></div>'), code=404)
        except sqlite3.OperationalError as e:
            # 最常见的一种：--db 指到了不存在的文件
            self.reply(page("打不开数据库",
                            '<div class="empty"><p>打不开数据库 %s。</p>'
                            '<p>确认路径对不对，或者先跑爬虫把它建出来。</p></div>'
                            % esc(self.db_path)), code=500)
            if not self.quiet:
                sys.stderr.write("数据库错误: %s\n" % e)
        except ValueError as e:
            self.reply(page("查询有问题", '<div class="empty"><p>%s</p></div>' % esc(e)),
                       code=400)

    # ---------- 具体页面 ----------

    def _params(self, one):
        q = one("q").strip()[:MAX_QUERY]
        sort = one("sort", "relevance")
        if sort not in ORDER_SQL:
            sort = "relevance"
        minsz = one("min")
        try:
            min_bytes = parse_size(minsz) if minsz else 0
        except ValueError:
            minsz, min_bytes = "", 0
        try:
            page_no = max(1, min(int(one("page", "1")), MAX_PAGE))
        except ValueError:
            page_no = 1
        return q, sort, minsz, min_bytes, page_no, one("src")[:32]

    def page_search(self, one, has_query=True):
        q, sort, minsz, min_bytes, page_no, src = self._params(one)
        conn = conn_for(self.db_path)
        st = get_stats(conn, self.db_path)

        editable = False
        # 判空不走缓存里的 count。刚导完数据那会儿缓存还是旧的，
        # 拿它判空会把人送回「索引还是空的」那张引导页
        if not st["count"] and not has_any_rows(conn):
            # 空库仍然走老逻辑：不管之前看过什么，没东西可看就是没东西可看
            body = EMPTY_DB % (esc(py_cmd()), esc(self.db_path),
                               esc(py_cmd()), esc(self.db_path))
        elif not has_query:
            # 守一道：qs() 在参数全为默认时会返回 "/"，真跳过去就是自己转自己。
            # 眼下 sort 恒有值走不到这儿，但别把这个前提交给以后的自己
            if LAST_VIEW[0].startswith("?"):
                return self.redirect(LAST_VIEW[0])
            body = LANDING
        else:
            # 记下这一眼看的是什么，下次空手回到 / 就送回这里。
            # 搜索和浏览一视同仁，都是「我当时在看的东西」
            LAST_VIEW[0] = qs(q=q, sort=sort, min=minsz, src=src, page=page_no)
            rows, has_next = do_search(conn, q, page_no, sort, min_bytes,
                                       source=src)
            if rows:
                head = ""
                if not q:
                    head = ('<p class="browsehead">浏览全部 %s 条%s，按%s排序</p>'
                            % (format(st["count"], ","),
                               ("（来源 %s）" % esc(src)) if src else "",
                               esc(dict(ORDER_LABEL).get(
                                   sort if sort != "relevance" else "date", ""))))
                elif Index.needs_like(q) and st["count"] > LIKE_WINDOW:
                    # 单字搜索走的是没有索引的逐行比对，只能在最近入库的一段里找。
                    # 不说这句话，用户会以为这就是全库的结果
                    head = ('<p class="browsehead">单字搜索没有索引可用，'
                            '只在最近入库的 %s 条里找。多打一个字就能搜全库。</p>'
                            % format(LIKE_WINDOW, ","))
                editable = ALLOW_DELETE[0]
                n_match, capped = (count_by_filter(conn, q, min_bytes, src)
                                   if ALLOW_DELETE[0] else (0, False))
                body = head + render_results(
                    rows, q, page_no, has_next, sort, minsz, src,
                    total_matching=n_match, count_capped=capped,
                    can_delete=ALLOW_DELETE[0])
            elif q:
                body = NO_MATCH % (esc(q), format(st["count"], ","))
            else:
                body = ('<div class="empty"><p>这个筛选条件下没有条目。'
                        '<a href="/?q=">看全部</a></p></div>')

        title = ("%s — 本地搜索" % q) if q else "本地搜索"
        self.reply(page(title, body, q, sort, minsz, st,
                        sources=list_sources(conn), src=src, editable=editable))

    def page_detail(self, infohash):
        conn = conn_for(self.db_path)
        r = get_one(conn, infohash)
        if not r:
            return self.reply(page("找不到", '<div class="empty"><p>库里没有这个种子。'
                                   '<a href="/">回到搜索</a></p></div>'), code=404)
        self.reply(page(r["name"], render_detail(r), stats=get_stats(conn, self.db_path)))

    def api_search(self, one):
        q, sort, minsz, min_bytes, page_no, src = self._params(one)
        try:
            limit = max(1, min(int(one("limit", str(PER_PAGE))), MAX_LIMIT))
        except ValueError:
            limit = PER_PAGE
        conn = conn_for(self.db_path)
        rows, has_next = do_search(conn, q, page_no, sort, min_bytes,
                                   per_page=limit, source=src)
        out = [{"infohash": r["infohash"], "name": r["name"], "size": r["size"],
                "nfiles": r["nfiles"], "hits": r["hits"], "last_seen": r["last_seen"],
                "magnet": magnet(r["infohash"], r["name"])} for r in rows]
        self.reply(json.dumps({"query": q, "page": page_no, "has_next": has_next,
                               "results": out}, ensure_ascii=False, indent=2),
                   "application/json; charset=utf-8")


def main():
    setup_console()
    ap = argparse.ArgumentParser(description="本地种子索引的网页界面")
    ap.add_argument("--db", default=DB_DEFAULT, help="索引路径（默认 %s）" % DB_DEFAULT)
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址，默认只监听本机")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-tasks", action="store_true",
                    help="关掉任务面板，网页就不能启动爬虫和导入了")
    ap.add_argument("--no-delete", action="store_true",
                    help="关掉网页上的删除功能，数据库只读打开")
    ap.add_argument("-q", "--quiet", action="store_true", help="不打访问日志")
    if ap.epilog:
        ap.epilog = ap.epilog.replace("python3 ", py_cmd() + " ")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        # 空库先建出来。否则任务面板要读统计却读不到——
        # 而用户恰恰是想用任务面板去启动爬虫、把这个库填起来。
        try:
            from btindex import Index
            Index(args.db).close()
            print("新建了空索引 %s" % args.db)
        except Exception as e:
            print("建不了 %s：%s" % (args.db, e), file=sys.stderr)

    Handler.db_path = args.db
    Handler.quiet = args.quiet
    DB_PATH[0] = args.db
    ALLOW_DELETE[0] = not args.no_delete
    # 统计数字先在后台算起来。库大的时候这一次要扫几十秒，
    # 趁人还在切窗口的工夫算完，首页就不用等
    warm_cache()
    if not args.no_tasks:
        from bttasks import TaskManager
        TASKS[0] = TaskManager(os.path.abspath(args.db))
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True

    print("在 http://%s:%d 上开着（%s：%s）"
          % (args.host if args.host != "0.0.0.0" else "127.0.0.1", args.port,
             "可删除" if ALLOW_DELETE[0] else "只读", args.db))
    if TASKS[0]:
        print("任务面板：http://%s:%d/tasks 可以直接启动爬虫和导入"
              % (args.host if args.host != "0.0.0.0" else "127.0.0.1", args.port))
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("注意：监听在 %s，局域网或公网都能访问到。"
              "这个页面没有任何登录控制，放到公网前请自己加一层认证或反向代理。"
              % args.host, file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if TASKS[0]:
            TASKS[0].stop_all()      # 网页一关，它起的后台任务也跟着收掉
        srv.server_close()


if __name__ == "__main__":
    main()
