"""Run one spec file under both drivers and diff the outcomes.

§15.4: compatibility is not measured by reading Playwright's
documentation and ticking items off. It is measured by running the same spec
under both and comparing — the same assertions passing, and the same ones
failing.

    .venv/bin/python differential/compare.py

Two interpreters, because the two drivers cannot share one: wirespec's venv has
no Playwright in it, and the reference venv has no ``msgspec``. The spec file
says ``from playwright.async_api import ...`` either way; under wirespec's
interpreter ``-p wirespec.compat.shim`` is what makes that resolve.

Both runs get the same Chrome, found once here, and the same fixture pages.
Timing is printed and never compared: wirespec being faster is the point, and a
check that fails when it gets faster is not a check.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SPEC = HERE / "test_surface.py"

#: The interpreter with the real Playwright in it. Made with
#: ``uv venv .venv-playwright && uv pip install --python .venv-playwright/bin/python
#: playwright pytest pytest-asyncio`` -- and no ``playwright install``, because
#: both runs are pointed at the Chrome already on the machine.
REFERENCE = Path(os.environ.get("WIRESPEC_PLAYWRIGHT_PYTHON", ROOT / ".venv-playwright/bin/python"))

#: Outcomes that are not "passed", in the order a reader wants to see them.
BAD = ("error", "failure", "skipped")


def run(name: str, python: Path, plugins: list[str], report: Path) -> tuple[dict[str, str], float]:
    """One pytest run, as a map of test name to outcome."""
    command = [str(python), "-m", "pytest", str(SPEC), "-q", "--no-header", f"--junit-xml={report}"]
    for plugin in plugins:
        command += ["-p", plugin]
    started = time.monotonic()
    finished = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    took = time.monotonic() - started
    if not report.exists():
        print(f"--- {name} produced no report ---\n{finished.stdout}\n{finished.stderr}")
        raise SystemExit(2)
    return _outcomes(report), took


def _outcomes(report: Path) -> dict[str, str]:
    """Read a JUnit report. ``--junit-xml`` is pytest's own, so neither
    interpreter needs a reporting plugin the other does not have."""
    outcomes: dict[str, str] = {}
    for case in ElementTree.parse(report).getroot().iter("testcase"):
        outcome = "passed"
        detail = ""
        for kind in BAD:
            found = case.find(kind)
            if found is not None:
                outcome = kind
                detail = (found.get("message") or "").splitlines()[0][:120]
                break
        outcomes[case.get("name") or "?"] = f"{outcome}: {detail}" if detail else outcome
    return outcomes


def main() -> int:
    if not REFERENCE.exists():
        print(f"no reference interpreter at {REFERENCE}; set WIRESPEC_PLAYWRIGHT_PYTHON")
        return 3
    sys.path.insert(0, str(ROOT))
    from wirespec.browser import find_chrome

    chrome = find_chrome()
    if chrome is None:
        print("no Chrome on this machine")
        return 3
    os.environ["WIRESPEC_DIFFERENTIAL_CHROME"] = chrome

    reports = ROOT / ".differential"
    reports.mkdir(exist_ok=True)
    reference, reference_took = run("playwright", REFERENCE, [], reports / "playwright.xml")
    ours, ours_took = run("wirespec", Path(sys.executable), ["wirespec.compat.shim"], reports / "wirespec.xml")

    names = sorted(set(reference) | set(ours))
    disagreed = [name for name in names if reference.get(name, "absent") != ours.get(name, "absent")]

    width = max((len(name) for name in names), default=0)
    for name in disagreed:
        print(f"{name:<{width}}  playwright: {reference.get(name, 'absent')}")
        print(f"{'':<{width}}  wirespec:   {ours.get(name, 'absent')}")
    passed = sum(1 for name in names if reference.get(name) == "passed")
    print(
        f"\n{len(names)} tests · {passed} pass under Playwright · "
        f"{len(names) - len(disagreed)} agree · {len(disagreed)} differ"
    )
    print(f"playwright {reference_took:.1f}s · wirespec {ours_took:.1f}s")
    return 1 if disagreed else 0


if __name__ == "__main__":
    raise SystemExit(main())
