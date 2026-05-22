import json
import os
import time
import uuid
from typing import Any, Dict, Iterable, List

import httpx
import streamlit as st
from jose import jwt


DEFAULT_API_BASE = os.getenv("API_BASE", "http://localhost:8000")
DEFAULT_JWT_SECRET = os.getenv("JWT_SECRET_KEY", "change_this_to_a_secure_random_string_for_jwt")
DEFAULT_JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")


def make_token(
    user_id: str,
    role: str,
    ttl_seconds: int,
    secret: str,
    algorithm: str,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "role": role,
            "permissions": ["chat"],
            "iat": now,
            "exp": now + ttl_seconds,
        },
        secret,
        algorithm=algorithm,
    )


def reset_session() -> None:
    st.session_state.session_id = str(uuid.uuid4())
    st.session_state.messages = []
    st.session_state.last_events = []


def refresh_token() -> None:
    st.session_state.jwt_token = make_token(
        user_id=st.session_state.user_id,
        role=st.session_state.role,
        ttl_seconds=st.session_state.ttl_seconds,
        secret=st.session_state.jwt_secret,
        algorithm=st.session_state.jwt_algorithm,
    )
    st.session_state.token_refreshed_at = int(time.time())


def ensure_state() -> None:
    defaults = {
        "api_base": DEFAULT_API_BASE,
        "jwt_secret": DEFAULT_JWT_SECRET,
        "jwt_algorithm": DEFAULT_JWT_ALGORITHM,
        "user_id": "streamlit-user",
        "role": "admin",
        "ttl_seconds": 3600,
        "jwt_token": "",
        "token_refreshed_at": 0,
        "session_id": str(uuid.uuid4()),
        "messages": [],
        "last_events": [],
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    if not st.session_state.jwt_token:
        refresh_token()


def stream_chat_events(api_base: str, token: str, session_id: str, message: str) -> Iterable[Dict[str, Any]]:
    url = f"{api_base.rstrip('/')}/api/v1/chat/stream"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "message": message,
        "session_id": session_id,
    }

    with httpx.stream("POST", url, headers=headers, json=payload, timeout=120.0) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"type": "error", "content": f"Could not parse event: {line}"}


def render_history(messages: List[Dict[str, str]]) -> None:
    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])


def render_events(events: List[Dict[str, Any]]) -> None:
    if not events:
        return

    with st.expander("Last request trace", expanded=False):
        for event in events:
            event_type = event.get("type", "event")
            if event_type == "routing":
                action = event.get("action")
                tool = event.get("tool") or "none"
                st.write(f"routing: action={action}, tool={tool}")
            elif event_type == "status":
                st.write(event.get("info") or f"completed {event.get('node')}")
            elif event_type == "error":
                st.error(event.get("content", "Unknown error"))


def sidebar() -> None:
    with st.sidebar:
        st.header("Session")
        st.text_input("API base URL", key="api_base")
        st.text_input("User ID", key="user_id")
        st.selectbox("Role", ["admin", "user"], key="role")
        st.number_input("JWT TTL seconds", min_value=60, max_value=86400, step=60, key="ttl_seconds")

        st.divider()
        st.text_input("JWT algorithm", key="jwt_algorithm")
        st.text_input("JWT secret", type="password", key="jwt_secret")

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("New session", use_container_width=True):
                reset_session()
                refresh_token()
                st.rerun()
        with col_b:
            if st.button("Refresh JWT", use_container_width=True):
                refresh_token()
                st.rerun()

        st.caption(f"Session ID: `{st.session_state.session_id}`")
        if st.session_state.token_refreshed_at:
            refreshed = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(st.session_state.token_refreshed_at),
            )
            st.caption(f"JWT refreshed: {refreshed}")

        st.text_area("Bearer token", key="jwt_token", height=180)


def main() -> None:
    st.set_page_config(page_title="Enterprise RAG Chat", page_icon=":material/chat:", layout="wide")
    ensure_state()

    st.title("Enterprise RAG Chat")
    st.caption("Authenticated chat client for `/api/v1/chat/stream`.")
    sidebar()

    render_history(st.session_state.messages)
    render_events(st.session_state.last_events)

    prompt = st.chat_input("Ask a question")
    if not prompt:
        return

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    answer = ""
    events: List[Dict[str, Any]] = []
    with st.chat_message("assistant"):
        status_slot = st.empty()
        answer_slot = st.empty()
        try:
            for event in stream_chat_events(
                api_base=st.session_state.api_base,
                token=st.session_state.jwt_token,
                session_id=st.session_state.session_id,
                message=prompt,
            ):
                events.append(event)
                event_type = event.get("type")
                if event_type == "routing":
                    action = event.get("action")
                    tool = event.get("tool") or "none"
                    status_slot.info(f"Routing: action={action}, tool={tool}")
                elif event_type == "status":
                    status_slot.info(event.get("info", "Processing..."))
                elif event_type == "answer":
                    answer = event.get("content", "")
                    answer_slot.markdown(answer)
                elif event_type == "error":
                    answer = event.get("content", "An internal error occurred.")
                    answer_slot.error(answer)
            status_slot.empty()
        except httpx.HTTPStatusError as exc:
            answer = f"Request failed: HTTP {exc.response.status_code} - {exc.response.text}"
            answer_slot.error(answer)
        except httpx.RequestError as exc:
            answer = f"Could not reach API: {exc}"
            answer_slot.error(answer)

    st.session_state.last_events = events
    if answer:
        st.session_state.messages.append({"role": "assistant", "content": answer})


if __name__ == "__main__":
    main()
