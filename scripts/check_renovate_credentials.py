#!/usr/bin/env python3
"""Fail early when a classic bot token cannot update workflow files."""

from __future__ import annotations

import os
import urllib.error
import urllib.request


def validate_scopes(scope_header: str | None) -> bool:
    """Return whether classic workflow scope was verified; absence is unverified."""
    if scope_header is None:
        return False
    scopes = {scope.strip() for scope in scope_header.split(",")}
    if "workflow" not in scopes:
        raise ValueError(
            "BOT_PAT is missing the workflow scope. Update the organization secret "
            "with a bot token allowed to write repository contents and workflows. "
            "Renovate otherwise skips rejected pushes without failing its run."
        )
    return True


def main() -> None:
    for name in ("RENOVATE_TOKEN", "RENOVATE_GIT_PRIVATE_KEY"):
        if not os.environ.get(name):
            raise SystemExit(f"Missing required credential: {name}")
    request = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {os.environ['RENOVATE_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            workflow_scope_verified = validate_scopes(
                response.headers.get("X-OAuth-Scopes")
            )
    except (urllib.error.URLError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print("Bot token authenticated; signing key is configured.")
    if workflow_scope_verified:
        print("The classic workflow scope is present.")
    else:
        print(
            "::warning::No X-OAuth-Scopes header was returned; workflow write permissions "
            "remain unverified. For a fine-grained token, confirm Contents and Workflows "
            "write permissions on every configured target repository."
        )


if __name__ == "__main__":
    main()
