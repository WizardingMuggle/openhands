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
  AZURE_DEVOPS_TOKEN  Azure DevOps personal access token, used the same way for
                      Azure DevOps repositories. Optional but recommended.
  CLOUD_BASE          Default: https://app.all-hands.dev
  BRIDGE_PORT         Default: 8765

Per run the page can override the token for the selected provider; the value is
forwarded to the sandbox and never stored or logged by the bridge.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
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

# Providers the bridge knows how to talk to. GitHub is the default so existing
# callers keep working without sending a provider field.
DEFAULT_PROVIDER = "github"
PROVIDERS = ("github", "azure_devops")

# Secret name the sandbox reads for each provider, and the environment variable
# that supplies a default token when the page does not send one.
PROVIDER_SECRET_NAME = {"github": "github_token", "azure_devops": "azure_devops_token"}
PROVIDER_TOKEN_ENV = {"github": "GITHUB_TOKEN", "azure_devops": "AZURE_DEVOPS_TOKEN"}

# Repository format and the matching PR/MR URL shape differ per provider.
# Azure DevOps repositories are organization/project/repository.
REPO_PATTERN = {
    "github": r"[\w.-]+/[\w.-]+",
    "azure_devops": r"[A-Za-z0-9._\- ]+/[A-Za-z0-9._\- ]+/[A-Za-z0-9._\- ]+",
}

PROVIDER_TOKENS = {
    provider: os.environ.get(env, "") for provider, env in PROVIDER_TOKEN_ENV.items()
}

# Conversations are one-shot per button press. State lives in memory.
RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()

PR_PATTERNS = [
    re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+"),
    re.compile(r"https?://[\w.-]+/[\w./-]+/-/merge_requests/\d+"),
    # Azure DevOps: https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/{n}
    re.compile(r"https?://dev\.azure\.com/[\w.-]+/[\w.-]+/_git/[\w.-]+/pullrequest/\d+"),
    # Legacy Azure DevOps host: https://{org}.visualstudio.com/{project}/_git/{repo}/pullrequest/{n}
    re.compile(r"https?://[\w.-]+\.visualstudio\.com/[\w.-]+/_git/[\w.-]+/pullrequest/\d+"),
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


def http_json(
    url: str, headers: dict[str, str], timeout: int = 30
) -> tuple[int, Any]:
    """GET url and return (status, parsed_json_or_text). Never raises for HTTP errors."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            raw = resp.read().decode(errors="replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except urllib.error.URLError as exc:
        raise ApiError(0, f"Could not reach {url}: {exc.reason}") from None


def _github_branches(repo: str, token: str) -> list[str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    status, data = http_json(
        f"https://api.github.com/repos/{repo}/branches?per_page=100", headers
    )
    if status == 200 and isinstance(data, list):
        return [b["name"] for b in data if isinstance(b, dict) and b.get("name")]
    if token and status in (401, 403):
        raise ApiError(status, "GitHub rejected the token while listing branches.")
    return []


def _azure_branches(repo: str, token: str) -> list[str]:
    parts = repo.split("/")
    if len(parts) != 3:
        raise ApiError(400, "Azure DevOps repo must be organization/project/repository")
    org, project, name = parts
    auth = base64.b64encode(f":{token}".encode()).decode()
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/"
        f"{urllib.parse.quote(name)}/refs?filter=heads&api-version=7.1"
    )
    status, data = http_json(url, {"Authorization": f"Basic {auth}"})
    if status != 200:
        raise ApiError(status, str(data))
    refs = data.get("value", []) if isinstance(data, dict) else []
    prefix = "refs/heads/"
    return [r["name"][len(prefix):] for r in refs if str(r.get("name", "")).startswith(prefix)]


def _cloud_branches(repo: str, provider: str) -> list[str]:
    """Fall back to OpenHands Cloud, which lists branches with the account's
    linked provider credentials when the bridge has no token of its own."""
    query = urllib.parse.urlencode(
        {"provider": provider, "repository": repo, "query": "", "limit": 100}
    )
    data = api_request("GET", f"/git/branches/search?{query}")
    items = data.get("items", []) if isinstance(data, dict) else []
    return [b["name"] for b in items if isinstance(b, dict) and b.get("name")]


AZURE_TOKEN_HELP = (
    "Azure DevOps needs a personal access token to list branches. "
    "Paste one, or set AZURE_DEVOPS_TOKEN on the server."
)


def list_branches(provider: str, repo: str, token: str) -> list[str]:
    """Return branch names for repo, using the token when one is available.

    A token is required for private repositories and for Azure DevOps. When no
    token is given, GitHub public repositories still list unauthenticated, and
    other cases fall back to OpenHands Cloud's linked provider credentials.
    """
    token = token or PROVIDER_TOKENS.get(provider, "")

    if provider == "github":
        names = _github_branches(repo, token)
    elif provider == "azure_devops":
        names = _azure_branches(repo, token) if token else []
    else:
        names = []
    if names:
        return sorted(set(names))

    # No token, or the token returned nothing. Try OpenHands Cloud, which uses
    # the provider credentials linked to the account.
    if not token and API_KEY:
        try:
            names = _cloud_branches(repo, provider)
        except ApiError:
            names = []
        if names:
            return sorted(set(names))

    if provider == "azure_devops" and not token:
        raise ApiError(401, AZURE_TOKEN_HELP)
    return []


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
    if provider == "azure_devops":
        return azure_pr_url(repo, number)
    if provider == "github" or provider is None:
        return f"https://github.com/{repo}/pull/{number}"
    return None


def azure_pr_url(repo: str, number: Any) -> str | None:
    """Azure DevOps PR URLs are org/project/_git/repo/pullrequest/n."""
    parts = repo.split("/")
    if len(parts) != 3:
        return None
    org, project, name = parts
    return (
        f"https://dev.azure.com/{org}/{project}/_git/{name}/pullrequest/{number}"
    )


def scan_events_for_pr(
    conversation_id: str, expected_repo: str | None = None, provider: str | None = None
) -> str | None:
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
            if expected_repo and not _url_matches_repo(url, expected_repo, provider):
                continue
            return url
    return None


def _url_matches_repo(url: str, repo: str, provider: str | None = None) -> bool:
    """True if the PR/MR URL belongs to repo (owner/name or org/project/name).

    Each provider interleaves the repository path with its own segments:
    GitHub uses owner/name directly, GitLab inserts /-/, and Azure DevOps
    inserts /_git/ between the project and the repository.
    """
    parts = repo.split("/")
    if provider == "azure_devops" or len(parts) == 3:
        if len(parts) != 3:
            return False
        org, project, name = parts
        return f"/{org}/{project}/_git/{name}/" in url
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


def run_worker(
    run_id: str, prompt: str, repo: str, branch: str | None, provider: str, token: str
) -> None:
    """Start the Cloud conversation and poll it until a PR URL appears or it ends."""
    try:
        payload: dict = {
            "initial_message": {"content": [{"type": "text", "text": prompt}]},
            "selected_repository": repo,
            "title": f"[bridge] {repo}",
        }
        if branch:
            payload["selected_branch"] = branch
        if token:
            # Forward the provider token to the sandbox so the agent can clone and
            # push. Without it, only public repositories work.
            payload["secrets"] = {PROVIDER_SECRET_NAME[provider]: token}
        payload["git_provider"] = provider

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
                conversation_id, expected_repo=repo, provider=provider
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


def build_prompt(repo: str, branch: str | None, instruction: str, provider: str) -> str:
    if provider == "azure_devops":
        pr_url_hint = (
            "https://dev.azure.com/<organization>/<project>/_git/<repository>/pullrequest/<number>"
        )
    else:
        pr_url_hint = "https://github.com/<owner>/<repo>/pull/<number>"
    return (
        "You are working in a git repository. Open a pull request when done.\n\n"
        f"Repository: {repo}\n"
        f"Git provider: {provider}\n"
        f"Base branch: {branch or 'the repository default branch'}\n\n"
        "Task:\n"
        f"{instruction.strip()}\n\n"
        "Requirements:\n"
        "- Clone the repository and create a new branch for your changes.\n"
        "- Implement the task and run any relevant tests or checks.\n"
        "- Commit your changes and push the branch to the remote.\n"
        "- Open a pull request against the base branch.\n"
        "- Finish by printing the full pull request URL on its own line, "
        f"in the form {pr_url_hint}.\n"
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
        path = self.path.partition("?")[0]
        if path in ("/", "/index.html"):
            html = (HERE / "index.html").read_bytes()
            self._send(200, "text/html; charset=utf-8", html)
            return
        if path == "/api/health":
            self._send_json(
                200,
                {
                    "ok": bool(API_KEY),
                    "cloud_base": CLOUD_BASE,
                    "providers": list(PROVIDERS),
                    "tokens": {p: bool(PROVIDER_TOKENS.get(p)) for p in PROVIDERS},
                },
            )
            return
        if path.startswith("/api/status/"):
            run_id = path.rsplit("/", 1)[-1]
            state = get_state(run_id)
            if state is None:
                self._send_json(404, {"error": "unknown run"})
            else:
                self._send_json(200, state)
            return
        self._send_json(404, {"error": "not found"})

    def _handle_branches(self, provider: str, repo: str, token: str) -> None:
        if provider not in PROVIDERS:
            self._send_json(400, {"error": f"provider must be one of {list(PROVIDERS)}"})
            return
        if not re.fullmatch(REPO_PATTERN[provider], repo):
            self._send_json(400, {"error": f"invalid repository for {provider}"})
            return
        try:
            branches = list_branches(provider, repo, token)
        except ApiError as exc:
            self._send_json(502, {"error": str(exc)})
            return
        self._send_json(200, {"branches": branches})

    def do_POST(self):  # noqa: N802
        if self.path not in ("/api/run", "/api/branches"):
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        if self.path == "/api/branches":
            # POST keeps the token out of the URL (and therefore out of logs).
            self._handle_branches(
                (payload.get("provider") or DEFAULT_PROVIDER).strip(),
                (payload.get("repo") or "").strip(),
                (payload.get("token") or "").strip(),
            )
            return

        provider = (payload.get("provider") or DEFAULT_PROVIDER).strip()
        repo = (payload.get("repo") or "").strip()
        instruction = (payload.get("instruction") or "").strip()
        branch = (payload.get("branch") or "").strip() or None
        token = (payload.get("token") or "").strip() or PROVIDER_TOKENS.get(provider, "")

        if provider not in PROVIDERS:
            self._send_json(400, {"error": f"provider must be one of {list(PROVIDERS)}"})
            return
        if not re.fullmatch(REPO_PATTERN[provider], repo):
            example = (
                "organization/project/repository"
                if provider == "azure_devops"
                else "owner/name"
            )
            self._send_json(400, {"error": f"repo must look like {example}"})
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
                "provider": provider,
                "repo": repo,
                "branch": branch,
                "created_at": time.time(),
            }
        prompt = build_prompt(repo, branch, instruction, provider)
        threading.Thread(
            target=run_worker,
            args=(run_id, prompt, repo, branch, provider, token),
            daemon=True,
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
    configured = ", ".join(
        f"{p}={'yes' if PROVIDER_TOKENS.get(p) else 'no'}" for p in PROVIDERS
    )
    print(f"Provider tokens from the environment: {configured}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
