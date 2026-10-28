# Contributing

Thanks for your interest in improving Button to Pull Request. This project is a
small, dependency-free bridge between a web page and OpenHands Cloud, so the bar
for contributions is simple: keep it readable, keep it standard-library only, and
keep the behavior easy to reason about.

## Requirements

- Python 3.10 or newer. The bridge uses `X | Y` type syntax in annotations.
- No third-party packages are required. `certifi` is optional and only used to
  fix TLS trust on python.org macOS builds.
- `OPENHANDS_API_KEY` if you want to exercise the Cloud API end to end.
- `GITHUB_TOKEN` if you want the agent sandbox to work with private repos.

## Setup

```bash
git clone https://github.com/WizardingMuggle/openhands.git
cd openhands

# Optional: create an isolated environment.
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# Optional but recommended for real runs.
export OPENHANDS_API_KEY=...
export GITHUB_TOKEN=...
```

There is nothing to install; `server.py` imports only the standard library.

## Running the bridge locally

```bash
python3 server.py
```

Then open <http://localhost:8765>. Useful environment variables:

| Variable            | Default                      | Purpose                                     |
| ------------------- | ---------------------------- | ------------------------------------------- |
| `OPENHANDS_API_KEY` | (unset)                      | Authenticates the bridge to OpenHands Cloud.|
| `GITHUB_TOKEN`      | (unset)                      | Forwarded to the sandbox for private repos. |
| `CLOUD_BASE`        | `https://app.all-hands.dev`  | Overrides the Cloud URL.                    |
| `BRIDGE_PORT`       | `8765`                       | Overrides the local port.                   |

Check that the bridge is up and configured without spending money:

```bash
curl -s http://localhost:8765/api/health
```

## Tests and checks

There is no test suite yet. Run these checks before opening a pull request.

Byte-compile the Python sources to catch syntax errors:

```bash
python3 -m compileall -q server.py
```

Verify the module imports cleanly and the helpers behave as expected:

```bash
python3 - <<'PY'
import server

assert server.extract_pr_url("see https://github.com/o/r/pull/12 for details") == \
    "https://github.com/o/r/pull/12"
assert server.extract_pr_url("no link here") is None
assert server.pr_url_from_record(
    {"pr_number": 7, "selected_repository": "o/r", "git_provider": "github"}, "c1"
) == "https://github.com/o/r/pull/7"
assert server.first_item([{"id": "a"}])["id"] == "a"
assert server.first_item({"items": [{"id": "b"}]})["id"] == "b"
assert "Open a pull request when done." in server.build_prompt("o/r", "main", "Do it")
print("checks passed")
PY
```

Smoke-test the HTTP surface without hitting the Cloud API:

```bash
python3 server.py &            # note the PID
curl -s http://localhost:8765/api/health
curl -s http://localhost:8765/api/status/unknown   # expect 404 {"error":"unknown run"}
kill %1
```

If you change request parsing, also confirm validation still rejects bad input:

```bash
curl -s -X POST http://localhost:8765/api/run \
  -H 'Content-Type: application/json' \
  -d '{"repo":"bad","instruction":"x"}'      # expect 400 owner/name error
```

## Style

- Python standard library only. Do not add runtime dependencies.
- Keep functions small and typed where it helps; use `from __future__ import annotations`.
- Keep comments for non-obvious intent, not for restating the code.
- Match the existing formatting: 4-space indentation and a soft 88-column limit.
- Prefer plain functions and explicit state over abstractions. The whole bridge is
  three files, and it should stay easy to read in one sitting.

## Submitting changes

1. Create a branch: `git checkout -b my-change`.
2. Make your change and run the checks above.
3. Commit with a short, imperative message, e.g. `Fix PR URL detection for GitLab`.
4. Push the branch and open a pull request against `main`.
5. In the pull request, describe what changed, why, and how you verified it.

Note that each end-to-end run starts a real OpenHands conversation and creates a
sandbox, which costs money. Prefer the offline checks above and run the full flow
only when the change touches the Cloud API calls.
