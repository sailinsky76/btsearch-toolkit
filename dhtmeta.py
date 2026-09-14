#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dhtmeta —— 把一串 infohash 变成「这到底是什么东西」

原理：BitTorrent 的种子元数据（info 字典）本身就存在每个 peer 手里。
只要连上任意一个持有该种子的人，走扩展协议向他要 ut_metadata，
他就会把 info 字典分块发过来。拼起来做一次 SHA-1，等于原 infohash 就说明拿对了。

流程一共四步：
  1. TCP 连上 peer，做标准 BitTorrent 握手，保留位里点亮「我支持扩展协议」
  2. 交换扩展握手，对方告诉我们两件事：ut_metadata 用几号消息、元数据一共多大
  3. 按 16 KiB 一块，把所有块都请求一遍，收齐拼起来
  4. SHA-1 校验，然后 bencode 解码，拿到文件名和文件列表

两种用法：
    # 已知某个 peer 持有某个种子，直接问他要
    python3 dhtmeta.py c7e8c989...b07 --peer 203.0.113.9:51413 --save out.torrent

    # 全自动：一边嗅探 DHT，一边把抓到的 infohash 立刻还原成名字
    python3 dhtmeta.py --sniff --out names.tsv

第二种模式需要 dhtsniff.py 放在同一个目录下（bencode 也是从它那儿导入的，
两个文件是一套东西；真要做成项目的话应该把 bencode 单独抽成一个模块）。

依赖：无，标准库足够。
"""

import argparse
import hashlib
import os
import queue
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import dhtsniff                      # run_pipeline 里要用 dhtsniff.DHTSniffer
    from btcompat import BUILD, py_cmd, setup_console
    from dhtsniff import bencode, bdecode, _bdecode, neighbor, decode_nodes
except ImportError:
    sys.exit("需要把 dhtsniff.py 放在本文件旁边（bencode 编解码从那里导入）")


PSTR = b"BitTorrent protocol"
BLOCK = 16384                      # BEP 9 规定的元数据分块大小，固定 16 KiB
EXT_HANDSHAKE = 0                  # 扩展握手固定是 0 号扩展消息
MSG_EXTENDED = 20                  # 扩展协议在 BT 消息里的编号
MY_UT_METADATA = 1                 # 我们在握手里声明的编号，见下方 _fetch_pieces 的注释
MAX_FRAME = 1 << 20                # 单条消息最大 1 MiB，防止对方报个天文数字让我们撑爆内存
DEFAULT_MAX_META = 8 << 20


class MetaError(Exception):
    pass


# --------------------------------------------------------------------------
# BT 线路协议的收发（长度前缀分帧）
# --------------------------------------------------------------------------

def _recv_exact(sock, n: int) -> bytes:
    """TCP 是字节流，recv 给多少是不保证的，必须自己凑够 n 字节。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise MetaError("对方关闭了连接")
        buf += chunk
    return bytes(buf)


def _recv_message(sock):
    """
    读一条 BT 消息，返回 (消息号, 载荷)。
    帧格式是 4 字节大端长度 + 1 字节消息号 + 载荷；长度为 0 是心跳包。
    """
    length = struct.unpack(">I", _recv_exact(sock, 4))[0]
    if length == 0:
        return None, b""                       # keepalive
    if length > MAX_FRAME:
        raise MetaError("消息长度异常: %d" % length)
    body = _recv_exact(sock, length)
    return body[0], body[1:]


def _send_message(sock, msg_id: int, payload: bytes = b""):
    sock.sendall(struct.pack(">IB", len(payload) + 1, msg_id) + payload)


def _send_extended(sock, ext_id: int, payload: bytes):
    _send_message(sock, MSG_EXTENDED, bytes([ext_id]) + payload)


# --------------------------------------------------------------------------
# 第一步：握手
# --------------------------------------------------------------------------

def _handshake(sock, infohash: bytes, peer_id: bytes):
    reserved = bytearray(8)
    # BEP 10：从右往左数第 20 位表示支持扩展协议，落在第 5 个字节的 0x10 上
    reserved[5] |= 0x10
    sock.sendall(bytes([len(PSTR)]) + PSTR + bytes(reserved) + infohash + peer_id)

    resp = _recv_exact(sock, 68)
    if resp[0] != len(PSTR) or resp[1:20] != PSTR:
        raise MetaError("对方不是 BitTorrent 协议")
    if resp[28:48] != infohash:
        raise MetaError("对方握手返回的 infohash 不匹配")
    if not (resp[20:28][5] & 0x10):
        raise MetaError("对方不支持扩展协议，拿不到元数据")


# --------------------------------------------------------------------------
# 第二步：扩展握手，问出 ut_metadata 的编号和元数据大小
# --------------------------------------------------------------------------

def _extended_handshake(sock, max_meta):
    _send_extended(sock, EXT_HANDSHAKE, bencode({
        "m": {"ut_metadata": MY_UT_METADATA},  # 告诉对方：你要发元数据给我，就用这个编号
        "v": "dhtmeta/1.0",
        "metadata_size": 0,
    }))

    # 对方可能先甩几条 bitfield/have 过来，要一直读到扩展握手为止
    for _ in range(32):
        msg_id, payload = _recv_message(sock)
        if msg_id != MSG_EXTENDED or not payload:
            continue
        if payload[0] != EXT_HANDSHAKE:
            continue
        info = bdecode(payload[1:])
        if not isinstance(info, dict):
            raise MetaError("扩展握手内容无法解析")
        ut = (info.get(b"m") or {}).get(b"ut_metadata")
        size = info.get(b"metadata_size")
        if not ut:
            raise MetaError("对方没有 ut_metadata（多半是它自己也还没拿到元数据）")
        if not size or size <= 0 or size > max_meta:
            raise MetaError("metadata_size 不合理: %r" % size)
        return int(ut), int(size)
    raise MetaError("等不到扩展握手")


# --------------------------------------------------------------------------
# 第三、四步：要块、拼装、校验
# --------------------------------------------------------------------------

def _fetch_pieces(sock, ut_id: int, size: int, infohash: bytes) -> bytes:
    """
    ⚠️ 这里有个方向性陷阱，很容易写错：扩展消息的编号是「各说各的」。
    握手时对方声明的编号（ut_id）是给我们发消息时用的；
    我们自己声明的编号（MY_UT_METADATA）才是对方发回来时会带的。
    所以——发出去用 ut_id，收进来认 MY_UT_METADATA。
    实际网上有些客户端这块实现得不严谨，所以收的时候两个都认，宽容一点。
    """
    total = (size + BLOCK - 1) // BLOCK
    # 一口气把所有块都请求出去，不用一问一答，快得多
    for i in range(total):
        _send_extended(sock, ut_id, bencode({"msg_type": 0, "piece": i}))

    pieces = {}
    deadline = time.time() + 30
    while len(pieces) < total:
        if time.time() > deadline:
            raise MetaError("收元数据超时（%d/%d 块）" % (len(pieces), total))
        msg_id, payload = _recv_message(sock)
        if msg_id != MSG_EXTENDED or not payload:
            continue
        if payload[0] not in (MY_UT_METADATA, ut_id):
            continue
        # data 消息的结构特别：一个 bencode 字典，紧跟着原始数据。
        # 所以必须知道字典在哪儿结束——解码函数顺便返回的位置正好派上用场。
        try:
            head, offset = _bdecode(payload[1:], 0)
        except (ValueError, IndexError):
            continue
        if not isinstance(head, dict):
            continue
        mtype = head.get(b"msg_type")
        idx = head.get(b"piece")
        if mtype == 2:
            raise MetaError("对方拒绝提供第 %s 块" % idx)
        if mtype != 1 or not isinstance(idx, int):
            continue
        pieces[idx] = payload[1 + offset:]

    raw = b"".join(pieces[i] for i in range(total))
    if len(raw) != size:
        raise MetaError("拼出来的长度对不上: %d != %d" % (len(raw), size))
    # 这一步不能省：拼出来的字节做 SHA-1 必须等于 infohash，否则就是收到脏数据了
    if hashlib.sha1(raw).digest() != infohash:
        raise MetaError("SHA-1 校验失败，数据不可信")
    return raw


def _xor_distance(a: bytes, b: bytes) -> int:
    return int.from_bytes(bytes(x ^ y for x, y in zip(a, b)), "big")


def _decode_peers(values) -> list:
    """get_peers 回复里的 values：每项 6 字节 = 4 字节 IP + 2 字节端口。"""
    out = []
    if not isinstance(values, list):
        return out
    for v in values:
        if isinstance(v, bytes) and len(v) == 6:
            try:
                out.append((socket.inet_ntoa(v[:4]), struct.unpack(">H", v[4:])[0]))
            except (OSError, struct.error):
                continue
    return [p for p in out if p[1] > 0]


_hot_nodes = []                  # 全局热节点池
_hot_lock = threading.Lock()
_hot_stamp = [0.0]


def warm_nodes(want=64, timeout=3.0):
    """
    用 find_node 捞一批真实节点备用。

    为什么必须有这一步：嗅探器的节点池会被 joiner 不断消耗，一旦见底，
    lookup_peers 就会退回入口路由器——而那些路由器只负责引导、不回应 get_peers，
    于是每一次查询都空手而归。症状是「查到持有者」这个数字彻底冻住不动。
    """
    with _hot_lock:
        if len(_hot_nodes) >= want and time.time() - _hot_stamp[0] < 120:
            return list(_hot_nodes[-want:])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.3)
    found = []
    try:
        me = os.urandom(20)
        seeds = []
        for host, port in (("router.bittorrent.com", 6881),
                           ("dht.transmissionbt.com", 6881),
                           ("router.utorrent.com", 6881),
                           ("dht.libtorrent.org", 25401)):
            try:
                seeds.append((socket.gethostbyname(host), port))
            except (OSError, socket.gaierror):
                pass
        with _hot_lock:
            seeds += list(_hot_nodes[-16:])
        for addr in seeds:
            try:
                sock.sendto(bencode({"t": os.urandom(2), "y": "q", "q": "find_node",
                                     "a": {"id": me, "target": os.urandom(20)}}), addr)
            except OSError:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline and len(found) < want * 2:
            try:
                data, _ = sock.recvfrom(4096)
                msg = bdecode(data)
            except Exception:
                continue
            if not isinstance(msg, dict) or msg.get(b"y") != b"r":
                continue
            new = [(ip, p) for _, ip, p in decode_nodes((msg.get(b"r") or {}).get(b"nodes", b""))]
            for addr in new[:6]:
                try:
                    sock.sendto(bencode({"t": os.urandom(2), "y": "q", "q": "find_node",
                                         "a": {"id": me, "target": os.urandom(20)}}), addr)
                except OSError:
                    pass
            found.extend(new)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    with _hot_lock:
        known = set(_hot_nodes)
        _hot_nodes.extend(a for a in found if a not in known)
        del _hot_nodes[:-400]
        _hot_stamp[0] = time.time()
        return list(_hot_nodes[-want:])


def lookup_peers(infohash: bytes, seeds=None, want=12, timeout=8, alpha=8):
    """
    自己向 DHT 发起一次 get_peers 迭代查询，问出「谁真的持有这个种子」。

    这是覆盖率的关键。嗅探到的 get_peers 只说明「有人在找」，那个人手里通常什么
    都没有；只有绕着 DHT 问一圈，才能把这部分 infohash 也变成可抓取的线索——
    而 get_peers 的流量比 announce_peer 高一两个数量级，这块捞回来收获很大。

    做法是标准的迭代收敛：从若干起始节点出发，每轮挑「离目标最近的」几个去问，
    对方要么直接回 values（真的 peer 地址），要么回 nodes（它认识的更近的节点），
    把新节点并进候选集继续问，直到问出足够的 peer 或者超时。

    另外一个好消息：这条路只需要出网，所以 NAT 后面照样能用——
    跟嗅探器必须要公网端口是两回事。
    """
    my_id = neighbor(infohash, os.urandom(20))     # 贴着目标，路由更准
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.35)

    candidates = {}          # (ip, port) -> 距离，越小越近
    queried = set()
    peers = []
    seen_peers = set()

    if seeds:
        for ip, port in seeds:
            candidates[(ip, port)] = 1 << 160      # 起始节点距离未知，给个最大值排最后
    else:
        for host, port in (("router.bittorrent.com", 6881),
                           ("dht.transmissionbt.com", 6881),
                           ("router.utorrent.com", 6881)):
            try:
                candidates[(socket.gethostbyname(host), port)] = 1 << 160
            except (OSError, socket.gaierror):
                pass

    query = bencode({"t": b"pl", "y": "q", "q": "get_peers",
                     "a": {"id": my_id, "info_hash": infohash}})
    deadline = time.time() + timeout
    try:
        while time.time() < deadline and len(peers) < want:
            batch = sorted(((d, a) for a, d in candidates.items() if a not in queried),
                           key=lambda x: x[0])[:alpha]
            for _, addr in batch:
                queried.add(addr)
                try:
                    sock.sendto(query, addr)
                except OSError:
                    pass
            if not batch and len(queried) >= len(candidates):
                break                                   # 问不出更近的了，收敛

            # 这一轮的回包要全部读干净，不能只读一个。
            # 每轮发 8 个查询却只收 1 个回复的话，候选集永远追不上查询速度，
            # 等到「所有候选都问过了」触发收敛时，大部分回包还堵在缓冲区里没读，
            # 迭代就停在离目标很远的地方——而只有最接近目标的节点才存着 peer 列表。
            drained = 0
            while time.time() < deadline and drained < 128:
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    break                      # 缓冲区空了，进入下一轮
                except OSError:
                    break
                drained += 1
                try:
                    msg = bdecode(data)
                    r = msg.get(b"r")
                except Exception:
                    continue
                if not isinstance(r, dict):
                    continue

                for p in _decode_peers(r.get(b"values")):
                    if p not in seen_peers:
                        seen_peers.add(p)
                        peers.append(p)
                for nid, ip, port in decode_nodes(r.get(b"nodes", b"")):
                    addr = (ip, port)
                    if addr not in candidates:
                        candidates[addr] = _xor_distance(nid, infohash)
                if len(peers) >= want:
                    break
    finally:
        sock.close()
    return peers


def fetch_metadata(infohash: bytes, peer, timeout=15, max_meta=DEFAULT_MAX_META):
    """
    向 peer 索取种子元数据。
    返回 (info 字典的原始字节, 解码后的 dict)。失败抛 MetaError。
    """
    if len(infohash) != 20:
        raise MetaError("infohash 必须是 20 字节")
    peer_id = b"-DM0001-" + os.urandom(12)

    # 每一步单独标注，失败时才能分清是连不上、还是连上了握不上手
    try:
        sock = socket.create_connection(peer, timeout=timeout)
    except socket.timeout:
        raise MetaError("[连接] 超时，对方多半在 NAT 后面")
    except OSError as e:
        raise MetaError("[连接] %s" % e)
    try:
        sock.settimeout(timeout)
        try:
            _handshake(sock, infohash, peer_id)
        except socket.timeout:
            raise MetaError("[BT握手] 连上了但对方不回应")
        try:
            ut_id, size = _extended_handshake(sock, max_meta)
        except socket.timeout:
            raise MetaError("[扩展握手] 握手通过但扩展协商无回应")
        try:
            raw = _fetch_pieces(sock, ut_id, size, infohash)
        except socket.timeout:
            raise MetaError("[取元数据] 协商完成但数据块收不全")
    finally:
        try:
            sock.close()
        except OSError:
            pass

    info = bdecode(raw)
    if not isinstance(info, dict):
        raise MetaError("info 字典解码失败")
    return raw, info


# --------------------------------------------------------------------------
# 结果整理
# --------------------------------------------------------------------------

def _text(v, default=""):
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else default


def describe(info: dict) -> dict:
    """把 info 字典整理成人能看的形式。utf-8 后缀的字段优先，编码更靠谱。"""
    name = _text(info.get(b"name.utf-8") or info.get(b"name"), "(无名)")
    files = []
    if isinstance(info.get(b"files"), list):          # 多文件种子
        for f in info[b"files"]:
            if not isinstance(f, dict):
                continue
            path = f.get(b"path.utf-8") or f.get(b"path") or []
            joined = "/".join(_text(p) for p in path if isinstance(p, bytes))
            files.append((joined or "(未知)", int(f.get(b"length") or 0)))
    else:                                             # 单文件种子
        files.append((name, int(info.get(b"length") or 0)))
    return {
        "name": name,
        "files": files,
        "count": len(files),
        "total_size": sum(sz for _, sz in files),
        "piece_length": int(info.get(b"piece length") or 0),
    }


def build_torrent(info_raw: bytes, trackers=()) -> bytes:
    """
    用抓到的 info 原始字节重建一个完整的 .torrent 文件。
    注意 info 部分必须原样塞回去——重新编码一遍就可能改变字节序，infohash 跟着就变了。
    外层键也必须按字节序排列，下面的顺序已经是排好的。
    """
    parts = [b"d"]
    if trackers:
        parts.append(b"8:announce" + bencode(trackers[0].encode()))
        parts.append(b"13:announce-list" + bencode([[t.encode()] for t in trackers]))
    parts.append(b"10:created by" + bencode(b"dhtmeta"))
    parts.append(b"13:creation date" + bencode(int(time.time())))
    parts.append(b"4:info" + info_raw)
    parts.append(b"e")
    return b"".join(parts)


def human(n: int) -> str:
    if n <= 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.1f %s" % (n, unit)).replace(".0 ", " ")
        n /= 1024.0
    return "-"


def parse_infohash(text: str) -> bytes:
    """接受纯十六进制，也接受直接粘一条磁力链进来。"""
    text = text.strip()
    if text.startswith("magnet:"):
        import re
        m = re.search(r"btih:([0-9a-fA-F]{40})", text)
        if not m:
            raise ValueError("磁力链里没找到 40 位十六进制的 infohash")
        text = m.group(1)
    if len(text) != 40:
        raise ValueError("infohash 应该是 40 位十六进制")
    return bytes.fromhex(text)


# --------------------------------------------------------------------------
# 模式一：单个抓取
# --------------------------------------------------------------------------

def run_single(args):
    ih = parse_infohash(args.infohash)
    host, _, port = args.peer.rpartition(":")
    raw, info = fetch_metadata(ih, (host, int(port)), timeout=args.timeout)
    d = describe(info)

    print("名称  : %s" % d["name"])
    print("大小  : %s（%d 个文件）" % (human(d["total_size"]), d["count"]))
    for path, size in d["files"][:20]:
        print("        %-60s %10s" % (path[:60], human(size)))
    if d["count"] > 20:
        print("        …… 还有 %d 个文件" % (d["count"] - 20))

    if args.save:
        with open(args.save, "wb") as fp:
            fp.write(build_torrent(raw))
        print("已保存: %s" % args.save)


# --------------------------------------------------------------------------
# 模式二：嗅探 + 抓取 全自动流水线
# --------------------------------------------------------------------------

def _parse_ports(spec: str):
    """支持 '6881'、'6881-6888'、'6881,6890,7000-7002' 三种写法。"""
    ports = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            ports.extend(range(int(lo), int(hi) + 1))
        else:
            ports.append(int(part))
    bad = [p for p in ports if not (1 <= p <= 65535)]
    if bad:
        raise ValueError("端口越界: %s" % bad)
    return sorted(set(ports))


def run_pipeline(args):
    """
    全自动流水线：嗅探 -> 找持有者 -> 抓元数据 -> 入库。

    两条线索来源，性质完全不同，所以走两条队列：

      announce_peer  报文里直接带着「确实持有者」的地址，连过去就能问，成本最低。
                     缺点是量少。

      get_peers      只说明有人在找，发问的那个人手里多半什么都没有。要用上它，
                     得自己向 DHT 查一圈问出真正的持有者（lookup_peers），
                     一次查询就是几十个 UDP 往返，贵得多。但它的量是 announce 的
                     几十倍，覆盖率主要靠它撑起来。

    所以 get_peers 那条队列必须设上限并且允许丢弃——嗅探速度远快于查询速度，
    不丢就会无限堆积吃光内存。丢掉不可惜，反正下一秒还有新的进来。
    """
    try:
        from btindex import Index
    except ImportError:
        sys.exit("需要把 btindex.py 放在本文件旁边")

    idx = Index(args.db)
    db_lock = threading.Lock()
    out_lock = threading.Lock()

    direct_q = queue.Queue(maxsize=5000)      # announce 来的，带现成的 peer
    lookup_q = queue.Queue(maxsize=args.lookup_queue)   # get_peers 来的，需要先查
    tried = set()
    tried_lock = threading.Lock()
    stat = dict(announce=0, getpeers=0, dropped=0, ok=0, fail=0, lookup_hit=0, shown=0)
    fail_why = {}                 # 失败原因 -> 次数，用来判断到底卡在哪一步
    fail_lock = threading.Lock()

    def note_fail(peer, err):
        """记录一次抓取失败的真实原因。默认只统计不打印，加 --debug-fail 才逐条显示。"""
        why = "%s: %s" % (type(err).__name__, err)
        with fail_lock:
            fail_why[why] = fail_why.get(why, 0) + 1
            if args.debug_fail and stat["shown"] < args.debug_fail:
                stat["shown"] += 1
                print("  [抓取失败] %s:%s  %s" % (peer[0], peer[1], why), file=sys.stderr)
    sniffer = None

    def is_new(h):
        with tried_lock:
            if h in tried:
                return False
            tried.add(h)
            if len(tried) > 500000:           # 别让这个集合无限长
                tried.clear()
                tried.add(h)
        with db_lock:
            if idx.has(h):
                idx.bump(h)                   # 已经收录过，只更新热度
                return False
        return True

    def on_hash(h, kind, peer):
        if kind == "announce_peer":
            stat["announce"] += 1
            if not is_new(h):
                return
            try:
                direct_q.put_nowait((h, [peer]))
            except queue.Full:
                stat["dropped"] += 1
        elif args.with_lookup:
            stat["getpeers"] += 1
            if not is_new(h):
                return
            try:
                lookup_q.put_nowait(h)
            except queue.Full:
                stat["dropped"] += 1          # 队列满就丢，这是有意为之

    def store(h, raw, info):
        d = describe(info)
        with db_lock:
            idx.upsert(h, d["name"], d["total_size"], d["count"],
                       [p for p, _ in d["files"]], source="dht")
            idx.commit()
        stat["ok"] += 1
        with out_lock:
            print("%-9s %s  [%d 个文件]"
                  % (human(d["total_size"]), d["name"][:68], d["count"]))
        if args.save_torrents:
            os.makedirs(args.save_torrents, exist_ok=True)
            with open(os.path.join(args.save_torrents, h + ".torrent"), "wb") as fp:
                fp.write(build_torrent(raw))

    polluters = {}                  # IP -> 连续「有 infohash 却没有元数据」的次数
    poll_lock = threading.Lock()
    POLLUTER_LIMIT = 4

    def skip_peer(ip):
        with poll_lock:
            return polluters.get(ip, 0) >= POLLUTER_LIMIT

    def mark_peer(ip):
        """
        DHT 里有大量往热门种子 peer 列表注水的节点：握手、扩展协商全都正常，
        唯独永远不报 metadata_size。同一批 IP 会对着不同种子反复出现，
        次数多了就没必要再试，把连接额度留给真 peer。
        """
        with poll_lock:
            polluters[ip] = polluters.get(ip, 0) + 1

    def try_peers(h, peers):
        """挨个试，有一个成功就够了。"""
        ih = bytes.fromhex(h)
        for peer in peers:
            if skip_peer(peer[0]):
                continue
            try:
                raw, info = fetch_metadata(ih, peer, timeout=args.timeout)
                store(h, raw, info)
                return True
            except Exception as e:
                # 接得比需要的宽，是有意的：这个循环的语义是「这个 peer 不行就换
                # 下一个」，而对面是陌生人，它发来的任何东西都可能触发想不到的
                # 异常类型。漏掉一种，代价就是剩下的 peer 全部不再试——
                # 放弃一整个种子，只因为第一个 peer 不怀好意。
                if "metadata_size" in str(e):
                    mark_peer(peer[0])
                note_fail(peer, e)
                continue
        stat["fail"] += 1
        return False

    def direct_worker():
        """
        专职干 TCP 抓取。announce 为 0 时这批线程本来全闲着，
        而查询线程却卡在抓取上——查完就把 peer 交到这条队列，两边都不浪费。
        """
        while True:
            h, peers = direct_q.get()
            try:
                try_peers(h, peers)
            except Exception:
                stat["fail"] += 1
            finally:
                direct_q.task_done()

    def lookup_worker():
        while True:
            h = lookup_q.get()
            try:
                # 拿嗅探器手上的活节点当查询起点，比每次都去敲 bootstrap 服务器
                # 快得多，也礼貌得多
                seeds = [(ip, port) for _, ip, port in list(sniffer.nodes)[:16]] \
                    if sniffer and sniffer.nodes else []
                if len(seeds) < 8:          # 嗅探器池子见底，拿热节点补上
                    seeds = (seeds + warm_nodes())[:24]
                peers = lookup_peers(bytes.fromhex(h), seeds=seeds,
                                     timeout=args.lookup_timeout)
                if peers:
                    stat["lookup_hit"] += 1
                    # 交棒给直连线程，自己立刻回去查下一个，
                    # 不再被一串 TCP 超时拖住
                    try:
                        direct_q.put_nowait((h, peers[:args.try_peers]))
                    except queue.Full:
                        stat["dropped"] += 1
                else:
                    stat["fail"] += 1
            except Exception:
                stat["fail"] += 1
            finally:
                lookup_q.task_done()

    for _ in range(args.workers):
        threading.Thread(target=direct_worker, daemon=True).start()
    if args.with_lookup:
        for _ in range(args.lookup_workers):
            threading.Thread(target=lookup_worker, daemon=True).start()

    sniffer = dhtsniff.DHTSniffer(port=args.port, rate=args.rate, on_hash=on_hash)

    def reporter():
        start = time.time()
        while sniffer.running:
            time.sleep(15)
            el = max(time.time() - start, 1)
            pool = len(sniffer.nodes) if sniffer else 0
            with poll_lock:
                blocked = sum(1 for v in polluters.values() if v >= POLLUTER_LIMIT)
            print("[%4ds] 入库 %d (%.1f/分)  announce %d  get_peers %d  "
                  "查到持有者 %d  失败 %d  队列 %d/%d  节点池 %d  已屏蔽 %d"
                  % (el, stat["ok"], stat["ok"] / el * 60, stat["announce"],
                     stat["getpeers"], stat["lookup_hit"], stat["fail"],
                     direct_q.qsize(), lookup_q.qsize(), pool, blocked),
                  file=sys.stderr)

    threading.Thread(target=reporter, daemon=True).start()
    print("[构建 %s] 流水线启动：UDP %d 嗅探，%d 个直连线程%s，写入 %s"
          % (BUILD, args.port, args.workers,
             "，%d 个 DHT 查询线程" % args.lookup_workers if args.with_lookup else "",
             args.db), file=sys.stderr)
    try:
        sniffer.run()
    except KeyboardInterrupt:
        pass
    finally:
        sniffer.stop()
        with db_lock:
            idx.commit()
            total = idx.stats()["count"]
            idx.close()
        print("\n本次入库 %d 条，数据库现有 %d 条" % (stat["ok"], total), file=sys.stderr)
        if fail_why:
            print("\n抓取失败原因分布（这是判断卡在哪一步的关键）：", file=sys.stderr)
            for why, n in sorted(fail_why.items(), key=lambda kv: -kv[1])[:8]:
                print("  %6d 次  %s" % (n, why[:76]), file=sys.stderr)
            print("\n  连接超时占绝大多数 -> 出站 TCP 到随机高位端口被拦，或对方都在 NAT 后面\n"
                  "  对方关闭了连接     -> 能连上但握手被拒，通常是对方不认我们\n"
                  "  不支持扩展协议     -> 连上了但对方没有元数据可给", file=sys.stderr)


def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description="把 infohash 还原成种子名，并喂进本地索引",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  # 单个抓取：已知谁持有，直接问他要
  python3 dhtmeta.py c7e8...b07 --peer 203.0.113.9:51413 --save x.torrent

  # 全自动，只用 announce 线索（省资源，覆盖率低）
  python3 dhtmeta.py --sniff --db bt.db

  # 全自动 + DHT 主动查询：覆盖率高一个数量级，但流量和 CPU 都吃得多
  python3 dhtmeta.py --sniff --db bt.db --with-lookup --lookup-workers 30
""")
    ap.add_argument("infohash", nargs="?", help="40 位十六进制，或直接粘一条磁力链")
    ap.add_argument("--peer", help="持有该种子的地址，格式 IP:端口")
    ap.add_argument("--save", help="把重建的 .torrent 存到这个路径")
    ap.add_argument("--timeout", type=int, default=8,
                    help="单次抓取超时秒数（默认 8。真能连上的 peer 一两秒就完事，"
                         "调大只是在死 peer 上多等）")

    g = ap.add_argument_group("全自动模式")
    g.add_argument("--sniff", action="store_true", help="边嗅探边还原并入库")
    g.add_argument("--db", default="bt.db", help="写入的索引库（默认 bt.db）")
    g.add_argument("--workers", type=int, default=30, help="直连抓取线程数")
    g.add_argument("--with-lookup", action="store_true",
                   help="把 get_peers 线索也用上：自己查 DHT 找持有者。"
                        "覆盖率能高一个数量级，代价是流量和 CPU 都大得多")
    g.add_argument("--lookup-workers", type=int, default=20, help="DHT 查询线程数")
    g.add_argument("--lookup-queue", type=int, default=2000,
                   help="待查询队列上限，满了直接丢（嗅探比查询快得多，必须丢）")
    g.add_argument("--lookup-timeout", type=int, default=8, help="单次 DHT 查询超时秒数")
    g.add_argument("--save-torrents", default="", help="顺便把 .torrent 存进这个目录")
    g.add_argument("--try-peers", type=int, default=10, metavar="N",
                   help="每个种子最多试几个 peer（默认 10）。"
                        "DHT 里假 peer 很多，多试几个才碰得到真的")
    g.add_argument("--debug-fail", type=int, default=0, metavar="N",
                   help="逐条打印前 N 次抓取失败的原因和对方地址，排查用")
    g.add_argument("--port", type=int, default=6881, help="DHT 监听端口")
    g.add_argument("--rate", type=int, default=30,
                   help="每秒发出的 find_node 数（默认 30）。家用路由器的 NAT 表通常只有"
                        "几千条、超时一分钟左右，150/秒会把它撑爆，表现为跑十几分钟后"
                        "收发双双掉到接近零。公网 IP 的机器可以往上调")
    if ap.epilog:
        ap.epilog = ap.epilog.replace("python3 ", py_cmd() + " ")
    args = ap.parse_args()

    if args.sniff:
        run_pipeline(args)
    elif args.infohash and args.peer:
        try:
            run_single(args)
        except (MetaError, OSError, ValueError) as e:
            sys.exit("失败: %s" % e)
    else:
        ap.error("要么给 infohash + --peer，要么用 --sniff")


if __name__ == "__main__":
    main()
