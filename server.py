#!/usr/bin/env python3
"""Local button-to-PR bridge for OpenHands Cloud.

Your web app runs on your machine. Pressing the button calls POST /api/run here.
This server starts an OpenHands Cloud conversation against the selected repo with
a prompt that instructs the agent to open a pull request. It then polls the
conversation and streams status back to the page until it finds the PR URL.

Only the Python standard library is required.

Environment:
  OPENHANDS_API_KEY   OpenHands Cloud API key (falls back to OPENHANDS_CLOUD_API_KEY)
  GITHUB_TOKEN        GitHub token the agent uses to clone/push. Optional but
                      strongly recommended for private repos.
  CLOUD_BASE          Default: https://app.all-hands.dev
  BRIDGE_PORT         Default: 8765
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

CLOUD_BASE = os.environ.get("CLOUD_BASE", "https://app.all-hands.dev").rstrip("/")
API_KEY = os.environ.get("OPENHANDS_API_KEY") or os.environ.get("OPENHANDS_CLOUD_API_KEY") or "sk-oh-X07oeOzlFPPx3uJcgsCuaVXsrZ1ocNKl"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
PORT = int(os.environ.get("BRIDGE_PORT", "8765"))

# Conversations are one-shot per button press. State lives in memory.
RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()

PR_PATTERNS = [
    re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+"),
    re.compile(r"https?://[\w.-]+/[\w./-]+/-/merge_requests/\d+"),
]

# Only these event kinds describe what the agent actually did. State/system
# events (e.g. ConversationStateUpdateEvent) carry the full conversation config,
# whose loaded skill documentation contains example PR URLs from unrelated repos.
AGENT_EVENT_KINDS = {"MessageEvent", "ActionEvent", "ObservationEvent"}


def build_ssl_context() -> ssl.SSLContext:
    """Return a verifying SSL context, preferring certifi's CA bundle.

    Python from python.org on macOS ships without a CA trust store, so HTTPS
    fails with CERTIFICATE_VERIFY_FAILED. certifi (or an explicit SSL_CERT_FILE)
    provides the missing roots.
    """
    env_file = os.environ.get("SSL_CERT_FILE")
    if env_file and os.path.exists(env_file):
        return ssl.create_default_context(cafile=env_file)
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CONTEXT = build_ssl_context()


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


def api_request(
    method: str, path: str, body: dict | None = None, timeout: int = 60
) -> Any:
    """Call the Cloud API. Returns parsed JSON (a dict or a list)."""
    if not API_KEY:
        raise ApiError(0, "OPENHANDS_API_KEY is not set in the server environment.")
    url = f"{CLOUD_BASE}/api/v1{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {API_KEY}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.read().decode(errors="replace")) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise ApiError(
                0,
                "TLS certificate verification failed. Python has no CA trust store. "
                "Fix: pip install certifi, or set SSL_CERT_FILE to a CA bundle "
                f"(original: {reason}).",
            ) from None
        raise ApiError(0, f"Could not reach {url}: {reason}") from None


def first_item(data: Any) -> dict:
    """Batch endpoints with ids= return a bare list; search endpoints return {items: [...]}."""
    if isinstance(data, list):
        return data[0] if data else {}
    if isinstance(data, dict):
        items = data.get("items") or []
        return items[0] if items else {}
    return {}


def find_pr_urls(text: str) -> list[str]:
    """All PR/MR URLs in text, in order of appearance."""
    return [m.group(0) for pattern in PR_PATTERNS for m in pattern.finditer(text)]


def extract_pr_url(text: str) -> str | None:
    urls = find_pr_urls(text)
    return urls[0] if urls else None


def pr_url_from_record(record: dict, conversation_id: str) -> str | None:
    """Prefer the structured pr_number field over scanning event text."""
    raw = record.get("pr_number")
    if isinstance(raw, list):
        number = raw[-1] if raw else None
    else:
        number = raw
    repo = record.get("selected_repository")
    provider = record.get("git_provider")
    if not number or not repo:
        return None
    if provider == "gitlab":
        return f"https://gitlab.com/{repo}/-/merge_requests/{number}"
    if provider == "github" or provider is None:
        return f"https://github.com/{repo}/pull/{number}"
    return None


def scan_events_for_pr(conversation_id: str, expected_repo: str | None = None) -> str | None:
    """Fallback: look for a PR URL in what the agent actually produced.

    Two filters prevent false positives:
    - only agent-originated event kinds are scanned, so config/state blobs (whose
      loaded skill docs contain example PR URLs) are ignored;
    - when expected_repo is given, only links to that repository are accepted.

    The events endpoint caps limit at 100.
    """
    data = api_request(
        "GET", f"/conversation/{conversation_id}/events/search?limit=100"
    )
    if not isinstance(data, dict):
        return None
    for event in data.get("items", []):
        if event.get("kind") not in AGENT_EVENT_KINDS:
            continue
        for url in find_pr_urls(json.dumps(event)):
            if expected_repo and not _url_matches_repo(url, expected_repo):
                continue
            return url
    return None


def _url_matches_repo(url: str, repo: str) -> bool:
    """True if the PR/MR URL belongs to repo (owner/name)."""
    return f"/{repo}/" in url or url.rstrip("/").endswith(f"/{repo}")


def set_state(run_id: str, **fields) -> None:
    with RUNS_LOCK:
        if run_id in RUNS:
            RUNS[run_id].update(fields)
            RUNS[run_id]["updated_at"] = time.time()


def get_state(run_id: str) -> dict | None:
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        return dict(run) if run else None


def run_worker(run_id: str, prompt: str, repo: str, branch: str | None) -> None:
    """Start the Cloud conversation and poll it until a PR URL appears or it ends."""
    try:
        payload: dict = {
            "initial_message": {"content": [{"type": "text", "text": prompt}]},
            "selected_repository": repo,
            "title": f"[bridge] {repo}",
        }
        if branch:
            payload["selected_branch"] = branch
        if GITHUB_TOKEN:
            # Forward the GitHub token to the sandbox so the agent can clone/push
            # private repos. Without it, only public repos work.
            payload["secrets"] = {"github_token": GITHUB_TOKEN}
            payload["git_provider"] = "github"

        start = api_request("POST", "/app-conversations", payload, timeout=90)
        start_task_id = start.get("id")
        conversation_id = start.get("app_conversation_id")
        set_state(
            run_id,
            status="starting",
            start_task_id=start_task_id,
            conversation_id=conversation_id,
        )

        deadline = time.time() + 60 * 30  # 30 minute ceiling
        while time.time() < deadline:
            time.sleep(4)
            if not conversation_id:
                task = first_item(
                    api_request(
                        "GET", f"/app-conversations/start-tasks?ids={start_task_id}"
                    )
                )
                conversation_id = task.get("app_conversation_id")
                if conversation_id:
                    set_state(run_id, conversation_id=conversation_id, status="starting")
                else:
                    continue

            record = first_item(
                api_request("GET", f"/app-conversations?ids={conversation_id}")
            )
            execution_status = record.get("execution_status") or "running"
            set_state(
                run_id,
                status="running",
                execution_status=execution_status,
                conversation_ui_url=record.get("conversation_ui_url")
                or f"{CLOUD_BASE}/canvas/conversations/{conversation_id}",
            )

            pr_url = pr_url_from_record(record, conversation_id) or scan_events_for_pr(
                conversation_id, expected_repo=repo
            )
            if pr_url:
                set_state(run_id, status="pr_found", pr_url=pr_url)
                return

            if execution_status in ("finished", "error", "stopped", "stuck"):
                set_state(run_id, status=execution_status)
                return

        set_state(run_id, status="timeout", error="Timed out waiting for a pull request.")
    except ApiError as exc:
        set_state(run_id, status="error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        set_state(run_id, status="error", error=f"{type(exc).__name__}: {exc}")


def build_prompt(repo: str, branch: str | None, instruction: str) -> str:
    return (
        "You are working in a git repository. Open a pull request when done.\n\n"
        f"Repository: {repo}\n"
        f"Base branch: {branch or 'the repository default branch'}\n\n"
        "Task:\n"
        f"{instruction.strip()}\n\n"
        "Requirements:\n"
        "- Clone the repository and create a new branch for your changes.\n"
        "- Implement the task and run any relevant tests or checks.\n"
        "- Commit your changes and push the branch to the remote.\n"
        "- Open a pull request against the base branch.\n"
        "- Finish by printing the full pull request URL on its own line, "
        "in the form https://github.com/<owner>/<repo>/pull/<number>.\n"
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "OHBridge/1.0"

    def log_message(self, fmt, *args):  # keep the console readable
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict) -> None:
        self._send(code, "application/json", json.dumps(obj).encode())

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            html = (HERE / "index.html").read_bytes()
            self._send(200, "text/html; charset=utf-8", html)
            return
        if self.path == "/api/health":
            self._send_json(
                200,
                {
                    "ok": bool(API_KEY),
                    "cloud_base": CLOUD_BASE,
                    "github_token": bool(GITHUB_TOKEN),
                },
            )
            return
        if self.path.startswith("/api/status/"):
            run_id = self.path.rsplit("/", 1)[-1]
            state = get_state(run_id)
            if state is None:
                self._send_json(404, {"error": "unknown run"})
            else:
                self._send_json(200, state)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/api/run":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        repo = (payload.get("repo") or "").strip()
        instruction = (payload.get("instruction") or "").strip()
        branch = (payload.get("branch") or "").strip() or None

        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            self._send_json(400, {"error": "repo must look like owner/name"})
            return
        if not instruction:
            self._send_json(400, {"error": "instruction is required"})
            return
        if not API_KEY:
            self._send_json(500, {"error": "OPENHANDS_API_KEY is not set on the server"})
            return

        run_id = uuid.uuid4().hex
        with RUNS_LOCK:
            RUNS[run_id] = {
                "run_id": run_id,
                "status": "queued",
                "repo": repo,
                "branch": branch,
                "created_at": time.time(),
            }
        prompt = build_prompt(repo, branch, instruction)
        threading.Thread(
            target=run_worker, args=(run_id, prompt, repo, branch), daemon=True
        ).start()
        self._send_json(202, {"run_id": run_id})


def main() -> None:
    if not API_KEY:
        print(
            "WARNING: OPENHANDS_API_KEY is not set. /api/run will fail.\n"
            "         export OPENHANDS_API_KEY=... then restart.",
            file=sys.stderr,
        )
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Bridge running at http://localhost:{PORT}")
    print(f"Cloud API base: {CLOUD_BASE}")
    print(f"GitHub token forwarded to sandbox: {bool(GITHUB_TOKEN)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
