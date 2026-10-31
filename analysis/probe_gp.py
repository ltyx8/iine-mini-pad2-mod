#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_gp.py —— 一次连接里连着发多条 V1 命令，专门摸清 get_gp 什么时候有数据。

用法： python probe_gp.py [--addr F8:B4:9A:CC:71:CE]
"""
import argparse
import asyncio
import sys

import winrt_link as W

JOYMI_SVC = W.JOYMI_SVC
AE41, AE42 = W.AE41, W.AE42


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default=W.ADDR_DEFAULT)
    ap.add_argument("--timeout", type=float, default=3.0)
    a = ap.parse_args()

    from winrt.windows.devices.bluetooth import BluetoothLEDevice

    mac = int(a.addr.replace(":", ""), 16)
    dev = await BluetoothLEDevice.from_bluetooth_address_async(mac)
    if dev is None:
        print("[!] 打不开设备")
        return 1
    print(f"已打开 {dev.name!r}  connection_status={dev.connection_status}")

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
    print("已订阅 AE42\n")

    from winrt.windows.storage.streams import DataWriter

    async def probe(cmd, key, label=None):
        while not got.empty():
            got.get_nowait()
        payload = W.cmd_simple(cmd, key)
        for f in W.build_frames(payload, 20):
            w = DataWriter()
            w.write_bytes(f)
            await rx.write_value_with_result_async(w.detach_buffer())
        frames = []
        try:
            while True:
                chunk = await asyncio.wait_for(got.get(), timeout=a.timeout)
                frames.append(chunk)
                if chunk[2] & 0x7F == 1:
                    break
            body = b"".join(f[4:] for f in frames)
            obj = W.mp_decode(body)
            n = len(obj.get("map", [])) if isinstance(obj, dict) else "-"
            print(f"[{label or cmd} key={key!r}] 帧={len(frames)} 条数={n}")
            if isinstance(obj, dict) and obj.get("map"):
                for it in obj["map"][:25]:
                    print("    ", it)
            else:
                print("     raw:", body.hex())
        except asyncio.TimeoutError:
            print(f"[{label or cmd} key={key!r}] 超时无响应")

    await probe("get_info", "all")
    await probe("get_kb", "all")
    await probe("get_gp", "all")
    await probe("get_gp", None)
    await probe("get_gp", "gp")
    await probe("get_xinput", "all")
    await probe("get_mouse", "all")

    try:
        dev.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
