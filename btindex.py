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

# 名字解析。导入方向只有这一个：btindex -> btparse。
# btparse 不许反过来导入 btindex，否则就是循环导入——
# 它需要的补列函数因此只能自己带一份，见那边 ensure_columns 的说明。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btparse import PARSE_VERSION, ensure_parse_columns, fields as parse_fields

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
    hits       INTEGER NOT NULL DEFAULT 1,
    -- 实测的做种/下载人数，btpeers 写回。-1 表示从来没测过，
    -- 0 表示测过但当时一个人都没有（死种）。两者含义完全不同，
    -- 不能合并成 0——「没人要」和「还不知道」在搜索结果里得分开显示。
    peers      INTEGER NOT NULL DEFAULT -1,
    checked_at INTEGER NOT NULL DEFAULT 0,
    -- 从名字里解析出来的分类和清晰度，规则在 btparse.py。
    -- 空串表示认不出来，不表示没解析过——那个看 parsed：
    -- 它存的是解析这条时用的规则版本号，0 是从来没解析过。
    kind       TEXT    NOT NULL DEFAULT '',
    res        TEXT    NOT NULL DEFAULT '',
    parsed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_last_seen ON torrents(last_seen);
CREATE INDEX IF NOT EXISTS idx_size      ON torrents(size);
CREATE INDEX IF NOT EXISTS idx_hits      ON torrents(hits);
-- 来源下拉每次都要 GROUP BY source。没这个索引就得扫主表再起两棵临时 B 树，
-- 600 万条上 2.7 秒；有了它走覆盖索引，同样的查询 0.26 秒。
-- source 只有几种取值，索引本身很小，白捡的十倍。
CREATE INDEX IF NOT EXISTS idx_source    ON torrents(source);
-- peers / checked_at / kind / res / parsed 的索引都不在这儿，在 ensure_columns 里建。
-- 因为 SCHEMA 跑在补列之前：老库那会儿还没有这两列，
-- 在这里写 CREATE INDEX ON torrents(peers) 会直接 no such column，
-- 把每一次打开库都炸掉——而且只炸老库，新库一点事没有，最难发现的那种。

CREATE VIRTUAL TABLE IF NOT EXISTS torrents_fts USING fts5(
    -- 两列而不是一列：名字（含中文二元组）和文件列表（含二元组）分开存。
    -- 分开是为了能给它们不同的权重——见下面 BM25_ORDER 的说明。
    -- 老库是单列的 body，照样能用，写入时会认出来；迁过来靠 btmigrate.py。
    name,
    files,
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


# 一个查询词的形状：可选的前导减号，然后要么是一段带引号的短语，要么是一串非空白。
# 减号只认**词首**这一个位置。写成 `(-?)(\S+)` 而不是到处扫减号，是因为
# WEB-DL、S01-S10、ImageNet-ILSVRC2012 这些名字里的连字符是内容不是操作符，
# 拿它们当排除词会把一大批正常查询搞坏。
_QUERY_TOKEN = re.compile(r'(-?)"([^"]*)"|(-?)(\S+)')


def split_query(query: str):
    """
    把查询串切成 [(要排除吗, 是短语吗, 文本), ...]。

    引号没闭合、只打了一个减号这类残缺输入不报错，退化成普通词处理——
    搜索框里的输入天然是半成品，用户打到一半的状态不该弹错误。
    """
    out = []
    for m in _QUERY_TOKEN.finditer(query or ""):
        if m.group(2) is not None:
            out.append((bool(m.group(1)), True, m.group(2)))
        else:
            out.append((bool(m.group(3)), False, m.group(4)))
    return out


def name_matches(query: str, name: str, prefix: bool = True) -> bool:
    """
    只看名字这一列，够不够满足查询里的正向词。

    用来在结果行上区分「名字里就有这个词」和「只是文件列表里提了一嘴」。
    搜索两列都搜（见 fts_write），所以名字里没有关键词的条目照样会出现在结果里，
    而看页面的人只能看到名字，于是第一反应是「这条为什么在这」——那个答案
    要点开详情页翻文件列表才知道。有了这个判断就能在行上直接标出来。

    **规则必须跟着 build_match 走。** 两边错开的后果是标反：明明名字里有这个词，
    行上却写着「文件名匹配」。所以这里逐条对应 _term_expr 的做法：
    中文按二元组逐个查（不是把整段当子串——FTS 要的是每个二元组都在，
    「复仇的仇者」含有「复仇」和「仇者」两个二元组，是真能被「复仇者」命中的），
    拉丁按词查、够长的按前缀，短语按整段相邻查。

    排除词不参与：能出现在结果里的条目，本来就已经满足了排除条件。
    """
    hay = (name or "").lower()
    words = [w.lower() for w in LATIN.findall(hay)]
    for excl, phrase, text in split_query(query or ""):
        if excl:
            continue
        cjk, lat = CJK_RUN.findall(text), LATIN.findall(text)
        if not cjk and not lat:
            continue                      # 光一个减号之类，没内容
        if phrase and cjk and not lat:
            if text.lower() not in hay:    # 纯中文短语要整段相邻
                return False
            continue
        if phrase and lat and not cjk:
            if " ".join(lat).lower() not in " ".join(words):
                return False
            continue
        for run in cjk:
            for b in _bigrams(run):
                if b.lower() not in hay:
                    return False
        for t in lat:
            t = t.lower()
            if prefix and len(t) >= PREFIX_MIN:
                if not any(w.startswith(t) for w in words):
                    return False
            elif t not in words:
                return False
    return True


def build_match(query: str, prefix: bool = True) -> str:
    """
    把用户输入变成 FTS5 的 MATCH 表达式，词与词之间是 AND。

    两个操作符：

    - `-词` 排除。`matrix -reloaded`、`-枪版`。减号只在词首算数。
    - `"短语"` 按相邻匹配。`"the matrix"` 不会命中「matrix」和「the」分散在
      名字两头的条目。`-"..."` 两个可以叠。

    排除词和普通词用同一套前缀规则（`-1080` 会连 `1080p` 一起排掉）。本来想让
    排除走精确匹配、少误伤一点，但那样 `1080` 搜得到 1080p、`-1080` 却排不掉
    1080p，同一个词在加不加减号时行为不一样——这种不一致比多排掉一点更难受，
    而且排除是用户自己打出来的，不是系统替他做的决定。

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
    pos, neg = [], []
    for excl, phrase, text in split_query(query):
        e = _term_expr(text, prefix=prefix, phrase=phrase)
        if e:
            (neg if excl else pos).append(e)
    if not pos:
        # 只给了排除词（`-枪版`）时不猜「那就是全库减去它」——那是个几百万条的
        # 结果集，翻不动也没意义。当成没给查询词处理，让上层去走浏览全部。
        return ""
    expr = " AND ".join(pos)
    if neg:
        # `(A AND B) NOT (C OR D)`。括号是必须的：FTS5 里 NOT 的优先级高于 AND，
        # 不加括号的 `A AND B NOT C` 会被读成 `A AND (B NOT C)`，
        # 于是只有 B 那一列受排除、A 照样能把条目捞回来。
        # 只有一项时 _term_expr 已经保证它是原子的，不用再套一层。
        left = expr if len(pos) == 1 else "(%s)" % expr
        expr = "%s NOT (%s)" % (left, " OR ".join(neg))
    return expr


def _quote(tok: str) -> str:
    """双引号在 FTS5 里要用两个双引号转义，否则用户输入能把查询语法带跑偏。"""
    return '"%s"' % tok.replace('"', '""')


def _term_expr(text: str, prefix: bool, phrase: bool) -> str:
    """
    一个词（或一段带引号的短语）变成一个 FTS5 子表达式。认不出内容时返回空串。

    短语只在「整段都是中文」或「整段都是拉丁」时才真的按短语查，理由是索引正文的
    形状：入库时存的是「原串 + 展开串」，而展开串里所有中文二元组排在前面、
    所有拉丁词排在后面。所以纯中文短语（连续的二元组）和纯拉丁短语（原串里连续的
    词）都能对上，中英混排的那种在索引里根本不相邻，按短语查必然一条都搜不到。
    与其给个静默搜不到，不如降级成 AND——结果多一点，但不会骗人。
    """
    cjk = CJK_RUN.findall(text)
    lat = LATIN.findall(text)
    if phrase and cjk and not lat:
        toks = []
        for run in cjk:
            toks.extend(_bigrams(run))
        return _quote(" ".join(toks)) if toks else ""
    if phrase and lat and not cjk:
        # 短语不加星：`"the matrix"*` 的星只作用在最后一个词上，
        # 而用户打引号的意思是「就这几个词，一字不差」，加星是在替他放宽。
        return _quote(" ".join(lat))

    terms = []                     # [(词, 要不要加星)]
    for run in cjk:
        terms.extend((b, False) for b in _bigrams(run))
    terms.extend((t, prefix and len(t) >= PREFIX_MIN) for t in lat)
    if not terms:
        return ""
    # 星号必须写在引号外面：`"ubun"*` 才是前缀查询，`"ubun*"` 是在找一个
    # 真的带星号的词。
    parts = [_quote(t) + ("*" if star else "") for t, star in terms]
    return parts[0] if len(parts) == 1 else "(%s)" % " AND ".join(parts)


# 名字的权重是文件列表的十倍。
#
# 为什么需要这个：一列的时候，「名字里就叫这个」和「文件列表里碰巧提了一嘴」
# 在 bm25 眼里没有区别，而且长度惩罚还会帮倒忙——搜 matrix，一条
# 「Dev Tools Pack 3 / bin/matrix.dll」比「The Matrix 1999 1080p BluRay x264」
# 整体更短，于是前者排在前面。实测过：一列时前十名全是文件列表命中的，
# 拆成两列加权之后前十名全是名字命中的。
#
# 为什么是 10：实测权重到 2 就已经翻过来了，而 2 到 30 之间前十名的构成完全一样，
# 所以这个数不敏感，取个稳妥的整数。也试过会不会把「合集」类资源埋掉——
# 不会，它们本来就因为文件列表长被长度惩罚压在最后。
#
# 这个表达式对**单列的老库也安全**：多给的权重会被忽略，剩下那个权重
# 对所有行是同一个倍数，排序不变。实测确认过前十名和 bm25(torrents_fts) 一致。
# 所以读这一侧不用分两套代码，只有写入要认表结构。
NAME_WEIGHT = 10.0
FILES_WEIGHT = 1.0
BM25 = "bm25(torrents_fts, %s, %s)" % (NAME_WEIGHT, FILES_WEIGHT)


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------
# 入库展开（expand_text）和查询展开（build_match）必须是同一套规则，
# 两边错开一点就是「库里明明有、怎么都搜不出来」，而且不报任何错。
# btparse 那边靠 CASES 守着规则，这边一直没有对应的东西——加前缀、拆两列、
# 现在又加排除词和短语，每一次都是手工验过就算数。这些断言就是把那些手工验证钉住。
#
# 断言写的是**生成的表达式原文**，不是搜索结果。这样不用建库、毫秒级跑完，
# 而且改动一眼能看出影响了哪条。表达式变了不一定是错，但一定得有人过目。
QUERY_CASES = [
    # 基本形：拉丁词加前缀星，短词不加（PREFIX_MIN）
    ("ubuntu server",        '"ubuntu"* AND "server"*'),
    ("ub",                   '"ub"'),
    ("1080",                 '"1080"*'),          # 加了星才搜得到 1080p
    # 中文走二元组，不加星
    ("复仇者联盟",            '("复仇" AND "仇者" AND "者联" AND "联盟")'),
    # 排除词
    ("matrix -reloaded",     '"matrix"* NOT ("reloaded"*)'),
    ("1080p -cam -hdts",     '"1080p"* NOT ("cam"* OR "hdts"*)'),
    ("复仇者 -国语",          '("复仇" AND "仇者") NOT ("国语")'),
    # 短语：拉丁按原串相邻，中文按二元组相邻
    ('"the matrix"',         '"the matrix"'),
    ('"复仇者联盟"',          '"复仇 仇者 者联 联盟"'),
    ('matrix "web dl" -x265', '("matrix"* AND "web dl") NOT ("x265"*)'),
    # 中英混排的短语没法按相邻查（索引里它们不相邻），降级成 AND 而不是搜不到
    ('"the matrix 复仇"',     '("复仇" AND "the"* AND "matrix"*)'),
    # 只有排除词、空串、光秃秃一个减号：都当没给查询词
    ("-枪版",                 ""),
    ("-",                    ""),
    ("",                     ""),
    # 注入：用户打的引号和 OR 必须变成普通词，不能变成查询语法
    ('a"b OR c',             '("a" AND "b") AND "OR" AND "c"'),
]

# name_matches 的规则要跟 build_match 咬住，错开就会在结果行上标反
NAME_CASES = [
    ("matrix",        "The.Matrix.1999.1080p.BluRay", True),
    ("matrix",        "Dev Tools Pack 0",             False),  # 只能是文件列表命中的
    ("matr",          "The.Matrix.1999",              True),   # 够长，按前缀
    ("ma",            "The.Matrix.1999",              False),  # 短于 PREFIX_MIN，要精确
    ("黑客帝国",       "黑客帝国.The.Matrix.1999",       True),
    ("黑客帝国",       "The.Matrix.1999",              False),
    # 二元组是逐个查的，不是把整段当子串：FTS 真会这么命中，这里得跟着
    ("复仇者",         "复仇的仇者",                    True),
    ("复仇者",         "复仇联盟",                      False),
    ('"the matrix"',  "The.Matrix.1999",              True),   # 短语要相邻
    ('"the matrix"',  "Matrix The Movie",             False),
    ('"复仇者联盟"',    "复仇者联盟4",                   True),
    ('"复仇者联盟"',    "复仇者与联盟",                   False),
    ("matrix -reloaded", "The.Matrix.1999",           True),   # 排除词不参与判断
    ("matrix 2003",   "The.Matrix.1999",              False),  # 多个词要全中
    ("",              "任何名字",                      True),   # 没给词就无所谓命中位置
    ("猫",            "小猫咪",                        True),   # 单字走 LIKE 那条路
]

EXPAND_CASES = [
    ("复仇者联盟4", "复仇 仇者 者联 联盟 4"),   # 中文数字粘连，4 要能被单独命中
    ("The Matrix", "The Matrix"),
    ("鬼灭之刃 S01E05", "鬼灭 灭之 之刃 S01E05"),
]


def selftest(verbose=False):
    """返回失败列表。btcheck 会调它，所以不能有 print 以外的副作用。"""
    bad = []
    for q, want in QUERY_CASES:
        got = build_match(q)
        if got != want:
            bad.append(("build_match", q, want, got))
        if verbose:
            print("  %-24r -> %s" % (q, got or "(空)"))
    for t, want in EXPAND_CASES:
        got = expand_text(t)
        if got != want:
            bad.append(("expand_text", t, want, got))
    for q, nm, want in NAME_CASES:
        got = name_matches(q, nm)
        if got != want:
            bad.append(("name_matches", "%s / %s" % (q, nm), want, got))
    return bad


def fts_two_col(conn, table="torrents_fts") -> bool:
    """这个库的全文索引是不是两列的。老库是单列 body。"""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return False
    return "name" in cols and "files" in cols


def fts_write(conn, rowid, name, filelist, two_col=None):
    """
    往 FTS 表里写一行。两种表结构都认，这是唯一知道该写几列的地方——
    btindex.upsert 和 btprune 的修复都走这儿，免得两边各写一份再漂掉。

    索引正文 = 原文 + 中文二元组展开，名字和文件列表各一份。
    单列的老库把两份拼起来写进 body，和拆分之前完全一样。
    """
    if two_col is None:
        two_col = fts_two_col(conn)
    name_body = "%s %s" % (name, expand_text(name))
    files_body = "%s %s" % (filelist, expand_text(filelist))
    if two_col:
        conn.execute("INSERT INTO torrents_fts(rowid, name, files) VALUES (?,?,?)",
                     (rowid, name_body, files_body))
    else:
        conn.execute("INSERT INTO torrents_fts(rowid, body) VALUES (?,?)",
                     (rowid, "%s %s" % (name_body, files_body)))


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
    # `[\d.]+` 能放过 `1.2.3` 这种多个小数点的写法，交给 float() 就抛出
    # 「could not convert string to float」——一句英文的 Python 内部报错，
    # 而同一个函数对 `abc` 给的是中文提示。同样是看不懂的输入，
    # 报错该长一个样，所以这里把小数点数目也一起卡掉
    m = re.match(r"(\d+(?:\.\d+)?)\s*([KMGTP])i?B?$", s, re.I)
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

def ensure_columns(conn):
    """
    幂等地把 peers / checked_at 补到老库上。

    新库由 SCHEMA 带出来，不用管。但 `CREATE TABLE IF NOT EXISTS` 对已经存在的表
    什么也不做——它不会去比对列——所以 2026-09 之前建的库不跑这一趟就永远缺这两列。

    放在这里而不是 btpeers 里，是因为网页是**只读**打开的，加不了列。
    只要跑过一次爬虫、导入或维护（都要写，都会走 Index.__init__），列就补上了。
    这和 idx_source 是同一个故事。

    返回这次真加了哪几列，调用方想说一句就说，不想说就扔掉。
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(torrents)")}
    added = []
    if "peers" not in have:
        conn.execute("ALTER TABLE torrents ADD COLUMN peers INTEGER NOT NULL DEFAULT -1")
        added.append("peers")
    if "checked_at" not in have:
        conn.execute("ALTER TABLE torrents ADD COLUMN checked_at INTEGER NOT NULL DEFAULT 0")
        added.append("checked_at")
    # 无条件建，不是「加了列才建」。新库的列由 SCHEMA 带出来，走不到上面那两个
    # 分支，但索引同样需要；IF NOT EXISTS 让重复调用不花钱。
    # 「只看还有人做种的」和「按做种数排序」都靠 idx_peers。绝大多数行是 -1，
    # 索引偏得厉害，但要的正是偏出来那一小撮。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_peers   ON torrents(peers)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checked ON torrents(checked_at)")
    # kind / res / parsed 三列连同它们的索引交给 btparse——那三列的定义只有一处，
    # 这里只是把它串进同一趟补列里，好让任何一个写库的工具开一次库就全补齐。
    added += ensure_parse_columns(conn)
    conn.commit()
    return added


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
        ensure_columns(self.db)          # 老库补列，新库这一步什么也不做
        # 全文索引是两列还是老的单列，开库时探一次记住。
        # 表结构在一个连接的生命周期里不会变——要变得走 btmigrate，那要独占写
        self.fts2 = fts_two_col(self.db)
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
        # 写的时候顺手解析，这样只有老条目需要回填，新进来的一律是解析好的。
        # 实测一条一两个微秒，相比爬虫那边一条种子几百毫秒的网络等待可以忽略。
        kind, res = parse_fields(name, filelist)

        if row:
            self.db.execute(
                "UPDATE torrents SET name=?, size=?, nfiles=?, filelist=?, "
                "source=?, last_seen=?, hits=hits+1, kind=?, res=?, parsed=? "
                "WHERE infohash=?",
                (name, size, nfiles, filelist, source, now,
                 kind, res, PARSE_VERSION, infohash))
            # FTS 表手动跟着改。所有写操作都走这个函数，所以不用触发器也能保持一致；
            # 真要多入口写库，就得换成 AFTER INSERT/UPDATE/DELETE 触发器。
            self.db.execute("DELETE FROM torrents_fts WHERE rowid=?", (row["rowid"],))
            fts_write(self.db, row["rowid"], name, filelist, self.fts2)
            return False

        cur = self.db.execute(
            "INSERT INTO torrents(infohash,name,size,nfiles,filelist,source,"
            "first_seen,last_seen,hits,kind,res,parsed) "
            "VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
            (infohash, name, size, nfiles, filelist, source, now, now,
             kind, res, PARSE_VERSION))
        fts_write(self.db, cur.lastrowid, name, filelist, self.fts2)
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
        # 只看正向词。`猫 -狗` 的正向部分还是一个单字，该走 LIKE 就得走：
        # 按整串判的话它有两个 token、第二个长度不为 1，会被判成不用回退，
        # 于是交给 FTS——而单字在二元组索引里配不上任何东西，静默返回 0 条。
        # 加一个排除词就把搜索搞哑了，这种坑必须在这里堵住。
        toks = [t for excl, _ph, t in split_query(query) if not excl and t]
        # 只对「单个中文字」回退。单个拉丁字母交给 FTS 按词匹配更准 ——
        # 用 LIKE 的话，搜 a 会把所有含字母 a 的条目全捞出来，噪音大到没法用。
        return bool(toks) and all(len(t) == 1 and is_cjk(t) for t in toks)

    @staticmethod
    def like_clause(query):
        """
        返回 (SQL 片段, 参数)。每个词都要出现在名字或文件列表里，`-词` 则要求两边都没有。

        这条路只有单字查询走得到，但排除词照样得认：`猫 -狗` 在 FTS 那边能用、
        在这边不能用的话，就是同一个搜索框里两种语法，比没有这个功能还糟。
        短语在这里不用特殊处理——LIKE 本来就是按整个子串匹配的，
        `"老 友"` 里的空格原样找过去，正好就是相邻的意思。
        """
        toks = [(excl, t) for excl, _ph, t in split_query(query) if t][:8]
        if not toks or all(excl for excl, _t in toks):
            return "", []
        parts, params = [], []
        for excl, t in toks:
            pat = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            if excl:
                parts.append("(t.name NOT LIKE ? ESCAPE '\\' "
                             "AND t.filelist NOT LIKE ? ESCAPE '\\')")
            else:
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
            "relevance": "%s, t.hits DESC" % BM25,
            "hits":      "t.hits DESC, %s" % BM25,
            "size":      "t.size DESC",
            "date":      "t.last_seen DESC",
        }.get(sort, "%s, t.hits DESC" % BM25)

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


def cmd_test(args):
    bad = selftest(verbose=args.verbose)
    n = len(QUERY_CASES) + len(EXPAND_CASES) + len(NAME_CASES)
    print("分词与查询自测：%d 条用例，%d 条不通过" % (n, len(bad)))
    for where, src, want, got in bad:
        print("  %s(%r)\n    期望 %r\n    实际 %r" % (where, src, want, got))
    sys.exit(1 if bad else 0)


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

    p = sub.add_parser("test", help="跑分词和查询构造的自测，不碰数据库")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="把每条查询生成的表达式打出来")
    p.set_defaults(func=cmd_test)

    if ap.epilog:
        ap.epilog = ap.epilog.replace("python3 ", py_cmd() + " ")
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
