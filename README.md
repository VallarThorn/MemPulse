# MemPulse

I was studying for Advance Operating Systems and did not really understand
memory layouts, so I built this tool in the weekend. Feel free to test it
out and give me some feedback.

A `btop`-style terminal visualizer for how Linux lays out memory: a zoomable
vertical "memory stick" of a process's **virtual** address space (user
mappings, the non-canonical hole, the kernel half) and of the machine's
**physical** address space (RAM, firmware regions, PCI/device MMIO).

```sh
uv run mempulse                 # virtual address space of MemPulse itself
uv run mempulse --pid 1234      # another process you own
uv run mempulse --view physical # the physical address bus
sudo "$(which uv)" run mempulse --view physical   # + PCI / device MMIO detail
```

Keys: `↑↓`/`jk` select · `Enter` zoom in · `Backspace`/`Esc` zoom out ·
`v`/`p` virtual/physical · `Tab` jump to the process table (`Enter` maps that
process) · `m` map MemPulse itself · `r` refresh · `?`/`h` help (legend,
concepts, keys) · `q` quit. Mouse: hover to inspect, click to select,
double-click to zoom.

Blocks **pulse** when a region changes between refreshes: green `▲ +size`
when it grows, red `▼` when it shrinks, `✚ new` when it appears. The
inspector keeps the last change for a minute. Panel borders turn amber or
red when their warning fires.

The right column shows:

- **Page events & swap**: sparklines of Disk Read Faults (`pgmajfault`), all
  page faults, Swap-In/Out, and a verdict that flags swap thrashing.
- **RAM & page cache health**: programs vs page cache vs free, and Dirty /
  Writeback gauges against the kernel's live flush (`┊`) and throttle limits
  (`nr_dirty_background_threshold` / `nr_dirty_threshold`).
- **Top memory consumers**: top 5 by resident memory. Other users' processes
  are dimmed: their memory maps need root.

## What is real and what is a template

| Shown | Source | Without root |
|---|---|---|
| User-space VMAs | `/proc/<pid>/maps` | real (own processes only) |
| Kernel half (direct map, vmalloc, vmemmap, modules...) | x86_64 layout from `Documentation/arch/x86/x86_64/mm.rst` | template, marked `≈`; KASLR shifts bases each boot |
| Kernel image sections | `/proc/kallsyms` | hidden (`kptr_restrict`) |
| Physical map with devices | `/proc/iomem` | addresses zeroed |
| Physical map, coarse | `/sys/firmware/memmap` + `/proc/zoneinfo` | real |

The stick uses a **log scale**: every region gets at least one row, and
neighbouring small regions are merged (`»` = zoomable) when they don't fit.

## Layout

```
src/mempulse/
├── main.py              CLI entry point
├── engine/
│   ├── collector.py     Collector base + counter→rate helper
│   ├── proc_parser.py   /proc & /sys parsers (pure functions over text)
│   └── vma_parser.py    Region model, kernel template, row layout
├── ui/
│   ├── app.py           Two-column dashboard and refresh timers
│   ├── memory_stick.py  The vertical address-space widget
│   ├── widgets.py       Sparklines, gauges, process table
│   ├── help.py          `?` overlay: legend, concepts, keys
│   └── theme.py         TrueColor palette
└── utils/formatters.py
```

Development: `uv run pytest`, `uv run ruff check .`, `uv run ruff format .`
