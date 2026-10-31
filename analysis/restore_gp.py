#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restore_gp.py —— 全量写 set_gp（一次把 17 条都写进去）。

实测结论：
  * 只发 1 条 set_gp，固件直接忽略（回读不变）
  * 必须像 App 一样一次写全部条目，固件才接受

用法：
    python restore_gp.py --show                      # 只读
    python restore_gp.py --set 15=15                 # 把 key=15 的 gp 改成 15
    python restore_gp.py --set 15=15 --set 8=7       # 可以写多条
"""
import argparse
import asyncio
import sys

import winrt_link as W
from probe_set_gp import build_set_gp

JOYMI_SVC = W.JOYMI_SVC
AE41, AE42 = W.AE41, W.AE42


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default=W.ADDR_DEFAULT)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=GP",
                    help="把某个槽位映射到另一个键，可重复")
    ap.add_argument("--show", action="store_true")
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
                print(f"    [!] 帧写失败 status={r.status}")

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
            return []
        body = b"".join(f[4:] for f in frames)
        obj = W.mp_decode(body)
        rows = obj.get("map", []) if isinstance(obj, dict) else []
        print(f"{tag}: {len(rows)} 条")
        for it in rows:
            print(f"    key={it.get('key'):>3}  gp={it.get('gp'):>3}  times={it.get('times')}")
        return rows

    rows = await read_gp("① 现状")
    if a.show or not a.set:
        try:
            dev.close()
        except Exception:
            pass
        return 0

    if not rows:
        print("\n[!] 表是空的：先让官方 App 在「手柄」栏改一次并点应用，固件才会建立这张表")
        try:
            dev.close()
        except Exception:
            pass
        return 1

    # 改指定槽位，其余原样
    want = {}
    for item in a.set:
        k, _, v = item.partition("=")
        want[int(k)] = int(v)
    entries = []
    for it in rows:
        k = it.get("key")
        g = want.pop(k, it.get("gp"))
        # App 实际写的是 map(2)，不带 times（logcat 抓包确认）。回读出来的 times=0 不要照抄回去。
        entries.append((k, g, None))

    print(f"\n② 全量写回 {len(entries)} 条（改 {len(a.set)} 个槽位）")
    body = build_set_gp(entries)
    print("   payload:", body.hex())
    await send(body)
    await asyncio.sleep(1.0)
    await read_gp("   写后回读")

    try:
        dev.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
