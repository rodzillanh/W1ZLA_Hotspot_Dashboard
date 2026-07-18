#!/bin/bash
# Mirrors README.md to this repo's Forgejo wiki Home page.
#
# A Forgejo wiki is just a second git repo (<repo>.wiki.git) on the same
# host -- no CI/runner required, this is a plain clone/copy/commit/push.
# Run manually whenever README.md changes and you want the wiki to match:
#   bash sync-wiki.sh
#
# The wiki repo doesn't exist until the Wiki feature is turned on for this
# repo (repo -> Settings -> Features -> Wiki) or a first page is created via
# the web UI -- if that hasn't happened yet, this script tries to initialize
# it itself on first run.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_URL="https://git.trytheitguy.com/rodney_berry/W1ZLAHotspot_Dashboard"
WIKI_URL="${REPO_URL}.wiki.git"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "==> Fetching wiki..."
FRESH_INIT=false
if git clone --quiet "$WIKI_URL" "$WORK_DIR" 2>/tmp/sync-wiki-clone-err; then
    cd "$WORK_DIR"
else
    if grep -qi "not found\|does not exist\|repository not found\|could not read from remote repository" /tmp/sync-wiki-clone-err; then
        # Git deliberately gives this same generic message for both "no wiki
        # repo exists yet" and "you don't have push access" -- there's no way
        # to tell those apart from the clone error alone. Assume the former
        # and try to initialize; if it's actually a credentials problem, the
        # push below will fail with a clearer auth-specific error instead.
        echo "==> No wiki repo found -- assuming it hasn't been created yet and initializing one."
        FRESH_INIT=true
        git init --quiet "$WORK_DIR"
        cd "$WORK_DIR"
        git remote add origin "$WIKI_URL"
    else
        echo "==> Could not reach the wiki repo:"
        cat /tmp/sync-wiki-clone-err
        exit 1
    fi
fi
rm -f /tmp/sync-wiki-clone-err

echo "==> Copying README.md -> Home.md..."
cp "${SCRIPT_DIR}/README.md" "${WORK_DIR}/Home.md"

if [ "$FRESH_INIT" = false ] && git diff --quiet -- Home.md 2>/dev/null && [ -z "$(git status --porcelain -- Home.md)" ]; then
    echo "==> Wiki already up to date, nothing to push."
    exit 0
fi

git add Home.md
git commit --quiet -m "Sync Home page from README.md"
if [ "$FRESH_INIT" = true ]; then
    # No existing branch to track yet -- push whatever git init named the
    # local branch (init.defaultBranch) and set it as the tracked upstream,
    # rather than guessing "master" vs "main" for a brand-new wiki repo.
    if ! git push --quiet -u origin HEAD; then
        echo "==> Push failed. If Wiki isn't enabled for this repo yet, turn it on under"
        echo "    repo -> Settings -> Features -> Wiki (or create the first page via the"
        echo "    web UI) and re-run this script. If it IS enabled, this is likely a"
        echo "    credentials issue -- same as any other push to this host."
        exit 1
    fi
else
    git push --quiet
fi
echo "==> Wiki updated."
