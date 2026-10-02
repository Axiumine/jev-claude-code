# jev-claude-code repo

## Data
- research/data/catalog.json: entries are in `.entries[]` (do not recurse with `..`: nested `judge` objects also have `category`). model-router entries carry `routes` (claude|mixed|provider-pool|openai|effort-only|other, legend in categories.model-router.routesLegend). Filter on it before reading entries or calling Jev. Labels live in research/data/routes.json; build_catalog.py merges them, so edit routes.json, not catalog.json.
- Generated, compared byte-for-byte by the tests: research/data/catalog.json, research/JEV-CATALOG.md (build_catalog.py), research/jev-in-claude-code.html (build_page.py), research/reference-build/skill/typesafe-ai/SKILL.md and upstream-to-local.diff (build-skill.py). Change the inputs, rerun the script, commit inputs and outputs together.

## Python checks
- Gate: `scripts/check.sh [step ...]`, always in the order qodana, ruff, mypy, pytest, semgrep, trivy, mutmut. One run at a time: mutmut rebuilds mutants/, Qodana rewrites .qodana/.
- Required: 100% line and branch coverage, every mutmut mutant killed. `# pragma: no mutate` only on the utf-8 I/O lines (equivalent mutants); no `pragma: no cover`.
- Never read .env (QODANA_TOKEN): check.sh reads that one key itself.
