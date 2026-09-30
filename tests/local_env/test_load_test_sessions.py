import runpy
import sys
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]


class Response:
    status_code = 200

    def __init__(self, lines=None):
        self.lines = lines or [b'{"type":"answer","content":"answer"}']
        self.failed = None
        self.succeeded = False
        self.request_meta = {"response_time": 0.0}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        return self.lines

    def success(self):
        self.succeeded = True

    def failure(self, message):
        self.failed = message


class Client:
    def __init__(self, lines=None):
        self.calls = []
        self.responses = []
        self.lines = lines

    def post(self, path, **kwargs):
        self.calls.append((path, kwargs))
        response = Response(self.lines)
        self.responses.append(response)
        return response


def _load_user(monkeypatch):
    locust = ModuleType("locust")
    locust.HttpUser = type("HttpUser", (), {})
    locust.task = lambda method: method
    locust.between = lambda start, end: (start, end)
    monkeypatch.setitem(sys.modules, "locust", locust)
    return runpy.run_path(str(REPO_ROOT / "scripts/load_test.py"))["RAGUser"]


def test_load_scenario_rotates_sessions_and_reuses_one_on_every_fifth_request(monkeypatch):
    user_type = _load_user(monkeypatch)
    user = user_type()
    user.client = Client()
    for _ in range(10):
        user.chat_stream_task()

    calls = [kwargs for _, kwargs in user.client.calls]
    session_ids = [call["json"]["session_id"] for call in calls]
    assert len(set(session_ids[:4])) == 4
    assert session_ids[4] in session_ids[:4]
    assert len(set(session_ids[5:9])) == 4
    assert not set(session_ids[5:9]) & set(session_ids[:4])
    assert session_ids[9] in session_ids[5:9]
    assert [call["headers"].get("X-Load-Test-Bypass-Cache") for call in calls] == [
        "1", "1", "1", None, "1", "1", "1", "1", None, "1",
    ]
    assert {call["json"]["message"] for call in calls} == {
        "What is the warranty policy for the new X1 processor?"
    }


def test_load_scenario_keeps_session_history_per_simulated_user(monkeypatch):
    user_type = _load_user(monkeypatch)
    first, second = user_type(), user_type()
    first.client, second.client = Client(), Client()
    for _ in range(5):
        first.chat_stream_task()
        second.chat_stream_task()
    first_ids = [call[1]["json"]["session_id"] for call in first.client.calls]
    second_ids = [call[1]["json"]["session_id"] for call in second.client.calls]
    assert first_ids[4] in first_ids[:4]
    assert second_ids[4] in second_ids[:4]
    assert not set(first_ids) & set(second_ids)


def test_load_scenario_counts_stream_error_as_failure(monkeypatch):
    user_type = _load_user(monkeypatch)
    user = user_type()
    user.client = Client(lines=[b'{"type":"error","content":"An internal error occurred."}'])
    user.chat_stream_task()

    response = user.client.responses[0]
    assert user.client.calls[0][1]["catch_response"] is True
    assert response.failed == "Stream error event"
    assert response.succeeded is False


def test_load_scenario_counts_completed_answer_as_success(monkeypatch):
    user_type = _load_user(monkeypatch)
    user = user_type()
    user.client = Client()
    user.chat_stream_task()

    response = user.client.responses[0]
    assert response.succeeded is True
    assert response.failed is None
    assert response.request_meta["response_time"] > 0
