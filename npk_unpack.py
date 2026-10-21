#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
npk_unpack.py - unpacker for NetEase NeoX NPK packages (Eggy Party / 蛋仔派对 PC, 2026 format).

This version of the engine stores every package's table of contents (TOC) ENCRYPTED at the
end of the .npk file, so the TOC has to be harvested from the running game's process memory
(`harvest` command), after which `info` / `list` / `extract` work fully offline.

Package layout (little endian):
    0x00  char[4] "NXPK"
    0x04  u32     entry_count
    0x08  u32     toc_offset_hi      (high 32 bits of the 64-bit TOC file offset)
    0x0C  u32     entry_encryption_mode (3 in this version)
    0x10  u32     hash_mode
    0x14  u32     toc_offset_lo
    data region starts at 0x18; the TOC sits at (toc_offset_hi<<32 | toc_offset_lo)
    file_size == toc_offset + entry_count*28  (so TOC lives at the very end)

Per-entry record (28 bytes):
    u32 id            = murmur3_32(path_with_backslashes, seed 0x9747B28C)
    u32 offset        (within the npk file)
    u32 packed_size
    u32 raw_size
    u32 checksum_packed
    u32 checksum_raw
    u32 flags         low byte = compression (0=stored, 1=zlib, 2=lz4, 3=zstd),
                      bit 24 set = data is additionally encrypted (gui2 has ~20k of these)

Usage:
    python npk_unpack.py info    gui2.npk
    python npk_unpack.py list    gui2.npk --limit 50
    python npk_unpack.py extract gui2.npk --out out/gui2 --limit 200 --convert-ktx
    python npk_unpack.py extract gui2.npk --out out/gui2 --all
    python npk_unpack.py harvest            # start the game first, keep it running!
    python npk_unpack.py harvest --game-dir D:\\FeverApps\\party_pc

The `harvest` command needs the game running and an administrator-free handle to client.exe
(OpenProcess with PROCESS_VM_READ).  Tested on the 1.0.32 PC build (2026-10-14).
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MAGIC = b"NXPK"
KTX1 = b"\xabKTX 11\xbb\r\n\x1a\n"
KTX2 = b"\xabKTX 20\xbb\r\n\x1a\n"

# compression ids (low byte of flags)
COMP_STORED, COMP_ZLIB, COMP_LZ4, COMP_ZSTD = 0, 1, 2, 3
FLAG_ENCRYPTED = 0x01000000


def eprint(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------------------------
# package / TOC reading
# --------------------------------------------------------------------------------------------
class Npk:
    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(24)
        if len(head) < 24 or head[:4] != MAGIC:
            raise SystemExit("%s: not an NXPK file" % path)
        (self.entries, self.unk1, self.entry_encryption_mode,
         self.hash_mode, self.off_lo) = struct.unpack_from("<5I", head, 4)
        self.toc_offset = (self.unk1 << 32) | self.off_lo
        self.toc_size = self.entries * 28
        self.toc = b""
        self.toc_path = None
        self._f = None

    def header_text(self):
        return ("%s: size=%d entries=%d toc_offset=0x%x (%d) toc_size=%d "
                "entry_encryption_mode=%d hash_mode=%d"
                % (os.path.basename(self.path), self.size, self.entries, self.toc_offset,
                   self.toc_offset, self.toc_size, self.entry_encryption_mode, self.hash_mode))

    def close(self):
        if self._f:
            self._f.close()
            self._f = None

    def load_toc(self, toc_path=None):
        cand = []
        if toc_path:
            cand.append(toc_path)
        base = os.path.splitext(os.path.basename(self.path))[0]
        for d in (HERE, os.path.join(HERE, "toc"), os.getcwd()):
            cand.append(os.path.join(d, "toc_%s.bin" % base))
            cand.append(os.path.join(d, "%s.toc" % base))
        for c in cand:
            if c and os.path.exists(c) and os.path.getsize(c) >= self.toc_size:
                self.toc = open(c, "rb").read(self.toc_size)
                self.toc_path = c
                return True
        return False

    def entry(self, i):
        return struct.unpack_from("<7I", self.toc, i * 28)

    def read_data(self, offset, packed_size):
        if self._f is None:
            self._f = open(self.path, "rb")
        self._f.seek(offset)
        return self._f.read(packed_size)

    def verify_toc(self):
        """returns (stored, zstd, other_comp, encrypted, bad) counters and list of bad indices"""
        c = {"stored": 0, "zstd": 0, "zlib": 0, "lz4": 0, "other": 0,
             "encrypted": 0, "bad": 0}
        bad = []
        for i in range(self.entries):
            eid, off, pk, rw, c1, c2, fl = self.entry(i)
            comp = fl & 0xFF
            if fl & FLAG_ENCRYPTED:
                c["encrypted"] += 1
            elif comp == COMP_STORED and pk == rw:
                c["stored"] += 1
            elif comp == COMP_ZSTD:
                c["zstd"] += 1
            elif comp == COMP_ZLIB:
                c["zlib"] += 1
            elif comp == COMP_LZ4:
                c["lz4"] += 1
            else:
                c["other"] += 1
            if pk == 0 or off + pk > self.size or off < 24 or rw == 0:
                c["bad"] += 1
                if len(bad) < 10:
                    bad.append((i, eid, off, pk, rw, fl))
        return c, bad


# --------------------------------------------------------------------------------------------
# payload inspection
# --------------------------------------------------------------------------------------------
_JSON_START = set(b"{[")


def guess_ext(data):
    if not data:
        return ".bin"
    d = data
    if d.startswith(KTX1):
        return ".ktx"
    if d.startswith(KTX2):
        return ".ktx2"
    if d.startswith(b"DDS "):
        return ".dds"
    if d.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if d.startswith(b"RIFF") and d[8:12] == b"WAVE":
        return ".wav"
    if d[:4] == b"BKHD":
        return ".bnk"
    if d[:4] == b"OggS":
        return ".ogg"
    if d[:4] == b"RIFX" or d[:3] == b"ID3":
        return ".wem"
    if d[:4] in (b"FxAA", b"\x00\x00\x00\x00"):
        return ".bin"
    if d.startswith(b"{\n") or d.startswith(b"[\n") or d[:1] in _JSON_START:
        try:
            json.loads(d[:min(len(d), 4 << 20)].decode("utf-8", "strict"))
            return ".json"
        except Exception:
            pass
    head = d[:512].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<"):
        return ".xml"
    # heuristic text
    sample = d[:4096]
    if sample:
        printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
        if printable / len(sample) > 0.97:
            return ".txt"
    return ".bin"


def ktx_info(data):
    """return (width, height, format_name) for a KTX1 file, or None"""
    if not data.startswith(KTX1) or len(data) < 64:
        return None
    gl_type, gl_type_size, gl_format, gl_internal = struct.unpack_from("<4I", data, 16)
    width, height = struct.unpack_from("<2I", data, 36)
    fmt = {0x83F0: "DXT1/BC1", 0x83F1: "DXT1/BC1", 0x83F2: "DXT3/BC2",
           0x83F3: "DXT5/BC3"}.get(gl_internal)
    if fmt is None:
        fmt = "0x%x" % gl_internal
    return width, height, fmt


# --------------------------------------------------------------------------------------------
# zstd batch decompression through bundled node
# --------------------------------------------------------------------------------------------
def find_node(explicit=None):
    if explicit:
        return explicit
    for name in ("node.exe", "node"):
        p = shutil.which(name)
        if p:
            return p
    cand = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        r"Programs\DeepSeek Harness\resources\app.asar.unpacked\dsh\..\..\node.exe")
    for p in (
        os.path.expandvars(r"%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\dsh\node\node.exe"),
    ):
        if os.path.exists(p):
            return p
    return None


def zstd_batch_decompress(node, jobs):
    """jobs: list of dicts {src: path, dst: path}; returns (ok, fail)"""
    js = os.path.join(HERE, "npk_zstd.js")
    jobs_path = os.path.join(tempfile.gettempdir(), "npk_zstd_jobs_%d.json" % os.getpid())
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f)
    try:
        r = subprocess.run([node, js, jobs_path], capture_output=True, text=True,
                           timeout=3600)
    finally:
        try:
            os.remove(jobs_path)
        except OSError:
            pass
    ok = fail = 0
    for line in (r.stdout or "").splitlines():
        if line.startswith("OK"):
            ok += 1
        elif line.startswith("FAIL"):
            fail += 1
    if fail:
        eprint("zstd: %d files failed to decompress" % fail)
    return ok, fail


# --------------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------------
def cmd_info(args):
    npk = Npk(args.npk)
    print(npk.header_text())
    if not npk.load_toc(args.toc):
        print("")
        print("No decrypted TOC available.  Harvest it from the running game first:")
        print("    python npk_unpack.py harvest")
        print("or pass --toc toc_%s.bin" % os.path.splitext(os.path.basename(npk.path))[0])
        return 1
    print("TOC: %s (%d bytes)" % (npk.toc_path, len(npk.toc)))
    c, bad = npk.verify_toc()
    print("entries: %d  stored=%d zstd=%d zlib=%d lz4=%d other=%d encrypted(bit24)=%d invalid=%d"
          % (npk.entries, c["stored"], c["zstd"], c["zlib"], c["lz4"], c["other"],
             c["encrypted"], c["bad"]))
    ids = [npk.entry(i)[0] for i in range(npk.entries)]
    print("ids: min=%08x max=%08x unique=%d sorted=%s"
          % (min(ids), max(ids), len(set(ids)), ids == sorted(ids)))
    pk_sum = sum(npk.entry(i)[2] for i in range(npk.entries))
    rw_sum = sum(npk.entry(i)[3] for i in range(npk.entries))
    print("packed sum=%d (%.1f MiB)  raw sum=%d (%.1f MiB)  file size=%d"
          % (pk_sum, pk_sum / 1048576.0, rw_sum, rw_sum / 1048576.0, npk.size))
    ext_count = {}
    for i in range(min(npk.entries, 20)):
        eid, off, pk, rw, c1, c2, fl = npk.entry(i)
        if fl & FLAG_ENCRYPTED:
            ext_count[".enc"] = ext_count.get(".enc", 0) + 1
            continue
        d = npk.read_data(off, min(pk, 64)) if pk else b""
        e = guess_ext(d)
        ext_count[e] = ext_count.get(e, 0) + 1
    if bad:
        print("first invalid entries:", bad)
    return 0


def parse_indices(spec, entries):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(i for i in out if 0 <= i < entries))


def cmd_list(args):
    npk = Npk(args.npk)
    if not npk.load_toc(args.toc):
        raise SystemExit("no decrypted TOC; run: python npk_unpack.py harvest")
    entries = npk.entries
    idxs = range(args.start, min(entries, args.start + args.limit))
    print("%-7s %-10s %-12s %-9s %-9s %-4s %-9s %s"
          % ("index", "id", "offset", "packed", "raw", "comp", "flags", "type"))
    for i in idxs:
        eid, off, pk, rw, c1, c2, fl = npk.entry(i)
        comp = fl & 0xFF
        if fl & FLAG_ENCRYPTED:
            t = "encrypted"
            comp = "?"
        elif comp == COMP_STORED:
            d = npk.read_data(off, min(pk, 64)) if pk else b""
            t = guess_ext(d)
        elif comp == COMP_ZSTD:
            t = "zstd"
        else:
            t = "?"
        print("%-7d %08x 0x%-10x %-9d %-9d %-4s 0x%-7x %s"
              % (i, eid, off, pk, rw, comp, fl, t))
    return 0


def sanitize_rel(p):
    p = p.replace("\\", "/").lstrip("/")
    parts = [x for x in p.split("/") if x not in ("", ".", "..")]
    return "/".join(parts)


def load_names(path):
    names = {}
    if not path:
        return names
    for line in open(path, "r", encoding="utf-8", errors="ignore"):
        line = line.rstrip("\r\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        try:
            names[int(parts[0].strip(), 16)] = sanitize_rel(parts[1].strip())
        except ValueError:
            continue
    return names


def cmd_extract(args):
    from ktx2png import ktx_to_png
    npk = Npk(args.npk)
    if not npk.load_toc(args.toc):
        raise SystemExit("no decrypted TOC; run: python npk_unpack.py harvest")
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    if args.all:
        idxs = list(range(npk.entries))
    elif args.indices:
        idxs = parse_indices(args.indices, npk.entries)
    else:
        idxs = list(range(min(npk.entries, args.limit)))
    if args.max_raw:
        idxs = [i for i in idxs if 0 < npk.entry(i)[3] <= args.max_raw]
    if args.types:
        want = set("." + t.strip().lstrip(".").lower() for t in args.types.split(",") if t.strip())
        keep = []
        for i in idxs:
            eid, off, pk, rw, c1, c2, fl = npk.entry(i)
            if fl & FLAG_ENCRYPTED:
                continue
            d = npk.read_data(off, min(pk, 64)) if pk else b""
            if guess_ext(d) in want:
                keep.append(i)
        idxs = keep
    names = load_names(args.names)
    total_raw = 0
    for i in idxs:
        eid, off, pk, rw, c1, c2, fl = npk.entry(i)
        total_raw += rw
    print("selected %d entries, raw size ~%.2f MiB -> %s"
          % (len(idxs), total_raw / 1048576.0, out))
    if total_raw > 2 * 1024 ** 3 and not args.yes:
        raise SystemExit("refusing to extract > 2 GiB without --yes (use filters: "
                         "--limit / --max-raw / --types)")

    node = find_node(args.node)
    tmpdir = os.path.join(out, ".npk_tmp_%d" % os.getpid())
    os.makedirs(tmpdir, exist_ok=True)
    if args.tmpdir:
        tmpdir = os.path.abspath(args.tmpdir)
        os.makedirs(tmpdir, exist_ok=True)

    jobs = []
    plan = []          # (i, eid, packed_path or direct data, fl, rw)
    counters = {"stored": 0, "zstd": 0, "skipped_encrypted": 0, "failed": 0, "written": 0}
    for n, i in enumerate(idxs):
        eid, off, pk, rw, c1, c2, fl = npk.entry(i)
        comp = fl & 0xFF
        if fl & FLAG_ENCRYPTED and not args.include_encrypted:
            counters["skipped_encrypted"] += 1
            continue
        d = npk.read_data(off, pk) if pk else b""
        if len(d) != pk:
            continue
        if fl & FLAG_ENCRYPTED:
            # raw packed bytes kept as .enc
            plan.append((i, eid, fl, rw, None, d))
        elif comp == COMP_STORED:
            plan.append((i, eid, fl, rw, None, d))
            counters["stored"] += 1
        elif comp == COMP_ZSTD:
            src = os.path.join(tmpdir, "p%06d.bin" % i)
            with open(src, "wb") as f:
                f.write(d)
            jobs.append({"src": src, "dst": os.path.join(tmpdir, "d%06d.bin" % i),
                         "i": i})
            counters["zstd"] += 1
        else:
            plan.append((i, eid, fl, rw, None, d))  # unknown compression: dump raw

    if jobs:
        if not node:
            raise SystemExit("zstd payloads need node.exe - pass --node <path to node.exe>")
        print("decompressing %d zstd payloads with node ..." % len(jobs), flush=True)
        ok, fail = zstd_batch_decompress(node, jobs)
        counters["failed"] += fail
        for j in jobs:
            if os.path.exists(j["dst"]):
                plan.append((j["i"], npk.entry(j["i"])[0], npk.entry(j["i"])[6],
                             npk.entry(j["i"])[3], j["dst"], None))
            else:
                counters["skipped_encrypted"] += 1

    def final_name(i, eid, data, ext):
        rel = names.get(eid)
        if rel:
            base = rel.rsplit("/", 1)[-1]
            return rel if "." in base else rel + ext
        return "%06d_%08x%s" % (i, eid, ext)

    for i, eid, fl, rw, path, direct in plan:
        if path:
            data_head = open(path, "rb").read(64)
        else:
            data_head = direct[:64]
        ext = guess_ext(data_head)
        if fl & FLAG_ENCRYPTED:
            ext = ".enc"
        rel = final_name(i, eid, data_head, ext)
        if args.flat:
            rel = rel.replace("/", "_")
        dst = os.path.join(out, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst) or out, exist_ok=True)
        if path:
            os.replace(path, dst)
        else:
            with open(dst, "wb") as f:
                f.write(direct)
        counters["written"] += 1
        if args.convert_ktx and ext == ".ktx":
            png = os.path.splitext(dst)[0] + ".png"
            if ktx_to_png(dst, png):
                counters["ktx_png"] = counters.get("ktx_png", 0) + 1

    shutil.rmtree(tmpdir, ignore_errors=True)
    print("done: written=%d stored=%d zstd=%d failed=%d skipped_encrypted=%d png=%d"
          % (counters["written"], counters["stored"], counters["zstd"], counters["failed"],
             counters["skipped_encrypted"], counters.get("ktx_png", 0)))
    return 0


# --------------------------------------------------------------------------------------------
# harvest: read the decrypted TOCs from the running game process
# --------------------------------------------------------------------------------------------
PROCESS_VM_READ = 0x10
PROCESS_QUERY_INFORMATION = 0x400
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
MEM_IMAGE = 0x1000000


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wt.DWORD), ("PartitionId", wt.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wt.DWORD),
                ("Protect", wt.DWORD), ("Type", wt.DWORD)]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


def _k32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def find_game_pids(exe_name="client.exe"):
    k32 = _k32()
    pids = []
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == -1:
        return pids
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    ok = k32.Process32First(snap, ctypes.byref(pe))
    while ok:
        if pe.szExeFile.lower() == exe_name.encode().lower():
            pids.append(pe.th32ProcessID)
        ok = k32.Process32Next(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    return pids


def _scan_objects(hp, want_entries=None):
    """return list of (va, entries, unk1, enc_mode, hash_mode, toc_off_lo, size)"""
    k32 = _k32()
    objs = []
    addr = 0
    mbi = MBI()
    chunk = 1 << 20
    buf = ctypes.create_string_buffer(chunk)
    while addr < 0x7FFFFFFFFFFF:
        if not k32.VirtualQueryEx(hp, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                  ctypes.sizeof(mbi)):
            break
        base, size = mbi.BaseAddress or 0, mbi.RegionSize or 0
        if size == 0:
            break
        addr = base + size
        if mbi.State != MEM_COMMIT or (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)):
            continue
        if mbi.Type == MEM_IMAGE:
            continue
        off = 0
        while off < size:
            n = min(chunk, size - off)
            got = ctypes.c_size_t(0)
            if k32.ReadProcessMemory(hp, ctypes.c_void_p(base + off), buf,
                                     ctypes.c_size_t(n), ctypes.byref(got)) and got.value:
                data = buf.raw[:got.value]
                idx = data.find(MAGIC)
                while idx >= 0:
                    if idx + 24 <= got.value:
                        e, u, m, h, eo = struct.unpack_from("<5I", data, idx + 4)
                        if 0 < e < 5_000_000 and m < 16 and h < 16:
                            objs.append((base + off + idx, e, u, m, h, eo))
                    idx = data.find(MAGIC, idx + 1)
            off += n
    return objs


def _read(hp, va, n):
    k32 = _k32()
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(hp, ctypes.c_void_p(va), buf, ctypes.c_size_t(n),
                                 ctypes.byref(got)):
        return b""
    return buf.raw[:got.value]


def _npk_index(game_dir):
    """entries-count -> npk file name, by reading every .npk header in game_dir"""
    idx = {}
    if not game_dir or not os.path.isdir(game_dir):
        return idx
    for name in os.listdir(game_dir):
        if not name.lower().endswith(".npk"):
            continue
        p = os.path.join(game_dir, name)
        try:
            with open(p, "rb") as f:
                head = f.read(24)
            if head[:4] != MAGIC:
                continue
            e, u, m, h, eo = struct.unpack_from("<5I", head, 4)
            idx[(e, u, m, h, eo)] = name[:-4]
        except OSError:
            pass
    return idx


def cmd_harvest(args):
    toc_dir = os.path.abspath(args.toc_dir or os.path.join(HERE, "toc"))
    os.makedirs(toc_dir, exist_ok=True)
    idx = _npk_index(args.game_dir)
    t0 = __import__("time").time()
    print("waiting for client.exe ... (start the game and keep it running)")
    pid = None
    while __import__("time").time() - t0 < args.wait_pid:
        pids = find_game_pids()
        if pids:
            pid = pids[0]
            break
        __import__("time").sleep(0.3)
    if not pid:
        raise SystemExit("client.exe is not running (start it, then rerun harvest)")
    k32 = _k32()
    hp = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not hp:
        raise SystemExit("OpenProcess failed (error %d)" % ctypes.get_last_error())
    print("pid=%d, scanning memory until %d TOC(s) are found (%ds max) ..."
          % (pid, args.wait_tocs, args.wait))
    found = {}
    t1 = __import__("time").time()
    while __import__("time").time() - t1 < args.wait:
        objs = _scan_objects(hp)
        for (va, e, u, m, h, eo) in objs:
            name = idx.get((e, u, m, h, eo)) or "e%d" % e
            expect = e * 28
            ctx = _read(hp, va, 0x60)
            if len(ctx) < 0x48:
                continue
            u0, u1, u2 = struct.unpack_from("<3Q", ctx, 0x18)
            for offv in (u0, u2):
                if offv and u1 > offv:
                    toc = _read(hp, offv, u1 - offv)
                else:
                    toc = b""
                if not toc or len(toc) not in (expect, expect + 1024):
                    # try to read exactly where the TOC is expected
                    continue
                if name in found and len(found[name]) >= len(toc):
                    continue
                p = os.path.join(toc_dir, "toc_%s.bin" % name)
                open(p, "wb").write(toc[:expect] if len(toc) >= expect else toc)
                found[name] = toc
                print("  found %-16s entries=%d va=0x%x -> %s" % (name, e, va, p))
        if len(found) >= args.wait_tocs:
            break
        __import__("time").sleep(1.5)
    k32.CloseHandle(hp)
    if not found:
        raise SystemExit("no TOC found - make sure the game reached its main menu")
    print("saved %d TOC(s) into %s : %s"
          % (len(found), toc_dir, ", ".join(sorted(found))))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="NeoX NPK unpacker (Eggy Party 2026 format)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("npk", help="path to .npk file")
        p.add_argument("--toc", help="decrypted TOC file (default: toc/toc_<name>.bin next to this script)")

    p = sub.add_parser("info", help="show package header + TOC statistics (offline)")
    common(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("list", help="list entries of a package (offline)")
    common(p)
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("extract", help="extract entries to a folder (offline)")
    common(p)
    p.add_argument("--out", required=True)
    p.add_argument("--all", action="store_true", help="extract every entry (can be huge)")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--indices", help="comma list / ranges, e.g. 0-50,100,300")
    p.add_argument("--max-raw", type=int, default=0, help="skip entries with raw_size above this")
    p.add_argument("--types", help="only extract these ext guesses, e.g. ktx,xml,json")
    p.add_argument("--names", help="names.txt with id<TAB>path lines (optional)")
    p.add_argument("--flat", action="store_true", help="flatten path names")
    p.add_argument("--convert-ktx", action="store_true", help="also convert KTX to PNG")
    p.add_argument("--include-encrypted", action="store_true",
                   help="also dump the bit24-encrypted entries as .enc")
    p.add_argument("--node", help="path to node.exe (needed for zstd payloads)")
    p.add_argument("--tmpdir", help="temp dir for zstd staging")
    p.add_argument("--yes", action="store_true", help="allow >2GiB extraction")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("harvest", help="dump decrypted TOCs from the running game")
    p.add_argument("--toc-dir", help="where to write toc_<name>.bin (default: ./toc)")
    p.add_argument("--game-dir", default=r"D:\FeverApps\party_pc",
                   help="game directory containing the .npk files (used to name TOCs)")
    p.add_argument("--wait", type=int, default=90, help="seconds to wait for TOCs")
    p.add_argument("--wait-pid", type=int, default=60, help="seconds to wait for the game process")
    p.add_argument("--wait-tocs", type=int, default=10)
    p.set_defaults(func=cmd_harvest)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
