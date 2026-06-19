#!/usr/bin/env python3

from __future__ import annotations

import sys

import gh_api
import log
from settings import Config, require_env


def _fail(repo: str, sha: str, msg: str, token: str) -> int:
    log.error(msg, title="Approval check failed")
    if not gh_api.post_status(repo, sha, "failure", msg, token):
        log.warning(f"Could not post failure status to {sha[:7]}")
    return 1


def _pass(repo: str, sha: str, msg: str, token: str) -> int:
    log.notice(msg, title="Approval check passed")
    if not gh_api.post_status(repo, sha, "success", msg, token):
        log.warning(f"Could not post success status to {sha[:7]}")
    return 0


def main() -> int:
    env = require_env("GITHUB_TOKEN", "ORG_TOKEN", "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_OWNER", "PR_NUMBER", "HEAD_SHA")
    status_token = env["GITHUB_TOKEN"]
    org_token = env["ORG_TOKEN"]
    repo = env["GITHUB_REPOSITORY"]
    repo_owner = env["GITHUB_REPOSITORY_OWNER"]
    pr = int(env["PR_NUMBER"])
    head_sha = env["HEAD_SHA"]

    cfg = Config.load()

    root = gh_api.get_root_of_trust(repo_owner, org_token)
    pr_data = gh_api.get_pr(repo, pr, status_token)
    base_branch = pr_data["base"]["ref"]
    required = gh_api.get_required_approvals(repo, base_branch, status_token)

    reviews = gh_api.get_latest_reviews(repo, pr, status_token)
    approvers = gh_api.approved_reviewers(reviews)
    commit_authors = gh_api.get_commit_authors(repo, pr, status_token)
    trusted = gh_api.get_team_members(cfg.org, cfg.team_slug, org_token) or set()

    if not trusted and not (root & approvers):
        names = ", ".join(f"@{r}" for r in sorted(root))
        return _fail(
            repo, head_sha,
            f"Trusted-contributors team is empty or unreachable. Approval from {names} required to proceed.",
            status_token,
        )

    # Rule 1: root-of-trust approval is always sufficient.
    root_approvals = root & approvers
    if root_approvals:
        names = ", ".join(f"@{r}" for r in sorted(root_approvals))
        return _pass(repo, head_sha, f"Approved by root of trust: {names}.", status_token)

    # Rule 2: no approver may be a commit author in this PR.
    self_approvers = approvers & commit_authors
    if self_approvers:
        names = ", ".join(f"@{a}" for a in sorted(self_approvers))
        return _fail(
            repo, head_sha,
            f"Self-approval not permitted: {names} both authored commits and approved.",
            status_token,
        )

    # Rule 3: enough trusted approvals.
    trusted_approvals = approvers & trusted
    if len(trusted_approvals) < required:
        untrusted = approvers - trusted
        if not approvers:
            msg = f"Requires {required} approval(s) from trusted contributor(s)."
        elif untrusted:
            names = ", ".join(f"@{a}" for a in sorted(untrusted))
            msg = (
                f"{names} {'has' if len(untrusted) == 1 else 'have'} not yet earned "
                f"trusted-contributor status. {required} trusted approval(s) required."
            )
        else:
            msg = f"Requires {required} trusted approval(s); {len(trusted_approvals)} received."
        return _fail(repo, head_sha, msg, status_token)

    names = ", ".join(f"@{a}" for a in sorted(trusted_approvals))
    return _pass(
        repo, head_sha,
        f"Approved by {len(trusted_approvals)} trusted contributor(s): {names}.",
        status_token,
    )


if __name__ == "__main__":
    sys.exit(main())
