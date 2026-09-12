#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btsearch —— 一个可扩展的 BT 资源聚合搜索命令行工具

思路很简单，三层：
  1. Provider：每个数据源只干一件事——把关键词变成一批结果，接口统一；
  2. 聚合层：并发去问所有 Provider，按 infohash 去重，按做种数排序；
  3. 展示层：表格 / 纯磁力链 / JSON 三种输出。

想接新站点，只要照着写一个 Provider 子类，塞进 PROVIDERS 就行，主程序一行都不用动。

内置的三个源：
  - InternetArchive   互联网档案馆，几百万个公开条目，每个都自动生成 .torrent
  - AcademicTorrents  科研数据集（ImageNet、各种基因组和语料库都在上面）
  - Torznab           通用协议，用来对接你自己跑的 Jackett / Prowlarr

只用标准库，Python 3.8+ 直接跑，不用 pip 装东西。
"""

import argparse
import hashlib
import os
import json
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcompat import setup_console
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from dataclasses import dataclass, asdict
from typing import List, Optional

UA = "btsearch/1.0 (+https://example.invalid)"
TIMEOUT = 20

# 公开的开放 tracker，只用来拼磁力链，方便客户端更快找到人
DEFAULT_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
]


# --------------------------------------------------------------------------
# 一些小工具
# --------------------------------------------------------------------------

_opener = None


def set_proxy(proxy):
    """外部源在部分网络里直连不通，留个代理口子。"""
    global _opener
    _opener = (urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        if proxy else None)


def http_get(url: str, binary: bool = False):
    """带 UA 和超时的 GET。BT 站点大多会拦空 UA 的请求。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
    })
    opened = _opener.open(req, timeout=TIMEOUT) if _opener \
        else urllib.request.urlopen(req, timeout=TIMEOUT)
    with opened as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", errors="replace")


def _bdecode(data: bytes, i: int):
    """
    极简 bencode 解码，返回 (值, 下一个字节的位置)。
    .torrent 文件就是 bencode 编码的，只需要够用就行，不追求完备。
    """
    c = data[i:i + 1]
    if c == b"i":                              # 整数 i123e
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":                              # 列表 l...e
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":                              # 字典 d...e
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            out[k] = v
        return out, i + 1
    if c.isdigit():                            # 字符串 5:hello
        j = data.index(b":", i)
        n = int(data[i:j])
        return data[j + 1:j + 1 + n], j + 1 + n
    raise ValueError("bencode 解析失败，位置 %d" % i)


def torrent_infohash(data: bytes) -> Optional[str]:
    """
    从 .torrent 文件内容算出 v1 infohash。
    定义就是：info 这个子字典的原始字节做 SHA-1。注意必须用文件里的原始字节，
    自己重新编码一遍再哈希，顺序稍微差一点结果就全变了。
    """
    key = b"4:info"
    start = data.find(key)
    if start < 0:
        return None
    start += len(key)
    try:
        _, end = _bdecode(data, start)         # 解析一遍，只为拿到 info 的结束位置
    except (ValueError, IndexError):
        return None
    return hashlib.sha1(data[start:end]).hexdigest()


def make_magnet(infohash: str, name: str = "", trackers=None) -> str:
    """把 infohash 拼成磁力链。磁力链本身就是 infohash + 一堆可选提示。"""
    parts = ["magnet:?xt=urn:btih:" + infohash]
    if name:
        parts.append("dn=" + urllib.parse.quote(name))
    for tr in (trackers if trackers is not None else DEFAULT_TRACKERS):
        parts.append("tr=" + urllib.parse.quote(tr, safe=""))
    return "&".join(parts)


def parse_size(value) -> int:
    """把 '1.2 GB' / '1234567' / 1234567 都变成字节数。"""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    m = re.match(r"([\d.]+)\s*([KMGTP]?)i?B", s, re.I)
    if not m:
        return 0
    mult = {"": 1, "K": 1024, "M": 1024 ** 2,
            "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
    return int(float(m.group(1)) * mult[m.group(2).upper()])


def human_size(n: int) -> str:
    if n <= 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.1f %s" % (n, unit)).replace(".0 ", " ")
        n /= 1024.0
    return "-"


def first(value):
    """Internet Archive 有些字段会给数组，取第一个。"""
    if isinstance(value, list):
        return value[0] if value else ""
    return value


# --------------------------------------------------------------------------
# 统一的结果结构
# --------------------------------------------------------------------------

@dataclass
class Result:
    source: str
    name: str
    size: int = 0            # 字节数，0 表示未知
    seeders: int = -1        # -1 表示这个源没提供
    leechers: int = -1
    infohash: str = ""
    magnet: str = ""
    torrent_url: str = ""    # .torrent 文件直链
    page_url: str = ""       # 详情页，人肉核对用

    def key(self):
        """去重用的键：优先 infohash，没有就退化成 名字+大小。"""
        return self.infohash.lower() or (self.name.lower(), self.size)

    def ensure_magnet(self):
        if not self.magnet and self.infohash:
            self.magnet = make_magnet(self.infohash, self.name)
        return self.magnet


# --------------------------------------------------------------------------
# Provider：每接一个新源，就在这儿加一个类
# --------------------------------------------------------------------------

class Provider:
    name = "base"

    def search(self, keyword: str, limit: int) -> List[Result]:
        raise NotImplementedError


class InternetArchive(Provider):
    """
    互联网档案馆。它的 advancedsearch 是 Lucene 语法，
    每个条目都会自动生成一个 {identifier}_archive.torrent。
    公有领域电影、老游戏、软件、录音基本都在这儿。
    """
    name = "InternetArchive"
    API = "https://archive.org/advancedsearch.php"

    def search(self, keyword, limit):
        query = urllib.parse.urlencode([
            ("q", keyword),
            ("rows", str(limit)),
            ("page", "1"),
            ("output", "json"),
            ("sort[]", "downloads desc"),
            ("fl[]", "identifier"),
            ("fl[]", "title"),
            ("fl[]", "item_size"),
            ("fl[]", "downloads"),
            ("fl[]", "mediatype"),
        ])
        payload = json.loads(http_get(self.API + "?" + query))
        docs = payload.get("response", {}).get("docs", [])

        out = []
        for d in docs:
            ident = first(d.get("identifier"))
            if not ident:
                continue
            out.append(Result(
                source=self.name,
                name=str(first(d.get("title")) or ident),
                size=parse_size(d.get("item_size")),
                torrent_url="https://archive.org/download/{0}/{0}_archive.torrent".format(ident),
                page_url="https://archive.org/details/" + ident,
            ))
        return out


class AcademicTorrents(Provider):
    """
    科研数据集专用站，非营利组织在运营，上面全是研究者主动分享的数据。
    接口是个没门槛的 JSON，详情页地址最后一段就是 infohash。
    """
    name = "AcademicTorrents"
    API = "https://academictorrents.com/apiv2/entries"

    def search(self, keyword, limit):
        url = self.API + "?" + urllib.parse.urlencode({"search": keyword})
        items = json.loads(http_get(url))
        if isinstance(items, dict):                 # 有时候会包一层
            items = items.get("entries") or items.get("data") or []

        out = []
        for it in items[:limit]:
            page = it.get("url") or ""
            ih = page.rstrip("/").split("/")[-1]
            if len(ih) != 40:
                ih = ""
            if page.startswith("/"):
                page = "https://academictorrents.com" + page
            out.append(Result(
                source=self.name,
                name=it.get("name") or "(未命名)",
                size=parse_size(it.get("size")),
                seeders=int(it.get("mirrors") or -1),
                leechers=int(it.get("downloaders") or -1),
                infohash=ih,
                page_url=page,
                torrent_url=("https://academictorrents.com/download/%s.torrent" % ih) if ih else "",
            ))
        return out


class Torznab(Provider):
    """
    Torznab 是索引站的通用查询协议（Jackett / Prowlarr / NZBHydra 都说这套话）。
    自己在本地起一个 Jackett，把想要的站点在它界面里配好，
    这里就能用同一个接口把它们全接进来——不用给每个站单独写爬虫。

    命令行传参格式： --torznab "http://127.0.0.1:9117/api/v2.0/indexers/all/results/torznab/api|你的APIKEY"
    """
    NS = {"torznab": "http://torznab.com/schemas/2015/feed"}

    def __init__(self, base_url: str, api_key: str = "", label: str = "Torznab"):
        self.base_url = base_url
        self.api_key = api_key
        self.name = label

    def search(self, keyword, limit):
        params = {"t": "search", "q": keyword, "limit": str(limit)}
        if self.api_key:
            params["apikey"] = self.api_key
        xml = http_get(self.base_url + "?" + urllib.parse.urlencode(params))
        root = ET.fromstring(xml)

        out = []
        for item in root.iter("item"):
            attrs = {}
            for a in item.findall("torznab:attr", self.NS):
                attrs[a.get("name")] = a.get("value")

            r = Result(
                source=self.name,
                name=(item.findtext("title") or "").strip(),
                size=parse_size(item.findtext("size") or attrs.get("size")),
                seeders=int(attrs.get("seeders") or -1),
                leechers=int(attrs.get("peers") or -1),
                infohash=(attrs.get("infohash") or "").strip(),
                magnet=(attrs.get("magneturl") or "").strip(),
                page_url=(item.findtext("comments") or "").strip(),
            )
            link = (item.findtext("link") or "").strip()
            if link.startswith("magnet:"):
                r.magnet = r.magnet or link
            elif link:
                r.torrent_url = link
            # 磁力链里能直接抠出 infohash
            if not r.infohash and r.magnet:
                m = re.search(r"btih:([0-9a-fA-F]{40}|[2-7A-Z]{32})", r.magnet)
                if m:
                    r.infohash = m.group(1)
            out.append(r)
        return out


class LocalIndex(Provider):
    """
    查自己爬出来的库。这是唯一不依赖任何外部站点的数据源——
    别的站可以关停、可以改版、可以封你 IP，本地索引一直都在。
    """
    name = "LocalIndex"

    def __init__(self, db_path):
        self.db_path = db_path

    def search(self, keyword, limit):
        from btindex import Index, magnet as make_magnet_link
        idx = Index(self.db_path)
        try:
            rows = idx.search(keyword, limit=limit)
        finally:
            idx.close()
        # 库里只存 infohash，磁力链是现拼的——存一份等长的字符串纯属浪费
        return [Result(
            source=self.name,
            name=r["name"],
            size=r["size"],
            seeders=r["hits"],          # 用 announce 次数当热度，量纲不同但排序意义相近
            infohash=r["infohash"],
            magnet=make_magnet_link(r["infohash"], r["name"]),
        ) for r in rows]


# --------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------

BUILTIN = {
    "ia": InternetArchive,
    "academic": AcademicTorrents,
}


def search_all(providers, keyword, limit, workers=8, verbose=True):
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(p.search, keyword, limit): p for p in providers}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                got = fut.result()
                results.extend(got)
                if verbose:
                    print("[ok]   %-18s %d 条" % (p.name, len(got)), file=sys.stderr)
            except Exception as e:
                # 一个源挂了不影响其他源，这是并发聚合的意义所在
                print("[warn] %-18s 失败: %s" % (p.name, e), file=sys.stderr)

    # 合并同一个种子在多个源的记录。
    # 早先这里是「见过就丢」，但并发下谁先返回是随机的，等于随机丢掉信息——
    # 甲站可能有做种数，乙站可能有磁力链，都留下来才划算。
    merged = {}
    for r in results:
        k = r.key()
        cur = merged.get(k)
        if cur is None:
            # 拷贝一份再存：下面会就地修改，不能污染 provider 返回的原对象
            merged[k] = copy.copy(r)
            continue
        cur.seeders = max(cur.seeders, r.seeders)
        cur.leechers = max(cur.leechers, r.leechers)
        cur.size = cur.size or r.size
        cur.infohash = cur.infohash or r.infohash
        cur.magnet = cur.magnet or r.magnet
        cur.torrent_url = cur.torrent_url or r.torrent_url
        cur.page_url = cur.page_url or r.page_url
        if r.source not in cur.source.split("+"):
            cur.source = cur.source + "+" + r.source     # 标明它在哪几个源都出现过
    uniq = list(merged.values())

    # 有做种数的排前面，没有的按体积排
    uniq.sort(key=lambda r: (r.seeders if r.seeders >= 0 else -1, r.size), reverse=True)
    return uniq


def resolve_infohashes(results, workers=6):
    """
    对只有 .torrent 直链、没有 infohash 的结果，下载种子文件算出 infohash。
    要多发一轮网络请求，所以做成可选（--resolve）。
    """
    targets = [r for r in results if not r.infohash and r.torrent_url]
    if not targets:
        return

    failed = {}
    lock = threading.Lock()

    def work(r):
        try:
            r.infohash = torrent_infohash(http_get(r.torrent_url, binary=True)) or ""
            if not r.infohash:
                with lock:
                    failed["下回来的不是合法种子"] = failed.get("下回来的不是合法种子", 0) + 1
        except Exception as e:
            # 以前这里是 except: pass，结果「有些条目没有磁力链」没有任何线索
            why = "%s: %s" % (type(e).__name__, str(e)[:48])
            with lock:
                failed[why] = failed.get(why, 0) + 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, targets))
    if failed:
        print("有 %d 条没能补上磁力链：" % sum(failed.values()), file=sys.stderr)
        for why, n in sorted(failed.items(), key=lambda kv: -kv[1])[:4]:
            print("  %4d 次  %s" % (n, why), file=sys.stderr)


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def print_table(results, width=62):
    if not results:
        print("没搜到东西。换个关键词，或者加一个 --torznab 源试试。")
        return
    print("%-4s %-*s %10s %7s %-16s" % ("#", width, "名称", "大小", "做种", "来源"))
    print("-" * (4 + width + 10 + 7 + 16 + 4))
    for i, r in enumerate(results, 1):
        name = r.name if len(r.name) <= width else r.name[:width - 1] + "…"
        seed = str(r.seeders) if r.seeders >= 0 else "-"
        print("%-4d %-*s %10s %7s %-16s" % (i, width, name, human_size(r.size), seed, r.source))


def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description="聚合搜索 BT 资源，输出磁力链",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python btsearch.py "ubuntu 24.04"
  python btsearch.py mnist --source academic --magnet
  python btsearch.py "night of the living dead" --source ia --resolve --magnet
  python btsearch.py 关键词 --torznab "http://127.0.0.1:9117/api/v2.0/indexers/all/results/torznab/api|APIKEY"
""")
    ap.add_argument("keyword", help="搜索关键词")
    ap.add_argument("-n", "--limit", type=int, default=20, help="每个源最多取几条（默认 20）")
    ap.add_argument("-s", "--source", action="append", choices=list(BUILTIN),
                    help="只用指定的内置源，可重复；不写就是全用")
    ap.add_argument("--torznab", action="append", default=[],
                    metavar="URL|APIKEY", help="额外的 Torznab 源，可重复")
    ap.add_argument("--db", default="", help="同时检索本地自建索引（btindex 的数据库）")
    ap.add_argument("--proxy", default="",
                    help="外部源走本地代理，如 http://127.0.0.1:7890")
    ap.add_argument("--resolve", action="store_true",
                    help="下载 .torrent 补齐 infohash（会慢一些，但能拿到磁力链）")
    ap.add_argument("--magnet", action="store_true", help="只输出磁力链，方便管道传给下载器")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    set_proxy(args.proxy)
    providers = [BUILTIN[k]() for k in (args.source or list(BUILTIN))]
    if args.db:
        providers.append(LocalIndex(args.db))
    for spec in args.torznab:
        url, _, key = spec.partition("|")
        label = "Torznab:" + urllib.parse.urlparse(url).hostname
        providers.append(Torznab(url, key, label))

    results = search_all(providers, args.keyword, args.limit,
                         verbose=not (args.magnet or args.json))

    if args.resolve or args.magnet:
        resolve_infohashes(results)
    for r in results:
        r.ensure_magnet()

    if args.json:
        print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))
    elif args.magnet:
        for r in results:
            if r.magnet:
                print(r.magnet)
    else:
        print_table(results)
        print("\n加 --magnet 输出磁力链，加 --json 输出结构化结果。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
