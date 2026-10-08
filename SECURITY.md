# Security Policy

## Reporting a Vulnerability

Private vulnerability reporting is enabled for this repository. Use
[Report a vulnerability](https://github.com/victron-venus/venus-os-ci-toolkit/security/advisories/new)
to send a confidential report to the maintainers. Follow
[GitHub's private reporting instructions](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)
if you need help submitting the report.

Include the affected version or commit, steps to reproduce, expected and actual
behavior, and potential impact. Remove access tokens, credentials and personal
data from examples. Do not disclose exploit details in public issues before
coordinating with the maintainers.

The maintained default branch is the supported toolkit version. Consumers pin
older commits, so a toolkit fix must be reviewed and installed in each affected
consumer. Include both revisions in a report.

Maintainers aim to acknowledge reports within 14 days, assess impact, and agree
on a fix and coordinated disclosure. Confirmed critical issues and active
exploitation take priority. Publicly known medium-or-higher vulnerabilities must
be fixed within 60 days of becoming public; do not restart that clock when a
report is confirmed. Release notes identify affected versions, mitigation and
any assigned CVE or advisory. Early mitigation guidance may be published when
waiting for coordinated disclosure would put users at risk.

## Release helper transport

The release helpers use GitHub CLI for authenticated HTTPS operations. See the
[per-command TLS profile](docs/RELEASE_TLS_PROFILE.md) for the verified toolchain,
certificate key restrictions and a read-only verification command.
