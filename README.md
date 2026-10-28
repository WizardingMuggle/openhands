# Button to Pull Request

A tiny local web app. A click counter at the top of the page tracks how many
times you pressed it during the current session (it starts at 0 and resets on
refresh). The main button starts an OpenHands Cloud run against a repository of
your choice and reports the opened pull request back on the page.

```
your browser  ->  local bridge (server.py, port 8765)  ->  OpenHands Cloud API
       ^                                                          |
       +---------------- status + PR link <-----------------------+
```

The page is served by the bridge, so there is nothing to install and no
frontend build step. Only the Python standard library is used.

## How it works

1. You fill in `owner/repo`, a base branch, and an instruction, then press the button.
2. The bridge (`server.py`) calls `POST /api/v1/app-conversations` on OpenHands Cloud
   with that repository and an instruction to open a pull request.
3. The bridge polls the conversation, streams status back to the page, and scans
   the conversation events for a pull request URL.
4. The page shows a live status badge, a link to the OpenHands conversation, and
   the pull request link when it appears.

## Setup

Set two environment variables, then run the bridge:

```bash
export OPENHANDS_API_KEY=...   # OpenHands Cloud API key
export GITHUB_TOKEN=...        # optional, needed for private repos
python3 server.py
```

Open <http://localhost:8765> in your browser and press the button.

- `OPENHANDS_API_KEY` authenticates the bridge to OpenHands Cloud.
- `GITHUB_TOKEN` is forwarded to the agent sandbox so it can clone and push.
  Without it, only public repositories work.
- `CLOUD_BASE` overrides the Cloud URL (default `https://app.all-hands.dev`).
- `BRIDGE_PORT` overrides the local port (default `8765`).

## Embedding in an existing web app

If your app already has a frontend, skip `index.html` and call the bridge from
your own button:

```js
const resp = await fetch("http://localhost:8765/api/run", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    repo: "owner/name",
    branch: "main",
    instruction: "Add a CONTRIBUTING.md with setup and test instructions.",
  }),
});
const { run_id } = await resp.json();

// Poll for status and the PR URL.
const run = await (await fetch(`http://localhost:8765/api/status/${run_id}`)).json();
// run.status: queued | starting | running | pr_found | finished | error | timeout
// run.pr_url: present once a pull request is detected
```

If your app runs on a different origin, add a CORS header to the bridge or proxy
`/api/run` and `/api/status/*` through your own server.

## API

| Method | Path                 | Purpose                                            |
| ------ | -------------------- | -------------------------------------------------- |
| POST   | `/api/run`           | Start a run. Body: `repo`, `branch`, `instruction`. |
| GET    | `/api/status/{id}`   | Current status of a run, including `pr_url`.        |
| GET    | `/api/health`        | Whether the API key and GitHub token are configured.|

## Troubleshooting

### `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`

Python from python.org on macOS ships without a CA trust store, so every HTTPS
call fails verification. Fix it in one of these ways:

```bash
pip install certifi          # the bridge picks this up automatically
```

or point Python at a CA bundle you already have:

```bash
export SSL_CERT_FILE=/etc/ssl/cert.pem   # macOS: also try $(brew --prefix)/etc/ca-certificates/cert.pem
```

Alternatively, run the macOS "Install Certificates.command" script that ships
with the python.org installer (in `/Applications/Python 3.x/`).

## Notes and limits

- Each press starts one conversation, which creates a sandbox and costs money.
  The bridge does not retry on failure.
- Run state is in memory; restarting the bridge clears it.
- The bridge polls for up to 30 minutes. It prefers the conversation record's
  `pr_number` field, which is authoritative. As a fallback it scans only
  agent-produced events (messages, actions, observations) and accepts a link
  only if it points at the repository you asked for. Conversation state events
  are deliberately excluded because they embed the full agent config, whose
  loaded skill documentation contains example PR URLs from unrelated repos.
- The events endpoint caps `limit` at 100, which is what the fallback uses.
- The bridge binds to `127.0.0.1`, so it is only reachable from your machine.
