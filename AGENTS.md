# Instructions for AI agents

This repo only works on an OCI compute instance. If a user asked you to install it into Hermes, follow
[`skills/oci-free-tier-report/SKILL.md`](skills/oci-free-tier-report/SKILL.md) — it is the install runbook.

Short version (no clone; the skill folder ships the installer, script and locales):

```bash
hermes [-p <profile>] skills install pgen0x/hermes-oci-free-tier-report/skills/oci-free-tier-report --yes
bash <skill dir>/install.sh --profile <hermes profile> --deliver <platform>:<chat_id> --schedule "0 7 * * *" --timezone <IANA tz>
```

- `<skill dir>` = `~/.hermes/profiles/<profile>/skills/oci-free-tier-report` (`~/.hermes/skills/…` for default).
- Never copy the skill into `skills/` by hand — it becomes a `local` skill `hermes skills update` can't track.
- Exit `0`: ready → `hermes [-p <profile>] cron run <job id>` to send a first report.
- Exit `3`: OCI IAM missing → relay the preflight's "One-time setup in the OCI Console" block to the user verbatim.
- The job is `--no-agent`: never convert it into an LLM prompt job.
- Update: `hermes [-p <profile>] skills update oci-free-tier-report`, then re-run `bash <skill dir>/install.sh` with
  the same `--deliver`.

Development: everything that ships lives in `skills/oci-free-tier-report/` (the Hermes hub installs that whole
folder). The report is a single file, `oci_free_tier_report.py` (only dependency: `oci`).
Every user-facing string lives in the `EN` dict and is rendered with `T(key, **vars)` — never hard-code text.
When you add or change a key, update every `locales/*.json` and run `--check-locale <code>` for each.
Bump `version:` in SKILL.md and `__version__` in the script together.
Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:` …).
Output is built as sections of rows, then rendered as GFM tables (`markdown`, default) or a monospace table
(`mono`/`plain`, kept within 34 characters + status icon so it fits a phone screen).
