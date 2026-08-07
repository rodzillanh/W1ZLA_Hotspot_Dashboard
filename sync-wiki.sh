#!/bin/bash
# Splits README.md into grouped wiki pages and pushes them to this repo's
# Forgejo wiki, replacing the old single-Home-page dump.
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
#
# Page grouping: every `## ` heading in README.md must be preceded by a
# `<!-- wiki-group: <Group Name> -->` comment (invisible in both GitHub's
# rendering and this app's own /readme page -- marked.js passes HTML
# comments straight through into the DOM, where the browser doesn't show
# them). Each distinct group becomes its own wiki page; a Home.md overview
# and a _Sidebar.md (Forgejo's own hidden file that replaces the default
# flat alphabetical page list with custom grouped navigation) are generated
# from the same markers -- see split_readme_wiki.py below, embedded inline
# rather than as a separate file since every other one-off script in this
# repo (install.sh/update.sh/uninstall.sh/docker-update.sh) is a single
# self-contained file too.
#
# Adding a new README section: put a `<!-- wiki-group: ... -->` line right
# above its `## ` heading, reusing an existing group name (Getting Started /
# Dashboard and Live Map / Hotspot Types / Integrations / Optional Cards /
# Admin and Maintenance) or introducing a new one -- either way it flows
# through automatically. An UNMARKED heading is a hard error from the
# Python step below, not a silent drop, so a forgotten marker can't quietly
# vanish from the wiki instead of failing loudly.

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

# Snapshot which files THIS SCRIPT generated last run, before Python
# overwrites the manifest below -- lets us prune a page whose group got
# renamed/removed without ever touching a page this script didn't create
# (a hand-added wiki page some day would survive a resync untouched).
OLD_MANIFEST=""
if [ -f "${WORK_DIR}/.wiki-sync-manifest" ]; then
    OLD_MANIFEST="$(cat "${WORK_DIR}/.wiki-sync-manifest")"
fi

echo "==> Splitting README.md into grouped wiki pages..."
python3 - "${SCRIPT_DIR}/README.md" "$WORK_DIR" <<'PY'
import re
import sys
import os

readme_path, work_dir = sys.argv[1], sys.argv[2]

MARKER_RE  = re.compile(r'^<!--\s*wiki-group:\s*(.+?)\s*-->\s*$')
HEADING_RE = re.compile(r'^## (.+)$')  # exactly two #'s -- ###+ subheadings stay inside their section's body


def slugify(name):
    return re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-')


with open(readme_path, encoding='utf-8') as f:
    lines = f.read().split('\n')

intro_lines = []
sections = []       # [{group, heading, body: [lines incl. the "## Heading" line itself]}]
pending_group = None
current = None

for line in lines:
    m_marker = MARKER_RE.match(line)
    if m_marker:
        pending_group = m_marker.group(1)
        continue
    m_heading = HEADING_RE.match(line)
    if m_heading:
        if current is not None:
            sections.append(current)
        if pending_group is None:
            sys.exit(
                f"ERROR: README.md heading {m_heading.group(1)!r} has no preceding "
                f"<!-- wiki-group: ... --> marker -- add one above it (see sync-wiki.sh)."
            )
        current = {'group': pending_group, 'heading': m_heading.group(1), 'body': [line]}
        pending_group = None
        continue
    (current['body'] if current is not None else intro_lines).append(line)
if current is not None:
    sections.append(current)

# Group sections in first-seen order, so page/sidebar order tracks marker
# placement in README.md rather than needing a second ordering list here.
group_order = []
group_sections = {}
for s in sections:
    g = s['group']
    if g not in group_sections:
        group_sections[g] = []
        group_order.append(g)
    group_sections[g].append(s)

title_match = re.match(r'^# (.+)$', intro_lines[0]) if intro_lines else None
title = title_match.group(1) if title_match else "Home"
intro_body = '\n'.join(intro_lines[1:] if title_match else intro_lines).strip('\n')

manifest = []


def write(filename, content):
    with open(os.path.join(work_dir, filename), 'w', encoding='utf-8', newline='\n') as f:
        f.write(content.rstrip('\n') + '\n')
    manifest.append(filename)


for group in group_order:
    parts = [f"# {group}", ""]
    for s in group_sections[group]:
        parts.append('\n'.join(s['body']).rstrip('\n'))
        parts.append("")
    write(f"{slugify(group)}.md", '\n'.join(parts))

home_parts = [f"# {title}", "", intro_body, "", "## Contents", ""]
for group in group_order:
    headings = ', '.join(s['heading'] for s in group_sections[group])
    home_parts.append(f"- **[[{group}]]** — {headings}")
write("Home.md", '\n'.join(home_parts))

sidebar_parts = ["**[[Home]]**", ""]
for group in group_order:
    sidebar_parts.append(f"- [[{group}]]")
write("_Sidebar.md", '\n'.join(sidebar_parts))

# .wiki-sync-manifest itself isn't a real page -- Forgejo just serves it as
# a plain file since it doesn't end in .md -- but it still needs pruning
# logic applied to IT too on a future run if this script's own output
# scheme ever changes, so it lists itself.
manifest.append(".wiki-sync-manifest")
with open(os.path.join(work_dir, ".wiki-sync-manifest"), 'w', encoding='utf-8', newline='\n') as f:
    f.write('\n'.join(sorted(manifest)) + '\n')

print(f"==> Generated {len(group_order)} group page(s) + Home.md + _Sidebar.md ({len(sections)} README section(s) total).")
PY

echo "==> Pruning any pages this script generated previously but not this time..."
if [ -n "$OLD_MANIFEST" ]; then
    while IFS= read -r old_file; do
        [ -z "$old_file" ] && continue
        if ! grep -qxF "$old_file" "${WORK_DIR}/.wiki-sync-manifest" 2>/dev/null; then
            if [ -f "${WORK_DIR}/${old_file}" ]; then
                echo "    removing stale generated page: ${old_file}"
                rm -f "${WORK_DIR}/${old_file}"
            fi
        fi
    done <<< "$OLD_MANIFEST"
fi

cd "$WORK_DIR"
git add -A

if [ "$FRESH_INIT" = false ] && git diff --cached --quiet; then
    echo "==> Wiki already up to date, nothing to push."
    exit 0
fi

git commit --quiet -m "Sync grouped wiki pages from README.md"
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
