#!/usr/bin/env python3
"""OCI Always Free daily usage report — pure OCI API calls, no LLM.

Designed for Hermes cron `--no-agent` jobs: stdout is delivered verbatim to whatever
platform the job targets (Telegram, Discord, WhatsApp, Slack, Signal, Matrix, email, ...).

Usage:
  oci_free_tier_report.py            # report (format from REPORT_FORMAT, default "table")
  oci_free_tier_report.py --check    # preflight: auth + IAM permission probe + setup hints
  oci_free_tier_report.py --format json|table|plain

Environment:
  OCI_AUTH        instance_principal (default) | config
  OCI_CONFIG_FILE path to OCI config when OCI_AUTH=config (default ~/.oci/config)
  OCI_PROFILE     profile in that config (default DEFAULT)
  REPORT_FORMAT   table (default; monospace table in a code block — renders on every chat app)
                  plain (same table without code fences — SMS / IRC / email)
                  json  (machine-readable)
  REPORT_ASCII    1 = status words (OK/WARN/HIGH/OVER) instead of emoji
  REPORT_TZ       IANA timezone for the header (default: system local time)
  REPORT_QUIET    1 = print nothing when everything is OK (alerts-only mode)
  REPORT_DEBUG    1 = tracebacks on stderr

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

try:
    import oci
except ImportError:  # pragma: no cover
    print("The `oci` Python SDK is missing. Run install.sh or: pip install oci", file=sys.stderr)
    sys.exit(2)

__version__ = "1.0.0"

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

# Table geometry: label + used + limit = 33 monospace chars, + status → fits a phone screen.
W_LABEL, W_USED, W_LIMIT = 15, 8, 8


# ---------------------------------------------------------------- data model
@dataclass
class Row:
    label: str
    used: float | None
    limit: float | None
    status: str
    used_s: str = ""
    limit_s: str = ""


@dataclass
class Section:
    title: str
    col_used: str = "USED"
    col_limit: str = "LIMIT"
    rows: list[Row] = field(default_factory=list)


@dataclass
class Report:
    generated_at: str = ""
    timezone: str = ""
    region: str = ""
    currency: str = ""
    sections: list[Section] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


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


def add_limit_row(sec: Section, label: str, used: float, limit: float, nd: int = 0) -> None:
    sec.rows.append(Row(label, used, limit, level(used, limit), num(used, nd), num(limit, 0)))


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
    sec = Section("COMPUTE")
    rep.sections.append(sec)
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
    add_limit_row(sec, "A1 OCPU live", ctx.a1_live_ocpu, LIMITS["a1_ocpu"])
    add_limit_row(sec, "A1 RAM GB live", ctx.a1_live_mem, LIMITS["a1_mem_gb"])
    add_limit_row(sec, "E2.1.Micro VMs", micro, LIMITS["micro_count"])
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

    sec = Section(f"COST {rep.currency}".strip(), "AMOUNT", "FREE")
    rep.sections.insert(0, sec)
    for label, amt in (("Yesterday", y_total), ("Month to date", mtd)):
        sec.rows.append(Row(label, amt, 0, "ok" if amt < 0.005 else "over", num(amt, 2), "0.00"))
    if mtd >= 0.005:
        for svc, amt in sorted(by_svc.items(), key=lambda kv: -kv[1]):
            if amt >= 0.005:
                sec.rows.append(Row(f" {svc}", amt, 0, "over", num(amt, 2), ""))
        rep.alerts.append(
            f"You were charged {num(mtd, 2)} {rep.currency} this month — something is outside Always Free!"
        )

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
    target = next((s for s in rep.sections if s.title == "COMPUTE"), sec)
    for label, used, key, live in (
        ("A1 OCPU-h used", a1_ocpu_h, "a1_ocpu_hours", ctx.a1_live_ocpu),
        ("A1 GB-h used", a1_gb_h, "a1_gb_hours", ctx.a1_live_mem),
    ):
        target.rows.append(Row(label, used, LIMITS[key], "info", num(used), num(LIMITS[key])))
        proj = used + live * hours_left
        add_limit_row(target, label.replace("used", "proj."), proj, LIMITS[key])
        if proj > LIMITS[key]:
            rep.alerts.append(f"{label.replace(' used', '')} projected to exceed the monthly free quota.")


def collect_storage(ctx: Ctx, rep: Report) -> None:
    sec = Section("STORAGE")
    rep.sections.append(sec)
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
    add_limit_row(sec, "Block+boot GB", vol_gb, LIMITS["block_gb"])
    add_limit_row(sec, "Volume backups", backups, LIMITS["backups"])
    if vol_gb > LIMITS["block_gb"] or backups > LIMITS["backups"]:
        rep.alerts.append("Block storage or backups exceed the Always Free limit.")

    osc = ctx.client(oci.object_storage.ObjectStorageClient)
    ns = osc.get_namespace().data
    obj = 0
    for comp in ctx.compartments:
        for b in ctx.list_all(osc.list_buckets, ns, comp):
            obj += osc.get_bucket(ns, b.name, fields=["approximateSize"]).data.approximate_size or 0
    add_limit_row(sec, "Object Stor. GB", obj / GB, LIMITS["object_gb"], nd=2)
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
    sec = Section("NETWORK")
    rep.sections.append(sec)
    mon = ctx.client(oci.monitoring.MonitoringClient)
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    # VNIC egress, month to date (conservative: also counts intra-VCN traffic)
    egress = sum(
        p.value or 0
        for s in _metrics(mon, ctx, "oci_vcn", "VnicToNetworkBytes[1h].sum()", now.replace(day=1, hour=0), now)
        for p in s.aggregated_datapoints
    )
    add_limit_row(sec, "Egress GB (MTD)", egress / GB, LIMITS["egress_gb"], nd=2)
    if egress / GB > LIMITS["egress_gb"]:
        rep.alerts.append("Outbound data transfer exceeds 10 TB this month.")


def collect_idle(ctx: Ctx, rep: Report, running: list) -> None:
    sec = Section("IDLE 7d p95", "CPU%", "RAM%")
    rep.sections.append(sec)
    mon = ctx.client(oci.monitoring.MonitoringClient)
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    stats: dict[str, dict[str, float]] = {}
    for metric in ("CpuUtilization", "MemoryUtilization"):
        for s in _metrics(mon, ctx, "oci_computeagent", f"{metric}[1h].mean()", now - dt.timedelta(days=7), now):
            stats.setdefault(s.dimensions.get("resourceId"), {})[metric] = p95(
                p.value for p in s.aggregated_datapoints
            )
    fmt = lambda v: "n/a" if math.isnan(v) else num(v, 1)  # noqa: E731
    for i in running:
        st = stats.get(i.id, {})
        cpu = st.get("CpuUtilization", math.nan)
        mem = st.get("MemoryUtilization", math.nan)
        is_a1 = i.shape == "VM.Standard.A1.Flex"
        if math.isnan(cpu):
            status = "info"  # no Oracle Cloud Agent metrics
        elif cpu < LIMITS["idle_pct"] and (not is_a1 or (not math.isnan(mem) and mem < LIMITS["idle_pct"])):
            status = "high"
            rep.alerts.append(
                f"'{i.display_name}' is idle by Oracle's rule (7-day p95 < 20%) — "
                "Free Tier accounts may get it reclaimed."
            )
        else:
            status = "ok"
        sec.rows.append(Row(i.display_name, cpu, mem, status, fmt(cpu), fmt(mem)))
    if not running:
        sec.rows.append(Row("no running VM", None, None, "info"))


def collect_other(ctx: Ctx, rep: Report) -> None:
    sec = Section("OTHER")
    rep.sections.append(sec)
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
    add_limit_row(sec, "Load balancers", lbs, LIMITS["lb"])
    add_limit_row(sec, "Network LBs", nlbs, LIMITS["nlb"])
    add_limit_row(sec, "Autonomous DBs", adb_free, LIMITS["adb"])
    if adb_paid:
        sec.rows.append(Row("Paid ADBs", adb_paid, 0, "over", str(adb_paid), "0"))
        rep.alerts.append(f"{adb_paid} Autonomous Database(s) are not Always Free.")
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
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            rep.errors.append(f"{name}: {short_err(e)}")
            if os.environ.get("REPORT_DEBUG"):
                traceback.print_exc(file=sys.stderr)
    return rep


# ---------------------------------------------------------------- renderers
def _fit(s: str, w: int, left: bool = True) -> str:
    s = s if len(s) <= w else s[: w - 1] + "…"
    return s.ljust(w) if left else s.rjust(w)


def render_table(rep: Report, fenced: bool = True) -> str:
    icons = STATUS_ASCII if os.environ.get("REPORT_ASCII") == "1" else STATUS_EMOJI
    when = dt.datetime.fromisoformat(rep.generated_at)
    out = [f"**OCI Always Free · {when:%a %d %b %Y}**", f"{rep.region} · {when:%H:%M} {rep.timezone}", ""]
    if rep.alerts:
        out.append(f"{icons['over']} **{len(rep.alerts)} alert(s)**")
        out += [f"• {a}" for a in rep.alerts]
    elif rep.errors:
        out.append(f"{icons['error']} Some checks failed — status incomplete")
    else:
        out.append(f"{icons['ok']} **All within Always Free — cost 0**")
    out.append("")

    tbl: list[str] = []
    for sec in rep.sections:
        if tbl:
            tbl.append("")
        tbl.append(
            f"{_fit(sec.title, W_LABEL)} {_fit(sec.col_used, W_USED, False)} {_fit(sec.col_limit, W_LIMIT, False)}"
        )
        for r in sec.rows:
            tbl.append(
                f"{_fit(r.label, W_LABEL)} {_fit(r.used_s, W_USED, False)} "
                f"{_fit(r.limit_s, W_LIMIT, False)} {icons[r.status]}".rstrip()
            )
    out += (["```"] + tbl + ["```"]) if fenced else tbl
    if rep.errors:
        out.append("")
        out += [f"{icons['error']} {e}" for e in rep.errors]
    out += ["", "  ".join(f"{icons[k]} {t}" for k, t in (("ok", "<70%"), ("warn", "70-90%"), ("high", "≥90%"), ("over", "over")))]
    return "\n".join(out)


def render_json(rep: Report) -> str:
    return json.dumps(asdict(rep), indent=2, default=str)


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
    if auth == "instance_principal":
        rule = f"instance.id = '{instance_id}'" if instance_id else "instance.compartment.id = '<compartment OCID>'"
        print(
            f"""
Missing permissions. One-time setup in the OCI Console (free, read-only):

1. Identity & Security → Domains → Default → Dynamic groups → Create dynamic group
     Name : oci-free-tier-report
     Rule : {rule}
   (Older tenancies without identity domains: Identity → Dynamic Groups.)

2. Identity & Security → Policies → select the ROOT compartment → Create policy
     Name : oci-free-tier-report-read
     Statements:
       Allow dynamic-group oci-free-tier-report to read usage-report in tenancy
       Allow dynamic-group oci-free-tier-report to read all-resources in tenancy

3. Wait 1-5 minutes for IAM to propagate, then re-run this check."""
        )
    else:
        print(
            "\nGrant the API-key user's group:\n"
            "  Allow group <group> to read usage-report in tenancy\n"
            "  Allow group <group> to read all-resources in tenancy"
        )
    return 3


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="OCI Always Free usage report (no LLM).")
    ap.add_argument("--check", action="store_true", help="preflight: auth + permission probe")
    ap.add_argument(
        "--format", choices=["table", "plain", "json"], default=os.environ.get("REPORT_FORMAT", "table")
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
    print(render_json(rep) if args.format == "json" else render_table(rep, fenced=args.format == "table"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
