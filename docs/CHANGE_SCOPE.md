# Checks selected by changed files

The toolkit maintains one classifier in `scripts/change_scope.py`. Generated
consumer pipelines receive a verified copy; standalone reusable workflows use
the generated `change-scope.yml` at the same immutable toolkit revision. The
reusable workflow embeds the classifier so it never executes a same-named
script from the consumer repository.

Pull requests compare the complete branch diff from the merge base. Pushes and
merge groups compare the complete before/after range. Classification uses Git
objects, including deleted paths and file modes, without API file-list limits.
Missing revisions, shallow history, unknown events, an empty diff and other
uncertainty require full validation.

Only ordinary Markdown and reStructuredText documentation is exempt by default:
root README, CHANGELOG, CONTRIBUTING, SECURITY, RELEASING and CODE_OF_CONDUCT,
plus documentation below `docs/`. Source, tests, fixtures, assets, templates,
scripts, dependencies, build configuration and hidden directories are never
exempt merely because a filename ends in `.md`. Executable files, symlinks,
submodules and mode changes require full validation. Both sides of a rename are
checked, so renaming source into documentation cannot hide a code change.

## Generated consumer pipelines

`scripts/install_release.py` renders a lightweight **Change scope** job before
validation or release preparation. Documentation-only PRs retain the **CI gate**
check. The gate verifies the scope result and accepts only the skips explicitly
authorized by that result; missing jobs, failures and unexpected skips still fail.

An optional policy section customizes exact paths and independently required
validators:

```json
"change_scope": {
  "documentation_paths": ["vendor/README.md"],
  "required_paths": ["docs/privacy-policy.md"],
  "always_validate_workflows": ["codeql.yml"]
}
```

These lists contain exact paths, not globs. An added documentation path cannot
override protected source or fixture locations. Use `required_paths` for prose
that generates application or deployed content. An always-required validator
must be named in `validation_workflows` and must genuinely succeed on every run.

A documentation-only push stops before release preparation: it allocates no
version, builds no artifacts and publishes no candidate. Existing nightly policy
is unchanged. A reviewed `nightly_cron` policy value (`M H * * *`, UTC) can retain
an existing daily slot when updating older generated workflows; otherwise the
usual deterministic schedule applies. Scheduled jobs and manual dispatch always request full validation;
release callers also pass `force-full: true` to Quality gate, including when the
release source differs only in documentation. Publication still requires real
validation and build evidence.

## Reusable workflows

Pin `.github/workflows/change-scope.yml` to the reviewed toolkit commit when
adding the same classifier to a custom pipeline. Its inputs are `force-full`
(boolean, default false), `documentation-paths` and `required-paths` (JSON arrays
of exact paths). Outputs are `run` (`true` or `false`) and `reason`. Only
`run=false` together with `reason=documentation-only` authorizes a skip.

The language and security reusable workflows keep `force-full: true` by default
for compatibility with release callers. A caller responsible for ordinary
PR/push validation can explicitly set it to false and supply the two path
inputs. Generated Quality gate decides scope before invoking validators, whose
full-validation defaults remain intact.

Do not use workflow-level `paths-ignore` for required checks: a workflow that
never starts cannot produce the gate result. Keep job names and existing review,
security and release requirements unchanged.

A skipped reusable workflow call emits only its caller's skipped context; it
does not emit the nested job contexts that a full run produces. Before enabling
documentation-only skips, verify the existing required checks against an actual
run. After validating equivalent coverage, consolidate legacy required build
and test job contexts into **CI gate**, which verifies their full-run results
and authorizes only proven documentation skips. Retain independently required
security contexts such as CodeQL and Sonar, and keep their real validators active.
The toolkit itself always runs CodeQL, configuration checks and its full-source
security scan. Documentation-only changes skip its separate contract-test workflow.

## Maintaining the classifier

After modifying the canonical script, run `python3 scripts/render_change_scope.py`.
The generated workflow includes its source digest. Contract tests check its
exact embedded source and execute it against temporary consumer repositories.
Run `scripts/install_release.py CONSUMER` to update generated consumers and use
`--check` to reject drift. No copied classifier is maintained independently.

## Activate scope in an existing generated consumer

The presence of a classifier or `scope` job does not mean skipping is enabled.
Without an explicit `change_scope` policy, generated workflows deliberately set
`FORCE_FULL` to true. Review required checks and secret/documentation validation
before opting in: workflows that supply those checks belong in
`always_validate_workflows`. Keep exact `required_paths` for documentation used
as a build or deployment input.

For an existing, recognized generated graph with the current shared classifier,
edit the consumer policy, then run:

```sh
python3 scripts/install_release.py /path/to/consumer --change-scope-only
python3 scripts/install_release.py /path/to/consumer --change-scope-only --check
```

This mode changes only selected scope inputs, validation conditions and the
final gate proof in the existing workflows. It also enables the release entry
point to stop before preparation on documentation-only pushes. Actual release
qualification still passes `force-full: true`; scheduled/manual events remain
full. Action pins, runner selection, triggers, timeouts, release engines, signing
and publication steps remain untouched. No GitHub settings are changed.

The migration rejects custom/legacy graphs, altered classifier commands,
unrecognized classifier copies and aliases of edited YAML nodes. Review those
consumers individually; do not bypass the rejection or replace their whole
release implementation merely to enable documentation routing. After migration,
verify a documentation-only and a source-change run and retain independent
security checks. The CI change itself correctly requests full validation.

The toolkit itself keeps its CodeQL and full-source security scanners active on
documentation changes. This includes secret scanning: prose can contain a secret
even when no source code changed. The aggregate gate rejects a skipped scanner.
The lightweight configuration check also validates the existing source package
inventory, so deleting a required documentation file cannot silently break the
next source release. Editing its contents still uses the documentation route.
