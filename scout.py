#!/usr/bin/env python3
"""OSS Scout, stage 1: find and score open-source projects worth contributing to.

Gathers candidates from GitHub search, collects objective health metrics via the
GraphQL API, filters out unsuitable repos and computes a deterministic
"contributability" score (0-100). Stage 2 (the `scout` subagent) reads the
shortlist and scores skill stretch / readability with an LLM.

Standard library only. All GitHub access goes through the `gh` CLI using a
dedicated config dir (personal account), never the machine's default login.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DEFAULT_GH_CONFIG = Path.home() / ".config" / "gh-oss"

REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
MAINTAINER = {"OWNER", "MEMBER", "COLLABORATOR"}
OUTSIDER = {"CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"}
LOOKBACK_DAYS = 180
MIN_AGE_H = 72            # give maintainers 3 days before an item counts as unanswered
NO_REPLY_CAP_H = 30 * 24  # unanswered items count as a 30-day response

REPO_QUERY = """
query($owner: String!, $name: String!, $since: GitTimestamp!, $labels: [String!]) {
  repository(owner: $owner, name: $name) {
    nameWithOwner url description stargazerCount isArchived isFork pushedAt
    primaryLanguage { name }
    licenseInfo { spdxId }
    repositoryTopics(first: 20) { nodes { topic { name } } }
    contributingRoot: object(expression: "HEAD:CONTRIBUTING.md") { __typename }
    contributingGithub: object(expression: "HEAD:.github/CONTRIBUTING.md") { __typename }
    contributingDocs: object(expression: "HEAD:docs/CONTRIBUTING.md") { __typename }
    defaultBranchRef { target { ... on Commit { history(since: $since) { totalCount } } } }
    releases(first: 1, orderBy: {field: CREATED_AT, direction: DESC}) { nodes { createdAt } }
    labeledIssues: issues(first: 50, states: OPEN, labels: $labels,
                          orderBy: {field: CREATED_AT, direction: DESC}) {
      totalCount
      nodes {
        number title url createdAt
        assignees { totalCount }
        timelineItems(first: 20, itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
          nodes {
            __typename
            ... on CrossReferencedEvent { source { ... on PullRequest { state } } }
            ... on ConnectedEvent { subject { ... on PullRequest { state } } }
          }
        }
      }
    }
    recentIssues: issues(first: 40, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        createdAt authorAssociation author { __typename login }
        comments(first: 20) { nodes { createdAt authorAssociation author { __typename login } } }
        closes: timelineItems(first: 1, itemTypes: [CLOSED_EVENT]) {
          nodes { ... on ClosedEvent { createdAt actor { __typename login } } }
        }
      }
    }
    recentPRs: pullRequests(first: 40, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        createdAt state merged mergedAt authorAssociation author { __typename login }
        mergedBy { __typename login }
        comments(first: 20) { nodes { createdAt authorAssociation author { __typename login } } }
        reviews(first: 20) { nodes { createdAt authorAssociation author { __typename login } } }
        closes: timelineItems(first: 1, itemTypes: [CLOSED_EVENT]) {
          nodes { ... on ClosedEvent { createdAt actor { __typename login } } }
        }
      }
    }
  }
}
"""


class GhError(RuntimeError):
    pass


def gh_env() -> dict:
    env = dict(os.environ)
    env["GH_CONFIG_DIR"] = os.environ.get("OSS_GH_CONFIG_DIR", str(DEFAULT_GH_CONFIG))
    # Never let a work token in the environment override the personal login.
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return env


def gh_json(args: list[str], stdin: str | None = None) -> dict:
    proc = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True,
                          timeout=120, env=gh_env())
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip()[:500] or proc.stdout.strip()[:500])
    return json.loads(proc.stdout)


def check_account(expected: str) -> None:
    try:
        login = gh_json(["api", "user"])["login"]
    except GhError as e:
        sys.exit(f"gh is not logged in for OSS work ({gh_env()['GH_CONFIG_DIR']}).\n"
                 f"Run: GH_CONFIG_DIR={gh_env()['GH_CONFIG_DIR']} gh auth login\n{e}")
    if login.lower() != expected.lower():
        sys.exit(f"gh is authenticated as '{login}', expected personal account '{expected}'. Aborting.")


def load_profile(path: Path) -> dict:
    profile = json.loads(path.read_text())
    for key in ("github_user", "skills", "search", "issue_labels", "filters", "shortlist_size"):
        if key not in profile:
            sys.exit(f"profile is missing required key: {key}")
    if not isinstance(profile["search"].get("queries"), list) or not profile["search"]["queries"]:
        sys.exit("profile.search.queries must be a non-empty list")
    return profile


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_bot(author: dict | None) -> bool:
    if not author:
        return True  # deleted ("ghost") users carry no signal either
    login = author.get("login", "").lower()
    # Some projects run bots from ordinary user accounts (e.g. "botberry"), so also match by name.
    return (author.get("__typename") == "Bot" or login.endswith("[bot]")
            or re.search(r"(^bot|bot$|-bot\b|-bot-)", login) is not None)


def avoid_hit(text: str, avoid: list[str]) -> str | None:
    text = text.lower()
    for word in avoid:
        if re.search(rf"\b{re.escape(word.lower())}\b", text):
            return word
    return None


def search_candidates(profile: dict, now: datetime) -> list[dict]:
    s = profile["search"]
    pushed_since = (now - timedelta(days=s["pushed_within_days"])).date().isoformat()
    per_query: list[list[dict]] = []
    for q in s["queries"]:
        full_q = f'{q} stars:{s["min_stars"]}..{s["max_stars"]} pushed:>{pushed_since} archived:false'
        print(f"search: {full_q}", file=sys.stderr)
        try:
            data = gh_json(["api", "-X", "GET", "search/repositories",
                            "-f", f"q={full_q}", "-f", f"per_page={int(s['per_query'])}"])
        except GhError as e:
            print(f"  search failed: {e}", file=sys.stderr)
            continue
        per_query.append(data.get("items", []))

    # Round-robin across queries so one topic doesn't crowd out the rest.
    seen: dict[str, dict] = {}
    for i in range(max((len(items) for items in per_query), default=0)):
        for items in per_query:
            if i < len(items):
                seen.setdefault(items[i]["full_name"], items[i])
    return list(seen.values())


def is_maintainer(author: dict | None, association: str | None, maintainers: set[str]) -> bool:
    if is_bot(author):
        return False
    return association in MAINTAINER or (author or {}).get("login") in maintainers


def first_maintainer_reply_h(item: dict, now: datetime, maintainers: set[str] = frozenset()) -> float | None:
    """Hours until first maintainer response (comment, review, merge or close);
    capped if unanswered; None if too young to judge."""
    created = parse_ts(item["createdAt"])
    author = (item.get("author") or {}).get("login")
    replies = [n for key in ("comments", "reviews") for n in (item.get(key) or {}).get("nodes", [])]
    times = [parse_ts(n["createdAt"]) for n in replies
             if is_maintainer(n.get("author"), n.get("authorAssociation"), maintainers)
             and (n.get("author") or {}).get("login") != author]
    if item.get("mergedAt"):
        times.append(parse_ts(item["mergedAt"]))
    for ev in (item.get("closes") or {}).get("nodes", []):
        actor = ev.get("actor")
        if ev.get("createdAt") and not is_bot(actor) and (actor or {}).get("login") != author:
            times.append(parse_ts(ev["createdAt"]))
    if times:
        return (min(times) - created).total_seconds() / 3600
    age_h = (now - created).total_seconds() / 3600
    return None if age_h < MIN_AGE_H else NO_REPLY_CAP_H


def has_linked_pr(issue: dict) -> bool:
    for ev in issue["timelineItems"]["nodes"]:
        pr = ev.get("source") or ev.get("subject") or {}
        if pr.get("state") in ("OPEN", "MERGED"):
            return True
    return False


def evaluate(full_name: str, profile: dict, now: datetime) -> dict:
    owner, name = full_name.split("/", 1)
    body = json.dumps({"query": REPO_QUERY, "variables": {
        "owner": owner, "name": name,
        "since": (now - timedelta(days=30)).isoformat(),
        "labels": profile["issue_labels"],
    }})
    resp = gh_json(["api", "graphql", "--input", "-"], stdin=body)
    r = (resp.get("data") or {}).get("repository")
    if not r:
        raise GhError(f"no data for {full_name}: {resp.get('errors')}")

    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    # Anyone who merged a PR has write access, even if their org membership is private.
    maintainers = {p["mergedBy"]["login"] for p in r["recentPRs"]["nodes"]
                   if p.get("mergedBy") and not is_bot(p["mergedBy"])}

    def outsider(item: dict) -> bool:
        return (item.get("authorAssociation") in OUTSIDER and not is_bot(item.get("author"))
                and (item.get("author") or {}).get("login") not in maintainers
                and parse_ts(item["createdAt"]) >= cutoff)

    def reply_times(nodes: list[dict]) -> list[float]:
        return [h for h in (first_maintainer_reply_h(i, now, maintainers) for i in nodes if outsider(i))
                if h is not None]

    def median(xs: list[float]) -> float | None:
        return round(statistics.median(xs), 1) if xs else None

    # Issues and PRs are reported separately: drive-by PR waves (e.g. Hacktoberfest)
    # can swamp PR review times while issue triage stays fast.
    issue_replies, pr_replies = reply_times(r["recentIssues"]["nodes"]), reply_times(r["recentPRs"]["nodes"])
    replies = issue_replies + pr_replies
    known = [m for m in (median(issue_replies), median(pr_replies)) if m is not None]
    median_h = round(sum(known) / len(known), 1) if known else None
    answered = sum(1 for h in replies if h < NO_REPLY_CAP_H)
    reply_rate = round(answered / len(replies), 2) if replies else None

    closed_prs = [p for p in r["recentPRs"]["nodes"] if outsider(p) and p["state"] in ("MERGED", "CLOSED")]
    merge_rate = (round(sum(p["merged"] for p in closed_prs) / len(closed_prs), 2)
                  if len(closed_prs) >= 3 else None)

    suitable = [i for i in r["labeledIssues"]["nodes"]
                if i["assignees"]["totalCount"] == 0 and not has_linked_pr(i)]

    target = (r.get("defaultBranchRef") or {}).get("target") or {}
    commits_30d = (target.get("history") or {}).get("totalCount", 0)
    rel = r["releases"]["nodes"]
    days_since_release = (now - parse_ts(rel[0]["createdAt"])).days if rel else None
    has_contributing = any(r.get(k) for k in ("contributingRoot", "contributingGithub", "contributingDocs"))
    topics = [n["topic"]["name"] for n in r["repositoryTopics"]["nodes"]]

    m = {
        "repo": r["nameWithOwner"],
        "url": r["url"],
        "description": r.get("description") or "",
        "stars": r["stargazerCount"],
        "language": (r.get("primaryLanguage") or {}).get("name"),
        "topics": topics,
        "license": (r.get("licenseInfo") or {}).get("spdxId"),
        "archived": r["isArchived"],
        "fork": r["isFork"],
        "pushed_at": r["pushedAt"],
        "commits_30d": commits_30d,
        "days_since_release": days_since_release,
        "has_contributing": has_contributing,
        "outsider_samples": len(replies),
        "median_first_reply_h": median_h,
        "issue_median_reply_h": median(issue_replies),
        "pr_median_reply_h": median(pr_replies),
        "maintainer_reply_rate": reply_rate,
        "outsider_pr_samples": len(closed_prs),
        "outsider_pr_merge_rate": merge_rate,
        "labeled_open_issues": r["labeledIssues"]["totalCount"],
        "suitable_issues": len(suitable),
        "sample_issues": [{"number": i["number"], "title": i["title"], "url": i["url"]} for i in suitable[:10]],
    }
    m["health_score"], m["flags"] = score(m)
    return m


def score(m: dict) -> tuple[int, list[str]]:
    flags: list[str] = []

    h = m["median_first_reply_h"]
    if h is None:
        resp = 10.0
        flags.append("no recent outsider activity to judge responsiveness")
    else:
        resp = 30 if h <= 24 else 24 if h <= 72 else 15 if h <= 168 else 7 if h <= 336 else 0
        resp *= 0.5 + 0.5 * (m["maintainer_reply_rate"] or 0)
        if h > 168:
            flags.append(f"slow maintainers (avg median first reply {h:.0f}h)")
    if 0 < m["outsider_samples"] < 5:
        flags.append("small responsiveness sample")

    mr = m["outsider_pr_merge_rate"]
    merge = 10.0 if mr is None else mr * 25
    if mr is None:
        flags.append("too few closed outsider PRs to judge merge rate")
    elif mr < 0.3:
        flags.append(f"low outsider PR merge rate ({mr:.0%})")

    supply = min(m["suitable_issues"], 10) / 10 * 20
    activity = min(m["commits_30d"], 60) / 60 * 10
    if m["days_since_release"] is not None and m["days_since_release"] <= 90:
        activity += 5
    friendly = (6 if m["has_contributing"] else 0) + (4 if m["license"] and m["license"] != "NOASSERTION" else 0)
    if not m["has_contributing"]:
        flags.append("no CONTRIBUTING.md")
    if not m["license"]:
        flags.append("no license detected")

    return round(resp + merge + supply + activity + friendly), flags


def fmt(v, suffix=""):
    return "-" if v is None else f"{v}{suffix}"


def print_table(rows: list[dict]) -> None:
    print("| # | Repo | Health | Stars | Reply h (issues / PRs) | Outsider merge | Suitable issues | Commits 30d | Flags |")
    print("|---|---|---|---|---|---|---|---|---|")
    for n, m in enumerate(rows, 1):
        mr = m["outsider_pr_merge_rate"]
        print(f"| {n} | [{m['repo']}]({m['url']}) | {m['health_score']} | {m['stars']} | "
              f"{fmt(m['issue_median_reply_h'])} / {fmt(m['pr_median_reply_h'])} | {'-' if mr is None else f'{mr:.0%}'} | "
              f"{m['suitable_issues']} | {m['commits_30d']} | {'; '.join(m['flags']) or '-'} |")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", type=Path, default=ROOT / "profile.json")
    ap.add_argument("--repo", action="append", default=[],
                    help="evaluate a specific owner/name (repeatable); skips search and is never filtered out")
    ap.add_argument("--max-candidates", type=int, help="override filters.max_candidates")
    ap.add_argument("--top", type=int, help="override shortlist_size")
    args = ap.parse_args()

    for repo in args.repo:
        if not REPO_RE.match(repo):
            sys.exit(f"invalid repo name: {repo!r} (expected owner/name)")

    profile = load_profile(args.profile.resolve())
    check_account(profile["github_user"])
    now = datetime.now(timezone.utc)
    avoid = profile["skills"].get("avoid", [])
    max_candidates = args.max_candidates or profile["filters"]["max_candidates"]
    top = args.top or profile["shortlist_size"]
    pushed_cutoff = now - timedelta(days=profile["search"]["pushed_within_days"])

    dropped: list[dict] = []
    if args.repo:
        names = args.repo
    else:
        names = []
        for item in search_candidates(profile, now):
            hit = avoid_hit(" ".join([item["full_name"], item.get("description") or "", *item.get("topics", [])]), avoid)
            if hit:
                dropped.append({"repo": item["full_name"], "reason": f"matches avoid keyword '{hit}'"})
            else:
                names.append(item["full_name"])
        names = names[:max_candidates]

    evaluated: list[dict] = []
    for n, full_name in enumerate(names, 1):
        print(f"[{n}/{len(names)}] {full_name}", file=sys.stderr)
        try:
            m = evaluate(full_name, profile, now)
        except (GhError, KeyError, ValueError, subprocess.TimeoutExpired) as e:
            dropped.append({"repo": full_name, "reason": f"error: {e}"})
            continue
        if not args.repo:
            reason = ("archived" if m["archived"] else "fork" if m["fork"]
                      else "stale" if parse_ts(m["pushed_at"]) < pushed_cutoff
                      else f"only {m['suitable_issues']} suitable issues"
                      if m["suitable_issues"] < profile["filters"]["min_suitable_issues"] else None)
            if reason:
                dropped.append({"repo": full_name, "reason": reason})
                continue
        evaluated.append(m)

    evaluated.sort(key=lambda m: m["health_score"], reverse=True)
    shortlist = evaluated[:top]

    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / f"scout-{now.strftime('%Y-%m-%d-%H%M')}.json"
    out.write_text(json.dumps({
        "generated_at": now.isoformat(),
        "profile": profile,
        "shortlist": [m["repo"] for m in shortlist],
        "evaluated": evaluated,
        "dropped": dropped,
    }, indent=2))
    (DATA_DIR / "latest.json").write_text(out.read_text())

    print(f"Evaluated {len(evaluated)}, dropped {len(dropped)}. Full results: {out}\n")
    print_table(shortlist)


if __name__ == "__main__":
    main()
