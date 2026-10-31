#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dump_all.py —— 一次连接把 V1 所有配置命令读一遍，结果存 JSON。

用法：
    python dump_all.py
    python dump_all.py --timeout 4
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import winrt_link as W

JOYMI_SVC = W.JOYMI_SVC
AE41, AE42 = W.AE41, W.AE42

CMDS = [
    ("get_info", "all"),
    ("get_kb", "all"),
    ("get_gp", "all"),
    ("get_mouse", "all"),
    ("get_cs", "all"),
    ("get_mt", "all"),
    ("get_screen", "all"),
    ("get_sleep", "all"),
]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default=W.ADDR_DEFAULT)
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--out", default="v1_dump.json")
    a = ap.parse_args()

    from winrt.windows.devices.bluetooth import BluetoothLEDevice

    mac = int(a.addr.replace(":", ""), 16)
    dev = await BluetoothLEDevice.from_bluetooth_address_async(mac)
    if dev is None:
        print("[!] 打不开设备")
        return 1
    print(f"已打开 {dev.name!r}")

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
            await rx.write_value_with_result_async(w.detach_buffer())

    result = {}
    for cmd, key in CMDS:
        while not got.empty():
            got.get_nowait()
        await send(W.cmd_simple(cmd, key))
        frames = []
        try:
            while True:
                ch = await asyncio.wait_for(got.get(), timeout=a.timeout)
                frames.append(ch)
                if ch[2] & 0x7F == 1:
                    break
        except asyncio.TimeoutError:
            print(f"-- {cmd:10} 超时无响应")
            result[cmd] = None
            continue
        body = b"".join(f[4:] for f in frames)
        try:
            obj = W.mp_decode(body)
        except Exception as e:
            print(f"-- {cmd:10} 解码失败 {e}  raw={body.hex()}")
            result[cmd] = {"_raw": body.hex()}
            continue
        result[cmd] = obj
        n = len(obj.get("map", [])) if isinstance(obj, dict) and isinstance(obj.get("map"), list) else "-"
        print(f"-- {cmd:10} 帧={len(frames):2} 条数={n}")

    out = Path(a.out)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n已写入 {out.resolve()}")

    try:
        dev.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
