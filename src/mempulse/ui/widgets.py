"""Right-column dashboard panels: page events, cache health, top processes."""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.text import Text
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable

from mempulse.engine.proc_parser import PAGE_SIZE, ProcessInfo
from mempulse.utils.formatters import human_bytes

SPARK_CHARS = "▁▂▃▄▅▆▇█"
OK, WARN, BAD, DIM = "#66bb6a", "#ffb74d", "#ef5350", "#78909c"


def set_alert(widget: Widget, color: str) -> None:
    """Tint a panel's border via CSS classes when its verdict isn't OK."""
    widget.set_class(color == WARN, "-warn")
    widget.set_class(color == BAD, "-bad")


def sparkline(values: Iterable[float], width: int, floor: float = 1.0) -> str:
    """Last `width` values as block characters, scaled to the visible maximum.

    `floor` stops a quiet system (0, 0, 1, 0) from looking like a crisis by
    rendering tiny values as full-height bars.
    """
    data = list(values)[-width:] if width > 0 else []
    if not data:
        return " " * max(width, 0)
    top = max(max(data), floor)
    steps = len(SPARK_CHARS) - 1
    chars = [SPARK_CHARS[min(steps, round(v / top * steps))] for v in data]
    return "".join(chars).rjust(width)


def gauge(fraction: float, width: int, marker: float | None = None) -> list[tuple[str, str]]:
    """A horizontal bar as (text, style-role) runs. Roles: 'fill', 'empty', 'mark'."""
    width = max(width, 1)
    filled = min(round(max(fraction, 0.0) * width), width)
    mark_at = None if marker is None else min(int(marker * width), width - 1)
    runs = []
    for i in range(width):
        if i < filled:
            runs.append(("█", "fill"))
        elif i == mark_at:
            runs.append(("┊", "mark"))
        else:
            runs.append(("·", "empty"))
    return runs


def _gauge_text(fraction: float, width: int, color: str, marker: float | None = None) -> Text:
    styles = {"fill": color, "empty": "#424242", "mark": WARN}
    text = Text()
    for ch, role in gauge(fraction, width, marker):
        text.append(ch, styles[role])
    return text


# --------------------------------------------------------------------------
# Page events & swap
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    key: str  # /proc/vmstat counter
    label: str  # plain-English name
    color: str
    in_pages: bool  # counts pages (show as bytes/s) rather than events


PAGE_METRICS = [
    Metric("pgmajfault", "Disk Read Faults", "#ffb300", in_pages=False),
    Metric("pgfault", "All Page Faults", "#90a4ae", in_pages=False),
    Metric("pswpin", "Swap-In", "#42a5f5", in_pages=True),
    Metric("pswpout", "Swap-Out", "#ef5350", in_pages=True),
]


def _rate_text(metric: Metric, rate: float) -> str:
    if metric.in_pages:
        return human_bytes(int(rate * PAGE_SIZE)) + "/s"
    if rate >= 10_000:
        return f"{rate / 1000:.0f}k/s"
    return f"{rate:.1f}/s" if rate < 100 else f"{rate:.0f}/s"


def swap_verdict(history: dict[str, deque[float]], window: int = 5) -> tuple[str, str]:
    """Classify recent swap/fault activity. Returns (message, color)."""

    def avg(key: str) -> float:
        recent = list(history.get(key, ()))[-window:]
        return sum(recent) / len(recent) if recent else 0.0

    swap_in, swap_out, majfault = avg("pswpin"), avg("pswpout"), avg("pgmajfault")
    if swap_in > 50 and swap_out > 50:
        return "⚠ Thrashing: pages are swapped out and straight back in", BAD
    if swap_out > 50:
        return "▼ Memory pressure: evicting pages to swap", WARN
    if swap_in > 50:
        return "▲ Reading swapped pages back (recovering)", WARN
    if majfault > 200:
        return "● Heavy disk reads to fill page faults", WARN
    return "✓ Calm: no swap traffic", OK


class PageEventsPanel(Widget):
    """Sparklines of page fault and swap rates from /proc/vmstat."""

    DEFAULT_CSS = "PageEventsPanel { height: auto; }"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rates: dict[str, float] = {}
        self.history: dict[str, deque[float]] = {}
        self.swap_used = 0
        self.swap_total = 0
        self.verdict = ("", OK)

    def update_data(
        self,
        rates: dict[str, float],
        history: dict[str, deque[float]],
        swap_used: int,
        swap_total: int,
    ) -> None:
        self.rates, self.history = rates, history
        self.swap_used, self.swap_total = swap_used, swap_total
        self.verdict = swap_verdict(history)
        set_alert(self, self.verdict[1])
        self.refresh(layout=True)

    def render(self) -> RenderableType:
        width = self.content_size.width
        label_w, value_w = 17, 11
        show_raw = width >= 60  # room to show the kernel's own counter name too
        raw_w = 12 if show_raw else 0
        spark_w = max(width - label_w - value_w - raw_w - 2, 4)
        lines = []
        for m in PAGE_METRICS:
            line = Text(m.label.ljust(label_w), style="bold")
            if show_raw:
                line.append(m.key.ljust(raw_w), style=DIM)
            line.append(sparkline(self.history.get(m.key, ()), spark_w), style=m.color)
            line.append(" " + _rate_text(m, self.rates.get(m.key, 0.0)).rjust(value_w))
            lines.append(line)

        message, color = self.verdict
        swap = (
            f"Swap used {human_bytes(self.swap_used)} / {human_bytes(self.swap_total)}"
            if self.swap_total
            else "No swap configured"
        )
        lines.append(Text(swap, style=DIM))
        lines.append(Text(message, style=color))
        for line in lines:
            line.no_wrap, line.overflow = True, "ellipsis"
        return Group(*lines)


# --------------------------------------------------------------------------
# Page cache & dirty data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DirtyLimits:
    background: int  # bytes: flusher threads start writing back
    throttle: int  # bytes: processes writing files get slowed down


def dirty_limits(vmstat: dict[str, int], meminfo: dict[str, int]) -> DirtyLimits:
    """The kernel's live thresholds (vmstat exports them in pages).

    Fallback for old kernels: the default 10% / 20% of available memory.
    """
    if "nr_dirty_threshold" in vmstat:
        return DirtyLimits(
            vmstat["nr_dirty_background_threshold"] * PAGE_SIZE,
            vmstat["nr_dirty_threshold"] * PAGE_SIZE,
        )
    avail = meminfo.get("MemAvailable", 0)
    return DirtyLimits(avail // 10, avail // 5)


def dirty_verdict(dirty: int, limits: DirtyLimits) -> tuple[str, str]:
    if limits.throttle and dirty >= limits.throttle:
        return "⚠ Over the dirty limit: writing processes are being throttled", BAD
    if limits.background and dirty >= limits.background:
        return "⟳ Background flush running: dirty data is being written to disk", WARN
    if limits.background and dirty >= 0.5 * limits.background:
        return "● Dirty data building up; flush starts soon", WARN
    return "✓ Healthy: dirty data far below the flush threshold", OK


class CacheHealthPanel(Widget):
    """RAM breakdown plus Dirty / Writeback gauges against kernel thresholds."""

    DEFAULT_CSS = "CacheHealthPanel { height: auto; }"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.meminfo: dict[str, int] = {}
        self.limits = DirtyLimits(0, 0)

    def update_data(self, meminfo: dict[str, int], vmstat: dict[str, int]) -> None:
        self.meminfo = meminfo
        self.limits = dirty_limits(vmstat, meminfo)
        set_alert(self, dirty_verdict(meminfo.get("Dirty", 0), self.limits)[1])
        self.refresh(layout=True)

    def _ram_line(self, bar_w: int) -> Text:
        mi = self.meminfo
        total = mi.get("MemTotal", 0) or 1
        free = mi.get("MemFree", 0)
        cache = mi.get("Buffers", 0) + mi.get("Cached", 0) + mi.get("SReclaimable", 0)
        used = max(total - free - cache, 0)
        text = Text("RAM".ljust(11), style="bold")
        used_w = round(used / total * bar_w)
        cache_w = min(round(cache / total * bar_w), bar_w - used_w)
        text.append("█" * used_w, "#43a047")
        text.append("▒" * cache_w, "#ffb300")
        text.append("░" * (bar_w - used_w - cache_w), "#424242")
        text.append(f" {human_bytes(used)} used", "#43a047")
        return text

    def render(self) -> RenderableType:
        width = self.content_size.width
        value_w = 23  # " 2.8 MiB" + "   0.1% of max"
        bar_w = max(width - 11 - value_w, 8)
        mi = self.meminfo
        dirty, writeback = mi.get("Dirty", 0), mi.get("Writeback", 0)
        lim = self.limits
        scale = lim.throttle or 1
        marker = lim.background / scale if lim.throttle else None

        ram = self._ram_line(bar_w)
        legend = Text(" " * 11)
        legend.append("█ programs  ", "#43a047")
        legend.append("▒ page cache  ", "#ffb300")
        legend.append("░ free", DIM)
        legend.append(f"   {human_bytes(mi.get('MemAvailable', 0))} available", DIM)

        dirty_line = Text("Dirty".ljust(11), style="bold")
        dirty_line.append_text(_gauge_text(dirty / scale, bar_w, "#ffb74d", marker))
        dirty_line.append(f" {human_bytes(dirty)}".rjust(10))
        dirty_line.append(f" {dirty / scale:5.1%} of max", DIM)

        wb_line = Text("Writeback".ljust(11), style="bold")
        wb_line.append_text(_gauge_text(writeback / scale, bar_w, "#4fc3f7"))
        wb_line.append(f" {human_bytes(writeback)}".rjust(10))

        limits = Text(" " * 11)
        limits.append("┊", WARN)
        limits.append(f" flush starts at {human_bytes(lim.background)}", DIM)
        limits.append(f" · throttle at {human_bytes(lim.throttle)}", DIM)

        message, color = dirty_verdict(dirty, lim)
        lines = [ram, legend, dirty_line, wb_line, limits, Text(message, style=color)]
        for line in lines:
            line.no_wrap, line.overflow = True, "ellipsis"
        return Group(*lines)


# --------------------------------------------------------------------------
# Top memory consumers
# --------------------------------------------------------------------------


class ProcessTable(DataTable):
    """Top processes by resident memory. Enter on a row maps that process."""

    class Chosen(Message):
        def __init__(self, pid: int, name: str) -> None:
            super().__init__()
            self.pid = pid
            self.name = name

    def __init__(self, **kwargs) -> None:
        super().__init__(cursor_type="row", zebra_stripes=True, **kwargs)
        self._names: dict[int, str] = {}
        self.viewing: int | None = None  # pid shown in the memory stick

    def on_mount(self) -> None:
        self.add_column("PID", key="pid")
        self.add_column("Process", key="name")
        self.add_column(Text("Resident (RSS)", justify="right"), key="rss")
        self.add_column(Text("Virtual (VMS)", justify="right"), key="vms")
        self.add_column(Text("% RAM", justify="right"), key="pct")

    def update_processes(self, procs: list[ProcessInfo], mem_total: int) -> None:
        cursor_pid = self._pid_at_cursor()
        self.clear()
        self._names = {}
        my_uid = os.getuid()
        for p in procs:
            self._names[p.pid] = p.name
            # Other users' address spaces can't be mapped without root: dim them.
            readable = my_uid == 0 or p.uid == my_uid
            style = "" if readable else DIM
            name = ("● " if p.pid == self.viewing else "  ") + p.name
            self.add_row(
                Text(str(p.pid), style=style),
                Text(name, style=f"bold {style}" if p.pid == self.viewing else style),
                Text(human_bytes(p.rss), style=style, justify="right"),
                Text(human_bytes(p.vms), style=style, justify="right"),
                Text(f"{p.rss / (mem_total or 1):.1%}", style=style, justify="right"),
                key=str(p.pid),
            )
        if cursor_pid is not None and str(cursor_pid) in self.rows:
            self.move_cursor(row=self.get_row_index(str(cursor_pid)), animate=False)

    def _pid_at_cursor(self) -> int | None:
        if not self.row_count:
            return None
        try:
            key = self.coordinate_to_cell_key(self.cursor_coordinate).row_key
        except Exception:  # cursor outside table after a shrink
            return None
        return int(key.value) if key.value else None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        pid = int(event.row_key.value or 0)
        self.post_message(self.Chosen(pid, self._names.get(pid, str(pid))))
