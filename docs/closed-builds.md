# Closed builds in the public release workflow

An application can opt into a reviewed local worker with this release policy:

```json
"closed_build": {"workflow": "closed-build.yml", "environment": "closed-build", "enabled": false}
```

Land and test the worker with `enabled: false`, then change the reviewed policy to
`enabled: true` after synthetic acceptance and protected input configuration.
The flag must be an explicit JSON boolean; missing or unknown values fail closed.
Disabled normal releases preserve the public flow and skip closed jobs. Explicit
closed requests and `closed-rebuild` are rejected while disabled, never ignored.
The request job freezes this source-controlled selection for the release graph.
The worker is a reusable
workflow with one `ubuntu-24.04` job in the literal `closed-build` environment.
Its string inputs are `action`, `closed_request`, `release_plan_artifact`,
`release_tag`, and `public_context_artifact`. It reads narrowly scoped secrets
from that protected environment, does not inherit caller secrets, and has only
read access to repository contents and Actions metadata. The wrapper must contain
all closed output, including failures, outside public logs, artifacts and caches.

Normal releases run `closed_build` after the unchanged public Release gate and
before candidate or final publication. Required failure, cancellation, or skip
blocks publication. A disabled policy accepts only a skipped closed dependency.
Stable promotion first resolves the exact public RC, then calls the worker with
`action=validate` to verify and reuse the accepted closed RC bytes without rebuilding.

`operation=closed-rebuild` requires an exact `existing_release` tag. It skips public
version allocation, build, validation and publication. The secretless resolver
checks the release's complete immutable inventory, retained Actions evidence,
successful source run, current attempt and both public gates before staging public
inputs for `action=rebuild`. Missing or expired evidence requires a newly qualified
public release; a closed rebuild cannot repair a failed source run.

`closed_request` is an optional opaque identifier of at most 80 ASCII letters,
digits, underscores or hyphens, starting with a letter or digit. Empty stays empty
in the public graph; the protected worker selects the approved default envelope.
It is not a URL, path, command, credential or authorization. The worker must verify
the signed request and exact input/upgrade intent independently.

The public context artifact contains only `context.json`, `version-plan.json`, and
the verified flat public `payloads/` inventory. Context binds repository, workflow,
decimal-string source run ID/attempt, source SHA, plan digest, payload sizes/digests,
mode and exact public release tag. The worker must authenticate the resolver's
current run/attempt and producer identity before consuming that artifact.

The workflow keeps `cancel-in-progress: false` and opts into `queue: max`.
[GitHub limits this queue to 100 pending runs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
Clients must retain durable requests, distinguish deliberate cancellation from
overflow, and bound retries. Queueing does not guarantee completion.

Until actionlint supports this GitHub field, `scripts/actionlint_compat.py`
validates literal queue values and rejects `max` with cancellation enabled. It
blanks only validated queue entries in an in-memory lint copy, preserving source
paths, positions, all other diagnostics and the original workflow bytes. The
canonical CI and fleet validators use this wrapper; no linter warnings are ignored.

A stored closed candidate is not install approval. The deployment client must
verify matching actual public publication and successful final run/attempt before
acceptance. This includes publisher or post-job failures after the closed build,
and successful publisher results that reused or superseded another plan.
