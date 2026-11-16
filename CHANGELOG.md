# Changelog

Versions follow [Semantic Versioning](https://semver.org/). The version lives in two places that must move
together: `__version__` in `skills/oci-free-tier-report/oci_free_tier_report.py` and `version:` in `skills/oci-free-tier-report/SKILL.md`.

## [1.4.0] — 2026-11-16

The Hermes skill is now the whole package ([#3](https://github.com/pgen0x/hermes-oci-free-tier-report/issues/3)).

### Changed
- **BREAKING:** `install.sh`, `oci_free_tier_report.py`, `requirements.txt` and `locales/` moved from the repo root
  into `skills/oci-free-tier-report/`. `hermes skills install` downloads that folder, so installing needs no
  `git clone`, and `hermes skills update` now updates the script as well as the instructions.
- SKILL.md: install from the hub, run `bash <skill dir>/install.sh` (hub installs drop the executable bit), never
  copy the skill into `skills/` by hand; new update and migration steps.
- Custom languages are created in the profile's `scripts/oci_free_tier_report_locales/`; reinstalls refresh the
  bundled locales but keep custom ones, so a skill update can't wipe them.

### Migrating from 1.3.x
```bash
hermes [-p <profile>] skills install pgen0x/hermes-oci-free-tier-report/skills/oci-free-tier-report
bash <skill dir>/install.sh --profile <profile> --deliver <same target>
rm -rf ~/.local/src/hermes-oci-free-tier-report   # old clone, no longer used
```
Existing cron jobs and settings are kept. If your bot copied SKILL.md into `skills/` by hand, delete that folder
before installing from the hub.

## [1.3.0] — 2026-11-16

### Added
- Localized reports ([#1](https://github.com/pgen0x/hermes-oci-free-tier-report/issues/1)): `REPORT_LANG` /
  `--lang`, locale files in `locales/<code>.json` overlaying the built-in English strings, with fallback to English
  for missing or broken keys. Numbers, dates, tables, notes, alerts and the IAM setup guide are localized.
- Bahasa Indonesia (`id`) bundled.
- `--check-locale`, `--dump-locale`, `--list-locales`.
- `install.sh --lang` ships locale files next to the script and validates the language.
- SKILL.md: the Hermes agent can create a new language once at install time; daily runs stay LLM-free.

### Fixed
- Storage note joined two sentences without a space when backups or Object Storage were over the limit.

## [1.2.1] — 2026-11-16

### Fixed
- Install and clone URLs in README / SKILL.md pointed at a repository name that does not exist (the rename was
  blocked on GitHub), so agent installs from v1.2.0 docs failed. Use `pgen0x/hermes-oci-free-tier-report`.

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
