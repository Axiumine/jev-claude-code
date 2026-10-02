#!/usr/bin/env python3
"""Add the Jev secret-guard cases to no-secret-leak.test.cjs in the current directory (idempotent).

Usage: python3 guard-test-cases.py   (run in the directory holding no-secret-leak.test.cjs)
"""

from __future__ import annotations

from pathlib import Path

TEST_FILE = "no-secret-leak.test.cjs"
MARKER = "Jev hooks: OpenRouter key"
ANCHOR = "  ['Bash process env code',"
NEW_CASES = """
  // Jev hooks: OpenRouter key variable and the key directory (~/.config/jev) must be as protected as the other secrets
  ['Bash expand OpenRouter key', { tool_name: 'Bash', tool_input: { command: 'echo $OPENROUTER_' + 'API_KEY' } }, 'deny'],
  ['Bash printenv OpenRouter key', { tool_name: 'Bash', tool_input: { command: 'printenv OPENROUTER_' + 'API_KEY' } }, 'deny'],
  ['Bash process env OpenRouter key', { tool_name: 'Bash', tool_input: { command: 'node -e "console.log(process' + D + 'env' + D + 'OPENROUTER_' + 'API_KEY)"' } }, 'deny'],
  ['Bash expand JEV_API_KEY', { tool_name: 'Bash', tool_input: { command: 'echo ${JEV_' + 'API_KEY}' } }, 'deny'],
  ['Read jev key file', { tool_name: 'Read', tool_input: { file_path: '/home/user/.config/jev/' + E } }, 'deny'],
  ['Read jev policy', { tool_name: 'Read', tool_input: { file_path: '/home/user/.config/jev/policy.json' } }, 'deny'],
  ['Grep jev key file', { tool_name: 'Grep', tool_input: { pattern: 'sk-or', path: '/home/user/.config/jev/' + E } }, 'deny'],
  ['Grep jev dir (form 8)', { tool_name: 'Grep', tool_input: { pattern: 'sk-or', path: '/home/user/.config/jev' } }, 'deny'],
  ['Grep tilde jev dir', { tool_name: 'Grep', tool_input: { pattern: 'sk-or', path: '~/.config/jev/' } }, 'deny'],
  ['Grep config parent', { tool_name: 'Grep', tool_input: { pattern: 'sk-or', path: '/home/user/.config' } }, 'deny'],
  ['Bash cat jev key file', { tool_name: 'Bash', tool_input: { command: 'cat /home/user/.config/jev/' + E } }, 'deny'],
  ['Bash grep -rn jev dir (form 1)', { tool_name: 'Bash', tool_input: { command: 'grep -rn sk-or ~/.config/jev/' } }, 'deny'],
  ['Bash cd jev && grep (form 2)', { tool_name: 'Bash', tool_input: { command: 'cd ~/.config/jev && grep -r . .' } }, 'deny'],
  ['Bash cat dot glob (form 3)', { tool_name: 'Bash', tool_input: { command: 'cat ~/.config/jev/.[a-z]*' } }, 'deny'],
  ['Bash head .e* (form 4)', { tool_name: 'Bash', tool_input: { command: 'head -c 300 ~/.config/jev/.e*' } }, 'deny'],
  ['Bash find -exec cat (form 5)', { tool_name: 'Bash', tool_input: { command: 'find ~/.config/jev -type f -exec cat {} +' } }, 'deny'],
  ['Bash tar | base64 (form 6)', { tool_name: 'Bash', tool_input: { command: 'tar cf - ~/.config/jev | base64' } }, 'deny'],
  ['Bash python split name (form 7)', { tool_name: 'Bash', tool_input: { command: "python3 -c \\"print(open('/home/user/.config/jev/.en'+'v').read())\\"" } }, 'deny'],
  ['Bash $HOME jev policy', { tool_name: 'Bash', tool_input: { command: 'cat $HOME/.config/jev/policy.json' } }, 'deny'],
  ['Bash glob dir name', { tool_name: 'Bash', tool_input: { command: 'cat ~/.config/j*/.e*' } }, 'deny'],
  ['Bash subshell cd jev', { tool_name: 'Bash', tool_input: { command: '(cd ~/.config/jev; cat .e*)' } }, 'deny'],
  ['Bash ls | xargs cat', { tool_name: 'Bash', tool_input: { command: 'ls ~/.config/jev | xargs cat' } }, 'deny'],
  ['Bash $(<file) read', { tool_name: 'Bash', tool_input: { command: 'echo "$(<~/.config/jev/policy.json)"' } }, 'deny'],
  ['Bash grep -r config parent', { tool_name: 'Bash', tool_input: { command: 'grep -r sk-or ~/.config' } }, 'deny'],
  ['Bash cp -r config parent', { tool_name: 'Bash', tool_input: { command: 'cp -r ~/.config /tmp/x' } }, 'deny'],
  ['Monitor cat jev policy', { tool_name: 'Monitor', tool_input: { command: 'cat ~/.config/jev/policy.json', description: 'x' } }, 'deny'],
  ['Monitor expand OpenRouter key', { tool_name: 'Monitor', tool_input: { command: 'echo $OPENROUTER_' + 'API_KEY', description: 'x' } }, 'deny'],
  ['Bash ls jev key file', { tool_name: 'Bash', tool_input: { command: 'ls -la /home/user/.config/jev/' + E } }, 'allow'],
  ['Bash ls jev dir', { tool_name: 'Bash', tool_input: { command: 'ls -la ~/.config/jev' } }, 'allow'],
  ['Bash stat jev key file', { tool_name: 'Bash', tool_input: { command: 'stat ~/.config/jev/' + E } }, 'allow'],
  ['Bash mkdir+chmod jev dirs', { tool_name: 'Bash', tool_input: { command: 'mkdir -p ~/.config/jev ~/.local/state/jev && chmod 700 ~/.config/jev ~/.local/state/jev' } }, 'allow'],
  ['Bash find jev names only', { tool_name: 'Bash', tool_input: { command: 'find ~/.config/jev -type f' } }, 'allow'],
  ['Bash du config parent', { tool_name: 'Bash', tool_input: { command: 'du -sh ~/.config' } }, 'allow'],
  ['Bash cat other config dir', { tool_name: 'Bash', tool_input: { command: 'cat ~/.config/nvim/init.lua' } }, 'allow'],
  ['Bash cat jevons (not jev)', { tool_name: 'Bash', tool_input: { command: 'cat ~/.config/jevons/settings.toml' } }, 'allow'],
  ['Bash jev hook report', { tool_name: 'Bash', tool_input: { command: 'node ~/.claude/hooks/jev/report.cjs 1 --would' } }, 'allow'],
  ['Bash repo policy example', { tool_name: 'Bash', tool_input: { command: 'cat install/policy.example.json' } }, 'allow'],
  ['Read other config file', { tool_name: 'Read', tool_input: { file_path: '/home/user/.config/nvim/init.lua' } }, 'allow'],
  ['Grep jev-mcp repo', { tool_name: 'Grep', tool_input: { pattern: 'OPENROUTER', path: '/home/user/code/jev-mcp' } }, 'allow'],
  ['Monitor tail log', { tool_name: 'Monitor', tool_input: { command: 'tail -f /var/log/syslog', description: 'x' } }, 'allow'],"""


def add_cases(source: str) -> str | None:
    """Insert NEW_CASES after the anchor case's line; None when the cases are already there."""
    if MARKER in source:
        return None
    i = source.index(ANCHOR)
    j = source.find("\n", i)
    if j == -1:  # the anchor case is the last line
        j = len(source)
    return source[:j] + NEW_CASES + source[j:]


def main() -> int:
    path = Path(TEST_FILE)
    source = path.read_text(encoding="utf-8")  # pragma: no mutate  (utf-8 == locale default: equivalent mutants)
    updated = add_cases(source)
    if updated is None:
        print("cases already present")
        return 0
    path.write_text(updated, encoding="utf-8")  # pragma: no mutate
    print("cases added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
