"""Live integration test: real HTTP server, real worker process, real SQLite DB.

Unlike test_agent_relay.py, which drives the app in-process via FastAPI's
TestClient, this test launches ``uvicorn`` and the deterministic worker as
separate OS processes -- exactly as a user would run them per README.md --
and talks to them only over the network. It exercises the full documented
flow (register two agents, submit a task, run the worker, observe
completion) against the actual API surface and an on-disk SQLite file, then
confirms the result by reading that file directly rather than trusting the
API's own read path.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_ready(base_url: str, process: subprocess.Popen, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server process exited early with code {process.returncode}")
        try:
            response = httpx.get(f"{base_url}/ready", timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_exc = exc
        time.sleep(0.1)
    raise RuntimeError(f"server never became ready: {last_exc}")


@pytest.fixture
def live_server(tmp_path: Path):
    db_path = tmp_path / "agent-relay-live.db"
    port = free_port()
    env = {**os.environ, "RELAY_DATABASE_URL": f"sqlite:///{db_path}"}
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_until_ready(base_url, process)
        yield base_url, env, db_path
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_worker_process_completes_task_via_real_http_and_sqlite(live_server):
    base_url, env, db_path = live_server

    sender = httpx.post(f"{base_url}/api/v1/agents", json={"name": "alice"}, timeout=5).json()
    recipient = httpx.post(f"{base_url}/api/v1/agents", json={"name": "uppercase"}, timeout=5).json()

    created = httpx.post(
        f"{base_url}/api/v1/tasks",
        headers={"Authorization": f"Bearer {sender['token']}"},
        json={"to": recipient["agent_id"], "input": "hello world"},
        timeout=5,
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    worker = subprocess.run(
        [
            sys.executable,
            "main.py",
            "worker",
            "--base-url",
            base_url,
            "--agent-id",
            recipient["agent_id"],
            "--token",
            recipient["token"],
            "--worker-id",
            "integration-test-worker",
            "--wait-seconds",
            "5",
            "--stop-after",
            "1",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert worker.returncode == 0, worker.stderr

    response = httpx.get(
        f"{base_url}/api/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {sender['token']}"},
        timeout=5,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["output"] == "HELLO WORLD"

    # Confirm persistence independently of the API's own read path.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT status, output FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    assert row == ("completed", "HELLO WORLD")
