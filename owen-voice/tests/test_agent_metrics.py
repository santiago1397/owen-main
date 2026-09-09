"""Per-call agent metrics — the numbers that make "is it getting better?" answerable.

Every tuning decision on this stack so far has been argued from synthesis samples and from
someone's memory of how a call sounded. These are the objective counterpart, and they are
worth testing because a metric that is quietly wrong is worse than none: it would be used to
justify a change.

Run:  python -m tests.test_agent_metrics      (from owen-voice/)
"""

import sys

sys.path.insert(0, ".")

from app.session import MediaSession  # noqa: E402

_checks = 0
_failures = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


def sess(**kw) -> MediaSession:
    s = MediaSession(session_uuid="t")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_percentiles_describe_the_distribution_not_the_average():
    print("\ntest_percentiles_describe_the_distribution_not_the_average")
    # A mean would report ~1500ms and hide the 4s turn entirely. The 4s turn is the one the
    # caller remembers, so p95 and max have to surface it.
    s = sess(turn_metrics=[{"first_audio_ms": v} for v in (900, 1000, 1100, 1200, 4000)])
    m = s.agent_metrics()
    check(m["turns"] == 5, "every turn is counted")
    check(m["first_audio_ms_p50"] == 1100, f"p50 is the median ({m['first_audio_ms_p50']})")
    check(m["first_audio_ms_max"] == 4000, "the worst turn is visible")
    check(m["first_audio_ms_p95"] >= 1200, "p95 leans toward the tail, not the middle")


def test_an_empty_call_reports_zeroes_not_a_crash():
    print("\ntest_an_empty_call_reports_zeroes_not_a_crash")
    # A call that failed before the first turn still has to produce a row: "no turns" is
    # itself the finding, and a metrics call that raised would take the call down with it.
    m = sess().agent_metrics()
    check(m["turns"] == 0, "no turns")
    check(m["first_audio_ms_p50"] == 0 and m["first_audio_ms_max"] == 0, "no latency invented")


def test_underruns_are_reported_because_they_decide_a_design_question():
    print("\ntest_underruns_are_reported_because_they_decide_a_design_question")
    # Streaming playout replaced whole-sentence buffering on the argument that Playout's
    # priming already prevents underruns. This counter is the evidence for or against that.
    check(sess(underruns=0).agent_metrics()["underruns"] == 0, "clean call reports 0")
    check(sess(underruns=3).agent_metrics()["underruns"] == 3, "gaps are surfaced, not hidden")


def test_caller_audio_level_is_reported():
    print("\ntest_caller_audio_level_is_reported")
    # peak==0 with frames received means we were bridged to digital silence; peak at full
    # scale means the caller's leg is clipping before it ever reaches us. Both are invisible
    # without this, and both look like "the agent didn't understand me".
    m = sess(peak_amplitude=32124, rms_sum=5000.0, rms_n=10).agent_metrics()
    check(m["caller_peak"] == 32124, "caller peak is carried")
    check(m["caller_rms_avg"] == 500.0, "caller average level is carried")


def test_stream_health_and_eager_rate_travel_with_the_call():
    print("\ntest_stream_health_and_eager_rate_travel_with_the_call")
    m = sess(stt_degraded=True, stt_failed=False, eager_hits=7, eager_retracted=2).agent_metrics()
    check(m["stt_degraded"] is True, "a call that silently fell back to the local VAD says so")
    check(m["eager_hits"] == 7 and m["eager_retracted"] == 2,
          "the eager hit/retract ratio is preserved — it is what says whether eager mode pays")


def test_recording_name_rides_along():
    print("\ntest_recording_name_rides_along")
    # OWEN cannot discover this any other way: the bridge is in owen-voice's Stasis app.
    m = sess(recording_name="1788978501.86-agent-1").agent_metrics()
    check(m["recording_name"] == "1788978501.86-agent-1", "the recording name is carried back")
    check(sess().agent_metrics()["recording_name"] is None, "absent when nothing was recorded")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_checks} checks, {len(_failures)} failed")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1 if _failures else 0)
