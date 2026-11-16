# Instructions for AI agents

This repo only works on an OCI compute instance. If a user asked you to install it into Hermes, follow
[`skills/oci-free-tier-report/SKILL.md`](skills/oci-free-tier-report/SKILL.md) — it is the install runbook.

Short version:

```bash
./install.sh --profile <hermes profile> --deliver <platform>:<chat_id> --schedule "0 7 * * *" --timezone <IANA tz>
```

- Exit `0`: ready → `hermes [-p <profile>] cron run <job id>` to send a first report.
- Exit `3`: OCI IAM missing → relay the preflight's "One-time setup in the OCI Console" block to the user verbatim.
- The job is `--no-agent`: never convert it into an LLM prompt job.

Development: the report is a single file, `oci_free_tier_report.py` (only dependency: `oci`).
Output is built as sections of rows, then rendered as GFM tables (`markdown`, default) or a monospace table
(`mono`/`plain`, kept within 34 characters + status icon so it fits a phone screen).
