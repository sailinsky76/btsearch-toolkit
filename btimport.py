#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btimport —— 把外部资源库批量灌进本地索引

自己爬 DHT 攒资源太慢，尤其在入站不通的网络里。这个工具直接从现成的库里
批量拉取，写进同一个 bt.db，网页和命令行搜索立刻就能搜到。

四个来源，性质完全不同：

  ia        互联网档案馆。公开资料库，几千万个条目，每个都自动生成种子。
            关键点：infohash 不用逐个下载种子文件去算，条目元数据的 files 里
            直接带着 btih 字段，一次请求就能拿全名字、大小、文件列表。
            深度翻页必须用 scrape 接口（带游标），advancedsearch 卡在 1 万条。

  academic  Academic Torrents，科研数据集，非营利组织运营。

  torznab   通用索引协议。你自己在本地跑一个 Jackett 或 Prowlarr，
            在它界面里配好想要的站点，这里用一个接口就能全接进来。
            这条路能接的站点数量级最大，但接哪些由你决定。

  folder    本地已有的一堆 .torrent 文件，直接算出 infohash 入库。

用法：
    py -3.11 btimport.py probe ia              先探一下，确认接口没变
    py -3.11 btimport.py ia --limit 5000
    py -3.11 btimport.py ia --query "mediatype:movies AND year:[2000 TO 2025]" --limit 20000
    py -3.11 btimport.py academic --limit 2000
    py -3.11 btimport.py torznab "http://127.0.0.1:9117/api/v2.0/indexers/all/results/torznab/api|APIKEY"
    py -3.11 btimport.py folder D:\\torrents

依赖：无，标准库足够。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcompat import BUILD, py_cmd, setup_console
from btindex import DB_DEFAULT, Index, human
from dhtsniff import _bdecode

UA = "btimport/1.0"
_opener = None          # 指定 --proxy 时用它，否则走系统默认（Windows 会读 IE 代理设置）


def set_timeout(sec):
    global TIMEOUT
    TIMEOUT = max(5, int(sec))


PROXY = [""]


def set_proxy(proxy):
    global _opener
    PROXY[0] = proxy or ""
    if not proxy:
        _opener = None
        return
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    _opener = urllib.request.build_opener(handler)


def _urlopen(req):
    if _opener is not None:
        return _opener.open(req, timeout=TIMEOUT)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def net_hint(err) -> str:
    """把网络错误翻译成能照着做的下一步。"""
    text = str(err)
    if "10060" in text or "timed out" in text or "timeout" in text.lower():
        return ("连接超时 —— 这个站点在你当前网络里不可达。\n"
                "  如果你有本地代理，加上 --proxy 再试，例如：\n"
                "    --proxy http://127.0.0.1:7890     (Clash 默认)\n"
                "    --proxy http://127.0.0.1:10809    (v2rayN 默认)\n"
                "  代理端口在你的客户端界面里能看到。")
    if "getaddrinfo" in text or "Name or service" in text or "11001" in text:
        return "域名解析失败 —— DNS 有问题，或者被拦了。同样可以试试 --proxy。"
    # TLS 相关的错误要分两类，给的建议完全相反
    if "UNEXPECTED_EOF" in text or "EOF occurred in violation" in text \
            or "reset by peer" in text or "10054" in text:
        return ("TLS 握手被中途掐断 —— 连接刚建立就被重置，"
                "通常意味着这个站点在你的网络里被阻断了，不是证书问题。\n"
                "  如果别的站点（比如互联网档案馆）已经能通，那多半是"
                "代理客户端在按规则分流：\n"
                "  已知域名走代理，没收录的域名走直连，于是这个站还是被挡。\n"
                "  解决办法二选一：把客户端切到全局模式，"
                "或者给 academictorrents.com 单独加一条走代理的规则。\n"
                "  如果你有本地代理，加上 --proxy 再试，例如：\n"
                "    --proxy http://127.0.0.1:7890     (Clash 默认)\n"
                "    --proxy http://127.0.0.1:10809    (v2rayN 默认)\n"
                "  网页任务面板里也有「代理」那一栏。")
    if "certificate verify failed" in text or "CERTIFICATE_VERIFY" in text:
        return ("证书校验失败 —— 多半是中间有设备在解密流量（企业网关或某些代理）。"
                "检查代理设置，或换一个代理。")
    if "SSL" in text or "TLS" in text:
        return ("TLS 出错：%s\n  多半是网络路径的问题，试试 --proxy。" % text[:60])
    return "网络不通，或者对方接口改了。"
TIMEOUT = 30          # 可被 --timeout 覆盖
HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
DUMP = [True]          # 取不到 infohash 时是否打印原始 XML，--no-dump 可关


# --------------------------------------------------------------------------
# 公共小工具
# --------------------------------------------------------------------------

def http_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            with _urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))      # 退避重试，别把对方服务器敲爆


def http_text(url, retries=2):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with _urlopen(req) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # Jackett 出错时回的是 400/500，但响应体里写着真正的原因
            # （某站点连不上、Cloudflare 拦截等）。不读出来就只剩一句 Bad Request。
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            detail = ""
            m = re.search(r'description="([^"]+)"', body)      # Torznab 的 XML 错误
            if m:
                detail = m.group(1)
            else:
                # Jackett 的管理类错误回的是 JSON，里面还带一大段堆栈。
                # 只取 error 字段，堆栈对使用者没有意义。
                try:
                    obj = json.loads(body)
                    detail = str(obj.get("error") or obj.get("result") or "")
                except (ValueError, AttributeError):
                    detail = re.sub(r"<[^>]+>", " ", body).strip()[:160] if body.strip() else ""
            msg = "HTTP %s%s" % (e.code, ("：" + detail) if detail
                                 else "（Jackett 没给出原因）")
            if "Unknown indexer" in detail:
                wrong = detail.split(":", 1)[-1].strip()
                msg += ("\n  索引站 id 区分大小写，Jackett 的 id 全是小写。"
                        "「%s」试试写成「%s」。\n"
                        "  拿不准就先列一遍：%s btimport.py probe torznab \"地址|APIKEY\" --each"
                        % (wrong, wrong.lower(), py_cmd()))
            raise TorznabError(msg)
        except (urllib.error.URLError, OSError) as e:
            if attempt == retries - 1:
                raise
            time.sleep(1.5)


def torrent_infohash(data: bytes):
    """
    从 .torrent 内容算 v1 infohash：info 子字典的原始字节做 SHA-1。
    必须切文件里的原始字节——解析成字典再重新编码，字段顺序差一点结果就变了。
    """
    key = b"4:info"
    start = data.find(key)
    if start < 0:
        return None
    start += len(key)
    try:
        _, end = _bdecode(data, start)
    except (ValueError, IndexError):
        return None
    return hashlib.sha1(data[start:end]).hexdigest()


class Writer:
    """统一的入库出口，带计数和批量提交。"""

    @staticmethod
    def ensure_cover(db):
        """按需加 cover 列。老库升上来也能用，不需要重建。"""
        have = {r[1] for r in db.execute("PRAGMA table_info(torrents)")}
        if "cover" not in have:
            db.execute("ALTER TABLE torrents ADD COLUMN cover TEXT NOT NULL DEFAULT ''")
            db.commit()

    def __init__(self, db_path, commit_every=50, quiet=False):
        self.idx = Index(db_path)
        self.lock = threading.Lock()
        self.commit_every = commit_every
        self.quiet = quiet
        self.new = self.dup = self.bad = 0
        self._last_commit = time.time()
        self._cover_ready = False
        # 重复导入时要把 hits 的自增撤回来，理由见 _undo_bump
        self._seen_again = []

    def set_cover(self, infohash, url):
        """
        记下封面地址。只存地址，不下载图片 ——
        取不取图由用户在详情页点一下决定，不点就不会有任何对外请求。
        """
        if not url or not str(url).startswith(("http://", "https://")):
            return
        if not self._cover_ready:
            self.ensure_cover(self.idx.db)
            self._cover_ready = True
        self.idx.db.execute("UPDATE torrents SET cover=? WHERE infohash=? AND cover=''",
                            (str(url)[:500], infohash.lower()))

    def add(self, infohash, name, size=0, nfiles=0, files=(), source="import"):
        if not infohash or not HEX40.match(infohash):
            self.bad += 1
            return False
        with self.lock:
            try:
                fresh = self.idx.upsert(infohash.lower(), name or "(无名)",
                                        int(size or 0), int(nfiles or 0),
                                        files, source=source)
            except Exception:
                self.bad += 1
                return False
            if fresh:
                self.new += 1
                if not self.quiet:
                    print("  %-9s %s" % (human(int(size or 0)), (name or "")[:64]))
            else:
                self.dup += 1
                self._seen_again.append(infohash.lower())
            # 条数或时间任一到了就提交。攒太久的话，网页那边（独立只读连接）
            # 看不到新条目，中途按停止也会把未提交的成果丢掉。
            if ((self.new + self.dup) % self.commit_every == 0
                    or time.time() - self._last_commit > 3):
                self._undo_bump()
                self.idx.commit()
                self._last_commit = time.time()
            return fresh

    def _undo_bump(self):
        """
        撤销重复导入造成的 hits 自增（调用方已持锁）。

        hits 的本意是「这个种子被 announce 过多少次」，是真实的热度信号，
        网页上那根热度条和「按热度排序」都靠它。但 Index.upsert 对已存在的记录
        一律 hits+1，于是同一个站反复导入几次，那些条目的热度就被抬上去了 ——
        涨的是「我跑了几次导入」，跟种子本身没关系，还会盖过爬虫发现的真热门。
        所以导入这条路要把自增撤回去，批量一条 SQL 解决。
        """
        if not self._seen_again:
            return
        batch, self._seen_again = self._seen_again, []
        for i in range(0, len(batch), 400):      # SQLite 有变量数上限，分批
            chunk = batch[i:i + 400]
            self.idx.db.execute(
                "UPDATE torrents SET hits = hits - 1 WHERE hits > 1 AND infohash IN (%s)"
                % ",".join("?" * len(chunk)), chunk)

    def close(self):
        with self.lock:
            self._undo_bump()
            self.idx.commit()
            total = self.idx.stats()["count"]
            self.idx.close()
        print("\n新增 %d 条，已存在 %d 条，跳过 %d 条无效；库里现有 %d 条"
              % (self.new, self.dup, self.bad, total))


# --------------------------------------------------------------------------
# 来源一：互联网档案馆
# --------------------------------------------------------------------------

IA_SCRAPE = "https://archive.org/services/search/v1/scrape"
IA_META = "https://archive.org/metadata/"
# 只要有种子文件的条目。没有这个限定会拉回大量取不到 btih 的条目，白跑一趟。
IA_BASE_Q = 'format:"Archive BitTorrent"'


def ia_identifiers(query, limit, page_size=1000):
    """
    用 scrape 接口滚动翻页。advancedsearch 深翻只能到 1 万条，
    scrape 靠游标可以一直往下走，这是批量导入的前提。
    """
    q = "(%s)" % IA_BASE_Q if not query else "(%s) AND (%s)" % (IA_BASE_Q, query)
    cursor, got = None, 0
    while got < limit:
        params = {"q": q, "fields": "identifier,title,item_size,mediatype",
                  "count": max(100, min(page_size, limit - got, 9999))}
        if cursor:
            params["cursor"] = cursor
        data = http_json(IA_SCRAPE + "?" + urllib.parse.urlencode(params))
        items = data.get("items") or []
        if not items:
            return
        for it in items:
            yield it
            got += 1
            if got >= limit:
                return
        cursor = data.get("cursor")
        if not cursor:          # 没有游标就是到底了
            return


def ia_item_detail(identifier):
    """
    取单个条目的元数据，挖出 btih 和真实文件列表。
    IA 会给每个条目自动生成一个 {id}_archive.torrent，它的 btih 就在这里，
    不用把种子文件下载下来自己算——这是整个导入能跑快的关键。
    """
    meta = http_json(IA_META + urllib.parse.quote(identifier))
    files = meta.get("files") or []
    btih = None
    for f in files:
        if f.get("format") == "Archive BitTorrent" or f.get("name", "").endswith("_archive.torrent"):
            btih = f.get("btih")
            if btih:
                break
    if not btih:
        return None

    # source=original 才是用户上传的正片，其余是 IA 自己派生的缩略图和元数据
    content = [f for f in files if f.get("source") == "original"
               and not f.get("name", "").startswith("__")]
    md = meta.get("metadata") or {}
    title = md.get("title") or identifier
    if isinstance(title, list):
        title = title[0] if title else identifier
    size = meta.get("item_size") or sum(int(f.get("size") or 0) for f in content)
    return {"infohash": btih.strip().lower(), "name": str(title),
            "size": int(size or 0), "nfiles": len(content) or 1,
            "files": [f.get("name", "") for f in content[:60]],
            # 档案馆给每个条目都生成缩略图，地址可以直接由标识拼出来
            "cover": "https://archive.org/services/img/" + urllib.parse.quote(identifier)}


def cmd_ia(args, writer):
    print("从互联网档案馆导入，最多 %d 条%s"
          % (args.limit, ("，条件：" + args.query) if args.query else ""))
    print("-" * 66)
    ids = []
    try:
        for it in ia_identifiers(args.query, args.limit):
            ident = it.get("identifier")
            if ident:
                ids.append(ident)
    except Exception as e:
        print("列表拉取中断：%s（已拿到 %d 个）" % (e, len(ids)))
        if not ids:
            print(net_hint(e))
            return
    if not ids:
        print("没拉到条目。检查网络，或把 --query 放宽一点。")
        return
    print("拿到 %d 个条目，开始取详情…\n" % len(ids))

    done = [0]
    def work(ident):
        try:
            d = ia_item_detail(ident)
            if d:
                if d.get("cover"):
                    writer.set_cover(d["infohash"], d["cover"])
                writer.add(d["infohash"], d["name"], d["size"], d["nfiles"],
                           d["files"], source="ia")
        except Exception:
            writer.bad += 1
        finally:
            done[0] += 1
            if done[0] % 200 == 0:
                print("  … 已处理 %d/%d" % (done[0], len(ids)), file=sys.stderr)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, ids))


# --------------------------------------------------------------------------
# 来源二：Academic Torrents
# --------------------------------------------------------------------------

# 官方文档（academictorrents-docs/docs/api.md）里公布的全库导出。
# 注意不要用 /apiv2/entries?search= —— 那个接口在官方 API 文档里根本不存在，
# 网上一些客户端里抄来抄去的写法早就失效了，服务器只会把连接断掉，
# 看起来像网络问题，其实是接口没了。
AT_DB = "https://academictorrents.com/database.xml"


def at_items(xml):
    """从全库 XML 里解析条目。字段就是 title / infohash / size / category。"""
    root = ET.fromstring(xml)
    for item in root.iter("item"):
        ih = (item.findtext("infohash") or "").strip().lower()
        if not HEX40.match(ih):
            continue
        yield {
            "infohash": ih,
            "name": (item.findtext("title") or "").strip() or "(无名)",
            "size": _size(item.findtext("size")),
            "category": (item.findtext("category") or "").strip(),
            "desc": (item.findtext("description") or "").strip(),
        }


def cmd_academic(args, writer):
    print("从 Academic Torrents 导入")
    print("一次拉取全库清单（%s），不再逐个关键词搜。" % AT_DB)
    print("-" * 66)
    try:
        xml = http_text(AT_DB, retries=2)
    except Exception as e:
        print("拉取失败：%s" % e)
        print(net_hint(e))
        return
    print("拿到 %.1f MB，开始解析…" % (len(xml.encode("utf-8")) / 1048576))

    try:
        items = list(at_items(xml))
    except ET.ParseError as e:
        print("解析失败：%s —— 对方返回的可能不是 XML（被网关拦了？）" % e)
        print("开头是：%s" % xml[:120].replace("\n", " "))
        return
    print("全库共 %s 条" % format(len(items), ","))

    q = (args.query or "").strip().lower()
    if q:
        items = [it for it in items
                 if q in it["name"].lower() or q in it["desc"].lower()]
        print("匹配「%s」的有 %s 条" % (args.query, format(len(items), ",")))

    for it in items:
        if writer.new >= args.limit:
            print("已达 --limit %d，停止。" % args.limit)
            return
        # 学术数据集的标题常常很晦涩（比如 GTDB R09-RS220），
        # 真正说明是什么的内容在描述里。把描述一起喂进全文索引，
        # 搜「echocardiography」「Wikipedia」这种词才找得到。
        extra = [it["category"]] if it["category"] else []
        if it["desc"]:
            extra += [it["desc"][i:i + 200] for i in range(0, min(len(it["desc"]), 1200), 200)]
        writer.add(it["infohash"], it["name"], it["size"], 1, extra, source="academic")


def _size(v):
    if v is None:
        return 0
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    m = re.match(r"([\d.]+)\s*([KMGTP])?i?B", s, re.I)
    if not m:
        return 0
    mult = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    return int(float(m.group(1)) * mult[(m.group(2) or "").upper()])


# --------------------------------------------------------------------------
# 来源三：Torznab（你自己的 Jackett / Prowlarr）
# --------------------------------------------------------------------------

NS = {"torznab": "http://torznab.com/schemas/2015/feed"}


class TorznabError(Exception):
    pass


def _b32_to_hex(s32):
    try:
        import base64
        pad = "=" * (-len(s32) % 8)
        return base64.b32decode(s32.upper() + pad).hex()
    except Exception:
        return ""


def item_download_url(item):
    """
    取这条 item 的下载地址。优先 enclosure，其次 link。
    有些索引站压根不给磁力链，只给 .torrent —— 那就得把种子下下来自己算 infohash。
    """
    for el in item:
        if el.tag.split("}")[-1] == "enclosure" and el.get("url"):
            u = el.get("url")
            if not u.startswith("magnet:"):
                return u
    link = (item.findtext("link") or "").strip()
    return link if link and not link.startswith("magnet:") else ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None          # 不跟随，自己看 Location


def resolve_download(url):
    """
    取 Jackett 的 /dl/ 地址。它有两种可能的结果：

      302 跳到 magnet:   站点本来给的就是磁力链，Jackett 只是包了一层代理地址
      直接回 .torrent    站点只给种子文件

    默认的 urlopen 会自动跟随跳转，然后在 magnet: 上抛
    "unknown url type: magnet" —— 这正是只给磁力链的站导入为 0 的原因。
    所以这里禁掉自动跟随，自己判断是哪种。
    返回 ("magnet", 磁力链) 或 ("torrent", 字节)。
    """
    handlers = [_NoRedirect]
    if PROXY[0]:
        handlers.insert(0, urllib.request.ProxyHandler({"http": PROXY[0],
                                                        "https": PROXY[0]}))
    op = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with op.open(req, timeout=TIMEOUT) as resp:
            return "torrent", resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location") or ""
            if loc.startswith("magnet:"):
                return "magnet", loc
            raise TorznabError("跳到了不认识的地址：%s" % loc[:60])
        raise


def parse_torrent_bytes(data):
    """把 .torrent 内容解析成 (infohash, 名字, 总大小, 文件列表)。"""
    ih = torrent_infohash(data)
    if not ih:
        return None
    name, size, files = "", 0, []
    try:
        info = (_bdecode(data, 0)[0] or {}).get(b"info") or {}
        raw = info.get(b"name.utf-8") or info.get(b"name") or b""
        name = raw.decode("utf-8", "replace")
        if isinstance(info.get(b"files"), list):
            for f in info[b"files"]:
                parts = f.get(b"path.utf-8") or f.get(b"path") or []
                files.append("/".join(x.decode("utf-8", "replace") for x in parts))
                size += int(f.get(b"length") or 0)
        else:
            size = int(info.get(b"length") or 0)
            files = [name] if name else []
    except Exception:
        pass
    return ih, name, size, files


def item_infohash(item):
    """
    从一条 item 里挖 infohash。

    Jackett 把磁力链放在哪儿，不同索引站差别很大：
      <torznab:attr name="infohash"/>、<torznab:attr name="magneturl"/>、
      <link>、<guid>、还有 <enclosure url="magnet:..."/>。
    而且 <link> 经常是 Jackett 自己的 /dl/ 代理下载地址，不是磁力链。
    只认其中一两处会把大量条目误判成「没有磁力链」——所以直接把整条 item
    序列化出来扫，哪儿放的都能找到。
    """
    # 先规规矩矩看 torznab:attr 元素，属性顺序之类的坑就绕开了
    for a in item.findall("torznab:attr", NS):
        if a.get("name") in ("infohash", "infoHash") and HEX40.match(a.get("value") or ""):
            return a.get("value").lower()
    blob = ET.tostring(item, encoding="unicode")
    m = re.search(r"btih:([0-9a-fA-F]{40})", blob)
    if m:
        return m.group(1).lower()
    m = re.search(r"btih:([A-Za-z2-7]{32})", blob)          # base32 形式的磁力链
    if m:
        hexed = _b32_to_hex(m.group(1))
        if len(hexed) == 40:
            return hexed
    # 没有磁力链，但 torznab:attr 里单独给了 infohash
    m = re.search(r'name="infohash"\s+value="([0-9a-fA-F]{40})"', blob)
    if m:
        return m.group(1).lower()
    return ""


def torznab_parse(xml):
    """
    解析 Torznab 响应。API Key 不对时 Jackett 返回的不是空结果，而是
    <error code="100" description="Invalid API Key"/>。不单独识别的话，
    会被当成「这个词没搜到东西」，用户排查半天找不到原因。
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        head = (xml or "")[:120].replace("\n", " ")
        raise TorznabError("返回的不是 XML（%s）。开头是：%s" % (e, head))
    if root.tag.endswith("error"):
        raise TorznabError("Jackett 报错 code=%s %s"
                           % (root.get("code"), root.get("description")))
    err = root.find(".//error")
    if err is not None:
        raise TorznabError("Jackett 报错 code=%s %s"
                           % (err.get("code"), err.get("description")))
    return root


def jackett_root(endpoint):
    """从 torznab 地址反推 Jackett 根地址和索引站名。"""
    url = endpoint.partition("|")[0]
    m = re.match(r"(https?://[^/]+)/api/v2\.0/indexers/([^/]+)/results/torznab/api", url)
    return (m.group(1), m.group(2)) if m else (None, None)


def list_indexers(endpoint):
    """
    列出 Jackett 里已配置的索引站。返回 (清单, 失败原因列表)。

    不能用 /api/v2.0/indexers?configured=true —— 那个管理接口只认登录 cookie，
    API Key 对它无效（Jackett issue #16324 说明了这点，我一开始就踩了这个坑）。
    能用 API Key 的是 torznab 自己的 t=indexers 功能。
    """
    problems = []

    # 正路：torznab 的 t=indexers
    try:
        root = torznab_parse(http_text(torznab_url(endpoint, t="indexers",
                                                   configured="true"), retries=1))
        out = []
        for el in root.iter():
            if el.tag.split("}")[-1] != "indexer" or not el.get("id"):
                continue
            title = el.get("id")
            for ch in el:
                if ch.tag.split("}")[-1] == "title" and (ch.text or "").strip():
                    title = ch.text.strip()
            out.append((el.get("id"), title))
        if out:
            return out, problems
        problems.append("t=indexers 返回了 0 个索引站 —— Jackett 里可能还没添加任何站点")
    except Exception as e:
        problems.append("t=indexers 失败：%s" % e)

    # 退路：管理接口。只有在 Jackett 没设管理密码时才可能通
    root_url, _ = jackett_root(endpoint)
    if root_url:
        key = endpoint.partition("|")[2]
        url = root_url + "/api/v2.0/indexers?configured=true" + (("&apikey=" + key) if key else "")
        try:
            data = http_json(url, retries=1)
            out = [(d["id"], d.get("name") or d["id"])
                   for d in (data if isinstance(data, list) else [])
                   if isinstance(d, dict) and d.get("id")]
            if out:
                return out, problems
        except Exception as e:
            problems.append("管理接口也不行：%s（它只认登录 cookie，这是正常的）" % e)
    return [], problems


def indexer_endpoint(endpoint, indexer_id):
    root, _ = jackett_root(endpoint)
    key = endpoint.partition("|")[2]
    return "%s/api/v2.0/indexers/%s/results/torznab/api|%s" % (root, indexer_id, key)


def torznab_url(endpoint, **params):
    url, _, key = endpoint.partition("|")
    if key:
        params["apikey"] = key
    return url + "?" + urllib.parse.urlencode(params)


# 默认扫描词。纯 a-z 对中文站几乎无效，所以数字和常见中文字也带上。
DEFAULT_TERMS = ([""] + list("0123456789") + list("abcdefghijklmnopqrstuvwxyz")
                 + list("的一是不了人我在有电影剧集动画国语中字全集第季"))


def cmd_torznab(args, writer):
    url = args.endpoint.partition("|")[0]
    if args.query:
        queries = [args.query]
    elif args.terms:
        queries = [t.strip() for t in args.terms.split(",")]
    else:
        queries = DEFAULT_TERMS

    fetch_err = {}
    seen = [0]          # 站点一共返回了多少条
    no_ih = [0]         # 其中拿不到 infohash 的有多少条
    print("从 Torznab 导入：%s" % url)
    if len(queries) > 1:
        print("将按 %d 组词扫，每组最多翻 %d 页。这是真的去站点搜，"
              "慢是正常的，中途 Ctrl-C 已导入的会保留。" % (len(queries), args.pages))
    print("-" * 66)

    for q in queries:
        n = 0
        q_seen = 0          # 本轮这个词返回了多少条（seen 是全程累计，不能混用）
        for page in range(args.pages):
            try:
                root = torznab_parse(http_text(
                    torznab_url(args.endpoint, t="search", q=q,
                                limit=str(args.page_size),
                                offset=str(page * args.page_size)), retries=1))
            except TorznabError as e:
                print("  [%s] %s" % (q or "(最新)", e))
                return          # 地址或 Key 不对，继续扫没意义
            except Exception as e:
                print("  [%s] 第 %d 页失败：%s" % (q or "(最新)", page + 1, str(e)[:50]))
                break

            items = list(root.iter("item"))
            if not items:
                break

            seen[0] += len(items)
            q_seen += len(items)
            for item in items:
                attrs = {x.get("name"): x.get("value")
                         for x in item.findall("torznab:attr", NS)}
                ih = item_infohash(item)
                name = (item.findtext("title") or "").strip()
                size = _size(item.findtext("size") or attrs.get("size"))
                files, nfiles = (), 1

                if not ih and args.fetch_torrents:
                    url_dl = item_download_url(item)
                    if url_dl:
                        try:
                            kind, payload = resolve_download(url_dl)
                            if kind == "magnet":
                                m = re.search(r"btih:([0-9a-fA-F]{40})", payload)
                                ih = m.group(1).lower() if m else ""
                                if not ih:
                                    m = re.search(r"btih:([A-Za-z2-7]{32})", payload)
                                    ih = _b32_to_hex(m.group(1)) if m else ""
                                if not ih:
                                    fetch_err["磁力链里没有 40 位 infohash"] = \
                                        fetch_err.get("磁力链里没有 40 位 infohash", 0) + 1
                            else:
                                parsed = parse_torrent_bytes(payload)
                                if parsed:
                                    ih, tname, tsize, files = parsed
                                    name = tname or name
                                    size = tsize or size
                                    nfiles = len(files) or 1
                                else:
                                    fetch_err["下回来的不是合法种子"] = \
                                        fetch_err.get("下回来的不是合法种子", 0) + 1
                        except Exception as e:
                            why = str(e)[:60]
                            fetch_err[why] = fetch_err.get(why, 0) + 1
                if not ih:
                    # 这一条没有 infohash，存不进来。以前是静默 continue，
                    # 结果就是「新增 0」而没有任何线索——最难查的那类问题。
                    no_ih[0] += 1
                    continue
                if writer.add(ih, name, size, nfiles, files[:60], source="torznab"):
                    n += 1
                # 部分索引站会给封面地址（Torznab 规范里的 coverurl）。
                # 只记地址，不取图。
                cover = (attrs.get("coverurl") or attrs.get("poster")
                         or attrs.get("banner") or "")
                if cover:
                    writer.set_cover(ih, cover)
                if writer.new >= args.limit:
                    print("  [%s] 新增 %d（已达 --limit）" % (q or "(最新)", n))
                    return

            if len(items) < args.page_size:
                break           # 这一页没满，说明到底了
            time.sleep(args.delay)

        if q_seen or n or not args.quiet:
            print("  [%s] 站点返回 %d 条，新增 %d" % (q or "(最新)", q_seen, n))
        time.sleep(args.delay)

    # 为什么一条都没进来，必须说清楚，别让人对着「新增 0」猜
    if seen[0] == 0:
        print("\n站点一条结果都没返回。两种可能，先分清是哪种：")
        if args.query:
            print("  1) 这个词在这个站上确实没有 —— 把关键词留空再跑一次，"
                  "会拉这个站的最新条目。\n"
                  "     能拉到，就说明站是好的，只是词不对路"
                  "（动漫站搜欧美片名、影视站搜番名，都搜不到）。")
            print("  2) 站点当前不可用 —— 留空也拉不到的话，测一遍：\n"
                  "     %s btimport.py probe torznab \"地址|APIKEY\" --each" % py_cmd())
        else:
            print("  关键词已经留空了还是没有，说明这个站当前不可用。测一遍：\n"
                  "  %s btimport.py probe torznab \"地址|APIKEY\" --each" % py_cmd())
    elif no_ih[0]:
        print("\n站点返回了 %d 条，其中 %d 条拿不到 infohash，存不进索引。"
              % (seen[0], no_ih[0]))
        if not args.fetch_torrents:
            print("  这个站只给 .torrent 下载地址，不给磁力链。"
                  "加上这个开关就能把种子取下来自己算 infohash：\n"
                  "    命令行：--fetch-torrents\n"
                  "    网页上：勾选「下载种子算 infohash」")
        else:
            print("  已经开了 --fetch-torrents 但还是取不到，看下面的失败原因。")

    if fetch_err:
        print("\n抓取种子/磁力链失败的原因：")
        for why, cnt in sorted(fetch_err.items(), key=lambda kv: -kv[1])[:6]:
            print("  %5d 次  %s" % (cnt, why))


# --------------------------------------------------------------------------
# 来源四：本地 .torrent 文件夹
# --------------------------------------------------------------------------

def cmd_folder(args, writer):
    paths = []
    for root, _, names in os.walk(args.path):
        paths.extend(os.path.join(root, n) for n in names
                     if n.lower().endswith(".torrent"))
    print("在 %s 下找到 %d 个 .torrent" % (args.path, len(paths)))
    print("-" * 66)
    broken = []
    for p in paths:
        try:
            with open(p, "rb") as fp:
                data = fp.read()
        except OSError:
            writer.bad += 1
            continue
        ih = torrent_infohash(data)
        if not ih:
            writer.bad += 1
            continue
        name, size, files = os.path.basename(p)[:-8], 0, []
        try:
            # infohash 靠定位 4:info 直接切字节，即使整体 bencode 有瑕疵也算得出；
            # 但名字和文件列表必须完整解析才有，解析失败要说出来，
            # 不然落库的是个没名字没大小的空壳条目，搜都搜不到。
            info = (_bdecode(data, 0)[0] or {}).get(b"info") or {}
            raw_name = info.get(b"name.utf-8") or info.get(b"name") or b""
            parsed = raw_name.decode("utf-8", "replace")
            if not parsed:
                raise ValueError("info 里没有 name")
            name = parsed
            if isinstance(info.get(b"files"), list):
                for f in info[b"files"]:
                    parts = f.get(b"path.utf-8") or f.get(b"path") or []
                    files.append("/".join(x.decode("utf-8", "replace") for x in parts))
                    size += int(f.get(b"length") or 0)
            else:
                size = int(info.get(b"length") or 0)
                files = [name]
        except Exception as e:
            broken.append((os.path.basename(p), str(e)[:50]))
        writer.add(ih, name, size, len(files) or 1, files[:60], source="folder")

    if broken:
        print("\n有 %d 个文件只算出了 infohash，名字和文件列表没解析出来：" % len(broken))
        for fn, why in broken[:5]:
            print("  %-40s %s" % (fn[:40], why))
        print("  这类条目入库了但搜不到名字，建议确认文件是否完整。")


# --------------------------------------------------------------------------
# probe：先探一下，确认对方接口没改
# --------------------------------------------------------------------------

def probe_torznab(endpoint):
    url = endpoint.partition("|")[0]
    has_key = bool(endpoint.partition("|")[2])
    print("\n[torznab] %s" % url)
    if not has_key:
        print("  没给 API Key。格式是 \"地址|你的APIKEY\"，注意竖线两边别有空格。")
    try:
        # 先问 caps：最轻的一次调用，能同时验证地址对不对、Key 对不对
        root = torznab_parse(http_text(torznab_url(endpoint, t="caps")))
    except TorznabError as e:
        print("  %s" % e)
        if "100" in str(e) or "Key" in str(e):
            print("  API Key 不对。到 Jackett 页面顶部把 API Key 重新复制一遍。")
        return False
    except Exception as e:
        print("  连不上：%s" % e)
        print("  确认 Jackett 正在运行，地址和端口对不对（默认 9117）。")
        return False

    cats = [c.get("name") for c in root.iter("category") if c.get("name")]
    print("  连上了。支持的分类 %d 个：%s" % (len(cats), "、".join(cats[:6])))
    try:
        root2 = torznab_parse(http_text(torznab_url(endpoint, t="search",
                                                    q="ubuntu", limit="20"), retries=1))
        items = list(root2.iter("item"))
        withih = sum(1 for it in items if item_infohash(it))
        print("  试搜 ubuntu：%d 条结果，其中 %d 条带得到 infohash" % (len(items), withih))
        if items and not withih and DUMP[0]:
            print("\n  ---- 第一条 item 的原始内容（把这段发我就能定位）----")
            print(ET.tostring(items[0], encoding="unicode")[:1500])
            print("  ---- 结束 ----")
        if items and not withih:
            print("  能搜到但都没有磁力链 —— 这些索引站只给 .torrent 下载地址，"
                  "存不进库。换几个支持磁力链的站点。")
            return False
        if not items:
            print("  搜不到结果。多半是 Jackett 里还没添加任何索引站，"
                  "或者添加的站点当前不可用。")
            return False
    except Exception as e:
        print("  caps 通了但搜索失败：%s" % e)
        if "timed out" in str(e) or "timeout" in str(e).lower():
            print("""
  caps 能通、search 超时，这两步性质完全不同：
    caps   是 Jackett 本地回答的，不出网，所以永远很快。
    search 是 Jackett 真的去各个索引站抓，站点连不上就一直挂着。

  用 indexers/all 时它要等所有站点都返回，一个慢的就拖垮整批。三个方向：

    1) 单独测一个站，看是不是只有某几个站有问题：
         %s btimport.py probe torznab "地址|KEY" --each
    2) 加大超时（Jackett 聚合搜索几十秒很常见）：
         --timeout 180
    3) 如果所有站点都超时，是 Jackett 出不了网。
       注意：本工具的 --proxy 在这里没用，我们连的是 127.0.0.1。
       要在 Jackett 自己的设置里配代理：打开 http://127.0.0.1:9117，
       点右上角齿轮，找 Proxy Type / Proxy URL / Proxy Port 填上你的本地代理，
       保存后 Jackett 会重启。""" % py_cmd())
        return False
    print("  可以正式导入了。")
    return True


def probe_query(endpoint, q, limit="20"):
    return list(torznab_parse(http_text(
        torznab_url(endpoint, t="search", q=q, limit=limit), retries=1)).iter("item"))


def probe_each(endpoint, timeout_each=None, only=None):
    """
    逐个测已配置的索引站，分三类给结论。

    探测词用空串而不是 "ubuntu"：空查询让站点返回它自己的最新条目，
    对动漫站、中文站一样有效。用 ubuntu 去问一个动漫站，它老老实实返回 0 条，
    会被误判成「不通」——实际上人家连得好好的。
    """
    if timeout_each is None:
        timeout_each = TIMEOUT if TIMEOUT != 30 else 45

    manual = bool(only)
    if manual:
        idx = [(x.strip(), x.strip()) for x in only.split(",") if x.strip()]
    else:
        idx, problems = list_indexers(endpoint)
        if not idx:
            print("\n拿不到索引站清单：")
            for p in problems:
                print("  · %s" % p)
            print("\n不影响使用，直接手工指定就行 —— 站点 id 在 Jackett 页面每个条目"
                  "左边就能看到（也可以点 Copy Torznab Feed，地址里 /indexers/ 后面那段）：")
            print("  %s btimport.py probe torznab \"地址|APIKEY\" --each "
                  "--indexers 1337x,nyaasi,torrentgalaxy" % py_cmd())
            return False

    print("\n%s %d 个索引站，逐个测（每个最多等 %d 秒）："
          % ("按你指定的" if manual else "Jackett 里已配置", len(idx), timeout_each))
    print("-" * 70)
    old = TIMEOUT
    set_timeout(timeout_each)
    good, fetchable, empty, dead, dumps = [], [], [], [], []
    try:
        for iid, name in idx:
            ep = indexer_endpoint(endpoint, iid)
            t0 = time.time()
            try:
                items = probe_query(ep, "")          # 空查询 = 要它的最新条目
                if not items:
                    items = probe_query(ep, "2024")  # 有些站不接受空查询，换个通用词
                withih = sum(1 for it in items if item_infohash(it))
                dl = sum(1 for it in items if item_download_url(it))
                if withih:
                    good.append(iid)
                    mark = "%2d 条，%2d 条有磁力链  直接可用" % (len(items), withih)
                elif dl:
                    fetchable.append(iid)
                    mark = "%2d 条，只给种子地址    需 --fetch-torrents" % len(items)
                    if DUMP[0] and len(dumps) < 2:
                        dumps.append((iid, ET.tostring(items[0], encoding="unicode")[:900]))
                else:
                    empty.append(iid)
                    mark = "连得上，但没返回条目"
            except TorznabError as e:
                dead.append(iid)
                mark = str(e)[:44]
            except Exception as e:
                dead.append(iid)
                mark = ("超时" if "timed out" in str(e) else str(e)[:44])
            print("  %-14s %-46s %4.0f 秒" % (iid[:14], mark, time.time() - t0))
    finally:
        set_timeout(old)
    print("-" * 70)
    for iid, raw in dumps:
        print("\n---- %s 第一条 item（供核对）----\n%s\n---- 结束 ----" % (iid, raw))

    root_url, _ = jackett_root(endpoint)
    def cmd_for(ids, extra=""):
        return ('  %s btimport.py torznab "%s/api/v2.0/indexers/%s/results/torznab/api'
                '|你的APIKEY" --query 关键词%s' % (py_cmd(), root_url, ids[0], extra))

    if good:
        print("\n直接可用 %d 个：%s" % (len(good), " ".join(good)))
        print(cmd_for(good))
    if fetchable:
        print("\n只给 .torrent 的 %d 个：%s" % (len(fetchable), " ".join(fetchable)))
        print("  它们不给磁力链，但加一个开关就能把种子下下来自己算 infohash，"
              "拿到的文件列表反而更全：")
        print(cmd_for(fetchable, " --fetch-torrents"))
    if empty:
        print("\n连得上但没返回条目 %d 个：%s" % (len(empty), " ".join(empty)))
        print("  站点本身是通的，只是这个探测词没命中。换成它擅长的词试试，"
              "比如动漫站用番名。")
    if dead:
        print("\n不通 %d 个：%s" % (len(dead), " ".join(dead)))
        print("  Jackett 连不上这些站。去 Jackett 右上角 Logs 看具体报错；"
              "如果是网络到不了，在设置里给 Jackett 配代理。")
    return bool(good or fetchable)


def probe_one(src):
    """返回 True 表示这个源通。"""
    print("\n[%s]" % src)
    try:
        if src == "ia":
            items = list(ia_identifiers("", 3))
            print("scrape 接口返回 %d 个条目：" % len(items))
            for it in items:
                print("   ", it.get("identifier"), "|", str(it.get("title"))[:40])
            if items:
                d = ia_item_detail(items[0]["identifier"])
                print("\n第一条的详情：")
                if d:
                    print("    infohash :", d["infohash"])
                    print("    名称     :", d["name"][:60])
                    print("    大小     :", human(d["size"]), "/", d["nfiles"], "个文件")
                    print("\n接口正常，可以正式导入了。")
                else:
                    print("    取不到 btih —— 接口结构可能变了，把这段输出发我")
        elif src == "academic":
            xml = http_text(AT_DB, retries=1)
            items = list(at_items(xml))
            print("  全库清单 %.1f MB，解析出 %s 条：" 
                  % (len(xml.encode("utf-8")) / 1048576, format(len(items), ",")))
            for it in items[:3]:
                print("     %s | %s | %s" % (it["infohash"][:12], human(it["size"]),
                                             it["name"][:44]))
            if items:
                print("\n  接口正常，可以正式导入了。")
        else:
            print("  probe 目前支持 ia 和 academic")
    except Exception as e:
        print("  探测失败：%s" % e)
        print("  " + net_hint(e).replace("\n", "\n  "))
        return False
    return True


def cmd_probe(args, writer=None):
    print("探测外部资源库（构建 %s）" % BUILD)
    print("=" * 66)
    sys_proxy = urllib.request.getproxies()
    if args.proxy:
        print("走指定代理：%s" % args.proxy)
    elif sys_proxy:
        print("检测到系统代理：%s"
              % ", ".join("%s=%s" % kv for kv in sorted(sys_proxy.items())))
        print("（urllib 会自动用它。如果没生效，用 --proxy 显式指定）")
    else:
        print("没有代理，直连。")

    if args.source == "torznab":
        if not args.endpoint:
            sys.exit('用法：btimport.py probe torznab "http://127.0.0.1:9117'
                     '/api/v2.0/indexers/all/results/torznab/api|你的APIKEY"')
        ok = [probe_each(args.endpoint, only=args.indexers) if args.each
              else probe_torznab(args.endpoint)]
    else:
        targets = ["ia", "academic"] if args.source == "all" else [args.source]
        ok = [probe_one(s) for s in targets]
    print("\n" + "=" * 66)
    if all(ok):
        print("都通了，可以正式导入。")
        return 0
    print("有源不通。注意各源互相独立——档案馆不通不影响用 Academic Torrents、"
          "Jackett 或本地 folder 导入。")
    return 1


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description="把外部资源库批量导入本地索引",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python3 btimport.py probe ia                        先确认接口正常
  python3 btimport.py ia --limit 5000                 拉 5000 条
  python3 btimport.py ia --query "mediatype:movies" --limit 20000
  python3 btimport.py academic
  python3 btimport.py torznab "http://127.0.0.1:9117/api/v2.0/indexers/all/results/torznab/api|APIKEY"
  python3 btimport.py folder D:\\torrents

导进来的条目和自己爬的存在同一个库里，用 source 字段区分，
btindex.py stats 能看到各来源各多少条。
""")
    ap.add_argument("--db", default=DB_DEFAULT, help="索引路径（默认 %s）" % DB_DEFAULT)
    ap.add_argument("--limit", type=int, default=5000, help="最多导入多少条")
    ap.add_argument("--workers", type=int, default=8,
                    help="并发数（默认 8，别调太高，对方是公益服务器）")
    ap.add_argument("--delay", type=float, default=1.0, help="每批之间歇多久")
    ap.add_argument("--timeout", type=int, default=30,
                    help="单次请求超时秒数（默认 30）。Jackett 聚合搜索很慢，"
                         "用 torznab 时建议 120 以上")
    ap.add_argument("--proxy", default="",
                    help="走本地代理，如 http://127.0.0.1:7890。"
                         "不给就用系统代理设置")
    ap.add_argument("-q", "--quiet", action="store_true", help="不逐条打印")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_proxy(parser):
        """
        这些选项写在子命令前面或后面都该管用。
        argparse 默认只认前面，而人会自然地写在后面——
        default=SUPPRESS 是关键：不给时子命令不写这个属性，
        顶层传进来的值才不会被子命令的默认值冲掉。
        """
        parser.add_argument("--db", default=argparse.SUPPRESS, help="索引路径")
        parser.add_argument("--limit", type=int, default=argparse.SUPPRESS,
                            help="最多导入多少条")
        parser.add_argument("--workers", type=int, default=argparse.SUPPRESS,
                            help="并发数")
        parser.add_argument("--delay", type=float, default=argparse.SUPPRESS,
                            help="每批之间歇多久")
        parser.add_argument("--timeout", type=int, default=argparse.SUPPRESS,
                            help="单次请求超时秒数")
        parser.add_argument("--proxy", default=argparse.SUPPRESS,
                            help="走本地代理，如 http://127.0.0.1:7890")
        parser.add_argument("-q", "--quiet", action="store_true",
                            default=argparse.SUPPRESS, help="不逐条打印")

    p = sub.add_parser("ia", help="互联网档案馆")
    p.add_argument("--query", default="", help="Lucene 语法，如 mediatype:movies")
    add_proxy(p)
    p.set_defaults(func=cmd_ia)

    p = sub.add_parser("academic", help="Academic Torrents 科研数据集")
    p.add_argument("--query", default="", help="只导这个关键词，不给就用一组默认词")
    add_proxy(p)
    p.set_defaults(func=cmd_academic)

    p = sub.add_parser("torznab", help="你自己的 Jackett / Prowlarr")
    p.add_argument("endpoint", help="URL|APIKEY")
    p.add_argument("--query", default="", help="只导这个关键词，不给就按 a-z 扫一遍")
    p.add_argument("--page-size", type=int, default=100, help="每页取多少条")
    p.add_argument("--pages", type=int, default=5,
                   help="每个词最多翻几页（默认 5）。想尽量抓全就调大")
    p.add_argument("--terms", default="",
                   help="自定义扫描词，逗号分隔。不给就用内置的一组"
                        "（数字 + 字母 + 常见中文字）")
    p.add_argument("--fetch-torrents", action="store_true",
                   help="没有磁力链时，把 .torrent 下下来自己算 infohash。"
                        "慢一些，但能救回只给种子下载地址的站点")
    add_proxy(p)
    p.set_defaults(func=cmd_torznab)

    p = sub.add_parser("folder", help="本地的一堆 .torrent 文件")
    p.add_argument("path")
    add_proxy(p)
    p.set_defaults(func=cmd_folder)

    p = sub.add_parser("probe", help="探一下接口有没有变")
    p.add_argument("source", nargs="?", default="all",
                   choices=["all", "ia", "academic", "torznab"])
    p.add_argument("endpoint", nargs="?", default="",
                   help='probe torznab 时给 "地址|APIKEY"')
    p.add_argument("--each", action="store_true",
                   help="逐个测 Jackett 里每个索引站，看哪些在你的网络里能用")
    p.add_argument("--indexers", default="",
                   help="配合 --each，手工指定要测哪些站，逗号分隔，如 1337x,nyaasi")
    p.add_argument("--no-dump", action="store_true",
                   help="取不到 infohash 时不打印原始 XML")
    add_proxy(p)
    p.set_defaults(func=cmd_probe)

    if ap.epilog:
        ap.epilog = ap.epilog.replace("python3 ", py_cmd() + " ")
    args = ap.parse_args()
    for name, default in (("db", DB_DEFAULT), ("limit", 5000), ("workers", 8),
                          ("delay", 1.0), ("timeout", 30), ("proxy", ""),
                          ("quiet", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    set_proxy(args.proxy)
    set_timeout(args.timeout)

    # 每次都报一下代理状态。不报的话，失败时根本分不清是
    # 「代理没生效」还是「代理生效了但站点还是不通」
    if args.cmd != "probe":
        if args.proxy:
            print("代理：%s" % args.proxy)
        else:
            sysp = urllib.request.getproxies()
            if sysp:
                print("代理：未指定 --proxy，将使用系统设置 %s"
                      % ", ".join("%s=%s" % kv for kv in sorted(sysp.items())))
            else:
                print("代理：无，直连")
    DUMP[0] = not getattr(args, "no_dump", False)

    if args.cmd == "probe":
        sys.exit(cmd_probe(args))

    writer = Writer(args.db, quiet=args.quiet)
    try:
        args.func(args, writer)
    except KeyboardInterrupt:
        print("\n手动中断，已导入的会保留。")
    finally:
        writer.close()


if __name__ == "__main__":
    main()
