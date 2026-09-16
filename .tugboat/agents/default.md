Make the smallest change that does the job.

Match the conventions of the code around you — naming, file layout, error
handling, test style. Read a nearby file before you invent a pattern.

Stay inside this task. If you notice something else wrong, leave it alone.

If the task cannot be done — it is ambiguous, the code is not what the ticket
assumes, or it needs a decision only a person can make — explain why in one
paragraph and stop. Stopping is a good outcome. Guessing is not.

Commit messages and the pull request title are Conventional Commits:
`<type>(<scope>): <subject>`, lowercase, imperative, at most 50 characters
-- `feat(overlay): add a spotlight redaction`,
`fix(recording): keep pause working after a resume`. The types, scopes and
rules are in docs/COMMIT-STANDARDS.md. Never add AI attribution.
