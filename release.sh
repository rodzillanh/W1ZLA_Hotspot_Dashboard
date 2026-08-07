#!/bin/bash
# Tags the current commit with the app's version and creates a matching
# Forgejo Release, using that version's changelog entry from
# templates/version.html as the release notes body.
#
# Run this as the LAST step of the existing version-bump workflow, after
# APP_VERSION/VERSION_CODENAMES (config.py) and the changelog entry
# (templates/version.html) have been committed AND pushed to main --
# the release is created against whatever commit is currently HEAD.
#   bash release.sh
#
# Requires a Forgejo API token with repository write access. Git tag
# pushes reuse your existing git credentials (same as any other
# `git push`), but creating a Release goes through Forgejo's REST API
# (POST /repos/{owner}/{repo}/releases -- confirmed live against this
# host's own swagger.v1.json before writing this script, not assumed
# from GitHub's differently-shaped API), which needs a token, not a git
# credential. Generate one at:
#   https://git.trytheitguy.com/user/settings/applications
# (needs repository write access) and either:
#   export FORGEJO_TOKEN=xxxx   (once, in your shell profile), or
#   put it in a file named .forgejo-token in this same directory
#   (already covered by .gitignore -- never commit this file)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_BASE="https://git.trytheitguy.com/api/v1"
OWNER="rodney_berry"
REPO="W1ZLAHotspot_Dashboard"

if [ -z "$FORGEJO_TOKEN" ] && [ -f "${SCRIPT_DIR}/.forgejo-token" ]; then
    FORGEJO_TOKEN="$(tr -d '[:space:]' < "${SCRIPT_DIR}/.forgejo-token")"
fi
if [ -z "$FORGEJO_TOKEN" ]; then
    echo "==> No Forgejo API token found (checked \$FORGEJO_TOKEN and .forgejo-token)."
    echo "    Generate one at https://git.trytheitguy.com/user/settings/applications"
    echo "    (needs repository write access), then either:"
    echo "      export FORGEJO_TOKEN=xxxx && bash release.sh"
    echo "    or save it (just the token, nothing else) to:"
    echo "      ${SCRIPT_DIR}/.forgejo-token"
    exit 1
fi

VERSION="$(python3 -c "import config; print(config.APP_VERSION)")"
CODENAME="$(python3 -c "import config; print(config.APP_CODENAME)")"
TAG="v${VERSION}"
TITLE="v${VERSION} \"${CODENAME}\""

echo "==> Releasing ${TITLE} (tag ${TAG}) against $(git rev-parse --short HEAD)..."

# The current release's changelog block is always the FIRST
# <div class="release"> in templates/version.html -- see the version-bump
# workflow (a new block is inserted at the top on every bump, "current"
# moved onto it). Strip inline tags (e.g. <code>) from each <li>, not just
# the outer <ul>, so the release notes read as plain text/Markdown.
#
# PYTHONIOENCODING=utf-8 matters here, not just as a nicety -- this
# shell's default Python stdout encoding is cp1252 (confirmed live), which
# silently mangles the em-dashes ("—") every changelog entry in this repo
# uses, rather than raising an error that would've caught it immediately.
NOTES="$(PYTHONIOENCODING=utf-8 python3 - "${SCRIPT_DIR}/templates/version.html" <<'PY'
import re, sys
html = open(sys.argv[1], encoding='utf-8').read()
m = re.search(r'<div class="release">.*?<ul class="change-list">(.*?)</ul>', html, re.S)
if not m:
    sys.exit("Could not find the current release's changelog block in version.html")
items = re.findall(r'<li>(.*?)</li>', m.group(1), re.S)
if not items:
    sys.exit("Found the changelog block but no <li> entries in it")

def clean(s):
    s = re.sub(r'<[^>]+>', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

print('\n'.join(f"- {clean(i)}" for i in items))
PY
)"

if [ -z "$(git tag -l "$TAG")" ]; then
    echo "==> Creating tag ${TAG}..."
    git tag "$TAG"
fi
# Push unconditionally, even if the tag already existed locally -- a
# previous run can create the local tag and then have the push itself
# fail (confirmed live: the same expired-credentials issue plain
# `git push` hits on this host). `git push` on an already-pushed tag is a
# harmless no-op, so this is always safe to (re-)run rather than
# assuming "exists locally" also means "reached the remote."
echo "==> Pushing tag ${TAG}..."
git push origin "$TAG"

echo "==> Creating Forgejo release..."
# NOTES goes over stdin, not argv -- an argument containing non-ASCII text
# (em-dashes, curly quotes) is a second, separate encoding risk on top of
# the stdout one above (shell argv decoding has its own locale-dependent
# behavior on Windows), so it's safer to sidestep entirely rather than
# assume it round-trips correctly.
PAYLOAD="$(PYTHONIOENCODING=utf-8 python3 -c "
import json, sys
tag, title = sys.argv[1], sys.argv[2]
notes = sys.stdin.read()
print(json.dumps({'tag_name': tag, 'name': title, 'body': notes}))
" "$TAG" "$TITLE" <<< "$NOTES")"

HTTP_CODE="$(curl -s -o /tmp/release-response.json -w "%{http_code}" -X POST \
    -H "Authorization: token ${FORGEJO_TOKEN}" \
    -H "Content-Type: application/json" \
    "${API_BASE}/repos/${OWNER}/${REPO}/releases" \
    -d "$PAYLOAD")"

case "$HTTP_CODE" in
    201)
        echo "==> Release ${TITLE} created."
        ;;
    409)
        echo "==> A release for tag ${TAG} already exists -- nothing to do."
        ;;
    *)
        echo "==> Release creation failed (HTTP ${HTTP_CODE}):"
        cat /tmp/release-response.json
        rm -f /tmp/release-response.json
        exit 1
        ;;
esac
rm -f /tmp/release-response.json
