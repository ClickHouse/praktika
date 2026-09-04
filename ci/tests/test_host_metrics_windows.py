"""The host-metrics timeline uses a growing reporting window so short jobs
render a curve from ~1s instead of a single point at the first full window."""
from praktika.host_metrics import HostMetricsCollector


def _flush_times(fine, report, job_len):
    """Simulate the sampler loop's window flushes and return each point's t."""
    first_window = min(report, max(fine, 1.0))
    window_start = 0.0
    window_target = first_window
    flushes = []
    now = 0.0
    while now < job_len:
        now = round(now + fine, 4)
        if now - window_start >= window_target:
            flushes.append(round(now, 1))
            window_start = now
            window_target = HostMetricsCollector._next_window_target(
                now, first_window, report
            )
    return flushes


def test_short_job_gets_a_curve_from_one_second():
    # A ~5s job used to yield a single point at the first full (5s) window; now
    # it emits ~1s-spaced points starting at 1s.
    flushes = _flush_times(fine=0.5, report=5.0, job_len=5.0)
    assert flushes[0] <= 1.0
    assert flushes[:5] == [1.0, 2.0, 3.0, 4.0, 5.0]


def test_window_grows_and_caps_at_report_interval():
    # Windows ramp up and never exceed report_interval on a long run.
    flushes = _flush_times(fine=0.5, report=5.0, job_len=90.0)
    gaps = [round(b - a, 1) for a, b in zip(flushes, flushes[1:])]
    assert max(gaps) <= 5.0  # never exceeds the reporting interval
    assert gaps[0] == 1.0  # dense at the start
    assert gaps[-1] == 5.0  # settled to full windows by the end


def test_next_window_target_bounds():
    # Floored at first_window, capped at report_interval.
    assert HostMetricsCollector._next_window_target(0.0, 1.0, 5.0) == 1.0
    assert HostMetricsCollector._next_window_target(8.0, 1.0, 5.0) == 2.0  # 8*0.25
    assert HostMetricsCollector._next_window_target(100.0, 1.0, 5.0) == 5.0  # capped
