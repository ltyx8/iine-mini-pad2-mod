#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v1_write.py —— set_mouse / set_cs / set_sleep 的读写工具。

字节格式全部来自 2026-10-29 用 adb logcat 抓到的官方 App 真实报文：

  set_mouse : 82 a3"cmd" a9"set_mouse" a3"map" <arr 17>
              每条 85 : {"key":cc u8, "but":cc u8, "w":d1 i16, "x":d1 i16, "y":d1 i16}
  set_cs    : 82 a3"cmd" a6"set_cs"    a3"map" <arr 17>
              每条 82 : {"key":cd u16, "cs":cd u16}
  set_sleep : 82 a3"cmd" a9"set_sleep" a3"map" 91 82 {"discon":cc u8, "con":cc u8}

用法：
    python v1_write.py --get all
    python v1_write.py --mouse 19=0,0,0,-2
    python v1_write.py --cs 19=181
    python v1_write.py --sleep 5,5
    python v1_write.py --restore          # 还原本次实验动过的三处

注意：必须全量写（数组长度 = 当前全部条目），只发 1 条固件直接忽略。
"""
import argparse
import asyncio
import sys

import winrt_link as W


# ---------------------------------------------------------------- msgpack 编码

def _str(s: str) -> bytes:
    b = s.encode()
    return bytes([0xA0 | len(b)]) + b


def _u8(v: int) -> bytes:
    return bytes([0xCC, v & 0xFF])


def _u16(v: int) -> bytes:
    n = v & 0xFFFF
    return bytes([0xCD, (n >> 8) & 0xFF, n & 0xFF])


def _i16(v: int) -> bytes:
    n = v & 0xFFFF
    return bytes([0xD1, (n >> 8) & 0xFF, n & 0xFF])


def _arr(n: int) -> bytes:
    return bytes([0x90 | n]) if n < 16 else bytes([0xDC, (n >> 8) & 0xFF, n & 0xFF])


def _map(n: int) -> bytes:
    return bytes([0x80 | n])


def build_set_mouse(rows) -> bytes:
    out = bytearray(_map(2))
    out += _str("cmd") + _str("set_mouse")
    out += _str("map") + _arr(len(rows))
    for key, but, w, x, y in rows:
        out += _map(5)
        out += _str("key") + _u8(key)
        out += _str("but") + _u8(but)
        out += _str("w") + _i16(w)
        out += _str("x") + _i16(x)
        out += _str("y") + _i16(y)
    return bytes(out)


def build_set_cs(rows) -> bytes:
    out = bytearray(_map(2))
    out += _str("cmd") + _str("set_cs")
    out += _str("map") + _arr(len(rows))
    for key, cs in rows:
        out += _map(2)
        out += _str("key") + _u16(key)
        out += _str("cs") + _u16(cs)
    return bytes(out)


def build_set_sleep(discon: int, con: int) -> bytes:
    out = bytearray(_map(2))
    out += _str("cmd") + _str("set_sleep")
    out += _str("map")
    out += bytes([0x91])
    out += _map(2)
    out += _str("discon") + _u8(discon)
    out += _str("con") + _u8(con)
    return bytes(out)


# ---------------------------------------------------------------- 主流程

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default=W.ADDR_DEFAULT)
    ap.add_argument("--get", choices=["all", "mouse", "cs", "sleep"])
    ap.add_argument("--mouse", action="append", default=[], help="key=but,w,x,y")
    ap.add_argument("--cs", action="append", default=[], help="key=value")
    ap.add_argument("--sleep", help="discon,con")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--timeout", type=float, default=5.0)
    a = ap.parse_args()

    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattClientCharacteristicConfigurationDescriptorValue as CCCD)
    from winrt.windows.storage.streams import DataWriter

    mac = int(a.addr.replace(":", ""), 16)
    dev = await BluetoothLEDevice.from_bluetooth_address_async(mac)
    if dev is None:
        print("[!] 打不开设备")
        return 1

    rx = tx = None
    for s in (await dev.get_gatt_services_async()).services:
        if str(s.uuid).lower() != W.JOYMI_SVC:
            continue
        for c in (await s.get_characteristics_async()).characteristics:
            cu = str(c.uuid).lower()
            if cu == W.AE41:
                rx = c
            elif cu == W.AE42:
                tx = c
    if not rx or not tx:
        print("[!] 没找到 AE41/AE42")
        return 1

    got = asyncio.Queue()

    def on_changed(_sender, args):
        try:
            got.put_nowait(W.to_bytes(args.characteristic_value))
        except Exception as e:
            print("    [!] 通知解析失败:", e)

    tx.add_value_changed(on_changed)
    await tx.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)
    await asyncio.sleep(0.6)

    async def send(payload):
        for f in W.build_frames(payload, 20):
            w = DataWriter()
            w.write_bytes(f)
            r = await rx.write_value_with_result_async(w.detach_buffer())
            if r.status != 0:
                print(f"    [!] 写帧 status={r.status}")

    async def query(cmd):
        while not got.empty():
            got.get_nowait()
        await send(W.cmd_simple(cmd, "all"))
        frames = []
        try:
            while True:
                ch = await asyncio.wait_for(got.get(), timeout=a.timeout)
                frames.append(ch)
                if ch[2] & 0x7F == 1:
                    break
        except asyncio.TimeoutError:
            return None
        return W.mp_decode(b"".join(f[4:] for f in frames))

    def rows_of(obj, name):
        if not obj:
            print(f"    [!] {name} 读取失败/超时")
            return []
        m = obj.get("map") if isinstance(obj, dict) else None
        return m if isinstance(m, list) else []

    want_mouse = {}
    for s in a.mouse:
        k, v = s.split("=")
        want_mouse[int(k)] = [int(t) for t in v.split(",")]
    want_cs = {}
    for s in a.cs:
        k, v = s.split("=")
        want_cs[int(k)] = int(v)

    if a.restore:
        want_mouse[19] = [0, 0, 0, -2]      # 十字右 → 鼠标上移（出厂）
        want_cs[19] = 181                   # 十字右 → 下一首（出厂）

    do_write = bool(want_mouse or want_cs or a.sleep)

    # ---------- 读 ----------
    print("[读] get_sleep")
    sl = rows_of(await query("get_sleep"), "get_sleep")
    for it in sl:
        print("    ", it)

    mrows = []
    if a.get in (None, "all", "mouse") or want_mouse:
        if not want_mouse or a.get in ("all", "mouse") or a.restore:
            print("[读] get_mouse")
            for it in rows_of(await query("get_mouse"), "get_mouse"):
                print(f"     key={it['key']:<3} but={it.get('but',0)} w={it.get('w',0)} "
                      f"x={it.get('x',0)} y={it.get('y',0)}")
        # 写入需要全量，重新读一份干净的
        raw = rows_of(await query("get_mouse"), "get_mouse")
        for it in raw:
            k = it["key"]
            but, w_, x_, y_ = it.get("but", 0), it.get("w", 0), it.get("x", 0), it.get("y", 0)
            if k in want_mouse:
                but, w_, x_, y_ = want_mouse[k]
            mrows.append((k, but, w_, x_, y_))

    crows = []
    if (a.get in (None, "all", "cs")) or want_cs or a.restore:
        if want_cs or a.get in ("all", "cs") or a.restore:
            print("[读] get_cs")
            for it in rows_of(await query("get_cs"), "get_cs"):
                cs = it.get("cs", 0)
                print(f"     key={it['key']:<3} cs={cs} (0x{cs:02X})")
        raw = rows_of(await query("get_cs"), "get_cs")
        for it in raw:
            k, cs = it["key"], it.get("cs", 0)
            if k in want_cs:
                cs = want_cs[k]
            crows.append((k, cs))

    if not do_write:
        try:
            dev.close()
        except Exception:
            pass
        return 0

    # ---------- 写 ----------
    if mrows and want_mouse:
        body = build_set_mouse(mrows)
        print(f"\n[写] set_mouse {len(mrows)} 条 / {len(body)} 字节")
        await send(body)
        await asyncio.sleep(1.0)
        back = rows_of(await query("get_mouse"), "回读")
        for it in back:
            if it["key"] in want_mouse:
                got4 = (it.get("but", 0), it.get("w", 0), it.get("x", 0), it.get("y", 0))
                exp = tuple(want_mouse[it["key"]])
                print(f"     回读 key={it['key']}: {got4} 期望 {exp} {'OK' if got4 == exp else 'FAIL'}")

    if crows and want_cs:
        body = build_set_cs(crows)
        print(f"\n[写] set_cs {len(crows)} 条 / {len(body)} 字节")
        await send(body)
        await asyncio.sleep(1.0)
        back = rows_of(await query("get_cs"), "回读")
        for it in back:
            if it["key"] in want_cs:
                g, e = it.get("cs", 0), want_cs[it["key"]]
                print(f"     回读 key={it['key']}: {g} 期望 {e} {'OK' if g == e else 'FAIL'}")

    if a.sleep or a.restore:
        if a.sleep:
            d, c = [int(t) for t in a.sleep.split(",")]
        else:
            d = c = 5
        print(f"\n[写] set_sleep discon={d} con={c}")
        await send(build_set_sleep(d, c))
        await asyncio.sleep(1.0)
        back = rows_of(await query("get_sleep"), "回读")
        for it in back:
            ok = it.get("discon") == d and it.get("con") == c
            print(f"     回读 {it} 期望 {d}/{c} {'OK' if ok else 'FAIL'}")

    try:
        dev.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
