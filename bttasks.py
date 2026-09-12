#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bttasks —— 后台任务管理：让网页能启动爬虫、跑导入、做维护

设计上最要紧的一条：**命令行是拼装出来的，不是拼接出来的**。

网页一旦能启动进程，就等于开了一个执行入口。所以这里不接受任何形式的
「命令字符串」，只接受一个白名单里的任务种类加若干具名参数；argv 由本模块
自己按固定模板组装，数字全部 int() 后夹到合理区间，路径和地址作为独立的
argv 元素传递，全程 shell=False。调用方就算想传一段 shell 命令进来也没有位置可放。

另外读子进程输出必须显式 encoding="utf-8"：Windows 上默认会用区域编码（GBK）
去解码，而各脚本都把自己的输出设成了 UTF-8，两边对不上会在读取线程里抛
UnicodeDecodeError，然后 stdout 变成 None。这个坑在 btcheck 里踩过一次。
"""

import os
import re
import subprocess
import sys
import threading
import time
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from btcompat import BUILD
except ImportError:
    BUILD = "?"
MAX_LINES = 400          # 每个任务保留多少行输出


def _clamp(value, low, high, default):
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def build_argv(kind, params, db):
    """
    按任务种类组装 argv。这是唯一能产生命令行的地方，模板写死。
    返回 (argv, 人话描述)。种类不认识就抛 ValueError。
    """
    params = params or {}
    py = [sys.executable]

    if kind == "crawler":
        port = _clamp(params.get("port"), 1, 65535, 6881)
        workers = _clamp(params.get("workers"), 1, 200, 40)
        lookup_workers = _clamp(params.get("lookup_workers"), 1, 200, 30)
        rate = _clamp(params.get("rate"), 1, 500, 30)
        argv = py + ["dhtmeta.py", "--sniff", "--db", db,
                     "--port", str(port), "--workers", str(workers),
                     "--rate", str(rate), "--try-peers", "12"]
        desc = "DHT 爬虫，UDP %d" % port
        if params.get("with_lookup"):
            argv += ["--with-lookup", "--lookup-workers", str(lookup_workers)]
            desc += "，带主动查询"
        return argv, desc

    if kind == "import":
        source = params.get("source")
        limit = _clamp(params.get("limit"), 1, 500000, 5000)
        timeout = _clamp(params.get("timeout"), 5, 600, 60)
        common = ["--db", db, "--limit", str(limit), "--timeout", str(timeout)]
        proxy = (params.get("proxy") or "").strip()
        if proxy:
            common += ["--proxy", proxy]

        if source == "ia":
            argv = py + ["btimport.py", "ia"] + common
            q = (params.get("query") or "").strip()
            if q:
                argv += ["--query", q]
            return argv, "从互联网档案馆导入"

        if source == "academic":
            argv = py + ["btimport.py", "academic"] + common
            return argv, "从 Academic Torrents 导入"

        if source == "folder":
            path = (params.get("path") or "").strip()
            if not path or not os.path.isdir(path):
                raise ValueError("目录不存在：%s" % (path or "(空)"))
            return py + ["btimport.py", "folder", path] + common, "导入本地种子文件"

        if source == "torznab":
            endpoint = (params.get("endpoint") or "").strip()
            if "|" not in endpoint or not endpoint.startswith("http"):
                raise ValueError('Torznab 地址格式应为 "http://...|你的APIKEY"')
            argv = py + ["btimport.py", "torznab", endpoint] + common
            argv += ["--pages", str(_clamp(params.get("pages"), 1, 200, 5)),
                     "--delay", str(_clamp(params.get("delay"), 0, 60, 2))]
            q = (params.get("query") or "").strip()
            if q:
                argv += ["--query", q]
            if params.get("fetch_torrents"):
                argv += ["--fetch-torrents"]
            return argv, "从 Torznab 导入"

        raise ValueError("不认识的导入来源：%r" % source)

    if kind == "maint":
        action = params.get("action")
        table = {
            "analyze": (["btprune.py", "--db", db, "analyze"], "索引体检"),
            "verify":  (["btprune.py", "--db", db, "verify", "--fix"], "修复索引一致性"),
            "vacuum":  (["btprune.py", "--db", db, "vacuum"], "整理磁盘空间"),
            "peers":   (["btpeers.py", "--db", db, "scan", "--limit",
                         str(_clamp(params.get("limit"), 1, 5000, 200))], "实测做种情况"),
            "enrich":  (["btenrich.py", "--db", db, "--limit",
                         str(_clamp(params.get("limit"), 1, 5000, 200))], "补全文件列表"),
            # 解析不联网也不删东西，所以不设 limit：一次跑完省得人点好几遍。
            # 一百万条大约两分钟，中途停掉也没事，下次接着上次的来
            "parse":   (["btparse.py", "backfill", "--db", db], "解析名字"),
        }
        if action not in table:
            raise ValueError("不认识的维护动作：%r" % action)
        args, desc = table[action]
        return py + args, desc

    raise ValueError("不认识的任务种类：%r" % kind)


class Task:
    def __init__(self, kind, argv, desc, panel=None):
        self.kind = kind
        # 任务槽（kind）和界面上的面板不是一一对应的：本地扫描和网络导入共用
        # 同一个槽（同时只跑一个，避免两个进程抢着写库），但在界面上是两块。
        # 记下是从哪块发起的，日志才不会同时在两块里滚。
        self.panel = panel or kind
        self.argv = argv
        self.desc = desc
        self.lines = deque(maxlen=MAX_LINES)
        self.started = time.time()
        self.rc = None
        self.proc = None
        self._lock = threading.Lock()

    @staticmethod
    def _safe_cmd(argv):
        """
        把实际命令行拼成一行给人看。Torznab 地址里的 API Key 打码——
        日志会一直留在页面上，没必要把它晾着。
        """
        out = []
        for a in argv[1:]:
            out.append(re.sub(r"\|[^|\s]{6,}$", "|***", a))
        return " ".join(out)

    def run(self):
        # 日志第一行永远是构建号 + 真实命令行。
        # 「我跑的是哪一版」「--proxy 到底传没传」这两个问题，看一眼就知道，
        # 不用再靠输出里有没有某句话去反推。
        self.lines.append("[构建 %s] %s" % (BUILD, self._safe_cmd(self.argv)))
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"      # 让子进程从一开始就用 UTF-8
        env["PYTHONUNBUFFERED"] = "1"          # 否则输出会卡在缓冲区里，页面上看不到进展
        self.proc = subprocess.Popen(
            self.argv, cwd=HERE, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                with self._lock:
                    self.lines.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        finally:
            # 用 wait 而不是 poll：stdout 读完时进程往往还没完全退出，
            # poll() 这时返回 None，页面上就显示成「退出码 None」
            try:
                self.rc = self.proc.wait(timeout=10)
            except Exception:
                self.rc = self.proc.poll()
            with self._lock:
                self.lines.append("—— 任务结束，退出码 %s ——" % self.rc)

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if not self.running:
            return False
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()          # 不肯走就强制
        except OSError:
            pass
        return True

    def snapshot(self, tail=200):
        with self._lock:
            lines = list(self.lines)[-tail:]
        return {"kind": self.kind, "panel": self.panel, "desc": self.desc,
                "running": self.running,
                "rc": self.rc, "elapsed": int(time.time() - self.started),
                "lines": lines}


class TaskManager:
    """每种任务同时只允许一个。爬虫开两份会抢端口，导入开两份会互相拖慢。"""

    def __init__(self, db_path):
        self.db = db_path
        self.tasks = {}
        self.lock = threading.Lock()

    def start(self, kind, params):
        argv, desc = build_argv(kind, params, self.db)
        with self.lock:
            cur = self.tasks.get(kind)
            if cur and cur.running:
                raise RuntimeError("已经有一个%s在跑了，先停掉再启动" %
                                   {"crawler": "爬虫", "import": "导入",
                                    "maint": "维护任务"}.get(kind, "任务"))
            panel = ("local" if kind == "import"
                     and (params or {}).get("source") == "folder" else kind)
            task = Task(kind, argv, desc, panel)
            self.tasks[kind] = task
        task.run()
        return task

    def stop(self, kind):
        with self.lock:
            task = self.tasks.get(kind)
        return task.stop() if task else False

    def status(self, tail=200):
        with self.lock:
            tasks = dict(self.tasks)
        return {k: t.snapshot(tail) for k, t in tasks.items()}

    def stop_all(self):
        for kind in list(self.tasks):
            self.stop(kind)
