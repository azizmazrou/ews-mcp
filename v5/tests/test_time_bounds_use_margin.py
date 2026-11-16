"""Every wall-clock bound in the tests is multiplied by conftest.TIME_MARGIN.

A cost bound that is right on a developer machine fails now and then on a shared CI runner
(one PDF test allowed 10 s and took 11 s there). The limits themselves are in the docs; the
tests multiply them by one shared margin, and this test keeps new tests from forgetting it.
"""
import re
from pathlib import Path

CLOCK = re.compile(r"\b(perf_counter|monotonic|time\.time)\(\)")
BOUND = re.compile(r"<=?\s*[0-9][0-9_.]*")
DURATION_NAME = re.compile(r"\bassert\s+\(?(?:max\()?(elapsed\w*|took\w*|wall\w*|duration\w*|dt\w*)\)?\s*<=?\s*[0-9]")


def test_wall_clock_assertions_use_the_shared_margin():
    offenders = []
    for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("assert") or "TIME_MARGIN" in line:
                continue
            if (CLOCK.search(line) and BOUND.search(line)) or DURATION_NAME.search(stripped):
                offenders.append(f"{path.name}:{number}: {stripped[:100]}")
    assert offenders == [], "wall-clock bounds without TIME_MARGIN:\n" + "\n".join(offenders)
