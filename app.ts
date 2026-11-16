/*
 * app.ts - front-end logic for the "Button to Pull Request" bridge page.
 *
 * index.html loads the compiled app.js (committed next to this file), so running
 * the app needs no build step. After editing this file, regenerate app.js with:
 *
 *     npm install && npm run build
 */

// Look up an element by id and return it typed as the element the caller wants.
// Every id used below exists in index.html; throwing on a missing id turns a
// silent null-dereference into an obvious error at load time.
const $ = <T extends HTMLElement>(id: string): T => {
  const el = document.getElementById(id); // null when the id is absent
  if (!el) throw new Error(`missing element #${id}`); // fail fast on bad markup
  return el as T; // narrow HTMLElement to the concrete element type
};

// Run statuses the bridge can report. The badge colour and the poll loop both
// branch on this value, so the union keeps both switches honest.
type RunStatus =
  | "queued" // accepted; the worker has not started yet
  | "starting" // the sandbox is coming up
  | "running" // the agent is working
  | "pr_found" // a pull request URL was found
  | "finished" // the agent ended without opening a PR
  | "error" // the start or run failed
  | "timeout" // the 30 minute ceiling was reached
  | "stuck" // the agent stalled
  | "stopped"; // the agent stopped early

// The fields the page reads from GET /api/status/{run_id}. Extra fields the
// bridge sends (repo, provider, timestamps) are ignored here.
interface RunState {
  run_id: string; // opaque id used to poll
  status: RunStatus; // drives the badge and the loop
  start_task_status?: string; // sandbox setup phase, shown while starting
  execution_status?: string; // agent execution phase, shown while running
  error?: string; // human-readable failure detail
  conversation_ui_url?: string; // link to the OpenHands conversation
  pr_url?: string; // the pull request link once it exists
}

// Response body of POST /api/branches: a list of names, or an error message.
interface BranchesResponse {
  branches?: string[];
  error?: string;
}

// Response body of POST /api/run: the new run id, or an error message.
interface RunResponse {
  run_id?: string;
  error?: string;
}

// Per-provider copy for the form. Switching the provider swaps every hint and
// the token label, and clears any branches loaded for the previous provider.
interface ProviderConfig {
  label: string; // provider name used in messages
  repoPlaceholder: string; // example repository shown in the input
  repoHint: string; // repository format hint
  tokenLabel: string; // token field placeholder
  tokenHint: string; // token field hint
}

// The two providers the bridge understands. The keys must match the <option>
// values in index.html and the provider list in server.py.
const PROVIDERS: Record<string, ProviderConfig> = {
  github: {
    label: "GitHub",
    repoPlaceholder: "octocat/hello-world",
    repoHint: "owner/name",
    tokenLabel: "GitHub token",
    tokenHint:
      "Optional for public repos. Needed for private repos and to list branches.",
  },
  azure_devops: {
    label: "Azure DevOps",
    repoPlaceholder: "contoso/web/PetStore",
    repoHint: "organization/project/repository",
    tokenLabel: "Azure DevOps PAT",
    tokenHint:
      "Required to list branches and to push. Set AZURE_DEVOPS_TOKEN on the server to skip this.",
  },
};

// Holds the pending setTimeout id so a new poll (or a finished run) can cancel
// the previous one instead of stacking overlapping requests.
let pollTimer: number | null = null;

// Repaint the labels and hints for the selected provider and drop any branches
// loaded for the previous one, since branch names are provider-specific.
function applyProvider(): void {
  const cfg = PROVIDERS[$<HTMLSelectElement>("provider").value]; // current provider config
  $<HTMLInputElement>("repo").placeholder = cfg.repoPlaceholder; // example repo
  $<HTMLDivElement>("repo-hint").textContent = `Format: ${cfg.repoHint}`; // format hint
  $<HTMLInputElement>("token").placeholder = cfg.tokenLabel; // token field label
  $<HTMLDivElement>("token-hint").textContent = cfg.tokenHint; // token field hint
  resetBranches(); // the branch list belongs to the old provider
}

// Reset the branch picker to its single default option and clear the hint.
function resetBranches(): void {
  const sel = $<HTMLSelectElement>("branch"); // the base-branch <select>
  sel.innerHTML = '<option value="">Repository default branch</option>'; // only the default
  $<HTMLDivElement>("branch-hint").textContent = ""; // clear any stale hint
}

// Fill the branch picker with the names returned by the bridge.
function renderBranches(branches: string[]): void {
  const sel = $<HTMLSelectElement>("branch"); // the base-branch <select>
  resetBranches(); // start from the default option
  for (const name of branches) {
    const opt = document.createElement("option"); // one option per branch
    opt.value = name; // value submitted with the run
    opt.textContent = name; // label the user sees
    sel.appendChild(opt); // add it to the picker
  }
  // Report the count, or explain that the default branch will be used.
  $<HTMLDivElement>("branch-hint").textContent = branches.length
    ? `${branches.length} branch(es). Pick the base branch for the new PR.`
    : "No branches found. The agent will use the repository default branch.";
}

// Ask the bridge to list branches for the current provider/repo/token. The token
// travels in the POST body, never the URL, so it stays out of access logs.
async function loadBranches(): Promise<void> {
  const provider = $<HTMLSelectElement>("provider").value; // selected provider
  const repo = $<HTMLInputElement>("repo").value.trim(); // repository, trimmed
  const token = $<HTMLInputElement>("token").value.trim(); // optional token
  if (!repo) {
    // Nothing to list without a repository; point the user at the field.
    $<HTMLDivElement>("branch-hint").textContent = "Enter a repository first.";
    return;
  }
  const button = $<HTMLButtonElement>("load-branches"); // the button we are about to block
  button.disabled = true; // prevent a second request while one is in flight
  $<HTMLDivElement>("branch-hint").textContent = "Loading branches..."; // progress hint
  try {
    const resp = await fetch("/api/branches", {
      method: "POST", // POST keeps the token out of the URL
      headers: { "Content-Type": "application/json" }, // JSON body
      body: JSON.stringify({ provider, repo, token }), // the request payload
    });
    const data = (await resp.json()) as BranchesResponse; // { branches } or { error }
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`); // surface the server error
    renderBranches(data.branches || []); // paint the options
  } catch (err) {
    // Any failure (network, 4xx/5xx, bad JSON) lands here with a readable message.
    $<HTMLDivElement>("branch-hint").textContent =
      `Could not load branches: ${(err as Error).message}`;
  } finally {
    button.disabled = false; // always re-enable the button
  }
}

// Map a run status onto one of the three badge colours: green when the run
// succeeded, red for the failure states, amber for anything still in progress.
function badgeClass(status: RunStatus): string {
  if (status === "pr_found" || status === "finished") return "badge ok"; // success
  if (
    status === "error" ||
    status === "stuck" ||
    status === "timeout" ||
    status === "stopped"
  ) {
    return "badge err"; // failure
  }
  return "badge running"; // queued / starting / running
}

// Render the current run into the status area: badge, phase, error, and links.
function render(run: RunState): void {
  const el = $<HTMLElement>("status"); // the status container
  const bits = [`<span class="${badgeClass(run.status)}">${run.status}</span>`]; // badge first
  if (run.status === "starting" && run.start_task_status) {
    bits.push(`setup: ${run.start_task_status}`); // sandbox setup phase
  }
  if (run.execution_status && run.status === "running") {
    bits.push(`execution: ${run.execution_status}`); // agent execution phase
  }
  if (run.error) {
    bits.push(`<div class="kv" style="color:#f87171">${run.error}</div>`); // failure detail
  }
  if (run.conversation_ui_url) {
    // Deep link to the conversation so the user can watch the agent work.
    bits.push(
      `<div class="kv">Conversation: <a href="${run.conversation_ui_url}" target="_blank" rel="noreferrer">${run.conversation_ui_url}</a></div>`,
    );
  }
  if (run.pr_url) {
    // The payoff: the pull request the agent opened.
    bits.push(
      `<div class="kv">Pull request: <a href="${run.pr_url}" target="_blank" rel="noreferrer">${run.pr_url}</a></div>`,
    );
  }
  el.innerHTML = bits.join(" "); // replace the previous status wholesale
}

// Poll the bridge until the run reaches a terminal status. Statuses that keep
// the run alive poll again after 3s; a transport error backs off to 5s so a
// dropped connection does not hammer the server.
function poll(runId: string): void {
  if (pollTimer !== null) clearTimeout(pollTimer); // cancel any pending poll
  fetch(`/api/status/${runId}`) // ask the bridge for the latest state
    .then((r) => r.json()) // parse the JSON snapshot
    .then((run: RunState) => {
      render(run); // paint the snapshot
      // These statuses end the loop: success, or a terminal failure.
      const done = [
        "pr_found",
        "finished",
        "error",
        "timeout",
        "stuck",
        "stopped",
      ].includes(run.status);
      if (!done) {
        pollTimer = window.setTimeout(() => poll(runId), 3000); // keep polling
      } else {
        $<HTMLButtonElement>("go").disabled = false; // allow another run
      }
    })
    .catch(() => {
      pollTimer = window.setTimeout(() => poll(runId), 5000); // back off and retry
    });
}

// Wire the provider switch and the branch loader, then paint the initial state.
$<HTMLSelectElement>("provider").addEventListener("change", applyProvider); // repaint on switch
$<HTMLButtonElement>("load-branches").addEventListener("click", loadBranches); // load on click
applyProvider(); // initial paint for the default provider

// Start a run: validate the form, POST to the bridge, then poll for the PR.
$<HTMLButtonElement>("go").addEventListener("click", async () => {
  const body = {
    provider: $<HTMLSelectElement>("provider").value, // selected provider
    repo: $<HTMLInputElement>("repo").value.trim(), // repository, trimmed
    branch: $<HTMLSelectElement>("branch").value.trim(), // base branch ("" = default)
    token: $<HTMLInputElement>("token").value.trim(), // optional per-run token
    instruction: $<HTMLTextAreaElement>("instruction").value.trim(), // the task
  };
  if (!body.repo || !body.instruction) {
    // The bridge rejects an empty repo/instruction; fail locally instead.
    $<HTMLElement>("status").innerHTML =
      '<span class="badge err">error</span> repo and instruction are required';
    return;
  }
  // Disable the button for the duration of the run so one press maps to one
  // conversation (and one billable sandbox).
  $<HTMLButtonElement>("go").disabled = true;
  $<HTMLElement>("status").innerHTML =
    '<span class="badge running">queued</span>'; // optimistic badge
  try {
    const resp = await fetch("/api/run", {
      method: "POST", // start the run
      headers: { "Content-Type": "application/json" }, // JSON body
      body: JSON.stringify(body), // the form values
    });
    const data = (await resp.json()) as RunResponse; // { run_id } or { error }
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`); // surface the error
    poll(data.run_id as string); // 202 accepted: begin polling
  } catch (err) {
    // A failed POST means the run never started; report it and re-enable.
    $<HTMLElement>("status").innerHTML =
      `<span class="badge err">error</span> ${(err as Error).message}`;
    $<HTMLButtonElement>("go").disabled = false;
  }
});
