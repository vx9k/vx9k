#!/usr/bin/env python3
"""Regenerate the projects block of README.md from the GitHub API.

Lists:
  * every public, non-fork repo owned by USER;
  * every public repo in ORG that USER has commits in (contributors list);
  * forks (of either) only when they carry commits by USER ahead of upstream.

Then writes them, plus a language breakdown across those repos, between the
<!-- projects:start --> and <!-- projects:end --> markers. The "last updated"
date only changes when the generated content does, so a scheduled run with
nothing new produces no commit.

Standard library only. Reads GITHUB_TOKEN (or GH_TOKEN) if set.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

USER = "vx9k"
ORG = "poopware-laboratories"
README = Path(__file__).resolve().parent.parent / "README.md"
START, END = "<!-- projects:start -->", "<!-- projects:end -->"

# Repos never listed: the profile repo itself and org metadata.
EXCLUDE = {f"{USER}/{USER}", f"{ORG}/.github"}
# Fallback blurbs for repos with no GitHub description.
BLURBS = {
    f"{USER}/dots": "Dotfiles.",
    f"{USER}/nvim": "My Neovim config.",
    f"{ORG}/pooplinux": "A Linux distribution built from source, on our own kernel fork, init and package manager.",
}
LANG_COUNT = 8  # languages shown in the breakdown
BAR = 24  # width of the breakdown bars

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def get(path: str):
    """GET an API path; returns (status, json). Raises on network errors."""
    req = urllib.request.Request(API + path)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", f"{USER}-profile-readme")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def get_all(path: str) -> list:
    out, page = [], 1
    sep = "&" if "?" in path else "?"
    while True:
        status, data = get(f"{path}{sep}per_page=100&page={page}")
        if status != 200:
            raise SystemExit(f"GET {path} failed: HTTP {status}: {data}")
        if not data:
            return out
        out += data
        if len(data) < 100:
            return out
        page += 1


def my_commits(repo: dict) -> int:
    """Commits by USER in this repo; for forks, only those ahead of upstream."""
    name = repo["full_name"]
    if repo["fork"]:
        status, full = get(f"/repos/{name}")
        parent = (full or {}).get("parent")
        if status != 200 or not parent:
            return 0
        status, cmp = get(
            f"/repos/{parent['full_name']}/compare/"
            f"{parent['default_branch']}...{repo['owner']['login']}:{repo['default_branch']}"
        )
        if status != 200:
            return 0
        return sum(1 for c in cmp.get("commits", []) if (c.get("author") or {}).get("login") == USER)

    status, data = get(f"/repos/{name}/contributors?per_page=100")
    if status == 200 and isinstance(data, list):
        return next((c["contributions"] for c in data if c.get("login") == USER), 0)
    # 204: empty repo. 403: history too large to list; fall back to a probe.
    status, data = get(f"/repos/{name}/commits?author={USER}&per_page=1")
    return 1 if status == 200 and data else 0


def collect() -> tuple[list[dict], list[dict]]:
    own = [r for r in get_all(f"/users/{USER}/repos?type=owner") if not r["private"]]
    org = [r for r in get_all(f"/orgs/{ORG}/repos?type=public")]

    def keep(repos: list[dict]) -> list[dict]:
        kept = []
        for r in repos:
            if r["full_name"] in EXCLUDE or r["private"]:
                continue
            n = my_commits(r)
            if n > 0:
                kept.append({**r, "mine": n})
        return sorted(kept, key=lambda r: (-r["mine"], r["name"].lower()))

    return keep(org), keep(own)


def languages(repos: list[dict]) -> list[tuple[str, float]]:
    totals: dict[str, int] = {}
    for r in repos:
        status, langs = get(f"/repos/{r['full_name']}/languages")
        if status == 200 and langs:
            for lang, n in langs.items():
                totals[lang] = totals.get(lang, 0) + n
    whole = sum(totals.values()) or 1
    top = sorted(totals.items(), key=lambda kv: -kv[1])[:LANG_COUNT]
    return [(lang, 100 * n / whole) for lang, n in top]


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def table(repos: list[dict]) -> str:
    rows = [
        "| Project | What it is | Language | ★ | My commits |",
        "| :-- | :-- | :-: | :-: | --: |",
    ]
    for r in repos:
        blurb = r.get("description") or BLURBS.get(r["full_name"], "")
        lang = f"`{r['language']}`" if r.get("language") else "–"
        tag = " _(fork)_" if r["fork"] else ""
        rows.append(
            f"| **[{r['name']}]({r['html_url']})**{tag} | {cell(blurb)} | {lang} "
            f"| {r['stargazers_count']} | {r['mine']} |"
        )
    return "\n".join(rows)


def lang_block(langs: list[tuple[str, float]]) -> str:
    if not langs:
        return ""
    width = max(len(l) for l, _ in langs)
    lines = []
    for lang, pct in langs:
        filled = round(BAR * pct / 100)
        lines.append(f"{lang:<{width}}  {'█' * filled}{'░' * (BAR - filled)}  {pct:5.1f}%")
    return "```text\n" + "\n".join(lines) + "\n```"


def render(org: list[dict], own: list[dict], langs) -> str:
    total = sum(r["mine"] for r in org + own)
    parts = [
        f"### 🧪 [Poopware Laboratories](https://github.com/{ORG})",
        "",
        "Where I'm a co-founder. Only the repos I've actually committed to.",
        "",
        table(org) if org else "_Nothing yet._",
        "",
        "### 🛠️ Personal",
        "",
        table(own) if own else "_Nothing yet._",
        "",
        "### 📊 What it's written in",
        "",
        f"<sub>Bytes of code across the {len(org) + len(own)} repos above "
        f"({total} commits of mine on their default branches).</sub>",
        "",
        lang_block(langs),
    ]
    return "\n".join(parts)


STAMP = re.compile(r"\n*<sub><i>Generated by .*?</i></sub>\s*$", re.S)


def main() -> int:
    text = README.read_text(encoding="utf-8")
    try:
        head, rest = text.split(START, 1)
        old, tail = rest.split(END, 1)
    except ValueError:
        print(f"README.md needs {START} and {END} markers", file=sys.stderr)
        return 1

    org, own = collect()
    body = render(org, own, languages(org + own))

    if STAMP.sub("", old).strip() == body.strip():
        print("projects block unchanged")
        return 0

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    stamp = (
        f"<sub><i>Generated by <a href=\"scripts/update_projects.py\">update_projects.py</a> "
        f"on {today}.</i></sub>"
    )
    README.write_text(f"{head}{START}\n{body}\n\n{stamp}\n{END}{tail}", encoding="utf-8")
    print(f"projects block updated: {len(org)} org repos, {len(own)} personal repos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
