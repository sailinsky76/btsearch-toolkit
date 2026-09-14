#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dhtsniff —— 一个最小但能真跑的 BitTorrent DHT 嗅探器

它干的事：假装成 DHT 网络里的一个普通节点混进去，但一个字节的内容都不下载，
只是听别人路过时说的话。别人来问「谁有 XXX」（get_peers）或者宣布「我有 XXX」
（announce_peer）的时候，报文里就带着 infohash——把这些攒下来，就是一份
「此时此刻网上正在流通什么」的清单。

为什么别人会来问我们？靠两件事：
  1. 不停地向外发 find_node，让尽量多的节点把我们写进它们的路由表；
  2. 每次说话都换一个「贴着对方」的节点 ID（见 neighbor 函数）。
     DHT 按 ID 异或距离决定把查询发给谁，我们把 ID 前 10 字节抄成对方关心的目标，
     它算出来就觉得我们离目标很近，于是查询就流向我们了。

跑起来长这样：
    python3 dhtsniff.py --out hashes.txt

⚠️ 一个很现实的前提：需要一个公网能打进来的 UDP 端口。
在 NAT / 家庭宽带后面跑，只能收到我们主动联系过的那些节点的回包，
收获会差一两个数量级。云服务器上开放对应端口效果最好。

依赖：无，Python 3.7+ 标准库足够。
"""

import argparse
import os
import socket
import struct
import sys
import threading
import time
from collections import deque, OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from btcompat import bind_udp, py_cmd, setup_console


# --------------------------------------------------------------------------
# bencode —— DHT 的报文（KRPC）和 .torrent 用的是同一种编码
# --------------------------------------------------------------------------

def bencode(v) -> bytes:
    if isinstance(v, bool):                       # 必须排在 int 前面
        raise TypeError("bencode 没有布尔类型")
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, str):
        raw = v.encode()
        return b"%d:%s" % (len(raw), raw)
    if isinstance(v, (list, tuple)):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        # bencode 规定字典的键必须按字节序排列，不然对面可能不认
        items = sorted(
            ((k.encode() if isinstance(k, str) else k), val) for k, val in v.items()
        )
        return b"d" + b"".join(bencode(k) + bencode(val) for k, val in items) + b"e"
    raise TypeError("bencode 不支持 %r" % type(v))


# 嵌套多深就不认了。这个解析器是递归的，而喂给它的东西全部来自网络上的陌生人：
# `l` 重复五千次再补五千个 `e`，一个十 KB 的包就能把 Python 的递归栈顶穿，
# 抛出 RecursionError。
#
# 要紧的不是「会抛异常」，而是抛的是**哪一种**异常。各处收包循环接的是
# `except Exception`，不受影响；但 dhtmeta 里逐个试 peer 的 try_peers 接的是
# `(MetaError, OSError, ValueError)`，RecursionError 不在其中，会直接冲出那个
# 循环——于是一个恶意 peer 就能让**剩下的 peer 一个都不再试**，那个种子的元数据
# 就此放弃。DHT 上确实有人专门干投毒这行（所以这份代码里才有 polluters 那套）。
#
# 所以上限放在解析器自己身上，抛 ValueError：调用方本来就在接这个类型，
# 七个调用点一次全部覆盖，不用去改每一处的 except。
# 200 层远超真实种子的需要——info 字典最深也就三四层。
MAX_BDECODE_DEPTH = 200


def _bdecode(data: bytes, i: int, depth: int = 0):
    if depth > MAX_BDECODE_DEPTH:
        raise ValueError("bencode 嵌套太深（超过 %d 层），当畸形包丢掉"
                         % MAX_BDECODE_DEPTH)
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i, depth + 1)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i, depth + 1)
            v, i = _bdecode(data, i, depth + 1)
            out[k] = v
        return out, i + 1
    if c.isdigit():
        j = data.index(b":", i)
        n = int(data[i:j])
        return data[j + 1:j + 1 + n], j + 1 + n
    raise ValueError("bencode 解析失败，位置 %d" % i)


def bdecode(data: bytes):
    return _bdecode(data, 0)[0]


# --------------------------------------------------------------------------
# 节点 ID 与紧凑节点信息
# --------------------------------------------------------------------------

def rand_id() -> bytes:
    return os.urandom(20)


def neighbor(target: bytes, nid: bytes) -> bytes:
    """
    造一个「贴着 target」的节点 ID：前 10 字节照抄 target，后 10 字节用自己的。
    这是整个嗅探器的关键技巧——DHT 用异或算距离，前缀一样就意味着距离极近，
    对方于是把我们当成该区段的合适邻居，后续查询就更容易发到我们头上。
    """
    return target[:10] + nid[10:]


def decode_nodes(blob: bytes):
    """紧凑节点信息：每 26 字节一个 = 20 字节 ID + 4 字节 IPv4 + 2 字节端口（大端）。"""
    out = []
    if not isinstance(blob, bytes):
        return out
    for i in range(0, len(blob) - 25, 26):
        chunk = blob[i:i + 26]
        try:
            ip = socket.inet_ntoa(chunk[20:24])
        except OSError:
            continue
        port = struct.unpack(">H", chunk[24:26])[0]
        if port == 0 or ip.startswith(("0.", "127.")):
            continue
        out.append((chunk[:20], ip, port))
    return out


def encode_nodes(nodes) -> bytes:
    out = b""
    for nid, ip, port in nodes:
        try:
            out += nid[:20].ljust(20, b"\0") + socket.inet_aton(ip) + struct.pack(">H", port)
        except (OSError, struct.error):
            continue
    return out


# --------------------------------------------------------------------------
# 嗅探器主体
# --------------------------------------------------------------------------

class DHTSniffer:
    # 这几个是公开的入口节点，只用来「进门」，进去之后就靠自己滚雪球了
    BOOTSTRAP = [
        ("router.bittorrent.com", 6881),
        ("dht.transmissionbt.com", 6881),
        ("router.utorrent.com", 6881),
        ("dht.libtorrent.org", 25401),
    ]

    def __init__(self, port=6881, rate=150, max_nodes=4000,
                 seen_size=200000, on_hash=None):
        self.nid = rand_id()
        self.rate = rate                    # 每秒往外发多少个 find_node
        self.on_hash = on_hash              # 抓到新 infohash 时的回调
        self.running = True

        self.nodes = deque(maxlen=max_nodes)   # 待联系的节点队列，满了自动丢老的
        self.seen = OrderedDict()              # infohash 去重，当 LRU 用
        self.seen_size = seen_size
        self.lock = threading.Lock()
        self.stats = dict(get_peers=0, announce=0, unique=0, rx=0, tx=0, started=time.time())

        # 端口复用选项在 Windows 上语义相反，交给 btcompat 按平台处理
        self.sock = bind_udp(port)
        self.sock.settimeout(1.0)

    # ---------------- 发包 ----------------

    def send(self, msg, addr):
        try:
            self.sock.sendto(bencode(msg), addr)
            self.stats["tx"] += 1
        except (OSError, TypeError):
            pass

    def send_find_node(self, addr, target=None, nid=None):
        self.send({
            "t": os.urandom(2),
            "y": "q",
            "q": "find_node",
            "a": {"id": nid or self.nid, "target": target or rand_id()},
        }, addr)

    def bootstrap(self):
        for host, port in self.BOOTSTRAP:
            try:
                self.send_find_node((socket.gethostbyname(host), port))
            except (OSError, socket.gaierror):
                pass

    # ---------------- 主动出击的线程 ----------------

    def joiner(self):
        """
        不停向已知节点发 find_node。注意目的不是「找到谁」——
        而是让对方把我们记进路由表。被越多节点记住，别人来问我们的次数就越多。
        """
        interval = 1.0 / max(self.rate, 1)
        last_boot = 0.0
        while self.running:
            if not self.nodes:
                if time.time() - last_boot > 5:
                    self.bootstrap()
                    last_boot = time.time()
                time.sleep(1)
                continue
            try:
                nid, ip, port = self.nodes.popleft()
            except IndexError:
                continue
            # 用「贴着对方」的 ID 去问，混脸熟
            self.send_find_node((ip, port), nid=neighbor(nid, self.nid))
            time.sleep(interval)

    # ---------------- 收包处理 ----------------

    def run(self):
        threading.Thread(target=self.joiner, daemon=True).start()
        self.bootstrap()
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                continue
            self.stats["rx"] += 1
            try:
                msg = bdecode(data)
            except Exception:
                continue                    # 网上什么畸形包都有，静默丢弃
            if not isinstance(msg, dict):
                continue
            y = msg.get(b"y")
            if y == b"q":
                self.on_query(msg, addr)
            elif y == b"r":
                self.on_response(msg, addr)
            # y == b"e" 是对方报错，不用管

    def on_response(self, msg, addr):
        """别人回给我们的 find_node 结果，里面是一批新节点，扔进队列继续滚。"""
        r = msg.get(b"r")
        if not isinstance(r, dict):
            return
        for node in decode_nodes(r.get(b"nodes", b"")):
            self.nodes.append(node)

    def on_query(self, msg, addr):
        """别人来问我们——真正的收获都在这儿。"""
        args = msg.get(b"a")
        if not isinstance(args, dict):
            return
        q = msg.get(b"q")
        tid = msg.get(b"t", b"")
        their_id = args.get(b"id", b"") or rand_id()

        if q == b"get_peers":
            # 「谁有这个种子？」——说明有人正在找它
            ih = args.get(b"info_hash", b"")
            if isinstance(ih, bytes) and len(ih) == 20:
                self.record(ih, "get_peers", addr)
                # 回复：我们没有 peer，但给你几个别的节点 + 一个 token。
                # 这是正常节点的标准行为，对方因此愿意继续把我们留在路由表里。
                self.send({"t": tid, "y": "r", "r": {
                    "id": neighbor(ih, self.nid),
                    "nodes": encode_nodes(list(self.nodes)[:8]),
                    "token": ih[:4],
                }}, addr)

        elif q == b"announce_peer":
            # 「我这儿有这个种子，端口是 XXX」——比 get_peers 更硬的证据
            ih = args.get(b"info_hash", b"")
            if isinstance(ih, bytes) and len(ih) == 20:
                port = addr[1] if args.get(b"implied_port") else (args.get(b"port") or addr[1])
                self.record(ih, "announce_peer", (addr[0], port))
            self.send({"t": tid, "y": "r",
                       "r": {"id": neighbor(their_id, self.nid)}}, addr)

        elif q == b"find_node":
            self.send({"t": tid, "y": "r", "r": {
                "id": neighbor(their_id, self.nid),
                "nodes": encode_nodes(list(self.nodes)[:8]),
            }}, addr)

        elif q == b"ping":
            self.send({"t": tid, "y": "r",
                       "r": {"id": neighbor(their_id, self.nid)}}, addr)

    # ---------------- 记录 ----------------

    def record(self, ih: bytes, kind: str, peer):
        h = ih.hex()
        with self.lock:
            self.stats["get_peers" if kind == "get_peers" else "announce"] += 1
            if h in self.seen:
                self.seen.move_to_end(h)
                return
            self.seen[h] = True
            while len(self.seen) > self.seen_size:
                self.seen.popitem(last=False)
            self.stats["unique"] += 1
        if self.on_hash:
            self.on_hash(h, kind, peer)

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description="BitTorrent DHT 嗅探器：被动收集网络上流通的 infohash",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""说明：
  * 需要公网可达的 UDP 端口，NAT 后面收获会少很多。
  * get_peers 表示「有人在找」，announce_peer 表示「有人确实持有」，后者更可信但少得多。
  * 输出的是 infohash，还不知道种子叫什么——那要再走一步 BEP 9 拿元数据。
""")
    ap.add_argument("-p", "--port", type=int, default=6881, help="监听的 UDP 端口（默认 6881）")
    ap.add_argument("-o", "--out", default="", help="把磁力链追加写到这个文件")
    ap.add_argument("-r", "--rate", type=int, default=150, help="每秒发出的 find_node 数量（默认 150）")
    ap.add_argument("-t", "--seconds", type=int, default=0, help="跑多少秒后自动停，0 表示一直跑")
    ap.add_argument("--announce-only", action="store_true",
                    help="只记录 announce_peer（噪音小很多，但速度慢）")
    ap.add_argument("-q", "--quiet", action="store_true", help="不逐条打印，只显示统计")
    if ap.epilog:
        ap.epilog = ap.epilog.replace("python3 ", py_cmd() + " ")
    args = ap.parse_args()

    fp = open(args.out, "a", encoding="utf-8") if args.out else None

    def on_hash(h, kind, peer):
        if args.announce_only and kind != "announce_peer":
            return
        if not args.quiet:
            mark = "★" if kind == "announce_peer" else " "
            print("%s %s  %s:%s" % (mark, h, peer[0], peer[1]))
        if fp:
            fp.write("magnet:?xt=urn:btih:%s\n" % h)
            fp.flush()

    sniffer = DHTSniffer(port=args.port, rate=args.rate, on_hash=on_hash)
    print("节点 ID %s，监听 UDP %d，Ctrl-C 停止" % (sniffer.nid.hex()[:16], args.port),
          file=sys.stderr)

    def reporter():
        while sniffer.running:
            time.sleep(10)
            s = sniffer.stats
            elapsed = max(time.time() - s["started"], 1)
            print("[%4ds] 唯一 %d  get_peers %d  announce %d  "
                  "已知节点 %d  收 %d 发 %d  (%.1f 个/分钟)"
                  % (elapsed, s["unique"], s["get_peers"], s["announce"],
                     len(sniffer.nodes), s["rx"], s["tx"], s["unique"] / elapsed * 60),
                  file=sys.stderr)

    threading.Thread(target=reporter, daemon=True).start()
    if args.seconds:
        threading.Timer(args.seconds, sniffer.stop).start()

    try:
        sniffer.run()
    except KeyboardInterrupt:
        pass
    finally:
        sniffer.stop()
        if fp:
            fp.close()
        print("\n共收集到 %d 个不重复 infohash" % sniffer.stats["unique"], file=sys.stderr)


if __name__ == "__main__":
    main()
