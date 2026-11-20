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

1. Pick a task type, a git provider (GitHub or Azure DevOps), fill in the
   repository, an optional personal access token, and a base branch, then press
   the button. Press **Load branches** to pick the base branch from a list.
2. The bridge (`server.py`) calls `POST /api/v1/app-conversations` on OpenHands Cloud
   with that repository and a task-specific instruction.
3. The bridge polls the conversation, streams status back to the page, and scans
   the conversation events for a pull request URL.
4. The page shows a live status badge, a link to the OpenHands conversation, and
   the pull request link when it appears.

## Use cases

### 1. User story → pull request

Fill in a user story and (optionally) acceptance criteria, one per line. The
bridge combines them into a single structured prompt: the agent implements the
story, satisfies every criterion, and opens a pull request.

### 2. PR checker (POC)

Paste the URL of an existing pull request and optional acceptance criteria. The
agent checks out the PR, reviews the diff, runs available tests/linters, and
posts its findings as a comment on the PR itself (using your token). No new PR
is opened; the page shows the agent's report, which ends with a final verdict
line (`VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`).

### 3. Dependency update (POC: JavaScript only)

Pick the JavaScript manifest (`package.json`). The agent bumps every dependency
and devDependency to the latest release within the **same major version**
(4.x.x → 4.y.z), updates the lockfile, runs the test/build script if present,
and opens a pull request with the bumps. Other languages are not supported yet.

### 4. Nightly PR patrol (POC)

One run reviews the open pull requests in a repository (oldest first, up to a
max): for each PR the agent checks it out, reviews the diff, runs quick
tests/linters, and posts one verdict comment on the PR. Nothing is merged or
closed. The page shows the final patrol report ending with
`PATROL SUMMARY: <approved> of <total> approved`.

Run it on demand with task type `pr_patrol` (the *Max PRs* field), or schedule
it once a day by setting `NIGHTLY_PATROL` on the server:

```bash
export NIGHTLY_PATROL='{"provider": "github", "repo": "owner/name", "hour": 2}'
```

Optional keys: `branch`, `token`, `max_prs` (default 10). POC simplifications:
the scheduler checks the local hour every 10 minutes and keeps only an
in-memory "already ran today" flag, so a bridge restart may run it twice, and
results are only visible on each run's conversation link.

### 5. Impact / risk analysis (POC)

Paste a proposed change (or user story). The agent clones the repository and
explores it **read-only**, then reports which components, files, tests,
configs, and docs the change would touch, the risks with severities, and
suggested tests, ending with a final `RISK: LOW | MEDIUM | HIGH` line. No PR is
opened and no files are modified. Use it before sprint planning to flag risky
stories. Task type: `impact` (the proposed change goes in the user-story
field, focus areas in the acceptance-criteria field).

## Setup

Set the environment variables, then run the bridge:

```bash
export OPENHANDS_API_KEY=...      # OpenHands Cloud API key
export GITHUB_TOKEN=...           # optional, needed for private GitHub repos
export AZURE_DEVOPS_TOKEN=...     # optional, needed for Azure DevOps repos
export NIGHTLY_PATROL='{"provider":"github","repo":"owner/name","hour":2}'
                                  # optional, schedules the nightly PR patrol (see below)
python3 server.py
```

Open <http://localhost:8765> in your browser and press the button.

- `OPENHANDS_API_KEY` authenticates the bridge to OpenHands Cloud.
- `GITHUB_TOKEN` / `AZURE_DEVOPS_TOKEN` are forwarded to the agent sandbox so it
  can clone and push. They also let the page list branches. Without a token, only
  public GitHub repositories work and Azure DevOps cannot list branches. You can
  also paste a token into the page instead of exporting it: the page has one
  token field per provider and sends the one matching the selected provider,
  so a GitHub and an Azure DevOps token can both be filled in at once. A small
  line under the token fields shows which tokens the server already holds from
  its environment. Page tokens win over server tokens for a single run, and
  tokens are sent in request bodies only (never URLs or logs).
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
    task_type: "user_story",       // "user_story" (default), "pr_check", "deps", "pr_patrol", "impact"
    user_story: "As a new contributor, I want a CONTRIBUTING.md so I can set up and test the project.",
    acceptance_criteria: "- A CONTRIBUTING.md exists at the repository root\n- It documents setup and test instructions",
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
| POST   | `/api/run`           | Start a run. Body: `provider`, `repo`, `branch`, `token`, `task_type`, and per-task fields: `user_story` + `acceptance_criteria` (user_story, impact), `pr_url` + `acceptance_criteria` (pr_check), `dep_language` (deps), `max_prs` (pr_patrol). |
| POST   | `/api/branches`      | List branches. Body: `provider`, `repo`, `token`.   |
| GET    | `/api/status/{id}`   | Current status of a run, including `pr_url` and `summary` (agent's final report). |
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
