# Contributing

Thanks for helping improve Button to Pull Request. This guide covers local
setup, the way we work as a team, and the checks a change must pass before it
lands.

## What this project is

A small local web app with one button. The page (`index.html`) is served by a
Python bridge (`server.py`) that starts an OpenHands Cloud run and reports the
pull request it opened. The whole backend is the Python standard library, so
there is no dependency to install and no build step.

```
index.html  ->  server.py (127.0.0.1:8765)  ->  OpenHands Cloud API
```

## Individual setup

### Prerequisites

- Python 3.10 or newer (`python3 --version`).
- An OpenHands Cloud API key.
- Optional: a GitHub token, and/or an Azure DevOps personal access token, if you
  want to work against private repositories or list Azure DevOps branches.

### Run it locally

```bash
git clone https://github.com/wizardingMuggle/openhands.git
cd openhands

export OPENHANDS_API_KEY=...      # required
export GITHUB_TOKEN=...           # optional
export AZURE_DEVOPS_TOKEN=...     # optional

python3 server.py
```

Open <http://localhost:8765>. The bridge binds to `127.0.0.1` only, so nothing is
exposed to your network. `CLOUD_BASE` overrides the Cloud URL and `BRIDGE_PORT`
overrides the port.

### Smoke checks

There is no test suite, so verify your change by hand:

```bash
python3 -c "import ast; ast.parse(open('server.py').read())"   # syntax
python3 server.py &                                            # start the bridge
curl -s http://localhost:8765/api/health                       # bridge is up
curl -s -X POST http://localhost:8765/api/branches \
  -H 'Content-Type: application/json' \
  -d '{"provider":"github","repo":"octocat/hello-world"}'
```

Then press the button in the browser and confirm the status badge advances and a
pull request link appears. A run costs money, so use a scratch repository.

### Secrets

Never commit tokens, `.env` files, or `__pycache__/`; they are covered by
`.gitignore`. The bridge forwards a token to the sandbox for one run and does not
store or log it. Keep it that way in new code: send tokens in request bodies,
never in URLs or query strings.

## Working as a team

### Branches and commits

- Branch from `main`; never commit directly to it. Use a descriptive name such
  as `add-contributing-guide` or `fix/azure-branch-listing`.
- Keep one logical change per branch so review stays small.
- Write imperative commit subjects ("Add branch fallback"), with a body that
  explains why when the reason is not obvious from the diff.

### Pull requests

- One pull request per change. Rebase or merge `main` into your branch instead of
  opening a replacement pull request.
- Describe what changed, why, and how you verified it. Include the manual checks
  you ran; reviewers cannot see your browser.
- Link the issue the work resolves, if there is one.
- Do not merge your own pull request without a second pair of eyes. A maintainer
  merges once review is approved and the manual checks pass.

### Reviewing

- Read the diff against the acceptance criteria, not just for style.
- Check the failure paths: missing tokens, invalid repository formats, provider
  errors, and the 30 minute polling timeout.
- Be specific and kind. Suggest a concrete change when you request one, and say
  so when something is fine as is.

### Reporting bugs

Open an issue with the provider, the repository shape you used, the exact steps,
what you expected, and what happened. Redact tokens and private repository names.
Bridge logs go to stderr; include the relevant lines.

## Code conventions

- Standard library only. Adding a dependency needs a clear justification in the
  pull request.
- Keep `server.py` readable: small functions, type hints on public helpers, and
  short comments only where the reason is not obvious from the code.
- `index.html` stays a single self-contained file with no build step.
- Update `README.md` when behaviour, environment variables, or API endpoints
  change. Document user-visible changes, not the history of the change.

## Adding a provider

1. Add the provider to `PROVIDERS` and its repository pattern to
   `REPO_PATTERN`.
2. Map its token secret name and environment variable in
   `PROVIDER_SECRET_NAME` and `PROVIDER_TOKEN_ENV`.
3. Add branch listing, and add a PR/MR URL pattern to `PR_PATTERNS` plus the
   record-based URL builder.
4. Extend the page's provider selector in `index.html`.
5. Update `README.md` and the `/api/health` provider list expectations.
