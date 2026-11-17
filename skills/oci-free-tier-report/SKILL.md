---
name: oci-free-tier-report
description: "Daily OCI Always Free usage report as a no-agent cron."
version: 0.1.2
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

This skill folder **is** the installer: `install.sh`, the report script, `requirements.txt` and `locales/` ship
next to this file. Nothing to clone.

0. **Make sure the skill is installed from the hub** (`hermes [-p <profile>] skills list` shows
   `oci-free-tier-report` with source `github`/`skills.sh`). If not — e.g. you are reading this on GitHub — run:
   ```bash
   hermes [-p <profile>] skills install pgen0x/hermes-oci-free-tier-report/skills/oci-free-tier-report --yes
   ```
   **Never copy this folder or SKILL.md into `skills/` by hand**: a hand-made copy is a `local` skill that
   `hermes skills update` can't track. If one exists, delete that folder first, then install from the hub.
   The skill folder (`SKILL_DIR` below) is `~/.hermes/profiles/<profile>/skills/oci-free-tier-report`, or
   `~/.hermes/skills/oci-free-tier-report` for the default profile.
1. **Pick the delivery target.** Default to the chat you are talking in right now, written as
   `<platform>:<chat_id>` (e.g. `telegram:123456789`, `discord:987654321`, `whatsapp:<jid>`).
   A bare platform name (`telegram`) means that platform's home channel. Ask only if unclear.
2. **Pick the profile.** Use the profile you are running as. Inside a Hermes session the installer detects it
   from `HERMES_HOME` automatically; passing `--profile <name>` explicitly is still fine.
3. **Pick the time.** Default 07:00 in the user's timezone; confirm in one line, don't interrogate.
4. **Pick the language.** Default to the language the user is chatting in. Bundled: `en`, `id`
   (`ls "$SKILL_DIR/locales"`). For any other language, install with `--lang en` first, then follow
   "Adding a language" below.
5. Run the installer from a terminal. Hub installs drop the executable bit, so always call it with `bash`:
   ```bash
   bash "$SKILL_DIR/install.sh" \
     --profile <profile> --deliver <target> --schedule "0 7 * * *" --timezone <IANA tz> --lang <code>
   ```
6. Read the installer's exit code:
   - `0` → permissions OK. Run the job once (`hermes [-p <profile>] cron run <id>`) so the user sees a real report.
   - `3` → OCI IAM is not set up yet. Send the user the **exact** "One-time setup in the OCI Console" block the
     preflight printed (it contains their own instance OCID). Do not paraphrase the policy statements.
     When they say it's done, re-check with
     `~/.local/share/hermes-oci-free-tier-report/venv/bin/python <profile scripts dir>/oci_free_tier_report.py --check`,
     then run the job once.
     If the user can't do it right now, that's fine: the cron job is already installed and each daily message
     will be this setup guide until the permissions exist, then it turns into the normal report by itself.
     You cannot create the IAM policy yourself — the VM has no admin rights; only the user can, in the Console.
   - `1` → read the error; most common: Hermes profile name wrong, or `python3-venv` missing (needs root — ask the user).

The cron job, schedule and target are now owned by Hermes cron. Do not wrap it in an agent prompt — the point is no LLM.

## Adding a language
The daily run never uses an LLM; you translate **once**, after a first install. Custom locales live in the
profile's scripts folder (`~/.hermes/profiles/<profile>/scripts`, or `~/.hermes/scripts` for the default profile),
so skill updates and reinstalls never overwrite them.
```bash
SCRIPTS=~/.hermes/profiles/<profile>/scripts
PY=~/.local/share/hermes-oci-free-tier-report/venv/bin/python
$PY $SCRIPTS/oci_free_tier_report.py --dump-locale > $SCRIPTS/oci_free_tier_report_locales/<code>.json
```
Then edit that `<code>.json`:
- translate the **values** only; keep every key and every `{placeholder}` exactly;
- set `_language`, `num.thousands` / `num.decimal`, 7 `date.weekdays` and 12 `date.months` (comma-separated),
  and `date.format` for that language;
- do **not** translate OCI identifiers: shape names, `OCPU`, `GB-h`, `A1`, Console menu paths in `setup.step1` /
  `setup.step2`, and the `Name:` / `Description:` / `Matching rule:` labels and “Show manual editor” the user must match in the Console;
- keep row labels short (≤ 15 characters) so the `mono` table fits a phone.

Validate, then switch the job to it (same flags as the first install, the job is updated in place):
```bash
$PY $SCRIPTS/oci_free_tier_report.py --check-locale <code>     # must print "is complete"
bash "$SKILL_DIR/install.sh" --profile <profile> --deliver <target> --lang <code>
```
Offer the user to contribute the file upstream (a PR adding `skills/oci-free-tier-report/locales/<code>.json`).

## Output format
`install.sh` picks it from `--deliver`: `markdown` (GFM tables → native tables on Telegram, aligned on
Slack/Signal/Matrix), `mono` (code-block table for Discord/WhatsApp), `plain` (SMS/email/IRC).
For Telegram the installer also sets `platforms.telegram.extra.rich_messages: true` (Hermes default is off);
if it prints "Restart the Hermes gateway", run `hermes [-p <profile>] gateway restart` (restarts only that
profile; without `-y` it refuses to interrupt live work). Override the format with `--format`.
Other settings go in `oci_free_tier_report.conf` next to the installed script:
`REPORT_LANG=<code>`, `REPORT_ASCII=1` (no emoji), `REPORT_QUIET=1` (alerts only), `REPORT_TZ=<IANA tz>`.
To switch language later, re-run `install.sh` with `--lang <code>` (it validates the locale first).

## Cron header
Hermes wraps cron output in `Cronjob Response: <name> (job_id: …)` + a footer; on Telegram tables it shows as a
big heading. Check first with `hermes [-p <profile>] config get cron.wrap_response` — if it is already `false`
there is no header. Otherwise, if the user dislikes it, re-run the installer with `--unwrap` (profile-wide: other
cron jobs in that profile lose the header too — say so).

## Maintain
- Update: `hermes [-p <profile>] skills check` → `hermes [-p <profile>] skills update oci-free-tier-report`, then
  re-run `bash "$SKILL_DIR/install.sh" --profile <profile> --deliver <target>` so the new script reaches the
  profile's `scripts/` (the cron job and settings are kept; `--lang` is remembered).
- Migrating from an early clone install (from `~/.local/src/hermes-oci-free-tier-report`): install the skill from
  the hub (step 0), re-run its `install.sh` with the same `--deliver`, then the old clone can be deleted.
- Change time/target: `hermes [-p <profile>] cron edit <id> --schedule ... --deliver ...`
- Remove: `bash "$SKILL_DIR/install.sh" --uninstall --profile <profile>`, then
  `hermes [-p <profile>] skills uninstall oci-free-tier-report`.

## Reading the report
- Cost rows must be `0.00`. Anything else = a paid resource exists; the alert names the service.
- The header shows the account type: `Free Tier`, `PAYG`, or `account type ?` (subscription unreadable → the
  stricter Free Tier quota is used). A1 limits depend on it: **Free Tier 2 OCPU / 12 GB, 1,500 OCPU-h / 9,000 GB-h**;
  **PAYG 4 OCPU / 24 GB, 3,000 OCPU-h / 18,000 GB-h** per month.
- On PAYG, `A1 OCPU 4/4`, `A1 RAM (GB) 24/24` and `Block+boot (GB) 200/200` show 🟠 but are **still free** — at, not
  over, the limit. A 4 OCPU / 24 GB A1 running 24/7 uses at most 2,976 OCPU-h and 17,856 GB-h in a 31-day month.
  On Free Tier the same A1 is 🔴: tell the user to shrink it to 2 OCPU / 12 GB (or upgrade to PAYG).
- ⚪ on the A1 hours rows (PAYG only) = above the Free Tier 1,500 / 9,000 but inside the paid 3,000 / 18,000. Oracle's
  pages disagree on whether PAYG gets the larger quota, so the user should watch the Cost rows; if A1 charges appear,
  shrink to 2 OCPU / 12 GB.
- 🔴 = over the free limit → tell the user what to delete or shrink.
- Idle 🟠 = 7-day CPU p95 < 20% (and RAM < 20% on A1): Free Tier (non-upgraded) accounts can have the VM reclaimed.
- The **Notes** footer only lists sections that need attention; no notes means everything is green.
