"""Unit test for job/lead detection (classification.job_tags + the dummy heuristic).

A "job" is a caller who both gives an address AND asks about a service. Either signal
alone is not a lead. No API, no DB — pure functions and the offline dummy engine.

Run: python -m tests.test_job_detection
"""

import asyncio
import sys

from app.analysis.classification import (
    JOB_TAG,
    DummyAnalysisEngine,
    job_tags,
    normalize_service_type,
)


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"job detection failed at: {name}")


def main():
    print("job_tags — address + service gating:")
    check("both signals -> job tag", job_tags(True, True) == [JOB_TAG])
    check("both + service type -> job + service tag",
          job_tags(True, True, "Garage Door") == [JOB_TAG, "service:garage-door"])
    check("address only -> no tag", job_tags(True, False, "roofing") == [])
    check("service only -> no tag", job_tags(False, True, "roofing") == [])
    check("neither -> no tag", job_tags(False, False) == [])
    check("blank service type omitted", job_tags(True, True, "   ") == [JOB_TAG])

    print("normalize_service_type — slugging:")
    check("spaces/casing slugged", normalize_service_type("Garage Door") == "garage-door")
    check("empty -> None", normalize_service_type("  ") is None)

    print("DummyAnalysisEngine — offline heuristic end-to-end:")
    eng = DummyAnalysisEngine()

    lead = asyncio.run(eng.analyze(
        "[Caller] Hi, I'm at 123 Main Street and my roof is leaking, can you help?\n"
        "[Operator] Sure, we do roofing."
    ))
    check("address + roofing -> job tag present", JOB_TAG in lead.tags)
    check("address + roofing -> service:roofing tag", "service:roofing" in lead.tags)
    check("genuine job categorized as booking", lead.category == "booking")

    no_addr = asyncio.run(eng.analyze(
        "[Caller] Do you do garage door repair?\n[Operator] Yes we do."
    ))
    check("service without address -> no job tag", JOB_TAG not in no_addr.tags)

    no_svc = asyncio.run(eng.analyze(
        "[Caller] My address is 456 Oak Avenue, just confirming you got it."
    ))
    check("address without service -> no job tag", JOB_TAG not in no_svc.tags)

    zip_only = asyncio.run(eng.analyze(
        "[Caller] I'm in 90210 and need my furnace looked at, the heating is out."
    ))
    check("ZIP + HVAC -> job tag", JOB_TAG in zip_only.tags)
    check("ZIP + HVAC -> service:hvac tag", "service:hvac" in zip_only.tags)

    print("\nALL JOB DETECTION CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)
