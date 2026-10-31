#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dotnet_il.py — 极简 .NET 程序集 IL 反汇编器（只读静态分析，不执行目标）

用途：不装 dnSpy/ILSpy 也能看清某个方法到底调了什么。
依赖：pip install dnfile

用法：
    python dotnet_il.py app.dll --list                       # 列出所有类型
    python dotnet_il.py app.dll --type iine.NotifyIconViewModel --list
    python dotnet_il.py app.dll --type iine.NotifyIconViewModel --method ExitApplication
"""
import argparse
import sys

import dnfile
from dnfile.enums import MetadataTables

# 单字节操作码 -> (名称, 操作数长度)
OP1 = {
    0x00: ("nop", 0), 0x01: ("break", 0),
    0x02: ("ldarg.0", 0), 0x03: ("ldarg.1", 0), 0x04: ("ldarg.2", 0), 0x05: ("ldarg.3", 0),
    0x06: ("ldloc.0", 0), 0x07: ("ldloc.1", 0), 0x08: ("ldloc.2", 0), 0x09: ("ldloc.3", 0),
    0x0A: ("stloc.0", 0), 0x0B: ("stloc.1", 0), 0x0C: ("stloc.2", 0), 0x0D: ("stloc.3", 0),
    0x0E: ("ldarg.s", 1), 0x0F: ("ldarga.s", 1), 0x10: ("starg.s", 1),
    0x11: ("ldloc.s", 1), 0x12: ("ldloca.s", 1), 0x13: ("stloc.s", 1),
    0x14: ("ldnull", 0),
    0x15: ("ldc.i4.m1", 0), 0x16: ("ldc.i4.0", 0), 0x17: ("ldc.i4.1", 0), 0x18: ("ldc.i4.2", 0),
    0x19: ("ldc.i4.3", 0), 0x1A: ("ldc.i4.4", 0), 0x1B: ("ldc.i4.5", 0), 0x1C: ("ldc.i4.6", 0),
    0x1D: ("ldc.i4.7", 0), 0x1E: ("ldc.i4.8", 0),
    0x1F: ("ldc.i4.s", 1), 0x20: ("ldc.i4", 4), 0x21: ("ldc.i8", 8),
    0x22: ("ldc.r4", 4), 0x23: ("ldc.r8", 8),
    0x25: ("dup", 0), 0x26: ("pop", 0), 0x27: ("jmp", 4),
    0x28: ("call", 4), 0x29: ("calli", 4), 0x2A: ("ret", 0),
    0x2B: ("br.s", 1), 0x2C: ("brfalse.s", 1), 0x2D: ("brtrue.s", 1),
    0x2E: ("beq.s", 1), 0x2F: ("bge.s", 1), 0x30: ("bgt.s", 1), 0x31: ("ble.s", 1),
    0x32: ("blt.s", 1), 0x33: ("bne.un.s", 1), 0x34: ("bge.un.s", 1), 0x35: ("bgt.un.s", 1),
    0x36: ("ble.un.s", 1), 0x37: ("blt.un.s", 1),
    0x38: ("br", 4), 0x39: ("brfalse", 4), 0x3A: ("brtrue", 4),
    0x3B: ("beq", 4), 0x3C: ("bge", 4), 0x3D: ("bgt", 4), 0x3E: ("ble", 4),
    0x3F: ("blt", 4), 0x40: ("bne.un", 4), 0x41: ("bge.un", 4), 0x42: ("bgt.un", 4),
    0x43: ("ble.un", 4), 0x44: ("blt.un", 4),
    0x45: ("switch", -4),
    0x46: ("ldind.i1", 0), 0x47: ("ldind.u1", 0), 0x48: ("ldind.i2", 0), 0x49: ("ldind.u2", 0),
    0x4A: ("ldind.i4", 0), 0x4B: ("ldind.u4", 0), 0x4C: ("ldind.i8", 0), 0x4D: ("ldind.i", 0),
    0x4E: ("ldind.r4", 0), 0x4F: ("ldind.r8", 0), 0x50: ("ldind.ref", 0),
    0x51: ("stind.ref", 0), 0x52: ("stind.i1", 0), 0x53: ("stind.i2", 0), 0x54: ("stind.i4", 0),
    0x55: ("stind.i8", 0), 0x56: ("stind.r4", 0), 0x57: ("stind.r8", 0),
    0x58: ("add", 0), 0x59: ("sub", 0), 0x5A: ("mul", 0), 0x5B: ("div", 0), 0x5C: ("div.un", 0),
    0x5D: ("rem", 0), 0x5E: ("rem.un", 0), 0x5F: ("and", 0), 0x60: ("or", 0), 0x61: ("xor", 0),
    0x62: ("shl", 0), 0x63: ("shr", 0), 0x64: ("shr.un", 0), 0x65: ("neg", 0),
    0x66: ("not", 0), 0x67: ("conv.i1", 0), 0x68: ("conv.i2", 0), 0x69: ("conv.i4", 0),
    0x6A: ("conv.i8", 0), 0x6B: ("conv.r4", 0), 0x6C: ("conv.r8", 0), 0x6D: ("conv.u4", 0),
    0x6E: ("conv.u8", 0),
    0x6F: ("callvirt", 4), 0x70: ("cpobj", 4), 0x71: ("ldobj", 4), 0x72: ("ldstr", 4),
    0x73: ("newobj", 4), 0x74: ("castclass", 4), 0x75: ("isinst", 4), 0x76: ("conv.r.un", 0),
    0x79: ("unbox", 4), 0x7A: ("throw", 0), 0x7B: ("ldfld", 4), 0x7C: ("ldflda", 4),
    0x7D: ("stfld", 4), 0x7E: ("ldsfld", 4), 0x7F: ("ldsflda", 4), 0x80: ("stsfld", 4),
    0x81: ("stobj", 4), 0x82: ("conv.ovf.i1.un", 0), 0x83: ("conv.ovf.i2.un", 0),
    0x84: ("conv.ovf.i4.un", 0), 0x85: ("conv.ovf.i8.un", 0), 0x86: ("conv.ovf.u1.un", 0),
    0x87: ("conv.ovf.u2.un", 0), 0x88: ("conv.ovf.u4.un", 0), 0x89: ("conv.ovf.u8.un", 0),
    0x8A: ("conv.ovf.i.un", 0), 0x8B: ("conv.ovf.u.un", 0),
    0x8C: ("box", 4), 0x8D: ("newarr", 4), 0x8E: ("ldlen", 0), 0x8F: ("ldelema", 4),
    0x90: ("ldelem.i1", 0), 0x91: ("ldelem.u1", 0), 0x92: ("ldelem.i2", 0),
    0x93: ("ldelem.u2", 0), 0x94: ("ldelem.i4", 0), 0x95: ("ldelem.u4", 0),
    0x96: ("ldelem.i8", 0), 0x97: ("ldelem.i", 0), 0x98: ("ldelem.r4", 0),
    0x99: ("ldelem.r8", 0), 0x9A: ("ldelem.ref", 0),
    0x9B: ("stelem.i", 0), 0x9C: ("stelem.i1", 0), 0x9D: ("stelem.i2", 0),
    0x9E: ("stelem.i4", 0), 0x9F: ("stelem.i8", 0), 0xA0: ("stelem.r4", 0),
    0xA1: ("stelem.r8", 0), 0xA2: ("stelem.ref", 0),
    0xA3: ("ldelem", 4), 0xA4: ("stelem", 4), 0xA5: ("unbox.any", 4),
    0xB3: ("conv.ovf.i1", 0), 0xB4: ("conv.ovf.u1", 0), 0xB5: ("conv.ovf.i2", 0),
    0xB6: ("conv.ovf.u2", 0), 0xB7: ("conv.ovf.i4", 0), 0xB8: ("conv.ovf.u4", 0),
    0xB9: ("conv.ovf.i8", 0), 0xBA: ("conv.ovf.u8", 0),
    0xC2: ("refanyval", 4), 0xC3: ("ckfinite", 0), 0xC6: ("mkrefany", 4),
    0xD0: ("ldtoken", 4), 0xD1: ("conv.u2", 0), 0xD2: ("conv.u1", 0), 0xD3: ("conv.i", 0),
    0xD4: ("conv.ovf.i", 0), 0xD5: ("conv.ovf.u", 0), 0xD6: ("add.ovf", 0),
    0xD7: ("add.ovf.un", 0), 0xD8: ("mul.ovf", 0), 0xD9: ("mul.ovf.un", 0),
    0xDA: ("sub.ovf", 0), 0xDB: ("sub.ovf.un", 0), 0xDC: ("endfinally", 0),
    0xDD: ("leave", 4), 0xDE: ("leave.s", 1), 0xDF: ("stind.i", 0), 0xE0: ("conv.u", 0),
}
OP2 = {
    0x00: ("arglist", 0), 0x01: ("ceq", 0), 0x02: ("cgt", 0), 0x03: ("cgt.un", 0),
    0x04: ("clt", 0), 0x05: ("clt.un", 0), 0x06: ("ldftn", 4), 0x07: ("ldvirtftn", 4),
    0x09: ("ldarg", 2), 0x0A: ("ldarga", 2), 0x0B: ("starg", 2), 0x0C: ("ldloc", 2),
    0x0D: ("ldloca", 2), 0x0E: ("stloc", 2), 0x0F: ("localloc", 0),
    0x11: ("endfilter", 0), 0x12: ("unaligned.", 1), 0x13: ("volatile.", 0),
    0x14: ("tail.", 0), 0x15: ("initobj", 4), 0x16: ("constrained.", 4),
    0x17: ("cpblk", 0), 0x18: ("initblk", 0), 0x19: ("no.", 1), 0x1A: ("rethrow", 0),
    0x1C: ("sizeof", 4), 0x1D: ("refanytype", 0), 0x1E: ("readonly.", 0),
}

TOKEN_TABLES = {
    0x01: MetadataTables.TypeRef, 0x02: MetadataTables.TypeDef, 0x04: MetadataTables.Field,
    0x06: MetadataTables.MethodDef, 0x0A: MetadataTables.MemberRef, 0x11: MetadataTables.StandAloneSig,
    0x1B: MetadataTables.TypeSpec,
}


def type_fullname(row):
    if row is None:
        return "<null>"
    ns = getattr(row, "TypeNamespace", None) or ""
    nm = getattr(row, "TypeName", None) or ""
    return f"{ns}.{nm}" if ns else nm


def resolve(pe, token):
    """把元数据 token 解析成可读名字。"""
    table = token >> 24
    rid = token & 0xFFFFFF
    try:
        if table == 0x70:  # UserString
            us = pe.net.user_strings
            return '"%s"' % us.get(rid)
        t = TOKEN_TABLES.get(table)
        if t is None:
            return f"0x{token:08X}"
        rows = pe.net.mdtables.__getattribute__(t.name)
        if rows is None or rid == 0 or rid > len(rows):
            return f"0x{token:08X}"
        row = rows[rid - 1]
        if t == MetadataTables.MethodDef:
            return f"{row.Name}"
        if t == MetadataTables.MemberRef:
            cls = getattr(row, "Class", None)
            owner = "<unknown>"
            if cls is not None:
                if getattr(cls, "row", None) is not None:
                    owner = type_fullname(cls.row)
            return f"{owner}::{row.Name}"
        if t == MetadataTables.Field:
            return f"{row.Name}"
        if t == MetadataTables.TypeRef:
            return type_fullname(row)
        if t == MetadataTables.TypeDef:
            return type_fullname(row)
        return f"0x{token:08X}"
    except Exception as e:  # 元数据残缺时不要让整个 dump 挂掉
        return f"0x{token:08X}(?)"


def iter_types(pe):
    for row in pe.net.mdtables.TypeDef:
        yield row


def find_type(pe, name):
    for row in iter_types(pe):
        full = type_fullname(row)
        if full == name or (row.TypeName == name):
            return row
    return None


def row_of(x):
    """dnfile 里 MDTableIndex 包着真实行，统一取出行对象。"""
    return getattr(x, "row", x)


def methods_of(pe, typedef_row):
    """dnfile 的 TypeDef.MethodList 给出该类型方法区间内的索引，取出行对象。"""
    ml = typedef_row.MethodList
    try:
        return [row_of(x) for x in ml]
    except Exception:
        return []


def get_il(pe, method_row):
    rva = method_row.Rva
    if not rva:
        return b"", 0
    try:
        off = pe.get_offset_from_rva(rva)
        fmt = pe.__data__[off]
        if (fmt & 0x3) == 0x2:  # tiny header
            size = fmt >> 2
            return pe.__data__[off + 1: off + 1 + size], size
        # fat header
        flags_size = int.from_bytes(pe.__data__[off:off + 2], "little")
        hdr = ((flags_size >> 12) & 0xF) * 4
        size = int.from_bytes(pe.__data__[off + 4:off + 8], "little")
        return pe.__data__[off + hdr: off + hdr + size], size
    except Exception:
        return b"", 0


def disasm(pe, code):
    i = 0
    out = []
    n = len(code)
    while i < n:
        start = i
        b = code[i]
        i += 1
        if b == 0xFE:
            if i >= n:
                break
            b2 = code[i]
            i += 1
            name, ln = OP2.get(b2, ("<fe?>", 0))
        else:
            name, ln = OP1.get(b, ("<?>", 0))
        operand = None
        if ln == -4:  # switch
            cnt = int.from_bytes(code[i:i + 4], "little")
            i += 4 + 4 * cnt
            operand = f"({cnt} targets)"
        elif ln:
            raw = code[i:i + ln]
            i += ln
            operand = int.from_bytes(raw, "little")
        # 解析 token 操作数
        text = name
        if operand is not None and ln == 4 and name in (
            "call", "callvirt", "newobj", "ldstr", "ldfld", "stfld", "ldsfld", "stsfld",
            "ldftn", "ldvirtftn", "castclass", "isinst", "box", "newarr", "ldtoken",
            "initobj", "constrained.", "ldobj", "stobj", "unbox.any", "sizeof", "ldelem", "stelem",
        ):
            text = f"{name} {resolve(pe, operand)}"
        elif operand is not None and name in ("ldc.i4", "ldc.i4.s"):
            text = f"{name} {operand}"
        elif operand is not None and name.startswith(("br", "beq", "bge", "bgt", "ble", "blt", "bne", "leave")):
            # 分支偏移是相对「下一条指令」的有符号数
            bits = ln * 8
            signed = operand - (1 << bits) if operand >= (1 << (bits - 1)) else operand
            text = f"{name} IL_{i + signed:04X}"
        elif operand is not None:
            text = f"{name} {operand}"
        out.append((start, text))
        if i <= start:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("assembly")
    ap.add_argument("--type", help="类型全名，如 iine.NotifyIconViewModel")
    ap.add_argument("--method", help="方法名（配合 --type）")
    ap.add_argument("--list", action="store_true", help="列出类型/方法")
    ap.add_argument("--grep", help="按名字子串过滤类型")
    args = ap.parse_args()

    pe = dnfile.dnPE(args.assembly)
    if pe.net is None:
        print("不是 .NET 程序集")
        return 1

    if args.list and not args.type:
        for row in iter_types(pe):
            full = type_fullname(row)
            if args.grep and args.grep.lower() not in full.lower():
                continue
            print(full)
        return 0

    if not args.type:
        print("需要 --type 或 --list")
        return 2

    td = find_type(pe, args.type)
    if td is None:
        print(f"找不到类型 {args.type}")
        return 3

    ms = methods_of(pe, td)
    if args.list or not args.method:
        print(f"// {type_fullname(td)}  ({len(ms)} 个方法)")
        for m in ms:
            print("  ", m.Name)
        return 0

    for m in ms:
        if m.Name == args.method:
            code, size = get_il(pe, m)
            print(f"// {type_fullname(td)}::{m.Name}  IL={size}B  RVA=0x{m.Rva:X}")
            for off, text in disasm(pe, code):
                print(f"  IL_{off:04X}: {text}")
            return 0
    print(f"类型里没有方法 {args.method}")
    return 4


if __name__ == "__main__":
    sys.exit(main())
