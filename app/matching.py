"""Regex matching with a hard, *enforceable* per-pattern runtime timeout.

Why processes and not threads:
    CPython's ``re`` engine does NOT release the GIL while matching. A pattern
    with catastrophic backtracking therefore pins the GIL for its entire
    (potentially exponential) run, which starves every other Python thread —
    including any watchdog trying to time it out. ``Thread.join(timeout)`` never
    even gets a chance to return. So a thread-based timeout is an illusion.

    The only reliable way to bound a CPU-bound ``re`` evaluation with stdlib is
    to run it in a separate OS process that the parent can forcibly
    ``terminate()`` (the kernel kills it regardless of what C code it's in).

Design:
    :class:`TimeoutRegexEvaluator` starts one long-lived worker process that
    holds the compiled patterns. For each search the parent sends ``(index,
    text)`` and waits up to ``timeout`` for a reply. A normal search costs only
    a cheap IPC round-trip — no per-search process spawn. Only when a pattern
    blows past the timeout do we pay to kill and respawn the worker, mark that
    pattern timed out, and carry on with the rest. The evaluator is reused
    across all entries of a source poll and closed at the end.

Matching strategy per entry:
    * Title first: if any enabled pattern matches the title, it's a match — stop.
    * Otherwise body/summary (already truncated by the caller).
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import re
from dataclasses import dataclass

log = logging.getLogger("rssphaser.matching")

# 'spawn' avoids the fork-inside-a-thread hazards (inherited locks) that would
# otherwise bite us, since polling runs sources in a thread pool.
_MP_CTX = mp.get_context("spawn")


@dataclass
class CompiledKeyword:
    keyword_id: int
    pattern: str
    regex: re.Pattern[str] | None  # None if it failed to compile
    compile_error: str | None = None


@dataclass
class MatchOutcome:
    matched: bool
    # keyword ids that exceeded the timeout (or failed to compile) for this entry
    timed_out_keyword_ids: set[int]
    matched_keyword_id: int | None = None


def compile_keywords(rows: list[tuple[int, str]]) -> list[CompiledKeyword]:
    """Compile (id, pattern) pairs case-insensitively. Bad patterns are kept
    with ``regex=None`` so callers can flag them rather than crash."""
    compiled: list[CompiledKeyword] = []
    for kid, pattern in rows:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
            compiled.append(CompiledKeyword(kid, pattern, rx))
        except re.error as exc:
            log.warning("Keyword %d failed to compile (%r): %s", kid, pattern, exc)
            compiled.append(CompiledKeyword(kid, pattern, None, compile_error=str(exc)))
    return compiled


def _worker_loop(patterns: list[str], req_q, resp_q) -> None:
    """Worker-process entrypoint: compile patterns, then serve search requests.

    Protocol: parent puts ``(index, text)`` on ``req_q``; worker replies with a
    bool on ``resp_q``. ``None`` on ``req_q`` is the shutdown sentinel.
    """
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    while True:
        msg = req_q.get()
        if msg is None:
            return
        idx, text = msg
        try:
            resp_q.put(compiled[idx].search(text) is not None)
        except Exception:  # noqa: BLE001 - never let the worker die on a search
            resp_q.put(False)


class TimeoutRegexEvaluator:
    """Reusable evaluator backed by a killable worker process.

    Patterns that failed to compile (``regex is None``) are never sent to the
    worker; they are reported as timed-out/problematic up front.
    """

    def __init__(self, keywords: list[CompiledKeyword], timeout: float) -> None:
        self.timeout = timeout
        # Keep only compilable patterns for the worker; remember the keyword id
        # and original pattern by worker index.
        self._valid: list[CompiledKeyword] = [k for k in keywords if k.regex is not None]
        self._uncompilable_ids: set[int] = {k.keyword_id for k in keywords if k.regex is None}
        self._patterns: list[str] = [k.pattern for k in self._valid]
        self._proc: mp.process.BaseProcess | None = None
        self._req_q = None
        self._resp_q = None
        if self._patterns:
            self._spawn()

    def _spawn(self) -> None:
        self._req_q = _MP_CTX.Queue()
        self._resp_q = _MP_CTX.Queue()
        self._proc = _MP_CTX.Process(
            target=_worker_loop,
            args=(self._patterns, self._req_q, self._resp_q),
            daemon=True,
        )
        self._proc.start()

    def _kill(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=2)
            if self._proc.is_alive():
                self._proc.kill()
                self._proc.join(timeout=2)
        # Drop the (possibly poisoned) queues entirely.
        self._proc = None
        self._req_q = None
        self._resp_q = None

    def _search(self, idx: int, text: str) -> tuple[bool, bool]:
        """Return (matched, timed_out) for one pattern against ``text``."""
        if self._proc is None or not self._proc.is_alive():
            # Worker died (e.g. prior kill); bring it back.
            self._spawn()
        try:
            self._req_q.put((idx, text))
            matched = self._resp_q.get(timeout=self.timeout)
            return bool(matched), False
        except queue.Empty:
            # The worker is stuck on a pathological pattern. Kill it and respawn
            # so the next search starts clean.
            self._kill()
            self._spawn()
            return False, True
        except (EOFError, OSError, ValueError):
            # Queue/process broke; respawn and treat as a timeout/skip.
            self._kill()
            self._spawn()
            return False, True

    def evaluate_entry(self, title: str, body: str) -> MatchOutcome:
        title = title or ""
        body = body or ""
        timed_out: set[int] = set(self._uncompilable_ids)

        if not self._valid:
            return MatchOutcome(matched=False, timed_out_keyword_ids=timed_out)

        for stage_name, text in (("title", title), ("body", body)):
            if not text:
                continue
            for idx, kw in enumerate(self._valid):
                matched, did_timeout = self._search(idx, text)
                if did_timeout:
                    log.warning(
                        "Keyword %d (%r) timed out (>%.1fs) on %s; skipping pattern.",
                        kw.keyword_id, kw.pattern, self.timeout, stage_name,
                    )
                    timed_out.add(kw.keyword_id)
                    continue
                if matched:
                    return MatchOutcome(
                        matched=True,
                        timed_out_keyword_ids=timed_out,
                        matched_keyword_id=kw.keyword_id,
                    )

        return MatchOutcome(matched=False, timed_out_keyword_ids=timed_out)

    def close(self) -> None:
        if self._proc is not None and self._proc.is_alive() and self._req_q is not None:
            try:
                self._req_q.put(None)  # graceful shutdown sentinel
                self._proc.join(timeout=2)
            except (OSError, ValueError):
                pass
        self._kill()

    def __enter__(self) -> "TimeoutRegexEvaluator":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def evaluate_entry(
    title: str,
    body: str,
    keywords: list[CompiledKeyword],
    timeout: float,
) -> MatchOutcome:
    """Convenience one-shot evaluation (spins up and tears down a worker).

    Prefer :class:`TimeoutRegexEvaluator` when evaluating many entries so the
    worker process is reused.
    """
    with TimeoutRegexEvaluator(keywords, timeout) as ev:
        return ev.evaluate_entry(title, body)
