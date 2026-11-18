---
name: oci-free-tier-guard
description: "Optional: shrink or stop Oracle Cloud A1 VMs before they leave the free quota (no-agent cron)."
version: 0.2.0
author: pgen0x
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [oracle-cloud, oci, always-free, free-tier, cost, cron, autoscale, guard]
    related_skills: [oci-free-tier-report]
---

# OCI Free Tier guard (auto-downscale / stop)

Repo: https://github.com/pgen0x/hermes-oci-free-tier-report
Optional companion of the `oci-free-tier-report` skill. A Hermes `--no-agent` cron job (hourly by default, zero
tokens) that projects Ampere A1 OCPU-hours and GB-hours to the end of the month and, **only when the projection passes
the free quota**, shrinks or stops the A1 instances it is allowed to touch. It is silent while everything is inside
the quota.

Quota by account type (read from the tenancy subscription, same as the report): **PAYG 3,000 OCPU-h / 18,000 GB-h**
(4 OCPU / 24 GB around the clock), **Free Tier 1,500 / 9,000** (2 / 12). If the account type can't be read, the guard
never changes anything, it only warns.

## When to Use
- The user wants Oracle Cloud to shrink or stop A1 automatically before it costs money, or asks for "auto
  downscale", "auto shutdown" or a "kill switch" for the free tier.
- The user wants to change the guard's mode, instances, minimum size or schedule, or remove it.
- The user asks what a guard message means.

Not for: reporting. The daily report is the `oci-free-tier-report` skill; the guard needs it installed.

## Modes (`GUARD_MODE`)
| Mode | What it does when the projection passes the quota |
|---|---|
| `dry-run` (default) | Sends what it *would* do, at most once a day. Changes nothing. |
| `downscale` | Resizes A1 Flex instances to the largest OCPU / RAM the remaining quota pays for. The instance **reboots**; when it is the VM running Hermes, Hermes comes back by itself. |
| `stop` | Soft-stops A1 instances. Skips the VM running Hermes unless `GUARD_ALLOW_SELF=1` (if allowed, Hermes is offline until the user starts the VM in the OCI Console). |
| `off` | Nothing. |

Other settings in `oci_free_tier_guard.conf` (next to the installed script):
`GUARD_INSTANCES` = `self` (default, the VM running Hermes) | `all` | comma-separated OCIDs or display names;
`GUARD_ALLOW_SELF=1`; `GUARD_MIN=<ocpu>/<gb>` = smallest size downscale may set (default `1/6`).
It acts on instances in this order: other instances first, the VM running Hermes last, biggest first.

## Install (in order)
0. **The report must be installed and working first** (`oci-free-tier-report`, its `install.sh` exit code 0). If
   it isn't, install that skill and follow its SKILL.md before continuing.
1. **Install this skill from the hub** (never copy the folder by hand; hub installs are what `skills update` tracks):
   ```bash
   hermes [-p <profile>] skills install pgen0x/hermes-oci-free-tier-report/skills/oci-free-tier-guard --yes
   ```
   `SKILL_DIR` = `~/.hermes/profiles/<profile>/skills/oci-free-tier-guard` (default profile: `~/.hermes/skills/…`).
2. **Explain the trade-off in two lines and let the user pick the mode.** Default `dry-run`. Do not pick `downscale`
   or `stop` on the user's behalf: they reboot or stop VMs.
3. Run the installer (same `--deliver` target as the report):
   ```bash
   bash "$SKILL_DIR/install.sh" --profile <profile> --deliver <target> --mode dry-run
   ```
4. Read the exit code:
   - `0` → done. Show the preview: `~/.local/share/hermes-oci-free-tier-report/venv/bin/python
     <profile scripts dir>/oci_free_tier_guard.py --dry-run`.
   - `3` → mode `downscale` and the VM has no update permission yet. Send the user the exact statement the preflight
     printed, to add to their existing `oci-free-tier-report` policy in the OCI Console (Identity & Security →
     Policies → the policy → Edit policy statements). Re-check with `oci_free_tier_guard.py --check` when they say
     it's done. You cannot add IAM statements yourself.
   - `1` → read the error (usually: the report is not installed in that profile).

### IAM (only for `downscale` / `stop`)
`dry-run` needs nothing new. The other modes need one more statement in the report's policy, for the same dynamic
group:
```
Allow dynamic-group oci-free-tier-report to use instances in tenancy where request.permission = 'INSTANCE_UPDATE'
Allow dynamic-group oci-free-tier-report to use instances in tenancy where request.permission = 'INSTANCE_POWER_ACTIONS'
```
The first is for `downscale`, the second for `stop`. OCI policies can't name a single instance, so these allow
resizing / power actions on any instance in the tenancy; the guard itself only touches `GUARD_INSTANCES`. Neither
allows creating, terminating or deleting anything. If the dynamic group lives in a non-Default identity domain,
write it as `'<domain>'/'oci-free-tier-report'` (and set `GUARD_DYNAMIC_GROUP` to match so messages show it right).

## Change settings
Re-run the installer with only what changes; everything else is kept:
```bash
bash "$SKILL_DIR/install.sh" --profile <profile> --deliver <target> --mode downscale     # or stop / dry-run / off
bash "$SKILL_DIR/install.sh" --profile <profile> --deliver <target> --instances all --min 2/12
```
Schedule: `--schedule "*/30 * * * *"` or `hermes [-p <profile>] cron edit <id> --schedule ...`. Running more often
than hourly gains little: Oracle's usage data lags by hours.

## Maintain
- Update: `hermes [-p <profile>] skills update oci-free-tier-guard`, then re-run
  `bash "$SKILL_DIR/install.sh" --profile <profile> --deliver <target>`.
- Remove: `bash "$SKILL_DIR/install.sh" --uninstall --profile <profile>`, then
  `hermes [-p <profile>] skills uninstall oci-free-tier-guard`, and tell the user they can delete the extra policy
  statements.

## Reading guard messages
- No message = inside the quota. The guard only speaks when the projection passes it, after acting, or on errors.
- `mode: dry-run` = a preview; nothing changed. The same preview repeats at most once a day.
- `Done:` = OCI accepted the change. The next run sends a follow-up with the instance's new size and state.
- `Still … over after this` = even after its changes the month will pass the quota, because of `GUARD_MIN`, the
  skipped Hermes VM in stop mode, or instances outside `GUARD_INSTANCES`. Billing is $0.01 per OCPU-h and $0.0015 per
  GB-h above the quota; tell the user what to stop or shrink.
- `OCI refused the change` = missing IAM statement (see above).
