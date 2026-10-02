# jev-claude-code repo

## Data
- research/data/catalog.json: entries are in `.entries[]` (do not recurse with `..`: nested `judge` objects also have `category`). model-router entries carry `routes` (claude|mixed|provider-pool|openai|effort-only|other, legend in categories.model-router.routesLegend). Filter on it before reading entries or calling Jev. Labels live in research/data/routes.json; build_catalog.py merges them, so edit routes.json, not catalog.json.
