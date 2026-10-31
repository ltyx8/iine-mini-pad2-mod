#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
joymi_probe.py — 良值/iINE 手柄 JoyMi 私有 BLE 协议探针（互操作验证用）

用途：在写 Windows 客户端之前，先在真机上把协议跑通一遍：
    1) 扫描并连接手柄（服务 0000AE40 / 写 AE41 / 通知 AE42）
    2) g_devinfo  -> 设备信息（vid/pid/固件/模式名）
    3) g_bat      -> 电量
    4) g_rkey     -> 读改键档位（默认读 0/1/2 档）
    5) s_key_test -> 打开实时按键上报，打印 g_kt 状态 5 秒后关闭
    6) (可选) --set-remap 演示如何下发一条改键配置

协议要点（全部来自对 APK 的静态反编译，详见同名报告）：
    帧 = B4 | LEN(本帧总长) | SEQ | CC | msgpack payload
    SEQ: bit7=1 表示新消息首片，低 7 位 = 剩余片数；最后一片 seq == 1
    分片大小 = MTU - 7
    整数必须用显式 msgpack 类型（uint8=0xCC / uint16=0xCD）

依赖：pip install bleak
用法：
    python joymi_probe.py --scan
    python joymi_probe.py --mac AA:BB:CC:DD:EE:FF
    python joymi_probe.py --mac ... --keytest 5
    python joymi_probe.py --mac ... --set-remap 0

注意：需 Windows 10 1809+ / 已配对的蓝牙适配器；首次连接可能需要在系统里配对一次。
"""

import argparse
import asyncio
import struct
import sys
from typing import Any, Dict, List, Optional

FRAME_HEAD = 0xB4
FRAME_TAG = 0xCC

SERVICE_UUID = "0000ae40-0000-1000-8000-00805f9b34fb"
RX_CHAR_UUID = "0000ae41-0000-1000-8000-00805f9b34fb"   # 下行：写入
TX_CHAR_UUID = "0000ae42-0000-1000-8000-00805f9b34fb"   # 上行：通知
BATTERY_CHAR = "00002a19-0000-1000-8000-00805f9b34fb"

KEY_NAMES = {
    1: "A", 2: "B", 3: "X", 4: "Y", 5: "L1", 6: "R1", 7: "L2", 8: "R2",
    9: "Select", 10: "Start", 11: "L3", 12: "R3", 13: "Down", 14: "Up",
    15: "Right", 16: "Left", 17: "Home", 18: "L3D", 19: "R3D",
    20: "L4", 21: "R4", 22: "L5", 23: "R5",
    92: "Sensor", 93: "Minus", 94: "Plus", 95: "CAP", 96: "Turbo",
    97: "ML", 98: "MR", 99: "Mode", 100: "Fn", 101: "SL", 102: "SR",
    103: "M1", 104: "M2", 105: "M3", 106: "M4", 107: "M5", 108: "M6",
}


def key_name(v: int) -> str:
    return KEY_NAMES.get(v, f"0x{v:02X}")


# ---------------------------------------------------------------- msgpack 编码
class MP:
    """极简 msgpack 写入器：只实现本协议用到的类型，且整数强制定长。"""

    @staticmethod
    def uint8(v: int) -> bytes:            # 0xCC
        return b"\xcc" + struct.pack(">B", v & 0xFF)

    @staticmethod
    def uint16(v: int) -> bytes:           # 0xCD
        return b"\xcd" + struct.pack(">H", v & 0xFFFF)

    @staticmethod
    def str(s: str) -> bytes:              # fixstr / str8
        b = s.encode("utf-8")
        if len(b) < 32:
            return bytes([0xA0 | len(b)]) + b
        if len(b) < 256:
            return b"\xd9" + struct.pack(">B", len(b)) + b
        return b"\xda" + struct.pack(">H", len(b)) + b

    @staticmethod
    def array(items: List[bytes]) -> bytes:
        n = len(items)
        if n < 16:
            head = bytes([0x90 | n])
        else:
            head = b"\xdc" + struct.pack(">H", n)
        return head + b"".join(items)

    @staticmethod
    def map(pairs: List[tuple]) -> bytes:
        n = len(pairs)
        if n < 16:
            head = bytes([0x80 | n])
        else:
            head = b"\xde" + struct.pack(">H", n)
        body = b"".join(MP.str(k) + v for k, v in pairs)
        return head + body


class Decoder:
    """极简 msgpack 解码器（够解析 g_* 响应）。"""

    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def _take(self, n: int) -> bytes:
        if self.i + n > len(self.d):
            raise ValueError("msgpack 越界")
        b = self.d[self.i:self.i + n]
        self.i += n
        return b

    def decode(self) -> Any:
        c = self._take(1)[0]
        if c <= 0x7F:
            return c
        if c >= 0xE0:
            return c - 0x100
        if 0x80 <= c <= 0x8F:
            return {self.decode(): self.decode() for _ in range(c & 0x0F)}
        if 0x90 <= c <= 0x9F:
            return [self.decode() for _ in range(c & 0x0F)]
        if 0xA0 <= c <= 0xBF:
            return self._take(c & 0x1F).decode("utf-8", "replace")
        if c == 0xC0:
            return None
        if c == 0xC2:
            return False
        if c == 0xC3:
            return True
        if c == 0xC4:
            return self._take(struct.unpack(">B", self._take(1))[0])
        if c == 0xC5:
            return self._take(struct.unpack(">H", self._take(2))[0])
        if c == 0xCA:
            return struct.unpack(">f", self._take(4))[0]
        if c == 0xCB:
            return struct.unpack(">d", self._take(8))[0]
        if c == 0xCC:
            return struct.unpack(">B", self._take(1))[0]
        if c == 0xCD:
            return struct.unpack(">H", self._take(2))[0]
        if c == 0xCE:
            return struct.unpack(">I", self._take(4))[0]
        if c == 0xCF:
            return struct.unpack(">Q", self._take(8))[0]
        if c == 0xD0:
            return struct.unpack(">b", self._take(1))[0]
        if c == 0xD1:
            return struct.unpack(">h", self._take(2))[0]
        if c == 0xD2:
            return struct.unpack(">i", self._take(4))[0]
        if c == 0xD3:
            return struct.unpack(">q", self._take(8))[0]
        if c == 0xD9:
            return self._take(struct.unpack(">B", self._take(1))[0]).decode("utf-8", "replace")
        if c == 0xDA:
            return self._take(struct.unpack(">H", self._take(2))[0]).decode("utf-8", "replace")
        if c == 0xDC:
            return [self.decode() for _ in range(struct.unpack(">H", self._take(2))[0])]
        if c == 0xDE:
            n = struct.unpack(">H", self._take(2))[0]
            return {self.decode(): self.decode() for _ in range(n)}
        raise ValueError(f"未支持的 msgpack 标记 0x{c:02X}")


# ---------------------------------------------------------------- 帧编解码
def build_frames(payload: bytes, mtu: int = 247, max_chunk: Optional[int] = None) -> List[bytes]:
    """把一条 msgpack payload 切成若干 B4/LEN/SEQ/CC 帧。"""
    chunk = max_chunk if max_chunk else max(16, mtu - 7)
    if len(payload) == 0:
        return []
    n = (len(payload) + chunk - 1) // chunk
    if n > 127:
        raise ValueError("片数超过 127，请增大 MTU / chunk")
    frames = []
    for i in range(n):
        part = payload[i * chunk:(i + 1) * chunk]
        remaining = n - i                    # 首片 = 总片数，最后一片 = 1
        seq = remaining
        if i == 0:
            seq |= 0x80                      # 首片标记
        body = bytes([FRAME_HEAD, (len(part) + 4) & 0xFF, seq & 0xFF, FRAME_TAG]) + part
        frames.append(body)
    return frames


class Assembler:
    """接收端：按 SEQ 重组多片，最后一片(seq==1)时返回完整 payload。"""

    def __init__(self):
        self.buf = bytearray()
        self.expect = 0

    def feed(self, data: bytes) -> Optional[bytes]:
        if len(data) < 4:
            return None
        if data[0] != FRAME_HEAD:
            print(f"  [warn] 非法 HEAD 0x{data[0]:02X}")
            return None
        if data[1] != len(data):
            print(f"  [warn] LEN 不匹配 declared={data[1]} actual={len(data)}")
            return None
        if data[3] != FRAME_TAG:
            print(f"  [warn] 非法 TAG 0x{data[3]:02X}")
            return None
        seq = data[2]
        first = bool(seq & 0x80)
        remain = seq & 0x7F
        if first:
            self.buf = bytearray()
            self.expect = remain
        self.buf += data[4:]
        if remain == 1:
            payload = bytes(self.buf)
            self.buf = bytearray()
            return payload
        return None


# ---------------------------------------------------------------- 请求构造
def req_simple(cmd: str, inner_map: Optional[List[tuple]] = None) -> bytes:
    pairs = [("cmd", MP.str(cmd))]
    if inner_map is not None:
        pairs.append(("map", MP.array([MP.map(inner_map)])))
    return MP.map(pairs)


def req_rkey(mod: int) -> bytes:
    # 注意：读改键档位的 map 是「十进制字符串」，与写接口不一致（App 既有行为）
    return MP.map([("cmd", MP.str("g_rkey")), ("map", MP.str(str(mod)))])


def req_set_keytest(on: bool) -> bytes:
    return req_simple("s_key_test", [("sw", MP.uint8(1 if on else 0))])


def req_set_remap(mod: int, keys: List[Dict[str, Any]]) -> bytes:
    items = []
    for k in keys:
        entry = [
            ("k", MP.uint8(k["key"])),
            ("ty", MP.uint8(k.get("ty", 0))),
            ("m", MP.uint8(k.get("m", 0))),
            ("p", MP.uint8(k.get("p", 0))),
            ("s", MP.uint16(k.get("s", 20))),
            ("rk", MP.array([MP.uint8(v) for v in k.get("rk", [])])),
        ]
        items.append(MP.map(entry))
    return req_simple("s_rkey", [("mod", MP.uint8(mod)), ("ks", MP.array(items))])


# ---------------------------------------------------------------- BLE 主流程
async def main_async(args) -> int:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        print("缺少依赖：pip install bleak")
        return 2

    if args.scan:
        print("扫描 IINE / Wireless Controller ...")
        devices = await BleakScanner.discover(timeout=8.0)
        for d in devices:
            name = d.name or ""
            if "IINE" in name.upper() or "Wireless Controller" in name:
                print(f"  {d.address}  {name}")
        print("（未列出则说明手柄未进入 BLE 协议模式，先按住组合键开机/切模式）")
        return 0

    if not args.mac:
        print("需要 --mac 或 --scan")
        return 2

    print(f"连接 {args.mac} ...")
    asm = Assembler()
    queue: asyncio.Queue = asyncio.Queue()

    def on_notify(_char, data: bytearray):
        payload = asm.feed(bytes(data))
        if payload is not None:
            queue.put_nowait(payload)

    async with BleakClient(args.mac, timeout=20.0) as client:
        print(f"已连接: {client.is_connected}")
        await client.start_notify(TX_CHAR_UUID, on_notify)
        print("已订阅 AE42 通知")

        mtu = 247
        try:
            mtu = client.mtu_size or 247
        except Exception:
            pass
        print(f"使用 MTU={mtu}，单片载荷={mtu - 7}")

        async def send(cmd: str, payload: bytes, wait: float = 2.0):
            for f in build_frames(payload, mtu):
                await client.write_gatt_char(RX_CHAR_UUID, f, response=False)
                await asyncio.sleep(args.gap)
            if not wait:
                return None
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=wait)
            except asyncio.TimeoutError:
                print(f"  [{cmd}] 超时无响应")
                return None
            try:
                return Decoder(raw).decode()
            except Exception as e:
                print(f"  [{cmd}] 解码失败 {e} raw={raw.hex()}")
                return None

        # 1) 设备信息
        r = await send("g_devinfo", req_simple("g_devinfo"))
        if r:
            print("g_devinfo ->", r)

        # 2) 电量
        r = await send("g_bat", req_simple("g_bat"))
        if r:
            print("g_bat ->", r)

        # 3) 电量特征直读（备用路径）
        try:
            raw = await client.read_gatt_char(BATTERY_CHAR)
            print("battery(2A19) ->", raw[0], "%")
        except Exception as e:
            print("读 2A19 失败:", e)

        # 4) 读改键档位
        for m in range(3):
            r = await send(f"g_rkey[{m}]", req_rkey(m))
            if r:
                print(f"g_rkey[{m}] ->", r)
            await asyncio.sleep(0.2)

        # 5) 实时按键状态
        if args.keytest:
            print(f"打开按键上报，采样 {args.keytest}s（现在按手柄上的键）...")
            await send("s_key_test(on)", req_set_keytest(True), wait=0)
            loop = asyncio.get_running_loop()
            end = loop.time() + args.keytest
            seen = 0
            while loop.time() < end:
                try:
                    raw = await asyncio.wait_for(queue.get(), timeout=max(0.1, end - loop.time()))
                except asyncio.TimeoutError:
                    break
                try:
                    obj = Decoder(raw).decode()
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("cmd") != "g_kt":
                    continue
                m = obj.get("map")
                st = m[0] if isinstance(m, list) and m else (m if isinstance(m, dict) else {})
                if not isinstance(st, dict):
                    continue
                pressed = [key_name(int(k)) for k in (st.get("key") or [])]
                seen += 1
                print(f"  keys={pressed or '-'}  L=({st.get('lx')},{st.get('ly')}) "
                      f"R=({st.get('rx')},{st.get('ry', st.get('r_y'))}) "
                      f"L2={st.get('l2')} R2={st.get('r2')}")
            print(f"收到 {seen} 帧 g_kt")
            await send("s_key_test(off)", req_set_keytest(False), wait=0)
            print("已关闭按键上报")

        # 6) 演示下发改键
        if args.set_remap is not None:
            demo = [{"key": 20, "ty": 1, "m": 0, "p": 1, "s": 20, "rk": [1]}]  # L4 -> A（枚举值待真机确认）
            print("下发 s_rkey:", demo)
            await send("s_rkey", req_set_remap(args.set_remap, demo), wait=2.0)
            r = await send("g_rkey(verify)", req_rkey(args.set_remap))
            print("回读校验:", r)

        try:
            await client.stop_notify(TX_CHAR_UUID)
        except Exception:
            pass

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="iINE/良值 手柄 JoyMi 协议探针")
    p.add_argument("--scan", action="store_true", help="只扫描设备")
    p.add_argument("--mac", help="手柄 BLE 地址，如 AA:BB:CC:DD:EE:FF")
    p.add_argument("--keytest", type=float, default=0, help="按键状态采样秒数（0=不做）")
    p.add_argument("--set-remap", type=int, default=None, help="向指定档位下发一条演示改键")
    p.add_argument("--gap", type=float, default=0.03, help="分片写入间隔秒（默认 30ms）")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
