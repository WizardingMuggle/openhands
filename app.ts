// Client-side controller for the Button to PR page.
// It drives the local bridge in server.py by starting a run,
// polling its status, and rendering the pull request link.

// Providers the bridge understands, keyed by the value sent
// in the "provider" field of every request. GitHub is the
// default so existing callers keep working unchanged.
export type Provider = "github" | "azure_devops";

// Field labels shown in the form, one entry per provider.
// Keeping them in one map avoids scattering provider checks
// across the DOM update code below.
interface ProviderConfig {
  label: string;
  repoPlaceholder: string;
  repoHint: string;
  tokenLabel: string;
  tokenHint: string;
}

// Status values the bridge can report for a run.
// "pr_found" and "finished" are terminal successes, while
// the remaining terminal values are failures.
export type RunStatus =
  | "queued"
  | "starting"
  | "running"
  | "pr_found"
  | "finished"
  | "error"
  | "timeout"
  | "stuck"
  | "stopped";

// Snapshot of a run as returned by GET /api/status/{id}.
// Optional fields appear only once the bridge learns them,
// so the renderer guards every optional access.
export interface Run {
  status: RunStatus;
  start_task_status?: string;
  execution_status?: string;
  error?: string;
  conversation_ui_url?: string;
  pr_url?: string;
}

// Response of POST /api/run: the id the page polls
// for status updates until the run reaches a terminal state.
interface StartResponse {
  run_id: string;
}

// Response of POST /api/branches, used to fill
// the base branch picker before a run starts.
interface BranchesResponse {
  branches: string[];
}

// Body sent to /api/run and /api/branches.
// The token is optional and, when present,
// overrides the token configured on the server.
interface RunRequest {
  provider: Provider;
  repo: string;
  branch: string;
  token: string;
  instruction: string;
}

// Provider metadata shared by the form labels and hints,
// keyed by the provider values the bridge accepts.
const PROVIDERS: Record<Provider, ProviderConfig> = {
  github: {
    label: "GitHub",
    repoPlaceholder: "octocat/hello-world",
    repoHint: "owner/name",
    tokenLabel: "GitHub token",
    tokenHint: "Optional for public repos. Needed for private repos and to list branches.",
  },
  azure_devops: {
    label: "Azure DevOps",
    repoPlaceholder: "contoso/web/PetStore",
    repoHint: "organization/project/repository",
    tokenLabel: "Azure DevOps PAT",
    tokenHint: "Required to list branches and to push. Set AZURE_DEVOPS_TOKEN on the server to skip this.",
  },
};

// Statuses that end the poll loop. While the status is not
// in this list the page keeps asking the bridge for updates.
const TERMINAL_STATUSES: RunStatus[] = [
  "pr_found",
  "finished",
  "error",
  "timeout",
  "stuck",
  "stopped",
];

// Poll every three seconds while a run is active, then back
// off to five seconds after a transient network error so a
// briefly unreachable bridge does not spin the loop.
const POLL_INTERVAL_MS = 3000;
const POLL_RETRY_MS = 5000;

let pollTimer: ReturnType<typeof setTimeout> | undefined;

// Small typed wrapper around getElementById.
// It throws early when the markup is missing an element,
// turning silent null errors into a clear message.
function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Missing element #${id}`);
  return node as T;
}

// Copy the selected provider's labels into the form and
// clear the stale branch list, so the page never shows
// branches that belong to a previously chosen repository.
function applyProvider(): void {
  const cfg = PROVIDERS[el<HTMLSelectElement>("provider").value as Provider];
  el<HTMLInputElement>("repo").placeholder = cfg.repoPlaceholder;
  el("repo-hint").textContent = `Format: ${cfg.repoHint}`;
  el<HTMLInputElement>("token").placeholder = cfg.tokenLabel;
  el("token-hint").textContent = cfg.tokenHint;
  resetBranches();
}

// Return the branch picker to its single default option and
// drop any hint text left over from an earlier lookup.
function resetBranches(): void {
  const sel = el<HTMLSelectElement>("branch");
  sel.innerHTML = '<option value="">Repository default branch</option>';
  el("branch-hint").textContent = "";
}

// Replace the branch options with the list from the bridge
// and report the count so the user knows the lookup worked.
function renderBranches(branches: string[]): void {
  const sel = el<HTMLSelectElement>("branch");
  resetBranches();
  for (const name of branches) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  }
  el("branch-hint").textContent = branches.length
    ? `${branches.length} branch(es). Pick the base branch for the new PR.`
    : "No branches found. The agent will use the repository default branch.";
}

// Ask the bridge for the repository's branches. The button
// is disabled during the call so a slow provider cannot
// queue several lookups, and errors land in the hint text.
async function loadBranches(): Promise<void> {
  const provider = el<HTMLSelectElement>("provider").value as Provider;
  const repo = el<HTMLInputElement>("repo").value.trim();
  const token = el<HTMLInputElement>("token").value.trim();
  if (!repo) {
    el("branch-hint").textContent = "Enter a repository first.";
    return;
  }
  const button = el<HTMLButtonElement>("load-branches");
  button.disabled = true;
  el("branch-hint").textContent = "Loading branches...";
  try {
    const resp = await fetch("/api/branches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, repo, token }),
    });
    const data = (await resp.json()) as Partial<BranchesResponse> & { error?: string };
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    renderBranches(data.branches || []);
  } catch (err) {
    el("branch-hint").textContent = `Could not load branches: ${(err as Error).message}`;
  } finally {
    button.disabled = false;
  }
}

// Map a run status onto a CSS class for the status badge.
// Success is green, failures are red, and anything still
// in flight keeps the default running colour.
function badgeClass(status: RunStatus): string {
  if (status === "pr_found" || status === "finished") return "badge ok";
  if (status === "error" || status === "stuck" || status === "timeout" || status === "stopped") {
    return "badge err";
  }
  return "badge running";
}

// Build the status panel from the latest run snapshot.
// Each optional field is appended only when present, so the
// panel grows as the bridge learns more about the run.
function render(run: Run): void {
  const bits = [`<span class="${badgeClass(run.status)}">${run.status}</span>`];
  if (run.status === "starting" && run.start_task_status) {
    bits.push(`setup: ${run.start_task_status}`);
  }
  if (run.execution_status && run.status === "running") bits.push(`execution: ${run.execution_status}`);
  if (run.error) bits.push(`<div class="kv" style="color:#f87171">${run.error}</div>`);
  if (run.conversation_ui_url) {
    bits.push(
      `<div class="kv">Conversation: <a href="${run.conversation_ui_url}" target="_blank" rel="noreferrer">${run.conversation_ui_url}</a></div>`,
    );
  }
  if (run.pr_url) {
    bits.push(
      `<div class="kv">Pull request: <a href="${run.pr_url}" target="_blank" rel="noreferrer">${run.pr_url}</a></div>`,
    );
  }
  el("status").innerHTML = bits.join(" ");
}

// Poll the bridge until the run reaches a terminal status.
// Each tick re-arms a timer instead of using an interval, so
// a slow response can never stack overlapping requests.
function poll(runId: string): void {
  clearTimeout(pollTimer);
  fetch(`/api/status/${runId}`)
    .then((r) => r.json() as Promise<Run>)
    .then((run) => {
      render(run);
      if (!TERMINAL_STATUSES.includes(run.status)) {
        pollTimer = setTimeout(() => poll(runId), POLL_INTERVAL_MS);
      } else {
        el<HTMLButtonElement>("go").disabled = false;
      }
    })
    .catch(() => {
      pollTimer = setTimeout(() => poll(runId), POLL_RETRY_MS);
    });
}

// Handle a press of the main button: validate the form,
// start a run, and hand the new run id to the poll loop.
// The button stays disabled until the run finishes.
async function startRun(): Promise<void> {
  const body: RunRequest = {
    provider: el<HTMLSelectElement>("provider").value as Provider,
    repo: el<HTMLInputElement>("repo").value.trim(),
    branch: el<HTMLSelectElement>("branch").value.trim(),
    token: el<HTMLInputElement>("token").value.trim(),
    instruction: el<HTMLTextAreaElement>("instruction").value.trim(),
  };
  if (!body.repo || !body.instruction) {
    el("status").innerHTML = '<span class="badge err">error</span> repo and instruction are required';
    return;
  }
  el<HTMLButtonElement>("go").disabled = true;
  el("status").innerHTML = '<span class="badge running">queued</span>';
  try {
    const resp = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = (await resp.json()) as Partial<StartResponse> & { error?: string };
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    poll(data.run_id as string);
  } catch (err) {
    el("status").innerHTML = `<span class="badge err">error</span> ${(err as Error).message}`;
    el<HTMLButtonElement>("go").disabled = false;
  }
}

// Wire the page up once the document is parsed: apply the
// default provider, then attach the three event handlers.
document.addEventListener("DOMContentLoaded", () => {
  el<HTMLSelectElement>("provider").addEventListener("change", applyProvider);
  el<HTMLButtonElement>("load-branches").addEventListener("click", () => void loadBranches());
  el<HTMLButtonElement>("go").addEventListener("click", () => void startRun());
  applyProvider();
});
