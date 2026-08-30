"""Keeping the failure artefact for the tests that failed, and only those.

§16.3: retention *is* the design. Recording every test and keeping
the output would dwarf the repository -- one artefact of a two-second recording
measures 800 kB -- so the buffer lives in memory, is dropped at teardown for a
passing test, and reaches the disk only when pytest already knows the outcome.

**Opt-in, deliberately.** This is not registered as a ``pytest11`` entry point,
so installing wirespec does not add options to every pytest run on the machine
or change how an unrelated suite behaves. A suite that wants it says so::

    # conftest.py
    pytest_plugins = ["wirespec.pytest_plugin"]

    @pytest_asyncio.fixture
    async def page(context, artefacts):
        page = await context.new_page()
        async with artefacts.record(page):
            yield page

The recording has to be stopped from a **fixture teardown**, not from inside the
test body: teardown is the only place that runs after pytest has written the
call report, and the call report is the only thing that knows whether the test
failed. ``record`` also treats an exception passing through it as a failure, so
the inline form still keeps what it should -- it just cannot see a failure that
happened after the block.

**Keeping nothing is not the same as costing nothing.** ``on-failure`` leaves a
green run's disk clean and still pays for every green test, because whether a
test fails is known only once it has finished. Measured as a real pytest run of
twelve green browser tests, recording them costs **26% of the CPU the whole run
burns**, Chrome included. On a workstation with cores to spare that never
reaches the wall clock; pinned to two cores, the shape of a CI runner, it is
**22% of the wall clock** (§16.3).

``--wirespec-record=on-retry`` is the answer to that, and it is a trade rather
than a free win. Nothing is recorded on a test's first attempt; if a rerun
plugin runs it again, that attempt is recorded and kept if it fails. A green run
then costs nothing at all -- and a failure that does not reproduce leaves no
artefact, which is the half worth knowing before choosing it.
"""

import contextlib
import hashlib
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from wirespec.artefact import write
from wirespec.recorder import Recorder, Timeline

if TYPE_CHECKING:
    from wirespec.page import Page

__all__ = ["Artefacts", "artefacts"]

#: ``on-failure`` is the default: it keeps nothing on a green run, which is the
#: property retention exists for. It is not *free* on a green run -- see
#: ``on-retry`` below. ``always`` is for the case the policy serves badly: a
#: test that passed and should not have. ``on-retry`` records nothing until a
#: test is run a second time.
POLICIES = ("on-failure", "on-retry", "always", "off")

#: Long enough to keep a parametrised test id readable, short enough to leave
#: room under the 255-byte limit every filesystem here imposes.
MAX_NAME = 120

_REPORTS: pytest.StashKey[dict[str, pytest.TestReport]] = pytest.StashKey()

#: How many times this item has entered setup. 1 on an ordinary run; 2 or more
#: only if something re-ran it. Counted here rather than read off a rerun
#: plugin's own attribute so that any of them will do -- every one of them
#: re-runs the item, and re-running an item runs its setup again.
_ATTEMPTS: pytest.StashKey[int] = pytest.StashKey()

#: Session-wide: whether anything was ever retried. What lets ``on-retry`` say
#: at the end that it wrote nothing because nothing re-ran it.
_RETRIED: pytest.StashKey[bool] = pytest.StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("wirespec")
    group.addoption(
        "--wirespec-record",
        default="on-failure",
        choices=POLICIES,
        help="when to record and keep the failure artefact (default: on-failure). "
        "on-retry records only re-run attempts, and needs a rerun plugin",
    )
    group.addoption(
        "--wirespec-artefacts",
        default="test-artefacts",
        metavar="DIR",
        help="where to write kept artefacts (default: test-artefacts)",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Count this attempt, and forget the last one's reports.

    Both halves matter. The count is what ``on-retry`` reads. The forgetting is
    what keeps :attr:`Artefacts.failed` about *this* attempt: a rerun runs
    against the same item object, so the previous attempt's reports are still in
    the stash while this one is being judged.

    Most of them are then overwritten in time -- each phase is stashed under its
    own name, and a retry writes its own ``setup`` and ``call`` before any
    fixture tears down. **Teardown is the exception**, and it is the reason this
    is here: teardown's report is written *after* the fixtures have gone, so a
    first attempt that failed there is on record while the second attempt's
    recording is being judged, and the artefact written would be of a run that
    passed, filed under the name of a test that no longer fails.
    """
    attempt = item.stash.get(_ATTEMPTS, 0) + 1
    item.stash[_ATTEMPTS] = attempt
    item.stash[_REPORTS] = {}
    if attempt > 1:
        item.config.stash[_RETRIED] = True


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    """Stash each phase's report where the fixture teardown can read it.

    pytest's own recipe. There is no other way to know at teardown whether the
    test passed: the fixture is torn down before anything is printed and the
    report is not handed to it.
    """
    report = yield
    item.stash.setdefault(_REPORTS, {})[report.when] = report
    return report


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, config: pytest.Config) -> None:
    """Say so when ``on-retry`` had nothing to record from.

    The one way this policy can disappoint: it is asked for, tests fail, and
    nothing re-runs them, so every artefact anybody wanted was never recorded.
    That is a configuration this plugin cannot see in advance -- there is no
    option name common to the rerun plugins to check for, and refusing to start
    over a missing ``--reruns`` would refuse the ones this has never heard of.
    So it is reported at the end, where the question is answerable exactly:
    something failed, and nothing was ever attempted twice.
    """
    if config.getoption("--wirespec-record") != "on-retry" or config.stash.get(_RETRIED, False):
        return
    failures = len(terminalreporter.stats.get("failed", []))
    if not failures:
        return
    terminalreporter.write_sep("-", "wirespec")
    terminalreporter.write_line(
        f"--wirespec-record=on-retry kept nothing: {failures} test(s) failed and none was re-run. "
        "This policy records re-run attempts only, so it needs a rerun plugin (pytest-rerunfailures' "
        "--reruns 1, say). Use --wirespec-record=on-failure to record every test instead."
    )


class Artefacts:
    """One test's handle on the retention policy."""

    def __init__(self, item: pytest.Item, policy: str, directory: Path) -> None:
        self.item = item
        self.policy = policy
        self.directory = directory
        #: Set when an exception passes through ``record``, so the inline form
        #: keeps its artefact too rather than depending on a report that has
        #: not been written yet.
        self.threw = False

    def __repr__(self) -> str:
        return f"<Artefacts {self.policy} -> {self.directory}>"

    @property
    def failed(self) -> bool:
        """Whether any phase of this test has failed *so far*.

        "So far" is the honest word: at fixture teardown the setup and call
        reports exist and the teardown report does not, so a test brought down
        by another fixture's teardown is not seen here. Nothing better is
        available at the point the recording has to stop.
        """
        return self.threw or any(report.failed for report in self.item.stash.get(_REPORTS, {}).values())

    @property
    def attempt(self) -> int:
        """Which run of this test this is. 1 unless something re-ran it."""
        return self.item.stash.get(_ATTEMPTS, 1)

    @property
    def recording(self) -> bool:
        """Whether to record this attempt **at all**.

        The distinction ``on-retry`` exists for. ``on-failure`` keeps nothing on
        a green run but still *records* every test, because whether a test fails
        is not known until it has finished -- and measured, that is **26% of a
        green suite's CPU** (§16.3). Under ``on-retry`` the first
        attempt of every test costs nothing, and the price is paid only by the
        tests that earned it.
        """
        if self.policy == "off":
            return False
        return self.attempt > 1 if self.policy == "on-retry" else True

    @property
    def wanted(self) -> bool:
        """Whether to write what was recorded. Never true if nothing was."""
        if not self.recording:
            return False
        return True if self.policy == "always" else self.failed

    def keep(self, timeline: Timeline, *, title: str = "") -> Path | None:
        """Write the artefact if the policy wants it. Returns where, or ``None``."""
        if not self.wanted:
            return None
        return write(timeline, self.directory / f"{self.name}.html", title=title or self.item.nodeid)

    @property
    def name(self) -> str:
        """The test's id as a file name, with collisions made impossible.

        Truncation is where two artefacts silently become one, and the survivor
        is whichever ran last rather than the interesting one. So a truncated
        name carries a digest of the id it was cut from (§1, goal 4).
        """
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", self.item.nodeid).strip("-")
        if len(safe) <= MAX_NAME:
            return safe
        return f"{safe[:MAX_NAME]}-{hashlib.blake2b(self.item.nodeid.encode(), digest_size=4).hexdigest()}"

    @contextlib.asynccontextmanager
    async def record(self, page: Page, **options: int) -> AsyncIterator[Recorder]:
        """Record this page for as long as the block lasts, then keep or drop.

        ``options`` go to :class:`~wirespec.recorder.Recorder` -- ``quality``,
        ``every_nth_frame`` and the three caps, all of which are counts.

        Under a policy that does not want this attempt recorded, the recorder is
        handed over **unstarted**: no screencast, no ``Network.enable``, no
        buffer. The block still gets a :class:`~wirespec.recorder.Recorder` so
        that a fixture written against this reads the same either way.
        """
        recorder = Recorder(page, **options)
        if self.recording:
            await recorder.start()
        try:
            yield recorder
        except BaseException:
            self.threw = True
            raise
        finally:
            await recorder.stop()
            self.keep(Timeline.of(recorder))


@pytest.fixture
def artefacts(request: pytest.FixtureRequest) -> Artefacts:
    """The retention policy, for this test."""
    # `getoption` is typed as returning Any | None because an option can be
    # absent; both of these have defaults and cannot be.
    directory = Path(str(request.config.getoption("--wirespec-artefacts")))
    if not directory.is_absolute():
        directory = Path(request.config.invocation_params.dir) / directory
    return Artefacts(request.node, str(request.config.getoption("--wirespec-record")), directory)
