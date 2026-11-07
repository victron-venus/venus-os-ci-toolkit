## Reviewed test-only automatic pushes

`change_scope.automatic_release_test_paths` opts a versioned repository into a
small, exact allowlist of test-source files. Ordinary default-branch pushes are
eligible only when their complete immutable `before..after` diff contains regular
`100644` additions/modifications entirely within that list. Event file lists and
last-commit-only diffs do not authorize admission.

Preparation still authenticates the current execution, source policy and base
version. It then reuses the existing scheduled-publication proof for the exact
`before` source: durable plan, published tag/source, manifest, complete asset
metadata, successful producer run and CI/Release gates, and retained Actions
evidence. The default-branch head must still equal the current run's `after`
before and after proof. Publisher and concurrency guards are unchanged.

Only this proved predecessor permits `test-only-already-published`, before any
new version reservation, package build or publication. A
`release-push-admission-<run>-<attempt>` artifact retains bounded revision, raw
diff digest, matched test paths and published tag evidence. It is not a test
result and does not replace PR checks.

Missing, pending, failed, expired, changed or unavailable publication evidence
retains the original full release route. So do unknown/mixed paths, documentation,
CI/dependencies/production, deletions, mode changes, renames, forced pushes and
unavailable Git history. Consecutive test-only pushes may run the full route when
the exact preceding test-only commit was not published; admission never assumes
an older production delivery is sufficient. Manual releases, schedules, RC/stable
acceptance and release-quality checks retain their existing behavior.

Adopt the explicit policy, runtime, workflow receipt and tests from one approved
canonical toolkit revision. The full renderer supports the policy. For an
existing supported consumer, `scripts/install_release.py PATH
--release-push-admission-only` migrates only the two reviewed prepare/reuse
functions, their helper and the receipt step. It rejects unknown function or
workflow topology, preserves other publication functions/jobs, and supports
`--check`. Fuzz, attempt, receipt and inventory migrations preserve this admission. The
documentation-scope migration rejects unsupported release-quality topology
without writes. These migrations do not install admission. Review and pin the actual
upstream revision before consumer adoption; no floating or invented pin is valid.
