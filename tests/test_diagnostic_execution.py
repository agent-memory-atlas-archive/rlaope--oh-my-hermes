"""Behavior tests for the bounded post-GREEN diagnostic execution engine."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, Semaphore
import unittest

from omh.coding.diagnostic_execution import (
    DiagnosticExecutionEngine,
    DiagnosticExecutionRequest,
    DiagnosticExecutionSettings,
    ProviderObservation,
)
from omh.coding.diagnostic_providers import DiagnosticProviderConfig, ProviderCapability

# These tests assert concurrency bounds, not timing. Every wait below exists to
# get threads in flight or to notice a deadlock -- never to measure how fast the
# machine is. At two seconds a loaded CI runner failed one: the second worker
# had not been scheduled before the first gave up waiting and decremented its
# slot, so the barrier the assertion waits on never formed.
#
# The cost is asymmetric, so this errs long. Too short reddens branches that
# changed nothing -- it did so twice in one day -- while too long is paid only
# when something is genuinely stuck. It is not unbounded, because a deadlock
# should fail rather than hang the shard: measured at sixty seconds a broken
# engine took 540s to report, so this is fifteen times the bound that failed
# rather than thirty. A passing run never reaches it; these nine tests finish
# in three milliseconds.
#
# Raised again 30 -> 60 (#1755): the same wait failed once more on a Windows
# shard at 30s, on a branch that touches nothing under
# `diagnostic_execution.py` / `diagnostic_providers.py`. The identical commit,
# rerun, passed -- comparing the two attempts' own logged suite runtimes
# (1835.4s vs 1239.0s for the same 6370 tests) puts the failing attempt at 48%
# slower than the passing one on the same runner class. 60s comfortably clears
# that measured overrun with margin for worse contention still to come.
#
# The third failure (#1790: 60s, Linux, a branch touching nothing near this
# code) was not margin at all. `_observe` took the global slot before the
# provider slot, so a request queued behind a busy provider held a global slot
# while it waited: when both requests reached pyright first, the two global
# slots were pyright-running and pyright-waiting, and ruff could not start
# although nothing ran in its place. The barrier below waits for two runners on
# different providers at one instant, which that order only delivered on
# interleavings the test did not control -- 29 of 30 runs on a loaded host. The
# engine now takes the provider slot first, so a global slot is held only by a
# runner that is executing and the overlap is guaranteed rather than scheduled;
# `test_a_request_queued_behind_a_busy_provider_does_not_hold_a_global_slot`
# forces the interleaving that hung. The deadline stays: it bounds a genuinely
# stuck engine, and a failure now reports the observed `active` map rather than
# `False is not true`.
_THREAD_DEADLINE_SECONDS = 60


class _Resolver:
    def resolve(self, workspace_id: str, baseline: str, end: str) -> tuple[str, ...]:
        self.args = (workspace_id, baseline, end)
        return ("src/a.py",)


class _Revisions:
    def __init__(self, end_values: tuple[str, ...] = ("end-1", "end-1")) -> None:
        self.end_values = iter(end_values)

    def read(self, workspace_id: str, revision: str) -> str:
        return "base-1" if revision == "base" else next(self.end_values)


class _UnsupportedResolver:
    def resolve(self, workspace_id: str, baseline: str, end: str) -> tuple[str, ...]:
        return ("src/a.ts",)


class _Runner:
    def __init__(self, barrier: Barrier | None = None) -> None:
        self.barrier = barrier
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.lock = Lock()

    def run(self, provider_id: str, workspace_id: str, revision: str, files: tuple[str, ...], timeout_ms: int,
            cancelled: object) -> ProviderObservation:
        with self.lock:
            self.calls.append((provider_id, revision, files))
        if self.barrier is not None:
            self.barrier.wait(timeout=_THREAD_DEADLINE_SECONDS)
        diagnostics = () if revision == "base-1" else (_item(provider_id),)
        return ProviderObservation.completed(files, diagnostics)


class _ObservedSlot:
    """Wrap one of the engine's semaphores and report every acquisition attempt.

    The attempt is reported before the acquire blocks, so a test can tell that a
    thread has reached the slot even while it waits there.
    """

    def __init__(self, inner: Semaphore, on_attempt: Callable[[], None]) -> None:
        self.inner = inner
        self.on_attempt = on_attempt

    def __enter__(self) -> bool:
        self.on_attempt()
        return self.inner.__enter__()

    def __exit__(self, *exc: object) -> None:
        self.inner.__exit__(*exc)


def _item(provider: str) -> dict[str, object]:
    return {"severity": "error", "code": "E1", "path": "src/a.py", "line": 2, "character": 3, "source": provider}


def _config(*providers: str, enabled: bool = True) -> DiagnosticProviderConfig:
    return DiagnosticProviderConfig(tuple(
        ProviderCapability(provider, ("python",), (".py",), 1_000, 10, 10, enabled) for provider in providers
    ))


class BaselineAndEndExecutionTests(unittest.TestCase):
    def test_records_normalized_metadata_deltas_once_and_revalidates_end_head(self) -> None:
        runner = _Runner(Barrier(2))
        engine = DiagnosticExecutionEngine(
            config=_config("pyright", "ruff"), resolver=_Resolver(), revisions=_Revisions(("end-1", "end-2", "end-1", "end-2")),
            runner=runner, settings=DiagnosticExecutionSettings(max_global_concurrency=2, max_provider_concurrency=1),
        )
        request = DiagnosticExecutionRequest("wrapper", "local/omh", "base", "HEAD")

        first = engine.execute(request)
        second = engine.execute(request)

        self.assertEqual(len(runner.calls), 4, "each provider/file/revision identity runs once")
        self.assertEqual({result.status for result in first.results}, {"stale"})
        pyright = next(result for result in first.results if result.provider_id == "pyright")
        self.assertEqual(pyright.evidence["introduced"], [_item("pyright")])
        self.assertEqual(pyright.evidence["resolved"], [])
        self.assertEqual(pyright.evidence["privacy"], "metadata_only")
        self.assertNotIn("verification", pyright.evidence["summary_label"].lower())
        self.assertEqual(first.results, second.results)

    def test_provider_outcome_scope_excludes_changed_files_with_other_suffixes(self) -> None:
        class MixedResolver:
            def resolve(
                self,
                workspace_id: str,
                baseline: str,
                end: str,
            ) -> tuple[str, ...]:
                return ("src/a.py", "docs/guide.md")

        result = DiagnosticExecutionEngine(
            config=_config("ruff"),
            resolver=MixedResolver(),
            revisions=_Revisions(),
            runner=_Runner(),
        ).execute(
            DiagnosticExecutionRequest(
                "wrapper",
                "local/omh",
                "base",
                "HEAD",
            )
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(
            result.results[0].end.in_scope_files,
            ("src/a.py",),
        )

    def test_fixed_revision_is_stale_when_the_execution_workspace_head_moves(self) -> None:
        end = "a" * 40
        moved = "c" * 40

        class WorkspaceRevisions:
            def __init__(self) -> None:
                self.heads = iter((end, moved))

            def read(self, workspace_id: str, revision: str) -> str:
                return next(self.heads) if revision == "HEAD" else revision

        result = DiagnosticExecutionEngine(
            config=_config("ruff"),
            resolver=_Resolver(),
            revisions=WorkspaceRevisions(),
            runner=_Runner(),
            settings=DiagnosticExecutionSettings(
                revalidate_workspace_head=True
            ),
        ).execute(
            DiagnosticExecutionRequest(
                "wrapper",
                "local/omh",
                "b" * 40,
                end,
                workspace_path="/tmp/diagnostic-workspace",
            )
        )

        self.assertEqual(result.status, "stale")
        self.assertEqual(
            result.results[0].evidence["verdict"],
            "stale_diagnostics",
        )

    def test_disabled_and_unsupported_are_distinct_and_never_run_a_provider(self) -> None:
        disabled = DiagnosticExecutionEngine(
            config=_config("pyright"), resolver=_Resolver(), revisions=_Revisions(), runner=_Runner(),
            settings=DiagnosticExecutionSettings(enabled=False),
        ).execute(DiagnosticExecutionRequest("wrapper", "local/omh", "base", "HEAD"))
        unsupported_runner = _Runner()
        unsupported = DiagnosticExecutionEngine(
            config=_config("pyright"), resolver=_UnsupportedResolver(), revisions=_Revisions(), runner=unsupported_runner,
        ).execute(DiagnosticExecutionRequest("wrapper", "local/omh", "base", "HEAD"))

        self.assertEqual(disabled.status, "disabled")
        self.assertEqual(unsupported.status, "unsupported")
        self.assertEqual(unsupported_runner.calls, [])


class BoundedExecutionTests(unittest.TestCase):
    def test_cancellation_unavailable_and_crash_remain_distinct(self) -> None:
        cancelled = Event()
        cancelled.set()
        engine = DiagnosticExecutionEngine(
            config=_config("pyright"), resolver=_Resolver(), revisions=_Revisions(), runner=_Runner(), cancellation=cancelled,
        )
        result = engine.execute(DiagnosticExecutionRequest("wrapper", "local/omh", "base", "HEAD"))
        self.assertEqual(result.status, "cancelled")

        for observation, expected in ((ProviderObservation.unavailable(), "unavailable"), (ProviderObservation.crashed(), "crashed")):
            class Runner:
                def run(self, *args: object) -> ProviderObservation:
                    return observation
            result = DiagnosticExecutionEngine(
                config=_config("pyright"), resolver=_Resolver(), revisions=_Revisions(), runner=Runner(),
            ).execute(DiagnosticExecutionRequest("wrapper", "local/omh", "base", "HEAD"))
            self.assertEqual(result.results[0].status, expected)

    def test_unexpected_runner_programming_errors_are_not_hidden_as_provider_crashes(self) -> None:
        class Runner:
            def run(self, *args: object) -> ProviderObservation:
                raise AssertionError("runner contract bug")

        engine = DiagnosticExecutionEngine(
            config=_config("pyright"),
            resolver=_Resolver(),
            revisions=_Revisions(),
            runner=Runner(),
        )

        with self.assertRaisesRegex(AssertionError, "runner contract bug"):
            engine.execute(DiagnosticExecutionRequest("wrapper", "local/omh", "base", "HEAD"))

        class ProcessRunner:
            def run(self, *args: object) -> ProviderObservation:
                raise OSError("provider process unavailable")

        process_result = DiagnosticExecutionEngine(
            config=_config("pyright"),
            resolver=_Resolver(),
            revisions=_Revisions(),
            runner=ProcessRunner(),
        ).execute(DiagnosticExecutionRequest("wrapper", "local/omh", "base", "HEAD"))
        self.assertEqual(process_result.results[0].status, "crashed")

    def test_provider_output_with_a_message_is_refused_without_persisting_it(self) -> None:
        class Runner:
            def run(self, provider: str, workspace: str, revision: str, files: tuple[str, ...], timeout: int,
                    cancelled: object) -> ProviderObservation:
                return ProviderObservation.completed(files, (dict(_item(provider), message="secret source body"),))

        result = DiagnosticExecutionEngine(
            config=_config("pyright"), resolver=_Resolver(), revisions=_Revisions(), runner=Runner(),
        ).execute(DiagnosticExecutionRequest("wrapper", "local/omh", "base", "HEAD"))

        self.assertEqual(result.results[0].status, "crashed")
        self.assertNotIn("secret source body", repr(result.results[0]))

    def test_stateful_provider_is_serial_across_overlapping_requests(self) -> None:
        started, release, second_started = Event(), Event(), Event()
        active = defaultdict(int)
        lock = Lock()

        class Runner:
            def run(self, provider: str, workspace: str, revision: str, files: tuple[str, ...], timeout: int,
                    cancelled: object) -> ProviderObservation:
                with lock:
                    active[provider] += 1
                    if active[provider] == 1:
                        started.set()
                    else:
                        second_started.set()
                release.wait(timeout=_THREAD_DEADLINE_SECONDS)
                with lock:
                    active[provider] -= 1
                return ProviderObservation.completed(files, ())

        engine = DiagnosticExecutionEngine(
            config=_config("pyright"), resolver=_Resolver(), revisions=_Revisions(("end-1",) * 4), runner=Runner(),
            settings=DiagnosticExecutionSettings(stateful_providers=frozenset(("pyright",))),
        )
        request = DiagnosticExecutionRequest("wrapper", "local/omh", "base", "HEAD")
        second_request = DiagnosticExecutionRequest("wrapper", "local/other", "base", "HEAD")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(engine.execute, request)
            self.assertTrue(started.wait(timeout=_THREAD_DEADLINE_SECONDS))
            second = pool.submit(engine.execute, second_request)
            release.set()
            first.result(timeout=_THREAD_DEADLINE_SECONDS)
            second.result(timeout=_THREAD_DEADLINE_SECONDS)
        self.assertFalse(second_started.is_set())

    def test_global_and_per_provider_slots_bound_overlapping_requests(self) -> None:
        two_running, release = Event(), Event()
        active: dict[str, int] = defaultdict(int)
        maximum: dict[str, int] = defaultdict(int)
        maximum_total = [0]
        lock = Lock()

        class Runner:
            def run(self, provider: str, workspace: str, revision: str, files: tuple[str, ...], timeout: int,
                    cancelled: object) -> ProviderObservation:
                with lock:
                    active[provider] += 1
                    maximum[provider] = max(maximum[provider], active[provider])
                    maximum_total[0] = max(maximum_total[0], sum(active.values()))
                    if sum(active.values()) == 2:
                        two_running.set()
                release.wait(timeout=_THREAD_DEADLINE_SECONDS)
                with lock:
                    active[provider] -= 1
                return ProviderObservation.completed(files, ())

        engine = DiagnosticExecutionEngine(
            config=_config("pyright", "ruff"), resolver=_Resolver(), revisions=_Revisions(("end-1",) * 4),
            runner=Runner(), settings=DiagnosticExecutionSettings(max_global_concurrency=2, max_provider_concurrency=1),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(engine.execute, DiagnosticExecutionRequest("wrapper", "local/one", "base", "HEAD"))
            second = pool.submit(engine.execute, DiagnosticExecutionRequest("wrapper", "local/two", "base", "HEAD"))
            overlapped = two_running.wait(timeout=_THREAD_DEADLINE_SECONDS)
            with lock:
                observed = dict(active)
            # Release whether or not the barrier formed: a failure reports the
            # observation now, not after every remaining runner has waited out
            # its own deadline (eight runners, six minutes, when it failed here).
            release.set()
            first.result(timeout=_THREAD_DEADLINE_SECONDS)
            second.result(timeout=_THREAD_DEADLINE_SECONDS)
        self.assertTrue(
            overlapped,
            f"two runners never overlapped before the deadline: active={observed} "
            f"maximum={dict(maximum)} maximum_total={maximum_total[0]}",
        )
        self.assertEqual(maximum_total[0], 2, dict(maximum))
        self.assertLessEqual(max(maximum.values()), 1, dict(maximum))

    def test_a_request_queued_behind_a_busy_provider_does_not_hold_a_global_slot(self) -> None:
        # The interleaving that hung the test above (#1790): both requests
        # reach pyright before either reaches ruff. The second pyright thread
        # queues on pyright's slot; if it held a global slot while it waited,
        # both global slots would be pyright's and ruff could never start.
        two_running, release, pyright_queued = Event(), Event(), Event()
        active: dict[str, int] = defaultdict(int)
        maximum: dict[str, int] = defaultdict(int)
        maximum_total = [0]
        pyright_attempts = [0]
        lock = Lock()

        class Runner:
            def run(self, provider: str, workspace: str, revision: str, files: tuple[str, ...], timeout: int,
                    cancelled: object) -> ProviderObservation:
                with lock:
                    active[provider] += 1
                    maximum[provider] = max(maximum[provider], active[provider])
                    maximum_total[0] = max(maximum_total[0], sum(active.values()))
                    if sum(active.values()) == 2:
                        two_running.set()
                release.wait(timeout=_THREAD_DEADLINE_SECONDS)
                with lock:
                    active[provider] -= 1
                return ProviderObservation.completed(files, ())

        class GatedEngine(DiagnosticExecutionEngine):
            def _observe_pair(self, request: DiagnosticExecutionRequest, capability: ProviderCapability,
                              files: tuple[str, ...], baseline: str, end: str,
                              execution_workspace: str) -> tuple[ProviderObservation, ProviderObservation]:
                if capability.provider_id == "ruff":
                    pyright_queued.wait(timeout=_THREAD_DEADLINE_SECONDS)
                return super()._observe_pair(request, capability, files, baseline, end, execution_workspace)

        def pyright_attempted() -> None:
            with lock:
                pyright_attempts[0] += 1
                if pyright_attempts[0] == 2:
                    pyright_queued.set()

        engine = GatedEngine(
            config=_config("pyright", "ruff"), resolver=_Resolver(), revisions=_Revisions(("end-1",) * 4),
            runner=Runner(), settings=DiagnosticExecutionSettings(max_global_concurrency=2, max_provider_concurrency=1),
        )
        # The slots are the engine's own; nothing public sits between the two
        # acquisitions, and this test exists to pin their order.
        engine._provider_slots["pyright"] = _ObservedSlot(engine._provider_slots["pyright"], pyright_attempted)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(engine.execute, DiagnosticExecutionRequest("wrapper", "local/one", "base", "HEAD"))
            second = pool.submit(engine.execute, DiagnosticExecutionRequest("wrapper", "local/two", "base", "HEAD"))
            overlapped = two_running.wait(timeout=_THREAD_DEADLINE_SECONDS)
            with lock:
                observed = dict(active)
                attempts = pyright_attempts[0]
            release.set()
            first.result(timeout=_THREAD_DEADLINE_SECONDS)
            second.result(timeout=_THREAD_DEADLINE_SECONDS)
        self.assertTrue(
            overlapped,
            f"ruff never ran beside pyright: active={observed} pyright_slot_attempts={attempts} "
            f"maximum={dict(maximum)} maximum_total={maximum_total[0]}",
        )
        self.assertEqual(maximum_total[0], 2, dict(maximum))
        self.assertLessEqual(max(maximum.values()), 1, dict(maximum))


if __name__ == "__main__":
    unittest.main()
