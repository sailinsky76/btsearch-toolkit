#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
造一批测试用的 .torrent —— 给 btimport.py folder 当靶子

生成的是结构完整、能被任何 BT 客户端解析的真 .torrent：bencode 编码正确，
info 字典齐全，infohash 是照着 info 原始字节算的 SHA-1。btimport 的
torrent_infohash() 靠定位 4:info 切字节，所以字段顺序也按规范排了。

但它们描述的是不存在的文件——piece 哈希是随机字节，不对应任何真实数据。
拿去下载什么也下不到，DHT 上也查无此种。这正是测试索引该用的东西：
要验的是 bencode 解析、中文二元组分词、入库和搜索，不是网络。

内容有意挑的是 BT 上本来就合法分发的那类东西：Linux 发行版镜像、
CC 授权的开源动画、维基媒体数据转储、公开科研数据集、公版书。
顺带一个好处是，这批名字拿来测中文分词恰好覆盖得挺全。

最后一组是专门的边界用例，每一条都对着 expand_text / build_match
里一个具体的坑：中文数字粘连、两字词、单字、中英混排、超长名、
无名种子、巨型文件列表。这组才是真正值钱的。

    python3 make_test_torrents.py                 # 默认在 ./test-torrents 下造 80 个
    python3 make_test_torrents.py --out D:\t --n 300
"""

import argparse
import hashlib
import os
import random
import re
import sys

# --------------------------------------------------------------------------
# bencode（只实现生成要用的这几种类型）
# --------------------------------------------------------------------------


def benc(v):
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, str):
        b = v.encode("utf-8")
        return b"%d:%s" % (len(b), b)
    if isinstance(v, list):
        return b"l" + b"".join(benc(x) for x in v) + b"e"
    if isinstance(v, dict):
        # 规范要求键按字节序排。不排的话别的客户端算出来的 infohash 会不一样
        out = b"d"
        for k in sorted(v, key=lambda x: x.encode("utf-8") if isinstance(x, str) else x):
            out += benc(k) + benc(v[k])
        return out + b"e"
    raise TypeError(type(v))


PIECE = 512 * 1024


def make_torrent(name, files=None, length=None, tracker=None, rnd=None):
    """files: [(路径, 字节数)]，给多文件种子；length: 单文件种子的字节数。"""
    rnd = rnd or random
    total = length if length is not None else sum(n for _, n in files)
    npieces = max(1, (total + PIECE - 1) // PIECE)
    npieces = min(npieces, 2000)             # 别让 pieces 把文件撑得太大
    info = {
        "name": name,
        "piece length": PIECE,
        # 真种子这里是每块数据的 SHA-1。我们没有数据，填随机字节——
        # 结构和长度都对，校验不过而已，索引根本不校验这个
        "pieces": bytes(rnd.getrandbits(8) for _ in range(20 * npieces)),
    }
    if files is not None:
        info["files"] = [{"length": n, "path": p.split("/")} for p, n in files]
    else:
        info["length"] = length
    meta = {"info": info, "created by": "make_test_torrents.py"}
    if tracker:
        meta["announce"] = tracker
    blob = benc(meta)
    raw = benc(info)
    return blob, hashlib.sha1(raw).hexdigest()


# --------------------------------------------------------------------------
# 素材：BT 上本来就合法分发的东西
# --------------------------------------------------------------------------

DISTROS = [
    ("ubuntu-24.04.1-desktop-amd64.iso", 6 * 1024**3),
    ("ubuntu-24.04.1-live-server-amd64.iso", 2600 * 1024**2),
    ("debian-12.7.0-amd64-DVD-1.iso", 3900 * 1024**2),
    ("debian-12.7.0-amd64-netinst.iso", 660 * 1024**2),
    ("archlinux-2025.01.01-x86_64.iso", 1100 * 1024**2),
    ("Fedora-Workstation-Live-x86_64-41.iso", 2200 * 1024**2),
    ("linuxmint-22-cinnamon-64bit.iso", 2900 * 1024**2),
    ("manjaro-kde-24.0-240513-linux69.iso", 4100 * 1024**2),
    ("openSUSE-Leap-15.6-DVD-x86_64.iso", 4700 * 1024**2),
    ("alpine-standard-3.20.3-x86_64.iso", 180 * 1024**2),
    ("TailsOS-6.8-amd64.img", 1400 * 1024**2),
    ("FreeBSD-14.1-RELEASE-amd64-disc1.iso", 1000 * 1024**2),
    ("rockylinux-9.4-x86_64-minimal.iso", 1800 * 1024**2),
    ("kali-linux-2024.3-installer-amd64.iso", 3800 * 1024**2),
]

# Blender 基金会的开源动画，全是 CC-BY，本来就靠 BT 分发
OPEN_MOVIES = [
    ("Big.Buck.Bunny.2008.1080p.CC-BY.Blender", 691 * 1024**2, "mp4"),
    ("Sintel.2010.2160p.CC-BY.Blender.Foundation", 4800 * 1024**2, "mkv"),
    ("Tears.of.Steel.2012.1080p.CC-BY.Blender", 1300 * 1024**2, "mkv"),
    ("Cosmos.Laundromat.2015.2160p.CC-BY", 2100 * 1024**2, "mkv"),
    ("Spring.2019.1080p.CC-BY.Blender.Studio", 890 * 1024**2, "mp4"),
    ("Elephants.Dream.2006.1080p.CC-BY", 850 * 1024**2, "mkv"),
    ("Agent.327.Operation.Barbershop.1080p.CC-BY", 320 * 1024**2, "mp4"),
    ("Caminandes.Llamigos.2016.2160p.CC-BY", 410 * 1024**2, "mkv"),
]

DUMPS = [
    ("enwiki-20250101-pages-articles-multistream.xml.bz2", 22 * 1024**3),
    ("zhwiki-20250101-pages-articles.xml.bz2", 2800 * 1024**2),
    ("wikidata-20250101-all.json.gz", 130 * 1024**3),
    ("commonswiki-20250101-files.tar", 41 * 1024**3),
    ("wiktionary-zh-20250101.xml.bz2", 480 * 1024**2),
]

DATASETS = [
    ("ImageNet-ILSVRC2012-train", 138 * 1024**3),
    ("COCO-2017-train-val-annotations", 25 * 1024**3),
    ("LibriSpeech-ASR-corpus-960h", 57 * 1024**3),
    ("OpenStreetMap-planet-250101.osm.pbf", 78 * 1024**3),
    ("Common-Crawl-CC-MAIN-2024-51-segment-0042", 240 * 1024**3),
    ("MNIST-handwritten-digits-full", 55 * 1024**2),
    ("GTEx-v8-RNAseq-gene-counts", 3400 * 1024**2),
    ("Sloan-Digital-Sky-Survey-DR17-spectra", 92 * 1024**3),
]

# 公版书：著作权早已过期
PUBLIC_DOMAIN = [
    ("西游记.吴承恩.明刊本.全一百回.PDF", 340 * 1024**2),
    ("红楼梦.曹雪芹.程乙本.全一百二十回.EPUB", 12 * 1024**2),
    ("三国演义.罗贯中.毛宗岗评本.PDF", 280 * 1024**2),
    ("水浒传.施耐庵.容与堂本.全七十回.EPUB", 15 * 1024**2),
    ("史记.司马迁.中华书局点校本.扫描版.PDF", 890 * 1024**2),
    ("资治通鉴.司马光.全二百九十四卷.EPUB", 46 * 1024**2),
    ("唐诗三百首.蘅塘退士.注释本.EPUB", 3 * 1024**2),
    ("庄子集释.郭庆藩.PDF", 120 * 1024**2),
    ("Complete.Works.of.Shakespeare.Gutenberg.EPUB", 8 * 1024**2),
    ("Pride.and.Prejudice.Jane.Austen.Gutenberg", 2 * 1024**2),
    ("War.and.Peace.Tolstoy.Maude.Translation.EPUB", 6 * 1024**2),
    ("Moby.Dick.Melville.1851.Gutenberg.EPUB", 3 * 1024**2),
]

AUDIO = [
    ("Bach.Goldberg.Variations.Kimiko.Ishizaka.CC0.FLAC", 780 * 1024**2),
    ("Musopen-Complete-Beethoven-Symphonies-PublicDomain", 5600 * 1024**2),
    ("FreeMusicArchive-Jazz-Collection-2024", 12 * 1024**3),
    ("LibriVox-有声书-鲁迅短篇集-公版.MP3", 340 * 1024**2),
]


def files_for(name, size, kind="video"):
    """按类型编一份像样的文件列表——文件名也会进索引，这部分要真实。"""
    if kind == "video":
        return [("%s/%s.mkv" % (name, name), int(size * 0.97)),
                ("%s/%s.zh-CN.srt" % (name, name), 60 * 1024),
                ("%s/%s.en.srt" % (name, name), 55 * 1024),
                ("%s/poster.jpg" % name, 380 * 1024),
                ("%s/README.txt" % name, 2 * 1024)]
    if kind == "season":
        out = []
        for ep in range(1, 13):
            out.append(("%s/%s.E%02d.1080p.mkv" % (name, name, ep), size // 12))
            out.append(("%s/Subs/%s.E%02d.zh-CN.ass" % (name, name, ep), 70 * 1024))
        return out
    if kind == "dataset":
        out = [("%s/README.md" % name, 8 * 1024),
               ("%s/LICENSE" % name, 4 * 1024),
               ("%s/checksums.sha256" % name, 64 * 1024)]
        for i in range(1, 9):
            out.append(("%s/data/shard-%04d.tar" % (name, i), size // 8))
        return out
    return [("%s/%s" % (name, name), size)]


# --------------------------------------------------------------------------
# 边界用例：每条都对着分词里一个具体的坑
# --------------------------------------------------------------------------

EDGE = [
    # expand_text 里那行补丁就是为这个打的：unicode61 会把「联盟4」当一个整词，
    # 所以字母数字必须再单独抽一遍，搜 4 才命中得了
    ("边界用例.中文数字粘连.第4季.全12集.2160p", "中文和数字粘在一起"),
    ("测试2025年度合集第3部分蓝光原盘", "中文数字混排，没有分隔符"),
    # 两字词是中文里最常见的搜法，trigram 分词器搜不到，二元组才行
    ("边界用例.两字词.电影.音乐.纪录.动画.教程", "纯两字词堆叠"),
    # 单字要走 LIKE 回退，现在有 50 万条窗口
    ("边界用例.单字.猫", "单个汉字，触发 LIKE 回退"),
    ("边界用例.单字.狗.鸟.鱼", "多个单字"),
    # 中英夹杂不带空格
    ("边界用例.中英混排NoSpaceBetween汉字AndLatin", "中英无分隔"),
    ("纪录片BBC地球脉动PlanetEarth第二季1080p中英双字", "中英混排长名"),
    # 繁体、日文假名、韩文谚文都在 CJK_RUN 的范围里
    ("邊界用例.繁體中文.臺灣正體.測試", "繁体"),
    ("ボーダーケース.日本語.カタカナとひらがな.テスト", "日文假名"),
    ("경계사례.한국어.한글.테스트", "韩文谚文"),
    # 超长名字，试试列表布局和 overflow-wrap
    ("边界用例.超长名字." + ".".join(["段落%02d" % i for i in range(1, 26)]) +
     ".2160p.HDR10Plus.Atmos.TrueHD.REMUX", "超长名字，测布局换行"),
    # 特殊字符：HTML 转义、FTS 查询语法、路径分隔符
    ("边界用例.特殊字符.<script>&\"'.双引号与尖括号", "HTML 转义"),
    ("边界用例.FTS语法.AND OR NOT NEAR* \"引号\"", "别让输入把 MATCH 语法带跑偏"),
    ("边界用例.百分号%和下划线_.LIKE通配符", "LIKE 的 ESCAPE 有没有生效"),
    ("边界用例.emoji.🎬🎵📚.表情符号", "四字节字符"),
    # 空名字 / 极短名字
    ("x", "极短名字"),
    ("边界用例.体积为零", "size=0"),
]


def main():
    ap = argparse.ArgumentParser(description="造一批测试用 .torrent")
    ap.add_argument("--out", default="test-torrents", help="输出目录")
    ap.add_argument("--n", type=int, default=80, help="总共造多少个")
    ap.add_argument("--seed", type=int, default=20260912)
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)
    TR = "udp://tracker.opentrackr.org:1337/announce"

    made = []

    def emit(name, files=None, length=None):
        blob, ih = make_torrent(name, files=files, length=length,
                               tracker=TR, rnd=rnd)
        # 文件名不能直接用种子名——里面有斜杠、尖括号、emoji，Windows 上建不了
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:80].rstrip(". ")
        fn = "%s.%s.torrent" % (safe or "unnamed", ih[:8])
        with open(os.path.join(args.out, fn), "wb") as fp:
            fp.write(blob)
        made.append((name, ih, len(blob)))

    for name, size in DISTROS:
        emit(name, length=size)                       # 单文件种子
    for name, size, ext in OPEN_MOVIES:
        emit(name, files=files_for(name, size, "video"))
    for name, size in DUMPS:
        emit(name, length=size)
    for name, size in DATASETS:
        emit(name, files=files_for(name, size, "dataset"))
    for name, size in PUBLIC_DOMAIN:
        emit(name, length=size)
    for name, size in AUDIO:
        emit(name, files=files_for(name, size, "dataset"))
    for name, _why in EDGE:
        if "体积为零" in name:
            emit(name, length=0)
        elif "超长" in name:
            emit(name, files=files_for(name[:60], 2 * 1024**3, "season"))
        else:
            emit(name, length=rnd.randint(10**7, 8 * 1024**3))

    # 一个巨型文件列表的种子：btindex 只索引前 40 个文件名（MAX_FILELIST），
    # 这条专门用来看那个截断是不是真的生效了
    big = "边界用例.巨型文件列表.全季合集.500个文件"
    emit(big, files=[("%s/第%03d集.1080p.mkv" % (big, i), 800 * 1024**2)
                     for i in range(1, 501)])

    # 凑数：想要更多就按模板繁衍，用来试分页和性能
    TPL = ["纪录片.{a}.第{n}季.{q}.中英双字", "{a}.Documentary.S{n:02d}.{q}.WEB-DL",
           "公开课.{a}.第{n}讲.{q}", "开源镜像.{a}.build{n}.{q}"]
    TOPIC = ["地球脉动", "宇宙时空之旅", "人类星球", "蓝色星球", "文明的轨迹",
             "OpenCourseWare", "Linux内核", "编译原理", "算法导论", "数据结构"]
    Q = ["2160p", "1080p", "720p", "FLAC", "PDF"]
    while len(made) < args.n:
        t = rnd.choice(TPL).format(a=rnd.choice(TOPIC), n=rnd.randint(1, 9),
                                   q=rnd.choice(Q))
        emit(t, length=rnd.randint(10**8, 60 * 1024**3))

    print("在 %s 下造了 %d 个 .torrent" % (os.path.abspath(args.out), len(made)))
    print("总字节 %.1f MB" % (sum(x[2] for x in made) / 2**20))
    print("\n前几个：")
    for name, ih, _ in made[:5]:
        print("  %s  %s" % (ih[:16], name[:56]))
    print("\n下一步：")
    print("  py -3.11 btimport.py folder %s" % os.path.abspath(args.out))


if __name__ == "__main__":
    main()
