#!/usr/bin/env python3
"""Extract RXM-COR / NAV-SIG / NAV-SAT / NAV-PVT layouts from cached manual text.

Research-only, stdlib-only. Reads full Firecrawl text extractions of the four
u-blox X20 HPG interface descriptions (not committed: proprietary manuals) and
writes ``manual-layouts.json`` beside this script: per firmware and message,
the printed page range, the declared payload length, every top-level and
repeated-group field offset/type/name, every bitfield's bit range/name, and the
enumerated values listed under selected fields.

The extraction is heuristic text parsing; every value it produces is checked
again by ``aggregate.py`` against hand-transcribed expectations from the
printed pages, so a parser slip shows up as a failure instead of a silent
"verified".

Usage:
    python3 manual_layouts.py /path/to/x20p-manual-extracts
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

VERSIONS = ("2.00", "2.02", "2.10", "2.11")
MESSAGES = {
    "RXM-COR": "UBX-RXM-COR (0x02 0x34)",
    "NAV-PVT": "UBX-NAV-PVT (0x01 0x07)",
    "NAV-SAT": "UBX-NAV-SAT (0x01 0x35)",
    "NAV-SIG": "UBX-NAV-SIG (0x01 0x43)",
}
# Fields whose bullet lists are enum definitions worth recording.
ENUM_FIELDS = {
    "RXM-COR": ("protocol", "errStatus", "msgUsed", "msgInputHandle",
                "msgEncrypted", "msgDecrypted"),
    "NAV-PVT": ("fixType", "carrSoln", "lastCorrectionAge"),
    "NAV-SAT": ("qualityInd", "health", "orbitSource"),
    "NAV-SIG": ("qualityInd", "corrSource", "ionoModel", "health", "authStatus"),
}

TYPE_RE = r"(?:U1|U2|U4|I1|I2|I4|X1|X2|X4|R4|R8|CH)(?:\[\d+\])?"
OFFSET_RE = re.compile(rf"^(\d+(?: \+ n·\d+)?)\s*({TYPE_RE})?$")
TYPE_ONLY_RE = re.compile(rf"^({TYPE_RE})$")
BITS_RE = re.compile(r"^bits? (\d+)(?:\.\.\.(\d+))?$")
PAGE_RE = re.compile(r"Page (\d+) of (\d+)")
ENUM_RE = re.compile(r"^•\s*(>?=?\s*\d+(?:,\s*\d+)*)\s*[:=]\s*(.*)$")
HEADING_RE = re.compile(r"^3\.\d+\.\d+ UBX-[A-Z0-9]+-[A-Z0-9]+ \(0x")
NOISE = ("UBXDOC-", "C1-Public", "u-blox X20 HPG", "u-blox 20 HPG", "3 UBX protocol")


def _section(lines: list[str], heading: str) -> tuple[int, list[str]]:
    """The message's body: from its last exact heading (the table of contents
    is earlier) to the next message heading."""
    starts = [i for i, line in enumerate(lines) if line.strip().endswith(heading)
              and HEADING_RE.match(line.strip())]
    if not starts:
        raise LookupError(heading)
    start = starts[-1]
    end = next((j for j in range(start + 1, len(lines))
                if HEADING_RE.match(lines[j].strip())), len(lines))
    return start + 1, lines[start:end]


def _clean(body: list[str]) -> list[str]:
    out = []
    for raw in body:
        line = raw.strip().replace("&gt;", ">").replace("&amp;", "&")
        if not line or any(line.startswith(n) for n in NOISE) or PAGE_RE.search(line):
            continue
        out.append(line)
    return out


def _name_after(lines: list[str], k: int) -> tuple[str, int]:
    """The field name starting at ``k``, joining a name split across lines
    (``msgSubType``/``Valid``, ``lastCorrection``/``Age``)."""
    while k < len(lines) and (lines[k] in ("U", "I", "") or re.fullmatch(r"U?\s*:?\d+|:\d+|U", lines[k])):
        k += 1
    name = lines[k]
    if k + 2 < len(lines) and lines[k + 1] != "--" and lines[k + 2] == "--" \
            and re.fullmatch(r"[A-Za-z]+", lines[k + 1]):
        return name + lines[k + 1], k + 2
    return name, k + 1


def parse_message(lines: list[str], message: str) -> dict:
    first_line, body = _section(lines, MESSAGES[message])
    pages = [int(m.group(1)) for line in body if (m := PAGE_RE.search(line))]
    # Footers close a page: content after the last footer is on the next page.
    last_footer = max((i for i, line in enumerate(body) if PAGE_RE.search(line)), default=-1)
    tail = _clean(body[last_footer + 1:]) if last_footer >= 0 else []
    total_pages = next((int(m.group(2)) for line in body if (m := PAGE_RE.search(line))), None)
    text = _clean(body)
    length = next((ln.split("0x", 4)[-1][2:] for ln in text if ln.startswith("0xb5 0x62")), None)
    fields: list[dict] = []
    bits: list[dict] = []
    enums: dict[str, list[list[str]]] = {}
    current: str | None = None
    parent: str | None = None
    i = 0
    while i < len(text):
        line = text[i]
        m_off = OFFSET_RE.match(line)
        m_bits = BITS_RE.match(line)
        if m_off and i + 1 < len(text):
            offset, typ = m_off.group(1), m_off.group(2)
            j = i + 1
            if typ is None:
                m_t = TYPE_ONLY_RE.match(text[j])
                if not m_t:
                    i += 1
                    continue
                typ, j = m_t.group(1), j + 1
            name, j = _name_after(text, j)
            fields.append({"offset": offset, "type": typ, "name": name})
            current = parent = name
            i = j
            continue
        if m_bits:
            hi, lo = int(m_bits.group(1)), int(m_bits.group(2) or m_bits.group(1))
            name, j = _name_after(text, i + 1)
            bits.append({"parent": parent, "lsb": min(hi, lo), "msb": max(hi, lo),
                         "width": abs(hi - lo) + 1, "name": name})
            current = name
            i = j
            continue
        m_enum = ENUM_RE.match(line)
        if m_enum and current in ENUM_FIELDS[message]:
            value = re.sub(r"\s+", "", m_enum.group(1))
            label = m_enum.group(2)
            # Join the bullet's wrapped continuation lines.
            k = i + 1
            while k < len(text) and not (ENUM_RE.match(text[k]) or OFFSET_RE.match(text[k])
                                         or BITS_RE.match(text[k])) and text[k] not in ("--",) \
                    and not text[k].startswith(("Start of", "End of", "Note", "Currently")):
                label += " " + text[k]
                k += 1
            enums.setdefault(current, []).append([value, " ".join(label.split())])
            i = k
            continue
        i += 1
    return {
        "extract_line": first_line,
        "printed_pages": [pages[0], pages[-1] + (1 if tail else 0)] if pages else None,
        "page_markers": pages,
        "document_pages": total_pages,
        "length": length,
        "fields": fields,
        "bits": bits,
        "enums": enums,
    }


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/ubuntu/.hermes/cache/scratch/x20p-manual-extracts")
    result: dict = {"source_dir": str(root), "documents": {}, "messages": {}}
    for version in VERSIONS:
        path = root / f"{version}.md"
        data = path.read_bytes()
        lines = data.decode("utf-8").splitlines()
        doc = next((m.group(0) for line in lines[:400]
                    if (m := re.search(r"UBXDOC-\d+-\d+ - R\d+", line))), None)
        result["documents"][version] = {"file": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                                        "lines": len(lines), "document": doc}
        for message in MESSAGES:
            result["messages"].setdefault(message, {})[version] = parse_message(lines, message)
    out = Path(__file__).with_name("manual-layouts.json")
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    for message, per in result["messages"].items():
        for version, info in per.items():
            print(f"{message:8} {version}: pages {info['page_markers'][:1]}..{info['page_markers'][-1:]} "
                  f"len={info['length']!r} fields={len(info['fields'])} bits={len(info['bits'])} "
                  f"enums={ {k: len(v) for k, v in info['enums'].items()} }")
    return 0


if __name__ == "__main__":
    sys.exit(main())
