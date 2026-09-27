"""Executor accounting for blocking SDK calls, using real threads and events."""
import asyncio
import logging
import threading
import time

import pytest

from services.api.app.evaluation.bounded_io import BoundedIO, BoundedIOClosed, BoundedIOTimeout

WAIT = 5.0  # Upper bound for any event wait; tests never rely on reaching it.


def _io_threads(prefix):
    return [t for t in threading.enumerate() if t.name.startswith(prefix)]


async def _until(predicate, timeout=WAIT):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError('condition not reached within bounded wait')
        await asyncio.sleep(0.005)


def _blocking(started: threading.Event, release: threading.Event, exited: threading.Event | None = None):
    def fn():
        started.set()
        try:
            if not release.wait(WAIT):
                raise AssertionError('test release event was never set')
            return 'released'
        finally:
            if exited is not None:
                exited.set()
    return fn


@pytest.fixture
def io_factory():
    created = []

    def make(workers, **kwargs):
        io = BoundedIO(workers, **kwargs)
        created.append(io)
        return io
    yield make
    for io in created:
        asyncio.run(io.close(time.monotonic() + WAIT))
        assert io.in_flight == 0


async def cancelled_waiter_contract(io, events):
    a_started, a_release, a_exited, b_started, b_release, c_started, c_release = events
    deadline = time.monotonic() + WAIT
    task_a = asyncio.create_task(io.call(_blocking(a_started, a_release, a_exited), deadline=deadline))
    task_b = asyncio.create_task(io.call(_blocking(b_started, b_release), deadline=deadline))
    await _until(lambda: a_started.is_set() and b_started.is_set())
    assert io.in_flight == 2

    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a
    # The await is gone but its blocking SDK call still holds the slot.
    assert not a_exited.is_set()
    assert io.in_flight == 2

    task_c = asyncio.create_task(io.call(_blocking(c_started, c_release), deadline=deadline))
    await asyncio.sleep(0.2)
    assert not c_started.is_set()
    assert io.waiting == 1
    assert len(_io_threads('evidence-test-io')) == 2
    assert io._executor._work_queue.qsize() == 0

    a_release.set()
    await _until(c_started.is_set)
    assert a_exited.is_set()
    assert len(_io_threads('evidence-test-io')) == 2
    assert io._executor._work_queue.qsize() == 0

    b_release.set()
    c_release.set()
    assert await task_b == 'released'
    assert await task_c == 'released'
    assert io.in_flight == 0 and io.waiting == 0


def test_cancelled_waiter_keeps_slot_until_sdk_call_exits(io_factory):
    io = io_factory(2, thread_name_prefix='evidence-test-io')
    events = tuple(threading.Event() for _ in range(7))
    try:
        asyncio.run(cancelled_waiter_contract(io, events))
    finally:
        for event in events:
            event.set()


async def hung_after_close_contract(io, started, release):
    with pytest.raises(BoundedIOTimeout):
        await io.call(_blocking(started, release), deadline=time.monotonic() + 0.1)
    assert started.is_set()
    remaining = await io.close(time.monotonic() + 0.1)
    assert remaining == 1
    return asyncio.get_running_loop()


def test_hung_call_finishing_after_loop_close_releases_quietly(io_factory, caplog):
    thread_errors = []
    previous_hook = threading.excepthook
    threading.excepthook = thread_errors.append
    io = io_factory(1, thread_name_prefix='evidence-hung-io')
    started, release = threading.Event(), threading.Event()
    try:
        with caplog.at_level(logging.DEBUG):
            loop = asyncio.run(hung_after_close_contract(io, started, release))
            assert loop.is_closed()
            assert io.in_flight == 1  # Accounting survives the loop that submitted the call.
            release.set()
            end = time.monotonic() + WAIT
            while io.in_flight and time.monotonic() < end:
                time.sleep(0.005)
            assert io.in_flight == 0
            for thread in _io_threads('evidence-hung-io'):
                thread.join(WAIT)
            assert _io_threads('evidence-hung-io') == []
    finally:
        release.set()
        threading.excepthook = previous_hook
    assert thread_errors == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_closed_io_rejects_new_work(io_factory):
    io = io_factory(1)
    calls = []

    async def contract():
        assert await io.close(time.monotonic() + 1) == 0
        with pytest.raises(BoundedIOClosed):
            await io.call(lambda: calls.append(1), deadline=time.monotonic() + 1)
    asyncio.run(contract())
    assert calls == []


def test_expired_deadline_never_starts_work(io_factory):
    io = io_factory(1, clock=lambda: 10.0)
    calls = []
    with pytest.raises(BoundedIOTimeout):
        asyncio.run(io.call(lambda: calls.append(1), deadline=10.0))
    assert calls == [] and io.in_flight == 0


async def permit_timeout_contract(io, started, release, calls):
    holder = asyncio.create_task(io.call(_blocking(started, release), deadline=time.monotonic() + WAIT))
    await _until(started.is_set)
    with pytest.raises(BoundedIOTimeout):
        await io.call(lambda: calls.append(1), deadline=time.monotonic() + 0.1)
    assert io.waiting == 0 and io.in_flight == 1
    release.set()
    assert await holder == 'released'


def test_deadline_while_waiting_for_permit_never_starts_work(io_factory):
    io = io_factory(1)
    started, release, calls = threading.Event(), threading.Event(), []
    try:
        asyncio.run(permit_timeout_contract(io, started, release, calls))
    finally:
        release.set()
    assert calls == [] and io.in_flight == 0


def test_results_and_exceptions_propagate(io_factory):
    io = io_factory(2)

    def boom():
        raise LookupError('synthetic')

    async def contract():
        assert await io.call(lambda: 42, deadline=time.monotonic() + 1) == 42
        with pytest.raises(LookupError):
            await io.call(boom, deadline=time.monotonic() + 1)
    asyncio.run(contract())
    assert io.in_flight == 0


def test_synchronous_submit_failure_releases_permit(io_factory, monkeypatch):
    io = io_factory(1)

    def refuse(*args, **kwargs):
        raise RuntimeError('cannot schedule new futures')
    monkeypatch.setattr(io._executor, 'submit', refuse)
    with pytest.raises(RuntimeError):
        asyncio.run(io.call(lambda: None, deadline=time.monotonic() + 1))
    assert io.in_flight == 0
    monkeypatch.undo()
    assert asyncio.run(io.call(lambda: 'ok', deadline=time.monotonic() + 1)) == 'ok'


def test_close_fails_queued_waiters(io_factory):
    io = io_factory(1)
    started, release, calls = threading.Event(), threading.Event(), []

    async def contract():
        holder = asyncio.create_task(io.call(_blocking(started, release), deadline=time.monotonic() + WAIT))
        await _until(started.is_set)
        queued = asyncio.create_task(io.call(lambda: calls.append(1), deadline=time.monotonic() + WAIT))
        await _until(lambda: io.waiting == 1)
        closing = asyncio.create_task(io.close(time.monotonic() + WAIT))
        with pytest.raises(BoundedIOClosed):
            await queued
        release.set()
        assert await closing == 0
        assert await holder == 'released'
    try:
        asyncio.run(contract())
    finally:
        release.set()
    assert calls == []


def test_workers_must_be_positive():
    with pytest.raises(ValueError):
        BoundedIO(0)
