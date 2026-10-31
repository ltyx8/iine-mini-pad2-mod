#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ble_scan.py —— 良值手柄 BLE 侧现场诊断

用途：在写死"能不能连"之前，先确认三件事
  1. 手柄当前有没有在 BLE 广播（名字是什么）
  2. 它暴露了哪些 GATT 服务（重点看有没有 JoyMi 私有服务 0xAE40）
  3. 特征值 AE41/AE42 是否可写/可通知

用法：
    python ble_scan.py                 # 扫 12 秒，列出手柄相关设备
    python ble_scan.py -t 20           # 扫 20 秒
    python ble_scan.py --all           # 列出全部 BLE 设备（不过滤）
    python ble_scan.py --dump <ADDR>   # 连接指定设备并打印完整服务树
"""
import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

JOYMI_SVC = "0000ae40-0000-1000-8000-00805f9b34fb"
AE41 = "0000ae41-0000-1000-8000-00805f9b34fb"
AE42 = "0000ae42-0000-1000-8000-00805f9b34fb"

HINTS = ("iine", "wireless controller", "xbox", "joy", "pad", "controller", "gamepad")


def interesting(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in HINTS)


async def scan(timeout: float, show_all: bool):
    print(f"扫描 {timeout:.0f} 秒（手柄需处于「快闪」广播状态）…\n")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    rows = []
    for addr, (dev, adv) in found.items():
        name = dev.name or adv.local_name or ""
        rows.append((name, addr, adv.rssi, dev))
    rows.sort(key=lambda r: (-(r[2] or -999)))

    hit = [r for r in rows if interesting(r[0])]
    show = rows if show_all else hit

    if not show:
        print("没有发现手柄相关的 BLE 广播。")
        print("排查：① 手柄是否已开机 ② 是否已按组合键进入快闪 ③ 是否已被电脑/手机连走")
        if not show_all:
            print(f"（本次共扫到 {len(rows)} 个 BLE 设备，加 --all 可看全部）")
        return

    print(f"{'名称':<32} {'地址':<20} {'RSSI':>6}")
    print("-" * 62)
    for name, addr, rssi, _ in show:
        print(f"{(name or '(无名)'):<32} {addr:<20} {rssi:>6}")
    print(f"\n共 {len(show)} 个" + ("" if show_all else f"（全部扫到 {len(rows)} 个）"))

    # 只对像是手柄的设备去连服务，别在无关设备上各耗 15s 超时
    for name, addr, _rssi, _dev in hit:
        print(f"\n=== 检查服务：{name or addr} ===")
        await dump(addr, quick=True)
    if show_all and not hit:
        print("\n（以上名称都不像手柄，未逐个连服务）")


async def watch(duration: float):
    """持续监听，实时打印新出现的广播设备 —— 适合「你现在去按组合键」的场景。"""
    import time as _t
    seen = {}
    hits = []

    def cb(dev, adv):
        name = dev.name or adv.local_name or ""
        if dev.address in seen:
            return
        seen[dev.address] = name
        flag = "   <<< 像手柄" if interesting(name) else ""
        print(f"  [{_t.strftime('%H:%M:%S')}] {dev.address}  {(name or '(无名)'):<30} "
              f"RSSI={adv.rssi}{flag}", flush=True)
        if interesting(name):
            hits.append((name, dev.address))

    print(f"监听 {max(0, duration):.0f} 秒 —— 现在去操作手柄（按组合键进快闪），"
          f"新出现的设备会实时打印。\n")
    async with BleakScanner(detect_callback=cb):
        await asyncio.sleep(duration)

    print(f"\n共发现 {len(seen)} 个设备，其中像手柄的 {len(hits)} 个。")
    for name, addr in hits:
        print(f"\n=== 检查服务：{name or addr} ===")
        await dump(addr, quick=True)


async def dump(address: str, quick: bool = False):
    try:
        async with BleakClient(address, timeout=15.0) as cli:
            svcs = cli.services
            if not svcs:
                print("  连上了但读不到服务列表")
                return
            has_joymi = False
            for s in svcs:
                u = str(s.uuid).lower()
                mark = ""
                if u == JOYMI_SVC:
                    mark = "   <<< JoyMi 私有服务（本页要用的）"
                    has_joymi = True
                print(f"  服务 {u}  {s.description or ''}{mark}")
                if quick:
                    continue
                for ch in s.characteristics:
                    props = ",".join(ch.properties)
                    tag = ""
                    if str(ch.uuid).lower() == AE41:
                        tag = "   <<< 写入(RX)"
                    elif str(ch.uuid).lower() == AE42:
                        tag = "   <<< 通知(TX)"
                    print(f"      ├ {str(ch.uuid).lower()}  [{props}]{tag}")
            print()
            if has_joymi:
                print("  ✅ 有 AE40：本页的改键 / 按键监控可用")
            else:
                print("  ❌ 没有 AE40：当前是标准蓝牙 HID 模式，网页端的私有协议功能用不了。")
                print("     换一个模式（见说明书对照表）再扫一次。")
    except Exception as e:
        print(f"  连接/读取失败：{type(e).__name__}: {e}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-t", "--timeout", type=float, default=12.0)
    ap.add_argument("--all", action="store_true", help="列出全部 BLE 设备")
    ap.add_argument("--watch", type=float, metavar="SEC", help="持续监听 SEC 秒，实时打印新设备")
    ap.add_argument("--dump", metavar="ADDR", help="连接并打印完整服务树")
    a = ap.parse_args()

    if a.dump:
        await dump(a.dump, quick=False)
    elif a.watch is not None:
        await watch(a.watch)
    else:
        await scan(a.timeout, a.all)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
