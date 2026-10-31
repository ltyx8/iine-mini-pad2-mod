"""键盘表读写探针：读 → 备份 → 改写 → 回读。只依赖本地桥 8848。

set_kb 结构（App 实测）：
    82 a3"cmd" a6"set_kb" a3"map" dc<len>
       83 a3"key" CC<key> a2"fn" CC<fn> a3"kb" c7 03 00 <b0> <b1> <b2>
key / fn 都是 uint8(0xCC)，kb 是 ext8(0xC7) 的 3 字节数组。
"""
import json
import sys
import time
import urllib.request

BRIDGE = "http://127.0.0.1:8848"
BACKUP = r"D:\Cache\wb\top.iine.android.client\analysis\kb_backup.json"


def un(b, i=0):
    c = b[i]
    if c <= 0x7F:
        return c, i + 1
    if 0x80 <= c <= 0x8F:
        n = c & 0x0F
        i += 1
        d = {}
        for _ in range(n):
            k, i = un(b, i)
            v, i = un(b, i)
            d[k] = v
        return d, i
    if 0x90 <= c <= 0x9F:
        n = c & 0x0F
        i += 1
        a = []
    elif c == 0xDC:
        n = int.from_bytes(b[i + 1:i + 3], "big")
        i += 3
        a = []
    elif c == 0xDD:
        n = int.from_bytes(b[i + 1:i + 5], "big")
        i += 5
        a = []
    else:
        a = None
        n = 0
    if a is not None:
        for _ in range(n):
            v, i = un(b, i)
            a.append(v)
        return a, i
    if 0xA0 <= c <= 0xBF:
        n = c & 0x1F
        return b[i + 1:i + 1 + n].decode("utf-8", "replace"), i + 1 + n
    if c == 0xC0:
        return None, i + 1
    if c == 0xC2:
        return False, i + 1
    if c == 0xC3:
        return True, i + 1
    if c == 0xC4:
        n = b[i + 1]
        return bytes(b[i + 2:i + 2 + n]), i + 2 + n
    if c == 0xC5:
        n = int.from_bytes(b[i + 1:i + 3], "big")
        return bytes(b[i + 3:i + 3 + n]), i + 3 + n
    if c == 0xC7:                                  # ext8
        n = b[i + 1]
        return (b[i + 2], bytes(b[i + 3:i + 3 + n])), i + 3 + n
    if c == 0xCC:
        return b[i + 1], i + 2
    if c == 0xCD:
        return int.from_bytes(b[i + 1:i + 3], "big"), i + 3
    if c == 0xD0:
        return int.from_bytes(b[i + 1:i + 2], "big", signed=True), i + 2
    if c == 0xD1:
        return int.from_bytes(b[i + 1:i + 3], "big", signed=True), i + 3
    raise ValueError("tag 0x%02x @%d" % (c, i))


def pack_str(s):
    b = s.encode()
    return bytes([0xA0 | len(b)]) + b


def pack_u8(v):
    return bytes([0xCC, v & 0xFF])


def pack_ext3(arr):
    return bytes([0xC7, 3, 0]) + bytes(arr[:3]).ljust(3, b"\x00")


def build_set_kb(rows):
    out = bytes([0x82]) + pack_str("cmd") + pack_str("set_kb") + pack_str("map")
    out += bytes([0xDC]) + len(rows).to_bytes(2, "big")
    for r in rows:
        out += bytes([0x83])
        out += pack_str("key") + pack_u8(r["key"])
        out += pack_str("fn") + pack_u8(r["fn"])
        out += pack_str("kb") + pack_ext3(r["kb"])
    return out


def build_get(cmd):
    return bytes([0x82]) + pack_str("cmd") + pack_str(cmd) + pack_str("key") + pack_str("all")


def send(payload, timeout=3.0, cmd=None):
    body = json.dumps({"hex": payload.hex(), "timeout": timeout, "cmd": cmd}).encode()
    req = urllib.request.Request(BRIDGE + "/api/send", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout + 8) as r:
        return json.load(r)


def read_kb():
    j = send(build_get("get_kb"), 3.0, "ret_kb")
    if j.get("timeout"):
        raise SystemExit("get_kb 超时")
    if j.get("error"):
        raise SystemExit("get_kb 出错：" + j["error"])
    obj, _ = un(bytes.fromhex(j["payload"]))
    rows = []
    for it in obj["map"]:
        ext = it.get("kb")
        raw = ext[1] if isinstance(ext, tuple) else (ext or b"")
        rows.append({"key": it["key"], "fn": it.get("fn", 0) or 0,
                     "kb": list(bytes(raw)[:3]) + [0] * (3 - len(bytes(raw)[:3]))})
    return rows


def write_kb(rows):
    p = build_set_kb(rows)
    t = time.time()
    j = send(p, 1.5)
    return len(p), time.time() - t, j


def dump(rows, tag=""):
    print("%s%d 条：" % (tag, len(rows)))
    for r in rows:
        print("  key=%-3d fn=%-2d kb=%s" %
              (r["key"], r["fn"], " ".join("%02X" % x for x in r["kb"])))


if __name__ == "__main__":
    rows = read_kb()
    dump(rows, "键盘表 ")
    print("载荷 %d 字节" % len(build_set_kb(rows)))
    with open(BACKUP, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("已备份到", BACKUP)
