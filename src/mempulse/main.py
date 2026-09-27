"""MemPulse entry point."""

from __future__ import annotations

import argparse
import platform
import sys


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mempulse",
        description="Real-time terminal visualizer for Linux virtual and physical memory layout.",
    )
    parser.add_argument(
        "-p",
        "--pid",
        default="self",
        help="process whose virtual address space to show (default: MemPulse itself)",
    )
    parser.add_argument(
        "--view", choices=("virtual", "physical"), default="virtual", help="initial view"
    )
    parser.add_argument(
        "-i", "--interval", type=float, default=2.0, help="refresh interval in seconds"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    if platform.system() != "Linux":
        sys.exit("MemPulse reads /proc and /sys and only runs on Linux.")
    args = parse_args(argv)
    from mempulse.ui.app import MemPulseApp  # deferred: keeps --help fast

    MemPulseApp(pid=args.pid, view=args.view, interval=args.interval).run()


if __name__ == "__main__":
    main()
