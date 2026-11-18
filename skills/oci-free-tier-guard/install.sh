#!/usr/bin/env bash
# Installer for oci-free-tier-guard (optional companion of oci-free-tier-report).
# Copies the guard script next to the report script in a Hermes profile, writes its settings, runs a preflight,
# and creates an hourly no-agent cron job. Needs oci-free-tier-report installed first (it reuses its venv and code).
#
#   bash install.sh --deliver telegram                        # dry-run mode (default): only warns
#   bash install.sh --profile work --deliver discord:123 --mode downscale
#   bash install.sh --uninstall [--profile NAME]
# Re-run after `hermes skills update oci-free-tier-guard` to deploy the new script; settings are kept.
set -euo pipefail

PROFILE=""
DELIVER=""
SCHEDULE="0 * * * *"
TIMEZONE=""
JOB_NAME="OCI Free Tier Guard"
MODE=""
INSTANCES=""
ALLOW_SELF=""
MIN_SIZE=""
CREATE_CRON=1
UNINSTALL=0
SKIP_CHECK=0

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --profile NAME     Hermes profile (default: the profile of the current Hermes session, else "default")
  --deliver TARGET   Hermes delivery target, same as the report (telegram:<chat_id>, discord:<id>, ...)
  --mode MODE        off | dry-run (default on first install) | downscale | stop
  --instances LIST   which A1 instances the guard may touch: self (default) | all | OCIDs or names, comma-separated
  --allow-self       stop mode may stop the VM running Hermes (it stays off until started in the Console)
  --no-allow-self    undo --allow-self
  --min OCPU/GB      smallest size downscale may set (default 1/6)
  --schedule CRON    cron expression (default: "0 * * * *", hourly)
  --timezone TZ      IANA timezone for the schedule (default: system timezone)
  --name NAME        cron job name (default: "OCI Free Tier Guard")
  --no-cron          install only, do not create the cron job
  --skip-check       skip the preflight
  --uninstall        remove the guard script, its settings and cron job (the report is not touched)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --deliver) DELIVER="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --instances) INSTANCES="$2"; shift 2 ;;
    --allow-self) ALLOW_SELF=1; shift ;;
    --no-allow-self) ALLOW_SELF=0; shift ;;
    --min) MIN_SIZE="$2"; shift 2 ;;
    --schedule) SCHEDULE="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    --name) JOB_NAME="$2"; shift 2 ;;
    --no-cron) CREATE_CRON=0; shift ;;
    --skip-check) SKIP_CHECK=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

case "$MODE" in ""|off|dry-run|downscale|stop) ;; *) die "--mode must be off, dry-run, downscale or stop" ;; esac
[[ -z "$MIN_SIZE" || "$MIN_SIZE" =~ ^[0-9]+(\.[0-9]+)?/[0-9]+(\.[0-9]+)?$ ]] || die "--min must look like 1/6 (OCPU/GB)"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="oci_free_tier_guard.py"
CONF_NAME="oci_free_tier_guard.conf"
STATE_NAME="oci_free_tier_guard.state.json"
REPORT_NAME="oci_free_tier_report.py"
# Hermes root. Inside a profile session HERMES_HOME points at <root>/profiles/<name>, not at the root.
HERMES_ROOT="${HERMES_HOME:-$HOME/.hermes}"
HERMES_ROOT="${HERMES_ROOT%/}"
SESSION_PROFILE=""
if [[ "$HERMES_ROOT" == */profiles/* ]]; then
  SESSION_PROFILE="${HERMES_ROOT##*/profiles/}"
  SESSION_PROFILE="${SESSION_PROFILE%%/*}"
  HERMES_ROOT="${HERMES_ROOT%/profiles/*}"
fi
PROFILE="${PROFILE:-${SESSION_PROFILE:-default}}"
PY="${XDG_DATA_HOME:-$HOME/.local/share}/hermes-oci-free-tier-report/venv/bin/python"  # the report's venv

command -v hermes >/dev/null 2>&1 || die "'hermes' not found on PATH. Install Hermes first."

if [[ "$PROFILE" == "default" ]]; then
  HCMD=(env HERMES_HOME="$HERMES_ROOT" hermes)
  SHOW="hermes"
  PROFILE_DIR="$HERMES_ROOT"
else
  HCMD=(env HERMES_HOME="$HERMES_ROOT" hermes -p "$PROFILE")
  SHOW="hermes -p $PROFILE"
  PROFILE_DIR="$HERMES_ROOT/profiles/$PROFILE"
fi
[[ -d "$PROFILE_DIR" ]] || die "Hermes profile '$PROFILE' not found at $PROFILE_DIR"
SCRIPTS_DIR="$PROFILE_DIR/scripts"

find_job_ids() {  # prints ids of jobs whose name matches $JOB_NAME
  "${HCMD[@]}" cron list --all 2>/dev/null | awk -v name="$JOB_NAME" '
    /^ +[0-9a-f]{12}( |$)/ { id = $1 }
    $1 == "Name:" { sub(/^ *Name: */, ""); if ($0 == name) print id }'
}

if (( UNINSTALL )); then
  for id in $(find_job_ids); do
    say "Removing cron job $id"
    "${HCMD[@]}" cron remove "$id" >/dev/null
  done
  rm -f "$SCRIPTS_DIR/$SCRIPT_NAME" "$SCRIPTS_DIR/$CONF_NAME" "$SCRIPTS_DIR/$STATE_NAME"
  say "Removed the guard from $SCRIPTS_DIR (the report and its venv are untouched)"
  say "Remove the guard's policy statements in the OCI Console too if you added them."
  exit 0
fi

# 1. the report must be there -----------------------------------------------------------
[[ -f "$SCRIPTS_DIR/$REPORT_NAME" && -x "$PY" ]] || die "oci-free-tier-report is not installed in profile '$PROFILE'. \
Install it first: $SHOW skills install pgen0x/hermes-oci-free-tier-report/skills/oci-free-tier-report, \
then run its install.sh, then re-run this one."

# 2. script + settings ------------------------------------------------------------------
install -m 0755 "$SRC_DIR/$SCRIPT_NAME" "$SCRIPTS_DIR/$SCRIPT_NAME"
say "Installed $SCRIPTS_DIR/$SCRIPT_NAME"
CONF="$SCRIPTS_DIR/$CONF_NAME"
touch "$CONF"
set_conf() {  # set_conf KEY VALUE — update or append
  if grep -q "^$1=" "$CONF"; then
    sed -i "s|^$1=.*|$1=$2|" "$CONF"
  else
    echo "$1=$2" >> "$CONF"
  fi
}
if [[ -n "$MODE" ]]; then
  set_conf GUARD_MODE "$MODE"
elif ! grep -q '^GUARD_MODE=' "$CONF"; then
  set_conf GUARD_MODE dry-run
fi
if [[ -n "$INSTANCES" ]]; then
  set_conf GUARD_INSTANCES "$INSTANCES"
elif ! grep -q '^GUARD_INSTANCES=' "$CONF"; then
  set_conf GUARD_INSTANCES self
fi
if [[ -n "$ALLOW_SELF" ]]; then set_conf GUARD_ALLOW_SELF "$ALLOW_SELF"; fi
if [[ -n "$MIN_SIZE" ]]; then set_conf GUARD_MIN "$MIN_SIZE"; fi
say "Settings: $CONF ($(grep -E '^GUARD_' "$CONF" | tr '\n' ' '))"

# 3. preflight --------------------------------------------------------------------------
CHECK_RC=0
if (( ! SKIP_CHECK )); then
  say "Running guard preflight"
  set +e
  "$PY" "$SCRIPTS_DIR/$SCRIPT_NAME" --check
  CHECK_RC=$?
  set -e
fi

# 4. cron job ---------------------------------------------------------------------------
if [[ -z "$TIMEZONE" ]]; then
  TIMEZONE="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)"
fi
if (( CREATE_CRON )); then
  [[ -n "$DELIVER" ]] || die "--deliver is required to create the cron job (or pass --no-cron)."
  existing="$(find_job_ids | head -1)"
  if [[ -n "$existing" ]]; then
    say "Cron job '$JOB_NAME' already exists ($existing) — updating it"
    "${HCMD[@]}" cron edit "$existing" --schedule "$SCHEDULE" --deliver "$DELIVER" \
      --script "$SCRIPT_NAME" --no-agent --interpreter "$PY" --timezone "$TIMEZONE" >/dev/null \
      || say "Could not update automatically; edit it with: $SHOW cron edit $existing"
  else
    say "Creating cron job '$JOB_NAME' ($SCHEDULE $TIMEZONE → $DELIVER)"
    "${HCMD[@]}" cron create "$SCHEDULE" \
      --name "$JOB_NAME" \
      --script "$SCRIPT_NAME" \
      --no-agent \
      --interpreter "$PY" \
      --timezone "$TIMEZONE" \
      --deliver "$DELIVER"
  fi
fi

echo
if (( CHECK_RC == 0 )); then
  say "Done. The guard is silent while A1 stays inside the free quota. Preview what it would do now with:"
  say "  $PY $SCRIPTS_DIR/$SCRIPT_NAME --dry-run"
else
  say "Installed, but the preflight failed (see above). Fix it, then verify with: $PY $SCRIPTS_DIR/$SCRIPT_NAME --check"
  exit "$CHECK_RC"
fi
