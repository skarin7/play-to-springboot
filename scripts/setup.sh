#!/usr/bin/env bash
# Play-to-Spring Boot plugin — workspace scaffolding for a target Play repo.
# Usage: scripts/setup.sh <path-to-play-repo> [--workspace <dir>] [--spring-name <name>]
#
# Example (from kit root): ./scripts/setup.sh /path/to/your-play-repo
#   -> Creates spring-<basename>/ (sibling) and workspace.yaml.
#
# Example: ./scripts/setup.sh /path/to/your-play-repo --workspace /tmp/migrate --spring-name my-spring-app
#   -> Uses /tmp/migrate as workspace; Spring project at /tmp/migrate/my-spring-app
#
# This script only scaffolds the workspace (Spring repo skeleton,
# workspace.yaml, .migration/ state dir, route map, endpoint probes, git
# init). It does not install anything into the Play repo's .claude/ — skills
# and agents come from the installed play-to-springboot plugin itself, never
# from a per-repo copy. A copy would also shadow the plugin's own agents,
# since Claude Code lets project-level .claude/agents/ override same-named
# plugin agents.

set -e

# Kit root is parent of this script (scripts/tools/, config/ live there).
KIT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLAY_REPO=""
WORKSPACE_DIR=""
SPRING_NAME=""
PRINT_PERMISSIONS=0

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace)
      WORKSPACE_DIR="$2"
      shift 2
      ;;
    --spring-name)
      SPRING_NAME="$2"
      shift 2
      ;;
    --print-permissions)
      PRINT_PERMISSIONS=1
      shift
      ;;
    -*)
      echo "Unknown option: $1"
      exit 1
      ;;
    *)
      if [[ -z "$PLAY_REPO" ]]; then
        PLAY_REPO="$1"
      fi
      shift
      ;;
  esac
done

if [[ -z "$PLAY_REPO" ]]; then
  echo "Usage: $0 <path-to-play-repo> [--workspace <dir>] [--spring-name <name>]"
  echo ""
  echo "  <path-to-play-repo>   Absolute or relative path to the Play project directory"
  echo "  --workspace <dir>     Where to create spring-* and migration state (default: parent of Play repo)"
  echo "  --spring-name <name>  Spring project directory name (default: spring-<play-repo-basename>)"
  echo "  --print-permissions   Print a paste-ready settings.json allow list for this workspace, then exit"
  exit 1
fi

# Resolve Play repo to absolute path
PLAY_REPO="$(cd "$PLAY_REPO" && pwd)"
PLAY_BASENAME="$(basename "$PLAY_REPO")"

# Default workspace = parent of Play repo (so spring-* is sibling of play repo)
if [[ -z "$WORKSPACE_DIR" ]]; then
  WORKSPACE_DIR="$(dirname "$PLAY_REPO")"
fi
WORKSPACE_DIR="$(mkdir -p "$WORKSPACE_DIR" && cd "$WORKSPACE_DIR" && pwd)"

# Default Spring project name = spring-<basename>
if [[ -z "$SPRING_NAME" ]]; then
  SPRING_NAME="spring-${PLAY_BASENAME}"
fi

SPRING_REPO="${WORKSPACE_DIR}/${SPRING_NAME}"

# --print-permissions: emit an allow list with *this* workspace's resolved
# absolute paths, and scaffold nothing. Hand-copying rules between workspaces is
# how a settings.local.json ends up full of paths from a session that ended
# months ago, matching nothing and silently prompting for everything.
if [[ $PRINT_PERMISSIONS -eq 1 ]]; then
  cat <<EOF
{
  "permissions": {
    "allow": [
      "Bash(python3 ${KIT_ROOT}/scripts/tools/*.py:*)",
      "Bash(python3 ${KIT_ROOT}/scripts/migration_orchestrator.py:*)",
      "Bash(mvn -B compile:*)",
      "Bash(mvn -B test:*)",
      "Bash(mvn -B package:*)",
      "Bash(mvn -B spring-boot:run:*)",
      "Bash(git -C ${SPRING_REPO}:*)",
      "Bash(git -C ${PLAY_REPO} status:*)",
      "Bash(git -C ${PLAY_REPO} log:*)",
      "Bash(git -C ${PLAY_REPO} diff:*)",
      "Read(${PLAY_REPO}/**)",
      "Edit(${SPRING_REPO}/**)",
      "Write(${SPRING_REPO}/**)"
    ],
    "deny": [
      "Edit(${PLAY_REPO}/**)",
      "Write(${PLAY_REPO}/**)"
    ]
  }
}
EOF
  echo "" >&2
  echo "Paste the above into .claude/settings.local.json (or merge the arrays)." >&2
  echo "The deny entries are the read-only Play invariant; keep them." >&2
  echo "java -jar rules are omitted: the jar path is version-pinned per run;" >&2
  echo "the plugin's PreToolUse hook allows it from \$CLAUDE_PLUGIN_DATA." >&2
  exit 0
fi

echo "Play repo:     $PLAY_REPO"
echo "Workspace:     $WORKSPACE_DIR"
echo "Spring project: $SPRING_REPO"

# Detect base Java package from Play app (first .java file)
detect_base_package() {
  local app_dir="$1"
  local f
  for f in $(find "$app_dir" -name "*.java" 2>/dev/null | head -5); do
    if [[ -f "$f" ]]; then
      local pkg=$(grep -m1 '^package ' "$f" 2>/dev/null | sed 's/package \([^;]*\);/\1/' | tr -d ' ')
      if [[ -n "$pkg" ]]; then
        # Use top-level package (first two segments) or full base for Application
        echo "$pkg"
        return
      fi
    fi
  done
  echo "com.example.application"
}

BASE_PACKAGE=$(detect_base_package "$PLAY_REPO/app" 2>/dev/null || echo "com.example.application")
echo "Base package:  $BASE_PACKAGE"

# Create Spring Boot project directory structure only — no templates.
# The builder agent (LLM) will generate pom.xml, Application.java, and application.properties
# by reading the Play project's build.sbt and application.conf for accurate dependencies and config.
echo "Creating Spring Boot project at $SPRING_REPO"
mkdir -p "$SPRING_REPO/src/main/java"
mkdir -p "$SPRING_REPO/src/main/resources"
mkdir -p "$SPRING_REPO/src/test/java"
mkdir -p "$SPRING_REPO/src/test/resources"

# Base package path
PACKAGE_PATH=$(echo "$BASE_PACKAGE" | tr '.' '/')
mkdir -p "$SPRING_REPO/src/main/java/$PACKAGE_PATH"
mkdir -p "$SPRING_REPO/src/test/java/$PACKAGE_PATH"

# workspace.yaml
WORKSPACE_YAML="${WORKSPACE_DIR}/workspace.yaml"
cat > "$WORKSPACE_YAML" << EOF
# Generated by the play-to-springboot plugin's scripts/setup.sh
play_repo: $PLAY_REPO
spring_repo: $SPRING_REPO
migration_root: $WORKSPACE_DIR
batch_size: 25
base_package: $BASE_PACKAGE

# Timeouts (seconds unless stated). These are the tool defaults, written out so
# they can be raised on a slow machine instead of being discovered as a
# mysterious kill. dev_bash_timeout_ms is what dev passes to the Bash tool:
# its 120000 default is shorter than a cold-cache Maven build.
mvn_timeout: 900
java_timeout: 300
dev_bash_timeout_ms: 900000
boot_timeout: 180
EOF
echo "Wrote $WORKSPACE_YAML"

# Progress is tracked by the migrate skill in <spring-repo>/migration-status.json.

# Agent working directory: research.md, decisions.md, QA findings, and the
# append-only dev journals that make a killed subagent resumable.
mkdir -p "${SPRING_REPO}/.migration/journal"
if [[ ! -f "${SPRING_REPO}/.gitignore" ]]; then
  echo ".migration/" > "${SPRING_REPO}/.gitignore"
fi

# Route map, populated from conf/routes rather than left as an empty stub.
# This is the baseline QA's T3 parity check compares Spring mappings against.
ROUTE_MAP="${WORKSPACE_DIR}/route-map.json"
if [[ -f "${PLAY_REPO}/conf/routes" ]]; then
  if python3 "$KIT_ROOT/scripts/tools/routes.py" \
        --routes-file "${PLAY_REPO}/conf/routes" \
        --spring-src "${SPRING_REPO}/src/main/java" > "$ROUTE_MAP" 2>/dev/null; then
    route_count=$(python3 -c "import json;print(len(json.load(open('$ROUTE_MAP'))['play_routes']))" 2>/dev/null || echo "?")
    echo "Wrote $ROUTE_MAP ($route_count Play routes)"
  else
    echo '{"play_routes":[],"spring_endpoints":[]}' > "$ROUTE_MAP"
    echo "Could not parse conf/routes; wrote empty $ROUTE_MAP" >&2
  fi
elif [[ ! -f "$ROUTE_MAP" ]]; then
  echo '{"play_routes":[],"spring_endpoints":[]}' > "$ROUTE_MAP"
  echo "No conf/routes found; created empty $ROUTE_MAP"
fi

# T5 probe list. Never overwritten: QA fills in path_params and enables mutating
# verbs by hand, and a re-run of setup must not throw that work away.
PROBES="${SPRING_REPO}/.migration/endpoint-probes.json"
if [[ -f "${PLAY_REPO}/conf/routes" ]] && [[ ! -f "$PROBES" ]]; then
  if python3 "$KIT_ROOT/scripts/tools/endpoint_diff.py" probes \
        --routes "${PLAY_REPO}/conf/routes" --out "$PROBES" 2>/dev/null; then
    probe_count=$(python3 -c "import json;print(sum(1 for p in json.load(open('$PROBES'))['probes'] if p['enabled']))" 2>/dev/null || echo "?")
    echo "Wrote $PROBES ($probe_count enabled by default; the rest need a sample value or a body)"
  fi
fi

# The Spring project needs to be a git repo so the manager can commit after each
# layer passes QA -- that is what gives a rejected review gate something to reset
# to, instead of a manual unwind.
MIGRATION_BRANCH="migration/${PLAY_BASENAME}"
if [[ ! -d "${SPRING_REPO}/.git" ]]; then
  git -C "$SPRING_REPO" init -q
  git -C "$SPRING_REPO" checkout -q -b "$MIGRATION_BRANCH" 2>/dev/null || true
  echo "Initialized git in $SPRING_REPO on branch $MIGRATION_BRANCH"
else
  current=$(git -C "$SPRING_REPO" branch --show-current 2>/dev/null || echo "")
  echo "Spring repo already under git (branch: ${current:-detached})"
fi

# Play-repo read-only guard. Note what is NOT happening here: the Play repo is
# never git-init'ed. It is declared read-only, so writing .git/ into it would
# contradict the invariant being enforced -- and if it sits inside a larger
# checkout, init would create a nested repo. guard.py records a checksum
# manifest instead whenever the Play repo is not a git root in its own right.
GUARD_MODE="unknown"
if python3 "$KIT_ROOT/scripts/tools/guard.py" baseline \
      --play-repo "$PLAY_REPO" --spring-repo "$SPRING_REPO" > /tmp/p2sb-guard.$$ 2>&1; then
  GUARD_MODE=$(python3 -c "import json,sys;print(json.load(open('/tmp/p2sb-guard.$$'))['mode'])" 2>/dev/null || echo "unknown")
  echo "Play repo guard: mode=$GUARD_MODE (baseline in $SPRING_REPO/.migration/guard/)"
else
  echo "WARNING: could not record the Play-repo guard baseline:" >&2
  cat /tmp/p2sb-guard.$$ >&2
  echo "The migration will halt at the first gate until this is fixed." >&2
fi
rm -f /tmp/p2sb-guard.$$

echo ""
echo "=== Setup complete ==="
echo ""
echo "  Workspace:      $WORKSPACE_DIR"
echo "  Play repo:      $PLAY_REPO  (READ-ONLY during migration)"
echo "  Play guard:     mode=$GUARD_MODE"
echo "  Spring project: $SPRING_REPO"
echo "  Agent state:    $SPRING_REPO/migration-status.json (created by /play-to-springboot:migrate)"
echo "  Agent scratch:  $SPRING_REPO/.migration/ (research, decisions, findings, journals)"
echo ""
echo "Next: run /play-to-springboot:migrate $PLAY_REPO"
echo ""
echo "Deterministic helpers (run these yourself any time):"
echo "  python3 $KIT_ROOT/scripts/tools/inventory.py --play-repo $PLAY_REPO"
echo "  python3 $KIT_ROOT/scripts/tools/verify.py --play-repo $PLAY_REPO --spring-repo $SPRING_REPO"
echo "  python3 $KIT_ROOT/scripts/tools/gate.py --play-repo $PLAY_REPO --spring-repo $SPRING_REPO --layer service --jar <jar>"
echo ""
