"""Chapter 8 - the cloud side.

Standard-library only:

    python cloud.py fit requirements.json              # eleven jobs x three platforms: native, add-on or you build
    python cloud.py cost fleet.json prices.json        # line items and totals at 10 / 1 000 / 100 000 devices
    python cloud.py cost fleet.json prices.json --devices 300
    python cloud.py fanout fleet.json prices.json      # what one phone costs per month, and where a listener is attached
    python cloud.py rules firebase|aws|mosquitto       # chapter 7's rules table in each platform's dialect

``fit`` is a table read from JSON: for each job the cloud side must do, how
each of the three shapes (a database the device writes to, a managed broker
with managed stores, a broker and a time-series database you run) does it.
``cost`` multiplies chapter 3's record sizes and rates, on chapter 6's node
mix, by each platform's billed units and the prices in ``prices.json``: every
quantity is derived from ``fleet.json`` and printed with its unit, every price
carries its source and whether it was fetched or remembered, and the hours a
self-hosted stack takes are printed as hours, not priced.  ``fanout`` prices
the listening phone, the part of the bill that is not proportional to the
fleet, under three ways of attaching a listener.  ``rules`` writes chapter 7's
least-privilege table as Firebase rules JSON, an AWS IoT policy and a
Mosquitto ACL, so the same five rows can be read in each dialect.  Nothing
here talks to a cloud; the numbers are arithmetic on the inputs.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).parent
DAYS = 30  # one billing month
MINUTES = DAYS * 24 * 60
HOURS = DAYS * 24


def load(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# 1. fit: what each platform does natively, with an add-on, or not at all
# ---------------------------------------------------------------------------

PLATFORMS = ("firebase", "aws", "self")
PLATFORM_NAMES = {
    "firebase": "Firebase RTDB",
    "aws": "AWS IoT Core + stores",
    "self": "self-hosted MQTT + Timescale",
}
LEVELS = ("native", "add-on", "you build")


def fit_rows(spec: dict) -> list[dict]:
    rows = []
    for job in spec["jobs"]:
        row = {"job": job["job"]}
        for p in PLATFORMS:
            level, how = job[p]
            if level not in LEVELS:
                raise ValueError(f"unknown level {level!r} for {job['job']!r}")
            row[p] = level
            row[p + "_how"] = how
        rows.append(row)
    return rows


def fit_counts(rows: list[dict]) -> dict[str, dict[str, int]]:
    return {
        p: {lv: sum(1 for r in rows if r[p] == lv) for lv in LEVELS} for p in PLATFORMS
    }


def print_fit(spec: dict) -> None:
    rows = fit_rows(spec)
    w = max(len(r["job"]) for r in rows)
    print(
        f"{'job':<{w}}  {'Firebase RTDB':<14}  {'AWS IoT Core':<14}  {'self-hosted':<14}"
    )
    for r in rows:
        print(f"{r['job']:<{w}}  {r['firebase']:<14}  {r['aws']:<14}  {r['self']:<14}")
    counts = fit_counts(rows)
    print()
    for p in PLATFORMS:
        c = counts[p]
        print(
            f"{PLATFORM_NAMES[p]:<30} native {c['native']:>2}   add-on {c['add-on']:>2}   you build {c['you build']:>2}"
        )
    print()
    for r in rows:
        print(f"- {r['job']}")
        for p in PLATFORMS:
            print(f"    {PLATFORM_NAMES[p]:<30} {r[p]:<9} {r[p + '_how']}")


# ---------------------------------------------------------------------------
# 2. usage: what a fleet of n devices does in a month, before any price
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    devices: int
    connections: int  # device-side sessions held open (Wi-Fi nodes + gateways)
    gateways: int
    apps: int
    uplink_msgs: float  # messages a month from devices and gateways
    uplink_bytes: float
    status_writes: float  # a month, all devices
    history_rows: float
    event_rows: float
    reported_writes: float
    stored_bytes: float  # steady state: history at retention, events at the sizing horizon, latest rows
    stored_hot_bytes: float  # history kept 30 days (the hot table), plus latest rows
    desired_writes: float  # settings changes a month, from apps
    app_status_deliveries: (
        float  # status messages delivered to listening phones a month
    )
    app_status_bytes: float
    chart_opens: float
    chart_bytes: float  # rows downloaded by chart opens a month
    chart_rows_per_open: int
    peak_app_connections: float
    by_class: list[dict] = field(default_factory=list)


def _per_day(stream: dict) -> float:
    return float(stream["per_day"])


def class_counts(fleet: dict, devices: int) -> list[int]:
    """Split n devices by class share; the last class takes the rounding."""
    shares = [c["share"] for c in fleet["classes"]]
    counts = [round(devices * s) for s in shares[:-1]]
    counts.append(devices - sum(counts))
    return counts


def usage(fleet: dict, devices: int) -> Usage:
    apps_spec = fleet["apps"]
    counts = class_counts(fleet, devices)
    conn = gw = 0
    uplink = ubytes = 0.0
    status = hist = ev = rep = 0.0
    stored = hot = 0.0
    status_per_day_all = 0.0
    by_class = []
    years = fleet["events_sizing_years"]
    for cls, n in zip(fleet["classes"], counts):
        if cls["holds_connection"]:
            conn += n
        if cls.get("gateway_per_nodes"):
            g = math.ceil(n / cls["gateway_per_nodes"]) if n else 0
            gw += g
            conn += g
        for s in cls["streams"]:
            rows_day = _per_day(s) * n
            row_bytes = s["bytes"] + s.get("key_bytes", 0)
            uplink += rows_day * DAYS
            ubytes += rows_day * DAYS * s["bytes"]
            if s["name"] == "status":
                status += rows_day * DAYS
                status_per_day_all += rows_day
                stored += n * row_bytes
                hot += n * row_bytes
            elif s["name"] == "history":
                hist += rows_day * DAYS
                stored += rows_day * s["retention_days"] * row_bytes
                hot += rows_day * 30 * row_bytes
            elif s["name"] == "events":
                ev += rows_day * DAYS
                stored += rows_day * 365 * years * row_bytes
                hot += rows_day * 365 * years * row_bytes
            elif s["name"] == "settings reported":
                rep += rows_day * DAYS
                stored += n * row_bytes
                hot += n * row_bytes
        by_class.append({"name": cls["name"], "devices": n})
    apps = round(devices * apps_spec["per_device"])
    # A listening phone receives every status write of its devices while it
    # listens; the fleet's mean status rate per device is used for the mix.
    status_per_device_hour = (status_per_day_all / devices / 24) if devices else 0.0
    deliveries_day = (
        apps
        * apps_spec["devices_per_app"]
        * status_per_device_hour
        * apps_spec["listen_hours_per_day"]
    )
    status_bytes = next(
        s["bytes"] for s in fleet["classes"][0]["streams"] if s["name"] == "status"
    )
    hist_bytes = next(
        s["bytes"] for s in fleet["classes"][0]["streams"] if s["name"] == "history"
    )
    chart_opens = apps * apps_spec["chart_opens_per_day"] * DAYS
    return Usage(
        devices=devices,
        connections=conn,
        gateways=gw,
        apps=apps,
        uplink_msgs=uplink,
        uplink_bytes=ubytes,
        status_writes=status,
        history_rows=hist,
        event_rows=ev,
        reported_writes=rep,
        stored_bytes=stored,
        stored_hot_bytes=hot,
        desired_writes=apps * apps_spec["settings_changes_per_app_per_day"] * DAYS,
        app_status_deliveries=deliveries_day * DAYS,
        app_status_bytes=deliveries_day * DAYS * status_bytes,
        chart_opens=chart_opens,
        chart_bytes=chart_opens * apps_spec["chart_rows"] * hist_bytes,
        chart_rows_per_open=apps_spec["chart_rows"],
        peak_app_connections=apps * apps_spec["peak_fraction"],
        by_class=by_class,
    )


# ---------------------------------------------------------------------------
# 3. cost: each platform's billed units times its price
# ---------------------------------------------------------------------------


@dataclass
class Line:
    item: str
    qty: float
    unit: str
    price: float
    per: str  # what the price is per: "GB-month", "million", "hour", ...
    usd: float
    note: str = ""


@dataclass
class Bill:
    platform: str
    lines: list[Line]
    hours: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(ln.usd for ln in self.lines)


def tiered(qty: float, tiers: list[list]) -> float:
    """Price a quantity through [[upper_bound_or_None, usd_per_million], ...]."""
    usd = 0.0
    lower = 0.0
    for upper, per_million in tiers:
        top = qty if upper is None else min(qty, float(upper))
        if top > lower:
            usd += (top - lower) / 1e6 * per_million
        lower = top if upper is None else float(upper)
        if qty <= lower:
            break
    return usd


def _gb(n_bytes: float, prices: dict) -> float:
    return n_bytes / prices["gb_bytes"]


def bill_firebase(u: Usage, prices: dict) -> Bill:
    p = prices["firebase_rtdb"]
    lines = []
    stored_gb = _gb(u.stored_bytes, prices)
    lines.append(
        Line(
            "storage (history at retention, events, latest rows)",
            stored_gb,
            "GB",
            p["storage_usd_per_gb_month"],
            "GB-month",
            stored_gb * p["storage_usd_per_gb_month"],
        )
    )
    f = p["download_overhead_factor"]
    live_gb = _gb(u.app_status_bytes, prices) * f
    chart_gb = _gb(u.chart_bytes, prices) * f
    lines.append(
        Line(
            "download: status changes to listening phones",
            live_gb,
            "GB",
            p["download_usd_per_gb"],
            "GB",
            live_gb * p["download_usd_per_gb"],
        )
    )
    lines.append(
        Line(
            "download: chart opens (indexed, limitToLast)",
            chart_gb,
            "GB",
            p["download_usd_per_gb"],
            "GB",
            chart_gb * p["download_usd_per_gb"],
        )
    )
    lines.append(
        Line(
            "device writes and uplink bytes",
            u.uplink_msgs,
            "writes",
            0.0,
            "-",
            0.0,
            "not billed",
        )
    )
    conns = u.connections + u.peak_app_connections
    lines.append(
        Line(
            "simultaneous connections at the peak (devices + gateways + phones)",
            conns,
            "connections",
            0.0,
            "-",
            0.0,
            f"cap {p['blaze_connections_per_database']:,} per database instance",
        )
    )
    warnings = []
    spark_ok = (
        conns <= p["spark_connections"]
        and stored_gb <= p["spark_storage_gb"]
        and (live_gb + chart_gb) <= p["spark_download_gb_month"]
    )
    if spark_ok:
        warnings.append(
            "fits the Spark (free) plan: $0 a month; the total above is what Blaze would bill"
        )
    if conns > p["blaze_connections_per_database"]:
        warnings.append(
            f"{conns:,.0f} simultaneous connections exceed one instance's {p['blaze_connections_per_database']:,}: shard across instances"
        )
    if conns > p["spark_connections"]:
        warnings.append(
            f"above the Spark plan's {p['spark_connections']} connections: Blaze plan required"
        )
    if stored_gb > p["spark_storage_gb"]:
        warnings.append(
            f"above the Spark plan's {p['spark_storage_gb']} GB stored: Blaze plan required"
        )
    if (live_gb + chart_gb) > p["spark_download_gb_month"]:
        warnings.append(
            f"above the Spark plan's {p['spark_download_gb_month']} GB downloaded a month: Blaze plan required"
        )
    return Bill("firebase", lines, 0.0, warnings)


def bill_aws(u: Usage, prices: dict) -> Bill:
    iot = prices["aws_iot_core"]
    ddb = prices["aws_dynamodb"]
    fh = prices["aws_firehose"]
    s3 = prices["aws_s3"]
    lines = []
    conn_min = u.connections * MINUTES
    lines.append(
        Line(
            "IoT Core connection minutes (Wi-Fi nodes + gateways)",
            conn_min,
            "minutes",
            iot["connection_minutes_usd_per_million"],
            "million",
            conn_min / 1e6 * iot["connection_minutes_usd_per_million"],
        )
    )
    # Every record is under one 5 KB metering unit, so one message each; the
    # broker also bills each delivery to a subscriber (the listening phones).
    msgs = u.uplink_msgs + u.app_status_deliveries + u.desired_writes
    lines.append(
        Line(
            "IoT Core messages (uplink + deliveries to phones + desired)",
            msgs,
            "messages",
            iot["messages_usd_per_million_tiers"][0][1],
            "million, tiered",
            tiered(msgs, iot["messages_usd_per_million_tiers"]),
        )
    )
    lines.append(
        Line(
            "rules triggered (one per uplink message)",
            u.uplink_msgs,
            "rules",
            iot["rules_triggered_usd_per_million"],
            "million",
            u.uplink_msgs / 1e6 * iot["rules_triggered_usd_per_million"],
        )
    )
    actions = u.status_writes + u.reported_writes + 2 * (u.history_rows + u.event_rows)
    lines.append(
        Line(
            "rule actions (status/reported to DynamoDB; history/events to Firehose and DynamoDB)",
            actions,
            "actions",
            iot["actions_usd_per_million"],
            "million",
            actions / 1e6 * iot["actions_usd_per_million"],
        )
    )
    shadow = 2 * u.desired_writes
    lines.append(
        Line(
            "Device Shadow operations (desired written, reported echoed)",
            shadow,
            "operations",
            iot["shadow_ops_usd_per_million"],
            "million",
            shadow / 1e6 * iot["shadow_ops_usd_per_million"],
        )
    )
    writes = (
        u.status_writes + u.reported_writes + u.history_rows + u.event_rows
    )  # each row < 1 KB: one WRU
    lines.append(
        Line(
            "DynamoDB writes (latest rows, hot history, events)",
            writes,
            "WRU",
            ddb["write_usd_per_million_wru"],
            "million",
            writes / 1e6 * ddb["write_usd_per_million_wru"],
        )
    )
    hist_bytes = u.chart_bytes / u.chart_opens if u.chart_opens else 0.0
    rru_per_open = (
        math.ceil(hist_bytes / ddb["rru_bytes_eventually_consistent"])
        if hist_bytes
        else 0
    )
    reads = u.chart_opens * rru_per_open  # chart opens only; live values come over MQTT
    lines.append(
        Line(
            "DynamoDB reads (chart opens, eventually consistent query)",
            reads,
            "RRU",
            ddb["read_usd_per_million_rru"],
            "million",
            reads / 1e6 * ddb["read_usd_per_million_rru"],
        )
    )
    hot_gb = _gb(u.stored_hot_bytes, prices)
    billed_gb = max(0.0, hot_gb - ddb["storage_free_gb"])
    lines.append(
        Line(
            "DynamoDB storage (30-day hot history, events, latest rows)",
            hot_gb,
            "GB",
            ddb["storage_usd_per_gb_month"],
            "GB-month",
            billed_gb * ddb["storage_usd_per_gb_month"],
            f"first {ddb['storage_free_gb']} GB free",
        )
    )
    fh_bytes = (u.history_rows + u.event_rows) * fh["min_record_bytes"]
    fh_gb = _gb(fh_bytes, prices)
    lines.append(
        Line(
            "Firehose ingest (history + events, each record rounded up to 5 KB)",
            fh_gb,
            "GB",
            fh["usd_per_gb"],
            "GB",
            fh_gb * fh["usd_per_gb"],
        )
    )
    s3_gb = _gb(u.stored_bytes, prices)  # the full retention lives in S3
    lines.append(
        Line(
            "S3 storage (history at retention, events)",
            s3_gb,
            "GB",
            s3["storage_usd_per_gb_month"],
            "GB-month",
            s3_gb * s3["storage_usd_per_gb_month"],
        )
    )
    real_bytes = (
        u.history_rows + u.event_rows
    ) * 200  # about one compact row with its key; the time buffer dominates
    puts = max(
        DAYS * 24 * 12, math.ceil(real_bytes / (128 * 1024 * 1024))
    )  # a 300 s or a 128 MB buffer, whichever fills first
    lines.append(
        Line(
            "S3 PUTs by Firehose (one object per 5-minute buffer, or per 128 MB)",
            puts,
            "requests",
            s3["put_usd_per_1000"],
            "1 000",
            puts / 1000 * s3["put_usd_per_1000"],
        )
    )
    return Bill("aws", lines)


def pick_tier(fleet: dict, devices: int) -> dict:
    for tier in fleet["self_hosted"]["tiers"]:
        if tier["max_devices"] is None or devices <= tier["max_devices"]:
            return tier
    return fleet["self_hosted"]["tiers"][-1]


def bill_self(u: Usage, fleet: dict, prices: dict) -> Bill:
    ec2 = prices["aws_ec2"]
    dt = prices["aws_data_transfer"]
    sh = fleet["self_hosted"]
    tier = pick_tier(fleet, u.devices)
    lines = []
    hourly = ec2["instances_usd_per_hour"][tier["instance"]]
    hours = tier["count"] * HOURS
    lines.append(
        Line(
            f"{tier['count']} x {tier['instance']} (broker, database, dashboard)",
            hours,
            "instance-hours",
            hourly,
            "hour",
            hours * hourly,
            "sizing is a judgement, not a benchmark",
        )
    )
    raw_gb = _gb(u.stored_bytes, prices)
    disk_gb = max(
        sh["min_disk_gb"],
        math.ceil(raw_gb / sh["compression_ratio"] * sh["disk_headroom"]),
    )
    lines.append(
        Line(
            f"EBS gp3 disk ({raw_gb:,.1f} GB raw / {sh['compression_ratio']}x compression x {sh['disk_headroom']} headroom)",
            disk_gb,
            "GB",
            ec2["ebs_gp3_usd_per_gb_month"],
            "GB-month",
            disk_gb * ec2["ebs_gp3_usd_per_gb_month"],
        )
    )
    snap_gb = max(1, math.ceil(raw_gb / sh["compression_ratio"]))
    lines.append(
        Line(
            "snapshots (one full copy of the compressed data)",
            snap_gb,
            "GB",
            ec2["snapshot_usd_per_gb_month"],
            "GB-month",
            snap_gb * ec2["snapshot_usd_per_gb_month"],
        )
    )
    egress_gb = _gb(u.app_status_bytes + u.chart_bytes, prices)
    billed = max(0.0, egress_gb - dt["free_gb_per_month"])
    lines.append(
        Line(
            "data out to phones (live values + charts)",
            egress_gb,
            "GB",
            dt["egress_usd_per_gb"],
            "GB",
            billed * dt["egress_usd_per_gb"],
            f"first {dt['free_gb_per_month']} GB free; price from memory",
        )
    )
    lines.append(
        Line(
            "device uplink into the instance",
            _gb(u.uplink_bytes, prices),
            "GB",
            0.0,
            "-",
            0.0,
            "inbound transfer is free",
        )
    )
    return Bill("self", lines, hours=float(tier["ops_hours_per_month"]))


def what_if(
    fleet: dict,
    status_period_s: int | None = None,
    history_retention_days: int | None = None,
) -> dict:
    """A copy of the fleet with the two levers moved: the status period and the history retention."""
    out = json.loads(json.dumps(fleet))
    for cls in out["classes"]:
        for s in cls["streams"]:
            if s["name"] == "status" and status_period_s:
                s["per_day"] = min(s["per_day"], 86400 / status_period_s)
            if s["name"] == "history" and history_retention_days:
                s["retention_days"] = history_retention_days
    return out


def bills(fleet: dict, prices: dict, devices: int) -> tuple[Usage, list[Bill]]:
    u = usage(fleet, devices)
    return u, [
        bill_firebase(u, prices),
        bill_aws(u, prices),
        bill_self(u, fleet, prices),
    ]


def _n(x: float) -> str:
    if x == 0:
        return "0"
    if abs(x) >= 100:
        return f"{x:,.0f}"
    if abs(x) >= 1:
        return f"{x:,.2f}"
    return f"{x:,.4f}"


def print_cost(fleet: dict, prices: dict, sizes: list[int]) -> None:
    summary: list[tuple[int, dict[str, Bill]]] = []
    for n in sizes:
        u, bs = bills(fleet, prices, n)
        mix = ", ".join(
            f"{c['devices']:,} {c['name'].split(' (')[0]}" for c in u.by_class
        )
        print(
            f"=== {n:,} devices: {mix}, {u.gateways:,} gateways, {u.apps:,} phones ==="
        )
        print(
            f"a month: {u.uplink_msgs:,.0f} uplink messages, {u.status_writes:,.0f} status writes, "
            f"{u.history_rows:,.0f} history rows, {u.event_rows:,.0f} events; "
            f"{_gb(u.stored_bytes, prices):,.2f} GB kept at steady state; "
            f"{u.app_status_deliveries:,.0f} status changes delivered to phones; {u.chart_opens:,.0f} chart opens"
        )
        for b in bs:
            print(f"\n  {PLATFORM_NAMES[b.platform]}")
            for ln in b.lines:
                note = f"   ({ln.note})" if ln.note else ""
                price = "" if ln.price == 0 else f" x ${ln.price:g}/{ln.per}"
                print(
                    f"    {ln.item:<78} {_n(ln.qty):>16} {ln.unit:<14}{price:<22} ${ln.usd:>10,.2f}{note}"
                )
            extra = (
                f" + {b.hours:g} h of operations a month (your rate)" if b.hours else ""
            )
            print(
                f"    {'total':<78} {'':>16} {'':<14}{'':<22} ${b.total:>10,.2f}{extra}"
            )
            for w in b.warnings:
                print(f"    ! {w}")
        summary.append((n, {b.platform: b for b in bs}))
        print()
    print("summary, USD a month")
    print(
        f"{'devices':>10}  {'Firebase RTDB':>14}  {'AWS IoT Core':>14}  {'self-hosted':>14}  {'+ hours':>8}"
    )
    for n, d in summary:
        print(
            f"{n:>10,}  {d['firebase'].total:>14,.2f}  {d['aws'].total:>14,.2f}  {d['self'].total:>14,.2f}  {d['self'].hours:>8g}"
        )
    print()
    unverified = [
        k
        for k, v in prices.items()
        if isinstance(v, dict) and v.get("verified") is False
    ]
    print(
        "prices: "
        + "; ".join(
            f"{k}: {prices[k]['source']}"
            for k in prices
            if isinstance(prices[k], dict) and "source" in prices[k]
        )
    )
    if unverified:
        print(
            f"from memory, not fetched: {', '.join(unverified)}. Not costed: "
            + " | ".join(prices["not_costed"])
        )


# ---------------------------------------------------------------------------
# 4. fanout: the listening phone, and where the listener is attached
# ---------------------------------------------------------------------------


def fanout_rows(fleet: dict, prices: dict, devices: int = 1000) -> list[dict]:
    """Per phone per month: bytes and dollars under three listener placements."""
    u = usage(fleet, devices)
    apps_spec = fleet["apps"]
    p = prices["firebase_rtdb"]
    iot = prices["aws_iot_core"]
    dt = prices["aws_data_transfer"]
    gh = fleet["classes"][0]
    status_b = next(s for s in gh["streams"] if s["name"] == "status")
    hist = next(s for s in gh["streams"] if s["name"] == "history")
    ev = next(s for s in gh["streams"] if s["name"] == "events")
    per_dev_subtree = (
        status_b["bytes"]
        + hist["per_day"] * hist["retention_days"] * (hist["bytes"] + hist["key_bytes"])
        + ev["per_day"]
        * 365
        * fleet["events_sizing_years"]
        * (ev["bytes"] + ev["key_bytes"])
    )
    live_msgs = (
        status_b["per_day"]
        / 24
        * apps_spec["listen_hours_per_day"]
        * apps_spec["devices_per_app"]
        * DAYS
    )
    live_bytes = live_msgs * status_b["bytes"]
    opens = apps_spec["chart_opens_per_day"] * DAYS
    chart_bytes = opens * apps_spec["chart_rows"] * hist["bytes"]
    rows = []

    def row(name, fb_bytes, note):
        gb = fb_bytes / prices["gb_bytes"] * p["download_overhead_factor"]
        rows.append(
            {
                "placement": name,
                "firebase_gb": gb,
                "firebase_usd": gb * p["download_usd_per_gb"],
                "note": note,
            }
        )

    row(
        "A  listener on /status, chart = orderByKey().limitToLast(144)",
        live_bytes + chart_bytes,
        "the changes while listening, plus 144 rows per chart open",
    )
    row(
        "B  listener on /devices/<id> (status + settings + history in one go)",
        live_bytes + opens * apps_spec["devices_per_app"] * per_dev_subtree,
        "every open downloads each device's whole subtree once, two years of history included",
    )
    row(
        "C  as A, but the chart orders by child ts and history has no .indexOn",
        live_bytes
        + opens
        * (
            hist["per_day"]
            * hist["retention_days"]
            * (hist["bytes"] + hist["key_bytes"])
        ),
        "an unindexed query downloads the whole history node and filters on the phone",
    )
    # The two other platforms for placement A, per phone per month.
    aws_usd = tiered(live_msgs, iot["messages_usd_per_million_tiers"])
    self_gb = (live_bytes + chart_bytes) / prices["gb_bytes"]
    return [
        *rows,
        {
            "placement": "A on AWS IoT Core: each status change is one delivered message",
            "firebase_gb": None,
            "firebase_usd": aws_usd,
            "note": f"{live_msgs:,.0f} deliveries a month at ${iot['messages_usd_per_million_tiers'][0][1]:g} per million; the chart is a DynamoDB query, cents",
        },
        {
            "placement": "A self-hosted: bytes out of the instance",
            "firebase_gb": self_gb,
            "firebase_usd": self_gb * dt["egress_usd_per_gb"],
            "note": f"at ${dt['egress_usd_per_gb']:g}/GB after the free 100 GB a month (price from memory); phones {u.apps:,} at {devices:,} devices",
        },
    ]


def print_fanout(fleet: dict, prices: dict) -> None:
    rows = fanout_rows(fleet, prices)
    print(
        "one phone, one month: ten devices watched two hours a day, a 24-hour chart opened twice a day"
    )
    print(f"{'listener placement':<72} {'GB':>10} {'USD':>10}  note")
    for r in rows:
        gb = "" if r["firebase_gb"] is None else f"{r['firebase_gb']:,.3f}"
        print(f"{r['placement']:<72} {gb:>10} {r['firebase_usd']:>10,.2f}  {r['note']}")


# ---------------------------------------------------------------------------
# 5. rules: chapter 7's table in three dialects
# ---------------------------------------------------------------------------

STATUS_FIELDS = ("ts", "temp", "hum", "heater")


def rules_firebase() -> dict:
    device = "auth != null && auth.token.device === true && auth.token.deviceId === $id"
    not_disabled = "root.child('devices/' + $id + '/disabled').val() !== true"
    owner = "auth != null && root.child('devices/' + $id + '/owners/' + auth.uid).val() === true"
    fields = ", ".join(f"'{f}'" for f in STATUS_FIELDS)
    status_validate = (
        f"newData.hasChildren([{fields}]) && newData.child('ts').isNumber() && "
        "newData.child('temp').isNumber() && newData.child('hum').isNumber() && newData.child('heater').isBoolean()"
    )
    return {
        "rules": {
            "devices": {
                "$id": {
                    "status": {
                        ".read": owner,
                        ".write": f"{device} && {not_disabled}",
                        ".validate": status_validate,
                        "$other": {".validate": False},
                    },
                    "history": {
                        ".read": owner,
                        ".indexOn": ["ts"],
                        "$ts": {
                            ".write": f"{device} && {not_disabled} && !data.exists()",
                            ".validate": "$ts.matches(/^[0-9]{10}$/) && newData.hasChildren(['ts', 'n'])",
                        },
                    },
                    "settings": {
                        "desired": {
                            ".read": f"({device}) || ({owner})",
                            ".write": owner,
                        },
                        "reported": {
                            ".read": owner,
                            ".write": f"{device} && {not_disabled}",
                        },
                    },
                    "owners": {".read": owner},
                    "disabled": {".read": owner},
                }
            }
        }
    }


def rules_aws(account: str = "123456789012", region: str = "eu-west-1") -> dict:
    arn = f"arn:aws:iot:{region}:{account}"
    thing = "${iot:Connection.Thing.ThingName}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ConnectAsYourself",
                "Effect": "Allow",
                "Action": "iot:Connect",
                "Resource": f"{arn}:client/{thing}",
                "Condition": {"Bool": {"iot:Connection.Thing.IsAttached": "true"}},
            },
            {
                "Sid": "PublishOwnStreams",
                "Effect": "Allow",
                "Action": "iot:Publish",
                "Resource": [
                    f"{arn}:topic/farm/{thing}/status",
                    f"{arn}:topic/farm/{thing}/history",
                    f"{arn}:topic/farm/{thing}/events",
                    f"{arn}:topic/$aws/things/{thing}/shadow/update",
                ],
            },
            {
                "Sid": "ReadOwnSettings",
                "Effect": "Allow",
                "Action": ["iot:Subscribe"],
                "Resource": [
                    f"{arn}:topicfilter/$aws/things/{thing}/shadow/update/delta",
                    f"{arn}:topicfilter/$aws/things/{thing}/shadow/get/accepted",
                ],
            },
            {
                "Sid": "ReceiveOwnSettings",
                "Effect": "Allow",
                "Action": ["iot:Receive"],
                "Resource": [
                    f"{arn}:topic/$aws/things/{thing}/shadow/update/delta",
                    f"{arn}:topic/$aws/things/{thing}/shadow/get/accepted",
                ],
            },
            {
                "Sid": "AskForSettings",
                "Effect": "Allow",
                "Action": "iot:Publish",
                "Resource": f"{arn}:topic/$aws/things/{thing}/shadow/get",
            },
        ],
    }


MOSQUITTO_ACL = """\
# mosquitto ACL: %u is the client's username, which is its device id.
# Credentials: one entry per device in the password file (mosquitto_passwd),
# or one client certificate per device with use_identity_as_username true.
# Shape checks and append-only history are the collector's job, not the broker's.
pattern write farm/%u/status
pattern write farm/%u/history
pattern write farm/%u/events
pattern write farm/%u/settings/reported
pattern read  farm/%u/settings/desired

# owners: one line per (owner, device) pair, written by the claim service
user app-uid-7f3a
topic read  farm/gh-1/#
topic write farm/gh-1/settings/desired

# a disabled device: remove its password entry and kill its session
#   mosquitto_ctrl dynsec disableClient gh-2      (dynamic-security plugin)
"""


def rules_mosquitto() -> str:
    return MOSQUITTO_ACL


RULES_MAP = [
    (
        "device writes exactly ts temp hum heater to its own status",
        "firebase",
        "`.write` with `auth.token.deviceId === $id`, `.validate` with `hasChildren` and `$other: false`",
    ),
    (
        "device writes exactly ts temp hum heater to its own status",
        "aws",
        "`iot:Publish` on `topic/farm/${thing}/status`; the shape check moves to the rule's SQL or a Lambda",
    ),
    (
        "device writes exactly ts temp hum heater to its own status",
        "mosquitto",
        "`pattern write farm/%u/status`; the shape check moves to the collector",
    ),
    ("history append-only", "firebase", "`!data.exists()` on `$ts`"),
    (
        "history append-only",
        "aws",
        "a conditional put in the rule's DynamoDB action (`attribute_not_exists`)",
    ),
    (
        "history append-only",
        "mosquitto",
        "`INSERT ... ON CONFLICT DO NOTHING` in the collector",
    ),
    (
        "owner writes desired, device reads it",
        "firebase",
        "`.write` for owners, `.read` for both",
    ),
    (
        "owner writes desired, device reads it",
        "aws",
        "the shadow: the app's identity may `UpdateThingShadow`; the thing subscribes to `shadow/update/delta`",
    ),
    (
        "owner writes desired, device reads it",
        "mosquitto",
        "retained `farm/<id>/settings/desired`; the owner's ACL line allows the write",
    ),
    (
        "owners and disabled written by the server only",
        "firebase",
        "no `.write` rule at those paths; the Admin SDK bypasses rules",
    ),
    (
        "owners and disabled written by the server only",
        "aws",
        "owners live in the app's API; disabled = deactivate the certificate or detach the policy",
    ),
    (
        "owners and disabled written by the server only",
        "mosquitto",
        "the claim service edits the ACL and password files, then reloads the broker",
    ),
    ("default deny", "firebase", "no rule at the root"),
    ("default deny", "aws", "an IoT policy allows only what it names"),
    ("default deny", "mosquitto", "an ACL file denies what it does not name"),
]


def print_rules(dialect: str) -> None:
    if dialect == "firebase":
        print(json.dumps(rules_firebase(), indent=2))
    elif dialect == "aws":
        print(json.dumps(rules_aws(), indent=2))
    elif dialect == "mosquitto":
        print(rules_mosquitto())
    elif dialect == "map":
        rows = {}
        for row, d, how in RULES_MAP:
            rows.setdefault(row, {})[d] = how
        print(
            "| chapter 7 row | Firebase rules | AWS IoT policy | Mosquitto ACL + collector |"
        )
        print("|---|---|---|---|")
        for row, d in rows.items():
            print(f"| {row} | {d['firebase']} | {d['aws']} | {d['mosquitto']} |")
    else:
        raise SystemExit(f"unknown dialect {dialect!r}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fit")
    p.add_argument("requirements", nargs="?", default=str(HERE / "requirements.json"))
    p = sub.add_parser("cost")
    p.add_argument("fleet", nargs="?", default=str(HERE / "fleet.json"))
    p.add_argument("prices", nargs="?", default=str(HERE / "prices.json"))
    p.add_argument(
        "--devices",
        type=int,
        nargs="+",
        help="fleet sizes (default: fleet.json's list)",
    )
    p.add_argument(
        "--status-period-s",
        type=int,
        help="what if status were sent every N seconds (never faster than the JSON)",
    )
    p.add_argument(
        "--history-retention-days", type=int, help="what if history were kept N days"
    )
    p = sub.add_parser("fanout")
    p.add_argument("fleet", nargs="?", default=str(HERE / "fleet.json"))
    p.add_argument("prices", nargs="?", default=str(HERE / "prices.json"))
    p = sub.add_parser("rules")
    p.add_argument("dialect", choices=["firebase", "aws", "mosquitto", "map"])
    a = ap.parse_args(argv)
    if a.cmd == "fit":
        print_fit(load(a.requirements))
    elif a.cmd == "cost":
        fleet = what_if(load(a.fleet), a.status_period_s, a.history_retention_days)
        if a.status_period_s or a.history_retention_days:
            print(
                f"what if: status period {a.status_period_s or 'as in the JSON'} s, "
                f"history retention {a.history_retention_days or 'as in the JSON'} days\n"
            )
        print_cost(fleet, load(a.prices), a.devices or fleet["fleet_sizes"])
    elif a.cmd == "fanout":
        print_fanout(load(a.fleet), load(a.prices))
    elif a.cmd == "rules":
        print_rules(a.dialect)
    return 0


if __name__ == "__main__":
    sys.exit(main())
