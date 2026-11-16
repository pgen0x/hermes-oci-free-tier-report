# hermes-oci-free-tier-report

A daily **Oracle Cloud (OCI) Always Free usage report** for [Hermes](https://github.com/NousResearch/hermes-agent) —
so your free VM stays free.

- **No AI / zero tokens.** Runs as a Hermes `--no-agent` cron job: pure OCI API calls, stdout is delivered verbatim.
- **Any chat platform.** A compact monospace table that renders on Telegram, Discord, WhatsApp, Slack, Signal,
  Matrix… — wherever your Hermes gateway delivers.
- **No secrets on disk.** Authenticates as the VM itself (OCI instance principal), read-only.
- **Checks Oracle's Always Free terms**, not just "is it running": cost, monthly A1 hours, storage, egress,
  idle-reclaim risk, load balancers, Autonomous DBs.

```
OCI Always Free · Mon 16 Nov 2026
eu-frankfurt-1 · 07:00 CET

✅ All within Always Free — cost 0

COST SGD          AMOUNT     FREE
Yesterday           0.00     0.00 ✅
Month to date       0.00     0.00 ✅

COMPUTE             USED    LIMIT
A1 OCPU live           4        4 🟠
A1 RAM GB live        24       24 🟠
E2.1.Micro VMs         0        2 ✅
A1 OCPU-h used       637    3,000 ⚪
A1 OCPU-h proj.    2,077    3,000 ✅
A1 GB-h used       3,824   18,000 ⚪
A1 GB-h proj.     12,464   18,000 ✅

STORAGE             USED    LIMIT
Block+boot GB        200      200 🟠
Volume backups         0        5 ✅
Object Stor. GB     0.00       20 ✅

NETWORK             USED    LIMIT
Egress GB (MTD)     5.09   10,240 ✅

IDLE 7d p95         CPU%     RAM%
my-a1-vm            18.4     36.1 ✅

OTHER               USED    LIMIT
Load balancers         0        1 ✅
Network LBs            0        1 ✅
Autonomous DBs         0        2 ✅

✅ <70%  🟡 70-90%  🟠 ≥90%  🔴 over
```

## Install — the easy way

Tell your Hermes bot, in the chat where you want the report:

> Install https://github.com/pgen0x/hermes-oci-free-tier-report as a daily cron job in this chat at 7am.

The bot follows [`skills/oci-free-tier-report/SKILL.md`](skills/oci-free-tier-report/SKILL.md): it clones the repo,
runs the installer for its own profile, targets the current chat, and — if Oracle permissions are missing — sends
you the exact two console steps to fix it (with your instance OCID filled in).

You can also install the skill so the bot always knows how to manage it:

```bash
hermes skills install pgen0x/hermes-oci-free-tier-report/skills/oci-free-tier-report
```

## Install — manually

On the OCI VM where Hermes runs:

```bash
git clone https://github.com/pgen0x/hermes-oci-free-tier-report
cd hermes-oci-free-tier-report
./install.sh --deliver telegram                    # default profile → Telegram home channel, 07:00 local time
```

More examples:

```bash
./install.sh --profile work --deliver discord:123456789012345678
./install.sh --deliver telegram:-1001234567890 --schedule "30 8 * * *" --timezone Asia/Jakarta
./install.sh --deliver whatsapp:6281234567890@s.whatsapp.net
./install.sh --no-cron                             # install + permission check only
./install.sh --uninstall --profile work
```

The installer:

1. builds a private venv with the OCI SDK at `~/.local/share/hermes-oci-free-tier-report/venv`,
2. copies `oci_free_tier_report.py` into the profile's `scripts/` folder (Hermes only runs scripts from there),
3. runs a **preflight** that tests every OCI permission the report needs,
4. creates (or updates, if it already exists) the cron job:
   `hermes cron create "0 7 * * *" --script oci_free_tier_report.py --no-agent --interpreter <venv python> --deliver <target>`.

Re-running it is safe. Exit code `3` means "installed, but OCI permissions are not set up yet".

## One-time OCI permission setup

The report reads your tenancy as the VM itself (instance principal), so the VM needs read-only access.
The preflight prints these steps with your instance OCID filled in:

1. **Identity & Security → Domains → Default → Dynamic groups → Create dynamic group**
   - Name: `oci-free-tier-report`
   - Rule: `instance.id = '<your instance OCID>'`
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

Dynamic groups and policies are free. Everything is read-only.

**Not running on an OCI VM?** Put an API key in `~/.oci/config` and add `OCI_AUTH=config` to the profile's `.env`.
Grant that user's group the same two `read` statements.

## What is checked

| Area | Always Free limit | Source |
|---|---|---|
| Cost yesterday / month to date | must be 0 | Usage API |
| A1 Flex live OCPU / RAM | 4 OCPU / 24 GB | Compute API |
| A1 OCPU-hours / GB-hours per month (used + projection) | 3,000 / 18,000 | Usage API |
| VM.Standard.E2.1.Micro | 2 instances | Compute API |
| Any other shape | not free → alert | Compute API |
| Boot + block volumes | 200 GB | Block Storage API |
| Volume backups | 5 | Block Storage API |
| Object Storage | 20 GB | Object Storage API |
| Outbound data (VNIC egress, conservative) | 10 TB / month | Monitoring `oci_vcn` |
| Idle reclaim (7-day p95) | CPU ≥ 20% or (A1) RAM ≥ 20% | Monitoring `oci_computeagent` |
| Load balancer / Network LB | 1 / 1 | LB / NLB API |
| Autonomous Database | 2 free, 0 paid | Database API |

Limits come from Oracle's [Always Free resources](https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) page.

**FAQ: my A1 shows 🟠 4/4 — will I be charged?** No. 🟠 means *at* the limit, not over it.
A 4 OCPU / 24 GB A1 running 24/7 uses at most 2,976 OCPU-hours and 17,856 GB-hours in a 31-day month,
below the 3,000 / 18,000 free quota, and the quota resets every month. The cost rows are the final word:
if they say `0.00`, you are not paying.

**Idle reclaim** only applies to Free Tier accounts that were not upgraded to Pay As You Go.
The idle check needs the Oracle Cloud Agent's Compute Instance Monitoring plugin (on by default on Oracle images).

## Options

Set these in the Hermes profile's `.env` (Hermes loads it for no-agent jobs):

| Variable | Effect |
|---|---|
| `REPORT_FORMAT=table` | default — table inside a code block; renders on all chat apps |
| `REPORT_FORMAT=plain` | same table without code fences (SMS, IRC, email) |
| `REPORT_FORMAT=json` | machine-readable output |
| `REPORT_ASCII=1` | `OK / WARN / HIGH / OVER` instead of emoji |
| `REPORT_QUIET=1` | send nothing on days when all checks pass and nothing failed |
| `REPORT_TZ=Asia/Jakarta` | timezone for the header (default: system timezone) |
| `OCI_AUTH=config` | use `~/.oci/config` instead of instance principal (`OCI_CONFIG_FILE`, `OCI_PROFILE`) |

## Manage

```bash
hermes cron list                       # find the job id ("OCI Always Free Usage Report")
hermes cron run <id>                   # send a report now
hermes cron edit <id> --schedule "0 9 * * *" --deliver discord:123
hermes cron pause <id>
./install.sh --uninstall
```

Prefix with `hermes -p <profile>` for a named profile.

## Cost of running this

A few dozen read-only API calls per day. OCI API calls are free; the only metered part is Object Storage requests
(50,000/month free) and this uses one or two per bucket per day.

## License

MIT
