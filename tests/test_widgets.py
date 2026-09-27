from collections import deque

from mempulse.engine import proc_parser as pp
from mempulse.engine.collector import FuncCollector, RateCollector
from mempulse.ui.widgets import (
    DirtyLimits,
    dirty_limits,
    dirty_verdict,
    gauge,
    sparkline,
    swap_verdict,
)

PAGE = pp.PAGE_SIZE


def test_sparkline_scales_and_pads():
    assert sparkline([0, 4, 8], 3, floor=1) == "▁▅█"
    assert sparkline([1, 2], 5) == "   ▅█"
    # below the floor, small blips stay small instead of filling the bar
    assert sparkline([0, 0.1], 2, floor=10) == "▁▁"
    assert sparkline([], 3) == "   "


def test_gauge_marker_and_fill():
    runs = gauge(0.5, 10, marker=0.8)
    text = "".join(ch for ch, _ in runs)
    assert text == "█████···┊·"
    assert "".join(ch for ch, _ in gauge(2.0, 4)) == "████"  # clamped


def test_dirty_limits_prefers_kernel_thresholds():
    vm = {"nr_dirty_threshold": 200, "nr_dirty_background_threshold": 100}
    assert dirty_limits(vm, {}) == DirtyLimits(100 * PAGE, 200 * PAGE)
    assert dirty_limits({}, {"MemAvailable": 1000}) == DirtyLimits(100, 200)


def test_dirty_verdict_levels():
    lim = DirtyLimits(100, 200)
    assert dirty_verdict(10, lim)[0].startswith("✓")
    assert dirty_verdict(60, lim)[0].startswith("●")
    assert dirty_verdict(150, lim)[0].startswith("⟳")
    assert dirty_verdict(250, lim)[0].startswith("⚠")


def test_swap_verdict_detects_thrashing():
    calm = {"pswpin": deque([0] * 5), "pswpout": deque([0] * 5)}
    assert swap_verdict(calm)[0].startswith("✓")
    thrash = {"pswpin": deque([500] * 5), "pswpout": deque([500] * 5)}
    assert "Thrashing" in swap_verdict(thrash)[0]
    evict = {"pswpin": deque([0] * 5), "pswpout": deque([500] * 5)}
    assert "pressure" in swap_verdict(evict)[0]


def test_rate_collector_computes_per_second_rates():
    samples = iter([{"pgmajfault": 100}, {"pgmajfault": 150}, {"pgmajfault": 140}])
    rates = RateCollector(FuncCollector(lambda: next(samples)), ["pgmajfault"])
    assert rates.collect() == {}  # first sample has nothing to diff against
    assert rates.collect()["pgmajfault"] > 0
    assert rates.collect()["pgmajfault"] == 0  # counter went backwards: clamp
    assert len(rates.history["pgmajfault"]) == 2


def test_collector_keeps_last_good_sample_on_error():
    values = iter([{"a": 1}])

    def reader():
        try:
            return next(values)
        except StopIteration:
            raise OSError("gone") from None

    c = FuncCollector(reader)
    assert c.collect() == {"a": 1}
    assert c.collect() == {"a": 1}
    assert c.error and "gone" in c.error


def test_parse_statm():
    assert pp.parse_statm("100 25 10 1 0 50 0\n", page_size=4096) == (409600, 102400)
