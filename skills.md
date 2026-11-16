# Skills

This project exposes one skill: **button-to-pull-request**. It turns a short
instruction into an OpenHands Cloud run that edits a repository and opens a pull
request, then reports the pull request URL back to the caller.

## Skill: button-to-pull-request

### What it does

Given an `owner/repo`, a base branch, and a natural-language instruction, the
bridge (`server.py`) starts an OpenHands Cloud conversation against that
repository. The agent clones the repo, makes the change on a new branch, runs
relevant checks, pushes, and opens a pull request. The bridge polls the
conversation and returns the pull request URL when it appears.

### When to use it

- You want a one-click path from a plain instruction to an opened pull request.
- You need the pull request link reported back programmatically.
- You are embedding a "make this change" button in an existing web app.

### When not to use it

- You only need a chat reply, not a code change. Use OpenHands Cloud directly.
- You need a long-lived, interactive session. Each run is one-shot and costs
  money because it creates a sandbox.
- You need retries or durable state. Run state is in memory and is not retried.

### Inputs

| Field         | Required | Description                                             |
| ------------- | -------- | ------------------------------------------------------- |
| `repo`        | yes      | Repository in `owner/name` form.                         |
| `branch`      | no       | Base branch. Defaults to the repository default branch.  |
| `instruction` | yes      | The task the agent should implement.                     |

### Outputs

A run record with a `status` of `queued`, `starting`, `running`, `pr_found`,
`finished`, `error`, or `timeout`. When `status` is `pr_found`, the record also
contains `pr_url` and `conversation_ui_url`.

### How to invoke it

Start a run:

```bash
curl -s -X POST http://localhost:8765/api/run \
  -H 'Content-Type: application/json' \
  -d '{
        "repo": "owner/name",
        "branch": "main",
        "instruction": "Add a CONTRIBUTING.md with setup and test instructions."
      }'
```

Poll for the result:

```bash
curl -s http://localhost:8765/api/status/<run_id>
```

Check configuration:

```bash
curl -s http://localhost:8765/api/health
```

### Configuration

- `OPENHANDS_API_KEY` (required) authenticates the bridge to OpenHands Cloud.
- `GITHUB_TOKEN` (optional) is forwarded to the sandbox so the agent can clone
  and push private repositories.
- `CLOUD_BASE` overrides the Cloud URL (default `https://app.all-hands.dev`).
- `BRIDGE_PORT` overrides the local port (default `8765`).

### Limits

- One conversation per invocation; the bridge does not retry on failure.
- Polling lasts up to 30 minutes.
- The bridge binds to `127.0.0.1` and is only reachable from the local machine.
- Pull request detection scans the last 100 conversation events, which the API
  caps at 100.

## See also

- `README.md` for setup, embedding, and troubleshooting.
- `server.py` for the bridge implementation and the prompt it sends to the agent.
