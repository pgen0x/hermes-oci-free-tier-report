---
name: oci-free-tier-report
description: "Daily OCI Always Free usage report as a no-agent cron."
version: 1.2.0
author: pgen0x
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [oracle-cloud, oci, always-free, free-tier, cost, cron, monitoring, telegram]
    related_skills: []
---

# Oracle Cloud Free Tier monitor (OCI Always Free usage report)

Repo: https://github.com/pgen0x/hermes-oci-free-tier-report
A Hermes `--no-agent` cron job: pure OCI API calls, zero tokens per run, stdout delivered verbatim to a chat.
It checks cost, A1 OCPU/RAM hours, block/object storage, egress, idle-reclaim risk, load balancers and
Autonomous DBs against Oracle's Always Free limits.

## When to Use
- The user asks to install, set up, update, move, test or remove this repo / "OCI free tier report".
- The user worries about being charged on Oracle Cloud, or asks whether their free tier usage is OK.
- The user asks what a line of the daily report means (see "Reading the report").

**Requirement:** Hermes must run **on an OCI compute instance** (Ampere A1 or E2.1.Micro). The script authenticates
as that VM (instance principal) — it does not work on other clouds, a laptop or a non-OCI VPS. If the machine is
not an OCI instance (the preflight prints `OCI instance : not detected`), stop and tell the user.

## Install (do these steps in order)

1. **Pick the delivery target.** Default to the chat you are talking in right now, written as
   `<platform>:<chat_id>` (e.g. `telegram:123456789`, `discord:987654321`, `whatsapp:<jid>`).
   A bare platform name (`telegram`) means that platform's home channel. Ask only if unclear.
2. **Pick the profile.** Use the profile you are running as. Inside a Hermes session the installer detects it
   from `HERMES_HOME` automatically; passing `--profile <name>` explicitly is still fine.
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
     If the user can't do it right now, that's fine: the cron job is already installed and each daily message
     will be this setup guide until the permissions exist, then it turns into the normal report by itself.
     You cannot create the IAM policy yourself — the VM has no admin rights; only the user can, in the Console.
   - `1` → read the error; most common: Hermes profile name wrong, or `python3-venv` missing (needs sudo — ask the user).

The cron job, schedule and target are now owned by Hermes cron. Do not wrap it in an agent prompt — the point is no LLM.

## Output format
`install.sh` picks it from `--deliver`: `markdown` (GFM tables → native tables on Telegram, aligned on
Slack/Signal/Matrix), `mono` (code-block table for Discord/WhatsApp), `plain` (SMS/email/IRC).
For Telegram the installer also sets `platforms.telegram.extra.rich_messages: true` (Hermes default is off);
if it prints "Restart the Hermes gateway", run `hermes [-p <profile>] gateway restart` (restarts only that
profile; without `-y` it refuses to interrupt live work). Override the format with `--format`.
Other settings go in `oci_free_tier_report.conf` next to the installed script:
`REPORT_ASCII=1` (no emoji), `REPORT_QUIET=1` (alerts only), `REPORT_TZ=<IANA tz>`.

## Cron header
Hermes wraps cron output in `Cronjob Response: <name> (job_id: …)` + a footer; on Telegram tables it shows as a
big heading. Check first with `hermes [-p <profile>] config get cron.wrap_response` — if it is already `false`
there is no header. Otherwise, if the user dislikes it, re-run the installer with `--unwrap` (profile-wide: other
cron jobs in that profile lose the header too — say so).

## Maintain
- Update: `git -C ~/.local/src/hermes-oci-free-tier-report pull` then re-run `install.sh` with the same flags (it updates the existing job).
- Change time/target: `hermes [-p <profile>] cron edit <id> --schedule ... --deliver ...`
- Remove: `install.sh --uninstall --profile <profile>`

## Reading the report
- Cost rows must be `0.00`. Anything else = a paid resource exists; the alert names the service.
- `A1 OCPU 4/4`, `A1 RAM (GB) 24/24` and `Block+boot (GB) 200/200` show 🟠 but are **still free** — at, not over,
  the limit. A 4 OCPU / 24 GB A1 running 24/7 uses at most 2,976 OCPU-h and 17,856 GB-h in a 31-day month
  (< 3,000 / 18,000).
- 🔴 = over the free limit → tell the user what to delete or shrink.
- Idle 🟠 = 7-day CPU p95 < 20% (and RAM < 20% on A1): Free Tier (non-upgraded) accounts can have the VM reclaimed.
- The **Notes** footer only lists sections that need attention; no notes means everything is green.
