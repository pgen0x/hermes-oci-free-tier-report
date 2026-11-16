# Oracle Cloud Free Tier Monitor for Hermes — OCI Always Free usage alerts on Telegram, Discord & WhatsApp

**Stay inside the Oracle Cloud Always Free tier and never get a surprise bill.**
A daily **Oracle Cloud Infrastructure (OCI) Always Free usage report** that runs as a
[Hermes Agent](https://github.com/NousResearch/hermes-agent) cron job and posts a table to Telegram, Discord,
WhatsApp, Slack, Signal or Matrix. It checks your tenancy against Oracle's Always Free limits — billed cost,
Ampere A1 OCPU and memory hours, block volume and Object Storage, outbound data transfer, idle instance
reclamation risk, load balancers and Autonomous Databases — and tells you when something is close to, or over,
the free limit.

- **Zero cost, zero AI tokens.** A Hermes `--no-agent` cron job: pure OCI SDK calls, no LLM involved.
- **Native tables in chat.** Real tables on Telegram (Bot API Rich Messages), aligned tables on Slack, Signal and
  Matrix, a phone-width monospace table on Discord and WhatsApp — picked automatically from the delivery target.
- **No API keys on disk.** Authenticates as the VM itself (OCI instance principal) with a read-only policy.
- **Plain-language warnings.** Green sections stay quiet; anything near or over a limit gets a note telling you
  what it means and what not to do.
- **Your language.** English and Bahasa Indonesia built in; your Hermes bot can add any other language once at
  install time — the daily report still uses no AI.
- **One sentence to install.** Ask your Hermes bot to install it and it sets up the cron job in the current chat.

> **Requirement: runs only on an OCI compute instance.** Hermes must be installed on the Oracle Cloud VM you want
> to monitor (Ampere A1 Flex or VM.Standard.E2.1.Micro, Ubuntu / Oracle Linux). The report signs in as that VM via
> instance principal, so it does not work on a laptop, another cloud or a non-OCI VPS.

## Contents
- [Example report](#example-report)
- [Install with one message to your Hermes bot](#install-with-one-message-to-your-hermes-bot)
- [Manual install](#manual-install)
- [One-time OCI permission setup](#one-time-oci-permission-setup)
- [Oracle Always Free limits checked](#oracle-always-free-limits-checked)
- [Languages](#languages)
- [FAQ](#faq)
- [Options](#options) · [Manage](#manage) · [How authentication works](#how-authentication-works)

## Example report

On Telegram every section arrives as a native table:

| Compute | Used | Limit | Status |
|:---|---:|---:|:---:|
| A1 OCPU | 4 | 4 | 🟠 |
| A1 RAM (GB) | 24 | 24 | 🟠 |
| E2.1.Micro VMs | 0 | 2 | ✅ |

| A1 hours | Used | Proj. | Limit | Status |
|:---|---:|---:|---:|:---:|
| OCPU-h | 637 | 2,077 | 3,000 | ✅ |
| GB-h | 3,824 | 12,464 | 18,000 | ✅ |

…plus **Cost**, **Storage**, **Network**, **Idle** and **Other** tables, then a footer that only explains what needs
attention:

> **Notes**
> 🟠 **Compute**: A1 is at the free cap (4 OCPU / 24 GB) — still free, but any new or bigger A1 instance would be billed.
> 🟠 **Storage**: Boot + block volumes use the full free 200 GB — don't create new volumes or enlarge the boot volume.

The `mono` format (Discord, WhatsApp) looks like:

```
COMPUTE        Used Limit
A1 OCPU           4     4 🟠
A1 RAM (GB)      24    24 🟠
E2.1.Micro VMs    0     2 ✅

A1 HOURS  Used  Proj.  Limit
OCPU-h     637  2,077  3,000 ✅
GB-h     3,824 12,464 18,000 ✅
```

## Install with one message to your Hermes bot

In the chat where you want the daily report, tell your Hermes bot:

> Install https://github.com/pgen0x/hermes-oci-free-tier-report for your own profile as a daily no-agent
> cron job that posts to this chat at 07:00. Follow skills/oci-free-tier-report/SKILL.md step by step and tell me
> the installer's exit code.

The bot follows [`skills/oci-free-tier-report/SKILL.md`](skills/oci-free-tier-report/SKILL.md): it clones the repo,
runs the installer for its own profile, targets the current chat, sends a first report, and — if Oracle
permissions are missing — gives you the two console steps with your instance OCID filled in.

To make the bot permanently aware of the skill (updates, "what does this line mean?"):

```bash
hermes skills install pgen0x/hermes-oci-free-tier-report/skills/oci-free-tier-report
```

## Manual install

On the OCI VM where Hermes runs:

```bash
git clone https://github.com/pgen0x/hermes-oci-free-tier-report
cd hermes-oci-free-tier-report
./install.sh --deliver telegram          # default profile → Telegram home channel, 07:00 local time
```

More examples:

```bash
./install.sh --profile work --deliver discord:123456789012345678
./install.sh --deliver telegram:-1001234567890 --schedule "30 8 * * *" --timezone Asia/Jakarta
./install.sh --deliver whatsapp:6281234567890@s.whatsapp.net   # → mono format
./install.sh --deliver telegram --format mono                     # override the automatic format
./install.sh --deliver telegram --lang id                         # report in Bahasa Indonesia
./install.sh --deliver telegram --unwrap                          # drop Hermes' "Cronjob Response" header
./install.sh --no-cron                                            # install + permission check only
./install.sh --uninstall --profile work
```

What the installer does:

1. builds a private venv with the OCI Python SDK at `~/.local/share/hermes-oci-free-tier-report/venv`;
2. copies `oci_free_tier_report.py` into the profile's `scripts/` folder (Hermes only runs scripts from there);
3. runs a **preflight** that tests every OCI permission the report needs;
4. writes `oci_free_tier_report.conf` next to the script (output format + timezone), and for Telegram enables
   `platforms.telegram.extra.rich_messages` in the profile — native tables are opt-in in Hermes, so restart that
   profile once afterwards: `hermes [-p <profile>] gateway restart`;
5. creates the cron job, or updates it if it already exists:
   `hermes cron create "0 7 * * *" --script oci_free_tier_report.py --no-agent --interpreter <venv python> --deliver <target>`.

Re-running it is safe. Inside a Hermes session it detects the session's profile on its own.
Exit codes: `0` ready · `3` installed but OCI permissions are missing · `1` error.

## One-time OCI permission setup

The VM needs read-only access to your tenancy. The preflight prints these steps with your instance OCID filled in:

1. **Identity & Security → Domains → Default → Dynamic groups → Create dynamic group**
   - Name: `oci-free-tier-report`
   - Matching rule: `instance.id = '<your instance OCID>'`
2. **Identity & Security → Policies →** select the **root** compartment **→ Create policy**
   ```
   Allow dynamic-group oci-free-tier-report to read usage-report in tenancy
   Allow dynamic-group oci-free-tier-report to read all-resources in tenancy
   ```
3. Wait 1–5 minutes, then check:
   ```bash
   ~/.local/share/hermes-oci-free-tier-report/venv/bin/python ~/.hermes/scripts/oci_free_tier_report.py --check
   ```
   (For a named profile the script lives in `~/.hermes/profiles/<name>/scripts/`.)

Dynamic groups and policies are free, and everything is read-only.

**Skipped this step?** Nothing breaks. Until the permissions exist, the daily message is this setup guide (instance
OCID filled in, policy lines in copyable code blocks). Once the policy exists, the next run sends the normal report
— no reinstall. It can't be automated from the VM: an instance cannot grant permissions to itself; a tenancy
administrator has to create them.

## Oracle Always Free limits checked

| Area | Always Free limit | Source |
|---|---|---|
| Billed cost, yesterday and month to date | must be 0 | Usage API |
| Ampere A1 Flex running OCPU / memory | 4 OCPU / 24 GB | Compute API |
| A1 OCPU-hours / GB-hours per month (used + projection) | 3,000 / 18,000 | Usage API |
| VM.Standard.E2.1.Micro | 2 instances | Compute API |
| Any other compute shape | not free → alert | Compute API |
| Boot + block volumes | 200 GB | Block Storage API |
| Volume backups | 5 | Block Storage API |
| Object Storage | 20 GB | Object Storage API |
| Outbound data transfer (VNIC egress, conservative) | 10 TB / month | Monitoring `oci_vcn` |
| Idle instance reclamation (7-day p95) | CPU ≥ 20%, or (A1) memory ≥ 20% | Monitoring `oci_computeagent` |
| Load balancer / Network Load Balancer | 1 / 1 | LB / NLB API |
| Autonomous Database | 2 free, 0 paid | Database API |

Limits follow Oracle's [Always Free resources](https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) documentation.

## Languages

Built in: **English** (`en`, default) and **Bahasa Indonesia** (`id`) — numbers, dates, tables, notes and the
setup guide are all localized. Pick one with `install.sh --lang <code>` (or `REPORT_LANG` in
`oci_free_tier_report.conf`).

**Any other language:** ask your Hermes bot for it when installing ("…daily report in Japanese"). Following the
skill, it generates `locales/<code>.json` once from the English template, validates it with `--check-locale`, and
installs with `--lang <code>`. The daily run stays LLM-free. Manually:

```bash
PY=~/.local/share/hermes-oci-free-tier-report/venv/bin/python
$PY oci_free_tier_report.py --dump-locale > locales/ja.json   # English template
# translate the values; keep keys and {placeholders}
$PY oci_free_tier_report.py --check-locale ja
./install.sh --deliver telegram --lang ja
```

Missing or broken keys fall back to English, so a partial translation never breaks the report. OCI identifiers
(shape names, OCPU, GB-h, Console menu paths, IAM policy statements) are intentionally left in English so they
match what you see and paste in the Oracle Console. Pull requests adding `locales/<code>.json` are welcome.

## FAQ

**Will I be charged if my Ampere A1 shows 4/4 OCPU?**
No. 🟠 means *at* the limit, not over it. A 4 OCPU / 24 GB A1 running 24/7 uses at most 2,976 OCPU-hours and
17,856 GB-hours in a 31-day month — below the 3,000 / 18,000 free quota, which resets every month. The cost rows are
the final word: `0.00` means you are not paying.

**What is Oracle's idle instance reclamation?**
On Free Tier accounts (not upgraded to Pay As You Go), Oracle may reclaim an Always Free VM whose 7-day 95th
percentile CPU is under 20% — and for A1 shapes, memory too. The report shows both numbers and warns before you
qualify. It needs the Oracle Cloud Agent's monitoring plugin, which is on by default on Oracle images.

**Does this cost anything to run?**
No. It makes a few dozen read-only API calls per day. OCI API calls are free; the only metered part is Object
Storage requests (50,000/month free), and the report uses one or two per bucket per day. No LLM tokens are used.

**Can I run it outside Oracle Cloud?**
No — it must run on the OCI compute instance it monitors (see [How authentication works](#how-authentication-works)).

**Tables arrive on Telegram as plain text.**
Telegram Rich Messages are off for that profile or the profile wasn't restarted after the installer enabled them:
`hermes [-p <profile>] gateway restart`.

**There's a big "Cronjob Response: …" heading above the report.**
That's Hermes' cron wrapper. Re-run the installer with `--unwrap` (sets `cron.wrap_response: false`; profile-wide).

## Options

Settings live in `oci_free_tier_report.conf` next to the installed script (`KEY=VALUE`; environment variables
override it). `install.sh --format` and `--timezone` write it for you.

| Setting | Effect |
|---|---|
| `REPORT_LANG=id` | report language (`en` default, `id`, or any `locales/<code>.json`) |
| `REPORT_FORMAT=markdown` | GFM tables — native on Telegram, aligned on Slack/Signal/Matrix (default for those) |
| `REPORT_FORMAT=mono` | monospace table in a code block — default for Discord and WhatsApp |
| `REPORT_FORMAT=plain` | monospace table without code fences — SMS, email, IRC |
| `REPORT_FORMAT=json` | machine-readable output |
| `REPORT_ASCII=1` | `OK / WARN / HIGH / OVER` instead of emoji |
| `REPORT_QUIET=1` | send nothing on days when everything is green |
| `REPORT_TZ=Asia/Jakarta` | timezone for the header |

## Manage

```bash
hermes cron list                       # find the job id ("OCI Always Free Usage Report")
hermes cron run <id>                   # send a report now
hermes cron edit <id> --schedule "0 9 * * *" --deliver discord:123
hermes cron pause <id>
./install.sh --uninstall
```

Prefix with `hermes -p <profile>` for a named profile. To update: `git pull`, then re-run `install.sh` with the
same flags.

## How authentication works

Every OCI VM can reach its Instance Metadata Service at `169.254.169.254`. The OCI SDK fetches the VM's
auto-rotated X.509 certificate from it, exchanges that for a short-lived security token (≈20 minutes) and signs
each API call with it — no API keys, no config file. The dynamic group says "this VM", the policy says "may read".
Keep that policy **read-only**: any process on the VM can obtain the same token.

## License

[MIT](LICENSE)
