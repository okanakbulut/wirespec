"""Retention: the artefact is kept for the tests that failed, and only those.

Run through pytest's own ``pytester``, which runs a real pytest over a real
test file, because the thing being tested *is* pytest's outcome machinery. No
browser anywhere near it -- the timeline is handed over directly, so these stay
fast and cannot fail for a reason that has nothing to do with retention.
"""

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from wirespec import pytest_plugin as plugin

if TYPE_CHECKING:
    from wirespec.page import Page

pytest_plugins = ["pytester"]

#: A test file that keeps a (blank) recording from a fixture teardown, which is
#: where a real one is kept from: teardown runs after pytest knows the outcome,
#: and nothing else does.
SUITE = """
    import pytest
    from wirespec.recorder import Timeline

    @pytest.fixture
    def recording(artefacts):
        yield
        artefacts.keep(Timeline(frames=[], traffic=[], messages=[], thinned=0))

    def test_that_fails(recording):
        assert 1 == 2

    def test_that_passes(recording):
        assert 1 == 1
"""


def run(pytester: pytest.Pytester, *options: str) -> list[str]:
    # The inner run does not inherit this repository's ini, and pytest-asyncio
    # warns loudly about the scope it then has to guess. The warning is about
    # the generated project, not about anything under test, and it is emitted
    # into *this* run's summary.
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makepyfile(SUITE)
    pytester.runpytest("-p", "wirespec.pytest_plugin", *options)
    kept = pytester.path / "test-artefacts"
    return sorted(path.name for path in kept.iterdir()) if kept.is_dir() else []


def test_a_failing_test_keeps_its_artefact(pytester: pytest.Pytester) -> None:
    kept = run(pytester)
    assert len(kept) == 1
    assert "test_that_fails" in kept[0]
    assert kept[0].endswith(".html")


def test_a_passing_test_keeps_nothing(pytester: pytest.Pytester) -> None:
    """The retention policy is the design (§16.3). Recording every
    test and keeping the output would dwarf the repository -- one artefact here
    is 800 kB."""
    assert not [name for name in run(pytester) if "passes" in name]


def test_off_keeps_nothing_at_all(pytester: pytest.Pytester) -> None:
    assert run(pytester, "--wirespec-record=off") == []


def test_always_keeps_both(pytester: pytest.Pytester) -> None:
    """For the case the policy exists to serve badly: a test that passes and
    should not have."""
    kept = run(pytester, "--wirespec-record=always")
    assert len(kept) == 2


def test_the_directory_is_the_one_that_was_asked_for(pytester: pytest.Pytester) -> None:
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makepyfile(SUITE)
    pytester.runpytest("-p", "wirespec.pytest_plugin", "--wirespec-artefacts=elsewhere/runs")
    assert (pytester.path / "elsewhere" / "runs").is_dir()
    assert not (pytester.path / "test-artefacts").exists()


def test_an_unknown_policy_is_refused_by_name(pytester: pytest.Pytester) -> None:
    """Nothing fails silently. ``--wirespec-record=onfailure`` recording
    everything, or nothing, would be found out much later."""
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makepyfile(SUITE)
    result = pytester.runpytest("-p", "wirespec.pytest_plugin", "--wirespec-record=sometimes")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*sometimes*"])


def test_two_tests_whose_long_names_share_a_prefix_do_not_overwrite_each_other(
    pytester: pytest.Pytester,
) -> None:
    """A file name is truncated to stay a legal file name, and truncation is
    where two artefacts silently become one. The one that survives is not even
    the interesting one -- it is whichever ran last."""
    long = "x" * 200
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makepyfile(f"""
        import pytest
        from wirespec.recorder import Timeline

        @pytest.fixture
        def recording(artefacts):
            yield
            artefacts.keep(Timeline(frames=[], traffic=[], messages=[], thinned=0))

        def test_{long}_one(recording):
            assert False

        def test_{long}_two(recording):
            assert False
    """)
    pytester.runpytest("-p", "wirespec.pytest_plugin")
    kept = sorted((pytester.path / "test-artefacts").iterdir())
    assert len(kept) == 2
    assert all(len(path.name) <= 160 for path in kept)


# -- on-retry: not recording at all, until a test has earned it ---------------

#: A miniature of what every rerun plugin does, so this can be tested without
#: depending on one. pytest-rerunfailures and pytest-retry differ in their
#: options and their bookkeeping and agree on the only part that matters here:
#: the same item goes through the protocol a second time.
RERUN = """
    from _pytest.runner import runtestprotocol

    def pytest_runtest_protocol(item, nextitem):
        reports = runtestprotocol(item, nextitem=nextitem, log=False)
        if not any(report.failed for report in reports):
            for report in reports:
                item.ihook.pytest_runtest_logreport(report=report)
            return True
        item._initrequest()
        runtestprotocol(item, nextitem=nextitem, log=True)
        return True
"""

#: Fails the first time it is run and passes the second -- a flake, which is
#: the case ``on-retry`` is worst at and has to be honest about.
FLAKY = """
    import pathlib
    import pytest
    from wirespec.recorder import Timeline

    @pytest.fixture
    def recording(artefacts):
        yield
        artefacts.keep(Timeline(frames=[], traffic=[], messages=[], thinned=0))

    def test_that_flakes(recording):
        marker = pathlib.Path("attempted")
        already = marker.exists()
        marker.touch()
        assert already, "fails the first time, passes the second"
"""


def rerun(pytester: pytest.Pytester, suite: str, *options: str) -> tuple[list[str], pytest.RunResult]:
    """Run ``suite`` under the miniature rerun plugin above."""
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makeconftest(RERUN)
    pytester.makepyfile(suite)
    result = pytester.runpytest("-p", "wirespec.pytest_plugin", *options)
    kept = pytester.path / "test-artefacts"
    return (sorted(path.name for path in kept.iterdir()) if kept.is_dir() else []), result


def test_on_retry_keeps_nothing_when_nothing_re_runs_the_test(pytester: pytest.Pytester) -> None:
    """The first attempt is not recorded, so there is nothing to keep -- not
    even for the test that failed. That is the trade, and it is the whole
    saving: measured, recording the tests that pass is 26% of a green suite's
    CPU, and 22% of its wall clock on two cores (§16.3)."""
    assert run(pytester, "--wirespec-record=on-retry") == []


def test_on_retry_says_so_rather_than_quietly_keeping_nothing(pytester: pytest.Pytester) -> None:
    """A policy that needs a rerun plugin and does not have one would otherwise
    look exactly like a policy that is working. There is no option name common
    to the rerun plugins to check at startup, so the answer is given at the end,
    when it can be given exactly: something failed and nothing was run twice."""
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makepyfile(SUITE)
    result = pytester.runpytest("-p", "wirespec.pytest_plugin", "--wirespec-record=on-retry")
    result.stdout.fnmatch_lines(["*on-retry kept nothing*none was re-run*"])


def test_on_retry_is_quiet_on_a_green_run(pytester: pytest.Pytester) -> None:
    """Nothing failed, so nothing was missed and there is nothing to say."""
    pytester.makeini("[pytest]\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makepyfile("""
        import pytest
        from wirespec.recorder import Timeline

        @pytest.fixture
        def recording(artefacts):
            yield
            artefacts.keep(Timeline(frames=[], traffic=[], messages=[], thinned=0))

        def test_that_passes(recording):
            assert 1 == 1
    """)
    result = pytester.runpytest("-p", "wirespec.pytest_plugin", "--wirespec-record=on-retry")
    assert "on-retry kept nothing" not in result.stdout.str()


def test_on_retry_keeps_the_artefact_of_the_second_attempt(pytester: pytest.Pytester) -> None:
    """The attempt that was recorded is the one that is kept, and the test that
    passed still keeps nothing."""
    kept, _ = rerun(pytester, SUITE, "--wirespec-record=on-retry")
    assert len(kept) == 1
    assert "test_that_fails" in kept[0]


def test_a_flake_that_passes_on_the_retry_keeps_nothing(pytester: pytest.Pytester) -> None:
    """The recorded attempt is the one that *worked*, so an artefact of it would
    show a test doing everything right -- and it would be filed under the name
    of a test that failed.

    This much comes free: each phase's report is stashed under its own name, so
    the retry's passing ``call`` overwrites the first attempt's failing one. The
    phase where that is not enough is the next test.
    """
    kept, result = rerun(pytester, FLAKY, "--wirespec-record=on-retry")
    result.assert_outcomes(passed=1)
    assert kept == []


#: Fails in *teardown* the first time and passes the second. The one phase whose
#: report a retry does not overwrite in time: teardown's report is written after
#: the fixtures have been torn down, so the report still in the stash while the
#: second attempt's fixtures are tearing down is the first attempt's.
LEAKY = """
    import pathlib
    import pytest
    from wirespec.recorder import Timeline

    @pytest.fixture
    def recording(artefacts):
        yield
        artefacts.keep(Timeline(frames=[], traffic=[], messages=[], thinned=0))

    @pytest.fixture
    def breaks_once():
        yield
        marker = pathlib.Path("torn-down")
        already = marker.exists()
        marker.touch()
        assert already, "the first teardown fails, the second does not"

    def test_that_tears_down_badly(recording, breaks_once):
        assert True
"""


def test_a_teardown_that_failed_on_the_previous_attempt_is_not_this_one(
    pytester: pytest.Pytester,
) -> None:
    """Why the attempt counter clears the stashed reports rather than trusting
    them to be overwritten. Without it, this writes an artefact of a run that
    passed, named after a test that failed for a reason it no longer has."""
    kept, result = rerun(pytester, LEAKY, "--wirespec-record=on-retry")
    result.assert_outcomes(passed=1)
    assert kept == []


def test_on_failure_is_unchanged_by_a_rerun(pytester: pytest.Pytester) -> None:
    """The default records every attempt, so the retry is recorded too and the
    failure is kept exactly as before."""
    kept, _ = rerun(pytester, SUITE)
    assert len(kept) == 1
    assert "test_that_fails" in kept[0]


# -- and that the first attempt really costs nothing --------------------------


class _Item:
    """Enough of a pytest item for :class:`Artefacts`: a stash and a node id."""

    def __init__(self, attempt: int) -> None:
        self.stash = pytest.Stash()
        self.stash[plugin._ATTEMPTS] = attempt
        self.nodeid = "tests/test_thing.py::test_it"


def calls_of(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Swap in a recorder that only remembers what was asked of it."""
    calls: list[str] = []

    class _NeverStarted:
        def __init__(self, page: object, **options: int) -> None:
            self.frames: list[object] = []
            self.traffic: list[object] = []
            self.messages: list[object] = []
            self.actions: list[object] = []
            self.pages: list[object] = []
            self.thinned = 0
            self.dropped = 0
            #: Read by `Timeline.of`, so the stand-in has to carry it too --
            #: this class stands in for the whole surface the writer touches.
            self.redact = True

        async def start(self) -> None:
            calls.append("start")

        async def stop(self) -> None:
            calls.append("stop")

    monkeypatch.setattr(plugin, "Recorder", _NeverStarted)
    return calls


def artefacts_for(attempt: int, policy: str, directory: Path) -> plugin.Artefacts:
    return plugin.Artefacts(cast("pytest.Item", _Item(attempt)), policy, directory)


async def test_the_first_attempt_starts_no_recorder_at_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Where the saving actually is. Not "records and throws away": no
    screencast is started and ``Network`` is never enabled, so the frames are
    never encoded, never sent and never decoded."""
    calls = calls_of(monkeypatch)
    async with artefacts_for(1, "on-retry", tmp_path).record(cast("Page", object())):
        assert "start" not in calls
    assert list(tmp_path.iterdir()) == []


async def test_the_second_attempt_starts_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = calls_of(monkeypatch)
    async with artefacts_for(2, "on-retry", tmp_path).record(cast("Page", object())):
        assert calls == ["start"]


async def test_the_block_still_gets_a_recorder_either_way(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A fixture written against ``record`` reads the same under every policy.
    Only whether the recorder was started differs."""
    calls_of(monkeypatch)
    for policy, attempt in (("on-retry", 1), ("on-retry", 2), ("on-failure", 1), ("off", 1)):
        async with artefacts_for(attempt, policy, tmp_path).record(cast("Page", object())) as recorder:
            assert recorder is not None


async def test_a_failure_on_a_recorded_attempt_is_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The other half: the retry that fails does produce a file."""
    calls_of(monkeypatch)
    with pytest.raises(AssertionError):
        async with artefacts_for(2, "on-retry", tmp_path).record(cast("Page", object())):
            raise AssertionError("the test failed")
    assert [path.suffix for path in tmp_path.iterdir()] == [".html"]
