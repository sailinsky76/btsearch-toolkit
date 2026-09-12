#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btindex —— 本地种子索引：存下来、搜得到、给出磁力链

先把一件最容易想岔的事说明白：

    DHT 网络本身不支持按关键字搜索。

它只回答一种问题：「谁有 infohash 为 XXX 的种子？」你没法问它「哪些种子名字里带
『复仇者』」——协议里压根没这个操作。所以关键字搜索只有两条路：
    (1) 查别人已经建好的索引   —— btsearch.py 干的事，胜在广度；
    (2) 自己把索引建起来再搜   —— 就是本文件，胜在日积月累。

中文检索是这里最费劲的地方，值得说清楚：
  FTS5 默认的 unicode61 分词器会把一整串汉字当成一个词，
  「复仇者联盟」进去就是一个 token，搜「联盟」永远落空。
  trigram 分词器好一些，但它按三字符滑窗切，搜「终局」这种两字词照样为零——
  而两字词恰恰是中文里最常见的搜法。（这两点我都实测过。）

  所以这里用二元组展开：入库时把「复仇者联盟」拆成「复仇 仇者 者联 联盟」
  一起塞进索引，查询时对关键词做同样的拆分再 AND 起来。
  代价是索引胖两三倍，换来中文任意子串都能搜到。
  另外中文和数字粘连时（「复仇者联盟4」）unicode61 会当成一个整词，
  所以字母数字必须单独再喂一遍，见 expand_text 里那行补丁。

用法：
    python3 btindex.py search "复仇者 2160p" --magnet
    python3 btindex.py import names.tsv
    python3 btindex.py stats

依赖：无，标准库的 sqlite3 就够（需要编译时带 FTS5，主流发行版都带）。
"""

import argparse
import os
import re
import sqlite3
import sys
import time
import urllib.parse

DB_DEFAULT = "bt.db"
MAX_FILELIST = 40                  # 每个种子最多索引多少个文件名，防止巨型种子撑爆索引
LIKE_WINDOW = 500000               # 单字查询退回 LIKE 时默认只扫最近这么多条
PREFIX_MIN = 3                     # 拉丁词至少这么长，才按前缀匹配（见 build_match）
MIN_SQLITE = (3, 43)               # 无正文 FTS 是这一版引进的（见 require_sqlite）

# 中日韩统一表意文字 + 假名 + 谚文
CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+")
LATIN = re.compile(r"[0-9A-Za-z]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS torrents (
    infohash   TEXT PRIMARY KEY,
    name       TEXT    NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    nfiles     INTEGER NOT NULL DEFAULT 0,
    filelist   TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_last_seen ON torrents(last_seen);
CREATE INDEX IF NOT EXISTS idx_size      ON torrents(size);
CREATE INDEX IF NOT EXISTS idx_hits      ON torrents(hits);
-- 来源下拉每次都要 GROUP BY source。没这个索引就得扫主表再起两棵临时 B 树，
-- 600 万条上 2.7 秒；有了它走覆盖索引，同样的查询 0.26 秒。
-- source 只有几种取值，索引本身很小，白捡的十倍。
CREATE INDEX IF NOT EXISTS idx_source    ON torrents(source);

CREATE VIRTUAL TABLE IF NOT EXISTS torrents_fts USING fts5(
    body,
    tokenize='unicode61',
    -- content='' 是无正文模式：只建倒排索引，不再把 body 原样存一份。
    -- 原因很直接——那份副本从来没人读。全文里所有 FTS 访问不是 MATCH、
    -- rowid 就是 bm25()，要显示的字段一律从主表 torrents 取，
    -- 而 body 本身（原名 + 二元组 + 文件名 + 文件名二元组）又完全可以
    -- 从 name 和 filelist 重新算出来，存着纯属占地方。
    -- 实测 600 万条的库：torrents_fts_content 一张表 2.30 GB，
    -- 占整个库 4.86 GB 的 47%，而真正的倒排索引只有 0.51 GB。
    --
    -- contentless_delete=1 要 SQLite 3.43 以上（3.45.1 实测可用）。
    -- 没有它，无正文表不能 DELETE，只能反过来把原文再喂一遍去抵消，
    -- 那样 upsert 和删除逻辑都得推倒重写。有了它，
    -- DELETE FROM torrents_fts WHERE rowid=? 照常能用，
    -- bm25() 也照常——docsize 还是存着的，只是正文不存了。
    --
    -- 代价：删除会留下墓碑，久了索引会虚胖。btprune 的 vacuum 里
    -- 已经带了 'optimize'，每周维护跑到就顺手合并了，不用额外处理。
    content='',
    contentless_delete=1
);
"""


# --------------------------------------------------------------------------
# 中文分词：入库展开与查询展开必须用同一套规则，否则怎么都对不上
# --------------------------------------------------------------------------

def _bigrams(run: str):
    """复仇者联盟 -> ['复仇','仇者','者联','联盟']；单字原样返回。"""
    if len(run) < 2:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def expand_text(text: str) -> str:
    """把一段文字展开成 unicode61 能索引的词串。"""
    out = []
    for run in CJK_RUN.findall(text):
        out.extend(_bigrams(run))
    # 这行是关键补丁：「联盟4」这种中文数字粘连会被当成一个整词，
    # 把所有字母数字串单独再抽一遍，"4" 才能被独立命中。
    out.extend(LATIN.findall(text))
    return " ".join(out)


def build_match(query: str, prefix: bool = True) -> str:
    """
    把用户输入变成 FTS5 的 MATCH 表达式，词与词之间是 AND。

    拉丁词默认按前缀匹配（`"ubun"*` 能命中 ubuntu）。这不只是为了「打了半个
    单词也能搜到」——更要紧的是 LATIN 把字母数字连在一起当一个词，`1080p`
    在索引里是一个整词，不加前缀的话搜 `1080` 一条都出不来，而库里明明有几十条。
    同类的还有 x264、S01E05、2160p、amd64。

    为什么是「所有拉丁词都加」而不是「只加最后一个」：只加最后一个的话，
    `matrix 1080` 搜得到、`1080 matrix` 搜不到——同样两个词换个顺序结果就变，
    在搜索框里这种不一致比多一点噪音难受得多。词与词是 AND，多打一个词
    只会让结果更窄，所以「全加」在结果数上是安全的。

    PREFIX_MIN 这道门槛是必须的：一两个字母的前缀能匹配上成千上万个不同的词，
    把它们的倒排表全读出来再求并，那就不是搜索是扫库了。短词老老实实精确匹配。

    中文这边不加星。索引里的中文 token 全是两个字的二元组，`"复仇"*` 能匹配到的
    两字词就只有「复仇」自己，加了等于没加，白白让查询计划多一层前缀扫描。

    代价要说清楚：前缀会把命中量放大，而 bm25 必须给每一条命中算分，没法提前
    终止。也就是说高频词那堵墙（`1080p` 在 600 万条里命中 45 万条）会来得更早。
    网页那边有 QUERY_DEADLINE 兜底，命令行嫌吵或嫌慢就用 --exact 关掉。
    """
    terms = []                     # [(词, 要不要加星)]
    for run in CJK_RUN.findall(query):
        terms.extend((b, False) for b in _bigrams(run))
    terms.extend((t, prefix and len(t) >= PREFIX_MIN) for t in LATIN.findall(query))
    if not terms:
        return ""
    # 双引号在 FTS5 里要用两个双引号转义，否则用户输入能把查询语法带跑偏。
    # 星号必须写在引号外面：`"ubun"*` 才是前缀查询，`"ubun*"` 是在找一个
    # 真的带星号的词。
    return " AND ".join('"%s"%s' % (t.replace('"', '""'), "*" if star else "")
                        for t, star in terms)


def human(n) -> str:
    n = int(n or 0)
    if n <= 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return ("%.1f %s" % (n, unit)).replace(".0 ", " ")
        n /= 1024.0
    return "-"


def parse_size(text) -> int:
    """把 '700MB' / '4G' / '1234' 变成字节数，给命令行的体积过滤用。"""
    if not text:
        return 0
    s = str(text).strip()
    if s.isdigit():
        return int(s)
    m = re.match(r"([\d.]+)\s*([KMGTP])i?B?$", s, re.I)
    if not m:
        raise ValueError("看不懂的体积写法: %s" % text)
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
    return int(float(m.group(1)) * mult[m.group(2).upper()])


def magnet(infohash: str, name: str = "") -> str:
    s = "magnet:?xt=urn:btih:" + infohash
    if name:
        s += "&dn=" + urllib.parse.quote(name)
    for tr in ("udp://tracker.opentrackr.org:1337/announce",
               "udp://open.demonii.com:1337/announce",
               "udp://tracker.torrent.eu.org:451/announce"):
        s += "&tr=" + urllib.parse.quote(tr, safe="")
    return s


# --------------------------------------------------------------------------
# 索引本体
# --------------------------------------------------------------------------

def require_sqlite():
    """
    建库之前先拦一道。SCHEMA 里的 content='' + contentless_delete=1 是
    SQLite 3.43 引进的，低于这版直接报 unrecognized option: "contentless_delete"——
    那句报错既不说要什么版本，也不说去哪儿改，对着它猜半天也猜不出是 Python
    自带的 SQLite 太老。

    判据是 SQLite 的版本，不是 Python 的：同一个 3.11，小版本不同带的
    SQLite 也不同，光看 Python 版本号推不出来。
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            "你这套 Python 自带的 SQLite 是 %s，这套索引要 %d.%d 以上"
            "（无正文 FTS 是那一版引进的，低于它一条种子也存不进去）。\n"
            "  解释器：%s\n"
            "  换 python.org 上新一点的安装包再跑（3.12 及以上肯定够），"
            "装好用 check.bat 确认一遍。"
            % (sqlite3.sqlite_version, MIN_SQLITE[0], MIN_SQLITE[1], sys.executable))


class Index:
    def __init__(self, path=DB_DEFAULT):
        require_sqlite()
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # WAL 让「一边灌数据一边搜索」不会互相锁死，流水线场景必开
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- 写 ----------

    def has(self, infohash: str) -> bool:
        """流水线用：已经在库里的就别再费劲去抓元数据了。"""
        return self.db.execute(
            "SELECT 1 FROM torrents WHERE infohash=?",
            (infohash.lower(),)).fetchone() is not None

    def bump(self, infohash: str):
        """又见到一次。announce 次数粗略等于热度，可以拿来排序。"""
        self.db.execute(
            "UPDATE torrents SET last_seen=?, hits=hits+1 WHERE infohash=?",
            (int(time.time()), infohash.lower()))

    def upsert(self, infohash, name, size=0, nfiles=0, files=(), source="dht") -> bool:
        """写入或更新一条。返回 True 表示这是新种子。"""
        infohash = infohash.lower().strip()
        if len(infohash) != 40 or not re.fullmatch(r"[0-9a-f]{40}", infohash):
            raise ValueError("infohash 必须是 40 位十六进制")
        name = (name or "").strip() or "(无名)"
        filelist = "\n".join(str(f) for f in list(files)[:MAX_FILELIST])
        now = int(time.time())

        row = self.db.execute(
            "SELECT rowid FROM torrents WHERE infohash=?", (infohash,)).fetchone()
        # 索引正文 = 原名 + 中文展开 + 文件名 + 文件名展开，四份都能搜
        body = "%s %s %s %s" % (name, expand_text(name), filelist, expand_text(filelist))

        if row:
            self.db.execute(
                "UPDATE torrents SET name=?, size=?, nfiles=?, filelist=?, "
                "source=?, last_seen=?, hits=hits+1 WHERE infohash=?",
                (name, size, nfiles, filelist, source, now, infohash))
            # FTS 表手动跟着改。所有写操作都走这个函数，所以不用触发器也能保持一致；
            # 真要多入口写库，就得换成 AFTER INSERT/UPDATE/DELETE 触发器。
            self.db.execute("DELETE FROM torrents_fts WHERE rowid=?", (row["rowid"],))
            self.db.execute("INSERT INTO torrents_fts(rowid, body) VALUES (?,?)",
                            (row["rowid"], body))
            return False

        cur = self.db.execute(
            "INSERT INTO torrents(infohash,name,size,nfiles,filelist,source,"
            "first_seen,last_seen,hits) VALUES (?,?,?,?,?,?,?,?,1)",
            (infohash, name, size, nfiles, filelist, source, now, now))
        self.db.execute("INSERT INTO torrents_fts(rowid, body) VALUES (?,?)",
                        (cur.lastrowid, body))
        return True

    def commit(self):
        self.db.commit()

    # ---------- 读 ----------

    @staticmethod
    def needs_like(query):
        """
        判断要不要退回 LIKE 扫描。

        索引正文存的是二元组（猫和/和老/老鼠）加原串，而 unicode61 把整段中文
        当成一个词。所以「猫」这种单字既配不上二元组，也配不上整串，
        FTS 会静默返回 0 条 —— 看起来像库里没有，其实是搜法够不着。
        单字查询很少，退回 LIKE 扫全表的代价可以接受，总比搜不到强。
        """
        def is_cjk(ch):
            o = ord(ch)
            return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
                    or 0xF900 <= o <= 0xFAFF or 0x3040 <= o <= 0x30FF)
        toks = [t for t in str(query or "").split() if t]
        # 只对「单个中文字」回退。单个拉丁字母交给 FTS 按词匹配更准 ——
        # 用 LIKE 的话，搜 a 会把所有含字母 a 的条目全捞出来，噪音大到没法用。
        return bool(toks) and all(len(t) == 1 and is_cjk(t) for t in toks)

    @staticmethod
    def like_clause(query):
        """返回 (SQL 片段, 参数)。按空白切词，每个词都要出现在名字或文件列表里。"""
        toks = [t for t in str(query or "").split() if t][:8]
        if not toks:
            return "", []
        parts, params = [], []
        for t in toks:
            pat = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            parts.append("(t.name LIKE ? ESCAPE '\\' OR t.filelist LIKE ? ESCAPE '\\')")
            params += [pat, pat]
        return " AND ".join(parts), params

    def _like_search(self, query, limit, min_size, max_size, sort, window=0):
        clause, params = self.like_clause(query)
        if not clause:
            return []
        where = [clause]
        if min_size:
            where.append("size >= ?"); params.append(min_size)
        if max_size:
            where.append("size <= ?"); params.append(max_size)
        order = {"size": "size DESC", "date": "last_seen DESC"}.get(sort, "hits DESC")
        # LIKE 用不上任何索引，是逐行比对，代价跟着库线性涨——600 万条的库里
        # 搜一个「猫」要 15.7 秒，命中 0 条时最惨，非扫到底不知道没有。
        # 只看最近入库的一段：rowid 自增，rowid > MAX-N 就是「最近 N 条」，
        # 顺序读，代价固定。搜不全是明摆着的代价，命令行这边用 --deep 解除。
        if window:
            where.append("t.rowid > (SELECT MAX(rowid) FROM torrents) - %d" % window)
            # 加号别删：没有它，ORDER BY hits DESC 会让 SQLite 走 idx_hits
            # 从头扫整个索引，上面那扇窗就白开了。一元加号让排序表达式不再
            # 对应任何索引列，查询计划才会改成按 rowid 范围扫。实测快 4 倍多
            order = "+" + order.replace(", ", ", +")
        sql = ("SELECT * FROM torrents t WHERE %s ORDER BY %s LIMIT ?"
               % (" AND ".join(where), order))
        try:
            return [dict(r) for r in self.db.execute(sql, params + [limit])]
        except sqlite3.OperationalError as e:
            raise ValueError("查询无法执行: %s" % e)

    def search(self, query, limit=25, min_size=0, max_size=0, sort="relevance",
               like_window=LIKE_WINDOW, prefix=True):
        # 单字查询直接走 LIKE。不能先试 FTS 再回退 —— FTS 偶尔会因为分词边界
        # （比如「字幕/猫.srt」里的斜杠）碰巧命中一两条，非空就不回退了，
        # 结果反而漏掉大部分真正该命中的条目。
        if self.needs_like(query):
            return self._like_search(query, limit, min_size, max_size, sort,
                                     window=like_window)

        match = build_match(query, prefix=prefix)
        if not match:
            return []

        where = ["torrents_fts MATCH ?"]
        params = [match]
        if min_size:
            where.append("t.size >= ?")
            params.append(min_size)
        if max_size:
            where.append("t.size <= ?")
            params.append(max_size)

        order = {
            "relevance": "bm25(torrents_fts), t.hits DESC",
            "hits":      "t.hits DESC, bm25(torrents_fts)",
            "size":      "t.size DESC",
            "date":      "t.last_seen DESC",
        }.get(sort, "bm25(torrents_fts), t.hits DESC")

        sql = ("SELECT t.* FROM torrents_fts JOIN torrents t ON t.rowid = torrents_fts.rowid "
               "WHERE %s ORDER BY %s LIMIT ?" % (" AND ".join(where), order))
        params.append(limit)
        try:
            rows = [dict(r) for r in self.db.execute(sql, params)]
        except sqlite3.OperationalError as e:
            raise ValueError("查询无法执行: %s" % e)
        return rows

    def stats(self) -> dict:
        # 四个聚合分开查。写成一条 SQLite 只能 SCAN 主表；拆开之后
        # COUNT 走 idx_hits、SUM 走 idx_size、两个时间走 idx_last_seen /
        # first_seen 的末端，只读索引不碰主表。600 万条上 2.5 秒变 0.9 秒，
        # 而且这个差距是跟着库线性放大的。
        n = self.db.execute("SELECT COUNT(*) FROM torrents").fetchone()[0]
        total = self.db.execute(
            "SELECT COALESCE(SUM(size),0) FROM torrents").fetchone()[0] if n else 0
        oldest = self.db.execute("SELECT MIN(first_seen) FROM torrents").fetchone()[0]
        newest = self.db.execute("SELECT MAX(last_seen) FROM torrents").fetchone()[0]
        row = {"n": n, "total": total, "oldest": oldest, "newest": newest}
        sources = self.db.execute(
            "SELECT source, COUNT(*) n FROM torrents GROUP BY source ORDER BY n DESC").fetchall()
        on_disk = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                on_disk += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return {"count": row["n"], "total_size": row["total"], "oldest": row["oldest"],
                "newest": row["newest"], "sources": [dict(s) for s in sources],
                "db_size": on_disk}

    def close(self):
        try:
            self.db.commit()
            self.db.close()
        except sqlite3.Error:
            pass


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def cmd_list(args):
    """
    列出库里的条目，不需要关键词。
    走的是主表不是 FTS——没有查询词时 FTS 用不上，直接查主表又快又直接。
    """
    idx = Index(args.db)
    try:
        where, params = [], []
        if args.source:
            where.append("source = ?")
            params.append(args.source)
        if args.min_size:
            where.append("size >= ?")
            params.append(parse_size(args.min_size))
        order = {"date": "last_seen DESC", "hits": "hits DESC, size DESC",
                 "size": "size DESC", "name": "name ASC",
                 "random": "RANDOM()"}.get(args.sort, "last_seen DESC")
        sql = ("SELECT * FROM torrents %s ORDER BY %s LIMIT ? OFFSET ?"
               % (("WHERE " + " AND ".join(where)) if where else "", order))
        rows = [dict(r) for r in idx.db.execute(sql, params + [args.limit, args.offset])]
        total = idx.stats()["count"]

        if not rows:
            print("没有条目。库里一共 %d 条。" % total)
            return
        if args.magnet:
            for r in rows:
                print(magnet(r["infohash"], r["name"]))
            return
        if args.tsv:
            for r in rows:
                print("%s\t%s\t%d\t%d\t%s" % (r["infohash"],
                      (r["name"] or "").replace("\t", " "), r["size"],
                      r["nfiles"], r["source"] or ""))
            return

        try:
            from btcompat import pad, clip
        except ImportError:                     # 拿不到就退回普通对齐，不至于跑不了
            pad = lambda t, w, right=False: ("%*s" if right else "%-*s") % (w, t)
            clip = lambda t, w: t[:w]
        print("%-4s %s %10s %6s %6s  %s"
              % ("#", pad("名称", 52), "大小", "文件", "热度", "来源"))
        print("-" * 94)
        for i, r in enumerate(rows, args.offset + 1):
            print("%-4d %s %10s %6d %6d  %s"
                  % (i, pad(clip(r["name"], 52), 52), human(r["size"]),
                     r["nfiles"], r["hits"], (r["source"] or "")[:9]))
        print("\n显示 %d 条 / 库里共 %s 条。--offset 翻页，--magnet 或 --tsv 导出。"
              % (len(rows), format(total, ",")))
    finally:
        idx.close()


def cmd_search(args):
    idx = Index(args.db)
    try:
        rows = idx.search(args.query, limit=args.limit, sort=args.sort,
                          min_size=parse_size(args.min_size),
                          max_size=parse_size(args.max_size),
                          like_window=0 if args.deep else LIKE_WINDOW,  # 0 = 不设窗口
                          prefix=not args.exact)
        if not rows:
            print("没搜到。库里一共 %d 条，可以先 stats 看看规模。" % idx.stats()["count"])
            return
        if args.magnet:
            for r in rows:
                print(magnet(r["infohash"], r["name"]))
            return
        print("%-3s %-58s %10s %6s %6s" % ("#", "名称", "大小", "文件", "热度"))
        print("-" * 88)
        for i, r in enumerate(rows, 1):
            name = r["name"] if len(r["name"]) <= 58 else r["name"][:57] + "…"
            print("%-3d %-58s %10s %6d %6d"
                  % (i, name, human(r["size"]), r["nfiles"], r["hits"]))
        print("\n共 %d 条。加 --magnet 直接输出磁力链。" % len(rows))
    finally:
        idx.close()


def cmd_import(args):
    """吃 dhtmeta --sniff 产出的 TSV：infohash \\t 名称 \\t 大小 \\t 文件数"""
    idx = Index(args.db)
    new = bad = 0
    try:
        with open(args.file, encoding="utf-8", errors="replace") as fp:
            for lineno, line in enumerate(fp, 1):
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2:
                    bad += 1
                    continue
                try:
                    size = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                    nf = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
                    if idx.upsert(parts[0], parts[1], size, nf, source=args.source):
                        new += 1
                except (ValueError, sqlite3.Error):
                    bad += 1
                if lineno % 2000 == 0:
                    idx.commit()
                    print("  已处理 %d 行…" % lineno, file=sys.stderr)
        idx.commit()
        print("导入完成：新增 %d 条，跳过 %d 条无效行，库里现有 %d 条"
              % (new, bad, idx.stats()["count"]))
    finally:
        idx.close()


def cmd_stats(args):
    idx = Index(args.db)
    s = idx.stats()
    print("种子总数 : %d" % s["count"])
    print("内容总量 : %s" % human(s["total_size"]))
    print("数据库   : %s（%s）" % (args.db, human(s["db_size"])))
    if s["oldest"]:
        fmt = "%Y-%m-%d %H:%M"
        print("时间跨度 : %s ~ %s" % (time.strftime(fmt, time.localtime(s["oldest"])),
                                     time.strftime(fmt, time.localtime(s["newest"]))))
    for src in s["sources"]:
        print("  来源 %-10s %d 条" % (src["source"], src["n"]))
    idx.close()


def main():
    try:
        from btcompat import py_cmd, setup_console
        setup_console()      # Windows 下重定向到文件时必须，否则中文符号会崩
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="本地种子索引：入库、检索、输出磁力链")
    ap.add_argument("--db", default=DB_DEFAULT, help="数据库路径（默认 bt.db）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="按关键词检索")
    p.add_argument("query", help="关键词，中英文都行，多个词是 AND 关系")
    p.add_argument("-n", "--limit", type=int, default=25)
    p.add_argument("--sort", choices=["relevance", "hits", "size", "date"],
                   default="relevance")
    p.add_argument("--min-size", default="", help="最小体积，如 700MB")
    p.add_argument("--max-size", default="", help="最大体积，如 20G")
    p.add_argument("--magnet", action="store_true", help="只输出磁力链")
    p.add_argument("--deep", action="store_true",
                   help="单字搜索时扫全库而不是只扫最近 50 万条（大库上会很慢）")
    p.add_argument("--exact", action="store_true",
                   help="英文词精确匹配，不当前缀用（嫌噪音大或嫌慢时用）")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("list", help="列出库里的条目，不用关键词")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0, help="从第几条开始，用来翻页")
    p.add_argument("--source", help="只看某个来源，如 dht / torznab / ia / folder")
    p.add_argument("--min-size", default="", help="最小体积，如 1GB")
    p.add_argument("--sort", default="date",
                   choices=["date", "hits", "size", "name", "random"],
                   help="排序方式（默认按最近出现）")
    p.add_argument("--magnet", action="store_true", help="只输出磁力链")
    p.add_argument("--tsv", action="store_true",
                   help="输出 TSV：infohash / 名称 / 大小 / 文件数 / 来源")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("import", help="导入 dhtmeta 产出的 TSV")
    p.add_argument("file")
    p.add_argument("--source", default="dht")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("stats", help="看看库里有多少东西")
    p.set_defaults(func=cmd_stats)

    if ap.epilog:
        ap.epilog = ap.epilog.replace("python3 ", py_cmd() + " ")
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
