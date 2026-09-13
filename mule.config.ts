export default {
  base: "main",
  verify: "python -m pytest -q",
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
  // satisfied; "verify" has Mule merge on green checks, for repositories that
  // cannot have protection. Read what `mule init` prints before turning it on.
  chain: "",

  // Any command that prints a JSON list of tickets. `mule init` detected this one.
  pull: "gh issue list --assignee @me --state open --limit 50 --json number,title,body,url,labels",
}
