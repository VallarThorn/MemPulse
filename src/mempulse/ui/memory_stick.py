"""The Memory Stick: a vertical, zoomable map of an address space.

High addresses are at the top and 0x0 at the bottom, like the address-space
diagrams in textbooks. Every region gets at least one row, and taller blocks
mean bigger regions (on a log scale - see vma_parser.layout).

    0xffff_ffff_ffff_ffff ┌──────────────────────────┐
                          │ Kernel image (vmlinux)  »│ Kernel image
    0xffff_ffff_8000_0000 │ 1.0 GiB                  │
                          │░░░░ 16 EiB non-canon ░░░░│
                      0x0 └──────────────────────────┘

Keys: ↑/↓ (or j/k) select, Enter zoom in, Backspace/Esc zoom out.
Mouse: hover to inspect, click to select, double-click to zoom.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from mempulse.engine.vma_parser import Block, Kind, Region, layout
from mempulse.ui import theme
from mempulse.utils.formatters import human_bytes, short_hex

ADDR_W = 22  # " 0xffff_ffff_ff60_1000 " fits
MIN_BAR_W = 22
MAX_BAR_W = 34
HEADER_ROWS = 2  # title, top frame
FOOTER_ROWS = 2  # bottom frame, key hints
PULSE_SECONDS = 2.5  # how long a changed block glows
GROW_TINT, SHRINK_TINT = "#69f0ae", "#ff5252"


@dataclass(frozen=True)
class Change:
    """A region's size change between two refreshes."""

    at: float  # time.monotonic()
    delta: int  # bytes; for a new region, its size
    new: bool = False

    def pulse(self, now: float) -> float:
        """1.0 right after the change, fading linearly to 0."""
        return max(0.0, 1.0 - (now - self.at) / PULSE_SECONDS)


def region_key(r: Region) -> tuple[int, str]:
    # Start + kind survives refreshes: a growing heap keeps its start address.
    return (r.start, r.kind.name)


def _fit(left: str, right: str, width: int) -> str:
    """`left` padded (or cut with …) so that `right` ends exactly at `width`."""
    if width <= 0:
        return ""
    room = width - len(right)
    if room < 4:  # not enough space for both; the name matters more
        right, room = "", width
    if len(left) > room:
        left = left[: max(room - 1, 0)] + "…"
    return left.ljust(room) + right


def _center(text: str, width: int, fill: str) -> str:
    if len(text) > width:
        text = text[: max(width - 1, 0)] + "…"
    pad = width - len(text)
    return fill * (pad // 2) + text + fill * (pad - pad // 2)


class MemoryStick(Widget, can_focus=True):
    """Vertical address-space map. Feed it a root Region with `set_root()`."""

    DEFAULT_CSS = """
    MemoryStick {
        width: 78;
        max-width: 55%;
        height: 1fr;
        min-height: 12;
    }
    """

    BINDINGS = [
        Binding("up,k", "move(-1)", "Up", show=False),
        Binding("down,j", "move(1)", "Down", show=False),
        Binding("home", "move_to(0)", "Top", show=False),
        Binding("end", "move_to(-1)", "Bottom", show=False),
        Binding("enter,right,l", "zoom_in", "Zoom in"),
        Binding("backspace,escape,left", "zoom_out", "Zoom out"),
    ]

    class Highlighted(Message):
        """Posted when the hovered or selected region changes."""

        def __init__(self, region: Region, view: Region) -> None:
            super().__init__()
            self.region = region
            self.view = view  # the region currently zoomed into

    class Zoomed(Message):
        def __init__(self, path: list[Region]) -> None:
            super().__init__()
            self.path = path

    def __init__(self, root: Region | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._path: list[Region] = [root] if root else []
        self._blocks: list[Block] = []
        self._row_to_block: list[int] = []
        self._selected = 0
        self._hover: int | None = None
        self._changes: dict[tuple[int, str], Change] = {}
        self._animating = False

    # ------------------------------------------------------------------ data

    @property
    def view(self) -> Region | None:
        return self._path[-1] if self._path else None

    @property
    def selected_region(self) -> Region | None:
        return self._blocks[self._selected].region if self._blocks else None

    def set_root(self, root: Region, keep_zoom: bool = True) -> None:
        """Swap in fresh data, keeping the zoom path and selection where possible.

        Pass keep_zoom=False when switching to a different address space.
        """
        same_space = keep_zoom and bool(self._path) and self._path[0].label == root.label
        old_keys = [(r.start, r.end) for r in self._path[1:]] if keep_zoom else []
        selected = self.selected_region if keep_zoom else None
        old_sizes = {
            region_key(b.region): (b.region.size, len(b.region.children)) for b in self._blocks
        }
        hover = self._hover
        self._path = [root]
        for key in old_keys:
            match = next(
                (b.region for b in self._layout() if (b.region.start, b.region.end) == key), None
            )
            if match is None or not match.zoomable:
                break
            self._path.append(match)
        self._relayout(keep=selected, hover=hover)
        if same_space and old_sizes:
            self._record_changes(old_sizes)
        elif not keep_zoom:
            self._changes.clear()

    def _record_changes(self, old_sizes: dict[tuple[int, str], tuple[int, int]]) -> None:
        now = time.monotonic()
        for b in self._blocks:
            r = b.region
            if r.kind.is_gap:  # holes resize whenever a neighbour does: noise
                continue
            key = region_key(r)
            if key in old_sizes:
                old_size, old_members = old_sizes[key]
                # A merged group that gained/lost members was reshuffled, not resized.
                reshuffled = r.synthetic and old_members != len(r.children)
                if old_size != r.size and not reshuffled:
                    self._changes[key] = Change(now, r.size - old_size)
            elif not r.synthetic:  # merged groups reshuffle; only real regions are "new"
                self._changes[key] = Change(now, r.size, new=True)
        # keep a minute of history for the inspector's "last change"
        self._changes = {k: c for k, c in self._changes.items() if now - c.at < 60}

    def last_change(self, region: Region) -> Change | None:
        return self._changes.get(region_key(region))

    def _pulse_of(self, region: Region, now: float) -> tuple[float, Change | None]:
        change = self._changes.get(region_key(region))
        return (change.pulse(now), change) if change else (0.0, None)

    def on_mount(self) -> None:
        self.set_interval(1 / 12, self._animate)

    def _animate(self) -> None:
        now = time.monotonic()
        active = any(c.pulse(now) > 0 for c in self._changes.values())
        if active or self._animating:  # one extra frame to clear the last glow
            self.refresh()
        self._animating = active

    def _body_rows(self) -> int:
        return max(self.size.height - HEADER_ROWS - FOOTER_ROWS, 1)

    def _layout(self) -> list[Block]:
        return layout(self.view, self._body_rows()) if self.view else []

    def _relayout(self, keep: Region | None = None, hover: int | None = None) -> None:
        if self.size.height == 0:
            return  # not laid out yet; on_resize will call us with a real size
        self._blocks = self._layout()
        self._row_to_block = [i for i, b in enumerate(self._blocks) for _ in range(b.height)]
        # A refresh keeps the mouse hover; a zoom or resize invalidates it.
        self._hover = hover if hover is not None and hover < len(self._blocks) else None
        if keep is not None:
            self._selected = self._index_containing(keep)
        elif self._blocks:
            # Start on the first real region rather than a hole.
            self._selected = next(
                (i for i, b in enumerate(self._blocks) if not b.region.kind.is_gap), 0
            )
        self._selected = min(self._selected, max(len(self._blocks) - 1, 0))
        self.refresh()
        self._announce()

    def _index_containing(self, region: Region) -> int:
        for i, b in enumerate(self._blocks):
            if b.region.start <= region.start < b.region.end:
                return i
        return self._selected

    def _announce(self) -> None:
        idx = self._hover if self._hover is not None else self._selected
        if self._blocks and self.view:
            self.post_message(self.Highlighted(self._blocks[idx].region, self.view))

    def on_resize(self, _: events.Resize) -> None:
        self._relayout(keep=self.selected_region)

    # --------------------------------------------------------------- actions

    def action_move(self, delta: int) -> None:
        if self._blocks:
            self._selected = max(0, min(len(self._blocks) - 1, self._selected + delta))
            self._hover = None
            self.refresh()
            self._announce()

    def action_move_to(self, index: int) -> None:
        if self._blocks:
            self.action_move((index % len(self._blocks)) - self._selected)

    def action_zoom_in(self) -> None:
        region = self.selected_region
        if region is not None and region.zoomable:
            self._path.append(region)
            self._relayout()
            self.post_message(self.Zoomed(list(self._path)))

    def action_zoom_out(self) -> None:
        if len(self._path) > 1:
            left = self._path.pop()
            self._relayout(keep=left)
            self.post_message(self.Zoomed(list(self._path)))

    # ----------------------------------------------------------------- mouse

    def _block_at(self, y: int) -> int | None:
        row = y - HEADER_ROWS
        return self._row_to_block[row] if 0 <= row < len(self._row_to_block) else None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        idx = self._block_at(event.y)
        if idx != self._hover:
            self._hover = idx
            self.refresh()
            self._announce()

    def on_leave(self, _: events.Leave) -> None:
        if self._hover is not None:
            self._hover = None
            self.refresh()
            self._announce()

    def on_click(self, event: events.Click) -> None:
        idx = self._block_at(event.y)
        if idx is None:
            return
        self.focus()
        self._selected = idx
        if getattr(event, "chain", 1) >= 2:
            self.action_zoom_in()
        else:
            self.refresh()
            self._announce()

    def on_mouse_scroll_down(self, _: events.MouseScrollDown) -> None:
        self.action_move(1)

    def on_mouse_scroll_up(self, _: events.MouseScrollUp) -> None:
        self.action_move(-1)

    # ------------------------------------------------------------- rendering

    def _geometry(self) -> tuple[int, int]:
        """(bar width, annotation width) for the current widget width."""
        free = self.size.width - ADDR_W - 2
        bar = max(MIN_BAR_W, min(MAX_BAR_W, free - 16 if free > MIN_BAR_W + 16 else free))
        return bar, max(free - bar, 0)

    def render_line(self, y: int) -> Strip:
        width = self.size.width
        if not self.view:
            return Strip([Segment(" " * width)])
        bar_w, note_w = self._geometry()
        frame = Style(color=theme.FRAME)
        addr = Style(color=theme.ADDRESS)
        body = self._body_rows()

        if y == 0:
            return self._title_line(width)
        if y == 1:
            segs = [
                Segment(short_hex(self.view.end - 1).rjust(ADDR_W - 1) + " ", addr),
                Segment("┌" + "─" * bar_w + "┐", frame),
            ]
        elif y == HEADER_ROWS + body:
            segs = [Segment(" " * ADDR_W), Segment("└" + "─" * bar_w + "┘", frame)]
            segs.append(Segment("  log scale", Style(color=theme.FRAME, italic=True)))
        elif y == HEADER_ROWS + body + 1:
            hint = " ↑↓ select  ⏎ zoom in  ⌫ zoom out  ? help"
            segs = [Segment(hint[:width], Style(color=theme.FRAME, dim=True))]
        else:
            segs = self._body_line(y - HEADER_ROWS, bar_w, note_w)
        return Strip(segs).adjust_cell_length(width)

    def _title_line(self, width: int) -> Strip:
        crumbs = " › ".join(r.label for r in self._path)
        title = Style(color="#ffffff", bold=True)
        return Strip([Segment(" " + crumbs[: width - 1], title)]).adjust_cell_length(width)

    def _body_line(self, row: int, bar_w: int, note_w: int) -> list[Segment]:
        if row >= len(self._row_to_block):
            return [
                Segment(" " * ADDR_W),
                Segment("│" + " " * bar_w + "│", Style(color=theme.FRAME)),
            ]
        idx = self._row_to_block[row]
        block = self._blocks[idx]
        region = block.region
        i = row - block.top  # row inside the block
        last = i == block.height - 1
        selected = idx == self._selected
        hovered = idx == self._hover

        kind = region.dominant_kind()
        color = theme.kind_color(kind)
        if idx % 2:  # alternate shading so neighbours of the same kind stay distinct
            color = theme.shade(color, 0.82)
        if selected:
            color = theme.shade(color, 1.25)
        pulse, change = self._pulse_of(region, time.monotonic())
        if pulse > 0 and change is not None:
            tint = SHRINK_TINT if change.delta < 0 else GROW_TINT
            color = theme.blend(color, Color.parse(tint), 0.7 * pulse)

        addr_text = short_hex(region.start) if last else ""
        addr_style = Style(color=theme.ADDRESS, bold=selected)
        edge_style = Style(color=theme.SELECT_MARK if (selected or hovered) else theme.FRAME)
        left_edge, right_edge = "│", "│"
        if selected:
            left_edge, right_edge = "▶", "◀"
        elif hovered:
            left_edge, right_edge = "▷", "◁"

        if region.kind.is_gap:
            bar = self._gap_bar(region, i, block.height, bar_w, color, selected or hovered)
        else:
            bar = self._region_bar(region, i, block.height, bar_w, color, selected, hovered)

        segs = [
            Segment(addr_text.rjust(ADDR_W - 1) + " ", addr_style),
            Segment(left_edge, edge_style),
            *bar,
            Segment(right_edge, edge_style),
        ]
        if note_w > 1 and i == 0 and pulse > 0 and change is not None:
            if change.new:
                note, tint = " ✚ new", GROW_TINT
            elif change.delta > 0:
                note, tint = f" ▲ +{human_bytes(change.delta)}", GROW_TINT
            else:
                note, tint = f" ▼ -{human_bytes(-change.delta)}", SHRINK_TINT
            segs.append(Segment(note[:note_w], Style(color=tint, bold=True)))
        elif note_w > 1 and i == 0 and (not region.kind.is_gap or kind is Kind.NONCANONICAL):
            note = (" " + kind.value)[:note_w]
            segs.append(Segment(note, Style(color=theme.kind_color(kind), dim=not selected)))
        return segs

    def _region_bar(
        self, r: Region, i: int, h: int, w: int, bg: Color, selected: bool, hovered: bool
    ) -> list[Segment]:
        style = Style(bgcolor=bg, color=theme.text_on(bg), bold=selected, underline=hovered)
        name = ("≈ " if r.approximate else "") + r.label
        zoom = " »" if r.zoomable else ""
        size = human_bytes(r.size)
        if h == 1:
            text = _fit(" " + name, f" {size}{zoom} ", w)
        elif i == 0:
            text = _fit(" " + name, zoom + " ", w)
        elif i == 1:
            text = _fit(f" {size}", f"{r.perms} " if r.perms else "", w)
        else:
            text = " " * w
        return [Segment(text, style)]

    def _gap_bar(
        self, r: Region, i: int, h: int, w: int, color: Color, active: bool
    ) -> list[Segment]:
        fill = "╱" if r.kind is Kind.NONCANONICAL else "░"
        style = Style(color=theme.shade(color, 1.3 if active else 0.9), bold=active)
        if i == h // 2:
            label = r.label if r.label != r.kind.value else r.kind.value.lower()
            text = _center(f" {human_bytes(r.size)} {label} ", w, fill)
        else:
            text = fill * w
        return [Segment(text, style)]
