"""TrueColor palette: one color family per part of the address space.

Kernel = reds, user code/libs = ambers/blues, heap = purple, RAM = green,
devices = magenta, holes = greys.
"""

from __future__ import annotations

from rich.color import Color

from mempulse.engine.vma_parser import Kind

KIND_COLORS: dict[Kind, str] = {
    # user space
    Kind.PROGRAM: "#4fc3f7",
    Kind.LIBRARY: "#ffb300",
    Kind.MAPPED_FILE: "#ffd54f",
    Kind.HEAP: "#ab47bc",
    Kind.STACK: "#42a5f5",
    Kind.ANON: "#9575cd",
    Kind.VDSO: "#80cbc4",
    Kind.GUARD: "#546e7a",
    # kernel
    Kind.KERNEL_TEXT: "#e53935",
    Kind.KERNEL_MODULES: "#ef5350",
    Kind.DIRECT_MAP: "#c62828",
    Kind.VMALLOC: "#d81b60",
    Kind.VMEMMAP: "#ad1457",
    Kind.KASAN: "#880e4f",
    Kind.CPU_ENTRY: "#ff7043",
    Kind.FIXMAP: "#f4511e",
    Kind.EFI: "#ec407a",
    Kind.KERNEL_OTHER: "#b71c1c",
    # holes
    Kind.NONCANONICAL: "#5c5c5c",
    Kind.UNMAPPED: "#6e6e6e",
    Kind.UNCLAIMED: "#6e6e6e",
    # physical
    Kind.RAM: "#43a047",
    Kind.RAM_KERNEL: "#e53935",
    Kind.RESERVED: "#78909c",
    Kind.ACPI: "#00acc1",
    Kind.PCI: "#d500f9",
    Kind.DEVICE: "#e040fb",
    Kind.ROM: "#a1887f",
    Kind.GROUP: "#9e9e9e",
}

FRAME = "#8a8a8a"
ADDRESS = "#b0bec5"
SELECT_MARK = "#ffffff"


def kind_color(kind: Kind) -> Color:
    return Color.parse(KIND_COLORS.get(kind, "#9e9e9e"))


def shade(color: Color, factor: float) -> Color:
    """Darken (factor < 1) or lighten (factor > 1) a TrueColor color."""
    r, g, b = color.get_truecolor()
    if factor <= 1:
        return Color.from_rgb(r * factor, g * factor, b * factor)
    f = factor - 1
    return Color.from_rgb(r + (255 - r) * f, g + (255 - g) * f, b + (255 - b) * f)


def text_on(bg: Color) -> Color:
    """Black or white, whichever reads better on `bg` (WCAG relative luminance)."""
    r, g, b = (c / 255 for c in bg.get_truecolor())
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return Color.parse("#101010" if lum > 0.22 else "#f5f5f5")


def blend(a: Color, b: Color, t: float) -> Color:
    """Mix `t` (0..1) of color b into color a."""
    (r1, g1, b1), (r2, g2, b2) = a.get_truecolor(), b.get_truecolor()
    t = min(max(t, 0.0), 1.0)
    return Color.from_rgb(r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t)
