# Static-analysis review notes

Keep executable source in the analysis scope. Assess findings individually and
record the affected flow, its validation and the evidence for any false-positive
decision. A documented assessment does not close an issue in the scanning service.

## Release preparation: `pythonsecurity:S8707`

[The finding in PR 125](https://sonarcloud.io/project/issues?id=victron-venus_venus-os-ci-toolkit&pullRequest=125&open=AaEbk_HvUxnp6n_oBMrt)
reports path traversal from the CLI `--version` argument to
`body.write_text(...)` in `scripts/prepare_version.py`.

Assessment: false positive for this specific flow. `choose_version` accepts an
explicit version only when it fully matches `version_plan.BASE`, an ASCII-only
numeric `X.Y.Z` expression. The accepted version is interpolated into the document
contents. It does not select the output path: `body` is always
`Path(temp) / "pr-body.md"`, where `temp` is created by `TemporaryDirectory`.
`Path.write_text` takes document data as its first argument; the receiver supplies
the filename. No version-derived path component reaches that receiver.

The preparation tests exercise real local Git repositories and worktrees while
replacing only GitHub transport. The regression test
`test_untrusted_requested_version_is_rejected_before_preparation_writes` checks
traversal, absolute-path, option-like, control-character and Unicode inputs. They
are rejected before version synchronization or preparation-branch handling, with
no changed files, refs, worktrees, pushes or PR creation. The existing successful
preparation test covers the accepted version path.

Run the focused suite in the development environment:

```sh
python3 -m unittest discover -s tests -p test_prepare_version.py
```

This assessment is limited to the reported version-to-document-content flow; it
does not exempt other release-policy inputs or external tools from review.
