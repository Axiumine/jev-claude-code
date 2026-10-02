#!/usr/bin/env bash
# Quality gate for the Python code, in this order:
#   qodana (Ultimate Plus) -> ruff -> mypy -> pytest (100% line+branch coverage) -> semgrep -> trivy -> mutmut
# Usage: scripts/check.sh [step ...]   (default: every step; steps always run in the order above)
# Needs uv and Docker. Qodana also needs the Qodana CLI and QODANA_TOKEN, taken from the
# environment or else from the last QODANA_TOKEN=... line of .env (git-ignored). The token reaches
# qodana only through the environment of that one child process: never a command line, a file or
# an xtrace line.
# Pinned: the tools (uv.lock, SEMGREP_VERSION, image digests). Live on purpose: the semgrep rule
# packs and the trivy vulnerability database, so new rules and advisories fail the gate.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

STEPS=(qodana ruff mypy pytest semgrep trivy mutmut)
SEMGREP_VERSION=1.179.0
TRIVY_IMAGE=aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa
QODANA_PYTHON=/data/cache/jev-venv/bin/python # in the container, made by the bootstrap in qodana.yaml

MASK="" # scratch stand-ins mounted over private files in the Qodana container
trap 'if [[ -n $MASK ]]; then rm -rf "$MASK"; fi' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Every .env, .env.* and *.env file outside .git and the tool directories, NUL-separated.
env_files() {
	find . \( -path ./.git -o -path ./.venv -o -path ./mutants -o -path ./.qodana \) -prune -o \
		-type f \( -name .env -o -name '.env.*' -o -name '*.env' \) -print0
}

# QODANA_TOKEN from the environment, else from the last `[export] QODANA_TOKEN=value` line of .env:
# the value may be quoted; unquoted, it ends at ` #`.
qodana_token() {
	if [[ -n ${QODANA_TOKEN:-} ]]; then
		printf '%s' "$QODANA_TOKEN"
		return
	fi
	[[ -f .env ]] || return 0
	local line value
	line=$(grep -E '^[[:space:]]*(export[[:space:]]+)?QODANA_TOKEN[[:space:]]*=' .env | tail -n 1) || return 0
	value=${line#*=}
	value=${value%$'\r'}
	value=${value#"${value%%[![:space:]]*}"}
	case $value in
	\"*) value=${value#\"} value=${value%%\"*} ;;
	\'*) value=${value#\'} value=${value%%\'*} ;;
	*) value=${value%%[[:space:]]#*} value=${value%"${value##*[![:space:]]}"} ;;
	esac
	printf '%s' "$value"
}

qodana_scan() {
	local token
	token=$(qodana_token)
	if [[ -z $token ]]; then
		echo "QODANA_TOKEN missing: export it or add a QODANA_TOKEN=... line to .env" >&2
		return 1
	fi
	# The container mounts the project read-write and caches every file it can read, so it gets
	# empty stand-ins for the env files, .idea (it would rewrite the IDE config), .venv (a host
	# interpreter) and mutants (a second copy of the sources).
	local name status=0 volumes=()
	MASK=$(mktemp -d)
	mkdir -p "$MASK/empty" "$MASK/idea"
	: >"$MASK/nothing"
	if [[ -d .idea ]]; then volumes+=(--volume "$MASK/idea:/data/project/.idea"); fi
	for name in .venv mutants; do
		if [[ -d $name ]]; then volumes+=(--volume "$MASK/empty:/data/project/$name"); fi
	done
	while IFS= read -r -d '' name; do
		volumes+=(--volume "$MASK/nothing:/data/project/${name#./}")
	done < <(env_files)
	rm -rf .qodana/results
	# The interpreter for the analysis: see bootstrap in qodana.yaml.
	QODANA_TOKEN="$token" qodana scan --results-dir .qodana/results --env "QODANA_PYTHON_PATH=$QODANA_PYTHON" \
		"${volumes[@]}" --print-problems || status=$?
	rm -rf "$MASK"
	MASK=""
	return "$status"
}

run_qodana() {
	# xtrace (bash -x) would print the token: off in here, restored afterwards.
	local status=0 xtrace=""
	if [[ $- == *x* ]]; then xtrace=1; fi
	{ set +x; } 2>/dev/null
	qodana_scan || status=$?
	if [[ -n $xtrace ]]; then set -x; fi
	return "$status"
}

# --locked: a uv.lock that no longer matches pyproject.toml fails instead of being ignored.
run_ruff() {
	uv run --locked ruff check .
	uv run --locked ruff format --check .
}

run_mypy() {
	uv run --locked mypy
}

run_pytest() {
	# fail_under = 100 and branch coverage come from [tool.coverage] in pyproject.toml.
	uv run --locked pytest --cov --cov-report=term-missing
}

run_semgrep() {
	# Targets: files git tracks or does not ignore, minus .semgrepignore.
	uvx "semgrep==$SEMGREP_VERSION" scan --error --metrics=off --disable-version-check \
		--config p/python --config p/security-audit --config p/secrets
}

run_trivy() {
	docker run --rm -v "$ROOT:/src:ro" -v trivy-cache:/root/.cache/trivy "$TRIVY_IMAGE" \
		fs --exit-code 1 --scanners vuln,secret,misconfig --include-dev-deps \
		--skip-dirs .git --skip-dirs .venv --skip-dirs mutants --skip-dirs .qodana \
		--skip-files '**/.env' --skip-files '**/.env.*' --skip-files '**/*.env' /src
}

run_mutmut() {
	rm -rf mutants
	uv run --locked mutmut run
	local left
	left=$(uv run --locked mutmut results)
	if [[ -n $left ]]; then
		printf '%s\n' "$left"
		echo "mutmut: the mutants above were not killed" >&2
		return 1
	fi
	echo "mutmut: every mutant killed"
}

need() {
	command -v "$1" >/dev/null || {
		echo "missing tool: $1 (needed by the $2 step)" >&2
		exit 2
	}
}

selected=("$@")
((${#selected[@]})) || selected=("${STEPS[@]}")
# Unknown steps and missing tools fail up front, before any slow step runs.
for s in "${selected[@]}"; do
	[[ " ${STEPS[*]} " == *" $s "* ]] || {
		echo "unknown step: $s (steps: ${STEPS[*]})" >&2
		exit 2
	}
	case $s in
	qodana) need qodana qodana && need docker qodana ;;
	trivy) need docker trivy ;;
	*) need uv "$s" ;;
	esac
done
for s in "${STEPS[@]}"; do
	[[ " ${selected[*]} " == *" $s "* ]] || continue
	printf '\n==> %s\n' "$s"
	"run_$s"
done
printf '\nAll checks passed: %s\n' "${selected[*]}"
