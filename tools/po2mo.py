#!/usr/bin/env python3
"""Minimal .po -> .mo compiler (pure Python; no gettext tools required).

Sufficient for flat catalogs: plain msgid/msgstr pairs, no plurals, contexts,
fuzzy or obsolete entries.
Usage: po2mo.py catalog.po -o catalog.mo
"""
import struct
import sys


def _unescape(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "n":
                out.append("\n")
            elif n == "t":
                out.append("\t")
            elif n == "r":
                out.append("\r")
            elif n == '"':
                out.append('"')
            elif n == "\\":
                out.append("\\")
            else:
                out.append(n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _split_keyword(line: str):
    """Return (keyword, inline_text) if the line starts with a po keyword."""
    stripped = line.strip()
    for kw in ("msgctxt", "msgid_plural", "msgid", "msgstr"):
        if stripped == kw or stripped.startswith(kw + " "):
            return kw, stripped[len(kw):].strip()
    return None, None


def parse_po(path: str) -> dict:
    entries = {}
    section = None
    msg = None
    msgstr = None

    def flush():
        nonlocal msg, msgstr
        if msg is not None and msgstr is not None:
            entries[msg] = msgstr
        msg = None
        msgstr = None

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            keyword, inline = _split_keyword(line)
            if keyword is not None:
                if keyword == "msgctxt":
                    section = "ctx"
                elif keyword == "msgid_plural":
                    section = "msgid_plural"
                elif keyword == "msgid":
                    flush()
                    section = "msgid"
                    msg = ""
                else:
                    section = "msgstr"
                    msgstr = None
                if inline:
                    if not (inline.startswith('"') and inline.endswith('"')):
                        raise ValueError(f"unterminated string in {path}: {line!r}")
                    value = _unescape(inline[1:-1])
                    if section == "msgid":
                        msg += value
                    elif section == "msgstr":
                        msgstr = value
                continue
            content = line.strip()
            if content.startswith('"'):
                if not content.endswith('"'):
                    raise ValueError(f"unterminated string in {path}: {line!r}")
                value = _unescape(content[1:-1])
                if section == "msgid":
                    msg += value
                elif section == "msgstr":
                    msgstr = (msgstr or "") + value
                elif section == "msgid_plural":
                    pass  # plurals unsupported
    flush()
    return entries


def write_mo(entries: dict, out_path: str) -> None:
    keys = sorted(entries.keys())
    n = len(keys)
    originals = b"".join(k.encode("utf-8") for k in keys)
    translations = b"".join(entries[k].encode("utf-8") for k in keys)

    # Standard .mo layout: 28-byte header (7 x uint32), original table
    # starts right after it, translation table follows the original table.
    # Offsets in the tables are absolute file positions: the original
    # strings start after the translation table.
    o_table = 7 * 4
    t_table = o_table + 8 * n
    data_start = t_table + 8 * n

    offs_o = []
    pos = data_start
    for k in keys:
        b = k.encode("utf-8")
        offs_o.append((len(b), pos))
        pos += len(b)
    offs_t = []
    pos = data_start + len(originals)
    for k in keys:
        b = entries[k].encode("utf-8")
        offs_t.append((len(b), pos))
        pos += len(b)

    data = struct.pack("<7I", 0x950412DE, 0, n, o_table, t_table, 0, 0)
    for length, offset in offs_o:
        data += struct.pack("<2I", length, offset)
    for length, offset in offs_t:
        data += struct.pack("<2I", length, offset)
    data += originals
    data += translations
    # Every .mo produced by msgfmt (and read by glibc/gettext tools) ends
    # with one trailing zero byte; CPython's gettext._parse also requires
    # the last string to end strictly before EOF, so keep it.
    data += b"\x00"
    with open(out_path, "wb") as f:
        f.write(data)


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 3 and args[1] == "-o":
        src, out = args[0], args[2]
    elif len(args) == 2:
        src, out = args
    else:
        raise SystemExit("usage: po2mo.py SRC -o OUT")
    entries = parse_po(src)
    write_mo(entries, out)
    print(f"{out}: {len(entries)} entries")
