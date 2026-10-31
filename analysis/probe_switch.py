"""Switch 模式下用 bleak 直连 0xAE40，验证 V1 私有协议是否仍可用。

设备在 Switch 模式会换成随机地址、广播名 IG05 Gamepad(BLE)，地址需用 --addr 传入。
"""
import argparse
import asyncio
import struct
import sys

from bleak import BleakClient

HEAD = 0xB4
TAG = 0xCC
SVC_AE40 = "0000ae40-0000-1000-8000-00805f9b34fb"
CH_AE41 = "0000ae41-0000-1000-8000-00805f9b34fb"
CH_AE42 = "0000ae42-0000-1000-8000-00805f9b34fb"
CH_AE02 = "0000ae02-0000-1000-8000-00805f9b34fb"


def mp_str(s: str) -> bytes:
    b = s.encode()
    return bytes([0xA0 | len(b)]) + b


def cmd_simple(name: str, key: str | None = None) -> bytes:
    pairs = [("cmd", mp_str(name))]
    if key:
        pairs.append(("key", mp_str(key)))
    return bytes([0x80 | len(pairs)]) + b"".join(mp_str(k) + v for k, v in pairs)


def build_frames(payload: bytes, chunk: int = 20) -> list[bytes]:
    n = (len(payload) + chunk - 1) // chunk
    out = []
    for i in range(n):
        part = payload[i * chunk:(i + 1) * chunk]
        seq = (n - i) | (0x80 if i == 0 else 0)
        out.append(bytes([HEAD, len(part) + 4, seq & 0xFF, TAG]) + part)
    return out


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
        if c in (0xC7, 0xC8, 0xC9):
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


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", required=True)
    ap.add_argument("--chunk", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--cmds", default="get_info,get_kb,get_gp,get_mouse,get_cs,get_sleep")
    a = ap.parse_args()

    frames_buf: list[bytes] = []
    got_done = asyncio.Event()

    def on_notify(_ch, data: bytearray):
        b = bytes(data)
        if not b or b[0] != HEAD:
            return
        frames_buf.append(b)
        if b[2] & 0x7F == 1:
            got_done.set()

    async with BleakClient(a.addr, timeout=20) as c:
        print(f"connected={c.is_connected} mtu={getattr(c, 'mtu_size', '?')}", flush=True)
        await c.start_notify(CH_AE42, on_notify)
        for uuid in (CH_AE02,):
            try:
                await c.start_notify(uuid, lambda _c, _d: None)
            except Exception:
                pass
        await asyncio.sleep(0.4)

        for name in a.cmds.split(","):
            name = name.strip()
            if not name:
                continue
            key = "all" if name not in ("get_info", "get_sleep") else None
            if name == "get_info":
                key = "all"
            payload = cmd_simple(name, key)
            frames_buf.clear()
            got_done.clear()
            for f in build_frames(payload, a.chunk):
                await c.write_gatt_char(CH_AE41, f, response=False)
                await asyncio.sleep(0.02)
            try:
                await asyncio.wait_for(got_done.wait(), a.timeout)
                await asyncio.sleep(0.05)
                body = b"".join(f[4:] for f in frames_buf)
                try:
                    obj = mp_decode(body)
                except Exception as e:
                    obj = f"<decode err {e}> hex={body.hex()[:120]}"
                if isinstance(obj, dict):
                    m = obj.get("map")
                    n = len(m) if isinstance(m, list) else "-"
                    print(f"{name:10} -> {obj.get('cmd')}  map={n}", flush=True)
                    if isinstance(m, list):
                        for it in m[:20]:
                            print(f"             {it}", flush=True)
                    else:
                        print(f"             {obj}", flush=True)
                else:
                    print(f"{name:10} -> {obj}", flush=True)
            except asyncio.TimeoutError:
                print(f"{name:10} -> TIMEOUT ({len(frames_buf)} 帧)", flush=True)

        await c.stop_notify(CH_AE42)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
