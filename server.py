#!/usr/bin/env python3
"""Mi Box 手机遥控器 —— 本机桥接服务

手机浏览器打开 http://<Mac 局域网 IP>:8795 就是遥控器。
服务端维持一条长驻的 `adb shell`，把按键直接写进去，
实测单次往返 ~20ms（每次新开 adb shell 要 1.3s，所以必须长驻）。
"""
from __future__ import annotations

import io
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
# launchd/systemd 下的 PATH 往往没有 homebrew，补上再找 adb
os.environ["PATH"] = os.environ.get("PATH", "/usr/bin:/bin") + ":/opt/homebrew/bin:/usr/local/bin"

# Termux 里没有 /tmp：adb 会去写 /tmp/adb.<pid>.log，权限不足直接起不来
if not os.access(os.environ.get("TMPDIR") or "/tmp", os.W_OK):
    for _cand in (os.path.join(os.environ.get("PREFIX", "/nonexistent"), "tmp"),
                  os.path.expanduser("~/.tmp")):
        try:
            os.makedirs(_cand, exist_ok=True)
            if os.access(_cand, os.W_OK):
                os.environ["TMPDIR"] = _cand
                break
        except OSError:
            continue
STATIC_DIR = os.path.join(BASE, "static")
CFG_PATH = os.path.join(BASE, "config.json")
LOG_PATH = os.path.join(BASE, "bridge.log")
SHOT_PATH = os.path.join(BASE, "cache", "screen.png")
SHOT_RAW = os.path.join(BASE, "cache", "screen_raw.png")

DEFAULT_CFG = {
    "bind": "0.0.0.0",
    "port": 8795,
    "active": "mibox",
    # 多设备：kind=adb 走 adb，kind=tizen 留作三星电视（现在仍走 /api/tv/*）
    "devices": [
        {"id": "mibox", "label": "小米盒子", "kind": "adb",
         "serial": "192.168.1.60:5555", "model": "MiBOX4",
         "input_dev": "/dev/input/event0"},
        {"id": "projector", "label": "投影仪", "kind": "adb",
         "serial": "192.168.1.56:5555", "model": "OBE_C3Air",
         "input_dev": "/dev/input/event0", "screencap": "pull"},
    ],
    "tv_ip": "192.168.1.61",
    "tv_port": 8002,
    "tv_token": "",
}

# Android keycode 映射（KEYCODE_*）—— 慢路径：`input keyevent`，每次约 1.3s
KEYS = {
    "POWER": 26, "SLEEP": 223, "WAKEUP": 224,
    "UP": 19, "DOWN": 20, "LEFT": 21, "RIGHT": 22, "OK": 23,
    "BACK": 4, "HOME": 3, "MENU": 82, "INFO": 165,
    "VOL_UP": 24, "VOL_DOWN": 25, "MUTE": 164,
    "PLAY_PAUSE": 85, "PREV": 88, "NEXT": 87,
    "REWIND": 89, "FAST_FORWARD": 90, "STOP": 86,
    "SEARCH": 84, "SETTINGS": 176, "APP_SWITCH": 187,
    "DEL": 67, "ENTER": 66, "ESCAPE": 111,
}

# Linux 键码（Generic.kl 里的映射）—— 快路径：sendevent，每次约 0.15s。
# `input keyevent` 要现起一个 ART 进程，在这台 2016 年的盒子上要 1.3s，
# 按一下等一秒多没法用，所以高频键走内核输入设备。
LINUX_KEYS = {
    "UP": 103, "DOWN": 108, "LEFT": 105, "RIGHT": 106, "OK": 28,
    "BACK": 158, "MENU": 139,
    "VOL_UP": 115, "VOL_DOWN": 114, "MUTE": 113,
    "PLAY_PAUSE": 164, "PREV": 165, "NEXT": 163,
    "SEARCH": 217, "APP_SWITCH": 580,
}
# HOME / POWER 必须走框架（`input keyevent`）：往 aml_keypad 写 KEY_HOME 时
# 系统不理会（实测停在本应用里不动），KEY_POWER 同理不敢赌。
INPUT_DEV = "/dev/input/event0"   # aml_keypad（Generic.kl）

# 小米盒子上实际装的应用（package → 显示名 / 渐变色）
APPS_MIBOX = [
    {"pkg": "com.baipiaozhe.tv", "name": "搜剧TV", "hue": 194},
    {"pkg": "com.mygithub0.tvbox0.osd_kitkat", "name": "影视仓", "hue": 258},
    {"pkg": "com.dstv.a1", "name": "毒舌电影", "hue": 14},
    {"pkg": "com.quark.yun.tv", "name": "夸克TV", "hue": 220},
    {"pkg": "com.hpplay.happyplay.aw.new", "name": "乐播投屏", "hue": 142},
    {"pkg": "com.dangbeimarket", "name": "当贝市场", "hue": 200},
    {"pkg": "com.droidvn.midiaexplorer", "name": "文件管理", "hue": 34},
    {"pkg": "app.orchard.tv", "name": "电视桌面", "hue": 280},
    {"pkg": "com.kjnapk.store", "name": "应用商店", "hue": 100},
]

# 投影仪（大眼橙 OBE C3Air）上装的
APPS_PROJECTOR = [
    {"pkg": "com.baipiaozhe.tv", "name": "搜剧TV", "hue": 194},
    {"pkg": "app.orchard.tv", "name": "桌面", "hue": 280},
    {"pkg": "com.dstv.a1", "name": "毒舌电影", "hue": 14},
    {"pkg": "com.mcat.tv1", "name": "网飞猫", "hue": 8},
    {"pkg": "com.xiaodianshi.tv.yst", "name": "小电视", "hue": 320},
    {"pkg": "com.obe.fileexplorer", "name": "文件浏览", "hue": 34},
    {"pkg": "com.android.tv.settings", "name": "设置", "hue": 210},
]

APPS_BY_ID = {"mibox": APPS_MIBOX, "projector": APPS_PROJECTOR}
APPS = APPS_MIBOX                      # 兼容：默认设备
APP_NAME = {a["pkg"]: a["name"] for a in APPS_MIBOX}

_lock_stdout = threading.Lock()


def log(msg: str) -> None:
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


def adb_server_alive(timeout: float = 8.0) -> bool:
    """本机 adb server 是否还能正常应答。

    设备关机也会让 connect 一直失败，所以不能拿「某台连不上」当重启 server
    的理由——那会把同一台手机上另一台正常设备一起踢下线。
    """
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True,
                             text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return False
    return out.returncode == 0 and "List of devices" in out.stdout


def load_cfg() -> dict:
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CFG_PATH) as fh:
            cfg.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(cfg: dict) -> None:
    try:
        with open(CFG_PATH, "w") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log("写配置失败: %s" % exc)


def _arp_candidates(prefix: str) -> list:
    """从 `arp -a` 里挑出同网段的邻居 IP（不需要打开 socket）。"""
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    ips = re.findall(r"\((\d+\.\d+\.\d+\.\d+)\)", out)
    seen: list = []
    for ip in ips:
        if ip.startswith(prefix + ".") and ip not in seen and not ip.endswith(".255"):
            seen.append(ip)
    return seen


def _serial_online(serial: str, timeout: float = 8.0) -> bool:
    """`adb devices` 里这个 serial 是否处于 device 状态（可用的长连接）。"""
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True,
                             text=True, timeout=timeout).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    for line in out.splitlines()[1:]:
        parts = line.split()
        if parts and parts[0] == serial:
            return len(parts) > 1 and parts[1] == "device"
    return False


def _local_ips() -> set:
    """本机自己的 IPv4 地址。

    扫描时要把自己排除掉：手机自己的 adbd 也开在 5555，`adb connect 自己`
    会在 adb server 里留下一条 unauthorized 传输层，还可能把正在用的
    长连接（小米盒子）挤掉——实测扫网段后盒子立刻掉线就是它干的。
    """
    ips: set = set()
    for cmd in (["ip", "-o", "-4", "addr", "show"], ["ifconfig"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=6).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        ips |= set(re.findall(r"inet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+)", out))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 53))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return ips


def discover_box(known_ip: str = "", want_model: str = "", timeout: float = 0.35) -> str:
    """盒子换了 IP 也能找回来：先查 ARP 表，必要时才轻扫 /24，最后用 getprop 认机型。

    只在连不上已知地址时调用（DHCP 换租约后 IP 会漂）。返回 "ip:5555" 或 ""。

    扫描必须"压着打"：一次性 254 个并发连接会把家里这台老路由器打懵，
    实测连带把小米盒子那条正常的长连接一起打掉（扫描后 6 秒盒子必掉线）。
    所以先看 ARP（零流量），真要扫也限制 8 并发、分批停一下。
    """
    prefix = ".".join((known_ip or "192.168.1.60").split(".")[:3])
    mine = _local_ips()

    def probe(host: str) -> bool:
        s = socket.socket()
        s.settimeout(timeout)
        try:
            return s.connect_ex((host, 5555)) == 0
        finally:
            s.close()

    hits: list = []
    # 1) ARP 表里的邻居：完全不打流量，够用就别扫全网段
    arp = [h for h in _arp_candidates(prefix) if h not in mine]
    for host in arp:
        if probe(host):
            hits.append(host)
    if hits:
        log("ARP 邻居里有 5555：%s" % hits)

    # 2) 没有线索才扫，而且分批：8 并发 + 每批之间歇 80ms
    if not hits:
        hosts = ["%s.%d" % (prefix, i) for i in range(1, 255)]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            for i in range(0, len(hosts), 8):
                batch = hosts[i:i + 8]
                hits.extend(h for h, ok in zip(batch, pool.map(probe, batch)) if ok)
                time.sleep(0.08)
        log("扫描 %s.0/24（%d 台，8 并发）：5555 开放 %s" % (prefix, len(hosts), hits or "无"))

    # launchd 起的进程没有"本地网络"权限时，原始 socket 会瞬间失败（假阴性），
    # 这时改用 ARP 表里的已知邻居，再让 adb 去试（adb 有权限）。
    candidates = list(hits)
    if not candidates:
        candidates = arp
        log("socket 扫描无结果，改用 ARP 候选：%s" % (candidates or "无"))

    # 把自己排除掉，否则会 adb connect 到本机 adbd（见 _local_ips 的说明）
    skipped = [h for h in candidates if h in mine]
    if skipped:
        log("跳过本机地址：%s" % ", ".join(skipped))
        candidates = [h for h in candidates if h not in mine]
    candidates.sort(key=lambda h: int(h.split(".")[-1]))

    known_serial = known_ip if ":" in (known_ip or "") else (known_ip + ":5555" if known_ip else "")
    for host in candidates:
        serial = host + ":5555"
        if serial == known_serial:
            continue          # 就是刚连不上的那台，别再 disconnect/connect 折腾一遍
        try:
            # 正在正常用的设备绝不能 disconnect：那会把它那条长连接一起掐掉。
            # 之前找投影仪时每轮都 disconnect 小米盒子，导致盒子每 5 分钟掉一次线。
            if not _serial_online(serial):
                subprocess.run(["adb", "disconnect", serial], capture_output=True, timeout=6)
                subprocess.run(["adb", "connect", serial], capture_output=True, timeout=8)
            out = subprocess.run(["adb", "-s", serial, "shell", "getprop ro.product.model"],
                                 capture_output=True, text=True, timeout=10)
            model = out.stdout.strip()
            if model and (not want_model or model == want_model
                          or model in want_model or want_model in model):
                log("找到盒子: %s (%s)" % (serial, model))
                return serial
        except (subprocess.SubprocessError, OSError):
            continue
    return ""


class Box:
    """一条长驻 adb shell，负责所有按键/命令。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.id = cfg.get("id", "box")
        self.label = cfg.get("label") or cfg.get("model") or self.id
        self.serial = cfg["serial"]
        self.input_dev = cfg.get("input_dev") or INPUT_DEV
        self.proc = None
        self.buf = bytearray()
        self.cv = threading.Condition()
        self.cmd_lock = threading.Lock()
        self.seq = 0
        self.state = "init"          # init | online | offline
        self.last_err = ""
        self.focus_cache = ("", 0.0)
        self.last_key_ms = 0
        self.exec_out_ok = True
        self.screencap_mode = cfg.get("screencap") or "auto"
        self._info = {}
        self._start()
        threading.Thread(target=self._focus_loop, daemon=True).start()
        threading.Thread(target=self._watch_loop, daemon=True).start()

    def _focus_loop(self) -> None:
        """后台每 4 秒刷新一次当前前台应用。

        `dumpsys window` 要 1s 左右，而且必须走独立进程：在长驻 shell 里跑
        会把按键排队卡在后面。
        """
        while True:
            try:
                out = subprocess.run(
                    ["adb", "-s", self.serial, "shell", "dumpsys window | grep mCurrentFocus"],
                    capture_output=True, text=True, timeout=20)
                m = re.search(r"mCurrentFocus=Window\{[^}]*?\s([A-Za-z0-9_.]+)/", out.stdout)
                if m:
                    self.focus_cache = (m.group(1), time.time())
            except (subprocess.SubprocessError, OSError):
                pass
            time.sleep(4.0)

    def _watch_loop(self) -> None:
        """掉线自动重连；IP 变了自动扫网段找回来，并把新地址写回 config.json。

        扫整个 /24 是 254 个连接，很打扰路由器，所以加了退避：
        正常每 2 分钟扫一次，一直找不到就翻倍，最多 10 分钟。
        """
        last_scan = 0.0
        scan_gap = 300.0
        tick = 0
        fails = 0
        while True:
            # 正常时 12 秒一轮；连不上就退避到最长 60 秒。
            # 一台关机的设备不该让桥接每 12 秒 fork 两个 adb 进程、刷两行日志。
            time.sleep(min(12.0 * (1 + fails // 3), 60.0))
            tick += 1
            if self.state == "online":
                scan_gap = 300.0
                fails = 0
                if tick % 5 == 0:      # 每分钟：心跳 + 补一次全键抬起
                    # 心跳必须有：长驻 shell 断了但进程还没被回收时，
                    # state 会一直停在线，界面看着正常、按键其实全丢。
                    try:
                        self.run("true", timeout=6)
                    except RuntimeError:
                        log("%s 心跳失败，重建连接" % self.label)
                        self.state = "offline"
                        self._kill()
                    self.release_all()
                continue
            if fails == 0:
                log("%s 掉线，开始重连" % self.label)
            elif fails % 10 == 0:
                log("%s 连续 %d 次连不上，继续重试" % (self.label, fails))
            if self.connect():
                log("%s 已恢复连接" % self.label)
                self.release_all()     # 断线期间可能留下按下的键，补发抬起
                fails = 0
                continue
            fails += 1
            # 连续失败本来是为了对付「本机 adb server 卡死」，但一个设备关机
            # 也会触发它：killing server 会把另一台正在用的设备一起踢下线
            # （实测投影仪关着 → 小米盒子每几分钟被断一次）。
            # 所以先只做设备级清理；只有 adb server 本身没响应才全局重启。
            if fails in (3, 6, 12):
                log("%s 连续 %d 次连不上，清理该设备的连接" % (self.label, fails))
                try:
                    subprocess.run(["adb", "disconnect", self.serial],
                                   capture_output=True, timeout=10)
                except (subprocess.SubprocessError, OSError) as exc:
                    log("adb disconnect 失败: %s" % exc)
                if not adb_server_alive():
                    log("adb server 无响应，整机重启")
                    try:
                        subprocess.run(["adb", "kill-server"], capture_output=True, timeout=10)
                        subprocess.run(["adb", "start-server"], capture_output=True, timeout=20)
                    except (subprocess.SubprocessError, OSError) as exc:
                        log("重启 adb server 失败: %s" % exc)
            if time.time() - last_scan < scan_gap:
                continue
            last_scan = time.time()
            found = discover_box(self.serial, self.cfg.get("model", ""))
            if found and found != self.serial:
                self.serial = found
                self.cfg["serial"] = found
                save_cfg(self.cfg)
                self.connect()
            else:
                scan_gap = min(scan_gap * 2, 900.0)

    # ---------- 进程管理 ----------
    def _start(self) -> None:
        self._kill()
        try:
            self.proc = subprocess.Popen(
                ["adb", "-s", self.serial, "shell"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0,
            )
        except FileNotFoundError:
            self.state = "offline"
            self.last_err = "adb 未安装"
            return
        threading.Thread(target=self._reader, daemon=True).start()
        # 不能因为「adb shell 进程起来了」就标 online：进程能 fork 不等于设备连上了。
        # 真正的连接证据是第一条命令的回显——run() 收到结束标记才置 online。
        # 之前这里直接置 online，设备关机时会短暂显示在线、白等一次 8 秒超时。
        self.state = "init"
        log("adb shell 进程已启动，等待握手 (%s)" % self.serial)

    def _kill(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.kill()
        except OSError:
            pass

    def _reader(self) -> None:
        p = self.proc
        while True:
            try:
                chunk = p.stdout.read(4096)
            except (ValueError, OSError):
                break
            if not chunk:
                break
            with self.cv:
                self.buf += chunk
                self.cv.notify_all()
        with self.cv:
            self.state = "offline"
            self.cv.notify_all()

    # ---------- 执行 ----------
    def run(self, cmd: str, timeout: float = 6.0) -> str:
        with self.cmd_lock:
            for attempt in (1, 2):
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                    if self.proc is None:
                        raise RuntimeError(self.last_err or "adb 启动失败")
                self.seq += 1
                i = self.seq
                s_mark = "S%d__" % (500000 + i)
                e_mark = "E%d__" % (600000 + i)
                # adb shell 带 pty，会把命令行回显出来；标记用算术展开生成，
                # 命令行里只有 "$((500000+7))"，输出才是 S500007__，不会误匹配。
                payload = ("printf 'S%s__\\n' \"$((500000+" + str(i) + "))\"; "
                           + cmd + "; "
                           "printf 'E%s__\\n' \"$((600000+" + str(i) + "))\"\n")
                with self.cv:
                    self.buf.clear()
                try:
                    self.proc.stdin.write(payload.encode())
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    self._kill()
                    continue
                deadline = time.time() + timeout
                with self.cv:
                    while True:
                        text = self.buf.decode("utf-8", "replace")
                        a = text.find(s_mark)
                        b = text.find(e_mark)
                        if a >= 0 and b > a:
                            self.state = "online"
                            out = text[a + len(s_mark):b]
                            return out.replace("\r\n", "\n").strip("\n")
                        left = deadline - time.time()
                        if left <= 0:
                            self.last_err = "命令超时: %s" % cmd[:60]
                            self._kill()
                            break
                        self.cv.wait(left)
                if attempt == 2:
                    raise RuntimeError(self.last_err or "命令超时")
            raise RuntimeError("命令执行失败")

    def connect(self) -> bool:
        """把 adb 传输层接上（盒子重启/换 IP 后需要）。"""
        try:
            # 先看当前状态：残留在 offline / unauthorized 的传输层，
            # 光 connect 是修不好的——adb 会回 "already connected"，然后继续连不上。
            try:
                devs = subprocess.run(["adb", "devices"], capture_output=True,
                                      text=True, timeout=10).stdout
                st = ""
                for line in devs.splitlines()[1:]:
                    parts = line.split()
                    if parts and parts[0] == self.serial:
                        st = parts[1] if len(parts) > 1 else ""
                if st in ("offline", "unauthorized"):
                    log("%s 残留状态 %s，先断开" % (self.serial, st))
                    subprocess.run(["adb", "disconnect", self.serial],
                                   capture_output=True, text=True, timeout=10)
            except (subprocess.SubprocessError, OSError):
                pass
            out = subprocess.run(["adb", "connect", self.serial],
                                 capture_output=True, text=True, timeout=12)
            msg = (out.stdout or out.stderr).strip()
            log("adb connect: %s" % msg)
            # adb 的失败措辞有好几种：cannot connect / failed to connect /
            # unable to connect / Connection refused / No route to host。
            # 以前只认 "cannot"，其余全被当成功，白等一次 8 秒超时、还把状态
            # 短暂标成在线（页面显示正常但按键全丢）。
            low = msg.lower()
            if any(w in low for w in ("cannot", "failed", "unable", "refused",
                                      "no route", "10061", "10060")):
                self.state = "offline"
                self.last_err = msg
                return False
            # 最终以 adb devices 的传输层状态为准：只有 device 才算真连上
            if not self._transport_ready():
                self.state = "offline"
                self.last_err = "传输层不是 device 状态"
                return False
        except (subprocess.SubprocessError, OSError) as exc:
            self.state = "offline"
            self.last_err = str(exc)
            return False
        try:
            self.run("echo ok", timeout=8)
            return True
        except RuntimeError:
            self.state = "offline"
            return False

    def _transport_ready(self) -> bool:
        """adb devices 里这台设备是否真的处于 device 状态（而不只是已登记）。"""
        return _serial_online(self.serial)

    # ---------- 遥控动作 ----------
    def key(self, name: str, n: int = 1) -> None:
        n = max(1, min(int(n), 12))
        t0 = time.time()
        code = LINUX_KEYS.get(name)
        if code is not None:
            try:
                self._sendevent(code, n)
                self.last_key_ms = int((time.time() - t0) * 1000)
                return
            except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
                log("sendevent 失败, 退回 input keyevent: %s" % exc)
                self.release_all()   # 补发抬起，别让键卡住
        kc = KEYS.get(name)
        if kc is None:
            raise ValueError("未知按键: %s" % name)
        self.run("input keyevent " + " ".join([str(kc)] * n), timeout=10)
        self.last_key_ms = int((time.time() - t0) * 1000)

    def release_all(self) -> None:
        """把所有按键补一个 up。

        为什么需要：按键是「按下 / 抬起」两个事件，如果中间 adb 连接断了
        （换 Wi-Fi、盒子忙、命令超时），抬起事件就永远发不出去，系统会认为
        某个键一直按着 → 电视上就会出现"没人碰它、App 自己一个个弹出来"。
        """
        codes = sorted(set(LINUX_KEYS.values()))
        chain = "".join("sendevent %s 1 %d 0; " % (self.input_dev, c) for c in codes)
        chain += "sendevent %s 0 0 0; echo __REL__" % self.input_dev
        try:
            subprocess.run(["adb", "-s", self.serial, "shell", chain],
                           capture_output=True, text=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            pass

    def _sendevent(self, code: int, n: int = 1) -> None:
        """直接往内核输入设备写按键：0.15s 一次，`input keyevent` 要 1.3s。

        注意：必须每次开一个独立的 `adb shell <cmd>`（非 pty），
        在长驻 pty shell 里跑 sendevent 会卡住不出结果。
        """
        chain = "".join(
            "sendevent %s 1 %d 1; sendevent %s 0 0 0; "
            "sendevent %s 1 %d 0; sendevent %s 0 0 0; "
            % (self.input_dev, code, self.input_dev, self.input_dev, code, self.input_dev)
            for _ in range(n)
        )
        out = subprocess.run(["adb", "-s", self.serial, "shell", chain + "echo __EV__"],
                             capture_output=True, text=True, timeout=8)
        if "__EV__" not in out.stdout:
            raise RuntimeError("sendevent 无响应: %s" % (out.stderr or out.stdout).strip()[:80])

    def text(self, s: str) -> None:
        s = (s or "").strip()
        if not s:
            return
        # input text 只吃 ASCII；空格用 %s 转义，单引号走 shell 转义
        safe = s.replace("'", "'\\''").replace(" ", "%s")
        self.run("input text '%s'" % safe, timeout=8)

    def launch(self, pkg: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.]+", pkg or ""):
            raise ValueError("非法包名")
        self.run("monkey -p %s -c android.intent.category.LAUNCHER 1" % pkg, timeout=10)

    def focus(self) -> str:
        out = self.run("dumpsys window | grep mCurrentFocus", timeout=8)
        m = re.search(r"mCurrentFocus=Window\{[^}]*?\s([A-Za-z0-9_.]+)/", out)
        return m.group(1) if m else ""

    def current_pkg(self) -> str:
        pkg, ts = self.focus_cache
        if time.time() - ts < 8.0:
            return pkg
        try:
            pkg = self.focus()
        except RuntimeError:
            pkg = ""
        self.focus_cache = (pkg, time.time())
        return pkg

    def info(self) -> dict:
        """机型/SDK — 只查一次，之后走缓存（串行 shell 上每次查询都要 20ms）。"""
        if not self._info:
            self._info = {
                "model": self.run("getprop ro.product.model", timeout=6).strip(),
                "sdk": self.run("getprop ro.build.version.sdk", timeout=6).strip(),
                "release": self.run("getprop ro.build.version.release", timeout=6).strip(),
            }
        return self._info

    def screencap(self) -> bytes:
        raw_path = os.path.join(BASE, "cache", "screen_raw_%s.png" % self.id)
        out_path = os.path.join(BASE, "cache", "screen_%s.png" % self.id)
        os.makedirs(os.path.dirname(raw_path), exist_ok=True)
        t0 = time.time()
        raw = b""
        # 快路径：exec-out（小米盒子 ~5s）。大眼橙 MT5862 上 exec-out 会被
        # "Init wrapper" 之类的启动日志污染、还要 28s，所以配置里可以直接写
        # "screencap": "pull" 跳过它；自动模式也会在失败后记住不再用。
        if self.screencap_mode != "pull" and self.exec_out_ok:
            try:
                with _lock_stdout:
                    with open(raw_path, "wb") as fh:
                        subprocess.run(["adb", "-s", self.serial, "exec-out", "screencap", "-p"],
                                       stdout=fh, timeout=8, check=True)
                raw = open(raw_path, "rb").read()
                if not raw.startswith(b"\x89PNG"):
                    self.exec_out_ok = False
            except (subprocess.SubprocessError, OSError):
                self.exec_out_ok = False
                raw = b""
        if not raw.startswith(b"\x89PNG"):
            # 慢路径：设备上落盘再 pull（实测 1~2s，稳）
            remote = "/sdcard/.tvremote_%s.png" % self.id
            for attempt in (1, 2):
                subprocess.run(["adb", "-s", self.serial, "shell", "screencap -p " + remote],
                               capture_output=True, timeout=40)
                if os.path.exists(raw_path):
                    os.remove(raw_path)
                subprocess.run(["adb", "-s", self.serial, "pull", remote, raw_path],
                               capture_output=True, timeout=60)
                raw = open(raw_path, "rb").read() if os.path.exists(raw_path) else b""
                if raw.startswith(b"\x89PNG"):
                    break
                log("落盘截屏第 %d 次失败（%d 字节），重试" % (attempt, len(raw)))
                time.sleep(0.8)
            subprocess.run(["adb", "-s", self.serial, "shell", "rm -f " + remote],
                           capture_output=True, timeout=20)
        if not raw.startswith(b"\x89PNG"):
            raise RuntimeError("截屏失败")
        out = raw
        try:  # 缩到 720 宽，手机端加载快很多
            from PIL import Image
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            w = 720
            img = img.resize((w, int(img.height * w / img.width)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82, optimize=True)
            out = buf.getvalue()
        except Exception as exc:  # PIL 不可用时退回原 PNG
            log("缩图失败，直出 PNG: %s" % exc)
        with open(out_path, "wb") as fh:
            fh.write(out)
        log("截屏 %.1fs, %d KB" % (time.time() - t0, len(out) // 1024))
        return out


def _norm_cfg(cfg: dict) -> dict:
    """老配置（只有 serial/model）自动迁移成 devices 列表。"""
    if not cfg.get("devices"):
        cfg["devices"] = [{
            "id": "mibox",
            "label": "小米盒子",
            "kind": "adb",
            "serial": cfg.get("serial", "192.168.1.60:5555"),
            "model": cfg.get("model", "MiBOX4"),
            "input_dev": cfg.get("input_dev", "/dev/input/event0"),
        }]
    ids = [d["id"] for d in cfg["devices"] if d.get("kind", "adb") == "adb"]
    if cfg.get("active") not in ids:
        cfg["active"] = ids[0]
    return cfg


CFG = _norm_cfg(load_cfg())

# 每台 adb 设备一条长驻 adb shell；切换时只换 BOX 指向，进程都留着（切回来是秒回）
BOXES = {}
for _d in CFG["devices"]:
    if _d.get("kind", "adb") != "adb":
        continue
    _dcfg = dict(CFG)
    _dcfg.update(_d)
    BOXES[_d["id"]] = Box(_dcfg)

BOX = BOXES[CFG["active"]]


def set_active(dev_id: str) -> bool:
    """切换当前控制的设备。切走前把旧设备的键全松开，防止卡键。"""
    global BOX
    if dev_id not in BOXES:
        return False
    if BOXES[dev_id] is not BOX:
        try:
            BOX.release_all()
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass
    CFG["active"] = dev_id
    BOX = BOXES[dev_id]
    save_cfg(CFG)
    log("切换设备 → %s (%s)" % (BOX.label, BOX.serial))
    return True


def apps_of(box) -> list:
    return APPS_BY_ID.get(box.id, APPS_MIBOX)


def app_name_of(box, pkg: str) -> str:
    if not pkg:
        return "—"
    for a in apps_of(box):
        if a["pkg"] == pkg:
            return a["name"]
    return pkg.split(".")[-1]


# 三星电视（Tizen 8002 加密通道，第一次要在电视上点「允许」拿 token）
try:
    from samsung import SamsungTV
    TV = SamsungTV(CFG.get("tv_ip", ""), CFG.get("tv_token", ""),
                   port=int(CFG.get("tv_port", 8002)))
    TV.start_keepalive()
except Exception as _exc:          # 没装/没配置也不影响盒子遥控
    TV = None
    log("三星电视模块不可用: %s" % _exc)


def tv_save_token() -> None:
    if TV and TV.token and TV.token != CFG.get("tv_token"):
        CFG["tv_token"] = TV.token
        save_cfg(CFG)
        log("保存电视 token: %s" % TV.token)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TVRemote/1.0"

    # ---------- 工具 ----------
    def _json(self, obj, code=200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 65536:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, TypeError):
            return {}

    def log_message(self, fmt, *args):
        pass

    def handle_one_request(self):
        # 手机上浏览器轮询会频繁断开/重连，还有设备会用 https 误连这个端口，
        # 这些噪音不该把日志刷满
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ssl.SSLError, OSError):
            self.close_connection = True

    # ---------- 路由 ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/status":
            return self.api_status()
        if path == "/api/shot":
            return self.api_shot()
        if path == "/api/apps":
            return self._json({"apps": apps_of(BOX), "device": BOX.id})
        if path == "/api/tv/status":
            return self._json({"ok": True, "ip": CFG.get("tv_ip", ""),
                               "tv": TV.info() if TV else {"state": "unavailable"}})
        return self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._body()
        try:
            if path == "/api/key":
                BOX.key(str(data.get("key", "")), int(data.get("n", 1) or 1))
                return self._json({"ok": True})
            if path == "/api/text":
                BOX.text(str(data.get("text", "")))
                return self._json({"ok": True})
            if path == "/api/app":
                pkg = str(data.get("pkg", ""))
                log("开应用 %s ← %s" % (pkg, self.client_address[0]))
                BOX.launch(pkg)
                return self._json({"ok": True, "pkg": pkg})
            if path == "/api/connect":
                ok = BOX.connect()
                return self._json({"ok": ok}, 200 if ok else 503)
            if path == "/api/switch":
                dev = str(data.get("id", ""))
                if not set_active(dev):
                    raise ValueError("没有这个设备: %s" % (dev or "?"))
                try:
                    ok = BOX.connect()
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    ok = False
                return self._json({"ok": ok, "device": BOX.id,
                                   "label": BOX.label, "model": BOX.cfg.get("model", ""),
                                   "serial": BOX.serial})
            if path == "/api/discover":
                found = discover_box(BOX.serial, BOX.cfg.get("model", ""))
                if found:
                    BOX.serial = found
                    BOX.cfg["serial"] = found
                    save_cfg(BOX.cfg)
                    BOX.connect()
                return self._json({"found": found}, 200 if found else 404)
            if path == "/api/tv/key":
                if TV is None:
                    raise ValueError("电视模块不可用")
                TV.key(str(data.get("key", "")))
                tv_save_token()
                return self._json({"ok": True})
            if path == "/api/tv/pair":
                if TV is None:
                    raise ValueError("电视模块不可用")
                ok = TV.connect(wait=90)
                tv_save_token()
                return self._json({"ok": ok, "token": TV.token, "error": TV.last_error},
                                  200 if ok else 503)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            log("命令失败: %s" % exc)
            return self._json({"ok": False, "error": str(exc)}, 503)
        return self._json({"ok": False, "error": "not found"}, 404)

    # ---------- API ----------
    def api_status(self):
        t0 = time.time()
        online = BOX.state == "online"
        if online:
            # 只在自认为在线时才做心跳；设备关着的时候别让页面等 20 秒
            try:
                BOX.run("echo ok", timeout=6)
                online = True
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                online = False
                BOX.last_err = str(exc)
        pkg = BOX.focus_cache[0] if online else ""
        model = ""
        if online:
            try:
                model = BOX.info().get("model", "")
            except RuntimeError:
                pass
        devices = [{
            "id": b.id,
            "label": b.label,
            "model": b.cfg.get("model", ""),
            "serial": b.serial,
            "online": b.state == "online",
        } for b in BOXES.values()]
        self._json({
            "online": online,
            "model": model,
            "serial": BOX.serial,
            "device": BOX.id,
            "device_label": BOX.label,
            "devices": devices,
            "latency_ms": int((time.time() - t0) * 1000),
            "key_ms": BOX.last_key_ms,
            "current_pkg": pkg,
            "current_name": app_name_of(BOX, pkg),
            "error": "" if online else (BOX.last_err or "设备未连接"),
            "apps": apps_of(BOX),
        })

    def api_shot(self):
        try:
            data = BOX.screencap()
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            log("截屏失败: %s" % exc)
            return self._json({"ok": False, "error": str(exc)}, 503)
        ctype = "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 静态文件 ----------
    def serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            full = os.path.join(STATIC_DIR, "index.html")
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".webmanifest": "application/manifest+json",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._json({"ok": False, "error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    cfg = CFG
    os.makedirs(os.path.join(BASE, "cache"), exist_ok=True)
    log("启动桥接: %s:%s → %s" % (cfg["bind"], cfg["port"],
                                 ", ".join("%s=%s" % (b.label, b.serial) for b in BOXES.values())))
    # 先把 HTTP 服务挂上（connect 可能要等十几秒，别让页面打不开）
    srv = ThreadingHTTPServer((cfg["bind"], int(cfg["port"])), Handler)
    srv.daemon_threads = True

    def _boot_connect() -> None:
        for b in BOXES.values():
            try:
                if b.connect():
                    b.release_all()   # 启动时先把所有键松开，防上次异常退出留下卡键
                else:
                    log("%s 首次连接失败，后台会自动重试/找设备" % b.label)
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                log("%s 连接异常: %s" % (b.label, exc))

    threading.Thread(target=_boot_connect, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
