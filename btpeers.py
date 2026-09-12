#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btpeers —— 用 DHT 查一个种子此刻到底有多少人在传

前面 btprune 判断死活靠的是 last_seen，那只是推测：你的节点只覆盖 DHT 键空间的一小片，
种子活得好好的但没往你这片 announce，看起来就跟死了一样。
这个工具把推测换成事实——直接去问 DHT。

做法是 Kademlia 的迭代查找：
  1. 从入口节点起步，向「离目标 infohash 最近」的一批节点发 get_peers；
  2. 对方要么回 values（真正的 peer 地址，这就是我们要的），
     要么回 nodes（它认识的、离目标更近的节点）；
  3. 把新节点按异或距离排进候选队列，继续问更近的；
  4. 越问越近，直到问不出更近的节点或者超时。

三个必须说清楚的局限：

  * values 给的是 peer，不区分做种者和下载者。想分清得挨个连上去看它的 bitfield，
    --verify 就是干这个的，但那是抽样估算，不是精确值。
  * DHT 只知道「通过 DHT 宣告过」的 peer。私有种子、只挂 tracker 的、
    靠 PEX 互相发现的，这里都看不到。所以查出来的数是下限，不是全量。
  * 查到 0 个 peer 只能说明此刻 DHT 里没人宣告，不等于种子文件不存在。
    但连续几次都是 0，基本可以判死。

好消息：这个工具在 NAT 后面也能正常用。它是主动发起查询，
回包顺着 NAT 映射就回来了，不像嗅探器那样需要公网可达的端口。

用法：
    python3 btpeers.py check c7e8c989...b07              # 查单个
    python3 btpeers.py check <magnet链> --verify          # 顺便估算做种者比例
    python3 btpeers.py scan --db bt.db --limit 500        # 批量查并写回索引
    然后就能用真实数据清理了：
    python3 btprune.py prune --max-peers 0 --checked

依赖：无，标准库足够。
"""

import argparse
import os
import random
import re
import select
import socket
import sqlite3
import struct
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcompat import BUILD, bind_udp, py_cmd, setup_console
from dhtsniff import bencode, bdecode, decode_nodes, rand_id
from btindex import DB_DEFAULT, human

BOOTSTRAP = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
    ("dht.libtorrent.org", 25401),
]

ALPHA = 8            # 每轮同时问多少个节点。Kademlia 论文用 3，查 peer 可以激进些
K = 8                # 最终关注最近的多少个节点
MAX_QUERIES = 200    # 单次查找最多问多少个节点，防止在大网里无限扩散


def pad(text, width, right=False) -> str:
    """按终端显示宽度补空格。中文是双宽，用 len() 排版会歪。"""
    text = str(text)
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    space = " " * max(width - w, 0)
    return space + text if right else text + space


# --------------------------------------------------------------------------
# 距离与解码
# --------------------------------------------------------------------------

def distance(a: bytes, b: bytes) -> int:
    """Kademlia 的距离就是节点 ID 异或后当成大整数比大小。"""
    return int.from_bytes(bytes(x ^ y for x, y in zip(a, b)), "big")


def decode_peers(values):
    """
    紧凑 peer 信息：每 6 字节 = 4 字节 IPv4 + 2 字节端口（大端）。
    有的客户端一条字符串里塞好几个，所以按 6 字节切而不是假定一条一个。
    """
    out = set()
    if not isinstance(values, list):
        return out
    for v in values:
        if not isinstance(v, bytes) or len(v) < 6 or len(v) % 6:
            continue
        for i in range(0, len(v), 6):
            ip = socket.inet_ntoa(v[i:i + 4])
            port = struct.unpack(">H", v[i + 4:i + 6])[0]
            if port and not ip.startswith(("0.", "127.")):
                out.add((ip, port))
    return out


def parse_infohash(text: str) -> bytes:
    text = (text or "").strip()
    if text.startswith("magnet:"):
        m = re.search(r"btih:([0-9a-fA-F]{40})", text)
        if not m:
            raise ValueError("磁力链里没找到 40 位十六进制的 infohash")
        text = m.group(1)
    if not re.fullmatch(r"[0-9a-fA-F]{40}", text):
        raise ValueError("infohash 应该是 40 位十六进制")
    return bytes.fromhex(text)


# --------------------------------------------------------------------------
# DHT 迭代查找
# --------------------------------------------------------------------------

_boot_cache = []
_boot_lock = threading.Lock()


def bootstrap_addrs():
    """入口节点解析一次就够，批量扫描时别每条都去查 DNS。"""
    with _boot_lock:
        if not _boot_cache:
            for host, port in BOOTSTRAP:
                try:
                    _boot_cache.append((socket.gethostbyname(host), port))
                except (socket.gaierror, OSError):
                    pass
        return list(_boot_cache)


# 预热出来的真实 DHT 节点，全局共用。
# 批量扫描时每次都从零预热太浪费，第一次捞到之后后面直接复用。
_hot_nodes = []
_hot_lock = threading.Lock()


def _remember(nodes):
    with _hot_lock:
        known = set(_hot_nodes)
        for a in nodes:
            if a not in known:
                _hot_nodes.append(a)
        del _hot_nodes[:-400]          # 只留最近 400 个，别无限长


def warmup(timeout=4.0, want=32, bind_port=0):
    """
    先用 find_node 把真实节点捞出来，再拿它们做 get_peers 的起点。

    这一步不能省：入口路由器（router.bittorrent.com 这些）只负责引导，
    它们基本只回应 find_node，你直接对它们发 get_peers 多半没有任何回音——
    查找会在一两秒内因为「没有更多节点可问」而空手退出，看起来就像种子死了。
    """
    with _hot_lock:
        if len(_hot_nodes) >= want:
            return list(_hot_nodes[-want:])

    sock = bind_udp(bind_port, rcvbuf=0)
    found = []
    try:
        sock.settimeout(0.4)
        me = rand_id()
        targets = list(bootstrap_addrs()) + list(_hot_nodes[-16:])
        for addr in targets:
            try:
                sock.sendto(bencode({"t": os.urandom(2), "y": "q", "q": "find_node",
                                     "a": {"id": me, "target": rand_id()}}), addr)
            except OSError:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline and len(found) < want * 3:
            ready, _, _ = select.select([sock], [], [], 0.3)
            if not ready:
                continue
            try:
                data, _ = sock.recvfrom(8192)
                msg = bdecode(data)
            except Exception:
                continue
            if not isinstance(msg, dict) or msg.get(b"y") != b"r":
                continue
            r = msg.get(b"r") or {}
            new = [(ip, port) for _, ip, port in decode_nodes(r.get(b"nodes", b""))]
            # 顺着新节点再问一轮，两跳就足够把节点数撑起来
            for addr in new[:8]:
                try:
                    sock.sendto(bencode({"t": os.urandom(2), "y": "q", "q": "find_node",
                                         "a": {"id": me, "target": rand_id()}}), addr)
                except OSError:
                    pass
            found.extend(new)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    _remember(found)
    with _hot_lock:
        return list(_hot_nodes[-want:]) or found


class PeerLookup:
    """一次查找用一个自己的 UDP socket，这样批量并发时回包不会串。"""

    def __init__(self, timeout=12.0, alpha=ALPHA, max_queries=MAX_QUERIES,
                 seeds=None, bind_port=0):
        self.nid = rand_id()
        self.seeds = list(seeds) if seeds else None
        self.bind_port = bind_port
        self.warm = 0            # 预热拿到几个节点，排查时很关键
        self.timeout = timeout
        self.alpha = alpha
        self.max_queries = max_queries

    def run(self, infohash: bytes):
        t0 = time.time()
        sock = bind_udp(self.bind_port, rcvbuf=0)
        try:
            res = self._loop(sock, infohash)
            res["warm"] = self.warm
            res["elapsed"] = round(time.time() - t0, 1)   # 含预热的真实耗时
            return res
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _loop(self, sock, target):
        # addr -> [距离, 是否问过]
        cand = {}
        seeds = self.seeds or warmup(bind_port=self.bind_port)
        self.warm = len(seeds)
        for addr in seeds:
            cand[addr] = [1 << 200, False]      # 起点距离未知，排最后
        if not cand:                            # 预热完全失败才退回入口路由器
            for addr in bootstrap_addrs():
                cand[addr] = [1 << 200, False]

        peers = set()
        pending = {}                            # 事务 id -> addr
        asked = 0
        deadline = time.time() + self.timeout
        nodes_seen = set()

        while time.time() < deadline:
            # 挑最近的、还没问过的一批
            todo = sorted((d, a) for a, (d, q) in cand.items() if not q)[:self.alpha]
            for _, addr in todo:
                if asked >= self.max_queries:
                    break
                cand[addr][1] = True
                tid = os.urandom(2)
                pending[tid] = addr
                try:
                    sock.sendto(bencode({
                        "t": tid, "y": "q", "q": "get_peers",
                        "a": {"id": self.nid, "info_hash": target},
                    }), addr)
                    asked += 1
                except OSError:
                    pass

            # 没有可问的了，且也没有在等的回包，说明查完了
            if not todo and not pending:
                break

            # 收一轮回包
            got_any = False
            while time.time() < deadline:
                ready, _, _ = select.select([sock], [], [], 0.35)
                if not ready:
                    break
                try:
                    data, addr = sock.recvfrom(8192)
                except OSError:
                    break
                got_any = True
                try:
                    msg = bdecode(data)
                except Exception:
                    continue
                if not isinstance(msg, dict) or msg.get(b"y") != b"r":
                    continue
                pending.pop(msg.get(b"t", b""), None)
                r = msg.get(b"r")
                if not isinstance(r, dict):
                    continue

                peers |= decode_peers(r.get(b"values"))
                for nid, ip, port in decode_nodes(r.get(b"nodes", b"")):
                    a = (ip, port)
                    if a in cand or a in nodes_seen:
                        continue
                    nodes_seen.add(a)
                    cand[a] = [distance(nid, target), False]

            if asked >= self.max_queries and not pending:
                break
            if not got_any and not todo:
                break

        _remember(list(nodes_seen))      # 这次认识的节点留给下次用，越跑越快
        return {
            "peers": peers,
            "asked": asked,
            "nodes_known": len(cand),
            "elapsed": round(self.timeout - max(deadline - time.time(), 0), 1),
        }


# --------------------------------------------------------------------------
# 区分做种者：连上去看它的 bitfield
# --------------------------------------------------------------------------

def bitfield_complete(bf: bytes) -> bool:
    """
    全 1 的 bitfield 表示对方拥有全部分片，也就是做种者。
    末字节要特殊判：分片数通常不是 8 的整数倍，末尾的填充位按规范必须是 0，
    所以末字节合法的「满」形态只有「前面若干个 1、后面全 0」这几种。
    """
    if not bf:
        return False
    if any(b != 0xFF for b in bf[:-1]):
        return False
    return bf[-1] in (0x80, 0xC0, 0xE0, 0xF0, 0xF8, 0xFC, 0xFE, 0xFF)


def peer_is_seeder(peer, infohash: bytes, timeout=6):
    """
    返回 True/False/None（None 表示没问出来）。
    这是抽样估算：对方完全可以谎报，而且连不上的 peer 只能算作未知。
    """
    from dhtmeta import PSTR, _recv_exact, _recv_message

    reserved = bytearray(8)
    reserved[5] |= 0x10          # 扩展协议
    reserved[7] |= 0x04          # fast 扩展，这样对方可能直接回 have_all，省事
    try:
        sock = socket.create_connection(peer, timeout=timeout)
    except OSError:
        return None
    try:
        sock.settimeout(timeout)
        sock.sendall(bytes([len(PSTR)]) + PSTR + bytes(reserved) + infohash
                     + b"-BP0001-" + os.urandom(12))
        resp = _recv_exact(sock, 68)
        if resp[1:20] != PSTR or resp[28:48] != infohash:
            return None
        for _ in range(12):
            msg_id, payload = _recv_message(sock)
            if msg_id == 5:                      # bitfield
                return bitfield_complete(payload)
            if msg_id == 14:                     # have_all（BEP 6）
                return True
            if msg_id == 15:                     # have_none
                return False
        return None
    except (OSError, Exception):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def estimate_seeders(peers, infohash, sample=20, workers=10, timeout=6):
    picked = random.sample(list(peers), min(sample, len(peers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda p: peer_is_seeder(p, infohash, timeout), picked))
    seed = sum(1 for r in results if r is True)
    leech = sum(1 for r in results if r is False)
    unknown = sum(1 for r in results if r is None)
    return {"sampled": len(picked), "seeders": seed,
            "leechers": leech, "unreachable": unknown}


# --------------------------------------------------------------------------
# 索引里加两列，用来存查询结果
# --------------------------------------------------------------------------

# 这两列的定义现在只有一处：btindex.ensure_columns，SCHEMA 里也带着同样的默认值。
# 以前这里自己写了一份一模一样的 ALTER，两边迟早会漂——比如这边补了索引、
# 那边没补。留这个别名是为了本文件里的调用点不用改。
from btindex import ensure_columns          # noqa: E402  （放这儿是为了紧挨着说明）


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def cmd_check(args):
    ih = parse_infohash(args.infohash)
    print("查询 %s …（构建 %s）" % (ih.hex(), BUILD))
    res = PeerLookup(timeout=args.timeout, bind_port=args.bind_port).run(ih)
    peers = res["peers"]

    print("预热拿到 %d 个起始节点，问了 %d 个，共认识 %d 个，用时 %.1f 秒"
          % (res.get("warm", 0), res["asked"], res["nodes_known"], res["elapsed"]))
    if res.get("warm", 0) <= 5:
        print("  预热几乎没拿到节点 —— 入口路由器连不上，或回包被防火墙丢了。"
              "试试 --bind-port 6882（防火墙只放行了 6881-6888）")
    print("找到 %d 个 peer" % len(peers))
    if not peers:
        print("\nDHT 里此刻没人宣告持有它。可能确实死了，"
              "也可能只是没走 DHT（私有种子、纯 tracker）。")
        return
    for ip, port in sorted(peers)[:args.show]:
        print("  %s:%d" % (ip, port))
    if len(peers) > args.show:
        print("  …… 还有 %d 个" % (len(peers) - args.show))

    if args.verify:
        print("\n抽样连接，看谁拥有全部分片…")
        est = estimate_seeders(peers, ih, sample=args.sample, timeout=args.peer_timeout)
        print("抽了 %d 个：做种 %d，下载中 %d，连不上 %d"
              % (est["sampled"], est["seeders"], est["leechers"], est["unreachable"]))
        reached = est["seeders"] + est["leechers"]
        if reached:
            ratio = est["seeders"] / reached
            print("按连得上的算，做种比例约 %.0f%%，推算全网做种者约 %d 个"
                  % (ratio * 100, round(len(peers) * ratio)))
        else:
            print("一个都没连上，估不出来。可能都在 NAT 后面。")


def cmd_scan(args):
    if not os.path.exists(args.db):
        sys.exit("找不到 %s" % args.db)
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    added = ensure_columns(conn)
    if added:
        print("已给索引补上列：%s" % "、".join(added))

    order = {"oldest": "last_seen ASC", "newest": "last_seen DESC",
             "hot": "hits DESC", "random": "RANDOM()"}[args.order]
    cutoff = int(time.time()) - args.recheck * 86400
    rows = conn.execute(
        "SELECT infohash, name FROM torrents WHERE checked_at < ? ORDER BY %s LIMIT ?"
        % order, (cutoff, args.limit)).fetchall()
    if not rows:
        print("没有需要检查的。所有种子都在 %d 天内查过了。" % args.recheck)
        return

    print("准备检查 %d 个种子，%d 个并发，每个最多 %.0f 秒"
          % (len(rows), args.workers, args.timeout))
    print("-" * 68)

    lock = threading.Lock()
    done = {"n": 0, "alive": 0, "dead": 0, "peers": 0}
    results = []

    def work(row):
        try:
            res = PeerLookup(timeout=args.timeout).run(bytes.fromhex(row["infohash"]))
            n = len(res["peers"])
        except (ValueError, OSError):
            n = -1
        with lock:
            done["n"] += 1
            if n > 0:
                done["alive"] += 1
                done["peers"] += n
            elif n == 0:
                done["dead"] += 1
            results.append((row["infohash"], n))
            mark = "%3d 人" % n if n > 0 else ("  没人" if n == 0 else "  出错")
            print("[%4d/%d] %s  %s" % (done["n"], len(rows), mark, row["name"][:44]))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, rows))

    now = int(time.time())
    with conn:
        conn.executemany(
            "UPDATE torrents SET peers=?, checked_at=? WHERE infohash=?",
            [(n, now, ih) for ih, n in results])

    print("-" * 68)
    total = done["alive"] + done["dead"]
    print("有人在传 %d 个，没人 %d 个%s"
          % (done["alive"], done["dead"],
             ("（存活率 %.0f%%）" % (done["alive"] * 100.0 / total)) if total else ""))
    if done["alive"]:
        print("活着的平均 %.1f 个 peer" % (done["peers"] / done["alive"]))
    print("\n结果已写回索引。现在可以用真实数据清理了：")
    print("  %s btprune.py --db %s prune --max-peers 0 --checked" % (py_cmd(), args.db))
    print("\n提醒：查到 0 个只说明此刻 DHT 里没人宣告。"
          "隔几天再扫一次，连续为 0 的才比较可信。")
    conn.close()


def cmd_report(args):
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    have = {r[1] for r in conn.execute("PRAGMA table_info(torrents)")}
    if "peers" not in have:
        sys.exit("索引里还没有 peers 列，先跑一次 scan。")

    total, checked = conn.execute(
        "SELECT count(*), SUM(checked_at > 0) FROM torrents").fetchone()
    print("全库 %s 条，其中 %s 条查过做种情况"
          % (format(total, ","), format(checked or 0, ",")))
    if not checked:
        return
    for label, cond in (("没人在传", "peers = 0"), ("1–5 个 peer", "peers BETWEEN 1 AND 5"),
                        ("6–50 个", "peers BETWEEN 6 AND 50"), ("50 个以上", "peers > 50")):
        n = conn.execute("SELECT count(*) FROM torrents WHERE checked_at > 0 AND %s"
                         % cond).fetchone()[0]
        print("  %s %s 条 (%.1f%%)"
              % (pad(label, 14), pad(format(n, ","), 7, right=True), n * 100.0 / checked))
    print("\n最热的几个：")
    for r in conn.execute("SELECT name, peers, size FROM torrents "
                          "WHERE checked_at > 0 ORDER BY peers DESC LIMIT 8"):
        print("  %4d 人  %s  %s"
              % (r["peers"], pad(human(r["size"]), 9, right=True), r["name"][:44]))
    conn.close()


def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description="用 DHT 查种子此刻真实的 peer 数量",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python3 btpeers.py check c7e8c989...b07 --verify
  python3 btpeers.py scan --db bt.db --limit 500 --workers 20
  python3 btpeers.py report --db bt.db
""")
    ap.add_argument("--db", default=DB_DEFAULT, help="索引路径（默认 %s）" % DB_DEFAULT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="查单个种子")
    p.add_argument("infohash", help="40 位十六进制，或直接粘磁力链")
    p.add_argument("--timeout", type=float, default=12, help="查找超时秒数")
    p.add_argument("--show", type=int, default=10, help="最多列出几个 peer 地址")
    p.add_argument("--verify", action="store_true", help="抽样连接以区分做种者和下载者")
    p.add_argument("--sample", type=int, default=20, help="抽样几个 peer")
    p.add_argument("--peer-timeout", type=float, default=6, help="单个 peer 连接超时")
    p.add_argument("--bind-port", type=int, default=0, metavar="N",
                   help="用固定的本地 UDP 端口查询。防火墙只放行了 6881-6888 时，"
                        "临时端口可能收不到回包，用 6882 试试")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("scan", help="批量查并把结果写回索引")
    p.add_argument("--limit", type=int, default=200, help="这次检查多少个")
    p.add_argument("--workers", type=int, default=16, help="并发数")
    p.add_argument("--timeout", type=float, default=12, help="单个查找超时秒数")
    p.add_argument("--recheck", type=int, default=7, help="多少天内查过的就跳过")
    p.add_argument("--order", default="oldest",
                   choices=["oldest", "newest", "hot", "random"], help="先查哪些")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("report", help="看看扫描结果的分布")
    p.set_defaults(func=cmd_report)

    if ap.epilog:
        ap.epilog = ap.epilog.replace("python3 ", py_cmd() + " ")
    args = ap.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        sys.exit("参数有问题：%s" % e)
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # 接 head/less 时对方提前关管道，正常退出即可，别吐 traceback
        try:
            sys.stdout.close()
        except OSError:
            pass
        os._exit(0)


if __name__ == "__main__":
    main()
