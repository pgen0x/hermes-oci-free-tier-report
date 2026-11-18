# Changelog

Versions follow [Semantic Versioning](https://semver.org/). Until 1.0.0 the project is in initial development:
minor versions may change paths or options. The version lives in four places that must move together:
`version:` in both `skills/*/SKILL.md` and `__version__` in both scripts.

## [Unreleased]

### Added
- `oci-free-tier-guard`, an optional second skill: an hourly no-agent cron that projects A1 OCPU-h / GB-h to the
  end of the month and, only when that passes the free quota, previews (`dry-run`, default), resizes (`downscale`)
  or soft-stops (`stop`) the A1 instances listed in `GUARD_INSTANCES`. Reuses the report's code and venv; silent
  while inside the quota; never acts when the account type is unknown (#14).

## [0.1.4] — 2026-11-18

### Fixed
- PAYG A1 quota is no longer treated as uncertain. Oracle's billing rate card (SKUs B93297 / B93298) prices
  Pay As You Go A1 at $0 up to 3,000 OCPU-h / 18,000 GB-h, so PAYG A1 hours rows use the normal thresholds instead
  of ⚪, and the "Oracle's pages disagree" follow-up note is gone (#12).

## [0.1.3] — 2026-11-17

### Changed
- Tidier Notes footer: shorter notes (one or two sentences each), A1 hours shows OCPU-h and GB-h used against the
  quota, and follow-up explanations hang under their note with `↳` instead of floating as a separate paragraph.

## [0.1.2] — 2026-11-17

### Fixed
- IAM setup guide matches the current OCI Console: policies open in the Policy Builder, so the guide now says to turn
  on **Show manual editor** before pasting; adds the required **Description** for the dynamic group and the policy,
  sets the compartment in the Create Policy form, and explains the `'<domain>'/'oci-free-tier-report'` form for
  dynamic groups outside the Default domain. README explains that the instance OCID is filled in for you and where
  to find it in the Console.
- The ⚪ status (row needs a look, see note) is now explained in the legend when it appears.
- Notes footer: one paragraph per note, so chat apps no longer run them together; long A1 explanations get their
  own paragraph.

## [0.1.1] — 2026-11-17

### Fixed
- Ampere A1 limits now depend on the account type ([#7](https://github.com/pgen0x/hermes-oci-free-tier-report/issues/7)).
  Oracle cut the Always Free A1 allowance to 1,500 OCPU-h / 9,000 GB-h (2 OCPU / 12 GB); paid (Pay As You Go)
  tenancies keep 3,000 / 18,000 (4 / 24). The report used 4 / 24 for everyone, so Free Tier accounts running a
  4 OCPU / 24 GB A1 showed green while over the limit.

### Added
- Account type (`Free Tier`, `PAYG`, `account type ?`) read from the tenancy subscription, shown in the header, the
  JSON output (`account`) and `--check`. Unknown falls back to the stricter Free Tier quota.
- PAYG months above 1,500 OCPU-h / 9,000 GB-h get a ⚪ note, because Oracle's Always Free page and Arm pricing page
  disagree on the paid quota.

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
