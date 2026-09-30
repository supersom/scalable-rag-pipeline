# scripts/load_test.py
from locust import HttpUser, task, between
import json
import os
import random
import time
from collections import deque
from uuid import uuid4

class RAGUser(HttpUser):
    # Wait 1 to 5 seconds between tasks
    wait_time = between(1, 5)
    
    @task
    def chat_stream_task(self):
        """
        Simulates a user sending a chat message.
        """
        if not hasattr(self, "_recent_session_ids"):
            self._recent_session_ids = deque(maxlen=4)
            self._request_count = 0
        self._request_count += 1
        if self._request_count % 5 == 0:
            session_id = random.choice(tuple(self._recent_session_ids))
        else:
            session_id = str(uuid4())
            self._recent_session_ids.append(session_id)

        headers = {
            "Authorization": f"Bearer {os.getenv('AUTH_TOKEN')}" # Load token from env
        }
        # Four of five requests exercise the agent; the fourth uses the cache normally.
        # The fifth reuses a session and bypasses cache so history is actually loaded.
        if self._request_count % 5 != 4:
            headers["X-Load-Test-Bypass-Cache"] = "1"
        
        # Example query
        payload = {
            "message": "What is the warranty policy for the new X1 processor?",
            "session_id": session_id
        }
        
        # Locust otherwise records the time to response headers, not the full stream.
        started = time.perf_counter()
        with self.client.post(
            "/api/v1/chat/stream", 
            json=payload, 
            headers=headers, 
            stream=True,
            catch_response=True,
            name="/chat/stream" # Group results under this name
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            answer_seen = False
            try:
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("Invalid stream event")
                    if event.get("type") == "error":
                        response.failure("Stream error event")
                        return
                    if event.get("type") == "answer":
                        answer_seen = True
            except (ValueError, OSError) as exc:
                response.failure(f"Invalid response stream ({type(exc).__name__})")
                return
            finally:
                response.request_meta["response_time"] = (time.perf_counter() - started) * 1000
            if answer_seen:
                response.success()
            else:
                response.failure("Missing answer event")
