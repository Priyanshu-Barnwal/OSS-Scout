# oss-scout

Finds and ranks open-source projects worth contributing to.

- **Stage 1, `scout.py`** (deterministic): GitHub search → health metrics → filter → `health_score` (0-100).
- **Stage 2, the `scout` subagent** (LLM): reads the shortlisted repos, scores skill stretch, readability and
  AI-contribution policy, and writes `reports/ranking-YYYY-MM-DD.md`.

Uses only the Python 3.10 standard library and the `gh` CLI.

## One-time setup

All GitHub API calls use a **separate gh config dir** (`~/.config/gh-oss`) logged into your personal account.
The machine's default `gh` login (work) is never used, and `GH_TOKEN`/`GITHUB_TOKEN` are stripped.

Recommended: log in with a **fine-grained token** that has *Public repositories (read-only)* access only.
Then the scout can't write to GitHub even by mistake:

```bash
# create the token at github.com/settings/personal-access-tokens (signed in to your personal account), then:
GH_CONFIG_DIR=~/.config/gh-oss gh auth login --with-token < /path/to/token.txt   # delete the file afterwards
GH_CONFIG_DIR=~/.config/gh-oss gh auth status
```

(`GH_CONFIG_DIR=~/.config/gh-oss gh auth login` with browser auth also works, but it grants write scopes.)

`scout.py` checks that the authenticated user matches `github_user` in `profile.json` and aborts otherwise.

Then fill in `skills.strong` in `profile.json`. Stretch scoring is only approximate until you do.

## Usage

```bash
cd ~/oss-scout
claude                       # then: "use the scout agent to find projects"

# or run stage 1 alone
python3 scout.py                                  # search + score using profile.json
python3 scout.py --repo pydantic/pydantic-ai       # evaluate specific repos you like (never filtered out)
python3 scout.py --max-candidates 10 --top 5       # quick run
```

Output: `data/scout-<timestamp>.json` + `data/latest.json`, and a markdown table on stdout.

## Health score (0-100)

| Component | Points | Signal |
|---|---|---|
| Responsiveness | 30 | Mean of the issue and PR medians of hours to first maintainer response (comment, review, merge or close by a non-bot maintainer) on outsider items from the last 180 days, scaled by reply rate |
| Outsider merge rate | 25 | Merged / closed PRs authored by non-maintainers (needs ≥3 samples) |
| Issue supply | 20 | Open issues with beginner/help-wanted labels, unassigned, no linked open/merged PR |
| Activity | 15 | Commits on default branch in last 30 days + release in last 90 days |
| Friendliness | 10 | CONTRIBUTING.md present + license |

Final rank (stage 2) = `0.6 × health + 2 × stretch + 2 × readability`.

## Known limitations

- `authorAssociation` shows `MEMBER` only for **public** org members. Maintainers with private membership
  look like outsiders, which can make responsiveness look worse and the merge rate look better.
- "Suitable issues" doesn't detect "I'm working on this" comments. The issue-analyst agent should re-check.
- Samples are the 40 most recent issues and 40 most recent PRs, so very busy repos are judged on recent weeks only.
- Search returns at most `per_query` repos per query. Add your own queries or `--repo` for projects you already like.

## Git for contributions

Clone with your personal SSH alias and set a personal identity per repo:

```bash
git clone git@github.com-personal:OWNER/REPO.git
git -C REPO config user.email "<your personal email>"
```
