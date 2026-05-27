#!/usr/bin/env python3
"""
GitHub Watchdog — polls profile + repos every 20 seconds, reports any change.

Uses `gh api` (authenticated, 5000 req/hr). Hashes responses to detect
changes; only diffs when something actually moved.
"""
import json
import subprocess
import sys
import time
import hashlib
import os
from pathlib import Path
from datetime import datetime, timezone

# Force UTF-8 on Windows to avoid charmap encoding crashes
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

USERNAME = "COMMENCINGTHESCOURGE"
CACHE_DIR = Path(os.path.expandvars(r"%LOCALAPPDATA%")) / "hermes" / "watchdog"
POLL_INTERVAL = 20  # seconds

PROFILE_FIELDS = [
    "name", "bio", "blog", "location", "company",
    "twitter_username", "public_repos", "followers", "following",
]

REPO_FIELDS = [
    "name", "description", "topics", "license", "language",
    "stargazers_count", "forks_count", "open_issues_count",
    "updated_at", "pushed_at", "has_pages", "has_projects",
    "archived", "disabled", "visibility", "default_branch",
]


def gh_json(endpoint: str) -> dict | list:
    """Call `gh api <endpoint>` and return parsed JSON."""
    result = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json",
         "-H", "X-GitHub-Api-Version: 2022-11-28", endpoint],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api failed: {result.stderr[:200]}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def hash_data(data: dict | list, fields: list[str] | None = None) -> str:
    """Stable hash of monitored fields only."""
    if fields is not None and isinstance(data, dict):
        subset = {k: data.get(k) for k in fields if k in data}
        if "topics" in subset and isinstance(subset["topics"], list):
            subset["topics"] = sorted(subset["topics"])
    elif fields is not None and isinstance(data, list):
        subset = [
            {k: item.get(k) for k in fields if k in item}
            for item in data
        ]
    else:
        subset = data
    raw = json.dumps(subset, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def diff_profile(old: dict, new: dict) -> list[str]:
    changes = []
    for key in PROFILE_FIELDS:
        ov, nv = old.get(key), new.get(key)
        if ov != nv:
            changes.append(f"  profile.{key}: {ov!r} → {nv!r}")
    return changes


def diff_repos(old: list[dict], new: list[dict]) -> list[str]:
    changes = []
    old_map = {r["name"]: r for r in old}
    new_map = {r["name"]: r for r in new}

    for name in set(new_map) - set(old_map):
        desc = (new_map[name].get("description") or "")[:80]
        changes.append(f"  + NEW REPO: {name} — {desc}")

    for name in set(old_map) - set(new_map):
        changes.append(f"  - DELETED REPO: {name}")

    for name in set(old_map) & set(new_map):
        o, n = old_map[name], new_map[name]
        for key in REPO_FIELDS:
            ov, nv = o.get(key), n.get(key)
            if key == "topics":
                ov = sorted(ov) if isinstance(ov, list) else ov
                nv = sorted(nv) if isinstance(nv, list) else nv
            if ov != nv:
                changes.append(f"  {name}.{key}: {ov!r} → {nv!r}")

    return changes


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / "github_watchdog_cache.json"

    cache = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
        except json.JSONDecodeError:
            pass

    last_profile_hash = cache.get("profile_hash", "")
    last_repos_hash = cache.get("repos_hash", "")
    old_profile = cache.get("profile_data", {})
    old_repos = cache.get("repos_data", [])

    print(f"[{now()}] GitHub Watchdog — {USERNAME} — every {POLL_INTERVAL}s", flush=True)

    errors = 0

    while True:
        try:
            # ── Profile ────────────────────────────────────
            profile = gh_json(f"/users/{USERNAME}")
            ph = hash_data(profile, PROFILE_FIELDS)

            if ph != last_profile_hash:
                diffs = diff_profile(old_profile, profile)
                if diffs:
                    print(f"\n[{now()}] PROFILE:", flush=True)
                    for d in diffs:
                        print(d, flush=True)
                old_profile = profile
                last_profile_hash = ph
                cache["profile_data"] = profile
                cache["profile_hash"] = ph

            # ── Repos ──────────────────────────────────────
            repos = gh_json(f"/users/{USERNAME}/repos?per_page=100&sort=updated")
            rh = hash_data(repos, REPO_FIELDS)

            if rh != last_repos_hash:
                diffs = diff_repos(old_repos, repos)
                if diffs:
                    print(f"\n[{now()}] REPOS:", flush=True)
                    for d in diffs:
                        print(d, flush=True)
                old_repos = repos
                last_repos_hash = rh
                cache["repos_data"] = repos
                cache["repos_hash"] = rh

            # Save on change
            if ph != cache.get("profile_hash", "") or rh != cache.get("repos_hash", ""):
                pass  # Already updated above
            cache_file.write_text(json.dumps(cache, indent=2, default=str))

            errors = 0

        except Exception as e:
            errors += 1
            print(f"[{now()}] ERROR ({errors}): {e}", file=sys.stderr, flush=True)
            if errors > 10:
                print(f"[{now()}] FATAL: {errors} consecutive errors", file=sys.stderr, flush=True)
                sys.exit(1)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
