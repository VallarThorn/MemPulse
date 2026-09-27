from itertools import pairwise

from mempulse.engine import proc_parser as pp
from mempulse.engine.vma_parser import (
    ADDR_SPACE_END,
    KernelConfig,
    Kind,
    Region,
    build_kernel_space,
    fill_gaps,
    group_user_vmas,
    layout,
)
from mempulse.utils.formatters import describe_perms, human_bytes, short_hex

GiB = 1 << 30

MAPS = """\
5566aa000000-5566aa001000 r--p 00000000 103:05 100 /usr/bin/demo
5566aa001000-5566aa002000 r-xp 00001000 103:05 100 /usr/bin/demo
5566aa002000-5566aa003000 rw-p 00002000 103:05 100 /usr/bin/demo
5566ab000000-5566ab021000 rw-p 00000000 00:00 0 [heap]
7f0000000000-7f0000028000 r--p 00000000 103:05 200 /usr/lib/libc.so.6
7f0000028000-7f00001b0000 r-xp 00028000 103:05 200 /usr/lib/libc.so.6
7f00001b0000-7f00001b1000 ---p 001b0000 103:05 200 /usr/lib/libc.so.6
7f00001b1000-7f00001b5000 rw-p 001b0000 103:05 200 /usr/lib/libc.so.6
7f00001b5000-7f00001c2000 rw-p 00000000 00:00 0
7f00001d0000-7f00001d2000 r-xp 00000000 00:00 0 [vdso]
7ffc00000000-7ffc00021000 rw-p 00000000 00:00 0 [stack]
ffffffffff600000-ffffffffff601000 --xp 00000000 00:00 0 [vsyscall]
"""

IOMEM = """\
00000000-00000fff : Reserved
00001000-0009efff : System RAM
000a0000-000fffff : PCI Bus 0000:00
  000c0000-000cffff : Video ROM
00100000-7fffffff : System RAM
  01000000-01ffffff : Kernel code
80000000-dfffffff : PCI Bus 0000:00
  a0000000-a0ffffff : 0000:01:00.0
    a0000000-a0ffffff : nvidia
fee00000-fee00fff : Local APIC
"""


def test_formatters():
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(16 << 60) == "16.0 EiB"
    assert short_hex(0x7FFEF75F9000) == "0x7ffe_f75f_9000"
    assert short_hex(0) == "0x0"
    assert describe_perms("---p").startswith("no access")
    assert describe_perms("rw-s") == "read, write; shared"


def test_meminfo_and_vmstat():
    info = pp.parse_meminfo("MemTotal:  16 kB\nHugePages_Total:   0\n")
    assert info == {"MemTotal": 16384, "HugePages_Total": 0}
    assert pp.parse_vmstat("pgmajfault 42\npswpin 7\n") == {"pgmajfault": 42, "pswpin": 7}


def test_iomem_tree():
    roots = pp.parse_iomem(IOMEM)
    assert [r.name for r in roots] == [
        "Reserved",
        "System RAM",
        "PCI Bus 0000:00",
        "System RAM",
        "PCI Bus 0000:00",
        "Local APIC",
    ]
    assert roots[3].children[0].name == "Kernel code"
    assert roots[4].children[0].children[0].name == "nvidia"  # nested two deep
    assert roots[1].end == 0x9F000  # inclusive end converted to exclusive
    assert not pp.iomem_is_redacted(roots)
    redacted = pp.parse_iomem("00000000-00000000 : System RAM\n00000000-00000000 : Reserved\n")
    assert pp.iomem_is_redacted(redacted)


def test_zoneinfo():
    text = """Node 0, zone      DMA
  pages free     3840
        spanned  4095
        present  3999
  start_pfn:           1
Node 0, zone   Normal
        spanned  1000
        present  1000
  start_pfn:           1048576
Node 0, zone  Movable
        spanned  0
"""
    zones = pp.parse_zoneinfo(text)
    assert [(z.name, z.start) for z in zones] == [("DMA", 4096), ("Normal", 4 * GiB)]


def test_kallsyms_redacted_is_empty():
    assert pp.parse_kallsyms("0000000000000000 T _text\n0000000000000000 T _etext\n") == {}
    syms = pp.parse_kallsyms("ffffffff81000000 T _text\nffffffff82000000 T _etext\n")
    assert syms == {"_text": 0xFFFFFFFF81000000, "_etext": 0xFFFFFFFF82000000}


def test_group_user_vmas():
    lines = [ln for ln in pp.parse_maps(MAPS) if ln.end <= 1 << 47]
    groups = group_user_vmas(lines, "/usr/bin/demo")
    kinds = [g.kind for g in groups]
    assert kinds == [Kind.PROGRAM, Kind.HEAP, Kind.LIBRARY, Kind.VDSO, Kind.STACK]
    libc = groups[2]
    # 4 file VMAs + the anonymous .bss glued to its rw segment
    assert libc.end == 0x7F00001C2000
    assert any(c.label.startswith(".bss") for c in libc.children)
    assert any(c.kind is Kind.GUARD or c.perms == "---p" for c in libc.children)
    assert libc.perms == "r-- r-x --- rw-"
    # single-VMA groups are flattened
    assert groups[1].children == [] and groups[1].perms == "rw-p"


def test_fill_gaps_tiles_parent():
    kids = [Region(10, 20, Kind.RAM, "a"), Region(30, 40, Kind.RAM, "b")]
    tiled = fill_gaps(kids, 0, 50)
    assert [(r.start, r.end) for r in tiled] == [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50)]
    assert all(tiled[i].end == tiled[i + 1].start for i in range(len(tiled) - 1))


def test_kernel_template_is_contiguous():
    for five_level in (False, True):
        cfg = KernelConfig(five_level=five_level, kaslr=True, image_size=GiB, kasan=False)
        regions = build_kernel_space(cfg, ram_top=32 * GiB)
        assert regions[-1].end == ADDR_SPACE_END
        for a, b in pairwise(regions):
            assert a.end == b.start, (a.label, b.label)
        direct = next(r for r in regions if r.kind is Kind.DIRECT_MAP)
        assert direct.children[0].size == 32 * GiB


def _space(n_small: int) -> Region:
    kids = [Region(i * 0x10000, i * 0x10000 + 0x1000, Kind.ANON, f"a{i}") for i in range(n_small)]
    heap = Region(0x10000000, 0x10100000, Kind.HEAP, "[heap]")
    user = fill_gaps([*kids, heap], 0, 1 << 47)
    hole = Region(1 << 47, 0xFFFF800000000000, Kind.NONCANONICAL, "hole")
    kernel = Region(0xFFFF800000000000, ADDR_SPACE_END, Kind.KERNEL_OTHER, "kernel")
    return Region(0, ADDR_SPACE_END, Kind.GROUP, "root", children=[*user, hole, kernel])


def test_layout_fills_rows_exactly():
    blocks = layout(_space(3), 30)
    assert sum(b.height for b in blocks) == 30
    assert blocks[0].region.label == "kernel"  # highest address on top
    assert blocks[-1].region.start == 0
    assert [b.top for b in blocks] == sorted(b.top for b in blocks)


def test_layout_merges_under_pressure_but_keeps_heap_and_hole():
    blocks = layout(_space(40), 10)
    assert len(blocks) <= 10
    labels = [b.region.label for b in blocks]
    assert "[heap]" in labels
    assert any(b.region.kind is Kind.NONCANONICAL for b in blocks)
    merged = [b.region for b in blocks if b.region.synthetic]
    assert merged and all(m.zoomable for m in merged)
    # merged groups still tile: zooming in shows every original region
    total = sum(b.region.size for b in blocks)
    assert total == ADDR_SPACE_END


def test_shared_anonymous_mappings_are_not_files():
    text = (
        "7f0000000000-7f0000100000 rw-s 00000000 00:01 5 /dev/zero (deleted)\n"
        "7f0000200000-7f0000300000 rw-s 00000000 00:01 6 /memfd:wayland-shm (deleted)\n"
    )
    groups = group_user_vmas(pp.parse_maps(text), exe=None)
    assert [(g.kind, g.label) for g in groups] == [
        (Kind.ANON, "shared anonymous"),
        (Kind.ANON, "memfd: wayland-shm"),
    ]


def test_physical_space_from_root_iomem(monkeypatch):
    from mempulse.engine import vma_parser

    monkeypatch.setattr(pp, "read_iomem", lambda: pp.parse_iomem(IOMEM))
    monkeypatch.setattr(pp, "read_zoneinfo", list)
    root, privileged = vma_parser.build_physical_space()
    assert privileged
    assert root.end == 1 << 32
    kinds = {r.label: r.kind for r in root.children}
    assert kinds["Local APIC"] is Kind.DEVICE
    assert kinds["unclaimed"] is Kind.UNCLAIMED
    pci = next(r for r in root.children if r.start == 0x80000000)
    assert pci.kind is Kind.PCI and pci.zoomable
    gpu = next(c for c in pci.children if c.label == "0000:01:00.0")
    assert gpu.children[0].label == "nvidia"
    ram = next(r for r in root.children if r.start == 0x100000)
    assert any(c.kind is Kind.RAM_KERNEL for c in ram.children)
    # children always tile their parent exactly
    for parent in (root, pci, ram):
        assert parent.children[0].start == parent.start
        assert parent.children[-1].end == parent.end
