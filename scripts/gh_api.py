"""GitHub REST and GraphQL API wrappers."""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from datetime import datetime, timezone

import requests

_API = "https://api.github.com"
_GRAPHQL_URL = f"{_API}/graphql"

# (merged_at, additions, deletions)
PREntry = tuple[datetime, int, int]


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_paginated(url: str, token: str, params: dict | None = None) -> list[dict]:
    results: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            url,
            headers=_headers(token),
            params={**(params or {}), "per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        results.extend(data)
        if len(data) < 100:
            break
        page += 1
    return results


def graphql(query: str, variables: dict, token: str) -> dict:
    resp = requests.post(
        _GRAPHQL_URL,
        headers=_headers(token),
        json={"query": query, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data


# ── Root of trust ────────────────────────────────────────────────────────────

def get_root_of_trust(repo_owner: str, token: str) -> set[str]:
    """Return the root-of-trust logins for a repository.

    Personal account → {repo_owner}.
    Organization     → all members with the 'owner' role.
    Requires read:org scope when repo_owner is an organization.
    """
    resp = requests.get(f"{_API}/users/{repo_owner}", headers=_headers(token), timeout=30)
    resp.raise_for_status()
    if resp.json().get("type") != "Organization":
        return {repo_owner}
    owners = get_paginated(f"{_API}/orgs/{repo_owner}/members", token, {"role": "owner"})
    return {m["login"] for m in owners}


# ── Pull requests ─────────────────────────────────────────────────────────────

def get_pr(repo: str, pr: int, token: str) -> dict:
    resp = requests.get(f"{_API}/repos/{repo}/pulls/{pr}", headers=_headers(token), timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_pr_base_sha(repo: str, pr: int, token: str) -> str:
    return get_pr(repo, pr, token)["base"]["sha"]


def get_commit_authors(repo: str, pr: int, token: str) -> set[str]:
    commits = get_paginated(f"{_API}/repos/{repo}/pulls/{pr}/commits", token)
    return {
        c["author"]["login"]
        for c in commits
        if c.get("author") and c["author"].get("login")
    }


def get_latest_reviews(repo: str, pr: int, token: str) -> list[dict]:
    """Return the most-recent review per user (by review id)."""
    reviews = get_paginated(f"{_API}/repos/{repo}/pulls/{pr}/reviews", token)
    latest: dict[str, dict] = {}
    for r in reviews:
        login = (r.get("user") or {}).get("login")
        if not login:
            continue
        if login not in latest or r["id"] > latest[login]["id"]:
            latest[login] = r
    return list(latest.values())


def approved_reviewers(reviews: list[dict]) -> set[str]:
    return {
        (r.get("user") or {}).get("login")
        for r in reviews
        if r["state"] == "APPROVED"
    } - {None}  # type: ignore[operator]


def get_required_approvals(repo: str, branch: str, token: str) -> int:
    """Return required_approving_review_count from branch protection, or 1 if unset."""
    resp = requests.get(
        f"{_API}/repos/{repo}/branches/{branch}/protection",
        headers=_headers(token),
        timeout=30,
    )
    if resp.status_code == 404:
        return 1
    resp.raise_for_status()
    return (
        resp.json()
        .get("required_pull_request_reviews", {})
        .get("required_approving_review_count", 1)
    )


def get_authors_in_range(
    repo: str, base_sha: str, head_sha: str, token: str
) -> set[str] | None:
    """Return author logins for commits in base..head, or None if unverifiable.

    None means the range cannot be trusted - either a SHA is unknown (force
    push rewrote history) or the range exceeds GitHub's 250-commit compare cap.
    Callers should treat None as an unverifiable state and take the conservative
    action (dismiss all approvals).
    """
    resp = requests.get(
        f"{_API}/repos/{repo}/compare/{base_sha}...{head_sha}",
        headers=_headers(token),
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    if data.get("total_commits", 0) > len(data.get("commits", [])):
        return None
    return {
        c["author"]["login"]
        for c in data.get("commits", [])
        if c.get("author") and c["author"].get("login")
    }


# ── Branch signature protection ───────────────────────────────────────────────

def get_pr_commits(repo: str, pr: int, token: str) -> list[dict]:
    """Return all commit objects for a PR, each including verification info."""
    return get_paginated(f"{_API}/repos/{repo}/pulls/{pr}/commits", token)


def has_required_signatures(repo: str, branch: str, token: str) -> bool:
    resp = requests.get(
        f"{_API}/repos/{repo}/branches/{branch}/protection/required_signatures",
        headers=_headers(token),
        timeout=30,
    )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return resp.json().get("enabled", False)


def enable_required_signatures(repo: str, branch: str, token: str) -> bool:
    # required_signatures requires a protection rule to exist on the branch.
    # If none exists yet, create a minimal one that preserves the defaults
    # (force pushes and deletions allowed, no required checks or reviews).
    resp = requests.get(
        f"{_API}/repos/{repo}/branches/{branch}/protection",
        headers=_headers(token),
        timeout=30,
    )
    if resp.status_code == 404:
        put = requests.put(
            f"{_API}/repos/{repo}/branches/{branch}/protection",
            headers=_headers(token),
            json={
                "required_status_checks": None,
                "enforce_admins": None,
                "required_pull_request_reviews": None,
                "restrictions": None,
                "allow_force_pushes": True,
                "allow_deletions": True,
            },
            timeout=30,
        )
        if not put.ok:
            return False
    elif not resp.ok:
        resp.raise_for_status()

    resp = requests.post(
        f"{_API}/repos/{repo}/branches/{branch}/protection/required_signatures",
        headers=_headers(token),
        timeout=30,
    )
    return resp.status_code in (200, 201)


def disable_required_signatures(repo: str, branch: str, token: str) -> bool:
    resp = requests.delete(
        f"{_API}/repos/{repo}/branches/{branch}/protection/required_signatures",
        headers=_headers(token),
        timeout=30,
    )
    return resp.status_code == 204


# ── Commit statuses ───────────────────────────────────────────────────────────

STATUS_CONTEXT = "pr-governance/approval-check"


def post_status(repo: str, sha: str, state: str, description: str, token: str) -> bool:
    resp = requests.post(
        f"{_API}/repos/{repo}/statuses/{sha}",
        headers=_headers(token),
        json={"state": state, "description": description[:140], "context": STATUS_CONTEXT},
        timeout=30,
    )
    return resp.status_code in (200, 201)


# ── Reviews and comments ──────────────────────────────────────────────────────

def get_pr_comments(repo: str, pr: int, token: str) -> list[dict]:
    """Return all issue comments on a PR in chronological order."""
    return get_paginated(f"{_API}/repos/{repo}/issues/{pr}/comments", token)


def post_pr_comment(repo: str, pr: int, body: str, token: str) -> bool:
    resp = requests.post(
        f"{_API}/repos/{repo}/issues/{pr}/comments",
        headers=_headers(token),
        json={"body": body},
        timeout=30,
    )
    return resp.status_code in (200, 201)


def dismiss_review(repo: str, pr: int, review_id: int, message: str, token: str) -> bool:
    resp = requests.put(
        f"{_API}/repos/{repo}/pulls/{pr}/reviews/{review_id}/dismissals",
        headers=_headers(token),
        json={"message": message},
        timeout=30,
    )
    return resp.status_code in (200, 201)


# ── Teams ─────────────────────────────────────────────────────────────────────

def get_team_members(org: str, team_slug: str, token: str) -> set[str] | None:
    """Return team member logins, or None if the team does not exist."""
    members: set[str] = set()
    page = 1
    while True:
        resp = requests.get(
            f"{_API}/orgs/{org}/teams/{team_slug}/members",
            headers=_headers(token),
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        members.update(m["login"] for m in data)
        if len(data) < 100:
            break
        page += 1
    return members


def ensure_team(org: str, team_slug: str, token: str) -> bool:
    resp = requests.get(f"{_API}/orgs/{org}/teams/{team_slug}", headers=_headers(token), timeout=30)
    if resp.status_code == 200:
        return True
    if resp.status_code != 404:
        resp.raise_for_status()
    resp = requests.post(
        f"{_API}/orgs/{org}/teams",
        headers=_headers(token),
        json={
            "name": team_slug,
            "description": "Contributors trusted to review and approve pull requests",
            "privacy": "closed",
            "permission": "pull",
        },
        timeout=30,
    )
    return resp.status_code in (200, 201)


def add_member(org: str, team_slug: str, login: str, token: str) -> bool:
    resp = requests.put(
        f"{_API}/orgs/{org}/teams/{team_slug}/memberships/{login}",
        headers=_headers(token),
        json={"role": "member"},
        timeout=30,
    )
    return resp.status_code in (200, 201)


def remove_member(org: str, team_slug: str, login: str, token: str) -> bool:
    resp = requests.delete(
        f"{_API}/orgs/{org}/teams/{team_slug}/memberships/{login}",
        headers=_headers(token),
        timeout=30,
    )
    return resp.status_code == 204


# ── Merged PR history (GraphQL) ───────────────────────────────────────────────

def _is_excluded(login: str, ignore_users: frozenset[str]) -> bool:
    return any(fnmatch.fnmatch(login, pattern) for pattern in ignore_users)

_MERGED_PRS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: MERGED
      first: 100
      after: $cursor
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      nodes {
        author { login __typename }
        mergedAt
        additions
        deletions
        reviews(first: 100) {
          nodes {
            author { login __typename }
            submittedAt
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def fetch_merged_prs(
    owner: str,
    name: str,
    token: str,
    ignore_users: frozenset[str] = frozenset({"*[bot]", "dependabot"}),
    max_prs: int = 500,
) -> tuple[dict[str, list[PREntry]], dict[str, list[datetime]]]:
    """Return (author_prs, reviewer_dates).

    author_prs:     {login: [(merged_at, additions, deletions), ...]}
    reviewer_dates: {login: [submitted_at, ...]} - reviews given on merged PRs,
                    excluding self-reviews, Bot-type accounts, and ignored users.
    Fetches the most recent PRs first; stops after max_prs (0 = unlimited).
    """
    author_prs: dict[str, list[PREntry]] = defaultdict(list)
    reviewer_dates: dict[str, list[datetime]] = defaultdict(list)
    cursor: str | None = None
    total = 0
    done = False

    while not done:
        data = graphql(_MERGED_PRS_QUERY, {"owner": owner, "name": name, "cursor": cursor}, token)
        prs = data["data"]["repository"]["pullRequests"]
        for node in prs["nodes"]:
            if not node["author"] or not node["mergedAt"]:
                continue
            if node["author"]["__typename"] == "Bot":
                continue
            pr_author: str = node["author"]["login"]
            if _is_excluded(pr_author, ignore_users):
                continue
            merged_at = datetime.fromisoformat(node["mergedAt"].replace("Z", "+00:00"))
            author_prs[pr_author].append((merged_at, node["additions"], node["deletions"]))

            for review in (node.get("reviews") or {}).get("nodes", []):
                rev_author = review.get("author") or {}
                reviewer = rev_author.get("login")
                submitted_str = review.get("submittedAt")
                if not reviewer or not submitted_str:
                    continue
                if reviewer == pr_author:
                    continue  # self-review
                if rev_author.get("__typename") == "Bot":
                    continue
                if _is_excluded(reviewer, ignore_users):
                    continue
                reviewer_dates[reviewer].append(
                    datetime.fromisoformat(submitted_str.replace("Z", "+00:00"))
                )

            total += 1
            if max_prs > 0 and total >= max_prs:
                done = True
                break

        if not prs["pageInfo"]["hasNextPage"]:
            break
        cursor = prs["pageInfo"]["endCursor"]

    return dict(author_prs), dict(reviewer_dates)
