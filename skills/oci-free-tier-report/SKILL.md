---
name: oci-free-tier-report
description: Install, configure, test or remove the daily OCI (Oracle Cloud) Always Free usage report — a no-LLM Hermes cron job that checks cost, A1 OCPU/RAM hours, block/object storage, egress, idle-reclaim risk, load balancers and Autonomous DBs against Oracle's Always Free limits and posts a table to any chat platform. Use when the user mentions Oracle/OCI free tier usage, "don't get charged on Oracle", or asks to install https://github.com/pgen0x/hermes-oci-free-tier-report.
---

# OCI Always Free usage report

Repo: https://github.com/pgen0x/hermes-oci-free-tier-report
Runs as a `--no-agent` cron job: zero tokens per run, the script's stdout is delivered verbatim.

## Install (do these steps in order)

1. **Pick the delivery target.** Default to the chat you are talking in right now, written as
   `<platform>:<chat_id>` (e.g. `telegram:123456789`, `discord:987654321`, `whatsapp:<jid>`).
   A bare platform name (`telegram`) means that platform's home channel. Ask only if unclear.
2. **Pick the profile.** Use the profile you are running as (`default` unless you are a named profile).
3. **Pick the time.** Default 07:00 in the user's timezone; confirm in one line, don't interrogate.
4. Clone and run the installer from a terminal:
   ```bash
   git clone --depth 1 https://github.com/pgen0x/hermes-oci-free-tier-report ~/.local/src/hermes-oci-free-tier-report \
     || git -C ~/.local/src/hermes-oci-free-tier-report pull --ff-only
   ~/.local/src/hermes-oci-free-tier-report/install.sh \
     --profile <profile> --deliver <target> --schedule "0 7 * * *" --timezone <IANA tz>
   ```
5. Read the installer's exit code:
   - `0` → permissions OK. Run the job once (`hermes [-p <profile>] cron run <id>`) so the user sees a real report.
   - `3` → OCI IAM is not set up yet. Send the user the **exact** "One-time setup in the OCI Console" block the
     preflight printed (it contains their own instance OCID). Do not paraphrase the policy statements.
     When they say it's done, re-check with
     `~/.local/share/hermes-oci-free-tier-report/venv/bin/python <profile scripts dir>/oci_free_tier_report.py --check`,
     then run the job once.
   - `1` → read the error; most common: Hermes profile name wrong, or `python3-venv` missing (needs sudo — ask the user).

The cron job, schedule and target are now owned by Hermes cron. Do not wrap it in an agent prompt — the point is no LLM.

## Not on an OCI VM?
Instance-principal auth only works on an OCI instance. Elsewhere the user needs an API key in `~/.oci/config`;
add `OCI_AUTH=config` to the profile's `.env` (Hermes loads it for no-agent jobs).

## Options (profile `.env`)
- `REPORT_FORMAT=plain` — no code fences (SMS, IRC, email). Default `table` works on Telegram, Discord, WhatsApp, Slack, Signal, Matrix.
- `REPORT_ASCII=1` — OK/WARN/HIGH/OVER instead of emoji.
- `REPORT_QUIET=1` — send only when there is an alert or an error.
- `REPORT_TZ=Asia/Jakarta` — timezone for the report header.

## Maintain
- Update: `git -C ~/.local/src/hermes-oci-free-tier-report pull` then re-run `install.sh` with the same flags (it updates the existing job).
- Change time/target: `hermes [-p <profile>] cron edit <id> --schedule ... --deliver ...`
- Remove: `install.sh --uninstall --profile <profile>`

## Reading the report for the user
- Cost rows must be `0.00`. Anything else = a paid resource exists; the alert names the service.
- `A1 OCPU live 4/4` and `Block+boot GB 200/200` show 🟠 but are **still free** — they are at, not over, the limit.
  A 4 OCPU / 24 GB A1 running 24/7 uses at most 2,976 OCPU-h and 17,856 GB-h in a 31-day month (< 3,000 / 18,000).
- 🔴 = over the free limit → tell the user what to delete or shrink.
- Idle 🟠 = 7-day CPU p95 < 20% (and RAM < 20% on A1): Free Tier (non-upgraded) accounts can have the VM reclaimed.
