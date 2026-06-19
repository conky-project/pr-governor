#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import gh_api
import log
from settings import require_env

_CMD_ALLOW_UNSIGNED = "/allow-unsigned"
_CMD_APPROVE = "/approve"


def _diffs_are_equal(
    repo: str,
    base_sha: str,
    before_sha: str,
    after_sha: str,
    token: str,
) -> bool:
    """Return True if the PR diff is identical before and after a force push.

    Clones the repo (blobless, depth=1) and compares
    `git diff base before` vs `git diff base after`.
    Returns False on any failure - fail safe, so uncertainty always dismisses.
    """
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo"}

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            def _git(*args: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["git", *args],
                    cwd=tmpdir, capture_output=True, env=env, timeout=120,
                )

            if _git("init").returncode != 0:
                return False
            if _git("remote", "add", "origin", url).returncode != 0:
                return False
            # Fetch the three commits we need without walking full history.
            # --filter=blob:none defers blob downloads until git-diff needs them.
            for sha in (base_sha, before_sha, after_sha):
                if _git("fetch", "--filter=blob:none", "--depth=1", "origin", sha).returncode != 0:
                    # SHA may be unreachable (fully rewritten history) - fail safe.
                    return False

            def _diff(head: str) -> bytes | None:
                r = _git("diff", "--no-color", base_sha, head)
                return r.stdout if r.returncode == 0 else None

            diff_before = _diff(before_sha)
            diff_after = _diff(after_sha)
            if diff_before is None or diff_after is None:
                return False
            return diff_before == diff_after
    except Exception:
        return False


def _load_event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    return json.loads(Path(path).read_text()) if path else {}


def _parse_commands(body: str) -> list[tuple[str, list[str]]]:
    """Return (command, args) pairs for every command line in body.

    A command line is any line whose first non-whitespace character is '/'.
    Commands are lowercased; arguments are returned as-is.
    """
    commands = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("/"):
            parts = stripped.split()
            commands.append((parts[0].lower(), parts[1:]))
    return commands


def _get_persistent_approvals(comments: list[dict]) -> frozenset[str]:
    """Compute per-user persistent-approval state from PR comment history.

    Scans comments in order; last /approve command per user wins.
    `/approve persistent` (or `/approve p`) → True.
    `/approve discard` or `/approve` alone → False.
    Returns the set of users with an active persistent approval.
    """
    state: dict[str, bool] = {}
    for comment in comments:
        body = comment.get("body") or ""
        user = (comment.get("user") or {}).get("login")
        if not user:
            continue
        for cmd, args in _parse_commands(body):
            if cmd == _CMD_APPROVE:
                state[user] = "persistent" in args or "p" in args
    return frozenset(u for u, active in state.items() if active)


def _dismiss_all(
    repo: str,
    pr: int,
    reviews: list[dict],
    message: str,
    token: str,
    persistent: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Dismiss reviews, skipping any reviewer with an active persistent approval.

    Returns the set of logins whose reviews were preserved due to persistent approval.
    """
    preserved: set[str] = set()
    for review in reviews:
        reviewer = (review.get("user") or {}).get("login", "unknown")
        if reviewer in persistent:
            log.info(f"  preserving @{reviewer} (persistent approval active)")
            preserved.add(reviewer)
            continue
        ok = gh_api.dismiss_review(repo, pr, review["id"], message, token)
        log.info(f"  {'dismissed' if ok else 'FAILED to dismiss'} review by @{reviewer}")
    return frozenset(preserved)


def _handle_synchronize(
    repo: str,
    pr_number: int,
    before_sha: str,
    after_sha: str,
    token: str,
) -> int:
    pr_info = gh_api.get_pr(repo, pr_number, token)
    pr_base_sha = pr_info["base"]["sha"]
    pr_author = pr_info["user"]["login"]
    head_branch = pr_info["head"]["ref"]

    reviews = gh_api.get_latest_reviews(repo, pr_number, token)
    approved = [r for r in reviews if r["state"] == "APPROVED"]
    comments = gh_api.get_pr_comments(repo, pr_number, token)
    persistent = _get_persistent_approvals(comments)
    if persistent:
        log.info(f"Persistent approvals active: {sorted(persistent)}")

    null_sha = "0" * 40
    if approved and before_sha and before_sha != null_sha:
        # Use PR base (not before_sha) as range start so a rebase of the
        # author's own commits is not flagged as a new author.
        old_authors = gh_api.get_authors_in_range(repo, pr_base_sha, before_sha, token)
        new_authors = gh_api.get_authors_in_range(repo, pr_base_sha, after_sha, token)

        if old_authors is None or new_authors is None:
            # SHAs not comparable - either a force push or the range is too large.
            # Clone and diff to distinguish a clean rebase from a genuine rewrite.
            log.info("SHA range unverifiable - comparing diffs to check for clean rebase …")
            if _diffs_are_equal(repo, pr_base_sha, before_sha, after_sha, token):
                log.info("Clean rebase - diff unchanged, approvals preserved.")
            else:
                dismiss_message = (
                    "History was rewritten and the diff changed - re-review required."
                )
                log.warning(
                    "Diff changed after force push - dismissing all approvals.",
                    title="Force push with diff change detected",
                )
                preserved = _dismiss_all(repo, pr_number, approved, dismiss_message, token, persistent)
                body = (
                    "**Approvals dismissed - history rewritten**\n\n"
                    "This branch was force-pushed and the diff against the base changed. "
                    "All previous approvals have been dismissed; re-review is required."
                )
                if preserved:
                    listed = ", ".join(f"@{u}" for u in sorted(preserved))
                    body += f"\n\n{listed} used `/approve persistent` and their approvals were preserved."
                gh_api.post_pr_comment(repo, pr_number, body, token)
        else:
            truly_new = new_authors - old_authors
            if truly_new:
                formatted = ", ".join(f"@{a}" for a in sorted(truly_new))
                dismiss_message = (
                    f"New contributor(s) appeared in PR commits after this review: "
                    f"{formatted}. This changes the authorship of the PR - "
                    "please re-review."
                )
                log.warning(
                    f"New authors detected: {formatted}. Dismissing {len(approved)} approval(s).",
                    title="New author detected after approval",
                )
                preserved = _dismiss_all(repo, pr_number, approved, dismiss_message, token, persistent)
                body = (
                    f"**Approvals dismissed - new author in commit history**\n\n"
                    f"{formatted} appeared in this PR's commit history after existing "
                    f"reviews were given. All approvals have been dismissed; "
                    f"re-review is required."
                )
                if preserved:
                    listed = ", ".join(f"@{u}" for u in sorted(preserved))
                    body += f"\n\n{listed} used `/approve persistent` and their approvals were preserved."
                gh_api.post_pr_comment(repo, pr_number, body, token)
            else:
                log.info(f"No new authors introduced. Current PR authors: {sorted(new_authors)}")
    elif not approved:
        log.info("No active approvals - nothing to guard.")

    commits = gh_api.get_pr_commits(repo, pr_number, token)
    author_signed = any(
        c.get("commit", {}).get("verification", {}).get("verified")
        for c in commits
        if (c.get("author") or {}).get("login") == pr_author
    )

    if author_signed and not gh_api.has_required_signatures(repo, head_branch, token):
        ok = gh_api.enable_required_signatures(repo, head_branch, token)
        log.info(
            f"{'Enabled' if ok else 'Failed to enable'} required signatures on "
            f"'{head_branch}' - PR author @{pr_author} uses signed commits."
        )
        if ok:
            gh_api.post_pr_comment(
                repo, pr_number,
                f"**Signature protection enabled**\n\n"
                f"@{pr_author}'s commits on this branch are GPG-signed. "
                f"Branch signature protection has been automatically enabled - "
                f"any further pushes must be signed with a verified key.\n\n"
                f"To allow unsigned commits, comment `/allow-unsigned` "
                f"(PR author and repository owners only).",
                token,
            )
    elif not author_signed:
        log.info(
            f"PR author @{pr_author} has no verified commits - "
            "signature lock not applied."
        )

    return 0


def _handle_comment(repo: str, repo_owner: str, token: str, org_token: str) -> int:
    event = _load_event()

    # Ignore comments on plain issues.
    if not event.get("issue", {}).get("pull_request"):
        return 0

    body = event.get("comment", {}).get("body") or ""
    commenter = event["comment"]["user"]["login"]
    pr_number = event["issue"]["number"]

    for cmd, args in _parse_commands(body):
        if cmd == _CMD_ALLOW_UNSIGNED:
            _handle_allow_unsigned(repo, repo_owner, pr_number, commenter, token, org_token)
        elif cmd == _CMD_APPROVE:
            persistent = "persistent" in args or "p" in args
            _handle_approve(repo, pr_number, commenter, persistent, token)

    return 0


def _handle_allow_unsigned(
    repo: str,
    repo_owner: str,
    pr_number: int,
    commenter: str,
    token: str,
    org_token: str,
) -> None:
    pr_info = gh_api.get_pr(repo, pr_number, token)
    pr_author = pr_info["user"]["login"]
    head_branch = pr_info["head"]["ref"]

    root = gh_api.get_root_of_trust(repo_owner, org_token)
    if commenter != pr_author and commenter not in root:
        log.info(
            f"@{commenter} is not authorized to disable the signature lock "
            f"(must be PR author @{pr_author} or a repository owner)."
        )
        return

    if not gh_api.has_required_signatures(repo, head_branch, token):
        log.info(f"No signature lock active on '{head_branch}' - nothing to disable.")
        return

    ok = gh_api.disable_required_signatures(repo, head_branch, token)
    log.info(
        f"{'Disabled' if ok else 'Failed to disable'} required signatures on "
        f"'{head_branch}' (requested by @{commenter})."
    )
    if ok:
        gh_api.post_pr_comment(
            repo, pr_number,
            f"**Signature protection disabled**\n\n"
            f"@{commenter} has removed the signature requirement from this branch. "
            f"Unsigned commits are now accepted. Existing approvals remain valid.",
            token,
        )
    # Approvals are not dismissed here. This command only changes what future
    # commits are allowed - the commits that were already reviewed haven't
    # changed. If a new commit is pushed afterward, the synchronize handler
    # will dismiss approvals if a new author appears.


def _handle_approve(
    repo: str,
    pr_number: int,
    commenter: str,
    persistent: bool,
    token: str,
) -> None:
    if persistent:
        log.info(f"@{commenter} set persistent approval on PR #{pr_number}.")
        gh_api.post_pr_comment(
            repo, pr_number,
            f"**Persistent approval set** - @{commenter}\n\n"
            f"Your approval will not be automatically dismissed by governance actions "
            f"(force pushes, new authors) for the lifetime of this PR.\n\n"
            f"To remove, comment `/approve` (without arguments).",
            token,
        )
    else:
        log.info(f"@{commenter} revoked persistent approval on PR #{pr_number}.")
        gh_api.post_pr_comment(
            repo, pr_number,
            f"**Persistent approval removed** - @{commenter}\n\n"
            f"Your approval may now be automatically dismissed by governance actions "
            f"if the PR history changes.",
            token,
        )


def main() -> int:
    env = require_env("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_REPOSITORY_OWNER")
    token = env["GITHUB_TOKEN"]
    repo = env["GITHUB_REPOSITORY"]
    repo_owner = env["GITHUB_REPOSITORY_OWNER"]
    org_token = os.environ.get("ORG_TOKEN") or token

    event_name = os.environ.get("GITHUB_EVENT_NAME", "pull_request_target")

    if event_name == "issue_comment":
        return _handle_comment(repo, repo_owner, token, org_token)

    # pull_request_target / synchronize
    pr_env = require_env("PR_NUMBER")
    pr_number = int(pr_env["PR_NUMBER"])
    before_sha = os.environ.get("BEFORE_SHA", "")
    after_sha = os.environ.get("AFTER_SHA", "")
    return _handle_synchronize(repo, pr_number, before_sha, after_sha, token)


if __name__ == "__main__":
    sys.exit(main())
