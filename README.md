# Button to Pull Request

A tiny local web app with one button. Pressing it starts an OpenHands Cloud run
against a repository of your choice and reports the opened pull request back on
the page. It works with GitHub and Azure DevOps repositories.

```
your browser  ->  local bridge (server.py, port 8765)  ->  OpenHands Cloud API
       ^                                                          |
       +---------------- status + PR link <-----------------------+
```

The page is served by the bridge, so there is nothing to install and no
frontend build step. Only the Python standard library is used.

## How it works

1. You pick a git provider (GitHub or Azure DevOps), fill in the repository, an
   optional personal access token, a base branch, and an instruction, then press
   the button. Press **Load branches** to pick the base branch from a list.
2. The bridge (`server.py`) calls `POST /api/v1/app-conversations` on OpenHands Cloud
   with that repository and an instruction to open a pull request.
3. The bridge polls the conversation, streams status back to the page, and scans
   the conversation events for a pull request URL.
4. The page shows a live status badge, a link to the OpenHands conversation, and
   the pull request link when it appears.

## Setup

Set the environment variables, then run the bridge:

```bash
export OPENHANDS_API_KEY=...      # OpenHands Cloud API key
export GITHUB_TOKEN=...           # optional, needed for private GitHub repos
export AZURE_DEVOPS_TOKEN=...     # optional, needed for Azure DevOps repos
python3 server.py
```

Open <http://localhost:8765> in your browser and press the button.

- `OPENHANDS_API_KEY` authenticates the bridge to OpenHands Cloud.
- `GITHUB_TOKEN` / `AZURE_DEVOPS_TOKEN` are forwarded to the agent sandbox so it
  can clone and push. They also let the page list branches. Without a token, only
  public GitHub repositories work and Azure DevOps cannot list branches. You can
  also paste a token into the page for a single run instead of exporting it.
- `CLOUD_BASE` overrides the Cloud URL (default `https://app.all-hands.dev`).
- `BRIDGE_PORT` overrides the local port (default `8765`).

### Repository format

- GitHub: `owner/name`, for example `octocat/hello-world`.
- Azure DevOps: `organization/project/repository`, for example
  `contoso/web/PetStore` (the same shape OpenHands uses). Azure DevOps Services
  at `https://dev.azure.com` is supported; Azure DevOps Server is not.

## Embedding in an existing web app

If your app already has a frontend, skip `index.html` and call the bridge from
your own button:

```js
const resp = await fetch("http://localhost:8765/api/run", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    provider: "github",            // "github" (default) or "azure_devops"
    repo: "owner/name",
    branch: "main",
    token: "",                     // optional; overrides the server token
    instruction: "Add a CONTRIBUTING.md with setup and test instructions.",
  }),
});
const { run_id } = await resp.json();

// Poll for status and the PR URL.
const run = await (await fetch(`http://localhost:8765/api/status/${run_id}`)).json();
// run.status: queued | starting | running | pr_found | finished | error | timeout
// run.pr_url: present once a pull request is detected
```

To list branches for the base-branch picker:

```js
const branches = await (await fetch("http://localhost:8765/api/branches", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ provider: "github", repo: "owner/name", token: "" }),
})).json();  // { branches: ["main", "release/1.0", ...] }
```

If your app runs on a different origin, add a CORS header to the bridge or proxy
`/api/run`, `/api/branches` and `/api/status/*` through your own server.

## API

| Method | Path                 | Purpose                                            |
| ------ | -------------------- | -------------------------------------------------- |
| POST   | `/api/run`           | Start a run. Body: `provider`, `repo`, `branch`, `token`, `instruction`. |
| POST   | `/api/branches`      | List branches. Body: `provider`, `repo`, `token`.   |
| GET    | `/api/status/{id}`   | Current status of a run, including `pr_url`.        |
| GET    | `/api/health`        | Whether the API key is set and which provider tokens the server has. |

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
- While the sandbox is starting, the page shows the setup phase (for example
  `SETTING_UP_SKILLS`, `PREPARING_REPOSITORY`). If the sandbox cannot start, the
  bridge reports the reason right away instead of waiting for the 30 minute
  timeout. The most common reason is a missing or rejected token: Azure DevOps
  always needs one, and private GitHub repositories do too.
- The bridge polls for up to 30 minutes. It prefers the conversation record's
  `pr_number` field, which is authoritative, and builds the PR URL from the
  provider (`github` -> `/pull/`, `azure_devops` -> `/_git/.../pullrequest/`).
  As a fallback it scans only agent-produced events (messages, actions,
  observations) and accepts a link only if it points at the repository you asked
  for. Conversation state events are deliberately excluded because they embed the
  full agent config, whose loaded skill documentation contains example PR URLs
  from unrelated repos.
- Branch listing uses the provider API directly (`api.github.com` or
  `dev.azure.com`). Azure DevOps always needs a token; GitHub can list public
  repositories without one. If neither the page nor the server has a token, the
  bridge falls back to OpenHands Cloud's `/git/branches/search`, which uses the
  credentials linked to your account.
- Tokens are sent in request bodies, not URLs, so they stay out of access logs.
  The bridge does not store or log them; it forwards a token to the sandbox for
  the run only.
- The events endpoint caps `limit` at 100, which is what the fallback uses.
- The bridge binds to `127.0.0.1`, so it is only reachable from your machine.
