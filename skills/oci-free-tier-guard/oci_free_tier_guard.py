#!/usr/bin/env python3
"""OCI Free Tier guard — optional companion of oci-free-tier-report (no LLM).

Runs as a Hermes --no-agent cron (hourly by default). It projects Ampere A1 OCPU-hours and GB-hours to the end of the
month with the same numbers as the daily report and, only when that projection passes the free quota, shrinks or
stops the A1 instances it is allowed to touch.

  GUARD_MODE       off | dry-run (default: only says what it would do) | downscale | stop
  GUARD_INSTANCES  self (default: the VM this runs on) | all | comma-separated OCIDs or display names
  GUARD_ALLOW_SELF 1 lets stop mode stop the VM that runs Hermes (it stays off until started in the Console)
  GUARD_MIN        smallest size downscale may set, <ocpu>/<gb> (default 1/6)

Prints nothing while the projection is inside the quota, so Hermes delivers nothing. Settings live in
oci_free_tier_guard.conf next to this script; OCI auth, language and timezone come from the report's settings.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.2.0"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import oci_free_tier_report as R  # installed next to this script by the report skill
except ImportError:
    print(
        "oci_free_tier_report.py was not found next to this script. Install the oci-free-tier-report skill "
        "and run its install.sh first, then re-run this skill's install.sh.",
        file=sys.stderr,
    )
    sys.exit(2)
oci = R.oci

CONF_PATH = HERE / "oci_free_tier_guard.conf"
STATE_PATH = HERE / "oci_free_tier_guard.state.json"
MODES = ("off", "dry-run", "downscale", "stop")
A1 = "VM.Standard.A1.Flex"
MEM_PER_OCPU = (1, 64)  # A1 Flex: 1-64 GB of memory per OCPU

_CONF: dict[str, str] = {}


def load_conf() -> None:
    try:
        lines = CONF_PATH.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            _CONF[key.strip()] = val.strip().strip("'\"")


def setting(key: str, default: str | None = None) -> str | None:
    """Environment variable > guard conf > report conf > default."""
    val = os.environ.get(key)
    if val is not None:
        return val
    return _CONF[key] if key in _CONF else R.setting(key, default)


# ---------------------------------------------------------------- text
TEXT = {
    "en": {
        "title": "🛡️ OCI Free Tier guard",
        "mode": "mode: {mode}",
        "over": "A1 is on track to pass the {account} quota this month: {proj_ocpu} / {quota_ocpu} OCPU-h and "
        "{proj_gb} / {quota_gb} GB-h by month end.",
        "allowed": "To stay free, A1 can run at most {ocpu} OCPU / {mem} GB until the 1st.",
        "would": "Dry-run, nothing was changed. It would:",
        "did": "Done:",
        "resize": "resize {name}: {o1} OCPU / {m1} GB → {o2} OCPU / {m2} GB",
        "stop": "stop {name} ({o1} OCPU / {m1} GB)",
        "self_resize": "   ↳ this VM: it reboots, Hermes comes back by itself",
        "self_stop": "   ↳ this VM: Hermes stays offline until you start it in the OCI Console",
        "skip_self": "{name} is the VM running Hermes, so stop mode skips it (GUARD_ALLOW_SELF=1 allows it).",
        "short": "Still {ocpu} OCPU / {mem} GB over after this ({why}). The rest of the month will be billed unless "
        "you stop or shrink something yourself.",
        "why_floor": "the minimum size GUARD_MIN",
        "why_self": "the VM running Hermes is skipped",
        "why_unmanaged": "{ocpu} OCPU / {mem} GB run on instances outside GUARD_INSTANCES",
        "enable": "To let the guard act, set GUARD_MODE=downscale (or stop) in {conf}, or ask your agent.",
        "unknown": "The account type could not be read, so the guard only warns and does not change anything.",
        "failed": "failed {name}: {err}",
        "perm": "OCI refused the change: the VM has no permission yet. Add this statement to the "
        "oci-free-tier-report policy (Identity & Security → Policies):",
        "followup": "Guard follow-up: {name} is now {ocpu} OCPU / {mem} GB ({state}).",
        "error": "The guard could not read OCI usage: {err}",
        "inside": "Inside the {account} quota: month-end projection {proj_ocpu} / {quota_ocpu} OCPU-h and "
        "{proj_gb} / {quota_gb} GB-h. Nothing to do.",
    },
    "id": {
        "title": "🛡️ OCI Free Tier guard",
        "mode": "mode: {mode}",
        "over": "A1 diproyeksikan lewat kuota {account} bulan ini: {proj_ocpu} / {quota_ocpu} OCPU-h dan "
        "{proj_gb} / {quota_gb} GB-h di akhir bulan.",
        "allowed": "Supaya tetap gratis, A1 maksimal {ocpu} OCPU / {mem} GB sampai tanggal 1.",
        "would": "Dry-run, belum ada yang diubah. Yang akan dilakukan:",
        "did": "Sudah dilakukan:",
        "resize": "ubah {name}: {o1} OCPU / {m1} GB → {o2} OCPU / {m2} GB",
        "stop": "stop {name} ({o1} OCPU / {m1} GB)",
        "self_resize": "   ↳ VM ini: akan reboot, Hermes nyala lagi sendiri",
        "self_stop": "   ↳ VM ini: Hermes mati sampai kamu start lagi di OCI Console",
        "skip_self": "{name} adalah VM yang menjalankan Hermes, jadi mode stop melewatinya (GUARD_ALLOW_SELF=1 "
        "untuk mengizinkan).",
        "short": "Setelah ini masih lewat {ocpu} OCPU / {mem} GB ({why}). Sisa bulan ini akan ditagih kecuali kamu "
        "stop atau kecilkan sesuatu sendiri.",
        "why_floor": "batas minimum GUARD_MIN",
        "why_self": "VM yang menjalankan Hermes dilewati",
        "why_unmanaged": "{ocpu} OCPU / {mem} GB jalan di instance di luar GUARD_INSTANCES",
        "enable": "Supaya guard bertindak, set GUARD_MODE=downscale (atau stop) di {conf}, atau minta ke agent-mu.",
        "unknown": "Jenis akun tidak terbaca, jadi guard hanya memberi peringatan dan tidak mengubah apa pun.",
        "failed": "gagal {name}: {err}",
        "perm": "OCI menolak perubahan: VM belum punya izin. Tambahkan statement ini ke policy "
        "oci-free-tier-report (Identity & Security → Policies):",
        "followup": "Lanjutan guard: {name} sekarang {ocpu} OCPU / {mem} GB ({state}).",
        "error": "Guard tidak bisa membaca pemakaian OCI: {err}",
        "inside": "Masih di dalam kuota {account}: proyeksi akhir bulan {proj_ocpu} / {quota_ocpu} OCPU-h dan "
        "{proj_gb} / {quota_gb} GB-h. Tidak ada yang perlu dilakukan.",
    },
}
_LANG = "en"
FORCE_DRY = False  # --dry-run: plan with the configured mode, change nothing


def G(key: str, **kw) -> str:
    return TEXT.get(_LANG, TEXT["en"]).get(key, TEXT["en"][key]).format(**kw)


def guard_min() -> dict:
    """GUARD_MIN=<ocpu>/<gb>: downscale never shrinks an instance below this (default 1/6, enough for Hermes)."""
    try:
        o, m = (float(x) for x in (setting("GUARD_MIN", "1/6") or "1/6").split("/"))
    except ValueError:
        o, m = 1.0, 6.0
    return {"min_ocpu": max(1.0, o), "min_mem": max(1.0, m)}


def policy_statement(mode: str) -> str:
    dg = setting("GUARD_DYNAMIC_GROUP", "oci-free-tier-report")
    perm = "INSTANCE_POWER_ACTIONS" if mode == "stop" else "INSTANCE_UPDATE"
    return f"Allow dynamic-group {dg} to use instances in tenancy where request.permission = '{perm}'"


# ---------------------------------------------------------------- planning (pure, unit-testable)
@dataclass
class Inst:
    id: str
    name: str
    ocpu: float
    mem: float
    is_self: bool = False


@dataclass
class Action:
    kind: str  # resize | stop
    inst: Inst
    to_ocpu: float = 0
    to_mem: float = 0


@dataclass
class Plan:
    proj_ocpu: float
    proj_gb: float
    allowed_ocpu: int
    allowed_mem: int
    actions: list[Action] = field(default_factory=list)
    short_ocpu: float = 0
    short_mem: float = 0
    skipped_self: Inst | None = None


def make_plan(
    *,
    used_ocpu_h: float,
    used_gb_h: float,
    live_ocpu: float,
    live_mem: float,
    hours_left: float,
    quota_ocpu_h: float,
    quota_gb_h: float,
    cap_ocpu: float,
    cap_mem: float,
    managed: list[Inst],
    mode: str,
    allow_self: bool = False,
    min_ocpu: float = 1,
    min_mem: float = 6,
) -> Plan | None:
    """None when the month-end projection stays inside both quotas. Otherwise the smallest changes, within the
    managed instances, that bring the live A1 rate down to what the remaining quota can pay for."""
    if hours_left <= 0:
        return None  # month rollover: the quota just reset
    proj_ocpu = used_ocpu_h + live_ocpu * hours_left
    proj_gb = used_gb_h + live_mem * hours_left
    if proj_ocpu <= quota_ocpu_h and proj_gb <= quota_gb_h:
        return None
    allowed_ocpu = int(min(cap_ocpu, max(0.0, math.floor((quota_ocpu_h - used_ocpu_h) / hours_left))))
    allowed_mem = int(min(cap_mem, max(0.0, math.floor((quota_gb_h - used_gb_h) / hours_left))))
    plan = Plan(proj_ocpu, proj_gb, allowed_ocpu, allowed_mem)
    need_o = max(0.0, live_ocpu - allowed_ocpu)
    need_m = max(0.0, live_mem - allowed_mem)
    # other instances first, the VM running Hermes last; biggest first
    order = sorted(managed, key=lambda i: (i.is_self, -i.ocpu, -i.mem))
    if mode == "stop":
        for inst in order:
            if need_o <= 0 and need_m <= 0:
                break
            if inst.is_self and not allow_self:
                plan.skipped_self = inst
                continue
            plan.actions.append(Action("stop", inst))
            need_o -= inst.ocpu
            need_m -= inst.mem
        plan.short_ocpu, plan.short_mem = max(0.0, need_o), max(0.0, need_m)
        return plan
    # downscale pass 1: fewer OCPUs, keeping each instance's GB-per-OCPU ratio (memory shrinks along)
    target = {i.id: [i.ocpu, i.mem] for i in order}
    for inst in order:
        if need_o <= 0:
            break
        t = target[inst.id]
        t[0] = max(min(min_ocpu, inst.ocpu), inst.ocpu - math.ceil(need_o))
        t[1] = max(min(min_mem, inst.mem), float(math.floor(t[0] * inst.mem / inst.ocpu)))
        need_o -= inst.ocpu - t[0]
        need_m -= inst.mem - t[1]
    # pass 2: memory still over → trim it, down to the 1 GB-per-OCPU minimum
    for inst in order:
        if need_m <= 0:
            break
        t = target[inst.id]
        new_m = max(t[0] * MEM_PER_OCPU[0], min(min_mem, inst.mem), t[1] - math.ceil(need_m))
        need_m -= t[1] - new_m
        t[1] = new_m
    for inst in order:
        to_o, to_m = target[inst.id]
        to_m = min(to_m, to_o * MEM_PER_OCPU[1])
        if (to_o, to_m) != (inst.ocpu, inst.mem):
            plan.actions.append(Action("resize", inst, to_o, to_m))
    plan.short_ocpu, plan.short_mem = max(0.0, need_o), max(0.0, need_m)
    return plan


# ---------------------------------------------------------------- state
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state))
    except OSError as e:
        print(f"cannot write {STATE_PATH}: {e}", file=sys.stderr)


def once_a_day(state: dict, text: str) -> bool:
    """True if this exact message was not sent yet today (dry-run and warnings repeat at most daily)."""
    sig = hashlib.sha1(text.encode()).hexdigest()[:16]
    today = dt.date.today().isoformat()
    if state.get("sig") == sig and state.get("day") == today:
        return False
    state.update(sig=sig, day=today)
    return True


# ---------------------------------------------------------------- OCI
def hours_left_in_month() -> float:
    """Same window as the report's collect_cost(): usage up to today 00:00 UTC, live rate from then on."""
    today = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = (today - dt.timedelta(days=1)).replace(day=1)
    next_month = (month_start + dt.timedelta(days=32)).replace(day=1)
    return (next_month - today).total_seconds() / 3600


def wanted(inst, spec: str, self_id: str | None) -> bool:
    items = {s.strip() for s in spec.split(",") if s.strip()}
    if "all" in items:
        return True
    if "self" in items and inst.id == self_id:
        return True
    return inst.id in items or inst.display_name in items


def gather():
    """(ctx, managed A1 instances, used OCPU-h, used GB-h) using the report's own collectors."""
    ctx = R.Ctx()
    rep = R.Report()
    running = R.collect_compute(ctx, rep)
    R.collect_cost(ctx, rep)
    hours = next(s for s in rep.sections if s.id == "a1_hours")
    used_ocpu_h = hours.rows[0].values["used"]
    used_gb_h = hours.rows[1].values["used"]
    self_id = R._imds("instance/id")
    spec = setting("GUARD_INSTANCES", "self") or "self"
    managed = [
        Inst(i.id, i.display_name, float(i.shape_config.ocpus or 0), float(i.shape_config.memory_in_gbs or 0),
             i.id == self_id)
        for i in running
        if i.shape == A1 and wanted(i, spec, self_id)
    ]
    return ctx, managed, used_ocpu_h, used_gb_h


def apply(ctx, action: Action) -> None:
    compute = ctx.client(oci.core.ComputeClient)
    if action.kind == "stop":
        compute.instance_action(action.inst.id, "SOFTSTOP")
    else:
        compute.update_instance(
            action.inst.id,
            oci.core.models.UpdateInstanceDetails(
                shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                    ocpus=float(action.to_ocpu), memory_in_gbs=float(action.to_mem)
                )
            ),
        )


def describe(a: Action) -> list[str]:
    n = R.num
    if a.kind == "stop":
        lines = ["• " + G("stop", name=a.inst.name, o1=n(a.inst.ocpu), m1=n(a.inst.mem))]
        return lines + ([G("self_stop")] if a.inst.is_self else [])
    lines = [
        "• " + G("resize", name=a.inst.name, o1=n(a.inst.ocpu), m1=n(a.inst.mem), o2=n(a.to_ocpu), m2=n(a.to_mem))
    ]
    return lines + ([G("self_resize")] if a.inst.is_self else [])


def is_auth_error(e: Exception) -> bool:
    return isinstance(e, oci.exceptions.ServiceError) and e.status in (401, 403, 404)


# ---------------------------------------------------------------- run
def followups(ctx, state: dict) -> list[str]:
    out = []
    pending = state.pop("followup", [])
    if not pending:
        return out
    compute = ctx.client(oci.core.ComputeClient)
    for item in pending:
        try:
            i = compute.get_instance(item["id"]).data
            out.append(
                G(
                    "followup",
                    name=i.display_name,
                    ocpu=R.num(i.shape_config.ocpus or 0),
                    mem=R.num(i.shape_config.memory_in_gbs or 0),
                    state=i.lifecycle_state,
                )
            )
        except Exception as e:  # noqa: BLE001
            out.append(G("failed", name=item.get("name", item["id"]), err=R.short_err(e)))
    return out


def run() -> int:
    mode = (setting("GUARD_MODE", "dry-run") or "dry-run").lower()
    if mode not in MODES:
        print(f"GUARD_MODE={mode} is not one of {', '.join(MODES)}", file=sys.stderr)
        return 2
    if mode == "off":
        return 0
    state = load_state()
    try:
        ctx, managed, used_o, used_g = gather()
    except Exception as e:  # noqa: BLE001
        msg = G("error", err=R.short_err(e))
        if once_a_day(state, msg):
            print(f"**{G('title')}**\n\n{msg}")
        save_state(state)
        return 0
    out = followups(ctx, state)
    cap_o, cap_m, quota_o, quota_g = R.A1_ALLOWANCE.get(ctx.account, R.A1_ALLOWANCE["free"])
    effective = "dry-run" if FORCE_DRY or ctx.account == "unknown" else mode
    plan = make_plan(
        used_ocpu_h=used_o,
        used_gb_h=used_g,
        live_ocpu=ctx.a1_live_ocpu,
        live_mem=ctx.a1_live_mem,
        hours_left=hours_left_in_month(),
        quota_ocpu_h=quota_o,
        quota_gb_h=quota_g,
        cap_ocpu=cap_o,
        cap_mem=cap_m,
        managed=managed,
        mode="stop" if mode == "stop" else "downscale",
        allow_self=setting("GUARD_ALLOW_SELF", "0") == "1",
        **guard_min(),
    )
    n = R.num
    account = R.T("acct." + ("payg" if ctx.account == "payg" else "free"))
    if plan is None:
        if FORCE_DRY:
            hl = max(0.0, hours_left_in_month())
            out.append(G("inside", account=account, quota_ocpu=n(quota_o), quota_gb=n(quota_g),
                         proj_ocpu=n(used_o + ctx.a1_live_ocpu * hl), proj_gb=n(used_g + ctx.a1_live_mem * hl)))
        if out:
            print(f"**{G('title')}**\n\n" + "\n".join(out))
        save_state(state)
        return 0

    body = [
        G(
            "over",
            account=account,
            proj_ocpu=n(plan.proj_ocpu),
            quota_ocpu=n(quota_o),
            proj_gb=n(plan.proj_gb),
            quota_gb=n(quota_g),
        ),
        G("allowed", ocpu=n(plan.allowed_ocpu), mem=n(plan.allowed_mem)),
    ]
    if ctx.account == "unknown":
        body.append(G("unknown"))
    tail = []
    if plan.skipped_self:
        tail.append(G("skip_self", name=plan.skipped_self.name))
    if plan.short_ocpu > 0 or plan.short_mem > 0:
        um_o = ctx.a1_live_ocpu - sum(i.ocpu for i in managed)
        um_m = ctx.a1_live_mem - sum(i.mem for i in managed)
        if plan.skipped_self:
            why = G("why_self")
        elif um_o > 0 or um_m > 0:
            why = G("why_unmanaged", ocpu=n(um_o), mem=n(um_m))
        else:
            why = G("why_floor")
        tail.append(G("short", ocpu=n(plan.short_ocpu), mem=n(plan.short_mem), why=why))

    if effective == "dry-run":
        lines = list(body)
        if plan.actions:
            lines += [G("would")] + [ln for a in plan.actions for ln in describe(a)]
        lines += tail + ([G("enable", conf=CONF_PATH)] if mode == "dry-run" else [])
        text = "\n".join(lines)
        if FORCE_DRY or once_a_day(state, text) or out:
            label = "dry-run" if mode == "dry-run" else f"{mode}, dry-run"
            print(f"**{G('title')}** · {G('mode', mode=label)}\n\n" + "\n".join(out + [text]))
        save_state(state)
        return 0

    done, failed, perm = [], [], False
    for a in plan.actions:
        state.setdefault("followup", []).append({"id": a.inst.id, "name": a.inst.name})
        if a.inst.is_self:  # this VM may reboot or stop right after the call: persist the follow-up first
            save_state(state)
        try:
            apply(ctx, a)
            done += describe(a)
        except Exception as e:  # noqa: BLE001
            perm = perm or is_auth_error(e)
            failed.append("• " + G("failed", name=a.inst.name, err=R.short_err(e)))
            state["followup"] = [f for f in state.get("followup", []) if f["id"] != a.inst.id]
    lines = list(body)
    if done:
        lines += [G("did")] + done
    lines += failed
    if perm:
        lines += [G("perm"), "```", policy_statement(mode), "```"]
    lines += tail
    text = "\n".join(lines)
    if done or once_a_day(state, text) or out:
        print(f"**{G('title')}** · {G('mode', mode=mode)}\n\n" + "\n".join(out + [text]))
    save_state(state)
    return 0


def preflight() -> int:
    mode = (setting("GUARD_MODE", "dry-run") or "dry-run").lower()
    print(f"oci-free-tier-guard {__version__} — preflight\n")
    print(f"mode         : {mode}")
    print(f"instances    : {setting('GUARD_INSTANCES', 'self')}")
    print(f"allow self   : {setting('GUARD_ALLOW_SELF', '0')}")
    print(f"minimum size : {setting('GUARD_MIN', '1/6')} (OCPU/GB, downscale)")
    print(f"report       : {R.__version__} ({Path(R.__file__).resolve()})")
    if mode not in MODES:
        print(f"\nGUARD_MODE must be one of: {', '.join(MODES)}")
        return 1
    try:
        ctx, managed, used_o, used_g = gather()
    except Exception as e:  # noqa: BLE001
        print(f"OCI          : FAILED — {R.short_err(e)}")
        print("Fix the report first: its --check prints the IAM setup.")
        return 1
    _, _, quota_o, quota_g = R.A1_ALLOWANCE.get(ctx.account, R.A1_ALLOWANCE["free"])
    hl = hours_left_in_month()
    print(
        f"account type : {ctx.account} → quota {quota_o:,} OCPU-h / {quota_g:,} GB-h"
        + ("  (unknown: guard only warns)" if ctx.account == "unknown" else "")
    )
    print(
        f"A1 now       : {ctx.a1_live_ocpu:g} OCPU / {ctx.a1_live_mem:g} GB live; used {used_o:,.0f} OCPU-h / "
        f"{used_g:,.0f} GB-h; month-end projection {used_o + ctx.a1_live_ocpu * hl:,.0f} OCPU-h / "
        f"{used_g + ctx.a1_live_mem * hl:,.0f} GB-h"
    )
    print(
        "managed      : "
        + (
            ", ".join(f"{i.name} {i.ocpu:g}/{i.mem:g}{' (this VM)' if i.is_self else ''}" for i in managed)
            or "none running — check GUARD_INSTANCES"
        )
    )
    if mode in ("off", "dry-run"):
        print(f"\nOK. {mode} needs no extra permission. Before switching to downscale or stop, add to the policy:")
        print(f"  {policy_statement('downscale')}")
        print(f"  {policy_statement('stop')}   (stop mode only)")
        return 0
    if mode == "stop":
        print("\nThe stop permission can't be probed without stopping something. The policy needs:")
        print(f"  {policy_statement('stop')}")
        return 0
    probe = next(iter(managed), None)
    if probe is None:
        print("\nNo managed A1 instance is running, nothing to probe.")
        return 0
    try:  # no-op update (same display name): proves INSTANCE_UPDATE without changing anything
        ctx.client(oci.core.ComputeClient).update_instance(
            probe.id, oci.core.models.UpdateInstanceDetails(display_name=probe.name)
        )
    except Exception as e:  # noqa: BLE001
        print(f"\n  ✗ update permission on {probe.name}: {R.short_err(e)}")
        print("\nAdd this statement to the oci-free-tier-report policy (Identity & Security → Policies):")
        print(f"  {policy_statement('downscale')}")
        return 3
    print(f"\n  ✓ update permission on {probe.name}\n\nReady.")
    return 0


def main() -> int:
    global _LANG, FORCE_DRY
    ap = argparse.ArgumentParser(description="OCI Free Tier guard: shrink or stop A1 before it leaves the free quota.")
    ap.add_argument("--check", action="store_true", help="preflight: settings, projection and permission probe")
    ap.add_argument("--dry-run", action="store_true", help="show what the configured mode would do now, change nothing")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    load_conf()
    FORCE_DRY = args.dry_run
    lang = R.set_language(setting("REPORT_LANG", "en") or "en")
    _LANG = lang if lang in TEXT else "en"
    return preflight() if args.check else run()


if __name__ == "__main__":
    sys.exit(main())
