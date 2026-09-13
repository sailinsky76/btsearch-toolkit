#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
btparse —— 把糊在种子名里的信息拆成字段

种子名是人拼出来的一串东西，不是数据：

    Some.Show.S02E07.2160p.WEB-DL.DDP5.1.HDR.H.265-GROUP
    [某字幕组] 某某动画 [12][1080p][简繁内封]
    某某电影.2019.BluRay.1080p.x264.国语中字.mkv

里面其实有分类、清晰度、第几季第几集、年份、编码、片源，但它们只是名字里的
几个词。搜索能搜到它们（索引里每个字母数字串都是一个词），可是搜「1080p」
搜到的是「名字里带 1080p」，不是「这条是 1080p」——一部 720p 的片子，
名字里写着「1080p 版本另发」，照样命中。而「只看剧集」这种意思根本没有
对应的词可搜：没有哪个 token 是「这是一部剧」。

所以这一步做的事是：把其中两样**归一化**之后存成列——

    kind  分类：movie 电影 / tv 剧集 / video 影视（分不出是电影还是剧） /
          music 音乐 / game 游戏 / software 软件 / book 图书 / '' 认不出
    res   清晰度：2160p / 1080p / 720p / 480p / '' 认不出

只有这两样进库，因为只有它们**归一化之后比原词更有用**：
4K、UHD、2160p、3840x2160 是同一件事，搜词搜不到一起，列可以；
「剧集」压根不是一个词，只能靠推断。

其余的（年份、第几季第几集、编码、片源、发布组）解析出来只用来显示，不进库。
理由很实在：它们在名字里本来就是独立的词，搜索已经能搜到，
再存一列除了占地方和多一份要维护的真相之外，没有新增能力。
年份尤其明显——搜 2019 和 year=2019 命中的是同一批东西。

**这里不做标题归一化。** 把「同一部片的不同版本」合并成一条，要处理别名、
译名、分隔符、发布组习惯，是另一件事，收益也不同（去重而不是筛选）。
掺进来只会让这一步的对错说不清楚。

用法：
    py -3.11 btparse.py try "Some.Show.S02E07.1080p.WEB-DL.x265-GRP"
    py -3.11 btparse.py test                  # 跑规则自测，btcheck 也会调
    py -3.11 btparse.py backfill --db bt.db   # 给老条目补上这两列
    py -3.11 btparse.py stats --db bt.db      # 看解析覆盖率
    py -3.11 btparse.py sample --db bt.db -n 30   # 抽几条看看认得对不对

依赖：无，纯标准库。这个模块被 btindex 导入，所以它不能反过来导入 btindex
（循环导入），也不该有任何重量级依赖——每写一条种子都要过一遍这里。
"""

import argparse
import os
import re
import sqlite3
import sys
import time

# 规则版本号。改了规则就 +1，backfill 会据此认出「这条是旧规则解析的」并重跑。
# 没有这个号的话，唯一的办法是全库重扫，或者干脆不管、让新旧规则的结果混在库里。
PARSE_VERSION = 2

# 分类的取值和界面上的说法。存进库的是左边那个 ascii 串，
# 中文只在界面上出现——库里存中文，将来改说法就得动数据。
KIND_LABEL = [("movie", "电影"), ("tv", "剧集"), ("video", "影视"),
              ("music", "音乐"), ("game", "游戏"), ("software", "软件"),
              ("book", "图书")]
KIND_TEXT = dict(KIND_LABEL)

# 清晰度只分四档。档内的差别（1080p 还是 1080i、480p 还是 576p）
# 对「我要找一个能看的版本」这件事没有影响，分得越细筛起来越麻烦。
RES_LABEL = [("2160p", "4K/2160p"), ("1080p", "1080p"),
             ("720p", "720p"), ("480p", "480p 以下")]
RES_TEXT = dict(RES_LABEL)

MEDIUM_TEXT = {"remux": "Remux", "bluray": "BluRay", "webdl": "WEB-DL",
               "webrip": "WEBRip", "web": "WEB", "hdtv": "HDTV",
               "dvd": "DVD", "hdrip": "HDRip", "cam": "枪版"}
# x264 和 H.264 不是一回事：前者是编码器，后者是格式。原先 avc 也归到 x264，
# 于是 `...BluRay.REMUX.AVC` 显示成「Remux · x264」——而 remux 恰恰是原封不动
# 拆封装、没有重新编码的那种，说它用 x264 压过是错的。名字里写了哪个就显示哪个。
# 这两组只影响显示，codec 不进库，所以拆开没有迁移代价。
CODEC_TEXT = {"x265": "x265", "hevc": "HEVC", "x264": "x264", "h264": "H.264",
              "av1": "AV1", "vp9": "VP9",
              "xvid": "XviD", "divx": "DivX", "mpeg2": "MPEG-2"}


# --------------------------------------------------------------------------
# 规则
# --------------------------------------------------------------------------

# 归一化：除了字母数字和中日韩文字，其它一律变成空格。
# 这一步之后 "web-dl" 是 "web dl"、"H.265" 是 "h 265"、"[1080p]" 是 " 1080p "，
# 而 "x264"、"s01e05"、"1920x1080" 这些本来就连在一起的不受影响。
# 所有规则都写成「在空格分隔的串上匹配」，就不用给每个模式都配一套分隔符变体。
_NON = re.compile(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
                  r"\u3040-\u30ff\uac00-\ud7af]+")


def flatten(text: str) -> str:
    return " " + _NON.sub(" ", (text or "").lower()).strip() + " "


# 清晰度。从高到低试，第一个命中的算数。
# 名字里同时出现两个清晰度是有的（「1080p 版本另发」「2160p 重制」），
# 取高的那个是个选择，不是真理——但比取第一个出现的稳定，
# 至少同一个名字每次解析结果一样。
_RES_RULES = [
    ("2160p", re.compile(r"2160[pi]|(?<![a-z0-9])4k(?![a-z0-9])|"
                         r"(?<![a-z0-9])uhd(?![a-z0-9])|3840 ?x ?2160")),
    ("1080p", re.compile(r"1080[pi]|1920 ?x ?1080|(?<![a-z0-9])fhd(?![a-z0-9])")),
    ("720p",  re.compile(r"720[pi]|1280 ?x ?720")),
    ("480p",  re.compile(r"(?<!\d)(?:576|480|360|240)[pi](?![a-z0-9])")),
]

# 片源。顺序要紧：remux 必须在 bluray 前面（"UHD BluRay REMUX" 是 remux），
# web dl / web rip 必须在光秃秃的 web 前面。
_MEDIUM_RULES = [
    ("remux",  re.compile(r"(?<![a-z])remux(?![a-z])")),
    ("bluray", re.compile(r"blu ?ray|(?<![a-z])b[dr]rip(?![a-z])|"
                          r"(?<![a-z])bdmv(?![a-z])|(?<![a-z])bd(?![a-z0-9])")),
    ("webdl",  re.compile(r"(?<![a-z])web ?dl(?![a-z])")),
    ("webrip", re.compile(r"(?<![a-z])web ?rip(?![a-z])")),
    ("web",    re.compile(r"(?<![a-z])web(?![a-z0-9])")),
    ("hdtv",   re.compile(r"(?<![a-z])[hp]dtv(?![a-z])")),
    ("dvd",    re.compile(r"(?<![a-z])dvd(?:rip|scr|r|5|9)?(?![a-z0-9])")),
    ("hdrip",  re.compile(r"(?<![a-z])hd ?rip(?![a-z])")),
    # 枪版只认写得明明白白的那几个。TS 不认——它既是 telesync 也是
    # transport stream（.ts 文件），认错的代价是把一个好版本标成枪版。
    ("cam",    re.compile(r"(?<![a-z])(?:cam ?rip|camrip|cam|telesync|hdts)(?![a-z])")),
]

# 编码器写法（x264/x265）排在格式写法（H.264/AVC/HEVC）前面：
# 名字里两个都写的时候，编码器那个信息量更大。
_CODEC_RULES = [
    ("x265",  re.compile(r"(?<![a-z0-9])x ?265(?![a-z0-9])")),
    ("x264",  re.compile(r"(?<![a-z0-9])x ?264(?![a-z0-9])")),
    ("hevc",  re.compile(r"(?<![a-z0-9])(?:h ?265|hevc)(?![a-z0-9])")),
    ("h264",  re.compile(r"(?<![a-z0-9])(?:h ?264|avc)(?![a-z0-9])")),
    ("av1",   re.compile(r"(?<![a-z0-9])av1(?![a-z0-9])")),
    ("vp9",   re.compile(r"(?<![a-z0-9])vp9(?![a-z0-9])")),
    ("xvid",  re.compile(r"(?<![a-z0-9])xvid(?![a-z0-9])")),
    ("divx",  re.compile(r"(?<![a-z0-9])divx(?![a-z0-9])")),
    ("mpeg2", re.compile(r"(?<![a-z0-9])mpeg ?2(?![a-z0-9])")),
]

# 季集。S01E05 这种写法最规矩，先试它。
_SXE = re.compile(r"(?<![a-z0-9])s(\d{1,2}) ?e(\d{1,3})(?![a-z0-9])")
# 1x05 这种老写法。左边限死两位数、右边限死两位数，
# 否则 1920x1080 会被读成「第 20 季第 10 集」——这是最容易撞上的一个坑，
# 而且撞上了还不报错，只是默默把一部电影标成剧集。
_NXN = re.compile(r"(?<![\d.])(\d{1,2})x(\d{2})(?![\d])")
_SEASON = re.compile(r"(?<![a-z0-9])s(?:eason)? ?(\d{1,2})(?![a-z0-9])")
# 光一个 e05 不算集数：发布组名字里带个 -E5 太常见了。
# 只有写全 ep / episode，或者前面已经认出季号的，才作数。
_EP_WORD = re.compile(r"(?<![a-z0-9])ep(?:isode)? ?(\d{1,3})(?![a-z0-9])")
_EP_BARE = re.compile(r"(?<![a-z0-9])e(\d{1,3})(?![a-z0-9])")
_CN_SEASON = re.compile(r"第\s*(\d{1,2})\s*[季部]")
_CN_EP = re.compile(r"第\s*(\d{1,4})\s*[集话話期]")
# 「全24集」「更新至12集」说的是这是一部剧，不是第几集
_CN_PACK = re.compile(r"(全|共|更新至)\s*\d{1,4}\s*[集话話期]")
# 字幕组那种 [12] 单独一格的写法。纯数字、两三位、方括号包着——
# 这个形状足够特别，[1080p] [2019] [BDRip] 都不是纯数字。
_BRACKET_EP = re.compile(r"\[(\d{2,3})(?:v\d)?\]")

# 剧集词分强弱两档，这个区分是必须的。
# 强的（S01E02、第几季、电视剧、complete series）足以断定「这是一部剧」，
# 因而也足以把这条归进影视大类。
# 弱的（全集、合集）只说明「这是一套东西」——音乐合集、丛书全集都这么叫。
# 早先把「合集」当强信号，结果「某歌手 无损专辑合集 APE」被判成了剧集：
# 合集 → 是剧 → 是影视 → 剧集，一路推下去，中间没有任何一步报错。
# 弱词只在已经确定是影视之后，用来分电影还是剧。
_TV_STRONG = re.compile(r"(?<![a-z])(?:complete|series|season|episodes?|"
                        r"miniseries)(?![a-z])|电视剧|连续剧|剧集")
_TV_WEAK = re.compile(r"(?<![a-z])(?:collection|pack)(?![a-z])|全集|合集")

# 年份。四位数，19xx 或 20xx，两边不能再跟数字**也不能粘着字母**。
# 字母那半边是拿真名字试出来的：ILSVRC2012 里的 2012 是数据集编号的一部分，
# 只挡数字的话它会被当成年份。名字里真写年份时前后总有分隔符，
# 而分隔符在 flatten 之后就是空格，所以这个收紧不会误伤。
_YEAR = re.compile(r"(?<![a-z0-9])((?:19|20)\d{2})(?![a-z0-9])")
# 放在方括号或圆括号里的年份基本不会错，优先用它。
_YEAR_BRACKET = re.compile(r"[\[(]((?:19|20)\d{2})[\])]")
# 1920x1080 里的 1920 是宽度不是年份。这个排除必须有：1920 正好落在合法年份
# 区间里，不挡的话每一条写着分辨率尺寸的片子都会被标成 1920 年。
_DIM = re.compile(r"(?<!\d)\d{3,4} ?x ?\d{3,4}(?!\d)")

# 发布组：结尾的 -GROUP，或者开头的 [字幕组]。只用来显示。
_GROUP_TAIL = re.compile(r"-([a-z0-9_@]{2,20})\s*$", re.I)
_GROUP_HEAD = re.compile(r"^\s*[\[【]([^\]】]{2,20})[\]】]")

# 游戏线索分两档，因为可信度差得很远。
#
# 这一档是「不可能是别的东西」的词：没有哪部电影叫 FitGirl，没有哪张专辑叫 .xci。
# 它们出现在名字的任何位置都算数。
_GAME_WORDS = re.compile(
    r"(?<![a-z])(?:fitgirl|dodi|elamigos|steamrip|goldberg|gog|nsp|xci|nsw|"
    r"ps[1-5]|psp|psvita|xbox360|xbox|wii|wiiu|switch|emulator)(?![a-z])|"
    r"免安装|绿色版游戏|单机游戏|游戏合集")

# 这一档是破解组的名字，而它们同时都是普通英文词。拿来匹配整个名字的话，
# `The.Matrix.Reloaded.2003.1080p.BluRay.x264` 直接判成游戏——实测过，
# 文件列表里全是 .mkv 也拦不住，因为游戏词原先无条件排在文件列表证据前面。
# 所以这一档只在**结尾 -GROUP 的位置**上算数：那是发布组该待的地方，
# 出现在片名中间的 Reloaded 就只是个单词。
_GAME_GROUPS = frozenset(
    "codex plaza skidrow reloaded razor1911 empress tenoke "
    "hoodlum prophet flt rune tinyiso darksiders".split())
# linux 前面允许粘字母：rockylinux、linuxmint 这类发行版名字是一个整词，
# 卡死左边界就一个都认不出来。iso 放在这儿是因为它作为扩展名不投票
# （什么都可能是），但写在名字里、而且前面几档都没命中时，软件是最好的猜测——
# 真是影碟镜像的话，片源那一档（DVD/BluRay）早就先命中了。
_SOFT_WORDS = re.compile(
    r"(?<![a-z])(?:keygen|crack|cracked|activator|portable|multilingual|"
    r"incl|setup|installer|x64|x86|amd64|win64|win32|winall|macos|"
    r"ubuntu|debian|centos|android|iso|32bit|64bit)(?![a-z])|"
    r"[a-z]*linux(?![a-z])|破解版|注册机|绿色版|安装版|"
    r"(?<![a-z0-9])v\d+ ?\d*(?: ?\d+)?(?![a-z0-9])")
_MUSIC_WORDS = re.compile(
    r"(?<![a-z])(?:flac|ape|wav|mp3|aac|m4a|dsd|discography|"
    r"vinyl|320kbps|320k|24bit|hi ?res|album|ost)(?![a-z])|"
    r"无损|专辑|单曲|音乐|歌曲|原声")
_BOOK_WORDS = re.compile(
    r"(?<![a-z])(?:epub|mobi|azw3|azw|djvu|cbz|cbr|kindle|ebook|pdf)(?![a-z])|"
    r"电子书|扫描版|全套书|漫画")
# repack 两边都没有，是故意的。影视发布里它是「重新压制」，游戏里是「重打包」，
# 单独出现时什么也证明不了。原先它同时写在游戏和影视两档里，游戏那档排在前面
# 先命中，于是每一条带 REPACK 的影视发布都被判成了游戏——而 REPACK 在 scene
# 命名里极常见，这不是偶发误判，是成批的。中性词就该谁也不投。
_VIDEO_WORDS = re.compile(r"(?<![a-z])(?:mkv|mp4|avi|rmvb|m2ts|hdr|dolby|"
                          r"vision|sdr|imax|proper)(?![a-z])|"
                          r"国语|中字|双语|字幕|蓝光|高清")

# 文件扩展名 -> 大类。这是最硬的证据：名字是人写的，文件列表是真的。
_EXT_FAMILY = {}
for _ext in ("mkv mp4 avi m2ts mov wmv flv rmvb rm webm mpg mpeg vob m4v ts "
             "mts asf 3gp"):
    _EXT_FAMILY[_ext] = "video"
for _ext in "mp3 flac wav ape m4a aac ogg wma dsf dff tta tak alac opus".split():
    _EXT_FAMILY[_ext] = "audio"
for _ext in "epub mobi azw3 azw djvu cbz cbr pdf chm fb2".split():
    _EXT_FAMILY[_ext] = "book"
for _ext in "exe msi dmg pkg apk deb rpm appimage xapk".split():
    _EXT_FAMILY[_ext] = "software"
for _ext in "nsp xci wbfs rvz gcm cso 3ds cia nds gba sfc smc z64 chd gdi".split():
    _EXT_FAMILY[_ext] = "game"
_EXT_FAMILY.update(dict.fromkeys(
    "mkv mp4 avi m2ts mov wmv flv rmvb rm webm mpg mpeg vob m4v ts mts".split(),
    "video"))

# 这些扩展名不投票。它们是跟着别人来的：字幕跟着视频、封面图跟着专辑、
# 说明文档跟着任何东西。让它们参与计数，一个带 30 张剧照的电影会被判成图片集。
# .rar/.zip/.iso 也不投票，但理由不同——它们什么都可能是，
# 投了票等于瞎猜，交给名字里的词去判更准。
_EXT_SKIP = set(
    "nfo txt jpg jpeg png gif bmp webp srt ass ssa sub idx sup md5 sfv url "
    "torrent db ini log cue m3u m3u8 lrc json xml html htm par2 rar zip 7z "
    "iso img bin dat part001 001 002 003 sample".split())

_EXT_RE = re.compile(r"\.([0-9a-z]{1,8})\s*$")


def _family_from_files(filelist: str):
    """
    看文件列表投票。要一个明显的多数（超过七成）才算数，
    否则宁可交给名字去判——一半视频一半音频的包，判成哪边都是错的。
    """
    votes = {}
    seen = 0
    for line in (filelist or "").split("\n"):
        m = _EXT_RE.search(line.strip().lower())
        if not m:
            continue
        ext = m.group(1)
        if ext in _EXT_SKIP:
            continue
        fam = _EXT_FAMILY.get(ext)
        if not fam:
            continue
        seen += 1
        votes[fam] = votes.get(fam, 0) + 1
    if not seen:
        return ""
    top, n = max(votes.items(), key=lambda kv: kv[1])
    return top if n * 10 >= seen * 7 else ""


def _season_episode(flat: str, raw: str):
    """
    返回 (季, 集, 强信号, 弱信号)。0 表示没认出来。

    强信号 = 足以断定这是一部剧；弱信号 = 只说明「这是一套东西」，
    要先知道是影视才用得上。区别见上面 _TV_STRONG / _TV_WEAK 的说明。
    """
    season = episode = 0
    m = _SXE.search(flat)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
    if not season:
        m = _CN_SEASON.search(raw)
        if m:
            season = int(m.group(1))
    if not episode:
        m = _CN_EP.search(raw)
        if m:
            episode = int(m.group(1))
    if not (season or episode):
        m = _NXN.search(flat)
        if m:
            season, episode = int(m.group(1)), int(m.group(2))
    if not episode:
        m = _EP_WORD.search(flat)
        if m:
            episode = int(m.group(1))
    if not season:
        m = _SEASON.search(flat)
        if m:
            season = int(m.group(1))
            # 认出季号之后，光秃秃的 e05 才可信——「S02 E07」是常见写法，
            # 而单独一个 -E5 更可能是发布组名字的尾巴
            if not episode:
                m2 = _EP_BARE.search(flat)
                if m2:
                    episode = int(m2.group(1))
    if not episode:
        m = _BRACKET_EP.search(raw)
        if m:
            episode = int(m.group(1))
    strong = bool(season or episode or _CN_PACK.search(raw)
                  or _TV_STRONG.search(flat) or _TV_STRONG.search(raw))
    weak = bool(_TV_WEAK.search(flat) or _TV_WEAK.search(raw))
    return season, episode, strong, weak


def _year(flat: str, raw: str) -> int:
    """
    先认括号里的，那种基本不会错。剩下的取最后一个——
    片名里带年份的（《2012》《1917》）常见，而技术信息总排在片名后面，
    所以「最后一个合法年份」比「第一个」对得多。
    """
    m = _YEAR_BRACKET.search(raw)
    now = time.localtime().tm_year
    if m and 1900 <= int(m.group(1)) <= now + 1:
        return int(m.group(1))
    # 先把 1920x1080 这类尺寸挖掉，再找年份
    masked = _DIM.sub(" ", flat)
    hits = [int(x) for x in _YEAR.findall(masked)]
    hits = [y for y in hits if 1900 <= y <= now + 1]
    return hits[-1] if hits else 0


def _first(rules, flat):
    for value, pat in rules:
        if pat.search(flat):
            return value
    return ""


# 结尾那一截不是组名的常见情况。光看这一截本身看不出来：
# "WEB-DL" 的 DL、"Blu-Ray" 的 Ray、"x264-10bit" 的 10bit，
# 单拿出来都像个组名，得连着前一个词一起看才认得出。
_NOT_GROUP = set("dl ray rip bit hd sd hdr sdr dts ac3 aac ddp truehd atmos "
                 "amd64 x64 x86 win64 win32 arm64 multi dual sub subs "
                 "proper repack internal final full part cd1 cd2".split())


def _group(raw: str, media: bool = True) -> str:
    """
    media=False 时不认结尾那种 -GROUP 写法。

    「结尾的 -XXX 是发布组」是影音发布圈的习惯，不是通用规则。拿真名字一试
    就露馅了：OpenStreetMap-planet-250101、linuxmint-22-cinnamon-64bit、
    ImageNet-ILSVRC2012-train，全被认成了发布组。这些名字只是带连字符而已。
    开头的 [字幕组] 那种形状特别得多，不受这个限制。
    """
    m = _GROUP_HEAD.search(raw or "")
    if m and not m.group(1).strip().isdigit():
        return m.group(1).strip()
    if not media:
        return ""
    m = _GROUP_TAIL.search((raw or "").strip())
    if not m:
        return ""
    tail = m.group(1).strip(" ._")
    # 去掉前面的数字再比一次，"10bit" "5ch" 这种才挡得住
    bare = tail.lower().lstrip("0123456789")
    if not tail or tail.isdigit() or tail.lower() in _NOT_GROUP or bare in _NOT_GROUP:
        return ""
    # 别把 "-1080p" "-x265" 当成发布组
    if _first(_RES_RULES, flatten(tail)) or _first(_CODEC_RULES, flatten(tail)):
        return ""
    return tail


def _game_group_tail(raw: str) -> bool:
    """名字结尾的 -GROUP 是不是破解组。只认这一个位置，理由见 _GAME_GROUPS。"""
    m = _GROUP_TAIL.search((raw or "").strip())
    return bool(m and flatten(m.group(1)).strip() in _GAME_GROUPS)


def parse(name: str, filelist: str = "") -> dict:
    """
    解析一条。只读不写，没有副作用，随便调。

    返回的字典里，kind / res 是要进库的，其余只用来显示。
    """
    raw = name or ""
    flat = flatten(raw)
    res = _first(_RES_RULES, flat)
    codec = _first(_CODEC_RULES, flat)
    medium = _first(_MEDIUM_RULES, flat)
    season, episode, tv_strong, tv_weak = _season_episode(flat, raw)
    year = _year(flat, raw)

    # 分类：文件列表是硬证据，先问它。名字里的词只在它没话说时才作数。
    fam = _family_from_files(filelist)

    # 游戏这一档要单独处理。游戏包里多半是 exe 加一堆 bin，按扩展名投票会投成
    # 「软件」，所以名字里的游戏线索必须能推翻「软件」——这是原先把游戏词排在
    # 最前面的理由，理由本身是对的，放的位置错了。它只该推翻「软件」和「没结论」：
    # 文件列表里七成以上是 .mkv 的时候，名字里那个词一定是认错了。
    #
    # 第二道闸是清晰度/片源/编码。游戏发布不写 1080p、BluRay、x264，影视发布
    # 几乎一定写。三样里出现任何一样，游戏线索一律不采信——
    # `Dune.2024.REPACK.1080p.WEB-DL` 和 `The.Switch.2010.1080p.BluRay`
    # 都是靠这道闸救回来的。
    if fam in ("", "software") and not (res or medium or codec):
        if _GAME_WORDS.search(flat) or _game_group_tail(raw):
            fam = "game"

    if not fam:
        if res or codec or medium or _VIDEO_WORDS.search(flat) or tv_strong:
            fam = "video"
        elif _MUSIC_WORDS.search(flat):
            fam = "audio"
        elif _BOOK_WORDS.search(flat):
            fam = "book"
        elif _SOFT_WORDS.search(flat):
            fam = "software"

    if fam == "video":
        # 已经确定是影视了，这时「全集/合集」才有意义：一套影视多半是剧。
        kind = "tv" if (tv_strong or tv_weak) else ("movie" if year else "video")
    elif fam == "audio":
        kind = "music"
    elif fam in ("book", "software", "game"):
        kind = fam
    else:
        kind = ""

    return {"kind": kind, "res": res, "year": year, "season": season,
            "episode": episode, "codec": codec, "medium": medium,
            "group": _group(raw, media=bool(res or codec or medium
                                            or kind in ("movie", "tv", "video")))}


def fields(name: str, filelist: str = ""):
    """只要进库的那两样。btindex.upsert 调的是这个。"""
    d = parse(name, filelist)
    return d["kind"], d["res"]


def se_text(season: int, episode: int) -> str:
    if season and episode:
        return "S%02dE%02d" % (season, episode)
    if season:
        return "S%02d" % season
    if episode:
        return "E%02d" % episode
    return ""


def describe(d: dict) -> str:
    """把解析结果拼成一行人话，给命令行和详情页用。"""
    bits = []
    if d["kind"]:
        bits.append(KIND_TEXT.get(d["kind"], d["kind"]))
    if d["res"]:
        bits.append(RES_TEXT.get(d["res"], d["res"]))
    se = se_text(d["season"], d["episode"])
    if se:
        bits.append(se)
    if d["year"]:
        bits.append(str(d["year"]))
    if d["medium"]:
        bits.append(MEDIUM_TEXT.get(d["medium"], d["medium"]))
    if d["codec"]:
        bits.append(CODEC_TEXT.get(d["codec"], d["codec"]))
    if d["group"]:
        bits.append("@" + d["group"])
    return " · ".join(bits)


# --------------------------------------------------------------------------
# 自测用例
# --------------------------------------------------------------------------
# 每条是 (名字, 文件列表, 期望值)。期望值只写要检查的键，
# 没写的键不检查——否则加一个新字段就要把全部用例改一遍。
#
# 这些用例分两类：一类是常见写法，保证基本功能；
# 另一类是踩过的坑（1920x1080 被读成季集、-E5 被读成集数、
# 电影里的 FLAC 被读成音乐专辑），保证它们不再回来。

CASES = [
    # ── 剧集 ────────────────────────────────────────────────────
    ("Some.Show.S02E07.2160p.WEB-DL.DDP5.1.HDR.H.265-GROUP", "",
     {"kind": "tv", "res": "2160p", "season": 2, "episode": 7,
      "codec": "hevc", "medium": "webdl", "group": "GROUP"}),
    # 编码器和格式分开报：remux 没有重新编码，说它用 x264 压过是错的
    ("A.Film.2021.2160p.BluRay.REMUX.AVC.DTS-HD", "",
     {"codec": "h264", "medium": "remux"}),
    ("A.Film.2021.1080p.BluRay.x264-GRP", "", {"codec": "x264"}),
    ("The.Office.US.S01E01.1080p.BluRay.x264-SHORTBREHD", "",
     {"kind": "tv", "res": "1080p", "season": 1, "episode": 1,
      "medium": "bluray", "codec": "x264"}),
    ("Show.Name.S03.COMPLETE.1080p.WEBRip", "",
     {"kind": "tv", "res": "1080p", "season": 3, "episode": 0,
      "medium": "webrip"}),
    ("Old.Show.4x12.DVDRip.XviD", "",
     {"kind": "tv", "season": 4, "episode": 12, "medium": "dvd",
      "codec": "xvid"}),
    ("某某剧 第02季 第15集 1080p 国语中字", "",
     {"kind": "tv", "res": "1080p", "season": 2, "episode": 15}),
    ("某某剧集 全24集 720p", "", {"kind": "tv", "res": "720p"}),
    ("[某字幕组] 某某动画 [12][1080p][简繁内封]", "",
     {"kind": "tv", "res": "1080p", "episode": 12, "group": "某字幕组"}),
    ("Series.Name.Season.2.Episode.5.HDTV.x264", "",
     {"kind": "tv", "season": 2, "episode": 5, "medium": "hdtv"}),

    # ── 电影 ────────────────────────────────────────────────────
    ("Movie.Title.2019.1080p.BluRay.x264-GRP", "",
     {"kind": "movie", "res": "1080p", "year": 2019, "medium": "bluray"}),
    ("Movie Title (2021) 2160p UHD BluRay REMUX HDR", "",
     {"kind": "movie", "res": "2160p", "year": 2021, "medium": "remux"}),
    ("Blade.Runner.2049.2017.1080p.BluRay", "",
     {"kind": "movie", "year": 2017}),            # 片名里的 2049 不是年份
    ("2012.2009.720p.BrRip.x264", "",
     {"kind": "movie", "year": 2009, "res": "720p"}),  # 片名本身就是个年份
    ("某某电影.2019.BluRay.1080p.x264.国语中字", "",
     {"kind": "movie", "res": "1080p", "year": 2019}),
    ("Some.Film.2020.1920x1080.WEB.mkv", "",
     {"kind": "movie", "res": "1080p", "year": 2020,
      "season": 0, "episode": 0}),                # 1920x1080 不是 20 季 10 集
    ("Documentary.2018.4K.HDR.WEB-DL", "",
     {"kind": "movie", "res": "2160p", "year": 2018}),
    ("Concert.Film.2015.1080p.BluRay.FLAC.2.0.x264", "",
     {"kind": "movie", "year": 2015}),            # 带 FLAC 的电影不是音乐

    # ── 分不出是电影还是剧 ──────────────────────────────────────
    ("Random.Clip.1080p.x264", "", {"kind": "video", "res": "1080p"}),

    # ── 音乐 ────────────────────────────────────────────────────
    ("Artist - Album Name (2020) [FLAC 24bit]", "",
     {"kind": "music", "year": 2020}),
    ("某歌手 无损专辑合集 APE", "", {"kind": "music"}),
    ("Band Discography 1985-2005 MP3 320kbps", "", {"kind": "music"}),
    ("Unknown Release", "a/01.flac\na/02.flac\na/cover.jpg",
     {"kind": "music"}),                          # 封面图不参与投票

    # ── 游戏 / 软件 / 图书 ──────────────────────────────────────
    ("Game.Name.v1.2.3.FitGirl.Repack", "", {"kind": "game"}),
    ("Some Game [NSP] Switch", "", {"kind": "game"}),
    ("Game.Name-CODEX", "setup.exe\ndata1.bin\ndata2.bin", {"kind": "game"}),
    ("Adobe Photoshop 2023 v24.0 Win64 Multilingual", "", {"kind": "software"}),
    ("ubuntu-24.04-desktop-amd64", "ubuntu.iso",
     {"kind": "software", "group": ""}),          # amd64 也不是组名
    ("Programming Book Collection EPUB PDF", "", {"kind": "book"}),
    ("某某丛书 全套 扫描版", "", {"kind": "book"}),
    ("Unnamed Pack", "x/a.epub\nx/b.epub\nx/c.mobi", {"kind": "book"}),

    # ── 认不出来的，要老实返回空，不要瞎猜 ──────────────────────
    ("random_stuff_2", "", {"kind": "", "res": "", "year": 0}),
    ("backup", "backup.rar\nbackup.r00\nbackup.r01", {"kind": ""}),

    # ── 单独盯着的坑 ────────────────────────────────────────────
    ("Movie.2019.1080p.WEB-DL.HEVC-E5", "",
     {"episode": 0, "kind": "movie"}),            # -E5 是组名不是集数
    ("Film.1996.1080p.BluRay.x264", "",
     {"year": 1996}),                             # 老片年份要认得
    ("Show.S01E01.1080p", "", {"medium": ""}),    # 没写片源就别编一个
    ("Doc.2018.4K.HDR.WEB-DL", "", {"group": ""}),     # WEB-DL 的 DL 不是组名
    ("A.Film.2020.2160p.BluRay.REMUX-FraMeSToR", "",
     {"group": "FraMeSToR", "medium": "remux"}),       # 这个才是组名

    # ── 拿真名字试出来的三处（见 _group / _YEAR / _SOFT_WORDS 的说明）──
    ("ImageNet-ILSVRC2012-train", "",
     {"year": 0, "group": ""}),                   # 2012 粘在字母上，不是年份
    ("OpenStreetMap-planet-250101.osm.pbf", "",
     {"group": ""}),                              # 带连字符 ≠ 有发布组
    ("linuxmint-22-cinnamon-64bit.iso", "",
     {"kind": "software", "group": ""}),
    ("rockylinux-9.4-x86_64-minimal.iso", "", {"kind": "software"}),
    ("Movie.2019.DVD.ISO", "", {"kind": "movie"}),     # 带 iso 的影碟还是影视
    ("Something.2160p.WEB-DL.and.1080p.version", "",
     {"res": "2160p"}),                           # 两个清晰度取高的

    # ── 游戏词抢在文件列表前面那个坑（见 _GAME_GROUPS / parse 的说明）──
    # 这六条原先全部判成「游戏」，包括文件列表里明摆着是 .mkv 的。
    ("Dune.Part.Two.2024.REPACK.1080p.WEB-DL.x265-CMRG",
     "Dune.Part.Two.2024.mkv\nDune.srt", {"kind": "movie"}),
    ("The.Matrix.Reloaded.2003.1080p.BluRay.x264-AMIABLE",
     "The.Matrix.Reloaded.2003.mkv", {"kind": "movie"}),
    ("Some.Show.S01E02.REPACK.1080p.WEB-DL", "ep.mkv", {"kind": "tv"}),
    ("Oppenheimer.2023.2160p.PLAZA.WEB", "", {"kind": "movie"}),
    ("Avatar.2009.EXTENDED.1080p.BluRay.x264-Elamigos", "", {"kind": "movie"}),
    ("The.Switch.2010.1080p.BluRay.x264", "", {"kind": "movie"}),
    # 反向：真游戏还得判得出来，不能为了修上面那些把这一档修没了
    ("Cyberpunk.2077.v2.13-FitGirl.Repack", "", {"kind": "game"}),
    ("Some.Game.Deluxe.Edition-CODEX", "setup.exe\ndata1.bin\ndata2.bin",
     {"kind": "game"}),                           # 文件列表说「软件」，组名翻盘
    ("Another.Game.v1.4-SKIDROW", "", {"kind": "game"}),
    ("Zelda.Tears.of.the.Kingdom.NSP", "", {"kind": "game"}),
]


def selftest(verbose=False):
    """返回失败列表。btcheck 会调它，所以不能有 print 以外的副作用。"""
    bad = []
    for name, files, want in CASES:
        got = parse(name, files)
        for key, expect in want.items():
            if got.get(key) != expect:
                bad.append((name, key, expect, got.get(key)))
        if verbose:
            print("  %-58s %s" % (name[:58], describe(got)))
    return bad


# --------------------------------------------------------------------------
# 入库：补列、回填
# --------------------------------------------------------------------------

def ensure_parse_columns(conn):
    """
    幂等地把 kind / res / parsed 补到老库上，并建好索引。

    这三列的定义只有这一处，btindex.ensure_columns 是调过来的，不是各写一份。
    上一轮 peers 那两列就差点漂掉（两边各写一份 ALTER，一边建了索引一边没建），
    所以这次一开始就只留一个出处。放在 btparse 这边而不是 btindex 那边，
    是因为 PARSE_VERSION 在这儿——补列和写 parsed 的规矩得挨着。

    和 btindex.ensure_columns 是同一个故事：CREATE TABLE IF NOT EXISTS 不会
    去比对列，所以老库不走这一趟就永远缺这三列。索引必须建在补列之后，
    不能写进 SCHEMA——SCHEMA 跑在补列之前，那里引用新列会把每次开库都炸掉，
    而且只炸老库。

    parsed 存的是解析这条时用的规则版本号。规则改了就 +1，
    backfill 靠 parsed < PARSE_VERSION 找出该重跑的条目，不用全库重扫。
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(torrents)")}
    added = []
    if "kind" not in have:
        conn.execute("ALTER TABLE torrents ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        added.append("kind")
    if "res" not in have:
        conn.execute("ALTER TABLE torrents ADD COLUMN res TEXT NOT NULL DEFAULT ''")
        added.append("res")
    if "parsed" not in have:
        conn.execute("ALTER TABLE torrents ADD COLUMN parsed INTEGER NOT NULL DEFAULT 0")
        added.append("parsed")
    # 索引建成「筛选列 + last_seen」的复合形式，不是光一个筛选列。
    # 理由是实测出来的：浏览全部时默认按最近出现排，SQLite 面对
    # 「WHERE kind=? ORDER BY last_seen DESC」有两条路——用 idx_kind 定位再排序，
    # 或者顺着 idx_last_seen 扫下去逐行验 kind。它选了后者，因为那样不用排序。
    # 分类常见时无所谓，扫二十几行就凑够一页了；分类稀有时就是一路扫到底：
    # 20 万条的库里筛一个只有 5 条的分类要 0.058 秒，而这个时间是跟着库线性涨的，
    # 一亿条上就撞到 8 秒闸了。
    # 复合索引让同一条查询变成一次索引定位加一段顺序读（计划里是
    # SEARCH USING INDEX idx_kind_seen），实测 0.058 秒变 0.00004 秒，
    # 而且不随库长大。
    # 它还顺便顶替了单列索引：WHERE kind=? 和 GROUP BY kind 都能走它的覆盖扫描，
    # 所以不用再单建一个 idx_kind。代价是比单列索引胖两成（20 万条上 3.53 MB
    # 对 2.88 MB），换掉之后总体反而省。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kind_seen ON torrents(kind, last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_res_seen  ON torrents(res, last_seen)")
    # 回填要找 parsed < N 的行。全库回填完之后这一列几乎全是同一个值，
    # 但正是那时候这个索引最有用：没有它，「还有没有没解析的」这个问题
    # 在全都解析过的库上要扫到最后一行才敢回答「没有」。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_parsed ON torrents(parsed)")
    conn.commit()
    return added


BATCH = 5000


def backfill(path, redo=False, limit=0, dry=False, progress=None):
    """
    给库里的条目补上 kind / res。

    默认只处理 parsed < PARSE_VERSION 的（没解析过的，或者用旧规则解析过的）。
    redo=True 是不管三七二十一全部重来，规则大改之后用。

    分批提交，随时可以 Ctrl-C：断了就是做了一半，下次接着来，
    不会留下半个事务。这一步不碰 FTS——kind/res 不进索引正文，
    全文索引一个字节都不用动，所以回填比 btmigrate 那种迁移便宜得多。

    分批必须靠 rowid 游标往前走，不能只写 LIMIT。
    第一版是 "WHERE parsed < V ... LIMIT 5000"：不加 redo 时看着没事，
    因为处理过的行 parsed 变了就自动退出了这个集合；
    但 --redo 的条件是空的，每一批查到的都是同样那前 5000 行，
    外面的计数照样往上加，于是「回填完成 100 万条」打出来了，
    实际只有前 5000 行被反复写了 200 遍。
    """
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        added = ensure_parse_columns(conn)
        cond = "" if redo else "parsed < %d" % PARSE_VERSION
        todo = conn.execute(
            "SELECT count(*) FROM torrents%s"
            % ((" WHERE " + cond) if cond else "")).fetchone()[0]
        if limit:
            todo = min(todo, limit)
        if not todo:
            return {"added": added, "todo": 0, "done": 0, "changed": 0, "kinds": {}}

        where = "WHERE rowid > ?" + ((" AND " + cond) if cond else "")
        done = changed = 0
        last = 0
        kinds = {}
        while done < todo:
            n = min(BATCH, todo - done)
            rows = conn.execute(
                "SELECT rowid, name, filelist, kind, res FROM torrents %s "
                "ORDER BY rowid LIMIT ?" % where, (last, n)).fetchall()
            if not rows:
                break
            updates = []
            for r in rows:
                kind, res = fields(r["name"], r["filelist"])
                kinds[kind or ""] = kinds.get(kind or "", 0) + 1
                if kind != r["kind"] or res != r["res"]:
                    changed += 1
                updates.append((kind, res, PARSE_VERSION, r["rowid"]))
            last = rows[-1]["rowid"]
            if not dry:
                conn.executemany(
                    "UPDATE torrents SET kind=?, res=?, parsed=? WHERE rowid=?",
                    updates)
                conn.commit()
            done += len(rows)
            if progress:
                progress(done, todo)
        return {"added": added, "todo": todo, "done": done,
                "changed": changed, "kinds": kinds}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

def _need_db(path):
    if not os.path.exists(path):
        sys.exit("找不到 %s" % path)


def cmd_try(args):
    for name in args.name:
        d = parse(name, "")
        print(name)
        print("  " + (describe(d) or "（一个字段都没认出来）"))
        print("  进库的两列：kind=%r  res=%r" % (d["kind"], d["res"]))


def cmd_test(args):
    bad = selftest(verbose=args.verbose)
    print("规则自测：%d 条用例，%d 条不通过" % (len(CASES), len(bad)))
    for name, key, want, got in bad:
        print("  %s\n    %s: 期望 %r，实际 %r" % (name, key, want, got))
    sys.exit(1 if bad else 0)


def cmd_backfill(args):
    _need_db(args.db)
    t0 = time.time()

    def show(done, todo):
        print("  %s / %s…" % (format(done, ","), format(todo, ",")))

    r = backfill(args.db, redo=args.redo, limit=args.limit, dry=args.dry_run,
                 progress=show if not args.quiet else None)
    if r["added"]:
        print("已给索引补上列：%s" % "、".join(r["added"]))
    if not r["todo"]:
        print("没有要处理的。所有条目都是用第 %d 版规则解析的。" % PARSE_VERSION)
        return
    if args.dry_run:
        print("预演：看了 %s 条，其中 %s 条的结果和库里现有的不一样。"
              % (format(r["done"], ","), format(r["changed"], ",")))
    else:
        print("回填完成：%s 条，用时 %s 秒，其中 %s 条的值有变化。"
              % (format(r["done"], ","), round(time.time() - t0, 1),
                 format(r["changed"], ",")))
    rank = sorted(r["kinds"].items(), key=lambda kv: -kv[1])
    for kind, n in rank:
        print("  %-8s %s 条" % (KIND_TEXT.get(kind, "未识别"), format(n, ",")))


def cmd_stats(args):
    _need_db(args.db)
    conn = sqlite3.connect(args.db, timeout=30)
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(torrents)")}
        if "kind" not in have:
            print("这个库还没有 kind / res 两列。跑一次 backfill 就有了：")
            print("  py -3.11 btparse.py backfill --db %s" % args.db)
            return
        total = conn.execute("SELECT count(*) FROM torrents").fetchone()[0]
        if not total:
            print("库是空的。")
            return
        todo = conn.execute("SELECT count(*) FROM torrents WHERE parsed < ?",
                            (PARSE_VERSION,)).fetchone()[0]
        print("库里 %s 条，其中 %s 条还没按第 %d 版规则解析过。"
              % (format(total, ","), format(todo, ","), PARSE_VERSION))
        print("\n分类")
        for row in conn.execute(
                "SELECT kind, count(*) n FROM torrents GROUP BY kind ORDER BY n DESC"):
            print("  %-8s %8s 条  %5.1f%%"
                  % (KIND_TEXT.get(row[0], "未识别"), format(row[1], ","),
                     row[1] * 100.0 / total))
        print("\n清晰度")
        for row in conn.execute(
                "SELECT res, count(*) n FROM torrents GROUP BY res ORDER BY n DESC"):
            print("  %-10s %8s 条  %5.1f%%"
                  % (RES_TEXT.get(row[0], "未识别"), format(row[1], ","),
                     row[1] * 100.0 / total))
    finally:
        conn.close()


def cmd_sample(args):
    """
    随便抽几条出来，把名字和解析结果并排打出来。

    这个命令是给人看的，不是给机器看的：解析规则对不对，只有拿真实的名字
    一条条看才知道。自测用例是我挑的，它们必然通过——真实库里的名字才会
    暴露我没想到的写法。
    """
    _need_db(args.db)
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        where = ""
        params = []
        if args.kind is not None:
            where = "WHERE kind = ?"
            params.append(args.kind)
        rows = conn.execute(
            "SELECT name, filelist FROM torrents %s ORDER BY RANDOM() LIMIT ?"
            % where, params + [args.limit]).fetchall()
        if not rows:
            print("没抽到东西。库是空的，或者这个分类下没有条目。")
            return
        for r in rows:
            print(r["name"][:100])
            print("    -> %s" % (describe(parse(r["name"], r["filelist"]))
                                 or "（认不出来）"))
    finally:
        conn.close()


def main():
    try:
        from btcompat import setup_console
        setup_console()
    except ImportError:
        pass

    ap = argparse.ArgumentParser(
        description="把种子名解析成字段：分类、清晰度、季集、年份、片源、编码")
    ap.add_argument("--db", default="bt.db", help="索引路径（默认 bt.db）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_db(parser):
        """
        --db 写在子命令前面或后面都该管用。argparse 默认只认前面，
        而人会自然地写成 `btparse.py backfill --db bt.db`。
        default=SUPPRESS 是关键：不给时子命令干脆不写这个属性，
        顶层那个值才不会被子命令的默认值冲掉。和 btimport 里是同一招。
        """
        parser.add_argument("--db", default=argparse.SUPPRESS, help="索引路径")

    p = sub.add_parser("try", help="解析几个名字看看，不碰数据库")
    p.add_argument("name", nargs="+")
    p.set_defaults(func=cmd_try)

    p = sub.add_parser("test", help="跑规则自测用例")
    p.add_argument("-v", "--verbose", action="store_true", help="把每条用例的结果也打出来")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("backfill", help="给库里的条目补上分类和清晰度")
    add_db(p)
    p.add_argument("--redo", action="store_true",
                   help="不管解析过没有，全部重来（规则大改之后用）")
    p.add_argument("--limit", type=int, default=0, help="最多处理多少条，0 是不限")
    p.add_argument("--dry-run", action="store_true", help="只看会改多少，不真写")
    p.add_argument("-q", "--quiet", action="store_true", help="不打进度")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("stats", help="看解析覆盖率和分类分布")
    add_db(p)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("sample", help="随机抽几条，看解析得对不对")
    add_db(p)
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--kind", help="只抽某个分类（movie/tv/music/…，空串是未识别）")
    p.set_defaults(func=cmd_sample)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
