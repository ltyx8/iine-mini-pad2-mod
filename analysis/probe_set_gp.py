#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_set_gp.py —— 验证 set_gp 到底吃不吃写入。

分三步，全部可逆：
  1) 读 get_gp 现状
  2) 原样写回（key=0/gp=0/times=0）→ 再读
  3) 写一条真实映射（--key/--gp 指定，默认 key=8 → gp=7）→ 再读

用法：
    python probe_set_gp.py --dry                  # 只读，不写
    python probe_set_gp.py                        # 写入 key=8→gp=7
    python probe_set_gp.py --key 8 --gp 7 --times 0
"""
import argparse
import asyncio
import sys

import winrt_link as W

JOYMI_SVC = W.JOYMI_SVC
AE41, AE42 = W.AE41, W.AE42


def mp_i8(v: int) -> bytes:
    """App 实际用的是 0xCC（uint8）。smali 里写的是 const/16 v5, -0x34，
       -0x34 = -52 → 0xCC，不是 0xD0 —— 一开始算错过，以 logcat 抓到的真实字节为准。"""
    return bytes([0xCC, v & 0xFF])


def mp_i16(v: int) -> bytes:
    """times 对应 smali 的 -0x33 = 0xCD（uint16 大端）。"""
    n = v & 0xFFFF
    return bytes([0xCD, (n >> 8) & 0xFF, n & 0xFF])


def build_set_gp(entries) -> bytes:
    """entries: [(key, gp, times|None)]  → set_gp 手写字节"""
    out = bytearray()
    out.append(0x82)                       # map(2)
    k = b"cmd"
    out += bytes([0xA0 | len(k)]) + k
    c = b"set_gp"
    out += bytes([0xA0 | len(c)]) + c
    k = b"map"
    out += bytes([0xA0 | len(k)]) + k
    n = len(entries)
    out += bytes([0x90 | n]) if n < 16 else bytes([0xDC, (n >> 8) & 0xFF, n & 0xFF])
    for key, gp, times in entries:
        out.append(0x83 if times is not None else 0x82)
        for name, val in ((b"key", key), (b"gp", gp)):
            out += bytes([0xA0 | len(name)]) + name + mp_i8(val)
        if times is not None:
            name = b"times"
            out += bytes([0xA0 | len(name)]) + name + mp_i16(times)
    return bytes(out)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default=W.ADDR_DEFAULT)
    ap.add_argument("--key", type=int, default=8)
    ap.add_argument("--gp", type=int, default=7)
    ap.add_argument("--times", type=int, default=None)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--timeout", type=float, default=3.0)
    a = ap.parse_args()

    from winrt.windows.devices.bluetooth import BluetoothLEDevice

    mac = int(a.addr.replace(":", ""), 16)
    dev = await BluetoothLEDevice.from_bluetooth_address_async(mac)
    if dev is None:
        print("[!] 打不开设备")
        return 1

    svcs = list((await dev.get_gatt_services_async()).services)
    rx = tx = None
    for s in svcs:
        if str(s.uuid).lower() != JOYMI_SVC:
            continue
        for c in (await s.get_characteristics_async()).characteristics:
            cu = str(c.uuid).lower()
            if cu == AE41:
                rx = c
            elif cu == AE42:
                tx = c
    if not rx or not tx:
        print("[!] 没找到 AE41/AE42")
        return 1

    got = asyncio.Queue()
    tx.add_value_changed(lambda _s, args: got.put_nowait(W.to_bytes(args.characteristic_value)))
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattClientCharacteristicConfigurationDescriptorValue as CCCD)
    await tx.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)

    from winrt.windows.storage.streams import DataWriter

    async def send(payload):
        for f in W.build_frames(payload, 20):
            w = DataWriter()
            w.write_bytes(f)
            r = await rx.write_value_with_result_async(w.detach_buffer())
            if r.status != 0:
                print(f"    [!] 写帧 status={r.status}")

    async def read_gp(tag):
        while not got.empty():
            got.get_nowait()
        await send(W.cmd_simple("get_gp", "all"))
        frames = []
        try:
            while True:
                ch = await asyncio.wait_for(got.get(), timeout=a.timeout)
                frames.append(ch)
                if ch[2] & 0x7F == 1:
                    break
        except asyncio.TimeoutError:
            print(f"{tag}: 超时")
            return None
        body = b"".join(f[4:] for f in frames)
        obj = W.mp_decode(body)
        print(f"{tag}: {obj}")
        return obj

    await read_gp("① 现状")

    if a.dry:
        dev.close()
        return 0

    print("\n② 原样写回 key=0 gp=0 times=0")
    body = build_set_gp([(0, 0, 0)])
    print("   payload:", body.hex())
    await send(body)
    await asyncio.sleep(0.8)
    await read_gp("   写后回读")

    print(f"\n③ 写入 key={a.key} → gp={a.gp} times={a.times}")
    body = build_set_gp([(a.key, a.gp, a.times)])
    print("   payload:", body.hex())
    await send(body)
    await asyncio.sleep(0.8)
    await read_gp("   写后回读")

    try:
        dev.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
