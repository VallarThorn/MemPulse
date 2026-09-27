"""The `?` help overlay: colour legend, the concepts behind each panel, keys."""

from __future__ import annotations

import re

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TabbedContent, TabPane

from mempulse.engine.vma_parser import KIND_INFO, Kind
from mempulse.ui import theme

LEGEND_GROUPS: list[tuple[str, list[Kind]]] = [
    (
        "User space (bottom of the virtual map - private to each process)",
        [Kind.PROGRAM, Kind.LIBRARY, Kind.MAPPED_FILE, Kind.HEAP, Kind.STACK, Kind.ANON,
         Kind.VDSO, Kind.GUARD],
    ),
    (
        "Kernel space (top of the virtual map - shared by every process)",
        [Kind.KERNEL_TEXT, Kind.KERNEL_MODULES, Kind.DIRECT_MAP, Kind.VMALLOC, Kind.VMEMMAP,
         Kind.CPU_ENTRY, Kind.FIXMAP, Kind.EFI, Kind.KASAN, Kind.KERNEL_OTHER],
    ),
    (
        "Physical address space (the real hardware bus)",
        [Kind.RAM, Kind.RAM_KERNEL, Kind.RESERVED, Kind.ACPI, Kind.PCI, Kind.DEVICE, Kind.ROM],
    ),
    ("Holes", [Kind.NONCANONICAL, Kind.UNMAPPED, Kind.UNCLAIMED]),
]  # fmt: skip


def render_legend() -> Group:
    parts: list = []
    for title, kinds in LEGEND_GROUPS:
        parts.append(Text(title, style="bold underline"))
        table = Table.grid(padding=(0, 1))
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True, style="bold")
        table.add_column(style="#cfd8dc")
        for kind in kinds:
            if kind.is_gap:
                fill = "╱╱╱╱" if kind is Kind.NONCANONICAL else "░░░░"
                swatch = Text(fill, style=theme.KIND_COLORS[kind])
            else:
                swatch = Text("    ", style=f"on {theme.KIND_COLORS[kind]}")
            # First sentence only: the inspector shows the full explanation.
            info = re.split(r"(?<=\.)\s+(?=[A-Z])", KIND_INFO[kind])[0]
            table.add_row(swatch, kind.value, info)
        parts += [table, Text()]

    marks = Table.grid(padding=(0, 1))
    marks.add_column(no_wrap=True, style="bold")
    marks.add_column(style="#cfd8dc")
    marks.add_row("»", "Zoomable: contains sub-regions (a library's segments, merged groups).")
    marks.add_row("≈", "Boundaries from the documented kernel layout, not read live.")
    marks.add_row("+N more", "Neighbouring regions merged so everything fits on screen.")
    marks.add_row(
        Text("▲ +2 MiB", style="#69f0ae"), "Pulse: the region grew since the last refresh."
    )
    marks.add_row(Text("▼ -1 MiB", style="#ff5252"), "Pulse: the region shrank.")
    marks.add_row(Text("✚ new", style="#69f0ae"), "Pulse: the region just appeared.")
    parts += [Text("Markers", style="bold underline"), marks]
    return Group(*parts)


CONCEPTS = """\
## Two address spaces

**Virtual** addresses are what a program uses. Every process has its own
private 0x0 to 0xffff_ffff_ffff_ffff map, and the CPU's page tables translate
each 4 KiB page of it to a **physical** address: a real location on the
memory bus. That location might be a stick of RAM, or a device register.
Press `v` and `p` to switch between the two views.

## What is a VMA?

A **Virtual Memory Area** is a run of virtual addresses with the same
permissions (`r`ead / `w`rite / e`x`ecute) and the same backing: a file,
or anonymous zero-filled memory. Every line of `/proc/<pid>/maps` is one VMA.

* Touch an address **outside** every VMA and you get a segmentation fault.
* Touch one **inside** a VMA whose page isn't in RAM yet and you get a *page
  fault*. The kernel quietly loads or zero-fills the page and the program
  continues. This is *demand paging*: a 1 GiB `malloc` costs no RAM until you
  write to it.

## Why is the kernel at the top?

```
0xffff_ffff_ffff_ffff ┌──────────────┐
                      │ kernel space │ 128 TiB, same in every process
0xffff_8000_0000_0000 ├──────────────┤
                      │ non-canonical│ illegal: bits 48-63 must copy bit 47
0x0000_8000_0000_0000 ├──────────────┤
                      │  user space  │ 128 TiB, private per process
0x0000_0000_0000_0000 └──────────────┘
```

x86_64 CPUs use 48 address bits (57 with 5-level paging). The upper bits
must all copy bit 47, which splits the space into two halves with a huge
illegal hole between them. Linux gives the **top half to the kernel** and
maps it into *every* process's page tables. Kernel pages are marked
supervisor-only, so user code can't touch them. But a system call or
interrupt can then run kernel code without switching address spaces.

User space starts at the bottom, so ordinary programs never need to know
where the kernel is. One bit of an address tells you which side it's on.
KASLR shifts the kernel's regions by a random offset at each boot, to make
exploits harder.

The **direct map** is a window onto *all* physical RAM: the kernel reads
physical page N at `page_offset_base + N × 4096`. Device registers are
mapped into the **vmalloc / ioremap** area. That's where the hardware from
the physical view shows up in virtual space.

## Page faults: minor vs major (Disk Read Faults)

* **Minor fault**: the page is already in RAM (in the page cache, or a fresh
  zero page). The kernel just updates the page tables. Takes microseconds, and
  thousands per second is normal.
* **Major fault** (`pgmajfault`, shown as *Disk Read Faults*): the data has to
  be **read from disk**. The file isn't cached, or the page was swapped out.
  Takes about 0.1 ms on an SSD and about 10 ms on a hard disk. A steady stream
  means your working set doesn't fit in RAM.

## Swap and thrashing

Under memory pressure the kernel **swaps out** (`pswpout`) anonymous pages
that haven't been used in a while, to free RAM. Occasional swap-out is healthy.
**Swap-in** (`pswpin`) means one of those pages was needed again.

**Thrashing** is both at once, continuously. Pages are evicted and then needed
straight back. The machine spends its time moving pages instead of running
programs: the UI freezes and the disk is busy. The fix is to use less memory,
add RAM, or use compressed swap in RAM (zram).

## Dirty pages and writeback

A `write()` to a file only copies data into the **page cache** and marks those
pages **dirty**. Kernel flusher threads write them to disk later. That happens
after about 30 s, or sooner once dirty data passes the **background
threshold** (`┊` on the gauge). **Writeback** is data being written right now.

If dirty data reaches the **dirty limit**, the kernel *throttles* processes
that write. They're made to wait for the disk. That's the stall you feel when
copying a big file to a slow USB stick.

## Reading the memory stick

The scale is **logarithmic**. A 4 KiB page gets a visible row, and a 64 TiB
region gets more rows but not billions of them. On a linear scale everything
except the holes would be invisible. Each block's start address is printed at
its bottom edge.
"""

KEYS = """\
| Key | Action |
|---|---|
| `↑` `↓` / `j` `k` | Select region (or scroll the mouse wheel) |
| `Home` / `End` | First / last region |
| `Enter` / `→` / `l` | Zoom into the selected region (double-click works too) |
| `Backspace` / `Esc` / `←` | Zoom out |
| `v` / `p` | Virtual / physical address space |
| `Tab` | Move focus between the memory stick and the process table |
| `Enter` (process table) | Show that process's address space |
| `m` | Back to MemPulse's own address space |
| `r` | Refresh now |
| `?` / `h` | This help |
| `q` | Quit |

Run with `sudo` to see real kernel addresses, PCI/device memory, and other
users' processes.
"""


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help {
        width: 90%;
        max-width: 110;
        height: 90%;
        border: round #8a8a8a;
        background: $surface;
    }
    #help VerticalScroll { padding: 0 1; }
    """
    BINDINGS = [
        Binding("escape,q,question_mark,h", "dismiss", "Close"),
        Binding("1", "tab('legend')", show=False),
        Binding("2", "tab('concepts')", show=False),
        Binding("3", "tab('keys')", show=False),
    ]

    def compose(self) -> ComposeResult:
        with TabbedContent(id="help", initial="concepts"):
            with TabPane("1 Legend", id="legend"), VerticalScroll():
                yield Static(render_legend())
            with TabPane("2 Concepts", id="concepts"), VerticalScroll():
                yield Markdown(CONCEPTS)
            with TabPane("3 Keys", id="keys"), VerticalScroll():
                yield Markdown(KEYS)

    def on_mount(self) -> None:
        help_box = self.query_one("#help")
        help_box.border_title = "MemPulse help"
        help_box.border_subtitle = "1-3 switch tabs · Esc closes"

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
