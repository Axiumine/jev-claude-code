#!/usr/bin/env bash
# merge-settings.sh - add the Jev hook entries and deny rules to ~/.claude/settings.json without touching anything else.
# Idempotent: entries that run a /hooks/jev/ script this snippet also installs are replaced (other Jev hooks stay), deny rules are
# appended only if missing, order is preserved. settings-snippet-nudge.json installs only the jev-nudge reminder hook.
# Run in YOUR terminal.   Default: dry run (prints a diff).   --apply: back up, then write.   --web-sync: make the webscreen handlers
# synchronous (needed when policy.mode.webscreen = "warn", because only a synchronous hook can add context to the tool result).
# Usage: merge-settings.sh [--apply] [--web-sync] [--settings PATH] [--snippet PATH]
# The snippet may use __HOME__ and __NODE__; NODE is $JEV_NODE, else ~/.nvm/current/bin/node if present, else `command -v node`.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"; SNIPPET="$HERE/settings-snippet.json"; APPLY=0; SYNC=0
while [ $# -gt 0 ]; do case "$1" in
  --apply) APPLY=1;; --web-sync) SYNC=1;; --settings) SETTINGS="$2"; shift;; --snippet) SNIPPET="$2"; shift;;
  *) echo "unknown arg $1" >&2; exit 2;; esac; shift; done
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }
[ -f "$SETTINGS" ] || { echo "no $SETTINGS" >&2; exit 2; }
jq -e . "$SETTINGS" >/dev/null || { echo "$SETTINGS is not valid JSON" >&2; exit 2; }
if [ -n "${JEV_NODE:-}" ]; then NODE="$JEV_NODE"; elif [ -x "$HOME/.nvm/current/bin/node" ]; then NODE="$HOME/.nvm/current/bin/node"; else NODE="$(command -v node || true)"; fi
[ -n "$NODE" ] || { echo "node not found; set JEV_NODE" >&2; exit 2; }
TMP="$(mktemp)"; SNIP="$(mktemp)"; trap 'rm -f "$TMP" "$SNIP"' EXIT
sed -e "s#__HOME__#$HOME#g" -e "s#__NODE__#$NODE#g" "$SNIPPET" > "$SNIP"
jq --slurpfile add "$SNIP" --argjson sync "$SYNC" '
  ([$add[0].hooks[]?[]?.hooks[]? | (.args // [])[0] // empty] | unique) as $mine |
  def ours: ((.args // [])[0] // "") as $a | ($a | contains("/hooks/jev/")) and ($mine | index($a));
  # drop our hooks one by one, so a user hook sharing a matcher entry with one of ours stays; drop an entry only if that empties it
  def strip: map(if ((.hooks // []) | any(ours)) then (.hooks |= map(select(ours | not))) | select(.hooks | length > 0) else . end);
  def unasync: if $sync == 1 then (.hooks |= map(del(.async))) else . end;
  (($add[0].hooks.PreToolUse  // []) | map(unasync)) as $pre  |
  (($add[0].hooks.PostToolUse // []) | map(unasync)) as $post |
  .hooks.PreToolUse  = (((.hooks.PreToolUse  // []) | strip) + $pre)  |
  .hooks.PostToolUse = (((.hooks.PostToolUse // []) | strip) + $post) |
  .hooks.PreToolUse |= map(if ((.hooks // []) | any((.command // "") | contains("no-secret-leak.cjs")))
                              and ((.matcher // "") | test("(^|[|])Monitor([|]|$)") | not)
                           then .matcher = ((.matcher // "") + "|Monitor") else . end) |
  (.permissions.deny // []) as $d |
  .permissions.deny = ($d + (($add[0].permissions.deny // []) | map(select(. as $x | ($d | index($x)) | not))))
' "$SETTINGS" > "$TMP"
jq -e . "$TMP" >/dev/null
if diff -u "$SETTINGS" "$TMP" >/dev/null; then echo "no change needed"; exit 0; fi
diff -u "$SETTINGS" "$TMP" || true
if [ "$APPLY" -eq 1 ]; then
  BAK="$SETTINGS.bak-jev-$(date +%Y%m%d-%H%M%S)"; cp -p "$SETTINGS" "$BAK"; chmod 600 "$BAK"
  cat "$TMP" > "$SETTINGS"      # keeps the original file mode and owner
  echo "applied. backup: $BAK  (rollback: cp -p '$BAK' '$SETTINGS')"
else
  echo "--- dry run only. Re-run with --apply to write."
fi
