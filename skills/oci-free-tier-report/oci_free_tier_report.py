#!/usr/bin/env python3
"""OCI Always Free daily usage report — pure OCI API calls, no LLM.

Must run on the OCI compute instance it monitors (instance-principal auth via the Instance Metadata
Service). Designed for Hermes cron `--no-agent` jobs: stdout is delivered verbatim to whatever
platform the job targets (Telegram, Discord, Slack, WhatsApp, Signal, Matrix, email, ...).

Usage:
  oci_free_tier_report.py                 # report
  oci_free_tier_report.py --check         # preflight: auth + IAM permission probe + setup hints
  oci_free_tier_report.py --format markdown|mono|plain|json
  oci_free_tier_report.py --list-locales | --dump-locale | --check-locale <code>

Settings come from environment variables, or from `oci_free_tier_report.conf` (KEY=VALUE lines)
next to this script — install.sh writes that file. Environment variables win.

  REPORT_FORMAT   markdown (default) GFM pipe tables. Hermes adapts them per platform:
                                     Telegram → native table (Bot API Rich Messages),
                                     Slack/Signal → aligned table, Discord → bullet list.
                  mono               monospace table in a ``` code block (WhatsApp, Discord)
                  plain              monospace table without fences (SMS, IRC, email)
                  json               machine-readable
  REPORT_ASCII    1 = status words (OK/WARN/HIGH/OVER) instead of emoji
  REPORT_TZ       IANA timezone for the header (default: system local time)
  REPORT_LANG     report language: en (default), id, or any locales/<code>.json; missing keys fall back to en
  REPORT_QUIET    1 = print nothing when everything is OK (alerts-only mode)
  REPORT_DEBUG    1 = tracebacks on stderr
  OCI_AUTH        instance_principal (default) | config (unsupported fallback, untested)
  OCI_CONFIG_FILE path to OCI config when OCI_AUTH=config (default ~/.oci/config)
  OCI_PROFILE     profile in that config (default DEFAULT)

Always Free limits (https://docs.oracle.com/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm):
  Ampere A1 Flex   3,000 OCPU-hours + 18,000 GB-hours per month (= 4 OCPU + 24 GB 24/7)
  E2.1.Micro       2 instances
  Block volume     200 GB total (boot + block), 5 volume backups
  Object Storage   20 GB
  Outbound data    10 TB per month
  Load balancer    1 flexible (10 Mbps); Network Load Balancer 1
  Autonomous DB    2 Always Free instances
  Idle reclaim     Free Tier accounts: an instance whose 7-day CPU p95 < 20% (and, for A1,
                   memory p95 < 20%) may be reclaimed by Oracle.
  Any non-zero cost in the Usage API means something is outside Always Free.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
import traceback
import urllib.request
import re
import string
from dataclasses import asdict, dataclass, field
from pathlib import Path

__version__ = "0.1.0"


_CONF: dict[str, str] = {}       # oci_free_tier_report.conf
_OVERRIDES: dict[str, str] = {}  # command-line flags


def load_conf() -> None:
    """Read <script>.conf (KEY=VALUE) into _CONF. The process environment is never modified."""
    conf = Path(__file__).with_suffix(".conf")
    try:
        lines = conf.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        _CONF[key.strip()] = val.strip().strip("'\"")


def setting(key: str, default: str | None = None) -> str | None:
    """Command-line flag > environment variable > conf file > default."""
    if key in _OVERRIDES:
        return _OVERRIDES[key]
    val = os.environ.get(key)
    return val if val is not None else _CONF.get(key, default)


load_conf()

try:
    import oci
except ImportError:  # pragma: no cover
    print("The `oci` Python SDK is missing. Run install.sh, or pip install -r requirements.txt", file=sys.stderr)
    sys.exit(2)

GB = 1024**3
LIMITS = {
    "a1_ocpu": 4,
    "a1_mem_gb": 24,
    "a1_ocpu_hours": 3000,
    "a1_gb_hours": 18000,
    "micro_count": 2,
    "block_gb": 200,
    "backups": 5,
    "object_gb": 20,
    "egress_gb": 10 * 1024,
    "lb": 1,
    "nlb": 1,
    "adb": 2,
    "idle_pct": 20.0,
}
FREE_SHAPES = {"VM.Standard.A1.Flex", "VM.Standard.E2.1.Micro"}
LIVE_STATES = {"RUNNING", "STARTING", "PROVISIONING", "STOPPING"}
IMDS = "http://169.254.169.254/opc/v2"

STATUS_EMOJI = {"ok": "✅", "warn": "🟡", "high": "🟠", "over": "🔴", "info": "⚪", "error": "⚠️"}
STATUS_ASCII = {"ok": "OK", "warn": "WARN", "high": "HIGH", "over": "OVER", "info": "--", "error": "ERR"}
MONO_MAX_WIDTH = 34  # monospace table width budget (excluding the status icon) — fits a phone screen


# ---------------------------------------------------------------- i18n
# English is the source of truth and lives here so the script works as a single file.
# Other languages: locales/<code>.json with the same keys (`--dump-locale` prints this dict as JSON).
# Values are str.format templates; keep every {placeholder}. Never translate OCI identifiers
# (shape names, OCPU, GB-h, IAM policy statements, Console menu paths stay as Oracle shows them).
EN: dict[str, str] = {
    "_language": "English",
    "num.thousands": ",",
    "num.decimal": ".",
    "date.weekdays": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
    "date.months": "Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec",
    "date.format": "{weekday} {day} {month} {year}",
    "title": "OCI Always Free · {date}",
    "header.alerts": "{n} alert(s)",
    "header.incomplete": "Some checks failed — status incomplete",
    "header.ok": "All within Always Free — cost 0",
    "notes.title": "Notes",
    "legend.ok": "<70%",
    "legend.warn": "70-90%",
    "legend.high": "≥90%",
    "legend.over": "over",
    "col.status": "Status",
    "col.used": "Used",
    "col.limit": "Limit",
    "col.amount": "Amount",
    "col.proj": "Proj.",
    "col.cpu": "CPU %",
    "col.ram": "RAM %",
    "sec.cost": "Cost",
    "sec.cost_currency": "Cost ({currency})",
    "sec.compute": "Compute",
    "sec.a1_hours": "A1 hours",
    "sec.storage": "Storage",
    "sec.network": "Network",
    "sec.idle": "Idle",
    "sec.idle_col": "Idle 7d p95",
    "sec.other": "Other",
    "row.yesterday": "Yesterday",
    "row.mtd": "Month to date",
    "row.a1_ocpu": "A1 OCPU",
    "row.a1_ram": "A1 RAM (GB)",
    "row.micro": "E2.1.Micro VMs",
    "row.ocpu_h": "OCPU-h",
    "row.gb_h": "GB-h",
    "row.block": "Block+boot (GB)",
    "row.backups": "Volume backups",
    "row.object": "Object (GB)",
    "row.egress": "Egress MTD (GB)",
    "row.no_vm": "no running VM",
    "row.lb": "Load balancers",
    "row.nlb": "Network LBs",
    "row.adb": "Autonomous DBs",
    "row.adb_paid": "Paid ADBs",
    "alert.paid_shape": "Instance '{name}' uses paid shape {shape}.",
    "alert.a1_over": "Live A1 capacity exceeds 4 OCPU / 24 GB.",
    "alert.micro_over": "More than 2 E2.1.Micro instances.",
    "alert.charged": "You were charged {amount} {currency} this month — something is outside Always Free!",
    "alert.a1_hours_over": "A1 {unit} projected to exceed the monthly free quota.",
    "alert.storage_over": "Block storage or backups exceed the Always Free limit.",
    "alert.object_over": "Object Storage exceeds 20 GB.",
    "alert.egress_over": "Outbound data transfer exceeds 10 TB this month.",
    "alert.idle": "'{name}' is idle by Oracle's rule (7-day p95 < 20%) — Free Tier accounts may get it reclaimed.",
    "alert.adb_paid": "{n} Autonomous Database(s) are not Always Free.",
    "alert.other_over": "Load balancer / NLB / Autonomous DB count exceeds the free limit.",
    "note.compute.over": "Running A1 capacity ({ocpu} OCPU / {mem} GB) is above the free 4 OCPU / 24 GB — the extra is billed. Shrink or stop an A1 instance.",
    "note.compute.at_cap": "A1 is at the free cap ({ocpu} OCPU / {mem} GB) — still free, but any new or bigger A1 instance would be billed.",
    "note.compute.ok": "A1 uses {ocpu} of 4 OCPU and {mem} of 24 GB — {free_ocpu} OCPU / {free_mem} GB still free to use.",
    "note.compute.micro": "{n} of 2 free E2.1.Micro VMs in use.",
    "note.cost.charged": "Something billable is running — the rows above show which service is charging you.",
    "note.cost.ok": "Billed cost from Oracle's Usage API (data lags a few hours). 0.00 means you are not paying anything.",
    "note.a1_hours": "Free monthly quota: 3,000 OCPU-h + 18,000 GB-h. Used so far {used} OCPU-h; at the current size the month ends at ~{projected}. A 4 OCPU / 24 GB A1 running 24/7 needs at most {max} OCPU-h this month, so it never runs over. Resets on the 1st.",
    "note.storage.over": "Volumes total {total} GB, {over} GB over the free 200 GB — delete or shrink a volume.",
    "note.storage.full": "Boot + block volumes use the full free 200 GB — don't create new volumes or enlarge the boot volume.",
    "note.storage.ok": "{left} GB of the free 200 GB block storage still available.",
    "note.storage.backups": "{n} volume backups — only 5 are free, delete the oldest.",
    "note.storage.object": "Object Storage holds {gb} GB — only 20 GB is free.",
    "note.network": "Outbound traffic this month: {gb} GB of the free 10 TB ({pct}%). Counted from VNIC metrics, so slightly overstated.",
    "note.idle.rule": "Oracle may reclaim an Always Free VM (Free Tier accounts) when its 7-day p95 CPU is under 20% and, on A1, RAM is also under 20%.",
    "note.idle.no_metrics": "{name}: no metrics — enable the Oracle Cloud Agent monitoring plugin.",
    "note.idle.risk": "{name}: CPU {cpu}% and RAM {ram}% are both under 20% → at risk of reclaim.",
    "note.idle.safe_cpu": "{name}: CPU {cpu}% ({cpu_side} 20%) → safe.",
    "note.idle.safe_cpu_ram": "{name}: CPU {cpu}% ({cpu_side} 20%), RAM {ram}% ({ram_side} 20%) → safe.",
    "note.idle.below": "below",
    "note.idle.above": "above",
    "note.other.free": "Free: 1 load balancer (10 Mbps), 1 network LB, 2 Autonomous DBs.",
    "note.other.in_use": "In use: {items}.",
    "note.other.none": "None in use.",
    "note.other.lb": "{n} load balancer",
    "note.other.nlb": "{n} network LB",
    "note.other.adb": "{n} free Autonomous DB",
    "err.iam_hint": "(missing IAM policy?)",
    "setup.headline": "Oracle permissions are not set up yet",
    "setup.explain": "this VM can sign in to OCI but is not allowed to read usage, so no checks could run.",
    "setup.intro": "One-time setup in the OCI Console (free, read-only), as a tenancy administrator:",
    "setup.step1": "Identity & Security → Domains → Default → Dynamic groups → Create dynamic group",
    "setup.name": "Name: {name}",
    "setup.rule": "Matching rule:",
    "setup.legacy": "(Older tenancies without identity domains: Identity → Dynamic Groups.)",
    "setup.step2": "Identity & Security → Policies → pick the root compartment → Create policy",
    "setup.statements": "Name: {name} — statements:",
    "setup.step3": "Wait 1–5 minutes. The next scheduled report fills in automatically.",
    "setup.apikey": "Grant the group of the API-key user these policy statements (root compartment):",
}

_LOCALE: dict[str, str] = dict(EN)


def locale_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent
    dirs = [here / "oci_free_tier_report_locales", here / "locales"]
    extra = setting("REPORT_LOCALE_DIR")
    if extra:
        dirs.insert(0, Path(extra).expanduser())
    return dirs


def locale_file(code: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z]{2,3}([_-][A-Za-z0-9]{2,8})?", code or ""):
        return None
    return next((d / f"{code}.json" for d in locale_dirs() if (d / f"{code}.json").is_file()), None)


def available_locales() -> list[str]:
    codes = {"en"}
    for d in locale_dirs():
        if d.is_dir():
            codes |= {f.stem for f in d.glob("*.json")}
    return sorted(codes)


def set_language(code: str) -> str:
    """Load EN, then overlay locales/<code>.json. Unknown code → English (warning on stderr)."""
    global _LOCALE
    _LOCALE = dict(EN)
    if not code or code == "en":
        return "en"
    path = locale_file(code)
    if path is None:
        print(f"REPORT_LANG={code}: no locale file found, using English", file=sys.stderr)
        return "en"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"REPORT_LANG={code}: cannot read {path} ({e}), using English", file=sys.stderr)
        return "en"
    _LOCALE.update({k: v for k, v in data.items() if k in EN and isinstance(v, str)})
    return code


def T(key: str, **kw) -> str:
    """Localized text. A broken translation (bad placeholder) falls back to English for that key."""
    try:
        return _LOCALE.get(key, EN[key]).format(**kw)
    except (KeyError, IndexError, ValueError):
        return EN[key].format(**kw)


def _placeholders(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def check_locale(code: str) -> int:
    """Validate locales/<code>.json against EN: missing/extra keys, placeholder and list mismatches."""
    path = locale_file(code)
    if path is None:
        print(f"No locale file for '{code}' in: " + ", ".join(str(d) for d in locale_dirs()))
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"{path}: invalid JSON — {e}")
        return 1
    problems = []
    for key in EN:
        if key not in data:
            problems.append(f"missing key: {key}")
        elif not isinstance(data[key], str):
            problems.append(f"{key}: value must be a string")
        elif _placeholders(data[key]) != _placeholders(EN[key]):
            problems.append(
                f"{key}: placeholders {sorted(_placeholders(data[key]))} != {sorted(_placeholders(EN[key]))}"
            )
    problems += [f"unknown key (ignored): {k}" for k in data if k not in EN]
    for key, n in (("date.weekdays", 7), ("date.months", 12)):
        if isinstance(data.get(key), str) and len(data[key].split(",")) != n:
            problems.append(f"{key}: needs {n} comma-separated names")
    print(f"{path}: {len(EN)} keys checked")
    for prob in problems:
        print(f"  ✗ {prob}")
    if not problems:
        print(f"  ✓ '{code}' ({data.get('_language', '?')}) is complete")
    return 1 if any(not prob.startswith("unknown key") for prob in problems) else 0


def fmt_date(when: dt.datetime) -> str:
    days = T("date.weekdays").split(",")
    months = T("date.months").split(",")
    if len(days) != 7 or len(months) != 12:
        days, months = EN["date.weekdays"].split(","), EN["date.months"].split(",")
    return T(
        "date.format",
        weekday=days[when.weekday()].strip(),
        day=f"{when.day:02d}",
        month=months[when.month - 1].strip(),
        year=when.year,
    )


# ---------------------------------------------------------------- data model
@dataclass
class Row:
    cells: list[str]  # first cell = label, the rest = values (already formatted)
    status: str
    values: dict = field(default_factory=dict)  # raw numbers for JSON consumers


@dataclass
class Section:
    id: str  # stable, untranslated identifier
    title: str
    columns: list[str]  # header for the label column + each value column
    rows: list[Row] = field(default_factory=list)
    note: str = ""  # plain-language explanation shown under the table


@dataclass
class Report:
    generated_at: str = ""
    timezone: str = ""
    region: str = ""
    currency: str = ""
    lang: str = "en"
    sections: list[Section] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    setup_needed: bool = False  # every check failed on authorization → show IAM setup guide instead


def level(used: float, limit: float) -> str:
    if used > limit:
        return "over"
    p = 100.0 * used / limit if limit else 0.0
    return "high" if p >= 90 else "warn" if p >= 70 else "ok"


def num(x: float, nd: int = 0) -> str:
    s = f"{x:,.{nd}f}"
    th, dec = T("num.thousands"), T("num.decimal")
    if (th, dec) == (",", "."):
        return s
    return s.replace(",", "\0").replace(".", dec).replace("\0", th)


def short_err(e: Exception) -> str:
    if isinstance(e, oci.exceptions.ServiceError):
        hint = f" {T('err.iam_hint')}" if e.status in (401, 403, 404) else ""
        return f"{e.status} {e.code}{hint}"
    return f"{type(e).__name__}: {str(e)[:120]}"


def p95(values) -> float:
    vals = sorted(v for v in values if v is not None)
    return vals[round(0.95 * (len(vals) - 1))] if vals else math.nan


def limit_row(label: str, used: float, limit: float, nd: int = 0) -> Row:
    return Row([label, num(used, nd), num(limit)], level(used, limit), {"used": used, "limit": limit})


# ---------------------------------------------------------------- OCI context
class Ctx:
    def __init__(self) -> None:
        if setting("OCI_AUTH", "instance_principal") == "config":
            self.cfg = oci.config.from_file(
                os.path.expanduser(setting("OCI_CONFIG_FILE", "~/.oci/config")),
                setting("OCI_PROFILE", "DEFAULT"),
            )
            self.kw: dict = {}
            self.tenancy = self.cfg["tenancy"]
            self.region = self.cfg["region"]
        else:
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            self.cfg = {}
            self.kw = {"signer": signer}
            self.tenancy = signer.tenancy_id
            self.region = signer.region
        self.kw["retry_strategy"] = oci.retry.DEFAULT_RETRY_STRATEGY
        self.a1_live_ocpu = self.a1_live_mem = 0.0

        identity = self.client(oci.identity.IdentityClient)
        regions = identity.list_region_subscriptions(self.tenancy).data
        self.home_region = next(r.region_name for r in regions if r.is_home_region)
        self.compartments = [self.tenancy]
        try:
            self.compartments += [
                c.id
                for c in self.list_all(
                    identity.list_compartments,
                    self.tenancy,
                    compartment_id_in_subtree=True,
                    lifecycle_state="ACTIVE",
                )
            ]
        except oci.exceptions.ServiceError:
            pass  # fall back to the root compartment only
        self.ads = [a.name for a in identity.list_availability_domains(self.tenancy).data]

    def client(self, cls, region: str | None = None):
        c = cls(self.cfg, **self.kw)
        if region:
            c.base_client.set_region(region)
        return c

    @staticmethod
    def list_all(fn, *args, **kwargs):
        return oci.pagination.list_call_get_all_results(fn, *args, **kwargs).data


# ---------------------------------------------------------------- collectors
def collect_compute(ctx: Ctx, rep: Report) -> list:
    compute = ctx.client(oci.core.ComputeClient)
    instances = [
        i
        for comp in ctx.compartments
        for i in ctx.list_all(compute.list_instances, comp)
        if i.lifecycle_state != "TERMINATED"
    ]
    micro = 0
    for i in instances:
        sc = i.shape_config
        if i.shape == "VM.Standard.A1.Flex" and i.lifecycle_state in LIVE_STATES:
            ctx.a1_live_ocpu += sc.ocpus or 0
            ctx.a1_live_mem += sc.memory_in_gbs or 0
        if i.shape == "VM.Standard.E2.1.Micro":
            micro += 1
        if i.shape not in FREE_SHAPES:
            rep.alerts.append(T("alert.paid_shape", name=i.display_name, shape=i.shape))
    rep.sections.append(
        Section(
            "compute",
            T("sec.compute"),
            [T("sec.compute"), T("col.used"), T("col.limit")],
            [
                limit_row(T("row.a1_ocpu"), ctx.a1_live_ocpu, LIMITS["a1_ocpu"]),
                limit_row(T("row.a1_ram"), ctx.a1_live_mem, LIMITS["a1_mem_gb"]),
                limit_row(T("row.micro"), micro, LIMITS["micro_count"]),
            ],
        )
    )
    sec = rep.sections[-1]
    free_ocpu = LIMITS["a1_ocpu"] - ctx.a1_live_ocpu
    free_mem = LIMITS["a1_mem_gb"] - ctx.a1_live_mem
    cap = {"ocpu": num(ctx.a1_live_ocpu), "mem": num(ctx.a1_live_mem)}
    if free_ocpu < 0 or free_mem < 0:
        sec.note = T("note.compute.over", **cap)
    elif free_ocpu == 0 or free_mem == 0:
        sec.note = T("note.compute.at_cap", **cap)
    else:
        sec.note = T("note.compute.ok", **cap, free_ocpu=num(free_ocpu), free_mem=num(free_mem))
    if micro:
        sec.note += " " + T("note.compute.micro", n=micro)
    if ctx.a1_live_ocpu > LIMITS["a1_ocpu"] or ctx.a1_live_mem > LIMITS["a1_mem_gb"]:
        rep.alerts.append(T("alert.a1_over"))
    if micro > LIMITS["micro_count"]:
        rep.alerts.append(T("alert.micro_over"))
    return [i for i in instances if i.lifecycle_state == "RUNNING"]


def collect_cost(ctx: Ctx, rep: Report) -> None:
    usage = ctx.client(oci.usage_api.UsageapiClient, ctx.home_region)
    today = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    yday = today - dt.timedelta(days=1)
    month_start = yday.replace(day=1)
    M = oci.usage_api.models

    def query(gran: str, qtype: str, group_by: list[str]):
        return usage.request_summarized_usages(
            M.RequestSummarizedUsagesDetails(
                tenant_id=ctx.tenancy,
                time_usage_started=month_start,
                time_usage_ended=today,
                granularity=gran,
                query_type=qtype,
                group_by=group_by,
            )
        ).data.items

    rows = query("DAILY", "COST", ["service"])
    rep.currency = next((i.currency for i in rows if i.currency), "")
    by_svc: dict[str, float] = {}
    y_total = 0.0
    for i in rows:
        amt = float(i.computed_amount or 0)
        by_svc[i.service or "?"] = by_svc.get(i.service or "?", 0.0) + amt
        if i.time_usage_started and i.time_usage_started.date() == yday.date():
            y_total += amt
    mtd = sum(by_svc.values())

    title = T("sec.cost_currency", currency=rep.currency) if rep.currency else T("sec.cost")
    sec = Section("cost", T("sec.cost"), [title, T("col.amount")])
    for label, amt in ((T("row.yesterday"), y_total), (T("row.mtd"), mtd)):
        sec.rows.append(Row([label, num(amt, 2)], "ok" if amt < 0.005 else "over", {"amount": amt}))
    if mtd >= 0.005:
        for svc, amt in sorted(by_svc.items(), key=lambda kv: -kv[1]):
            if amt >= 0.005:
                sec.rows.append(Row([f"· {svc}", num(amt, 2)], "over", {"amount": amt}))
        rep.alerts.append(T("alert.charged", amount=num(mtd, 2), currency=rep.currency))
        sec.note = T("note.cost.charged")
    else:
        sec.note = T("note.cost.ok")
    rep.sections.insert(0, sec)

    # A1 hours: billed month-to-date + currently-live capacity for the rest of the month
    a1_ocpu_h = a1_gb_h = 0.0
    for i in query("MONTHLY", "USAGE", ["skuName", "unit"]):
        sku = (i.sku_name or "").lower()
        if "ampere" not in sku and "a1" not in sku:
            continue
        q = float(i.computed_quantity or 0)
        if "memory" in sku:
            a1_gb_h += q
        else:
            a1_ocpu_h += q
    next_month = (month_start + dt.timedelta(days=32)).replace(day=1)
    hours_left = (next_month - today).total_seconds() / 3600
    hours = Section(
        "a1_hours", T("sec.a1_hours"), [T("sec.a1_hours"), T("col.used"), T("col.proj"), T("col.limit")]
    )
    for label, used, key, live in (
        (T("row.ocpu_h"), a1_ocpu_h, "a1_ocpu_hours", ctx.a1_live_ocpu),
        (T("row.gb_h"), a1_gb_h, "a1_gb_hours", ctx.a1_live_mem),
    ):
        proj = used + live * hours_left
        hours.rows.append(
            Row(
                [label, num(used), num(proj), num(LIMITS[key])],
                level(proj, LIMITS[key]),
                {"used": used, "projected": proj, "limit": LIMITS[key]},
            )
        )
        if proj > LIMITS[key]:
            rep.alerts.append(T("alert.a1_hours_over", unit=label))
    proj_ocpu = a1_ocpu_h + ctx.a1_live_ocpu * hours_left
    max_ocpu = LIMITS["a1_ocpu"] * (next_month - month_start).days * 24
    hours.note = T("note.a1_hours", used=num(a1_ocpu_h), projected=num(proj_ocpu), max=num(max_ocpu))
    # place right after Compute when present
    idx = next((n + 1 for n, s in enumerate(rep.sections) if s.id == "compute"), len(rep.sections))
    rep.sections.insert(idx, hours)


def collect_storage(ctx: Ctx, rep: Report) -> None:
    bs = ctx.client(oci.core.BlockstorageClient)
    alive = lambda x: x.lifecycle_state not in ("TERMINATED", "TERMINATING")  # noqa: E731
    vol_gb = backups = 0
    for comp in ctx.compartments:
        for ad in ctx.ads:
            vol_gb += sum(
                v.size_in_gbs or 0
                for v in ctx.list_all(bs.list_boot_volumes, availability_domain=ad, compartment_id=comp)
                if alive(v)
            )
        vol_gb += sum(v.size_in_gbs or 0 for v in ctx.list_all(bs.list_volumes, compartment_id=comp) if alive(v))
        backups += sum(1 for b in ctx.list_all(bs.list_volume_backups, comp) if alive(b))
        backups += sum(1 for b in ctx.list_all(bs.list_boot_volume_backups, comp) if alive(b))

    osc = ctx.client(oci.object_storage.ObjectStorageClient)
    ns = osc.get_namespace().data
    obj = 0
    for comp in ctx.compartments:
        for b in ctx.list_all(osc.list_buckets, ns, comp):
            obj += osc.get_bucket(ns, b.name, fields=["approximateSize"]).data.approximate_size or 0

    rep.sections.append(
        Section(
            "storage",
            T("sec.storage"),
            [T("sec.storage"), T("col.used"), T("col.limit")],
            [
                limit_row(T("row.block"), vol_gb, LIMITS["block_gb"]),
                limit_row(T("row.backups"), backups, LIMITS["backups"]),
                limit_row(T("row.object"), obj / GB, LIMITS["object_gb"], nd=2),
            ],
        )
    )
    sec = rep.sections[-1]
    left = LIMITS["block_gb"] - vol_gb
    if left < 0:
        parts = [T("note.storage.over", total=num(vol_gb), over=num(-left))]
    elif left == 0:
        parts = [T("note.storage.full")]
    else:
        parts = [T("note.storage.ok", left=num(left))]
    if backups > LIMITS["backups"]:
        parts.append(T("note.storage.backups", n=backups))
    if obj / GB > LIMITS["object_gb"]:
        parts.append(T("note.storage.object", gb=num(obj / GB, 2)))
    sec.note = " ".join(parts)
    if vol_gb > LIMITS["block_gb"] or backups > LIMITS["backups"]:
        rep.alerts.append(T("alert.storage_over"))
    if obj / GB > LIMITS["object_gb"]:
        rep.alerts.append(T("alert.object_over"))


def _metrics(mon, ctx: Ctx, namespace: str, query: str, start, end):
    return mon.summarize_metrics_data(
        ctx.tenancy,
        oci.monitoring.models.SummarizeMetricsDataDetails(
            namespace=namespace, query=query, start_time=start, end_time=end
        ),
        compartment_id_in_subtree=True,
    ).data


def collect_network(ctx: Ctx, rep: Report) -> None:
    mon = ctx.client(oci.monitoring.MonitoringClient)
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    # VNIC egress, month to date (conservative: also counts intra-VCN traffic)
    egress = sum(
        p.value or 0
        for s in _metrics(mon, ctx, "oci_vcn", "VnicToNetworkBytes[1h].sum()", now.replace(day=1, hour=0), now)
        for p in s.aggregated_datapoints
    )
    rep.sections.append(
        Section(
            "network",
            T("sec.network"),
            [T("sec.network"), T("col.used"), T("col.limit")],
            [limit_row(T("row.egress"), egress / GB, LIMITS["egress_gb"], nd=2)],
        )
    )
    rep.sections[-1].note = T(
        "note.network", gb=num(egress / GB, 2), pct=num(100 * egress / GB / LIMITS["egress_gb"], 3)
    )
    if egress / GB > LIMITS["egress_gb"]:
        rep.alerts.append(T("alert.egress_over"))


def collect_idle(ctx: Ctx, rep: Report, running: list) -> None:
    mon = ctx.client(oci.monitoring.MonitoringClient)
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    stats: dict[str, dict[str, float]] = {}
    for metric in ("CpuUtilization", "MemoryUtilization"):
        for s in _metrics(mon, ctx, "oci_computeagent", f"{metric}[1h].mean()", now - dt.timedelta(days=7), now):
            stats.setdefault(s.dimensions.get("resourceId"), {})[metric] = p95(
                p.value for p in s.aggregated_datapoints
            )
    sec = Section("idle", T("sec.idle"), [T("sec.idle_col"), T("col.cpu"), T("col.ram")])
    fmt = lambda v: "n/a" if math.isnan(v) else num(v, 1)  # noqa: E731
    notes = [T("note.idle.rule")]
    for i in running:
        st = stats.get(i.id, {})
        cpu = st.get("CpuUtilization", math.nan)
        mem = st.get("MemoryUtilization", math.nan)
        is_a1 = i.shape == "VM.Standard.A1.Flex"
        thr = LIMITS["idle_pct"]
        side = lambda v: T("note.idle.below") if v < thr else T("note.idle.above")  # noqa: E731
        name = i.display_name
        if math.isnan(cpu):
            status = "info"  # no Oracle Cloud Agent metrics
            notes.append(T("note.idle.no_metrics", name=name))
        elif cpu < thr and (not is_a1 or (not math.isnan(mem) and mem < thr)):
            status = "high"
            rep.alerts.append(T("alert.idle", name=name))
            notes.append(T("note.idle.risk", name=name, cpu=fmt(cpu), ram=fmt(mem)))
        else:
            status = "ok"
            if is_a1 and not math.isnan(mem):
                notes.append(
                    T("note.idle.safe_cpu_ram", name=name, cpu=fmt(cpu), cpu_side=side(cpu),
                      ram=fmt(mem), ram_side=side(mem))
                )
            else:
                notes.append(T("note.idle.safe_cpu", name=name, cpu=fmt(cpu), cpu_side=side(cpu)))
        sec.rows.append(
            Row([i.display_name, fmt(cpu), fmt(mem)], status, {"cpu_p95": cpu, "mem_p95": mem, "id": i.id})
        )
    if not running:
        sec.rows.append(Row([T("row.no_vm"), "", ""], "info"))
    sec.note = " ".join(notes)
    rep.sections.append(sec)


def collect_other(ctx: Ctx, rep: Report) -> None:
    lb_c = ctx.client(oci.load_balancer.LoadBalancerClient)
    nlb_c = ctx.client(oci.network_load_balancer.NetworkLoadBalancerClient)
    db_c = ctx.client(oci.database.DatabaseClient)
    lbs = nlbs = adb_free = adb_paid = 0
    for comp in ctx.compartments:
        lbs += sum(1 for x in ctx.list_all(lb_c.list_load_balancers, comp) if x.lifecycle_state != "DELETED")
        page = None
        while True:  # NLB list returns a collection wrapper; paginate by hand
            resp = nlb_c.list_network_load_balancers(comp, page=page)
            nlbs += sum(1 for x in resp.data.items if x.lifecycle_state != "DELETED")
            page = resp.next_page
            if not page:
                break
        for a in ctx.list_all(db_c.list_autonomous_databases, compartment_id=comp):
            if a.lifecycle_state in ("TERMINATED", "TERMINATING"):
                continue
            if a.is_free_tier:
                adb_free += 1
            else:
                adb_paid += 1
    sec = Section(
        "other",
        T("sec.other"),
        [T("sec.other"), T("col.used"), T("col.limit")],
        [
            limit_row(T("row.lb"), lbs, LIMITS["lb"]),
            limit_row(T("row.nlb"), nlbs, LIMITS["nlb"]),
            limit_row(T("row.adb"), adb_free, LIMITS["adb"]),
        ],
    )
    if adb_paid:
        sec.rows.append(limit_row(T("row.adb_paid"), adb_paid, 0))
        rep.alerts.append(T("alert.adb_paid", n=adb_paid))
    in_use = [
        T(key, n=n) for n, key in ((lbs, "note.other.lb"), (nlbs, "note.other.nlb"), (adb_free, "note.other.adb")) if n
    ]
    sec.note = T("note.other.free") + " " + (
        T("note.other.in_use", items=", ".join(in_use)) if in_use else T("note.other.none")
    )
    rep.sections.append(sec)
    if lbs > LIMITS["lb"] or nlbs > LIMITS["nlb"] or adb_free > LIMITS["adb"]:
        rep.alerts.append(T("alert.other_over"))


def build_report() -> Report:
    tzname = setting("REPORT_TZ")
    if tzname:
        from zoneinfo import ZoneInfo

        now = dt.datetime.now(ZoneInfo(tzname))
    else:
        now = dt.datetime.now().astimezone()
    rep = Report(
        generated_at=now.isoformat(timespec="minutes"),
        timezone=now.tzname() or "",
        lang=set_language(setting("REPORT_LANG", "en")),
    )
    ctx = Ctx()  # auth failure propagates → non-zero exit → Hermes failure alert
    rep.region = ctx.region
    running: list = []
    steps = [
        (T("sec.compute"), lambda: running.extend(collect_compute(ctx, rep))),
        (T("sec.cost"), lambda: collect_cost(ctx, rep)),
        (T("sec.storage"), lambda: collect_storage(ctx, rep)),
        (T("sec.network"), lambda: collect_network(ctx, rep)),
        (T("sec.idle"), lambda: collect_idle(ctx, rep, running)),
        (T("sec.other"), lambda: collect_other(ctx, rep)),
    ]
    auth_failures = 0
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            rep.errors.append(f"{name}: {short_err(e)}")
            if isinstance(e, oci.exceptions.ServiceError) and e.status in (401, 403, 404):
                auth_failures += 1
            if setting("REPORT_DEBUG"):
                traceback.print_exc(file=sys.stderr)
    rep.setup_needed = auth_failures == len(steps)
    return rep


# ---------------------------------------------------------------- renderers
def _icons() -> dict:
    return STATUS_ASCII if setting("REPORT_ASCII") == "1" else STATUS_EMOJI


def _header(rep: Report, bold: bool) -> list[str]:
    icons = _icons()
    b = (lambda s: f"**{s}**") if bold else (lambda s: s)
    when = dt.datetime.fromisoformat(rep.generated_at)
    out = [b(T("title", date=fmt_date(when))), f"{rep.region} · {when:%H:%M} {rep.timezone}", ""]
    if rep.alerts:
        out.append(f"{icons['over']} {b(T('header.alerts', n=len(rep.alerts)))}")
        out += [f"• {a}" for a in rep.alerts]
    elif rep.errors:
        out.append(f"{icons['error']} {T('header.incomplete')}")
    else:
        out.append(f"{icons['ok']} {b(T('header.ok'))}")
    return out


NOTEWORTHY = {"warn", "high", "over", "info"}  # sections with any of these get their note in the footer


def _notes(rep: Report, bold: bool) -> list[str]:
    """Explanations only for sections that need attention; all-green sections stay silent."""
    icons = _icons()
    out = []
    order = ["info", "warn", "high", "over"]
    for sec in rep.sections:
        flagged = [r.status for r in sec.rows if r.status in NOTEWORTHY]
        if not flagged or not sec.note:
            continue
        worst = max(flagged, key=order.index)
        title = f"**{sec.title}**" if bold else sec.title
        out.append(f"{icons[worst]} {title}: {sec.note}")
    return ["", f"**{T('notes.title')}**" if bold else T("notes.title")] + out if out else []


def _footer(rep: Report, bold: bool = True) -> list[str]:
    icons = _icons()
    out = _notes(rep, bold)
    if rep.errors:
        out += [""] + [f"{icons['error']} {e}" for e in rep.errors]
    legend = "  ".join(
        f"{icons[k]} {T('legend.' + k)}" for k in ("ok", "warn", "high", "over")
    )
    return out + ["", legend]


def render_markdown(rep: Report) -> str:
    """GFM pipe tables — Hermes converts them per platform (Telegram: native rich table)."""
    icons = _icons()
    out = _header(rep, bold=True)
    for sec in rep.sections:
        cols = sec.columns + [T("col.status")]
        out += ["", "| " + " | ".join(cols) + " |", "|" + "|".join([":---"] + ["---:"] * (len(cols) - 2) + [":---:"]) + "|"]
        for r in sec.rows:
            cells = [c.replace("|", "/") for c in r.cells] + [icons[r.status]]
            out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out + _footer(rep))


def render_mono(rep: Report, fenced: bool) -> str:
    """Monospace table, label column trimmed so each row fits MONO_MAX_WIDTH characters."""
    icons = _icons()
    tbl: list[str] = []
    for sec in rep.sections:
        if tbl:
            tbl.append("")
        ncol = len(sec.columns)
        vw = [max(len(sec.columns[c]), *(len(r.cells[c]) for r in sec.rows)) for c in range(1, ncol)]
        lw = max(len(sec.columns[0]), *(len(r.cells[0]) for r in sec.rows))
        lw = max(6, min(lw, MONO_MAX_WIDTH - sum(vw) - len(vw)))

        def fit(s: str, w: int, left: bool) -> str:
            s = s if len(s) <= w else s[: w - 1] + "…"
            return s.ljust(w) if left else s.rjust(w)

        tbl.append(" ".join([fit(sec.columns[0].upper(), lw, True)] + [fit(h, w, False) for h, w in zip(sec.columns[1:], vw)]))
        for r in sec.rows:
            line = " ".join([fit(r.cells[0], lw, True)] + [fit(v, w, False) for v, w in zip(r.cells[1:], vw)])
            tbl.append(f"{line} {icons[r.status]}".rstrip())
    body = [""] + ((["```"] + tbl + ["```"]) if fenced else tbl)
    return "\n".join(_header(rep, bold=fenced) + body + _footer(rep, bold=fenced))


def render_json(rep: Report) -> str:
    return json.dumps(asdict(rep), indent=2, default=str)


def setup_steps(markdown: bool) -> list[str]:
    """One-time IAM setup instructions, with this VM's instance OCID filled in when available."""
    if setting("OCI_AUTH", "instance_principal") == "config":
        stmts = [
            "Allow group <your group> to read usage-report in tenancy",
            "Allow group <your group> to read all-resources in tenancy",
        ]
        intro = [T("setup.apikey")]
        return intro + ((["```"] + stmts + ["```"]) if markdown else [f"  {x}" for x in stmts])
    instance_id = _imds("instance/id")
    rule = f"instance.id = '{instance_id}'" if instance_id else "instance.compartment.id = '<compartment OCID>'"
    stmts = [
        "Allow dynamic-group oci-free-tier-report to read usage-report in tenancy",
        "Allow dynamic-group oci-free-tier-report to read all-resources in tenancy",
    ]
    block = (lambda lines: ["```"] + lines + ["```"]) if markdown else (lambda lines: [f"     {x}" for x in lines])
    b = (lambda t: f"**{t}**") if markdown else (lambda t: t)
    return [
        T("setup.intro"),
        "",
        f"1. {b(T('setup.step1'))}",
        "   " + T("setup.name", name="oci-free-tier-report"),
        "   " + T("setup.rule"),
        *block([rule]),
        "   " + T("setup.legacy"),
        "",
        f"2. {b(T('setup.step2'))}",
        "   " + T("setup.statements", name="oci-free-tier-report-read"),
        *block(stmts),
        "",
        "3. " + T("setup.step3"),
    ]


def render_setup(rep: Report, markdown: bool) -> str:
    icons = _icons()
    when = dt.datetime.fromisoformat(rep.generated_at)
    b = (lambda t: f"**{t}**") if markdown else (lambda t: t)
    out = [
        b(T("title", date=fmt_date(when))),
        f"{rep.region} · {when:%H:%M} {rep.timezone}",
        "",
        f"{icons['error']} {b(T('setup.headline'))} — {T('setup.explain')}",
        "",
    ]
    return "\n".join(out + setup_steps(markdown))


def render(rep: Report, fmt: str) -> str:
    if fmt == "json":
        return render_json(rep)
    if rep.setup_needed:
        return render_setup(rep, markdown=fmt in ("markdown", "mono", "table"))
    if fmt in ("mono", "table"):  # "table" = v1.0 name
        return render_mono(rep, fenced=True)
    if fmt == "plain":
        return render_mono(rep, fenced=False)
    return render_markdown(rep)


# ---------------------------------------------------------------- preflight
def _imds(path: str) -> str | None:
    try:
        req = urllib.request.Request(f"{IMDS}/{path}", headers={"Authorization": "Bearer Oracle"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.read().decode()
    except Exception:
        return None


def preflight() -> int:
    lang = set_language(setting("REPORT_LANG", "en"))
    print(f"oci-free-tier-report {__version__} — preflight\n")
    auth = setting("OCI_AUTH", "instance_principal")
    instance_id = _imds("instance/id")
    print(f"auth mode    : {auth}")
    print(f"format       : {setting('REPORT_FORMAT', 'markdown')}")
    print(f"language     : {lang}")
    print(f"OCI instance : {instance_id or 'not detected (not running on OCI?)'}")
    try:
        ctx = Ctx()
    except Exception as e:
        print(f"auth         : FAILED — {short_err(e)}")
        if auth == "instance_principal" and not instance_id:
            print("\nInstance principal only works on an OCI VM. Elsewhere use OCI_AUTH=config with ~/.oci/config.")
        return 1
    print(f"auth         : OK (home region {ctx.home_region})\n")

    probes = {
        "usage-report (cost)": lambda: collect_cost(ctx, Report()),
        "compute instances": lambda: ctx.client(oci.core.ComputeClient).list_instances(ctx.tenancy, limit=1),
        "block volumes": lambda: ctx.client(oci.core.BlockstorageClient).list_volumes(
            compartment_id=ctx.tenancy, limit=1
        ),
        "monitoring metrics": lambda: ctx.client(oci.monitoring.MonitoringClient).list_metrics(
            ctx.tenancy, oci.monitoring.models.ListMetricsDetails(namespace="oci_computeagent"), limit=1
        ),
        "load balancers": lambda: ctx.client(oci.load_balancer.LoadBalancerClient).list_load_balancers(
            ctx.tenancy, limit=1
        ),
        "autonomous databases": lambda: ctx.client(oci.database.DatabaseClient).list_autonomous_databases(
            compartment_id=ctx.tenancy, limit=1
        ),
    }
    missing = 0
    for name, fn in probes.items():
        try:
            fn()
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {short_err(e)}")
            missing += 1
    if not missing:
        print("\nAll permissions OK. Ready.")
        return 0
    print("\nMissing permissions.", *setup_steps(markdown=False), sep="\n")
    return 3


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="OCI Always Free usage report (no LLM).")
    ap.add_argument("--check", action="store_true", help="preflight: auth + permission probe")
    ap.add_argument(
        "--format",
        choices=["markdown", "mono", "plain", "json", "table"],
        default=setting("REPORT_FORMAT", "markdown"),
    )
    ap.add_argument("--lang", help="report language (overrides REPORT_LANG)")
    ap.add_argument("--list-locales", action="store_true", help="list available languages")
    ap.add_argument("--dump-locale", action="store_true", help="print the English strings as a locale template")
    ap.add_argument("--check-locale", metavar="CODE", help="validate locales/<CODE>.json against English")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    if args.lang:
        _OVERRIDES["REPORT_LANG"] = args.lang
    if args.list_locales:
        print("\n".join(available_locales()))
        return 0
    if args.dump_locale:
        print(json.dumps(EN, ensure_ascii=False, indent=2))
        return 0
    if args.check_locale:
        return check_locale(args.check_locale)
    if args.check:
        return preflight()
    try:
        rep = build_report()
    except Exception as e:
        print(f"OCI authentication failed: {short_err(e)}. Run with --check for setup steps.", file=sys.stderr)
        return 1
    if setting("REPORT_QUIET") == "1" and not rep.alerts and not rep.errors:
        return 0  # empty stdout → Hermes delivers nothing
    print(render(rep, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
