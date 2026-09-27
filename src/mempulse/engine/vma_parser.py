"""Address-space model: turns raw /proc data into regions, and regions into rows.

Two different address spaces are modeled here and must not be confused:

* **Virtual** - what one process sees: its own user-space mappings at the
  bottom, the non-canonical hole, and the kernel's half at the top (shared by
  every process). Built from /proc/<pid>/maps plus the documented x86_64
  kernel layout (Documentation/arch/x86/x86_64/mm.rst).
* **Physical** - the real address bus: RAM, firmware tables, and device MMIO
  windows (PCI BARs, APIC, ...). Built from /proc/iomem when root, otherwise
  from /sys/firmware/memmap.

`layout()` maps any region's children onto N terminal rows with a
logarithmic scale, because on a linear scale over 2^64 bytes everything but
the holes would be zero rows tall.
"""

from __future__ import annotations

import math
import platform
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from mempulse.engine import proc_parser as pp
from mempulse.utils.formatters import human_bytes

KiB, MiB, GiB, TiB, PiB = (1 << 10), (1 << 20), (1 << 30), (1 << 40), (1 << 50)
ADDR_SPACE_END = 1 << 64


class Kind(Enum):
    """What a region is. `value` is the human-readable name shown in the UI."""

    # user space
    PROGRAM = "Program image"
    LIBRARY = "Shared library"
    MAPPED_FILE = "Mapped file"
    HEAP = "Heap"
    STACK = "Stack"
    ANON = "Anonymous memory"
    VDSO = "vDSO / vvar"
    GUARD = "Guard page"
    # kernel half of the virtual space
    KERNEL_TEXT = "Kernel image"
    KERNEL_MODULES = "Kernel modules"
    DIRECT_MAP = "Direct map of RAM"
    VMALLOC = "vmalloc / ioremap"
    VMEMMAP = "vmemmap (page array)"
    KASAN = "KASAN shadow"
    CPU_ENTRY = "CPU entry area"
    FIXMAP = "Fixmap / vsyscall"
    EFI = "EFI runtime services"
    KERNEL_OTHER = "Kernel internal"
    # holes
    NONCANONICAL = "Non-canonical hole"
    UNMAPPED = "Unmapped"
    # physical
    RAM = "System RAM"
    RAM_KERNEL = "Kernel in RAM"
    RESERVED = "Firmware reserved"
    ACPI = "ACPI tables / NVS"
    PCI = "PCI bus window"
    DEVICE = "Device MMIO"
    ROM = "ROM"
    UNCLAIMED = "Unclaimed bus space"
    # synthetic
    GROUP = "Group"

    @property
    def is_gap(self) -> bool:
        return self in _GAP_KINDS


_GAP_KINDS = {Kind.NONCANONICAL, Kind.UNMAPPED, Kind.UNCLAIMED}

# Kinds that should survive row-pressure merging as long as possible: they are
# what a learner is looking for on the map.
_PINNED_KINDS = {Kind.HEAP, Kind.STACK, Kind.PROGRAM, Kind.KERNEL_TEXT, Kind.DIRECT_MAP}

KIND_INFO: dict[Kind, str] = {
    Kind.PROGRAM: "The executable's own code, read-only data and globals, mapped from "
    "its ELF file.",
    Kind.LIBRARY: "A shared library (.so) mapped into this process. Its code pages are shared "
    "with every other process using the same library.",
    Kind.MAPPED_FILE: "A file mapped with mmap(): reads come straight from the page cache.",
    Kind.HEAP: "The classic brk() heap used by malloc() for small allocations. Grows upwards.",
    Kind.STACK: "The main thread's stack. Grows downwards from the top of user space; "
    "a guard gap below it catches overflows.",
    Kind.ANON: "Anonymous memory: mmap()ed without a file, e.g. large malloc() blocks, "
    "thread stacks, JIT code, or a library's .bss section.",
    Kind.VDSO: "Pages the kernel maps into every process so calls like gettimeofday() "
    "run without a real system call.",
    Kind.GUARD: "Mapped with no permissions on purpose: touching it crashes the program, "
    "which catches overflows between neighbouring regions.",
    Kind.KERNEL_TEXT: "The kernel's own code and data (vmlinux), mapped at the very top of "
    "the address space. Its exact position is randomized at boot (KASLR).",
    Kind.KERNEL_MODULES: "Loadable drivers (.ko files) are placed here, close to the kernel "
    "image so they can call into it with short relative jumps.",
    Kind.DIRECT_MAP: "A 1:1 window onto ALL physical RAM, so the kernel can touch any "
    "physical page by adding a constant offset (page_offset_base).",
    Kind.VMALLOC: "Virtually contiguous kernel allocations (vmalloc) and device registers "
    "mapped with ioremap() - this is where MMIO shows up in virtual space.",
    Kind.VMEMMAP: "An array with one 64-byte 'struct page' for every 4 KiB physical page; "
    "the kernel's bookkeeping for all of RAM.",
    Kind.KASAN: "Shadow memory for the Kernel Address Sanitizer, a debugging feature.",
    Kind.CPU_ENTRY: "Per-CPU data needed at the moment of a system call or interrupt, "
    "mapped even in user page tables (Meltdown/PTI mitigation).",
    Kind.FIXMAP: "Compile-time fixed virtual addresses (APIC, early boot mappings) and the "
    "legacy vsyscall page.",
    Kind.EFI: "UEFI runtime services firmware code, mapped so the kernel can call it.",
    Kind.KERNEL_OTHER: "Kernel bookkeeping area (LDT remap, espfix stacks, guard hole).",
    Kind.NONCANONICAL: "x86_64 only uses 48 (or 57) address bits; the upper bits must copy "
    "bit 47. Everything in this range is illegal - any access faults. It splits "
    "user space (low half) from kernel space (high half).",
    Kind.UNMAPPED: "Legal addresses with nothing mapped. Touching them causes a segfault.",
    Kind.RAM: "Physical DRAM usable by the operating system.",
    Kind.RAM_KERNEL: "Physical RAM holding the loaded kernel image.",
    Kind.RESERVED: "Set aside by the firmware (BIOS/UEFI) - the OS must not use it.",
    Kind.ACPI: "Firmware tables describing the hardware (ACPI Tables), and firmware "
    "memory that must survive sleep (ACPI NVS).",
    Kind.PCI: "An address window routed to a PCI bus: reads and writes here go to "
    "devices, not RAM.",
    Kind.DEVICE: "Memory-mapped I/O: device registers (GPU, NVMe, USB, APIC ...) that "
    "look like memory to the CPU.",
    Kind.ROM: "Read-only firmware images (system BIOS, option ROMs).",
    Kind.UNCLAIMED: "Physical addresses no RAM or device responds to.",
    Kind.GROUP: "Several neighbouring regions merged to fit the screen. Zoom in to see them.",
}


@dataclass
class Region:
    start: int
    end: int  # exclusive
    kind: Kind
    label: str
    perms: str = ""
    detail: str = ""
    children: list[Region] = field(default_factory=list)
    approximate: bool = False  # boundaries come from a template, not the live system
    synthetic: bool = False  # created by layout merging, not by the data source

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def zoomable(self) -> bool:
        return len(self.children) > 1

    def dominant_kind(self) -> Kind:
        """Kind that covers the most bytes among non-gap children (for merged groups)."""
        if self.kind is not Kind.GROUP:
            return self.kind
        totals: dict[Kind, int] = {}
        for child in self.children:
            if not child.kind.is_gap:
                k = child.dominant_kind()
                totals[k] = totals.get(k, 0) + child.size
        return max(totals, key=lambda k: totals[k]) if totals else Kind.UNMAPPED


def fill_gaps(
    children: list[Region], start: int, end: int, kind: Kind = Kind.UNMAPPED, label: str = ""
) -> list[Region]:
    """Sort children and insert filler regions so they exactly tile [start, end).

    Overlapping children (which /proc/iomem can produce) are clipped.
    """
    out: list[Region] = []
    cursor = start
    for child in sorted(children, key=lambda r: r.start):
        if child.end <= cursor:
            continue
        if child.start > cursor:
            out.append(Region(cursor, child.start, kind, label or kind.value))
        child.start = max(child.start, cursor)
        out.append(child)
        cursor = child.end
    if cursor < end:
        out.append(Region(cursor, end, kind, label or kind.value))
    return out


# ==========================================================================
# Virtual address space
# ==========================================================================

# (start, end_inclusive, kind, label). From Documentation/arch/x86/x86_64/mm.rst.
_X86_64_4LEVEL_HEAD = [
    (0xFFFF800000000000, 0xFFFF87FFFFFFFFFF, Kind.KERNEL_OTHER, "Guard hole (hypervisor)"),
    (0xFFFF880000000000, 0xFFFF887FFFFFFFFF, Kind.KERNEL_OTHER, "LDT remap for PTI"),
    (0xFFFF888000000000, 0xFFFFC87FFFFFFFFF, Kind.DIRECT_MAP, "Direct map of all RAM"),
    (0xFFFFC88000000000, 0xFFFFC8FFFFFFFFFF, Kind.UNMAPPED, "unused hole"),
    (0xFFFFC90000000000, 0xFFFFE8FFFFFFFFFF, Kind.VMALLOC, "vmalloc / ioremap"),
    (0xFFFFE90000000000, 0xFFFFE9FFFFFFFFFF, Kind.UNMAPPED, "unused hole"),
    (0xFFFFEA0000000000, 0xFFFFEAFFFFFFFFFF, Kind.VMEMMAP, "vmemmap (struct pages)"),
    (0xFFFFEB0000000000, 0xFFFFEBFFFFFFFFFF, Kind.UNMAPPED, "unused hole"),
    (0xFFFFEC0000000000, 0xFFFFFBFFFFFFFFFF, Kind.KASAN, "KASAN shadow memory"),
]
_X86_64_5LEVEL_HEAD = [
    (0xFF00000000000000, 0xFF0FFFFFFFFFFFFF, Kind.KERNEL_OTHER, "Guard hole (hypervisor)"),
    (0xFF10000000000000, 0xFF10FFFFFFFFFFFF, Kind.KERNEL_OTHER, "LDT remap for PTI"),
    (0xFF11000000000000, 0xFF90FFFFFFFFFFFF, Kind.DIRECT_MAP, "Direct map of all RAM"),
    (0xFF91000000000000, 0xFF9FFFFFFFFFFFFF, Kind.UNMAPPED, "unused hole"),
    (0xFFA0000000000000, 0xFFD1FFFFFFFFFFFF, Kind.VMALLOC, "vmalloc / ioremap"),
    (0xFFD2000000000000, 0xFFD3FFFFFFFFFFFF, Kind.UNMAPPED, "unused hole"),
    (0xFFD4000000000000, 0xFFD5FFFFFFFFFFFF, Kind.VMEMMAP, "vmemmap (struct pages)"),
    (0xFFD6000000000000, 0xFFDEFFFFFFFFFFFF, Kind.UNMAPPED, "unused hole"),
    (0xFFDF000000000000, 0xFFFFFBFFFFFFFFFF, Kind.KASAN, "KASAN shadow memory"),
]
_X86_64_TAIL = [
    (0xFFFFFC0000000000, 0xFFFFFDFFFFFFFFFF, Kind.UNMAPPED, "unused hole"),
    (0xFFFFFE0000000000, 0xFFFFFE7FFFFFFFFF, Kind.CPU_ENTRY, "cpu_entry_area"),
    (0xFFFFFE8000000000, 0xFFFFFEFFFFFFFFFF, Kind.UNMAPPED, "unused hole"),
    (0xFFFFFF0000000000, 0xFFFFFF7FFFFFFFFF, Kind.KERNEL_OTHER, "espfix stacks"),
    (0xFFFFFF8000000000, 0xFFFFFFEEFFFFFFFF, Kind.UNMAPPED, "unused hole"),
    (0xFFFFFFEF00000000, 0xFFFFFFFEFFFFFFFF, Kind.EFI, "EFI runtime mapping"),
    (0xFFFFFFFF00000000, 0xFFFFFFFF7FFFFFFF, Kind.UNMAPPED, "unused hole"),
    # kernel image + modules are inserted here (their split depends on config)
    (0xFFFFFFFFFF000000, 0xFFFFFFFFFF5FFFFF, Kind.FIXMAP, "fixmap"),
    (0xFFFFFFFFFF600000, 0xFFFFFFFFFF600FFF, Kind.FIXMAP, "legacy vsyscall page"),
    (0xFFFFFFFFFF601000, 0xFFFFFFFFFFFFFFFF, Kind.UNMAPPED, "unused hole"),
]
_KERNEL_IMAGE_BASE = 0xFFFFFFFF80000000
_MODULES_END = 0xFFFFFFFFFF000000


@dataclass
class KernelConfig:
    """What we could learn about the running kernel without root."""

    five_level: bool = False
    kaslr: bool = True
    image_size: int = 1 * GiB  # KERNEL_IMAGE_SIZE: 1 GiB with RANDOMIZE_BASE, else 512 MiB
    kasan: bool | None = None  # None = unknown
    symbols: dict[str, int] = field(default_factory=dict)

    @classmethod
    def detect(cls) -> KernelConfig:
        cfg = cls()
        try:
            config_text = Path(f"/boot/config-{platform.release()}").read_text()
        except OSError:
            config_text = ""
        try:
            cmdline = Path("/proc/cmdline").read_text().split()
        except OSError:
            cmdline = []
        if config_text:
            randomize = "CONFIG_RANDOMIZE_BASE=y" in config_text
            cfg.image_size = 1 * GiB if randomize else 512 * MiB
            cfg.kaslr = randomize and "nokaslr" not in cmdline
            cfg.kasan = "CONFIG_KASAN=y" in config_text
        else:
            cfg.kaslr = "nokaslr" not in cmdline
        # 5-level paging needs CPU support (la57) and must not be disabled.
        cfg.five_level = "la57" in pp.cpu_flags() and "no5lvl" not in cmdline
        cfg.symbols = pp.read_kallsyms()
        return cfg


def _template(rows: list[tuple[int, int, Kind, str]], approx: bool) -> list[Region]:
    return [Region(s, e + 1, k, label, approximate=approx) for s, e, k, label in rows]


def build_kernel_space(cfg: KernelConfig, ram_top: int) -> list[Region]:
    """The kernel half of the virtual address space for x86_64."""
    head = _X86_64_5LEVEL_HEAD if cfg.five_level else _X86_64_4LEVEL_HEAD
    regions = _template(head, approx=cfg.kaslr) + _template(_X86_64_TAIL, approx=False)
    kaslr_note = (
        " KASLR shifts this region's base by a random offset at every boot, so the true "
        "start lies somewhere inside the range shown."
        if cfg.kaslr
        else ""
    )

    for r in regions:
        if r.kind is Kind.DIRECT_MAP:
            r.detail = (
                f"Your machine's physical RAM ({human_bytes(ram_top)} of physical address "
                f"space) occupies only the first part of this {human_bytes(r.size)} window."
                + kaslr_note
            )
            used_end = min(r.start + ram_top, r.end)
            r.children = fill_gaps(
                [Region(r.start, used_end, Kind.DIRECT_MAP, "RAM mapped 1:1", approximate=True)],
                r.start,
                r.end,
                label="room for more RAM",
            )
        elif r.kind in (Kind.VMALLOC, Kind.VMEMMAP):
            r.detail = kaslr_note.strip()
        elif r.kind is Kind.KASAN and cfg.kasan is False:
            r.kind = Kind.UNMAPPED
            r.label = "KASAN shadow (disabled)"
            r.detail = "This kernel was built without KASAN, so the range is unused."

    image_end = _KERNEL_IMAGE_BASE + cfg.image_size
    image = Region(
        _KERNEL_IMAGE_BASE,
        image_end,
        Kind.KERNEL_TEXT,
        "Kernel image (vmlinux)",
        approximate=True,
        detail=f"Reserved window for the kernel image ({human_bytes(cfg.image_size)})."
        + (
            " KASLR loads the kernel at a random 2 MiB-aligned slot inside it." if cfg.kaslr else ""
        ),
    )
    image.children = _kernel_image_sections(cfg.symbols, image)
    modules = Region(
        image_end, _MODULES_END, Kind.KERNEL_MODULES, "Module space", approximate=cfg.kaslr
    )
    regions += [image, modules]
    return sorted(regions, key=lambda r: r.start)


def _kernel_image_sections(sym: dict[str, int], image: Region) -> list[Region]:
    """Real code/rodata/data/bss addresses, available only when kallsyms isn't redacted."""
    order = [
        ("_text", "_etext", "code (.text)", "r-x"),
        ("__start_rodata", "_sdata", "read-only data (.rodata)", "r--"),
        ("_sdata", "_edata", "data (.data)", "rw-"),
        ("__bss_start", "_end", "zeroed data (.bss)", "rw-"),
    ]
    sections = [
        Region(sym[a], sym[b], Kind.KERNEL_TEXT, label, perms=perms)
        for a, b, label, perms in order
        if a in sym and b in sym and image.start <= sym[a] < sym[b] <= image.end
    ]
    if not sections:
        return []
    image.approximate = False
    return fill_gaps(sections, image.start, image.end, label="unused image slot")


def _classify_vma(line: pp.MapsLine, exe: str | None) -> tuple[Kind, str]:
    path = line.path
    if path == "[heap]":
        return Kind.HEAP, "[heap]"
    if path.startswith("[stack"):
        return Kind.STACK, path
    if path in ("[vdso]", "[vvar]", "[vvar_vclock]"):
        return Kind.VDSO, path
    if path.startswith("[anon:"):
        return Kind.ANON, path
    # Shared memory that the kernel backs with an internal (deleted) file.
    if path.startswith("/dev/zero"):
        return Kind.ANON, "shared anonymous"
    if path.startswith("/memfd:"):
        return Kind.ANON, "memfd: " + path.removeprefix("/memfd:").removesuffix(" (deleted)")
    if path.startswith("/SYSV"):
        return Kind.ANON, "SysV shared memory"
    if path.startswith("/"):
        name = path.removesuffix(" (deleted)").rsplit("/", 1)[-1]
        if exe and path == exe:
            return Kind.PROGRAM, name
        if ".so" in name:
            return Kind.LIBRARY, name
        return Kind.MAPPED_FILE, name
    if line.perms.startswith("---"):
        return Kind.GUARD, "guard"
    return Kind.ANON, "anonymous"


def _vma_region(line: pp.MapsLine, kind: Kind, label: str) -> Region:
    return Region(line.start, line.end, kind, label, perms=line.perms, detail=line.path)


def group_user_vmas(lines: list[pp.MapsLine], exe: str | None) -> list[Region]:
    """Collapse the dozens of raw VMAs into one region per file / anonymous run.

    A library is typically 4-5 VMAs (r--, r-x, r--, rw-, and an anonymous .bss
    right after). They become one LIBRARY region whose children are the VMAs.
    """
    groups: list[Region] = []
    for line in sorted(lines, key=lambda x: x.start):
        kind, label = _classify_vma(line, exe)
        vma = _vma_region(line, kind, label)
        prev = groups[-1] if groups else None
        same_file = prev is not None and line.path.startswith("/") and prev.detail == line.path
        # Anonymous rw mapping glued to a file's writable segment = its .bss.
        is_bss = (
            prev is not None
            and kind is Kind.ANON
            and not line.path
            and prev.kind in (Kind.PROGRAM, Kind.LIBRARY)
            and prev.end == line.start
            and prev.children[-1].perms[1] == "w"
        )
        same_anon_run = (
            prev is not None
            and kind is Kind.ANON
            and prev.kind is Kind.ANON
            and prev.label == label
            and prev.end == line.start
        )
        if prev is not None and (same_file or is_bss or same_anon_run):
            if is_bss:
                vma.label = ".bss (zeroed globals)"
            prev.children.append(vma)
            prev.end = line.end
        else:
            groups.append(
                Region(line.start, line.end, kind, label, detail=line.path, children=[vma])
            )

    for g in groups:
        g.perms = " ".join(dict.fromkeys(c.perms[:3] for c in g.children))
        g.children = fill_gaps(g.children, g.start, g.end, label="gap")
        if len(g.children) == 1:
            g.perms = g.children[0].perms
            g.children = []
    return groups


def build_virtual_space(pid: int | str = "self", cfg: KernelConfig | None = None) -> Region:
    """The full 2^64 virtual address space of one process."""
    cfg = cfg or KernelConfig.detect()
    arch = platform.machine()
    lines = pp.read_maps(pid)
    exe = pp.read_exe_path(pid)
    ram_top = physical_ram_top()

    if arch == "x86_64":
        user_top = (1 << 56) if cfg.five_level else (1 << 47)
        kernel_regions = build_kernel_space(cfg, ram_top)
        kernel_start = kernel_regions[0].start
    else:
        # Generic 48-bit split (aarch64 with 4 KiB pages and 4-level tables).
        user_top = 1 << 48
        kernel_start = 0xFFFF000000000000
        kernel_regions = [
            Region(
                kernel_start,
                ADDR_SPACE_END,
                Kind.KERNEL_OTHER,
                "Kernel space",
                approximate=True,
                detail=f"The detailed kernel layout for {arch} isn't modeled yet.",
            )
        ]

    # Anything above user space in the maps file (e.g. [vsyscall]) is a kernel
    # mapping made visible to the process; the kernel template already has it.
    user_lines = [ln for ln in lines if ln.end <= user_top]
    user = fill_gaps(group_user_vmas(user_lines, exe), 0, user_top)
    hole = Region(user_top, kernel_start, Kind.NONCANONICAL, "Non-canonical hole")

    name = exe.removesuffix(" (deleted)").rsplit("/", 1)[-1] if exe else str(pid)
    return Region(
        0,
        ADDR_SPACE_END,
        Kind.GROUP,
        f"Virtual address space of {name} (pid {pid})",
        children=[*user, hole, *kernel_regions],
    )


# ==========================================================================
# Physical address space
# ==========================================================================


def _classify_iomem(name: str) -> Kind:
    n = name.lower()
    if n.startswith("kernel "):
        return Kind.RAM_KERNEL
    if n in ("system ram", "persistent memory"):
        return Kind.RAM
    if n.startswith("acpi"):
        return Kind.ACPI
    if n.startswith(("pci bus", "pci ecam", "pci mmconfig")):
        return Kind.PCI
    if "rom" in n.split():
        return Kind.ROM
    if n in ("reserved", "soft reserved", "unknown e820 type"):
        return Kind.RESERVED
    return Kind.DEVICE


def _iomem_region(e: pp.IomemEntry) -> Region:
    kind = _classify_iomem(e.name)
    region = Region(e.start, e.end, kind, e.name)
    if e.children:
        filler = {
            Kind.RAM: (Kind.RAM, "free for general use"),
            Kind.PCI: (Kind.UNCLAIMED, "unassigned bus space"),
        }.get(kind, (Kind.UNCLAIMED, "unused"))
        region.children = fill_gaps(
            [_iomem_region(c) for c in e.children], e.start, e.end, filler[0], filler[1]
        )
    return region


def _split_by_zones(region: Region, zones: list[pp.Zone]) -> None:
    """Tell the user which kernel memory zone(s) a RAM region belongs to."""
    parts = []
    for z in zones:
        lo, hi = max(region.start, z.start), min(region.end, z.end)
        if lo < hi:
            parts.append(Region(lo, hi, Kind.RAM, f"System RAM in ZONE_{z.name.upper()}"))
    names = ", ".join(p.label.rsplit("_", 1)[-1] for p in parts)
    if names:
        region.detail = (region.detail + f" Kernel zone(s): {names}.").strip()
    if len(parts) > 1 and not region.children:
        region.children = fill_gaps(parts, region.start, region.end, Kind.RAM, "System RAM")


_ZONE_NOTE = (
    "ZONE_DMA (<16 MiB) and ZONE_DMA32 (<4 GiB) exist for old devices that can only "
    "address low memory; ZONE_NORMAL is everything else."
)


def build_physical_space() -> tuple[Region, bool]:
    """Physical address map. Returns (root, privileged) - privileged=False means
    device MMIO detail is missing because /proc/iomem was redacted."""
    try:
        zones = pp.read_zoneinfo()
    except OSError:
        zones = []

    entries = pp.read_iomem()
    privileged = not pp.iomem_is_redacted(entries)
    if privileged:
        top_level = [_iomem_region(e) for e in entries]
        source = "/proc/iomem"
    else:
        top_level = [
            Region(r.start, r.end, _classify_iomem(r.type), r.type)
            for r in pp.read_firmware_memmap()
        ]
        source = "/sys/firmware/memmap (run as root for PCI/device detail)"

    for r in top_level:
        if r.kind is Kind.RAM:
            _split_by_zones(r, zones)

    top = max((r.end for r in top_level), default=0)
    top = 1 << max(32, math.ceil(math.log2(top))) if top else 1 << 32
    root = Region(
        0,
        top,
        Kind.GROUP,
        "Physical address space",
        detail=f"Source: {source}. {_ZONE_NOTE}",
        children=fill_gaps(top_level, 0, top, Kind.UNCLAIMED, "unclaimed"),
    )
    return root, privileged


def physical_ram_top() -> int:
    """Highest physical RAM address + 1; how much of the direct map is in use."""
    ranges = pp.read_firmware_memmap()
    ram = [r.end for r in ranges if r.type == "System RAM"]
    if ram:
        return max(ram)
    try:
        return pp.read_meminfo()["MemTotal"]
    except (OSError, KeyError):
        return 0


# ==========================================================================
# Layout: regions -> terminal rows
# ==========================================================================


@dataclass
class Block:
    """One region placed on screen. Rows count from the top (high addresses)."""

    region: Region
    top: int
    height: int


def _weight(r: Region) -> float:
    # log2 scale: a 4 KiB page weighs 1, 1 GiB weighs 19, 128 TiB weighs 36.
    w = max(math.log2(max(r.size, 4 * KiB)) - 11, 1.0)
    return w * (0.35 if r.kind.is_gap else 1.0)


def _merge(a: Region, b: Region) -> Region:
    parts = [*(a.children if a.synthetic else [a]), *(b.children if b.synthetic else [b])]
    named = [p for p in parts if not p.kind.is_gap]
    biggest = max(named or parts, key=lambda p: p.size)
    others = len(named) - 1
    label = biggest.label + (f" +{others} more" if others > 0 else "")
    return Region(a.start, b.end, Kind.GROUP, label, children=parts, synthetic=True)


def _merge_cost(a: Region, b: Region) -> float:
    if Kind.NONCANONICAL in (a.kind, b.kind):
        return math.inf  # never hide the user/kernel boundary
    cost = float(a.size + b.size)
    if a.kind in _PINNED_KINDS or b.kind in _PINNED_KINDS:
        cost *= 1 << 40
    return cost


def layout(parent: Region, rows: int) -> list[Block]:
    """Place parent's children on `rows` rows, highest address at row 0.

    1. While there are more regions than rows, merge the adjacent pair with the
       smallest combined size (pinned kinds like heap/stack last).
    2. Give every region one row, then share out the remaining rows in
       proportion to log2(size) - so a 4 KiB vDSO and a 64 TiB direct map are
       both visible, and bigger still reads as bigger.
    """
    items = list(parent.children) or [parent]
    rows = max(rows, 1)
    while len(items) > rows:
        costs = [_merge_cost(items[i], items[i + 1]) for i in range(len(items) - 1)]
        i = min(range(len(costs)), key=costs.__getitem__)
        if math.isinf(costs[i]):
            break  # only unmergeable holes left; some blocks will fall off-screen
        items[i : i + 2] = [_merge(items[i], items[i + 1])]

    heights = [1] * len(items)
    spare = rows - len(items)
    if spare > 0:
        weights = [_weight(r) for r in items]
        total = sum(weights)
        shares = [spare * w / total for w in weights]
        extra = [int(s) for s in shares]
        # largest-remainder rounding so heights sum exactly to `rows`
        leftover = spare - sum(extra)
        for i in sorted(range(len(items)), key=lambda i: shares[i] - extra[i], reverse=True)[
            :leftover
        ]:
            extra[i] += 1
        heights = [h + e for h, e in zip(heights, extra, strict=True)]

    blocks = []
    row = 0
    for region, height in zip(reversed(items), reversed(heights), strict=True):
        blocks.append(Block(region, row, height))
        row += height
    return blocks
