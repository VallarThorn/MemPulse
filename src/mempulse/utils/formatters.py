"""Human-readable formatting for addresses, sizes and permissions."""

from __future__ import annotations

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def human_bytes(n: int, precision: int = 1) -> str:
    """Format a byte count with binary units: 1536 -> '1.5 KiB'."""
    value = float(n)
    for unit in _UNITS:
        if abs(value) < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def hex_addr(addr: int, width: int = 16) -> str:
    """Zero-padded hex address: 0x00007ffef75f9000."""
    return f"0x{addr:0{width}x}"


def short_hex(addr: int) -> str:
    """Compact hex address that still groups digits for readability.

    64-bit addresses are split into 4-digit groups (0x7ffe_f75f_9000) because
    a run of 16 hex digits is nearly impossible to read at a glance.
    """
    digits = f"{addr:x}"
    groups = []
    while digits:
        groups.append(digits[-4:])
        digits = digits[:-4]
    return "0x" + "_".join(reversed(groups or ["0"]))


def describe_perms(perms: str) -> str:
    """Expand a /proc/<pid>/maps permission string ('r-xp') into plain words."""
    if len(perms) != 4:
        return perms
    words = []
    if perms[0] == "r":
        words.append("read")
    if perms[1] == "w":
        words.append("write")
    if perms[2] == "x":
        words.append("execute")
    access = ", ".join(words) if words else "no access (guard page)"
    sharing = "shared" if perms[3] == "s" else "private (copy-on-write)"
    return f"{access}; {sharing}"
