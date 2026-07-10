"""Checks for updates to this app itself, comparing the locally deployed
build against the latest commit on its git remote's default branch.

The running app has no reliable way to know "what commit am I" on its own
(install.sh/update.sh copy plain files, no .git; the Docker image doesn't
even have the git binary installed) -- so install.sh, update.sh, and
docker-update.sh each write a BUILD_COMMIT file (just the commit SHA as
plain text) alongside the app at install/build time. This module reads
that file and compares it against the remote via the git host's REST API
(Forgejo/Gitea-compatible: GET /api/v1/repos/{owner}/{repo}/branches/
{branch}), confirmed against a real Forgejo instance before writing this.

Degrades silently on any failure (missing BUILD_COMMIT, unreachable git
host, unexpected response shape) -- same pattern as every other
integration client here (qrz.py, radioid.py, aslstats.py, ...): a flaky
git host or a dev checkout with no BUILD_COMMIT should never break the
Version page.
"""
import os
import re
import time
import threading
import urllib.request
import urllib.parse
import json

BUILD_COMMIT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "BUILD_COMMIT")
CHECK_TIMEOUT = 8
CACHE_TTL = 300  # seconds -- avoids hammering the git host on every page load


def _read_local_commit() -> str | None:
    try:
        with open(BUILD_COMMIT_PATH, "r") as f:
            commit = f.read().strip()
        return commit if commit and commit != "unknown" else None
    except OSError:
        return None


def _api_base(repo_url: str) -> str | None:
    """https://git.example.com/owner/repo(.git) -> https://git.example.com/api/v1/repos/owner/repo"""
    m = re.match(r"^https?://([^/]+)/([^/]+)/([^/]+?)(\.git)?/?$", (repo_url or "").strip())
    if not m:
        return None
    host, owner, repo = m.group(1), m.group(2), m.group(3)
    return f"https://{host}/api/v1/repos/{owner}/{repo}"


class UpdateChecker:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict | None = None
        self._cache_time = 0.0

    def check(self, repo_url: str, branch: str, force: bool = False) -> dict | None:
        """Returns None on any failure (caller shows "check failed" / hides
        the banner). Otherwise a dict with local_commit (may be None if this
        deployment has no BUILD_COMMIT -- e.g. a dev checkout), latest_commit,
        latest_message, latest_date, and update_available."""
        with self._lock:
            if not force and self._cache is not None and (time.time() - self._cache_time) < CACHE_TTL:
                return self._cache

        local_commit = _read_local_commit()
        api_base = _api_base(repo_url)
        if not api_base:
            return None

        try:
            req = urllib.request.Request(
                f"{api_base}/branches/{urllib.parse.quote(branch or 'main')}",
                headers={"User-Agent": "hotspot-dashboard-update-check"},
            )
            with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
                data = json.loads(resp.read())
            latest_commit  = data["commit"]["id"]
            latest_message = (data["commit"].get("message") or "").split("\n")[0]
            latest_date    = data["commit"].get("timestamp")
        except Exception:
            return None

        result = {
            "local_commit":     local_commit,
            "local_commit_short":  local_commit[:8] if local_commit else None,
            "latest_commit":    latest_commit,
            "latest_commit_short": latest_commit[:8],
            "latest_message":   latest_message,
            "latest_date":      latest_date,
            "update_available": bool(local_commit) and local_commit != latest_commit,
            "checked_at":       time.time(),
        }
        with self._lock:
            self._cache = result
            self._cache_time = time.time()
        return result
