"""手柄键映射表（set_gp）读写探针：读 → 备份 → 改写 → 回读。只依赖本地桥 8848。

set_gp 结构（logcat 实抓）：
    82 a3"cmd" a6"set_gp" a3"map" dc<len>
       82 a3"key" CC<u8> a2"gp" CC<u8>
key / gp 都是 uint8(0xCC)，gp 值域 = 槽位号（与 get_kb 的 key 同一套）。
出厂这张表是空的（map:[]），写过一次才存在。
"""
import json
import time
import urllib.request

BRIDGE = "http://127.0.0.1:8848"
BACKUP = r"D:\Cache\wb\top.iine.android.client\analysis\gp_backup.json"

SLOTS = [0, 1, 3, 4, 6, 7, 8, 9, 10, 11, 12, 15, 16, 17, 18, 19, 23]


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


def u8(v):
    return bytes([0xCC, v & 0xFF])


def build_set_gp(rows):
    out = bytes([0x82]) + ps("cmd") + ps("set_gp") + ps("map")
    out += bytes([0xDC]) + len(rows).to_bytes(2, "big")
    for r in rows:
        out += bytes([0x82]) + ps("key") + u8(r["key"]) + ps("gp") + u8(r["gp"])
    return out


def build_get(cmd):
    return bytes([0x82]) + ps("cmd") + ps(cmd) + ps("key") + ps("all")


def send(payload, timeout=3.0, cmd=None):
    body = json.dumps({"hex": payload.hex(), "timeout": timeout, "cmd": cmd}).encode()
    req = urllib.request.Request(BRIDGE + "/api/send", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout + 8) as r:
        return json.load(r)


def read_gp():
    j = send(build_get("get_gp"), 3.0, "ret_gp")
    if j.get("timeout"):
        raise SystemExit("get_gp 超时")
    if j.get("error"):
        raise SystemExit("get_gp 出错：" + j["error"])
    obj, _ = un(bytes.fromhex(j["payload"]))
    return [{"key": r["key"], "gp": r["gp"]} for r in (obj.get("map") or [])]


def write_gp(rows):
    p = build_set_gp(rows)
    t = time.time()
    j = send(p, 1.5)
    return len(p), time.time() - t, j


if __name__ == "__main__":
    rows = read_gp()
    if not rows:
        print("gp 表为空（出厂状态，等价于每个键映射为自己）")
        rows = [{"key": k, "gp": k} for k in SLOTS]
    print("手柄键映射 %d 条：" % len(rows))
    for r in rows:
        print("  key=%-3d → gp=%-3d" % (r["key"], r["gp"]))
    with open(BACKUP, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("已备份到", BACKUP)
