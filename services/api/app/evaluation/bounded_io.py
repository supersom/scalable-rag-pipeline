"""A fixed-size executor for blocking SDK calls with permit accounting that outlives awaits.

A permit is taken before submission and returned only when the thread-pool future
completes. Cancelling or timing out the awaiting coroutine never frees a slot while
its blocking call still runs, so the executor queue never holds a backlog.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import TypeVar

T = TypeVar("T")


class BoundedIOClosed(RuntimeError):
    def __init__(self) -> None:
        super().__init__("bounded IO is closed")


class BoundedIOTimeout(TimeoutError):
    def __init__(self) -> None:
        super().__init__("bounded IO deadline expired")


class BoundedIO:
    def __init__(self, workers: int, *, clock: Callable[[], float] = time.monotonic,
                 thread_name_prefix: str = "evidence-io") -> None:
        if workers <= 0:
            raise ValueError("workers must be positive")
        self.workers = workers
        self._clock = clock
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_name_prefix)
        # A threading lock, not asyncio primitives: releases happen on worker threads and
        # must stay correct after the submitting event loop has closed.
        self._lock = Lock()
        self._busy = 0
        self._waiters: deque[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = deque()
        self._closed = False

    @property
    def in_flight(self) -> int:
        return self._busy

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    async def call(self, fn: Callable[[], T], *, deadline: float) -> T:
        loop = asyncio.get_running_loop()
        # Convert the injected-clock deadline to loop time once, so every wait is finite.
        end = loop.time() + (deadline - self._clock())
        if end <= loop.time():
            raise BoundedIOTimeout()
        await self._acquire(loop, end)
        try:
            future = self._executor.submit(fn)
        except BaseException:
            self._release()
            if self._closed:
                raise BoundedIOClosed() from None
            raise
        result = loop.create_future()
        future.add_done_callback(lambda done: self._finished(done, loop, result))
        try:
            async with asyncio.timeout_at(end):
                # Cancelling `result` detaches this await only; the permit follows `future`.
                return await result
        except TimeoutError:
            raise BoundedIOTimeout() from None

    async def close(self, deadline: float) -> int:
        """Stop admitting work, wait until the deadline for in-flight calls, return the residue."""
        loop = asyncio.get_running_loop()
        end = loop.time() + (deadline - self._clock())
        with self._lock:
            self._closed = True
            waiters = list(self._waiters)
            self._waiters.clear()
        for owner, waiter in waiters:
            self._notify(owner, waiter, BoundedIOClosed())
        while self._busy and loop.time() < end:
            await asyncio.sleep(min(0.01, max(0.0, end - loop.time())))
        # Never join threads on the event loop; residual calls end on their socket timeouts
        # and still return their permits through the future callback.
        self._executor.shutdown(wait=False, cancel_futures=True)
        return self._busy

    async def _acquire(self, loop: asyncio.AbstractEventLoop, end: float) -> None:
        with self._lock:
            if self._closed:
                raise BoundedIOClosed()
            if self._busy < self.workers and not self._waiters:
                self._busy += 1
                return
            waiter = loop.create_future()
            entry = (loop, waiter)
            self._waiters.append(entry)
        try:
            async with asyncio.timeout_at(end):
                await waiter
        except BaseException as exc:
            with self._lock:
                queued = entry in self._waiters
                if queued:
                    self._waiters.remove(entry)
            # A permit handed over before we gave up is ours to return. If the hand-over
            # callback has not run yet, it sees the cancelled waiter and returns it itself.
            if not queued and waiter.done() and not waiter.cancelled() and waiter.exception() is None:
                self._release()
            if isinstance(exc, TimeoutError):
                raise BoundedIOTimeout() from None
            raise

    def _release(self) -> None:
        while True:
            with self._lock:
                if not self._waiters:
                    self._busy -= 1
                    return
                loop, waiter = self._waiters.popleft()
            # The permit transfers to the waiter without dropping the busy count.
            if self._notify(loop, waiter, None):
                return

    def _notify(self, loop: asyncio.AbstractEventLoop, waiter: asyncio.Future,
                error: BaseException | None) -> bool:
        def deliver() -> None:
            if waiter.done():
                if error is None:
                    self._release()
            elif error is None:
                waiter.set_result(None)
            else:
                waiter.set_exception(error)
        try:
            loop.call_soon_threadsafe(deliver)
            return True
        except RuntimeError:  # The waiter's loop has closed; nobody can take the permit there.
            return False

    def _finished(self, done: Future, loop: asyncio.AbstractEventLoop, result: asyncio.Future) -> None:
        self._release()

        def transfer() -> None:
            if result.done():
                return
            if done.cancelled():
                result.cancel()
            elif (error := done.exception()) is not None:
                result.set_exception(error)
            else:
                result.set_result(done.result())
        try:
            loop.call_soon_threadsafe(transfer)
        except RuntimeError:  # Loop closed after the caller gave up; the outcome is abandoned.
            pass
