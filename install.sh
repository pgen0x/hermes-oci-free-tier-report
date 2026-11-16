#!/usr/bin/env bash
# Installer for hermes-oci-free-tier-report.
# Copies the report script into a Hermes profile, builds a private venv with the OCI SDK,
# runs a permission preflight, and (optionally) creates a no-agent daily cron job.
#
#   ./install.sh --deliver telegram                      # default profile, home channel, 07:00 local
#   ./install.sh --profile work --deliver discord:123456789 --schedule "30 8 * * *"
#   ./install.sh --no-cron                               # just install + preflight
#   ./install.sh --uninstall [--profile NAME]
set -euo pipefail

PROFILE="default"
DELIVER=""
SCHEDULE="0 7 * * *"
TIMEZONE=""
JOB_NAME="OCI Always Free Usage Report"
CREATE_CRON=1
UNINSTALL=0
SKIP_CHECK=0
FORMAT=""
TELEGRAM_RICH=1
UNWRAP=0
NEED_RESTART=0

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --profile NAME     Hermes profile (default: "default")
  --deliver TARGET   Hermes delivery target, e.g. telegram | telegram:<chat_id> | discord:<channel_id>
                     | whatsapp:<jid> | slack:<channel> | signal:<number> ... (required for cron)
  --schedule CRON    cron expression (default: "0 7 * * *")
  --timezone TZ      IANA timezone for the schedule (default: system timezone)
  --name NAME        cron job name (default: "OCI Always Free Usage Report")
  --format FMT       markdown | mono | plain | json (default: picked from --deliver platform —
                     markdown for Telegram/Slack/Signal/Matrix, mono for Discord/WhatsApp, plain for SMS/email/IRC)
  --no-telegram-rich do not enable Telegram Rich Messages (native tables) in the profile config
  --unwrap           set cron.wrap_response=false for the profile: drops Hermes' "Cronjob Response: …"
                     header/footer (it renders as a big heading with Telegram tables). Profile-wide.
  --no-cron          install only, do not create the cron job
  --skip-check       skip the OCI permission preflight
  --uninstall        remove the script, venv and cron job
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --deliver) DELIVER="$2"; shift 2 ;;
    --schedule) SCHEDULE="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    --name) JOB_NAME="$2"; shift 2 ;;
    --format) FORMAT="$2"; shift 2 ;;
    --no-telegram-rich) TELEGRAM_RICH=0; shift ;;
    --unwrap) UNWRAP=1; shift ;;
    --no-cron) CREATE_CRON=0; shift ;;
    --skip-check) SKIP_CHECK=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="oci_free_tier_report.py"
CONF_NAME="oci_free_tier_report.conf"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/hermes-oci-free-tier-report"
VENV="$DATA_DIR/venv"
PY="$VENV/bin/python"

command -v hermes >/dev/null 2>&1 || die "'hermes' not found on PATH. Install Hermes first."

if [[ "$PROFILE" == "default" ]]; then
  HCMD=(hermes)
  PROFILE_DIR="$HERMES_HOME"
else
  HCMD=(hermes -p "$PROFILE")
  PROFILE_DIR="$HERMES_HOME/profiles/$PROFILE"
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
  rm -f "$SCRIPTS_DIR/$SCRIPT_NAME" "$SCRIPTS_DIR/$CONF_NAME"
  say "Removed $SCRIPTS_DIR/$SCRIPT_NAME"
  others="$(find "$HERMES_HOME/scripts" "$HERMES_HOME"/profiles/*/scripts -maxdepth 1 \
    -name "$SCRIPT_NAME" 2>/dev/null || true)"
  if [[ -n "$others" ]]; then
    say "Keeping shared venv $DATA_DIR — still used by: $(echo $others)"
  elif [[ -d "$DATA_DIR" ]]; then
    rm -rf "$DATA_DIR"
    say "Removed $DATA_DIR"
  fi
  exit 0
fi

# 1. venv with the OCI SDK -------------------------------------------------------------
say "Preparing Python venv at $VENV"
mkdir -p "$DATA_DIR"
if [[ ! -x "$PY" ]]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv -q "$VENV"
  else
    python3 -m venv "$VENV" 2>/dev/null \
      || die "python3 -m venv failed. Install it (Ubuntu/Debian: sudo apt install python3-venv) or install uv."
  fi
fi
if command -v uv >/dev/null 2>&1; then
  uv pip install -q --python "$PY" -r "$SRC_DIR/requirements.txt"
else
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r "$SRC_DIR/requirements.txt"
fi

# 2. script into the profile -----------------------------------------------------------
mkdir -p "$SCRIPTS_DIR"
install -m 0755 "$SRC_DIR/$SCRIPT_NAME" "$SCRIPTS_DIR/$SCRIPT_NAME"
say "Installed $SCRIPTS_DIR/$SCRIPT_NAME"

if [[ -z "$TIMEZONE" ]]; then
  TIMEZONE="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)"
fi
if [[ -z "$FORMAT" ]]; then
  case "${DELIVER%%:*}" in
    discord|whatsapp|whatsapp_cloud|weixin|wecom|line|qqbot|dingtalk|feishu) FORMAT="mono" ;;
    sms|email|irc|ntfy|simplex) FORMAT="plain" ;;
    *) FORMAT="markdown" ;;  # telegram (native tables), slack, signal, matrix, mattermost, teams, ...
  esac
fi
set_conf() {  # set_conf KEY VALUE — update or append in the profile's conf file
  local f="$SCRIPTS_DIR/$CONF_NAME"
  touch "$f"
  if grep -q "^$1=" "$f"; then
    sed -i "s|^$1=.*|$1=$2|" "$f"
  else
    echo "$1=$2" >> "$f"
  fi
}
set_conf REPORT_FORMAT "$FORMAT"
set_conf REPORT_TZ "$TIMEZONE"
say "Settings: $SCRIPTS_DIR/$CONF_NAME (REPORT_FORMAT=$FORMAT, REPORT_TZ=$TIMEZONE)"

# Telegram only renders GFM tables natively via Bot API Rich Messages, which Hermes keeps opt-in
# (platforms.telegram.extra.rich_messages). Without it, tables degrade to plain text.
if [[ "${DELIVER%%:*}" == "telegram" && "$FORMAT" == "markdown" ]]; then
  rich="$("${HCMD[@]}" config get platforms.telegram.extra.rich_messages 2>/dev/null | tail -1 || true)"
  if [[ "$rich" == "true" || "$rich" == "True" ]]; then
    say "Telegram Rich Messages already enabled (native tables)"
  elif (( TELEGRAM_RICH )); then
    "${HCMD[@]}" config set platforms.telegram.extra.rich_messages true >/dev/null
    say "Enabled Telegram Rich Messages for profile '$PROFILE' (platforms.telegram.extra.rich_messages: true)"
    NEED_RESTART=1
  else
    say "WARNING: Telegram Rich Messages are off — tables will arrive as plain text. Use --format mono instead."
  fi
fi

# 3. preflight -------------------------------------------------------------------------
CHECK_RC=0
if (( ! SKIP_CHECK )); then
  say "Running OCI preflight"
  set +e
  "$PY" "$SCRIPTS_DIR/$SCRIPT_NAME" --check
  CHECK_RC=$?
  set -e
fi

# 4. cron job --------------------------------------------------------------------------
if (( CREATE_CRON )); then
  [[ -n "$DELIVER" ]] || die "--deliver is required to create the cron job (or pass --no-cron)."
  existing="$(find_job_ids | head -1)"
  if [[ -n "$existing" ]]; then
    say "Cron job '$JOB_NAME' already exists ($existing) — updating it"
    "${HCMD[@]}" cron edit "$existing" --schedule "$SCHEDULE" --deliver "$DELIVER" \
      --script "$SCRIPT_NAME" --no-agent --interpreter "$PY" --timezone "$TIMEZONE" >/dev/null \
      || say "Could not update automatically; edit it with: ${HCMD[*]} cron edit $existing"
  else
    say "Creating cron job '$JOB_NAME' ($SCHEDULE $TIMEZONE → $DELIVER)"
    "${HCMD[@]}" cron create "$SCHEDULE" \
      --name "$JOB_NAME" \
      --script "$SCRIPT_NAME" \
      --no-agent \
      --interpreter "$PY" \
      --timezone "$TIMEZONE" \
      --deliver "$DELIVER" \
      --catch-up on
  fi
  status_out="$("${HCMD[@]}" cron status 2>/dev/null || true)"
  [[ "$status_out" == *"Gateway is running"* ]] \
    || say "WARNING: the Hermes gateway does not seem to be running — cron jobs only fire while it runs."
fi

if (( UNWRAP )); then
  "${HCMD[@]}" config set cron.wrap_response false >/dev/null
  say "Set cron.wrap_response=false for profile '$PROFILE' (all its cron jobs now deliver without the header)"
fi

echo
if (( NEED_RESTART )); then
  say "Restart the Hermes gateway once so Telegram picks up native tables: ${HCMD[*]} gateway restart"
fi
if (( CHECK_RC == 0 )); then
  say "Done. Test a real delivery now with: ${HCMD[*]} cron run <job id from '${HCMD[*]} cron list'>"
else
  say "Installed, but OCI permissions are not ready yet (see the preflight output above)."
  say "After the IAM setup, verify with: $PY $SCRIPTS_DIR/$SCRIPT_NAME --check"
  exit 3
fi
