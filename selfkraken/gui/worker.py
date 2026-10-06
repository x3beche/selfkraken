"""Background jobs on a ``QThreadPool`` — the UI thread never blocks.

Each job runs :class:`Job` (a ``QRunnable``) which opens **its own**
:class:`~selfkraken.db.Store` from ``load_settings().db_path`` (SQLite
connections are per-thread), calls ``fn(store, settings)`` and closes the
store again.  ``SKError`` messages reach the user verbatim; any other
exception becomes a generic message — no tracebacks, no secrets.

``JobRunner(inline=True)`` runs the same code path synchronously on the
caller's thread; tests use it to drive the full loop without an event loop.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from selfkraken.config import Settings, load_settings, redact
from selfkraken.db import Store
from selfkraken.errors import SKError

#: a job body: runs with its own Store, returns anything the callback wants
JobFn = Callable[[Store, Settings], Any]

GENERIC_FAILURE = "unexpected failure — the action did not complete"


class _JobSignals(QObject):
    """Signals owned by one job; emitted from the worker, handled in the UI."""

    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)


class Job(QRunnable):
    """One background unit of work with result/error signals."""

    def __init__(self, fn: JobFn) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self._fn = fn
        self.signals = _JobSignals()

    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        try:
            settings = load_settings()
        except SKError as exc:
            # load_settings errors name variables only, never values.
            self.signals.failed.emit(str(exc))
            return
        except Exception:
            self.signals.failed.emit(GENERIC_FAILURE)
            return
        store: Optional[Store] = None
        try:
            store = Store(settings.db_path)
            result = self._fn(store, settings)
        except SKError as exc:
            # SKError messages are written for users; keep them, redact just
            # in case a path or value echoed an API key.
            self.signals.failed.emit(redact(str(exc), settings))
            return
        except Exception as exc:
            # Generic: never show a traceback, file contents or secrets.
            self.signals.failed.emit(
                f"{GENERIC_FAILURE} ({type(exc).__name__})"
            )
            return
        finally:
            if store is not None:
                store.close()
        self.signals.succeeded.emit(result)


class JobRunner(QObject):
    """Submits jobs, keeps strong references, reports busy state."""

    started = Signal(str)
    finished = Signal(str)

    def __init__(self, inline: bool = False, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._inline = inline
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._jobs: Dict[int, Job] = {}
        self._pending: Dict[int, tuple] = {}
        self._seq = 0
        self._active = 0

    @property
    def inline(self) -> bool:
        return self._inline

    def active_count(self) -> int:
        return self._active

    def submit(
        self,
        label: str,
        fn: JobFn,
        on_ok: Optional[Callable[[Any], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Run *fn* (in a worker thread, or inline) with callbacks on return."""
        self._seq += 1
        job = Job(fn)
        key = self._seq
        self._jobs[key] = job
        self._pending[key] = (label, on_ok, on_error)
        self._active += 1
        self.started.emit(label)
        job.signals.succeeded.connect(lambda r, k=key: self._on_ok(k, r))
        job.signals.failed.connect(lambda m, k=key: self._on_fail(k, m))
        if self._inline:
            job.run()  # synchronous; signals fire directly (same thread)
        else:
            self._pool.start(job)

    # ------------------------------------------------------------------
    def _settle(self, key: int) -> tuple:
        job = self._jobs.pop(key, None)
        label, on_ok, on_error = self._pending.pop(key, ("?", None, None))
        self._active = max(self._active - 1, 0)
        if job is not None and not self._inline:
            # keep it alive until the pool is done; now it can go
            pass
        return label, on_ok, on_error

    def _on_ok(self, key: int, result: Any) -> None:
        label, on_ok, _ = self._settle(key)
        self.finished.emit(label)
        if on_ok is not None:
            on_ok(result)

    def _on_fail(self, key: int, message: str) -> None:
        label, _, on_error = self._settle(key)
        self.finished.emit(label)
        if on_error is not None:
            on_error(message)

    def wait(self, msec: int = 10_000) -> bool:
        """Block until all pooled jobs finished (used at shutdown/tests)."""
        return self._pool.waitForDone(msec)
