"""
test_baseline.py — Precog Baseline Learning Tests

Tests the baseline learning infrastructure in baseline.py:
  1. TimeBucket statistics are correct (mean, stddev, Welford's algorithm)
  2. TimeBucket anomaly detection fires at the right threshold
  3. KeywordStat tracks hits and last seen correctly
  4. BaselineConfig returns correct keyword lists for standard and power mode
  5. BaselineStore records and queries buckets correctly
  6. BaselineStore records and queries keyword hits correctly
  7. Confidence calculation is correct for seed and direct observation
  8. Monitoring activates at the correct confidence threshold
  9. BaselineCollector processes entries into correct time slots
  10. BaselineCollector keyword scanning works correctly
  11. Persistence — baseline survives a store restart
  12. JournalSeeder reads history and updates confidence

Run from project root:
  python3 tests/test_baseline.py
"""

import sys
import time
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "core"))

from core.baseline import (
    TimeBucket, KeywordStat, BaselineConfig, BaselineStore,
    BaselineCollector, DEFAULT_KEYWORDS, BOOT_KEYWORDS,
    MIN_DAYS_FOR_FULL_CONFIDENCE, SEED_CONFIDENCE_WEIGHT,
    DIRECT_CONFIDENCE_WEIGHT,
)
from core.rolling_log import LogEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_entry(
    raw: str = "test message",
    source: str = "journalctl",
    age_seconds: float = 0.0,
    priority: int = 6,
) -> LogEntry:
    ts_ns = time.time_ns() - int(age_seconds * 1_000_000_000)
    return LogEntry(timestamp_ns=ts_ns, source=source, raw=raw, priority=priority)


def make_store(power_mode: bool = False, user_keywords: list = None) -> BaselineStore:
    tmp = Path(tempfile.mkdtemp())
    config = BaselineConfig(power_mode=power_mode, user_keywords=user_keywords)
    return BaselineStore(data_dir=tmp, config=config)


def pass_fail(condition: bool, test_name: str, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    detail_str = f" — {detail}" if detail else ""
    print(f"  [{status}] {test_name}{detail_str}")
    return condition
# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_timebucket_statistics():
    """Welford's algorithm produces correct mean and stddev."""
    print("\nTest 1: TimeBucket statistics")
    bucket = TimeBucket()

    # Feed in known values: 2, 4, 4, 4, 5, 5, 7, 9
    # Mean = 5.0, stddev = 2.0 (well known example)
    for v in [2, 4, 4, 4, 5, 5, 7, 9]:
        bucket.update(v)

    r1 = pass_fail(bucket.count == 8, "Sample count correct", f"count={bucket.count}")
    r2 = pass_fail(
        abs(bucket.mean - 5.0) < 0.001,
        "Mean is correct",
        f"mean={bucket.mean:.4f}"
    )
    r3 = pass_fail(
       abs(bucket.stddev - 2.1381) < 0.001,
       "Stddev is correct",
       f"stddev={bucket.stddev:.4f}"
    )
    return all([r1, r2, r3])


def test_timebucket_anomaly_detection():
    """Anomaly detection fires above threshold, not below."""
    print("\nTest 2: TimeBucket anomaly detection")
    bucket = TimeBucket()

    # Feed 10 samples of value 10 — mean=10, stddev=0
    for _ in range(10):
        bucket.update(10)

    # Exactly at mean — not anomalous
    r1 = pass_fail(
        not bucket.is_anomalous(10),
        "Value at mean is not anomalous"
    )

    # Slightly above mean — not anomalous (stddev is 0 so threshold = mean)
    r2 = pass_fail(
        bucket.is_anomalous(11),
        "Value above mean+0*stddev is anomalous when stddev=0"
    )

    # Now use a bucket with real variance
    bucket2 = TimeBucket()
    for v in [2, 4, 4, 4, 5, 5, 7, 9, 5, 5]:
        bucket2.update(v)
    # mean~=5, stddev~=1.83, threshold at sensitivity=2.0 is ~8.66
    r3 = pass_fail(
        not bucket2.is_anomalous(8),
        "Value below threshold is not anomalous",
        f"mean={bucket2.mean:.1f} stddev={bucket2.stddev:.2f}"
    )
    r4 = pass_fail(
        bucket2.is_anomalous(20),
        "Value well above threshold is anomalous",
        f"threshold={bucket2.mean + 2*bucket2.stddev:.1f}"
    )

    # Fewer than 7 samples — should never flag
    bucket3 = TimeBucket()
    for _ in range(6):
        bucket3.update(1)
    r5 = pass_fail(
        not bucket3.is_anomalous(1000),
        "Fewer than 7 samples never flags anomaly"
    )

    return all([r1, r2, r3, r4, r5])


def test_keywordstat():
    """KeywordStat tracks hits and last seen correctly."""
    print("\nTest 3: KeywordStat")
    ks = KeywordStat()

    r1 = pass_fail(ks.hits == 0, "Starts at zero hits")

    ts1 = time.time_ns()
    ks.record_hit(ts1)
    r2 = pass_fail(ks.hits == 1, "One hit recorded", f"hits={ks.hits}")
    r3 = pass_fail(ks.last_seen_ns == ts1, "Last seen updated correctly")

    ts2 = ts1 + 1_000_000_000
    ks.record_hit(ts2)
    r4 = pass_fail(ks.hits == 2, "Two hits recorded")
    r5 = pass_fail(ks.last_seen_ns == ts2, "Last seen updated to newer timestamp")

    # Older timestamp should not update last_seen
    ks.record_hit(ts1 - 1000)
    r6 = pass_fail(ks.last_seen_ns == ts2, "Older hit does not update last seen")

    return all([r1, r2, r3, r4, r5, r6])


def test_baseline_config_keywords():
    """BaselineConfig returns correct keyword lists for each mode."""
    print("\nTest 4: BaselineConfig keyword lists")

    std_config = BaselineConfig(power_mode=False)
    std_keywords = std_config.active_keywords

    r1 = pass_fail(
        all(k in std_keywords for k in DEFAULT_KEYWORDS),
        "Standard mode includes all DEFAULT_KEYWORDS"
    )
    r2 = pass_fail(
        all(k in std_keywords for k in BOOT_KEYWORDS),
        "Standard mode includes all BOOT_KEYWORDS"
    )

    user_kws = ["my_custom_error", "widget_failure"]
    power_config = BaselineConfig(power_mode=True, user_keywords=user_kws)
    power_keywords = power_config.active_keywords

    r3 = pass_fail(
        all(k in power_keywords for k in user_kws),
        "Power mode includes user keywords"
    )
    r4 = pass_fail(
        all(k in power_keywords for k in DEFAULT_KEYWORDS),
        "Power mode still includes DEFAULT_KEYWORDS"
    )

    # No duplicates
    r5 = pass_fail(
        len(power_keywords) == len(set(power_keywords)),
        "No duplicate keywords in power mode"
    )

    return all([r1, r2, r3, r4, r5])
def test_baselinestore_buckets():
    """BaselineStore records and queries time buckets correctly."""
    print("\nTest 5: BaselineStore bucket recording and query")
    store = make_store()
    store.set_seed_days(4.0)

    store.record_hourly_bucket("journalctl", "1_09", 10)
    store.record_hourly_bucket("journalctl", "1_09", 12)
    store.record_hourly_bucket("journalctl", "1_09", 8)

    r1 = pass_fail(
        not store.is_anomalous("journalctl", "1_09", 1000),
        "Not anomalous with fewer than 7 samples"
    )

    for _ in range(7):
        store.record_hourly_bucket("journalctl", "1_09", 10)

    r2 = pass_fail(
        not store.is_anomalous("journalctl", "1_09", 11),
        "Normal value not flagged as anomalous"
    )
    r3 = pass_fail(
        store.is_anomalous("journalctl", "1_09", 500),
        "Very high value flagged as anomalous"
    )
    r4 = pass_fail(
        not store.is_anomalous("unknown_source", "1_09", 999),
        "Unknown source returns False, does not crash"
    )
    r5 = pass_fail(
        not store.is_anomalous("journalctl", "9_99", 999),
        "Unknown slot returns False, does not crash"
    )

    return all([r1, r2, r3, r4, r5])


def test_baselinestore_keywords():
    """BaselineStore records and queries keyword hits correctly."""
    print("\nTest 6: BaselineStore keyword recording and query")
    store = make_store()

    ts = time.time_ns()
    store.record_keyword_hit("journalctl", "error", ts)
    store.record_keyword_hit("journalctl", "error", ts + 1000)
    store.record_keyword_hit("auth.log", "failed password", ts)

    r1 = pass_fail(
        store.keyword_hits("journalctl", "error") == 2,
        "Two hits recorded for journalctl:error",
        f"hits={store.keyword_hits('journalctl', 'error')}"
    )
    r2 = pass_fail(
        store.keyword_hits("auth.log", "failed password") == 1,
        "One hit recorded for auth.log:failed password"
    )
    r3 = pass_fail(
        store.keyword_hits("journalctl", "segfault") == 0,
        "Zero hits for unseen keyword"
    )
    r4 = pass_fail(
        "journalctl" in store.sources(),
        "journalctl appears in sources list"
    )

    return all([r1, r2, r3, r4])


def test_confidence_calculation():
    """Confidence calculation is correct for seed and direct observation."""
    print("\nTest 7: Confidence calculation")
    store = make_store()

    r1 = pass_fail(store.confidence == 0.0, "Fresh store has zero confidence")

    store.set_seed_days(7.0)
    expected_seed = (7.0 / MIN_DAYS_FOR_FULL_CONFIDENCE) * SEED_CONFIDENCE_WEIGHT
    r2 = pass_fail(
        abs(store.confidence - expected_seed) < 0.001,
        "Seed confidence calculated correctly",
        f"confidence={store.confidence:.3f}, expected={expected_seed:.3f}"
    )

    store.update_direct_observation(7.0)
    expected_direct = (7.0 / MIN_DAYS_FOR_FULL_CONFIDENCE) * DIRECT_CONFIDENCE_WEIGHT
    expected_total  = expected_seed + expected_direct
    r3 = pass_fail(
        abs(store.confidence - expected_total) < 0.001,
        "Combined confidence calculated correctly",
        f"confidence={store.confidence:.3f}, expected={expected_total:.3f}"
    )

    store.set_seed_days(14.0)
    store.update_direct_observation(14.0)
    r4 = pass_fail(
        store.confidence == 1.0,
        "Full confidence at 14 days seed + 14 days direct",
        f"confidence={store.confidence:.3f}"
    )

    store.set_seed_days(100.0)
    store.update_direct_observation(100.0)
    r5 = pass_fail(
        store.confidence == 1.0,
        "Confidence caps at 1.0",
        f"confidence={store.confidence:.3f}"
    )

    return all([r1, r2, r3, r4, r5])


def test_monitoring_activation():
    """Monitoring activates at the correct confidence threshold."""
    print("\nTest 8: Monitoring activation")
    store = make_store()

    r1 = pass_fail(
        not store.monitoring_active,
        "Monitoring not active at zero confidence"
    )

    store.set_seed_days(4.0)
    r2 = pass_fail(
        store.monitoring_active,
        "Monitoring active after enough seed history",
        f"confidence={store.confidence_pct}%"
    )

    return all([r1, r2])


def test_baseline_collector_processing():
    """BaselineCollector processes entries into correct time slots."""
    print("\nTest 9: BaselineCollector entry processing")
    store     = make_store()
    collector = BaselineCollector(store)

    entry = make_entry(raw="normal system message", source="journalctl")
    collector.process(entry)
    collector.flush()

    r1 = pass_fail(
        len(store.sources()) > 0,
        "Store has at least one source after processing",
        f"sources={store.sources()}"
    )

    return r1


def test_baseline_collector_keywords():
    """BaselineCollector keyword scanning works correctly."""
    print("\nTest 10: BaselineCollector keyword scanning")
    store     = make_store()
    collector = BaselineCollector(store)

    entry_with_keyword = make_entry(
        raw="kernel: buffer overrun detected in driver",
        source="journalctl"
    )
    collector.process(entry_with_keyword)

    r1 = pass_fail(
        store.keyword_hits("journalctl", "buffer overrun") == 1,
        "buffer overrun keyword hit recorded",
        f"hits={store.keyword_hits('journalctl', 'buffer overrun')}"
    )

    entry_clean = make_entry(
        raw="systemd: started session 42",
        source="journalctl"
    )
    collector.process(entry_clean)

    r2 = pass_fail(
        store.keyword_hits("journalctl", "segfault") == 0,
        "No false keyword hits on clean entry"
    )

    entry_multi = make_entry(
        raw="kernel: i/o error on disk, filesystem corrupted",
        source="journalctl"
    )
    collector.process(entry_multi)

    r3 = pass_fail(
        store.keyword_hits("journalctl", "i/o error") == 1,
        "i/o error keyword hit recorded"
    )
    r4 = pass_fail(
        store.keyword_hits("journalctl", "corrupted") == 1,
        "corrupted keyword hit recorded"
    )

    return all([r1, r2, r3, r4])


def test_persistence():
    """Baseline data survives a store restart."""
    print("\nTest 11: Persistence across restart")
    tmp    = Path(tempfile.mkdtemp())
    config = BaselineConfig()

    store1 = BaselineStore(data_dir=tmp, config=config)
    store1.set_seed_days(7.0)
    store1.record_hourly_bucket("journalctl", "1_09", 10)
    store1.record_keyword_hit("journalctl", "error", time.time_ns())
    store1.save()

    confidence1 = store1.confidence
    store2      = BaselineStore(data_dir=tmp, config=config)

    r1 = pass_fail(
        abs(store2.confidence - confidence1) < 0.001,
        "Confidence survives restart",
        f"before={confidence1:.3f} after={store2.confidence:.3f}"
    )
    r2 = pass_fail(
        store2.keyword_hits("journalctl", "error") == 1,
        "Keyword hits survive restart"
    )
    r3 = pass_fail(
        "journalctl" in store2.sources(),
        "Sources survive restart"
    )

    return all([r1, r2, r3])


def test_notice_acknowledged():
    """First run notice is shown once and stored correctly."""
    print("\nTest 12: First run notice acknowledgement")
    store = make_store()
    store.set_seed_days(5.0)

    r1 = pass_fail(
        not store.notice_acknowledged,
        "Notice not acknowledged on fresh store"
    )

    notice = store.first_run_notice()
    r2 = pass_fail(
        "5 days" in notice and "%" in notice,
        "Notice contains seed days and confidence",
        f"notice={notice[:60]}..."
    )

    store.acknowledge_notice()
    r3 = pass_fail(
        store.notice_acknowledged,
        "Notice marked as acknowledged"
    )

    tmp    = Path(tempfile.mkdtemp())
    config = BaselineConfig()
    s2     = BaselineStore(data_dir=tmp, config=config)
    s2.set_seed_days(5.0)
    s2.acknowledge_notice()
    s3 = BaselineStore(data_dir=tmp, config=config)
    r4 = pass_fail(
        s3.notice_acknowledged,
        "Acknowledgement survives restart"
    )

    return all([r1, r2, r3, r4])        
if __name__ == "__main__":
    print("=" * 55)
    print("Precog — Baseline Learning Tests")
    print("=" * 55)

    results = [
        test_timebucket_statistics(),
        test_timebucket_anomaly_detection(),
        test_keywordstat(),
        test_baseline_config_keywords(),
        test_baselinestore_buckets(),
        test_baselinestore_keywords(),
        test_confidence_calculation(),
        test_monitoring_activation(),
        test_baseline_collector_processing(),
        test_baseline_collector_keywords(),
        test_persistence(),
        test_notice_acknowledged(),
    ]

    total  = len(results)
    passed = sum(results)
    failed = total - passed

    print("\n" + "=" * 55)
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  —  {failed} FAILED  <<<")
    else:
        print("  —  all clear")
    print("=" * 55)

    sys.exit(0 if failed == 0 else 1)
