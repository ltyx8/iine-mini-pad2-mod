#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge.py —— 本地桥：网页 ↔ 手柄（PyWinRT 直连 BLE）

**为什么需要它**

Windows 上已经被系统连着的手柄不再广播，Web Bluetooth 的 `requestDevice()`
经常扫不到（扫到了也未必抢得到 GATT 连接）。这个桥用 WinRT 直接打开已配对设备
的 AE40 私有服务，网页改成走 HTTP 调它 —— 协议本身完全不变，仍是
`B4|LEN|SEQ|CC|msgpack`。

**一个进程干两件事**

  · 静态站点：`/`、`/keymap.html`、`/assets/*`、`/data/*`（同源，省掉 CORS）
  · BLE 桥：  `/api/status` / `/api/send` / `/api/connect` / `/api/close`

**用法**

    python bridge.py                      # 127.0.0.1:8848，自动连 IINE_mouse
    python bridge.py --port 8850
    python bridge.py --addr F8:B4:9A:CC:71:CE
    python bridge.py --no-auto            # 只起服务，等网页点「连接」

浏览器打开 http://127.0.0.1:8848/keymap.html
"""
import argparse
import asyncio
import collections
import json
import os
import socket
import sys
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import winrt_link as W

FROZEN = getattr(sys, "frozen", False)
# 只读资源（网页）：单文件 exe 会被解到 _MEIPASS，源码运行时就在脚本旁边
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
# 可写状态（地址缓存）：放 exe 所在目录，不放临时解包目录
APP_DIR = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
# App 实证（JoyMiManager.<clinit>）：currentMtu = 0x17 = 23，不主动 requestMtu，
# 每片净载荷 = MTU - 7 = 16，帧长 4+16 = 20 = MTU-3，正好卡在写入上限。
# 原来用 20 会写出 24 字节的帧，MTU 只有 23 时直接超限。
DEFAULT_CHUNK = 16

# 手柄在不同模式下的广播名完全不同，地址也会变（Switch 模式是随机地址）：
#   IINE_mouse / IINE-Keyboard → F8:B4:9A:CC:71:CE（静态）
#   IG05 Gamepad(BLE)          → 47:11:7B:FE:2C:3A 这类随机地址
# 所以不能写死地址，每次连之前按名字扫一遍。
GAMEPAD_HINTS = ("iine", "ig05", "gamepad", "controller", "良值")

# App 认设备的真正依据（JoyMiProductRegistry.<clinit>）：
#   复古迷你手柄2 = vid 1000 / pid 2001，scanIdentifier = "ig05"
# JoyMiManager 拿这个标识去**厂商广播数据**（AD 0xFF）里做 contains 匹配，
# 名字只是兜底（JOYMI_DEVICE_NAME_KEYWORDS = IINE_ / Wireless Controller）。
# 所以「没名字也能认出来」是对的：标识在 manufacturerData 里，不在名字里。
JOYMI_SCAN_ID = b"ig05"


def _rssi(d):
    """bleak 0.22 起 rssi 从 BLEDevice 挪到了 advertisement_data，两边都兜一下。"""
    r = getattr(d, "rssi", None)
    if r is None:
        ad = getattr(d, "advertisement_data", None)
        r = getattr(ad, "rssi", None) if ad is not None else None
    return r if r is not None else -999


ADDR_CACHE = os.path.join(APP_DIR, ".bridge_addr")


def _load_last_addr():
    try:
        with open(ADDR_CACHE, "r", encoding="utf-8") as f:
            a = f.read().strip()
        return a or None
    except Exception:
        return None


def _save_last_addr(a: str):
    try:
        with open(ADDR_CACHE, "w", encoding="utf-8") as f:
            f.write(a)
    except Exception:
        pass


def _clear_last_addr():
    """地址一旦证明没服务表就扔掉，否则每次启动都要在死地址上白等两轮。"""
    try:
        os.remove(ADDR_CACHE)
    except Exception:
        pass


def _peek_cmd(buf) -> str | None:
    """从 msgpack map 里抠出 cmd 字段。

    V1 的回包永远是 `{cmd: "ret_xxx", ...}`，cmd 一定是第一个 key，
    所以不用通用解码器，按 fixstr / str8 两种长度形式读一下就够。
    """
    try:
        if not buf or not (0x80 <= buf[0] <= 0x8F):     # 必须是 map
            return None
        i = 1
        k = buf[i]; i += 1                              # key
        if 0xA0 <= k <= 0xBF:
            i += k & 0x1F
        elif k == 0xD9:
            i += 1 + buf[i]
        else:
            return None
        v = buf[i]; i += 1                              # value
        if 0xA0 <= v <= 0xBF:
            return buf[i:i + (v & 0x1F)].decode("utf-8", "replace")
        if v == 0xD9:
            return buf[i + 1:i + 1 + buf[i]].decode("utf-8", "replace")
    except Exception:
        return None
    return None


def _expect_resp(payload) -> str | None:
    """请求 cmd → 期望的回包 cmd：get_kb / set_kb 都回 ret_kb。"""
    c = _peek_cmd(payload)
    if not c or "_" not in c:
        return None
    head, tail = c.split("_", 1)
    return ("ret_" + tail) if head in ("get", "set") else None


async def _paired_addrs() -> list[tuple[str, str]]:
    """Windows 已配对的 **BLE** 设备（name, mac）。2 秒内出不了结果就当没有。

    用途：鼠标档（IINE_mouse）连上 Windows 后**不再广播**，扫描一无所获，
    但系统里的配对记录还在 —— 这是唯一能自动找回它的路。

    坑：`find_all_async` 的第二个参数 `additional_properties` 传 None 会静默
    返回 0 条（实测），必须传空列表 `[]`。

    另注意手柄切到 Switch 模式后是被当成**经典蓝牙 HID**（BTHENUM\\DEV_xxx，
    名字 Pro Controller）连着的，不在这个列表里，也没有 AE40。
    """
    out = []
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEDevice
        from winrt.windows.devices.enumeration import DeviceInformation, DeviceInformationKind
        infos = await asyncio.wait_for(DeviceInformation.find_all_async(
            BluetoothLEDevice.get_device_selector(), [],
            DeviceInformationKind.ASSOCIATION_ENDPOINT), 2.0)
        for it in infos:
            try:
                # id 形如 BluetoothLE#BluetoothLE<本机mac>-<远端mac>
                raw = (it.id or "").split("-")[-1]
                if raw.count(":") == 5:
                    out.append((it.name or "", raw.upper()))
            except Exception:
                continue
    except Exception:
        pass
    return out


async def _has_joymi(addr: str, timeout: float = 3.5) -> bool:
    """能不能在这个地址上看到 0xAE40 服务。用来确认一台无名设备到底是不是手柄。"""
    from winrt.windows.devices.bluetooth import BluetoothLEDevice

    async def probe():
        mac = int(addr.replace(":", ""), 16)
        dev = await BluetoothLEDevice.from_bluetooth_address_async(mac)
        if dev is None:
            return False
        try:
            for s in (await dev.get_gatt_services_async()).services:
                if str(s.uuid).lower() == W.JOYMI_SVC:
                    return True
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return False

    try:
        return await asyncio.wait_for(probe(), timeout)
    except Exception:
        return False


async def resolve_addr(hint: str | None = None, fresh: bool = False) -> str | None:
    """返回要连的 MAC；扫不到就返回 None（不要瞎猜一个地址去连）。

    优先级：显式地址 → 上次连成功的地址（不扫描，快）→ 已配对列表里的 IINE_*
    → 扫描 → 已配对列表里的无名设备 → None。

    为什么已配对列表排在扫描前面：**鼠标档（IINE_mouse）连上 Windows 后不再广播**，
    这时扫描要白等 3.5 秒还一无所获，而已配对列表 0.2 秒就能给出 IINE_mouse。
    Switch 档反过来 —— 它是经典蓝牙，不在 BLE 配对表里，只能靠扫描找那个带
    ig05 标识的广播。
    """
    if hint and hint.lower() not in ("auto", "", "none"):
        return hint
    if not fresh:
        last = _load_last_addr()
        if last:
            print(f"[桥] 先试上次的地址 {last}")
            return last

    # 已配对列表优先：鼠标档不广播，这是唯一能自动找到它的路
    pair = await _paired_addrs()
    for name, mac in pair:
        if name and any(h in name.lower() for h in GAMEPAD_HINTS):
            print(f"[桥] 已配对记录里找到 {name!r}  {mac}")
            return mac

    try:
        from bleak import BleakScanner
        raw = await BleakScanner.discover(timeout=3.5, return_adv=True)
        items = list(raw.values()) if isinstance(raw, dict) else [(d, None) for d in raw]
        adv_of = {d.address: a for d, a in items}
        devs = [d for d, _ in items]

        def _rssi_of(d):
            a = adv_of.get(d.address)
            r = getattr(a, "rssi", None) if a is not None else None
            return r if r is not None else _rssi(d)

        def _is_joymi(d):
            """和 App 一样：厂商广播数据（AD 0xFF）里找 ig05 标识。"""
            a = adv_of.get(d.address)
            md = getattr(a, "manufacturer_data", None) or {}
            for v in md.values():
                try:
                    if JOYMI_SCAN_ID in bytes(v).lower():
                        return True
                except Exception:
                    continue
            return False

        tagged = [d for d in devs if _is_joymi(d)]
        named = [d for d in devs
                 if any(h in (d.name or "").lower() for h in GAMEPAD_HINTS)]
        unnamed = [d for d in devs if not d.name]
        tagged.sort(key=lambda d: -_rssi_of(d))
        named.sort(key=lambda d: -_rssi_of(d))
        unnamed.sort(key=lambda d: -_rssi_of(d))

        if tagged:
            d = tagged[0]
            print(f"[桥] 厂商数据含 ig05：{d.name!r}  {d.address}  rssi={_rssi_of(d)}")
            return d.address
        if named:
            d = named[0]
            print(f"[桥] 按名字扫描到 {d.name!r}  {d.address}  rssi={_rssi_of(d)}")
            return d.address
        for d in unnamed[:2]:
            if await _has_joymi(d.address):
                print(f"[桥] 无名设备 {d.address} 上有 AE40，判定为手柄")
                return d.address
        print(f"[桥] 扫了 3.5 秒没找到手柄（附近 {len(devs)} 个 BLE 设备，"
              f"{len(unnamed)} 个无名、{len(tagged)} 个带 ig05 标识）")

        # 扫描也没结果时，再探一下配对表里的无名设备
        for name, mac in pair:
            if not name and await _has_joymi(mac):
                print(f"[桥] 已配对的无名设备 {mac} 上有 AE40，判定为手柄")
                return mac
        print("[桥] 找不到可连的手柄。注意：Switch 模式 / 经典蓝牙的「Pro Controller」"
              "不在 BLE 里，读不到改键配置，要把手柄拨到鼠标档或切回私有（配置）模式。")
    except Exception as e:
        print(f"[桥] 扫描失败：{type(e).__name__}: {e}")
    return None



# ---------------------------------------------------------------- BLE 设备

class LinkLost(RuntimeError):
    """GATT 链路已经断了（手柄关机 / 换档位 / 被经典蓝牙抢走）。

    区别于「命令超时」：超时是链路活着但没回包，这条是链路本身没了。
    网页要据此提示「已断开」并触发重连，而不是让用户看到 HTTP 500。
    """


# 「对象已关闭」「设备不可达」这类 WinError，一律当成链路丢失
_LINK_LOST_CODES = (-2147483629,   # 0x80000013 该对象已关闭
                    -2147483638,   # 0x8000000A 数据无效 / 已断开
                    -2147023728)   # 0x80070490 找不到元素


def _is_link_lost(e: BaseException) -> bool:
    if isinstance(e, LinkLost):
        return True
    winerr = getattr(e, "winerror", None)
    if isinstance(winerr, int) and winerr in _LINK_LOST_CODES:
        return True
    msg = str(e).lower()
    for kw in ("该对象已关闭", "object has been closed", "已关闭",
               "未连接", "unreachable", "not connected", "not found"):
        if kw in msg:
            return True
    return False


class Device:
    """一个持久 GATT 连接。所有协程都在主线程的 loop 上跑。"""

    def __init__(self, addr: str, chunk: int = DEFAULT_CHUNK):
        self.addr = addr
        self.chunk = chunk
        self.dev = None
        self.rx = None
        self.tx = None
        self.got = None
        self.lock = None
        self.name = None
        self.bat = None
        self.battery = None
        self.mtu = None
        self.sess = None
        self._loop = None
        self.trace = collections.deque(maxlen=48)
        self.connected = False
        self.last_error = None      # 最近一次失败原因，网页上直接显示
        self._on_changed = None
        self._token = None

    def _release(self, close_dev: bool = True):
        """彻底放开当前连接。

        关键：Windows 对同一个 MAC 会返回**同一个** BluetoothLEDevice 实例。
        只 close 不把引用清掉，下次 from_bluetooth_address_async 拿回的就是这个
        已经失效的对象 —— 表现是 get_gatt_services_async 返回 0 个服务，
        于是「断开后再连接」永远连不上，只有重启进程（OS 强制回收）才恢复。

        所以这里要按顺序拆干净：先摘事件回调（它持有 tx 的引用），
        再断引用，最后 close，并强制一次 GC 让 COM 包装真的释放。
        """
        import gc

        self.connected = False
        tx, dev, handler, token = self.tx, self.dev, self._on_changed, self._token

        if tx is not None and (token is not None or handler is not None):
            removed = False
            if token is not None:
                try:
                    tx.remove_value_changed(token)
                    removed = True
                except Exception:
                    pass
            if not removed and handler is not None:
                try:
                    tx.remove_value_changed(handler)
                except Exception:
                    pass
        self._on_changed = None
        self._token = None

        self.rx = self.tx = self.bat = None
        sess, self.sess = self.sess, None
        if sess is not None:
            try:
                sess.maintain_connection = False
            except Exception:
                pass
            try:
                sess.close()
            except Exception:
                pass
        if self.got is not None:
            while not self.got.empty():
                try:
                    self.got.get_nowait()
                except Exception:
                    break
        self.got = None
        self.battery = None

        if close_dev and dev is not None:
            try:
                dev.close()
            except Exception:
                pass
        self.dev = None

        del tx, dev, handler, token
        gc.collect()

    async def connect(self, attempts: int = 3, force_uncached: bool = False):
        from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothCacheMode
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue as CCCD)

        mac = int(self.addr.replace(":", ""), 16)

        # 先把上一轮连接放干净；只有真断过一个活连接才需要等系统放手，
        # 冷启动（本来就没连）等这 1 秒纯属浪费。
        # 另外 App 的做法是 stopScan() 之后再 connectGatt —— 扫描器还活着时直连
        # 容易拿到 Unreachable，所以冷启动也给 0.25 秒让蓝牙栈收尾。
        had = self.dev is not None
        self._release(close_dev=True)
        await asyncio.sleep(1.0 if had else 0.25)

        last = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                await asyncio.sleep(0.8 * attempt)
            dev = None
            try:
                dev = await BluetoothLEDevice.from_bluetooth_address_async(mac)
                if dev is None:
                    raise RuntimeError(
                        f"打不开设备 {self.addr}（不在范围内 / 已关机休眠 / 被别的设备连着）")
                self.name = dev.name if dev.name else self.name

                # 第 1 次走缓存（快）；失败后强制 UNCACHED 重新发现服务，
                # 否则 Windows 会把上一次的（空）服务表直接还回来。
                # force_uncached：扫描刚抓到的新地址必须直接重发现 —— 系统缓存里
                # 存的是上一次（连不上时）的空表，走 CACHED 永远读到 0 个服务。
                use_uncached = force_uncached or attempt > 1
                cache = BluetoothCacheMode.UNCACHED if use_uncached else BluetoothCacheMode.CACHED
                res = await dev.get_gatt_services_async(cache)
                svcs = list(res.services)

                rx = tx = bat = None
                for s in svcs:
                    u = str(s.uuid).lower()
                    if u.startswith("0000180f"):           # Battery Service
                        try:
                            cs = (await s.get_characteristics_async()).characteristics
                            if cs:
                                bat = cs[0]
                        except Exception:
                            pass
                        continue
                    if u != W.JOYMI_SVC:
                        continue
                    for c in (await s.get_characteristics_async()).characteristics:
                        cu = str(c.uuid).lower()
                        if cu == W.AE41:
                            rx = c
                        elif cu == W.AE42:
                            tx = c

                if not rx or not tx:
                    mode = "UNCACHED" if use_uncached else "CACHED"
                    if not svcs:
                        raise RuntimeError(
                            f"设备可达但发现 0 个服务（{mode}）："
                            f"这个地址没有 GATT 服务表。手柄若是 Switch 模式（系统里叫 "
                            f"Pro Controller、走经典蓝牙），就没有 AE40 配置通道，"
                            f"要切回私有（配置）模式才能改键")
                    raise RuntimeError(
                        f"没找到 AE41/AE42（本次拿到 {len(svcs)} 个服务，cache={mode}）")

                self.dev, self.rx, self.tx, self.bat = dev, rx, tx, bat
                _props = int(getattr(rx, "characteristic_properties", 0) or 0)
                print(f"[桥] AE41 属性=0x{_props:02X}"
                      f"{'  WriteWithoutResponse' if _props & 4 else '  Write(with response)'}")
                self.got = asyncio.Queue()
                self.lock = asyncio.Lock()
                self._loop = asyncio.get_event_loop()

                def on_changed(_s, args):
                    """WinRT 在 **COM 线程**回调，不是事件循环线程。

                    asyncio.Queue 不是线程安全的：直接 put_nowait 只是把数据塞进
                    deque，**不会唤醒**正在 await 的协程。表现就是数据明明 0.2 秒
                    就到了，query 却等到超时才返回（单帧包偶尔蹭到循环轮询，所以
                    get_info 那种小包时好时坏）。必须用 call_soon_threadsafe。
                    """
                    try:
                        raw = W.to_bytes(args.characteristic_value)
                        self.trace.append((time.time(), raw))
                        q = self.got
                        if q is None:
                            return
                        lp = self._loop
                        if lp is not None:
                            lp.call_soon_threadsafe(q.put_nowait, raw)
                        else:
                            q.put_nowait(raw)
                    except Exception:
                        pass

                self._on_changed = on_changed
                try:
                    self._token = tx.add_value_changed(on_changed)   # PyWinRT 返回注册 token
                except Exception:
                    self._token = None
                await tx.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)
                await asyncio.sleep(0.4)
                self.connected = True
                await self.tune_connection()
                await self.negotiate_mtu()
                await self.read_battery()
                return self.name
            except Exception as e:
                last = e
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
                self.dev = None
                if attempt < 3:
                    print(f"[桥] 连接第 {attempt} 次失败：{e} —— 重试")
        raise last if last else RuntimeError("连接失败")

    async def tune_connection(self):
        """把连接参数拉向吞吐优先。

        Windows 默认按省电给 BLE 参数，连接间隔可能到 1.2~2 秒。发一帧等一个
        连接事件、设备回一帧再等一个 —— 一趟就是一个往返，正好卡出「3 秒一条」
        的观感。Android 侧（App）走 requestMtu + 系统高吞吐参数，所以它快。

        两条路都试：
          1. request_preferred_connection_parameters(THROUGHPUT_OPTIMIZED)
          2. GattSession.maintain_connection = True（让系统把这条链路当"正在用"）
        """
        try:
            from winrt.windows.devices.bluetooth import (
                BluetoothLEPreferredConnectionParameters as P)
            params = None
            for name in ("throughput_optimized", "ThroughputOptimized", "THROUGHPUT_OPTIMIZED"):
                try:
                    params = getattr(P, name)
                    break
                except Exception:
                    continue
            if params is None:
                print("[桥] 取不到吞吐优先的预置参数，改用 GATT 会话保活")
            else:
                ok = self.dev.request_preferred_connection_parameters(params)
                print(f"[桥] 连接参数 → 吞吐优先：{ok}")
        except Exception as e:
            print(f"[桥] 设置首选连接参数失败：{type(e).__name__}: {e}")

        try:
            from winrt.windows.devices.bluetooth.genericattributeprofile import GattSession
            self.sess = await asyncio.wait_for(
                GattSession.from_device_id_async(self.dev.bluetooth_device_id), 4)
            self.sess.maintain_connection = True
            print(f"[桥] GATT 会话保活已开，pdu={self.sess.max_pdu_size}")
        except Exception as e:
            print(f"[桥] GATT 会话保活失败：{type(e).__name__}: {e}")

    async def negotiate_mtu(self):
        """读系统协商出来的 MTU，把分片调到 MTU-7。

        App 连上第一件事就是 `requestMtu(517)`（日志："Requesting MTU of 517
        after connection."），所以它一帧能装 510 字节，一张 17 条的表一帧发完。
        WinRT **没有** requestMtu，只能读 GattSession 的协商结果。分片按
        MTU-7 走，别再写死 16；只有 MTU 真是 23 时才退回 16。
        """
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattSession
        self.mtu = None
        try:
            sess = await asyncio.wait_for(
                GattSession.from_device_id_async(self.dev.bluetooth_device_id), 4)
            try:
                pdu = int(sess.max_pdu_size or 0)
                self.mtu = pdu
                if pdu > 27:
                    self.chunk = max(16, min(pdu - 7, 240))
                print(f"[桥] MTU={pdu} → 分片 {self.chunk} 字节")
            finally:
                try:
                    sess.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"[桥] 读 MTU 失败，按 {self.chunk} 字节分片继续：{type(e).__name__}")
        return self.mtu

    async def read_battery(self):
        """标准 GATT 0x180F/0x2A19，和 App 连上后做的一样。读不到就留 None。"""
        self.battery = None
        if not self.bat:
            return None
        try:
            from winrt.windows.storage.streams import DataReader
            r = await self.bat.read_value_async()
            buf = r.value
            if buf is not None and buf.length:
                self.battery = DataReader.from_buffer(buf).read_byte()
        except Exception:
            self.battery = None
        return self.battery

    def _drain(self):
        if self.got is not None:
            while not self.got.empty():
                try:
                    self.got.get_nowait()
                except Exception:
                    break

    async def _send(self, payload: bytes):
        from winrt.windows.storage.streams import DataWriter
        frames = W.build_frames(payload, self.chunk)
        # AE41 是 Write Without Response（4 = WRITE_WITHOUT_RESPONSE）：
        # 对这类特征用 write_value_with_result_async 会一直等一个**永远不会来的**
        # Write Response，白等到 GATT 超时才算完 —— 这就是「读一条要 5~6 秒」的真凶。
        # App 那边同样不等响应（Android 对 WNR 特征立即回调 onCharacteristicWrite），
        # 所以它快。这里必须换成 write_value_async。
        props = int(getattr(self.rx, "characteristic_properties", 0) or 0)
        wnr = bool(props & 0x04)
        for i, f in enumerate(frames):
            if i:
                # WNR 没有流控，帧间留一点空，否则丢帧
                await asyncio.sleep(0.008 if wnr else 0.006)
            w = DataWriter()
            w.write_bytes(f)
            buf = w.detach_buffer()
            if wnr:
                await self.rx.write_value_async(buf)
            else:
                r = await self.rx.write_value_with_result_async(buf)
                if r.status != 0:
                    raise RuntimeError(f"写帧失败 status={r.status}")

    def _alive(self) -> bool:
        """链路是否真的还在：读 BluetoothLEDevice.connection_status（1=已连接）。

        `self.connected` 只是我们自己记的标志，手柄突然关机 / 换档位 / 被经典蓝牙
        接管时它不会自己变 —— 于是就出现「网页显示已连接、一发命令就 500」。
        """
        try:
            return self.dev is not None and int(getattr(self.dev, "connection_status", 0)) == 1
        except Exception:
            return False

    def _mark_lost(self, why: str):
        self.connected = False
        self.last_error = why

    async def query(self, payload: bytes, timeout: float = 5.0, cmd: str | None = None):
        """发一条，收完整响应，返回 msgpack 原始字节（不含帧头）。超时返回 None。

        `cmd` 是期望的回包 cmd（ret_kb 之类）。给了就只认它 —— 上一条命令的
        迟到通知会在这条的超时窗口里冒出来，不校验就会被当成这条的答案，
        网页侧再校验一次就直接报「响应不匹配」，整栏读不出来。

        链路真断了（手柄关机 / 换档位）抛 LinkLost，让上层复位状态并触发重连；
        链路活着但设备没回包，仍然返回 None（正常超时）。
        """
        if not self.connected:
            raise LinkLost(self.last_error or "未连接")
        if cmd is None:
            cmd = _expect_resp(payload)
        loop = asyncio.get_event_loop()
        async with self.lock:
            self._drain()
            try:
                await self._send(payload)
            except Exception as e:
                if _is_link_lost(e):
                    self._mark_lost(f"写命令时链路已断开（{type(e).__name__}）")
                    raise LinkLost(self.last_error) from e
                raise
            deadline = loop.time() + timeout
            buf = bytearray()
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    # 没等到回包：如果是链路死了才没回，按断线处理，别只报超时
                    if not self._alive():
                        self._mark_lost("链路已断开（命令无响应，且设备已离线）")
                        raise LinkLost(self.last_error)
                    return None
                try:
                    ch = await asyncio.wait_for(self.got.get(), left)
                except asyncio.TimeoutError:
                    if not self._alive():
                        self._mark_lost("链路已断开（命令无响应，且设备已离线）")
                        raise LinkLost(self.last_error)
                    return None
                if ch[2] & 0x80:            # 首帧：重新起一条消息
                    buf = bytearray(ch[4:])
                else:                       # 后续帧
                    buf += ch[4:]
                if (ch[2] & 0x7F) != 1:     # 还有后续帧
                    continue
                if cmd is None or _peek_cmd(buf) == cmd:
                    self.last_error = None
                    return bytes(buf)
                buf = bytearray()           # 不是等的那条，丢掉接着等

    def close(self):
        self._release(close_dev=True)


# ---------------------------------------------------------------- 全局

LOOP = None
DEVICE = None
CONFIG = None
CONNECTING = False
RECOVERING = False            # 后台自动重连进行中
_CONNECT_LOCK = threading.Lock()   # 同一时间只允许一个连接任务（手动 / 自动）
RECOVER_SECONDS = 120.0       # 掉线后自动抢连的时长


def run_ble(coro, timeout=20.0):
    """从 HTTP 线程把协程投到主线程 loop 上执行。"""
    fut = asyncio.run_coroutine_threadsafe(coro, LOOP)
    return fut.result(timeout)


# ---------------------------------------------------------------- HTTP

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write("  %s %s\n" % (self.command, self.path))

    # ---------- 工具 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        if self.path.split("?")[0] == "/api/status":
            self._json(status())
            return
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/send":
                q = self._read_json()
                hexstr = (q.get("hex") or "").strip()
                if not hexstr:
                    self._json({"error": "缺少 hex"}, 400)
                    return
                timeout = float(q.get("timeout") or 5.0)
                try:
                    payload = bytes.fromhex(hexstr.replace(" ", ""))
                except ValueError:
                    self._json({"error": "hex 格式不对"}, 400)
                    return
                try:
                    raw = run_ble(DEVICE.query(payload, timeout, q.get("cmd") or None), timeout + 4)
                except LinkLost as e:
                    # 链路断了：明确告诉网页「已断开」，顺手在后台开始抢连，
                    # 不要让异常冒成 HTTP 500（网页只会显示「本地桥返回 500」）。
                    _start_recover()
                    self._json({"error": str(e), "disconnected": True,
                                "connected": False})
                    return
                if raw is None:
                    self._json({"timeout": True})
                    return
                self._json({"payload": raw.hex()})
                return

            if path == "/api/connect":
                q = self._read_json()
                try:
                    wait = float(q.get("wait") or 0)
                except Exception:
                    wait = 0.0
                self._json(do_connect(wait))
                return

            if path == "/api/close":
                DEVICE.close()
                # 这是用户主动断开，不算掉线：清掉错误态，也不要触发自动重连
                DEVICE.last_error = None
                self._json({"ok": True})
                return

            self._json({"error": "未知接口"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def status():
    d = DEVICE
    connected = bool(d and d.connected)
    # 自己记的标志可能已经过期（手柄关机 / 换档位）。这里顺手对一下真实链路，
    # 过期就直接复位，网页下次轮询就能看到「未连接」而不是假的已连接。
    alive = False
    if d is not None:
        alive = d._alive()
        if connected and not alive:
            d._mark_lost("链路已断开")
            connected = False
            _start_recover()      # 自动掉线就后台抢回，不用用户点
    return {
        "ok": True,
        "bridge": True,
        "connecting": CONNECTING,
        "recovering": RECOVERING,
        "connected": connected,
        "alive": alive,
        "name": d.name if d else None,
        "addr": ((d.addr or None) if d else None),
        "battery": d.battery if d else None,
        "mtu": d.mtu if d else None,
        "chunk": d.chunk if d else None,
        "last_error": d.last_error if d else None,
        "trace": [[round(t, 3), b.hex()] for t, b in list(d.trace)[-14:]] if d else [],
    }


def do_connect(wait_sec: float = 0.0):
    """两轮连：第 1 轮只信缓存地址、只给 1 次机会；第 2 轮才真正扫描。

    以前是「缓存地址 ×3 次退避 → 再扫 5 秒 → 再 ×3 次」，地址一过期就要 13 秒
    才失败。地址过期之后再多试也没用，早点发现早点去扫。

    `wait_sec > 0` 时进入**抢连**模式：单轮只给 1 次机会快失败，然后每隔
    0.7 秒再来一轮，直到连上或超时。用于「你去断开经典蓝牙，我在这边等着抢」——
    Windows 会在你断开后很快把经典连接拉回去，慢一步就抢不到了。
    """
    if DEVICE.connected:
        return status()
    if not _CONNECT_LOCK.acquire(blocking=False):
        # 已经有人在连（手动点了连接 / 后台掉线重连），别重复开一轮
        return {"ok": True, "bridge": True, "connected": bool(DEVICE.connected),
                "connecting": True, "recovering": RECOVERING,
                "error": None, "note": "已有连接任务在进行中"}
    try:
        deadline = time.time() + max(0.0, wait_sec)
        quick = wait_sec > 0
        while True:
            r = _connect_once(quick=quick)
            if r.get("connected"):
                return r
            if time.time() >= deadline:
                return r
            time.sleep(0.7)
    finally:
        _CONNECT_LOCK.release()


def _connect_once(quick: bool = False):
    last = None
    tried = set()
    plan = ((False, 1), (True, 1 if quick else 2))
    for fresh, tries in plan:
        addr = run_ble(resolve_addr(None if fresh else CONFIG.addr, fresh=fresh), 16)
        if not addr:
            last = RuntimeError("没找到手柄广播（可能已关机或休眠，或被手机 / Switch 占着）")
            if fresh:
                break
            print("[桥] 缓存地址没用，重新扫描")
            continue
        if addr in tried:
            # 同一个 MAC 不代表能跳过：第 1 轮失败往往是因为它当时还挂在
            # 经典蓝牙（Switch 模式）上，扫描到时已经切回 BLE 广播了。
            print(f"[桥] 又是 {addr}，但它现在是广播状态，再试一次")
        tried.add(addr)
        DEVICE.addr = addr
        try:
            # 扫描刚抓到的地址 = 新状态，必须 UNCACHED 重新发现服务
            name = run_ble(DEVICE.connect(attempts=tries, force_uncached=fresh), 45)
            _save_last_addr(addr)
            print(f"[桥] 已连接 {name} ({addr})")
            return status()
        except Exception as e:
            last = e
            DEVICE.connected = False
            # 缓存地址证明没有服务表 → 立刻作废，别让它下次启动再拖两轮
            if not fresh and "0 个服务" in str(e):
                _clear_last_addr()
                print(f"[桥] 缓存地址 {addr} 已作废（没有 GATT 服务表）")
            print(f"[桥] 连接 {addr} 失败：{type(e).__name__}: {e}")
    return {"ok": False, "connected": False,
            "error": (f"{type(last).__name__}: {last}" if last else "连接失败")}


def _start_recover(seconds: float = RECOVER_SECONDS):
    """掉线后在后台自动抢连，不阻塞 HTTP —— 网页轮询 status 看到 connected 就直接可用。

    抢连场景（Switch 档 / 手柄刚重启）下链路要等几十秒才会放开，让用户手动反复点
    「连接」没有意义，交给这里后台循环试。
    """
    global RECOVERING
    if RECOVERING or CONNECTING or (DEVICE is not None and DEVICE.connected):
        return
    def run():
        global RECOVERING
        RECOVERING = True
        try:
            print(f"[桥] 链路断了，后台自动重连（最多 {seconds:.0f} 秒）")
            r = do_connect(seconds)
            if r.get("connected"):
                print(f"[桥] 自动重连成功：{r.get('name')} ({r.get('addr')})")
            else:
                print(f"[桥] 自动重连未成功：{r.get('error')}")
        except Exception as e:
            print(f"[桥] 自动重连异常：{type(e).__name__}: {e}")
        finally:
            RECOVERING = False
    threading.Thread(target=run, daemon=True, name="recover").start()


# ---------------------------------------------------------------- main

async def _auto_connect():
    """启动时的自动连接，跑在 loop 里（不挡 HTTP 服务，网页能秒开）。"""
    global CONNECTING
    CONNECTING = True
    try:
        tried = set()
        for fresh, tries in ((False, 1), (True, 2)):
            try:
                addr = await asyncio.wait_for(
                    resolve_addr(None if fresh else CONFIG.addr, fresh=fresh), 16)
            except Exception:
                addr = None
            if not addr:
                if fresh:
                    break
                print("[桥] 缓存地址没用，重新扫描")
                continue
            if addr in tried:
                print(f"[桥] 又是 {addr}，但它现在是广播状态，再试一次")
            tried.add(addr)
            DEVICE.addr = addr
            print(f"[桥] 正在连 {addr} …")
            try:
                name = await asyncio.wait_for(
                    DEVICE.connect(attempts=tries, force_uncached=fresh), 45)
                _save_last_addr(addr)
                print(f"[桥] 已连接 {name} ({addr})")
                return
            except Exception as e:
                DEVICE.connected = False
                if not fresh and "0 个服务" in str(e):
                    _clear_last_addr()
                    print(f"[桥] 缓存地址 {addr} 已作废（没有 GATT 服务表）")
                print(f"[桥] 连接 {addr} 失败：{type(e).__name__}: {e}")
    finally:
        CONNECTING = False
        if not DEVICE.connected:
            print("[桥] 服务照常提供，网页上点「连接手柄」可以重试")


def main():
    global LOOP, DEVICE, CONFIG
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="auto",
                    help="auto = 每次按广播名扫描；也可写死 MAC")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK)
    ap.add_argument("--no-auto", action="store_true", help="启动时不自动连手柄")
    ap.add_argument("--open", dest="open_browser", action="store_true", default=FROZEN,
                    help="起好之后打开浏览器（打包成 exe 时默认开）")
    ap.add_argument("--no-open", dest="open_browser", action="store_false")
    CONFIG = ap.parse_args()

    LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(LOOP)
    DEVICE = Device("", CONFIG.chunk)

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", CONFIG.port), Handler)
    except OSError as e:
        # 端口被占多半是已经开着一份了 —— 直接把网页打开，别报错退出
        print(f"[!] 端口 {CONFIG.port} 已被占用（{e}），可能已经开着一份，直接打开网页")
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{CONFIG.port}/keymap.html")
        return 0

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{CONFIG.port}/keymap.html"
    print(f"[桥] 网页：{url}")
    print(f"[桥] 状态：{url.replace('keymap.html', 'api/status')}")
    print("[桥] 关掉这个窗口即退出")

    # 连手柄放后台：以前是先连再起服务，扫描 + 重试的那十几秒里网页根本打不开
    if not CONFIG.no_auto:
        LOOP.create_task(_auto_connect())

    if CONFIG.open_browser:
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        LOOP.run_forever()
    except KeyboardInterrupt:
        print("\n[桥] 退出")
    finally:
        DEVICE.close()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
