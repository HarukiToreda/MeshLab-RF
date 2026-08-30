from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Iterable, Iterator
from typing import Generic, TypeVar


Input = TypeVar("Input")
Output = TypeVar("Output")


class DaemonTask(Generic[Output]):
    """A small future whose worker cannot hold the process open at shutdown."""

    def __init__(self, function: Callable[[], Output], *, name: str) -> None:
        self._done = threading.Event()
        self._value: Output | None = None
        self._error: Exception | None = None

        def worker() -> None:
            try:
                self._value = function()
            except Exception as error:  # noqa: BLE001 - re-raised by result()
                self._error = error
            finally:
                self._done.set()

        threading.Thread(target=worker, name=name, daemon=True).start()

    def result(self) -> Output:
        self._done.wait()
        if self._error is not None:
            raise self._error
        return self._value  # type: ignore[return-value]


def daemon_map_as_completed(
    function: Callable[[Input], Output],
    items: Iterable[Input],
    *,
    max_workers: int,
    name: str,
) -> Iterator[tuple[Input, Output]]:
    """Run bounded parallel work without non-daemon executor shutdown waits."""
    pending = list(items)
    if not pending:
        return
    tasks: queue.Queue[tuple[int, Input]] = queue.Queue()
    completed: queue.Queue[tuple[int, Output | None, Exception | None]] = queue.Queue()
    for index, item in enumerate(pending):
        tasks.put((index, item))

    def worker() -> None:
        while True:
            try:
                index, item = tasks.get_nowait()
            except queue.Empty:
                return
            try:
                completed.put((index, function(item), None))
            except Exception as error:  # noqa: BLE001 - re-raised on the caller thread
                completed.put((index, None, error))

    worker_count = min(max(1, max_workers), len(pending))
    for index in range(worker_count):
        threading.Thread(
            target=worker,
            name=f"{name}{index + 1}",
            daemon=True,
        ).start()

    for _completed_count in range(len(pending)):
        index, value, error = completed.get()
        if error is not None:
            raise error
        yield pending[index], value  # type: ignore[misc]
