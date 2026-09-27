"""Raw parsers for the kernel's /proc and /sys memory interfaces.

Every parser takes the file *text* so it can be unit tested with fixtures; the
`read_*` helpers do the actual file I/O. Nothing here interprets the data - see
vma_parser.py for turning it into address-space regions.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PROC = Path("/proc")
FIRMWARE_MEMMAP = Path("/sys/firmware/memmap")


# --------------------------------------------------------------------------
# /proc/meminfo and /proc/vmstat
# --------------------------------------------------------------------------


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo into bytes. 'MemTotal:  16 kB' -> {'MemTotal': 16384}.

    A few fields (HugePages_Total, ...) are page counts with no unit; those are
    returned as-is.
    """
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        parts = rest.split()
        if not parts:
            continue
        value = int(parts[0])
        if len(parts) > 1 and parts[1] == "kB":
            value *= 1024
        out[key.strip()] = value
    return out


def parse_vmstat(text: str) -> dict[str, int]:
    """Parse /proc/vmstat ('pgmajfault 1234' per line) into a dict of counters."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[0]] = int(parts[1])
    return out


def read_meminfo() -> dict[str, int]:
    return parse_meminfo((PROC / "meminfo").read_text())


def read_vmstat() -> dict[str, int]:
    return parse_vmstat((PROC / "vmstat").read_text())


# --------------------------------------------------------------------------
# /proc/iomem - the physical address map, including devices
# --------------------------------------------------------------------------


@dataclass
class IomemEntry:
    """One line of /proc/iomem. Children are nested by indentation."""

    start: int
    end: int  # exclusive (the file prints inclusive ends; we add 1)
    name: str
    depth: int
    children: list[IomemEntry] = field(default_factory=list)

    @property
    def size(self) -> int:
        return self.end - self.start


_IOMEM_LINE = re.compile(r"^(\s*)([0-9a-fA-F]+)-([0-9a-fA-F]+) : (.*)$")


def parse_iomem(text: str) -> list[IomemEntry]:
    """Parse /proc/iomem into a tree of top-level entries.

    Indentation (two spaces per level) encodes nesting, e.g. a PCI device's
    BAR sits inside its "PCI Bus" window, and "Kernel code" sits inside
    "System RAM".
    """
    roots: list[IomemEntry] = []
    stack: list[IomemEntry] = []
    for line in text.splitlines():
        m = _IOMEM_LINE.match(line)
        if not m:
            continue
        indent, start, end, name = m.groups()
        entry = IomemEntry(int(start, 16), int(end, 16) + 1, name.strip(), len(indent) // 2)
        while stack and stack[-1].depth >= entry.depth:
            stack.pop()
        (stack[-1].children if stack else roots).append(entry)
        stack.append(entry)
    return roots


def iomem_is_redacted(entries: list[IomemEntry]) -> bool:
    """True when the kernel zeroed all addresses (reading as non-root)."""
    return bool(entries) and all(e.start == 0 and e.end == 1 for e in entries)


def read_iomem() -> list[IomemEntry]:
    return parse_iomem((PROC / "iomem").read_text())


# --------------------------------------------------------------------------
# /sys/firmware/memmap - the firmware (E820/UEFI) memory map, readable as a
# normal user. Coarser than iomem: no device names, but real addresses.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FirmwareRange:
    start: int
    end: int  # exclusive
    type: str


def read_firmware_memmap(root: Path = FIRMWARE_MEMMAP) -> list[FirmwareRange]:
    ranges = []
    if not root.is_dir():
        return ranges
    for entry in root.iterdir():
        try:
            start = int((entry / "start").read_text(), 16)
            end = int((entry / "end").read_text(), 16) + 1
            kind = (entry / "type").read_text().strip()
        except (OSError, ValueError):
            continue
        ranges.append(FirmwareRange(start, end, kind))
    return sorted(ranges, key=lambda r: r.start)


# --------------------------------------------------------------------------
# /proc/zoneinfo - how the kernel partitions physical RAM (DMA, DMA32, Normal)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Zone:
    node: int
    name: str
    start: int  # physical byte address
    end: int  # exclusive
    present_pages: int


def parse_zoneinfo(text: str, page_size: int = 4096) -> list[Zone]:
    zones: list[Zone] = []
    node = -1
    name = ""
    spanned = present = 0
    header = re.compile(r"^Node (\d+), zone\s+(\S+)")

    def flush(start_pfn: int) -> None:
        if name and spanned:
            zones.append(
                Zone(node, name, start_pfn * page_size, (start_pfn + spanned) * page_size, present)
            )

    for line in text.splitlines():
        if m := header.match(line):
            node, name = int(m.group(1)), m.group(2)
            spanned = present = 0
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == "spanned":
            spanned = int(parts[1])
        elif parts[0] == "present":
            present = int(parts[1])
        elif parts[0] == "start_pfn:":
            flush(int(parts[1]))
            name = ""  # zone complete; ignore any further lines until the next header
    return zones


def read_zoneinfo() -> list[Zone]:
    return parse_zoneinfo((PROC / "zoneinfo").read_text())


# --------------------------------------------------------------------------
# /proc/<pid>/maps - raw VMA lines
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MapsLine:
    start: int
    end: int  # exclusive (maps already prints exclusive ends)
    perms: str
    offset: int
    dev: str
    inode: int
    path: str


def parse_maps(text: str) -> list[MapsLine]:
    out = []
    for line in text.splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) < 5:
            continue
        lo, _, hi = parts[0].partition("-")
        out.append(
            MapsLine(
                start=int(lo, 16),
                end=int(hi, 16),
                perms=parts[1],
                offset=int(parts[2], 16),
                dev=parts[3],
                inode=int(parts[4]),
                path=parts[5].strip() if len(parts) > 5 else "",
            )
        )
    return out


def read_maps(pid: int | str = "self") -> list[MapsLine]:
    return parse_maps((PROC / str(pid) / "maps").read_text())


def read_exe_path(pid: int | str = "self") -> str | None:
    try:
        return str((PROC / str(pid) / "exe").readlink())
    except OSError:
        return None


# --------------------------------------------------------------------------
# /proc/kallsyms - kernel symbol addresses (zeroed unless root / kptr_restrict=0)
# --------------------------------------------------------------------------

KERNEL_IMAGE_SYMBOLS = (
    "_text",
    "_etext",
    "__start_rodata",
    "_sdata",
    "_edata",
    "__bss_start",
    "_end",
)


def parse_kallsyms(text: str, wanted: tuple[str, ...] = KERNEL_IMAGE_SYMBOLS) -> dict[str, int]:
    """Pick out the kernel-image boundary symbols. Returns {} if addresses are hidden."""
    found: dict[str, int] = {}
    want = set(wanted)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] in want and parts[2] not in found:
            found[parts[2]] = int(parts[0], 16)
            if len(found) == len(want):
                break
    if not found or all(v == 0 for v in found.values()):
        return {}
    return found


def read_kallsyms() -> dict[str, int]:
    try:
        return parse_kallsyms((PROC / "kallsyms").read_text())
    except OSError:
        return {}


def cpu_flags() -> set[str]:
    try:
        for line in (PROC / "cpuinfo").read_text().splitlines():
            if line.startswith("flags"):
                return set(line.partition(":")[2].split())
    except OSError:
        pass
    return set()


# --------------------------------------------------------------------------
# Processes - /proc/<pid>/statm and comm
# --------------------------------------------------------------------------

PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    rss: int  # resident set size: bytes actually in RAM
    vms: int  # virtual size: bytes of address space mapped
    uid: int


def parse_statm(text: str, page_size: int = PAGE_SIZE) -> tuple[int, int]:
    """/proc/<pid>/statm -> (vms_bytes, rss_bytes). Fields are page counts."""
    size, resident = text.split()[:2]
    return int(size) * page_size, int(resident) * page_size


def _process_name(entry: Path) -> str:
    # comm is the *thread* name (a browser shows up as "MainThread"); argv[0]
    # is usually what a person recognizes.
    argv0 = (entry / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
    if argv0:
        return argv0.split(" ", 1)[0].rsplit("/", 1)[-1]
    return (entry / "comm").read_text().strip()


def read_processes() -> list[ProcessInfo]:
    """Every userspace process. Kernel threads (no address space) are skipped.

    Processes can exit between listing and reading, so each one is best-effort.
    """
    procs = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            vms, rss = parse_statm((entry / "statm").read_text())
            if vms == 0:
                continue
            name = _process_name(entry)
            uid = entry.stat().st_uid
        except (OSError, ValueError):
            continue
        procs.append(ProcessInfo(int(entry.name), name, rss, vms, uid))
    return procs


def top_processes(n: int = 5) -> list[ProcessInfo]:
    return sorted(read_processes(), key=lambda p: p.rss, reverse=True)[:n]
