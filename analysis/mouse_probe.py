"""鼠标表读写探针：读 → 备份 → 改写 → 回读 → 还原。只依赖本地桥 8848。"""
import json
import sys
import time
import urllib.request

BRIDGE = "http://127.0.0.1:8848"
BACKUP = r"D:\Cache\wb\top.iine.android.client\analysis\mouse_backup.json"


# ---------------- msgpack 最小实现 ----------------
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
    if c == 0xD9:
        n = b[i + 1]
        return b[i + 2:i + 2 + n].decode("utf-8", "replace"), i + 2 + n
    if c == 0xC0:
        return None, i + 1
    if c == 0xC2:
        return False, i + 1
    if c == 0xC3:
        return True, i + 1
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


def pack_i16(v):
    return bytes([0xD1]) + int(v).to_bytes(2, "big", signed=True)


def build_set_mouse(rows):
    out = bytes([0x82]) + pack_str("cmd") + pack_str("set_mouse") + pack_str("map")
    out += bytes([0xDC]) + len(rows).to_bytes(2, "big")
    for r in rows:
        out += bytes([0x85])
        out += pack_str("key") + pack_u8(r["key"])
        out += pack_str("but") + pack_u8(r["but"])
        out += pack_str("w") + pack_i16(r["w"])
        out += pack_str("x") + pack_i16(r["x"])
        out += pack_str("y") + pack_i16(r["y"])
    return out


def build_get(cmd):
    out = bytes([0x82]) + pack_str("cmd") + pack_str(cmd) + pack_str("key") + pack_str("all")
    return out


# ---------------- 桥调用 ----------------
def send(payload, timeout=3.0, cmd=None):
    body = json.dumps({"hex": payload.hex(), "timeout": timeout, "cmd": cmd}).encode()
    req = urllib.request.Request(BRIDGE + "/api/send", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout + 8) as r:
        return json.load(r)


def read_mouse():
    j = send(build_get("get_mouse"), 3.0, "ret_mouse")
    if j.get("timeout"):
        raise SystemExit("get_mouse 超时")
    if j.get("error"):
        raise SystemExit("get_mouse 出错：" + j["error"])
    obj, _ = un(bytes.fromhex(j["payload"]))
    return obj["map"]


def write_mouse(rows):
    p = build_set_mouse(rows)
    t = time.time()
    j = send(p, 1.5)
    return len(p), time.time() - t, j


if __name__ == "__main__":
    rows = read_mouse()
    print("读回 %d 条，载荷 %d 字节" % (len(rows), len(build_set_mouse(rows))))
    for r in rows:
        print("  key=%-3d but=%-3d w=%-3d x=%-3d y=%-3d" %
              (r["key"], r["but"], r["w"], r["x"], r["y"]))
    with open(BACKUP, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("已备份到", BACKUP)
