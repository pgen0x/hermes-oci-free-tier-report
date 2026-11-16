#!/usr/bin/env python3
"""OCI Always Free daily usage report — pure OCI API calls, no LLM.

Designed for Hermes cron `--no-agent` jobs: stdout is delivered verbatim to whatever
platform the job targets (Telegram, Discord, Slack, WhatsApp, Signal, Matrix, email, ...).

Usage:
  oci_free_tier_report.py                 # report
  oci_free_tier_report.py --check         # preflight: auth + IAM permission probe + setup hints
  oci_free_tier_report.py --format markdown|mono|plain|json

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
  REPORT_QUIET    1 = print nothing when everything is OK (alerts-only mode)
  REPORT_DEBUG    1 = tracebacks on stderr
  OCI_AUTH        instance_principal (default) | config
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
from dataclasses import asdict, dataclass, field
from pathlib import Path

__version__ = "1.2.0"


def load_conf() -> None:
    """Fill os.environ from <script>.conf (KEY=VALUE); real environment variables take precedence."""
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
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


load_conf()

try:
    import oci
except ImportError:  # pragma: no cover
    print("The `oci` Python SDK is missing. Run install.sh or: pip install oci", file=sys.stderr)
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


# ---------------------------------------------------------------- data model
@dataclass
class Row:
    cells: list[str]  # first cell = label, the rest = values (already formatted)
    status: str
    values: dict = field(default_factory=dict)  # raw numbers for JSON consumers


@dataclass
class Section:
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
    return f"{x:,.{nd}f}"


def short_err(e: Exception) -> str:
    if isinstance(e, oci.exceptions.ServiceError):
        hint = " (missing IAM policy?)" if e.status in (401, 403, 404) else ""
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
        if os.environ.get("OCI_AUTH", "instance_principal") == "config":
            self.cfg = oci.config.from_file(
                os.path.expanduser(os.environ.get("OCI_CONFIG_FILE", "~/.oci/config")),
                os.environ.get("OCI_PROFILE", "DEFAULT"),
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
            rep.alerts.append(f"Instance '{i.display_name}' uses paid shape {i.shape}.")
    rep.sections.append(
        Section(
            "Compute",
            ["Compute", "Used", "Limit"],
            [
                limit_row("A1 OCPU", ctx.a1_live_ocpu, LIMITS["a1_ocpu"]),
                limit_row("A1 RAM (GB)", ctx.a1_live_mem, LIMITS["a1_mem_gb"]),
                limit_row("E2.1.Micro VMs", micro, LIMITS["micro_count"]),
            ],
        )
    )
    sec = rep.sections[-1]
    free_ocpu = LIMITS["a1_ocpu"] - ctx.a1_live_ocpu
    free_mem = LIMITS["a1_mem_gb"] - ctx.a1_live_mem
    if free_ocpu < 0 or free_mem < 0:
        sec.note = (
            f"Running A1 capacity ({num(ctx.a1_live_ocpu)} OCPU / {num(ctx.a1_live_mem)} GB) is above the free "
            "4 OCPU / 24 GB — the extra is billed. Shrink or stop an A1 instance."
        )
    elif free_ocpu == 0 or free_mem == 0:
        sec.note = (
            f"A1 is at the free cap ({num(ctx.a1_live_ocpu)} OCPU / {num(ctx.a1_live_mem)} GB) — still free, "
            "but any new or bigger A1 instance would be billed."
        )
    else:
        sec.note = (
            f"A1 uses {num(ctx.a1_live_ocpu)} of 4 OCPU and {num(ctx.a1_live_mem)} of 24 GB — "
            f"{num(free_ocpu)} OCPU / {num(free_mem)} GB still free to use."
        )
    if micro:
        sec.note += f" {micro} of 2 free E2.1.Micro VMs in use."
    if ctx.a1_live_ocpu > LIMITS["a1_ocpu"] or ctx.a1_live_mem > LIMITS["a1_mem_gb"]:
        rep.alerts.append("Live A1 capacity exceeds 4 OCPU / 24 GB.")
    if micro > LIMITS["micro_count"]:
        rep.alerts.append("More than 2 E2.1.Micro instances.")
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

    title = f"Cost ({rep.currency})" if rep.currency else "Cost"
    sec = Section("Cost", [title, "Amount"])
    for label, amt in (("Yesterday", y_total), ("Month to date", mtd)):
        sec.rows.append(Row([label, num(amt, 2)], "ok" if amt < 0.005 else "over", {"amount": amt}))
    if mtd >= 0.005:
        for svc, amt in sorted(by_svc.items(), key=lambda kv: -kv[1]):
            if amt >= 0.005:
                sec.rows.append(Row([f"· {svc}", num(amt, 2)], "over", {"amount": amt}))
        rep.alerts.append(
            f"You were charged {num(mtd, 2)} {rep.currency} this month — something is outside Always Free!"
        )
        sec.note = "Something billable is running — the rows above show which service is charging you."
    else:
        sec.note = (
            "Billed cost from Oracle's Usage API (data lags a few hours). 0.00 means you are not paying anything."
        )
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
    hours = Section("A1 hours", ["A1 hours", "Used", "Proj.", "Limit"])
    for label, used, key, live in (
        ("OCPU-h", a1_ocpu_h, "a1_ocpu_hours", ctx.a1_live_ocpu),
        ("GB-h", a1_gb_h, "a1_gb_hours", ctx.a1_live_mem),
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
            rep.alerts.append(f"A1 {label} projected to exceed the monthly free quota.")
    proj_ocpu = a1_ocpu_h + ctx.a1_live_ocpu * hours_left
    max_ocpu = LIMITS["a1_ocpu"] * (next_month - month_start).days * 24
    hours.note = (
        f"Free monthly quota: 3,000 OCPU-h + 18,000 GB-h. Used so far {num(a1_ocpu_h)} OCPU-h; at the current size "
        f"the month ends at ~{num(proj_ocpu)}. A 4 OCPU / 24 GB A1 running 24/7 needs at most "
        f"{num(max_ocpu)} OCPU-h this month, so it never runs over. Resets on the 1st."
    )
    # place right after Compute when present
    idx = next((n + 1 for n, s in enumerate(rep.sections) if s.title == "Compute"), len(rep.sections))
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
            "Storage",
            ["Storage", "Used", "Limit"],
            [
                limit_row("Block+boot (GB)", vol_gb, LIMITS["block_gb"]),
                limit_row("Volume backups", backups, LIMITS["backups"]),
                limit_row("Object (GB)", obj / GB, LIMITS["object_gb"], nd=2),
            ],
        )
    )
    sec = rep.sections[-1]
    left = LIMITS["block_gb"] - vol_gb
    if left < 0:
        sec.note = f"Volumes total {vol_gb} GB, {-left} GB over the free 200 GB — delete or shrink a volume."
    elif left == 0:
        sec.note = (
            "Boot + block volumes use the full free 200 GB — don't create new volumes or enlarge the boot volume."
        )
    else:
        sec.note = f"{left} GB of the free 200 GB block storage still available."
    if backups > LIMITS["backups"]:
        sec.note += f"{backups} volume backups — only 5 are free, delete the oldest. "
    if obj / GB > LIMITS["object_gb"]:
        sec.note += f"Object Storage holds {num(obj / GB, 2)} GB — only 20 GB is free. "
    sec.note = sec.note.strip()
    if vol_gb > LIMITS["block_gb"] or backups > LIMITS["backups"]:
        rep.alerts.append("Block storage or backups exceed the Always Free limit.")
    if obj / GB > LIMITS["object_gb"]:
        rep.alerts.append("Object Storage exceeds 20 GB.")


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
            "Network",
            ["Network", "Used", "Limit"],
            [limit_row("Egress MTD (GB)", egress / GB, LIMITS["egress_gb"], nd=2)],
        )
    )
    rep.sections[-1].note = (
        f"Outbound traffic this month: {num(egress / GB, 2)} GB of the free 10 TB "
        f"({num(100 * egress / GB / LIMITS['egress_gb'], 3)}%). Counted from VNIC metrics, so slightly overstated."
    )
    if egress / GB > LIMITS["egress_gb"]:
        rep.alerts.append("Outbound data transfer exceeds 10 TB this month.")


def collect_idle(ctx: Ctx, rep: Report, running: list) -> None:
    mon = ctx.client(oci.monitoring.MonitoringClient)
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    stats: dict[str, dict[str, float]] = {}
    for metric in ("CpuUtilization", "MemoryUtilization"):
        for s in _metrics(mon, ctx, "oci_computeagent", f"{metric}[1h].mean()", now - dt.timedelta(days=7), now):
            stats.setdefault(s.dimensions.get("resourceId"), {})[metric] = p95(
                p.value for p in s.aggregated_datapoints
            )
    sec = Section("Idle", ["Idle 7d p95", "CPU %", "RAM %"])
    fmt = lambda v: "n/a" if math.isnan(v) else num(v, 1)  # noqa: E731
    notes = [
        "Oracle may reclaim an Always Free VM (Free Tier accounts) when its 7-day p95 CPU is under 20% "
        "and, on A1, RAM is also under 20%."
    ]
    for i in running:
        st = stats.get(i.id, {})
        cpu = st.get("CpuUtilization", math.nan)
        mem = st.get("MemoryUtilization", math.nan)
        is_a1 = i.shape == "VM.Standard.A1.Flex"
        thr = LIMITS["idle_pct"]
        low = lambda v: "below" if v < thr else "above"  # noqa: E731
        if math.isnan(cpu):
            status = "info"  # no Oracle Cloud Agent metrics
            notes.append(f"{i.display_name}: no metrics — enable the Oracle Cloud Agent monitoring plugin.")
        elif cpu < thr and (not is_a1 or (not math.isnan(mem) and mem < thr)):
            status = "high"
            rep.alerts.append(
                f"'{i.display_name}' is idle by Oracle's rule (7-day p95 < 20%) — "
                "Free Tier accounts may get it reclaimed."
            )
            notes.append(
                f"{i.display_name}: CPU {fmt(cpu)}% and RAM {fmt(mem)}% are both under 20% → at risk of reclaim."
            )
        else:
            status = "ok"
            why = (
                f"CPU {fmt(cpu)}% ({low(cpu)} 20%)"
                + (f", RAM {fmt(mem)}% ({low(mem)} 20%)" if is_a1 and not math.isnan(mem) else "")
            )
            notes.append(f"{i.display_name}: {why} → safe.")
        sec.rows.append(
            Row([i.display_name, fmt(cpu), fmt(mem)], status, {"cpu_p95": cpu, "mem_p95": mem, "id": i.id})
        )
    if not running:
        sec.rows.append(Row(["no running VM", "", ""], "info"))
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
        "Other",
        ["Other", "Used", "Limit"],
        [
            limit_row("Load balancers", lbs, LIMITS["lb"]),
            limit_row("Network LBs", nlbs, LIMITS["nlb"]),
            limit_row("Autonomous DBs", adb_free, LIMITS["adb"]),
        ],
    )
    if adb_paid:
        sec.rows.append(limit_row("Paid ADBs", adb_paid, 0))
        rep.alerts.append(f"{adb_paid} Autonomous Database(s) are not Always Free.")
    used = [f"{n} {what}" for n, what in ((lbs, "load balancer"), (nlbs, "network LB"), (adb_free, "free Autonomous DB")) if n]
    sec.note = (
        "Free: 1 load balancer (10 Mbps), 1 network LB, 2 Autonomous DBs. "
        + (f"In use: {', '.join(used)}." if used else "None in use.")
    )
    rep.sections.append(sec)
    if lbs > LIMITS["lb"] or nlbs > LIMITS["nlb"] or adb_free > LIMITS["adb"]:
        rep.alerts.append("Load balancer / NLB / Autonomous DB count exceeds the free limit.")


def build_report() -> Report:
    tzname = os.environ.get("REPORT_TZ")
    if tzname:
        from zoneinfo import ZoneInfo

        now = dt.datetime.now(ZoneInfo(tzname))
    else:
        now = dt.datetime.now().astimezone()
    rep = Report(generated_at=now.isoformat(timespec="minutes"), timezone=now.tzname() or "")
    ctx = Ctx()  # auth failure propagates → non-zero exit → Hermes failure alert
    rep.region = ctx.region
    running: list = []
    steps = [
        ("Compute", lambda: running.extend(collect_compute(ctx, rep))),
        ("Cost", lambda: collect_cost(ctx, rep)),
        ("Storage", lambda: collect_storage(ctx, rep)),
        ("Network", lambda: collect_network(ctx, rep)),
        ("Idle", lambda: collect_idle(ctx, rep, running)),
        ("Other", lambda: collect_other(ctx, rep)),
    ]
    auth_failures = 0
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            rep.errors.append(f"{name}: {short_err(e)}")
            if isinstance(e, oci.exceptions.ServiceError) and e.status in (401, 403, 404):
                auth_failures += 1
            if os.environ.get("REPORT_DEBUG"):
                traceback.print_exc(file=sys.stderr)
    rep.setup_needed = auth_failures == len(steps)
    return rep


# ---------------------------------------------------------------- renderers
def _icons() -> dict:
    return STATUS_ASCII if os.environ.get("REPORT_ASCII") == "1" else STATUS_EMOJI


def _header(rep: Report, bold: bool) -> list[str]:
    icons = _icons()
    b = (lambda s: f"**{s}**") if bold else (lambda s: s)
    when = dt.datetime.fromisoformat(rep.generated_at)
    out = [b(f"OCI Always Free · {when:%a %d %b %Y}"), f"{rep.region} · {when:%H:%M} {rep.timezone}", ""]
    if rep.alerts:
        out.append(f"{icons['over']} {b(f'{len(rep.alerts)} alert(s)')}")
        out += [f"• {a}" for a in rep.alerts]
    elif rep.errors:
        out.append(f"{icons['error']} Some checks failed — status incomplete")
    else:
        out.append(f"{icons['ok']} {b('All within Always Free — cost 0')}")
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
    return ["", "**Notes**" if bold else "Notes"] + out if out else []


def _footer(rep: Report, bold: bool = True) -> list[str]:
    icons = _icons()
    out = _notes(rep, bold)
    if rep.errors:
        out += [""] + [f"{icons['error']} {e}" for e in rep.errors]
    legend = "  ".join(
        f"{icons[k]} {t}" for k, t in (("ok", "<70%"), ("warn", "70-90%"), ("high", "≥90%"), ("over", "over"))
    )
    return out + ["", legend]


def render_markdown(rep: Report) -> str:
    """GFM pipe tables — Hermes converts them per platform (Telegram: native rich table)."""
    icons = _icons()
    out = _header(rep, bold=True)
    for sec in rep.sections:
        cols = sec.columns + ["Status"]
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
    if os.environ.get("OCI_AUTH", "instance_principal") == "config":
        stmts = [
            "Allow group <your group> to read usage-report in tenancy",
            "Allow group <your group> to read all-resources in tenancy",
        ]
        intro = ["Grant the group of the API-key user these policy statements (root compartment):"]
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
        "One-time setup in the OCI Console (free, read-only), as a tenancy administrator:",
        "",
        f"1. {b('Identity & Security → Domains → Default → Dynamic groups → Create dynamic group')}",
        "   Name: oci-free-tier-report",
        "   Matching rule:",
        *block([rule]),
        "   (Older tenancies without identity domains: Identity → Dynamic Groups.)",
        "",
        f"2. {b('Identity & Security → Policies →')} pick the {b('root')} compartment {b('→ Create policy')}",
        "   Name: oci-free-tier-report-read — statements:",
        *block(stmts),
        "",
        "3. Wait 1–5 minutes. The next scheduled report fills in automatically.",
    ]


def render_setup(rep: Report, markdown: bool) -> str:
    icons = _icons()
    when = dt.datetime.fromisoformat(rep.generated_at)
    b = (lambda t: f"**{t}**") if markdown else (lambda t: t)
    out = [
        b(f"OCI Always Free · {when:%a %d %b %Y}"),
        f"{rep.region} · {when:%H:%M} {rep.timezone}",
        "",
        f"{icons['error']} {b('Oracle permissions are not set up yet')} — this VM can sign in to OCI but is not "
        "allowed to read usage, so no checks could run.",
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
    print(f"oci-free-tier-report {__version__} — preflight\n")
    auth = os.environ.get("OCI_AUTH", "instance_principal")
    instance_id = _imds("instance/id")
    print(f"auth mode    : {auth}")
    print(f"format       : {os.environ.get('REPORT_FORMAT', 'markdown')}")
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
        default=os.environ.get("REPORT_FORMAT", "markdown"),
    )
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    if args.check:
        return preflight()
    try:
        rep = build_report()
    except Exception as e:
        print(f"OCI authentication failed: {short_err(e)}. Run with --check for setup steps.", file=sys.stderr)
        return 1
    if os.environ.get("REPORT_QUIET") == "1" and not rep.alerts and not rep.errors:
        return 0  # empty stdout → Hermes delivers nothing
    print(render(rep, args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
