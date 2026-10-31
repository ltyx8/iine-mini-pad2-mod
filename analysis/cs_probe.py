"""媒体键表（set_cs）读写探针：读 → 备份 → 改写 → 回读。只依赖本地桥 8848。

set_cs 结构（logcat 实抓）：
    82 a3"cmd" a6"set_cs" a3"map" dc<len>
       82 a3"key" CD<u16> a2"cs" CD<u16>
key / cs 都是 uint16(0xCD)，条目数必须与读到的条数一致（全量写）。
"""
import json
import time
import urllib.request

BRIDGE = "http://127.0.0.1:8848"
BACKUP = r"D:\Cache\wb\top.iine.android.client\analysis\cs_backup.json"


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
    if c == 0xCC:
        return b[i + 1], i + 2
    if c == 0xCD:
        return int.from_bytes(b[i + 1:i + 3], "big"), i + 3
    if c == 0xD0:
        return int.from_bytes(b[i + 1:i + 2], "big", signed=True), i + 2
    if c == 0xD1:
        return int.from_bytes(b[i + 1:i + 3], "big", signed=True), i + 3
    raise ValueError("tag 0x%02x @%d" % (c, i))


def ps(s):
    b = s.encode()
    return bytes([0xA0 | len(b)]) + b


def u16(v):
    return bytes([0xCD]) + int(v).to_bytes(2, "big")


def build_set_cs(rows):
    out = bytes([0x82]) + ps("cmd") + ps("set_cs") + ps("map")
    out += bytes([0xDC]) + len(rows).to_bytes(2, "big")
    for r in rows:
        out += bytes([0x82]) + ps("key") + u16(r["key"]) + ps("cs") + u16(r["cs"])
    return out


def build_get(cmd):
    return bytes([0x82]) + ps("cmd") + ps(cmd) + ps("key") + ps("all")


def send(payload, timeout=3.0, cmd=None):
    body = json.dumps({"hex": payload.hex(), "timeout": timeout, "cmd": cmd}).encode()
    req = urllib.request.Request(BRIDGE + "/api/send", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout + 8) as r:
        return json.load(r)


def read_cs():
    j = send(build_get("get_cs"), 3.0, "ret_cs")
    if j.get("timeout"):
        raise SystemExit("get_cs 超时")
    if j.get("error"):
        raise SystemExit("get_cs 出错：" + j["error"])
    obj, _ = un(bytes.fromhex(j["payload"]))
    return [{"key": r["key"], "cs": r["cs"]} for r in obj["map"]]


def write_cs(rows):
    p = build_set_cs(rows)
    t = time.time()
    j = send(p, 1.5)
    return len(p), time.time() - t, j


if __name__ == "__main__":
    rows = read_cs()
    print("媒体键表 %d 条，载荷 %d 字节：" % (len(rows), len(build_set_cs(rows))))
    for r in rows:
        print("  key=%-3d cs=0x%04X (%d)" % (r["key"], r["cs"], r["cs"]))
    with open(BACKUP, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("已备份到", BACKUP)
