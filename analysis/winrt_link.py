#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
winrt_link.py —— 绕过 Web Bluetooth，用 Windows 原生 BLE API 直连已配对手柄

为什么需要它：
    BLE 设备一旦被 Windows 连上（本例手柄被识别成 IINE_mouse 鼠标），就不再广播，
    于是 bleak / Web Bluetooth 都扫不到它。但 WinRT 的
    BluetoothLEDevice.from_bluetooth_address_async() 可以按 MAC 直接打开已配对设备，
    不需要设备处于广播状态。

用法：
    python winrt_link.py                     # 打开设备，列服务，找 AE40
    python winrt_link.py --info              # 额外发 g_devinfo 读设备信息
    python winrt_link.py --addr F8:B4:9A:CC:71:CE

注意：
    设备若被系统当作 HID 占用，写特征可能返回 ACCESS_DENIED / UNREACHABLE —— 届时只能
    在系统里删掉配对让它重新广播，再用网页连。
"""
import argparse
import asyncio
import struct
import sys

ADDR_DEFAULT = "F8:B4:9A:CC:71:CE"
JOYMI_SVC = "0000ae40-0000-1000-8000-00805f9b34fb"
AE41 = "0000ae41-0000-1000-8000-00805f9b34fb"
AE42 = "0000ae42-0000-1000-8000-00805f9b34fb"

HEAD, TAG = 0xB4, 0xCC


# ---------- msgpack 极简编码（整数显式定长，与固件一致） ----------
def mp_str(s: str) -> bytes:
    b = s.encode("utf-8")
    if len(b) < 32:
        return bytes([0xA0 | len(b)]) + b
    return bytes([0xD9, len(b)]) + b


def mp_map1(k: str, v: bytes) -> bytes:
    return bytes([0x81]) + mp_str(k) + v


def build_frames(payload: bytes, chunk: int = 20) -> list[bytes]:
    n = (len(payload) + chunk - 1) // chunk
    out = []
    for i in range(n):
        part = payload[i * chunk:(i + 1) * chunk]
        seq = (n - i) | (0x80 if i == 0 else 0)
        out.append(bytes([HEAD, len(part) + 4, seq & 0xFF, TAG]) + part)
    return out


def cmd_simple(name: str, key: str | None = None) -> bytes:
    """构造 {cmd:name} 或 {cmd:name, key:key} —— V1 的 get_info 需要 key=all。"""
    pairs = [("cmd", mp_str(name))]
    if key:
        pairs.append(("key", mp_str(key)))
    return bytes([0x80 | len(pairs)]) + b"".join(mp_str(k) + v for k, v in pairs)


# ---------- 解码 ----------
def mp_decode(d: bytes):
    i = 0

    def rd():
        nonlocal i
        c = d[i]; i += 1
        if c <= 0x7F: return c
        if c >= 0xE0: return c - 0x100
        if 0x80 <= c <= 0x8F:
            return {rd(): rd() for _ in range(c & 0x0F)}
        if 0x90 <= c <= 0x9F:
            return [rd() for _ in range(c & 0x0F)]
        if 0xA0 <= c <= 0xBF:
            n = c & 0x1F; s = d[i:i + n].decode("utf-8", "replace"); i += n; return s
        if c == 0xC0: return None
        if c == 0xC2: return False
        if c == 0xC3: return True
        if c == 0xCC: v = d[i]; i += 1; return v
        if c == 0xCD: v = struct.unpack_from(">H", d, i)[0]; i += 2; return v
        if c == 0xCE: v = struct.unpack_from(">I", d, i)[0]; i += 4; return v
        if c == 0xD0: v = struct.unpack_from(">b", d, i)[0]; i += 1; return v
        if c == 0xD1: v = struct.unpack_from(">h", d, i)[0]; i += 2; return v
        if c == 0xD9: n = d[i]; i += 1; s = d[i:i + n].decode("utf-8", "replace"); i += n; return s
        if c == 0xDC:
            n = struct.unpack_from(">H", d, i)[0]; i += 2
            return [rd() for _ in range(n)]
        if c == 0xDD:
            n = struct.unpack_from(">I", d, i)[0]; i += 4
            return [rd() for _ in range(n)]
        if c == 0xDE:
            n = struct.unpack_from(">H", d, i)[0]; i += 2
            return {rd(): rd() for _ in range(n)}
        if c in (0xC7, 0xC8, 0xC9):          # ext8 / ext16 / ext32
            if c == 0xC7:
                n = d[i]; i += 1
            elif c == 0xC8:
                n = struct.unpack_from(">H", d, i)[0]; i += 2
            else:
                n = struct.unpack_from(">I", d, i)[0]; i += 4
            t = d[i]; i += 1
            payload = d[i:i + n]; i += n
            return {"$ext": t, "data": payload.hex()}
        raise ValueError(f"未支持标记 0x{c:02X}")

    return rd()


def to_bytes(buf) -> bytes:
    """IBuffer -> bytes。PyWinRT 的 read_bytes 只接受 winrt Array，这里逐字节读（包很小，够快）。"""
    from winrt.windows.storage.streams import DataReader
    dr = DataReader.from_buffer(buf)
    n = dr.unconsumed_buffer_length
    return bytes(dr.read_byte() for _ in range(n))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default=ADDR_DEFAULT)
    ap.add_argument("--info", action="store_true", help="发命令并等响应")
    ap.add_argument("--cmd", default="get_info",
                    help="要发送的命令名（V1: get_info/get_kb/get_mouse/get_screen/get_mt/get_cs/get_sleep）")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--key", default=None, help="附加字段 key（V1: key=all）")
    a = ap.parse_args()

    from winrt.windows.devices.bluetooth import BluetoothLEDevice

    mac = int(a.addr.replace(":", ""), 16)
    print(f"打开已配对设备 {a.addr} (0x{mac:X}) …")
    dev = await BluetoothLEDevice.from_bluetooth_address_async(mac)
    if dev is None:
        print("[!] 打不开设备：可能没配对过，或蓝牙适配器不可用")
        return 1
    print(f"  名称={dev.name!r}  连接状态={dev.connection_status}")

    try:
        res = await dev.get_gatt_services_async()
        svcs = list(res.services)
    except Exception as e:
        print(f"[!] 读服务失败: {type(e).__name__}: {e}")
        return 1

    print(f"\n共 {len(svcs)} 个服务：")
    rx = tx = None
    for s in svcs:
        u = str(s.uuid).lower()
        mark = "   <<< JoyMi 私有服务" if u == JOYMI_SVC else ""
        print(f"  {u}{mark}")
        if u != JOYMI_SVC:
            continue
        chs = list((await s.get_characteristics_async()).characteristics)
        for c in chs:
            cu = str(c.uuid).lower()
            props = c.characteristic_properties
            tag = ""
            if cu == AE41:
                tag = "   <<< 写入 RX"; rx = c
            elif cu == AE42:
                tag = "   <<< 通知 TX"; tx = c
            print(f"      ├ {cu}  props=0x{props:02X}{tag}")

    if not rx or not tx:
        print("\n[!] 没找到 AE41/AE42 —— 该设备的 AE40 服务不完整，或当前模式不暴露私有协议")
        return 1

    if not a.info:
        print("\n[=] AE40 / AE41 / AE42 都在。加 --info 可试发 g_devinfo。")
        return 0

    # 订阅通知
    got = asyncio.Queue()

    def on_changed(_sender, args):
        try:
            got.put_nowait(to_bytes(args.characteristic_value))
        except Exception as e:
            print("  通知解析失败:", e)

    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattClientCharacteristicConfigurationDescriptorValue as CCCD)
    tx.add_value_changed(on_changed)
    await tx.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)
    print("\n[ok] 已订阅 AE42 通知")

    payload = cmd_simple(a.cmd, a.key)
    print(f"发送 {a.cmd}  payload={payload.hex()}")
    from winrt.windows.storage.streams import DataWriter
    for f in build_frames(payload, 20):
        w = DataWriter(); w.write_bytes(f); buf = w.detach_buffer()
        r = await rx.write_value_with_result_async(buf)
        print(f"  写 {len(f)}B -> status={r.status}")

    try:
        frames = []
        while True:
            chunk = await asyncio.wait_for(got.get(), timeout=a.timeout)
            frames.append(chunk)
            # 首片带 bit7、低 7 位是剩余片数；收满即可
            if chunk[2] & 0x7F == 1:
                break
        body = b"".join(f[4:] for f in frames)
        print(f"\n收到 {len(frames)} 帧，payload={body.hex()}")
        try:
            print("解码：", mp_decode(body))
        except Exception as e:
            print(f"[!] 解码中断：{e}")
        print(f"    原始 payload（{len(body)}B）：{body.hex()}")
    except asyncio.TimeoutError:
        print(f"\n[!] {a.timeout:.0f} 秒内没有收到 {a.cmd} 的响应")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
