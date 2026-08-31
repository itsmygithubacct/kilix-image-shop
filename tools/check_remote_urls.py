#!/usr/bin/python3
"""Refuse Git remotes that do not resolve to the authorized private repository."""

from __future__ import annotations

import subprocess
import sys
import urllib.parse


AUTHORIZED_HOST = "github.com"
AUTHORIZED_PATH = "/itsmygithubacct/kilix-image-shop"
AUTHORIZED_SSH_PATH = "itsmygithubacct/kilix-image-shop"


def is_authorized_remote_url(url: str) -> bool:
    """Accept canonical HTTPS or scp-style SSH spellings, with optional .git."""

    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:").removesuffix(".git")
        return path == AUTHORIZED_SSH_PATH

    try:
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path.removesuffix(".git")
        return (
            parsed.scheme == "https"
            and parsed.hostname == AUTHORIZED_HOST
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and path == AUTHORIZED_PATH
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def git(*args: str) -> list[str]:
    completed = subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.splitlines()


def main() -> int:
    for remote in git("remote"):
        urls = set(git("remote", "get-url", "--all", remote))
        urls.update(git("remote", "get-url", "--push", "--all", remote))
        if not urls or any(not is_authorized_remote_url(url) for url in urls):
            # Do not print a rejected URL: it may contain credentials.
            print(f"unauthorized remote URL on {remote}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
