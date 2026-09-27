"""MemPulse app shell.

Left: the Memory Stick. Right: region inspector, page-event sparklines,
RAM / dirty-page gauges, and the top memory consumers.
"""

from __future__ import annotations

import os
import time

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Static

from mempulse.engine import proc_parser as pp
from mempulse.engine.collector import FuncCollector, RateCollector
from mempulse.engine.vma_parser import (
    KIND_INFO,
    KernelConfig,
    Kind,
    Region,
    build_physical_space,
    build_virtual_space,
)
from mempulse.ui import theme
from mempulse.ui.help import HelpScreen
from mempulse.ui.memory_stick import GROW_TINT, SHRINK_TINT, Change, MemoryStick
from mempulse.ui.widgets import PAGE_METRICS, CacheHealthPanel, PageEventsPanel, ProcessTable
from mempulse.utils.formatters import describe_perms, hex_addr, human_bytes


def _describe_change(change: Change, now: float) -> Text:
    age = max(now - change.at, 0)
    when = f"{age:.0f} s ago"
    if change.new:
        return Text(f"✚ appeared {when}", style=GROW_TINT)
    if change.delta > 0:
        return Text(f"▲ grew by {human_bytes(change.delta)} {when}", style=GROW_TINT)
    return Text(f"▼ shrank by {human_bytes(-change.delta)} {when}", style=SHRINK_TINT)


def _largest_inside(region: Region, n: int = 6) -> Text:
    named = sorted(
        (c for c in region.children if not c.kind.is_gap), key=lambda c: c.size, reverse=True
    )
    text = Text()
    for c in named[:n]:
        text.append("■ ", style=theme.KIND_COLORS.get(c.dominant_kind(), "#9e9e9e"))
        text.append(f"{c.label}  ")
        text.append(human_bytes(c.size) + "\n", style="#90a4ae")
    if len(named) > n:
        text.append(f"… and {len(named) - n} more", style="#90a4ae")
    text.rstrip()  # in place; returns None
    return text if named else Text("nothing mapped", style="#90a4ae")


def render_inspector(region: Region, change: Change | None = None) -> Group:
    kind = region.dominant_kind()
    color = theme.KIND_COLORS.get(kind, "#9e9e9e")
    title = Text(
        f" {region.label} ", style=f"bold {theme.text_on(theme.kind_color(kind))} on {color}"
    )

    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold #b0bec5", no_wrap=True)
    t.add_column()
    # Text() everywhere: labels like "[vdso]" would otherwise be eaten as Rich markup.
    if region.synthetic:
        t.add_row("Type", Text(f"Merged group, mostly {kind.value}", style=color))
    else:
        t.add_row("Type", Text(kind.value, style=color))
    t.add_row("Start", Text(hex_addr(region.start)))
    t.add_row("End", Text(hex_addr(region.end - 1) + "  (inclusive)"))
    t.add_row("Size", Text(f"{human_bytes(region.size, 2)}  ({region.size:,} bytes)"))
    if region.perms:
        perms = region.perms
        if len(perms) == 4:
            perms = f"{perms}  -  {describe_perms(perms)}"
        t.add_row("Permissions", Text(perms))
    if region.detail and region.detail != region.label:
        t.add_row("Detail", Text(region.detail))
    if change is not None:
        t.add_row("Last change", _describe_change(change, time.monotonic()))
    if region.zoomable:
        t.add_row("Contains", Text(f"{len(region.children)} sub-regions - press Enter to zoom in"))
        t.add_row("Largest", _largest_inside(region))

    parts: list = [title, Text(), t, Text()]
    if region.approximate:
        parts.append(
            Text(
                "≈ Boundaries come from the documented kernel layout, not the live "
                "system. The kernel hides real kernel addresses from non-root users.",
                style="italic #ffb74d",
            )
        )
        parts.append(Text())
    info = KIND_INFO[Kind.GROUP] if region.synthetic else KIND_INFO.get(kind, "")
    parts.append(Text(info, style="#cfd8dc"))
    return Group(*parts)


class MemPulseApp(App):
    TITLE = "MemPulse"
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #right { width: 1fr; }
    #right > * { border: round #555555; padding: 0 1; }
    #inspector-wrap { height: 1fr; min-height: 8; }
    #procs { height: 9; padding: 0; }
    #procs:focus { border: round #8a8a8a; }
    #right > .-warn { border: round #ffb74d; }
    #right > .-bad { border: round #ef5350; }
    #notice { height: auto; color: #ffb74d; padding: 0 1; }
    """
    BINDINGS = [
        Binding("v", "show('virtual')", "Virtual view"),
        Binding("p", "show('physical')", "Physical view"),
        Binding("m", "map_pid('self')", "Map MemPulse"),
        Binding("r", "refresh_data", "Refresh"),
        Binding("question_mark,h", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, pid: int | str = "self", view: str = "virtual", interval: float = 2.0):
        super().__init__()
        self.pid = pid
        self.view_name = view
        self.interval = interval
        self.kernel_cfg = KernelConfig.detect()
        self.vmstat = FuncCollector(pp.read_vmstat)
        self.meminfo = FuncCollector(pp.read_meminfo)
        self.rates = RateCollector(self.vmstat, [m.key for m in PAGE_METRICS])
        self.processes = FuncCollector(lambda: pp.top_processes(5))
        # Held directly (not queried per tick) so a timer firing during shutdown is harmless.
        self.events_panel = PageEventsPanel(id="events")
        self.cache_panel = CacheHealthPanel(id="cache")
        self.process_table = ProcessTable(id="procs")

    def compose(self) -> ComposeResult:
        yield Static(id="notice")
        with Horizontal(id="main"):
            yield MemoryStick(id="stick")
            with Vertical(id="right"):
                with VerticalScroll(id="inspector-wrap", can_focus=False):
                    yield Static(id="inspector")
                yield self.events_panel
                yield self.cache_panel
                yield self.process_table
        yield Footer()

    def on_mount(self) -> None:
        titles = {
            "#inspector-wrap": "Region inspector",
            "#events": "Page events & swap",
            "#cache": "RAM & page cache health",
            "#procs": "Top memory consumers · Enter = map process",
        }
        for selector, title in titles.items():
            self.query_one(selector).border_title = title
        self.process_table.viewing = os.getpid() if self.pid == "self" else int(self.pid)
        self.action_refresh_data()
        self.tick_metrics()
        self.tick_processes()
        self.query_one(MemoryStick).focus()
        self.set_interval(self.interval, self.action_refresh_data)
        self.set_interval(1.0, self.tick_metrics)
        self.set_interval(2.0, self.tick_processes)

    # ------------------------------------------------------------ live panels

    def tick_metrics(self) -> None:
        rates = self.rates.collect() or {}
        meminfo = self.meminfo.collect() or {}
        vmstat = self.vmstat.latest or {}
        swap_total = meminfo.get("SwapTotal", 0)
        swap_used = swap_total - meminfo.get("SwapFree", 0)
        self.events_panel.update_data(rates, self.rates.history, swap_used, swap_total)
        self.cache_panel.update_data(meminfo, vmstat)

    def tick_processes(self) -> None:
        procs = self.processes.collect()
        if procs is not None:
            mem_total = (self.meminfo.latest or {}).get("MemTotal", 0)
            self.process_table.update_processes(procs, mem_total)

    # ------------------------------------------------------------ memory stick

    def _build(self) -> tuple[Region, str]:
        if self.view_name == "physical":
            root, privileged = build_physical_space()
            notice = (
                ""
                if privileged
                else "Not root: /proc/iomem addresses are hidden, so PCI and device MMIO "
                "windows can't be shown. Using the firmware memory map. "
                "Run with sudo for the full hardware map."
            )
            return root, notice
        root = build_virtual_space(self.pid, self.kernel_cfg)
        notice = (
            ""
            if self.kernel_cfg.symbols
            else "Kernel addresses are hidden (kptr_restrict), so kernel regions show the "
            "documented x86_64 layout (≈). Run with sudo to see the real kernel image."
        )
        return root, notice

    def _set_notice(self, text: str) -> None:
        notice = self.query_one("#notice", Static)
        notice.update(text)
        notice.display = bool(text)

    def action_refresh_data(self, keep_zoom: bool = True) -> bool:
        try:
            root, notice = self._build()
        except OSError as exc:
            self._set_notice(f"Cannot read memory map: {exc}")
            return False
        self._set_notice(notice)
        self.query_one(MemoryStick).set_root(root, keep_zoom=keep_zoom)
        return True

    def action_show(self, view: str) -> None:
        if view != self.view_name:
            self.view_name = view
            self.action_refresh_data(keep_zoom=False)

    def action_map_pid(self, pid: int | str) -> None:
        """Point the memory stick at another process (and the virtual view)."""
        previous = (self.pid, self.view_name)
        self.pid, self.view_name = pid, "virtual"
        if not self.action_refresh_data(keep_zoom=False):
            # Keep showing the old process rather than an empty map.
            self.pid, self.view_name = previous
            self.action_refresh_data(keep_zoom=False)
            self._set_notice(
                f"Can't read the memory map of pid {pid}: it belongs to another user. "
                "Run MemPulse with sudo to inspect any process."
            )
            return
        self.process_table.viewing = os.getpid() if pid == "self" else int(pid)
        self.tick_processes()
        self.query_one(MemoryStick).focus()

    def on_process_table_chosen(self, message: ProcessTable.Chosen) -> None:
        self.action_map_pid(message.pid)

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def on_memory_stick_highlighted(self, message: MemoryStick.Highlighted) -> None:
        change = self.query_one(MemoryStick).last_change(message.region)
        self.query_one("#inspector", Static).update(render_inspector(message.region, change))
