# Changelog

Versions follow [Semantic Versioning](https://semver.org/). The version lives in two places that must move
together: `__version__` in `oci_free_tier_report.py` and `version:` in `skills/oci-free-tier-report/SKILL.md`.

## [1.2.0] — 2026-11-16

### Added
- Notes footer: plain-language explanation only for sections that need attention (🟡 🟠 🔴 ⚪); green stays quiet.
- IAM setup guide: while OCI permissions are missing, the daily message is the one-time setup steps (instance OCID
  filled in, policy statements in copyable code blocks) and turns into the normal report once IAM is in place.
- `install.sh --unwrap` to drop Hermes' "Cronjob Response" header (`cron.wrap_response: false`).
- Hermes skill metadata in `SKILL.md` (version, author, license, platforms, tags, "When to Use").

### Fixed
- `install.sh` inside a Hermes profile session: `HERMES_HOME` points at `<root>/profiles/<name>`, which made the
  installer look for `profiles/<name>/profiles/<name>` and exit 1. It now derives the Hermes root and defaults
  `--profile` to the session's profile.

### Changed
- Documented as OCI-compute-instance only (instance principal); the API-key fallback is unsupported.
- README rewritten for search (title, intro, contents, FAQ).

## [1.1.0] — 2026-11-16

### Added
- `markdown` output (default): GFM tables, rendered natively on Telegram via Bot API Rich Messages.
- `mono` / `plain` / `json` formats; `install.sh` picks the format from the `--deliver` platform.
- `oci_free_tier_report.conf` next to the installed script for settings.
- `install.sh` enables `platforms.telegram.extra.rich_messages` for Telegram targets.
- "A1 hours" table with used, projected and limit.

## [1.0.0] — 2026-11-16

### Added
- Daily OCI Always Free report as a Hermes `--no-agent` cron job: cost, A1 OCPU/RAM, A1 hours, E2.1.Micro,
  block/boot volumes, backups, Object Storage, egress, idle-reclaim risk, load balancers, Autonomous DB.
- `install.sh` with venv setup, permission preflight (`--check`), idempotent cron create/update, uninstall.
- `SKILL.md` runbook so a Hermes agent can install it from one chat message.
