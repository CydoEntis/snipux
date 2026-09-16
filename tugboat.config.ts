export default {
  // Work lands on dev; main moves only when dev is released.
  base: "dev",
  // uv rather than python: a fresh worktree has no .venv, and Tugboat installs no
  // Python packages. conftest.py sets the offscreen Qt platform itself.
  verify: "uv run --no-project --with-requirements requirements.txt python -m pytest -q",
  maxTasks: 10,
  maxHours: 4,
  concurrency: 1,

  // Which coding agent does the work, and what to call the model you want from it.
  // "claude" or "codex", or your own: { command: ["wrapper", "--model={model}", "{promptFile}"] }
  // — where {model}, {prompt} and {promptFile} are the only placeholders, a runner
  // naming none of them is handed the prompt on stdin, and model: "" means whichever
  // model that runner defaults to.
  runner: "claude",
  model: "sonnet",

  // Carry on down the dependency chain unattended, merging as it goes. Off by
  // default. "protected" asks GitHub to merge once this branch's protection is
  // satisfied; "verify" has Tugboat merge on green checks, for repositories that
  // cannot have protection. Read what `tug init` prints before turning it on.
  chain: "",

  // Any command that prints a JSON list of tickets. `tug init` detected this one.
  // needs-desk marks a ticket that can only be worked by watching a real desktop,
  // which an unattended agent cannot do.
  pull: "gh issue list --assignee=@me --state=open --limit=50 --search=-label:needs-desk --json=number,title,body,url,labels",
}
