# Changelog

Versions follow [Semantic Versioning](https://semver.org/). Until 1.0.0 the project is in initial development:
minor versions may change paths or options. The version lives in two places that must move together:
`version:` in `skills/oci-free-tier-report/SKILL.md` and `__version__` in
`skills/oci-free-tier-report/oci_free_tier_report.py`.

## [0.1.0] — 2026-11-16

First public release.

### Features
- Daily **OCI Always Free usage report** as a Hermes `--no-agent` cron job: pure OCI SDK calls, zero LLM tokens.
- Checks billed cost (in the tenancy's currency), Ampere A1 OCPU-hours and GB-hours with a month-end projection,
  E2.1.Micro count, block/boot volumes and backups, Object Storage, outbound data transfer, idle-reclaim risk
  (7-day CPU/RAM p95), load balancers, network load balancers and Autonomous Databases.
- Authenticates as the VM (instance principal), read-only IAM policy, no API keys on disk.
- Output formats picked from the delivery target: `markdown` (native Telegram tables via Rich Messages), `mono`
  (Discord/WhatsApp), `plain` (SMS/email/IRC), `json`.
- Notes footer only for sections that need attention; IAM setup guide (with the instance OCID filled in) when
  permissions are missing, which turns into the normal report once the policy exists.
- Localized reports: English (default) and Bahasa Indonesia bundled; agents can add any language once at install
  time (`--dump-locale`, `--check-locale`); custom locales survive updates.

### Packaging
- Ships as a Hermes skill: `hermes skills install pgen0x/hermes-oci-free-tier-report/skills/oci-free-tier-report`
  downloads the runbook, installer, script and locales together; `hermes skills update` keeps them current.
- Passes the Hermes skills guard as a community skill (safe verdict).
- `install.sh`: profile auto-detection inside Hermes sessions, private venv, permission preflight, Telegram rich
  messages, cron job created or updated in place, `--lang`, `--format`, `--unwrap`, `--uninstall`.
