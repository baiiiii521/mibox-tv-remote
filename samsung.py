#!/usr/bin/env python3
"""三星电视（Tizen）局域网遥控 —— 纯标准库 WebSocket 实现

三星 2016+ 的电视在 8001 端口开了 WebSocket 遥控接口：
  ws://<ip>:8001/api/v2/channels/samsung.remote.control?name=<base64名字>
第一次连会弹「允许连接吗」，用户用三星遥控器点允许，会回一个 token，
之后带上 token 就不再弹窗。

不依赖第三方库，方便在 Termux 里跑。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import socket
import struct
import threading
import time

TV_KEYS = {
    "POWER": "KEY_POWER", "VOL_UP": "KEY_VOLUP", "VOL_DOWN": "KEY_VOLDOWN", "MUTE": "KEY_MUTE",
    "UP": "KEY_UP", "DOWN": "KEY_DOWN", "LEFT": "KEY_LEFT", "RIGHT": "KEY_RIGHT", "OK": "KEY_ENTER",
    "BACK": "KEY_RETURN", "HOME": "KEY_HOME", "MENU": "KEY_MENU", "GUIDE": "KEY_GUIDE",
    "INFO": "KEY_INFO", "SOURCE": "KEY_SOURCE", "TOOLS": "KEY_TOOLS", "EXIT": "KEY_EXIT",
    "CH_UP": "KEY_CHUP", "CH_DOWN": "KEY_CHDOWN",
    "PLAY": "KEY_PLAY", "PAUSE": "KEY_PAUSE", "STOP": "KEY_STOP",
    "REWIND": "KEY_REWIND", "FAST_FORWARD": "KEY_FF",
    "HDMI": "KEY_HDMI", "TV": "KEY_TV",
}
for _i in range(10):
    TV_KEYS["NUM_%d" % _i] = "KEY_%d" % _i


def wake_on_lan(mac: str, ip: str = "", rounds: int = 6) -> None:
    """发 WoL 魔术包（6×FF + 16×MAC）。

    电视在待机时 IP 栈是关的，ARP 里没有它，单播发不进去，必须走广播；
    所以同时发 255.255.255.255 和本网段广播（192.168.1.255），端口 9 和 7。
    Android/Termux 对 255.255.255.255 有时会丢，网段广播更可靠。
    """
    clean = (mac or "").replace("-", ":").replace(".", "").strip()
    parts = clean.split(":")
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        raise ValueError("MAC 格式不对: %r" % mac)
    macb = bytes(int(p, 16) for p in parts)
    pkt = b"\xff" * 6 + macb * 16

    targets = {("255.255.255.255", 9), ("255.255.255.255", 7)}
    if ip:
        targets.add((ip.rsplit(".", 1)[0] + ".255", 9))
        targets.add((ip.rsplit(".", 1)[0] + ".255", 7))
        targets.add((ip, 9))          # 有的固件也认单播
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for _ in range(rounds):
            for host, port in targets:
                try:
                    sock.sendto(pkt, (host, port))
                except OSError:
                    pass
            time.sleep(0.2)
    finally:
        sock.close()


def _port_open(ip: str, port: int, timeout: float = 1.0) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


class WSClient:
    """够用就好的 WebSocket 客户端（只处理这些电视用到的帧类型）。"""

    def __init__(self, host: str, port: int = 8001, path: str = "/", timeout: float = 10.0,
                 secure: bool = False):
        self.host, self.port, self.path = host, port, path
        self.sock = socket.create_connection((host, port), timeout=timeout)
        if secure:
            # 三星 8002 是自签证书，不校验
            import ssl
            ctx = ssl._create_unverified_context()
            self.sock = ctx.wrap_socket(self.sock, server_hostname=host)
        self.sock.settimeout(timeout)
        self.buf = b""

    def handshake(self, origin: str = "") -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: %s\r\n"
            "\r\n" % (self.path, self.host, self.port, key, origin or "http://%s:%d" % (self.host, self.port))
        )
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手被关闭")
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        self.buf = rest
        first = head.split(b"\r\n")[0]
        if b"101" not in first:
            raise ConnectionError("握手失败: %s" % first.decode("latin-1"))

    # ---------- 帧 ----------
    def send_text(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_json(self, obj) -> None:
        self.send_text(json.dumps(obj))

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("连接已关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_text(self, timeout: float = 20.0) -> str:
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError("等待电视消息超时")
            self.sock.settimeout(left)
            b1, b2 = self._read_exact(2)
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            data = self._read_exact(length) if length else b""
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError("电视关闭了连接")
            if opcode == 0x9:          # ping -> pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode in (0x1, 0x2):
                return data.decode("utf-8", "replace")
            # 其它（pong 等）忽略

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class SamsungTV:
    def __init__(self, ip: str, token: str = "", name: str = "HermesRemote", port: int = 8002,
                 mac: str = ""):
        self.ip, self.token, self.port = ip, token, port
        self.mac = (mac or "").strip()
        self.secure = port == 8002
        self.name = name
        self.ws = None
        self.lock = threading.RLock()   # key() 里会重入 connect()，必须可重入
        self.state = "paired" if token else "idle"   # 有 token 说明之前允许过
        self.last_error = ""
        self.last_ok = 0.0

    # ---------- 连接 ----------
    def connect(self, wait: float = 35.0) -> bool:
        """连电视。第一次会弹窗要人在电视上点「允许」。"""
        b64name = base64.b64encode(self.name.encode()).decode()
        path = "/api/v2/channels/samsung.remote.control?name=%s" % b64name
        if self.token:
            path += "&token=%s" % self.token
        with self.lock:
            self._close()
            try:
                self.ws = WSClient(self.ip, self.port, path, timeout=15, secure=self.secure)
                self.ws.handshake()
                self.ws.send_json({"method": "ms.channel.connect",
                                   "params": {"name": b64name}})
                self.state = "pairing" if not self.token else "paired"
                deadline = time.time() + wait
                while time.time() < deadline:
                    msg = json.loads(self.ws.recv_text(timeout=deadline - time.time()))
                    event = msg.get("event", "")
                    if event == "ms.channel.connect":
                        tok = (msg.get("data") or {}).get("token")
                        if tok:
                            self.token = tok
                        self.state = "paired"
                        self.last_error = ""
                        return True
                    if event == "ms.channel.unauthorized":
                        self.last_error = "电视拒绝了连接"
                        self.state = "error"
                        self._close()
                        return False
            except (OSError, ConnectionError, TimeoutError, ValueError) as exc:
                self.last_error = str(exc)
                self.state = "error"
                self._close()
                return False
        return False

    def _close(self) -> None:
        if self.ws:
            self.ws.close()
            self.ws = None

    # ---------- 发键 ----------
    def _socket_alive(self) -> bool:
        try:
            return self.ws is not None and self.ws.sock.fileno() != -1
        except OSError:
            return False

    def is_reachable(self) -> bool:
        """8002 端口通不通 —— 电视待机时这条就是断的（ping 也不通）。"""
        return _port_open(self.ip, self.port, 0.8)

    def wake(self, wait: float = 30.0) -> bool:
        """发 WoL 把待机电视叫醒，等它 8002 起来后自动重连（token 还在，不用重新配对）。

        前提：电视里「设置 → 常规 → 网络 → 专家设置」的
        「通过手机开机 / Power On with Mobile」和「IP 遥控 / IP Remote」都打开，
        否则电视待机会把网卡整个断掉，魔术包没人收。
        """
        if not self.mac:
            self.last_error = "没配电视 MAC（config.json 里的 tv_mac）"
            raise RuntimeError(self.last_error)
        if self.is_reachable():
            return self.connect(wait=15)
        wake_on_lan(self.mac, self.ip)
        deadline = time.time() + max(5.0, wait)
        while time.time() < deadline:
            if self.is_reachable():
                time.sleep(1.2)          # 端口刚开时服务还没就绪
                if self.connect(wait=20):
                    return True
            time.sleep(1.0)
        self.state = "error"
        self.last_error = "唤醒包已发，但电视没起来（检查电视的网络待机设置）"
        return False

    def key(self, name: str) -> None:
        code = TV_KEYS.get(name)
        if not code:
            raise ValueError("未知电视按键: %s" % name)
        with self.lock:
            idle = time.time() - self.last_ok
            # 电视会悄悄掐掉空闲连接：太久没发过、或者 socket 已经废了，先重连
            if not self._socket_alive() or self.state != "paired" or idle > 90:
                self._close()
                if not self.connect():
                    raise RuntimeError(self.last_error or "电视未连接")

        payload = {"method": "ms.remote.control",
                   "params": {"Cmd": "Click", "DataOfCmd": code,
                              "Option": "false", "TypeOfRemote": "SendRemoteKey"}}
        for attempt in (1, 2):
            try:
                with self.lock:
                    self.ws.send_json(payload)
                    self.last_ok = time.time()
                    self.state = "paired"
                    return
            except (OSError, ConnectionError, ValueError, AttributeError) as exc:
                # 旧连接已经死了：关掉重连再发一次
                with self.lock:
                    self._close()
                    ok = self.connect()
                if not ok:
                    raise RuntimeError("电视重连失败: %s" % (self.last_error or exc))
                if attempt == 2:
                    raise RuntimeError("电视连接反复失败: %s" % exc)

    def start_keepalive(self) -> None:
        """空闲时在后台重连，避免用户按下第一个键时等 5 秒握手。"""
        def loop() -> None:
            while True:
                time.sleep(60)
                try:
                    with self.lock:
                        if self.state == "paired" and time.time() - self.last_ok > 120:
                            self._close()
                            self.connect(wait=20)
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()

    def info(self) -> dict:
        return {"ip": self.ip, "state": self.state, "paired": bool(self.token),
                "token": self.token, "error": self.last_error}
