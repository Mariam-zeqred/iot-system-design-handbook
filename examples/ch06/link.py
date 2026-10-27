"""Chapter 6 - connectivity: choosing the link.

Five small tools in one file, standard-library only:

    python link.py airtime  [--bytes 11] [--markdown]
    python link.py protocol [--body 84] [--keepalive-s 60] [--markdown]
    python link.py budget   nodes.json links.json [--markdown]
    python link.py select   nodes.json links.json [--markdown]
    python link.py backoff  [--nodes 1000] [--capacity 50] [--no-jitter] [--seed 1]

``airtime`` is the LoRa time-on-air formula from the Semtech SX127x
datasheet, printed for one application payload at every spreading factor
of the EU868 plan, with the messages per day each of the two airtime
budgets allows (the regulatory 1 % duty cycle, and The Things Network's
30 s per day fair use).

``protocol`` counts the bytes one status record costs on the wire under
MQTT on a kept TLS session, HTTPS on a kept connection, and HTTPS with a
new TLS handshake per request, plus the keep-alive traffic of the kept
session.

``budget`` is the energy model: for every node and every link, mAh per
day and the days a battery lasts, from the per-link currents and seconds
in ``links.json`` (labelled assumptions, every one with a ``source``).

``select`` applies the chapter's five exclusion rules, in order, to every
node x link pair and picks the cheapest link that survives them.

``backoff`` prints the reconnect schedule and simulates a fleet
reconnecting after a broker outage, with and without jitter.

Every number printed is a property of the parameters in the two JSON files
and of these formulas, not a measurement.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1. LoRa time on air
# ---------------------------------------------------------------------------

LORAWAN_HEADER_BYTES = 13  # MHDR 1 + DevAddr 4 + FCtrl 1 + FCnt 2 + FPort 1 + MIC 4
EU868_SFS = (7, 8, 9, 10, 11, 12)
EU868_MAX_PAYLOAD = {7: 222, 8: 222, 9: 115, 10: 51, 11: 51, 12: 51}
DUTY_CYCLE_S_PER_HOUR = 36.0  # 1 % of 3 600 s on one sub-band
TTN_FAIR_USE_S_PER_DAY = 30.0


def lora_airtime_s(
    payload_bytes: int,
    sf: int,
    bw_hz: int = 125_000,
    cr: int = 1,
    preamble_symbols: int = 8,
    explicit_header: bool = True,
    crc: bool = True,
) -> float:
    """Time on air of one LoRa packet, SX127x datasheet section 4.1.1.6.

    ``payload_bytes`` is the PHY payload (for LoRaWAN: application bytes
    plus the 13-byte header).  ``cr`` is 1 for coding rate 4/5.  Low data
    rate optimisation is on for SF11 and SF12 at 125 kHz, as LoRaWAN
    requires.
    """
    if sf not in range(6, 13):
        raise ValueError(f"spreading factor {sf} is not in 6..12")
    t_sym = (2**sf) / bw_hz
    t_preamble = (preamble_symbols + 4.25) * t_sym
    de = 1 if (bw_hz == 125_000 and sf >= 11) else 0
    ih = 0 if explicit_header else 1
    numerator = 8 * payload_bytes - 4 * sf + 28 + 16 * int(crc) - 20 * ih
    denominator = 4 * (sf - 2 * de)
    n_payload = 8 + max(math.ceil(numerator / denominator) * (cr + 4), 0)
    return t_preamble + n_payload * t_sym


def lorawan_airtime_s(app_bytes: int, sf: int) -> float:
    """Airtime of a LoRaWAN uplink carrying ``app_bytes`` of application data."""
    return lora_airtime_s(app_bytes + LORAWAN_HEADER_BYTES, sf)


def airtime_report(app_bytes: int, markdown: bool) -> str:
    rows = []
    for sf in EU868_SFS:
        t = lorawan_airtime_s(app_bytes, sf)
        fits = app_bytes <= EU868_MAX_PAYLOAD[sf]
        rows.append(
            [
                f"SF{sf}",
                str(EU868_MAX_PAYLOAD[sf]),
                "yes" if fits else "NO",
                f"{t * 1000:.1f}",
                f"{DUTY_CYCLE_S_PER_HOUR / t:.0f}",
                f"{TTN_FAIR_USE_S_PER_DAY / t:.0f}",
            ]
        )
    head = [
        "SF",
        "max app bytes",
        f"{app_bytes} B fits",
        "airtime ms",
        "msg/h under 1 % duty cycle",
        "msg/day under 30 s fair use",
    ]
    title = (
        f"LoRaWAN uplink of {app_bytes} application bytes "
        f"({app_bytes + LORAWAN_HEADER_BYTES} B on air), 125 kHz, CR 4/5, "
        "8 preamble symbols:"
    )
    return "\n".join([title, _table(head, rows, markdown)])


# ---------------------------------------------------------------------------
# 2. Protocol bytes per message
# ---------------------------------------------------------------------------

TLS_RECORD_OVERHEAD = 29  # as chapter 2's traffic tool: TLS 1.2 AES-GCM record
TLS_FULL_HANDSHAKE_BYTES = (
    4500  # a typical two-certificate chain: an order of magnitude
)
MQTT_TOPIC = "farm/gh1/status"
MQTT_PING_BYTES = 2  # PINGREQ or PINGRESP
HTTP_REQUEST_HEADER_BYTES = 250  # request line, Host, Content-Type, Length, Auth
HTTP_RESPONSE_BYTES = 150  # status line and a few headers, empty body


def mqtt_publish_bytes(body: int, topic: str = MQTT_TOPIC) -> int:
    """Chapter 2's model: 2-byte fixed header, 2-byte topic length, the topic."""
    return body + 4 + len(topic) + TLS_RECORD_OVERHEAD


def https_keepalive_bytes(body: int) -> int:
    return (
        body
        + HTTP_REQUEST_HEADER_BYTES
        + TLS_RECORD_OVERHEAD
        + HTTP_RESPONSE_BYTES
        + TLS_RECORD_OVERHEAD
    )


def https_new_connection_bytes(body: int) -> int:
    return https_keepalive_bytes(body) + TLS_FULL_HANDSHAKE_BYTES


def protocol_report(body: int, keepalive_s: int, markdown: bool) -> str:
    per_day = 1440  # one status record a minute, as chapter 2's path B
    ping_per_day = 86400 / keepalive_s
    ping_bytes_day = ping_per_day * 2 * (MQTT_PING_BYTES + TLS_RECORD_OVERHEAD)
    rows = [
        ["MQTT, kept TLS session", mqtt_publish_bytes(body), 0.0],
        [
            f"  + keep-alive every {keepalive_s} s",
            2 * (MQTT_PING_BYTES + TLS_RECORD_OVERHEAD),
            ping_bytes_day / 1e3,
        ],
        ["HTTPS, kept connection", https_keepalive_bytes(body), 0.0],
        ["HTTPS, new TLS handshake per request", https_new_connection_bytes(body), 0.0],
    ]
    for r in rows:
        if r[2] == 0.0:
            r[2] = r[1] * per_day / 1e3
    head = ["transport", "bytes per status", "kB/day at 1 per min"]
    table = _table(head, [[r[0], str(r[1]), f"{r[2]:.1f}"] for r in rows], markdown)
    note = (
        f"Body {body} B (chapter 2's status record). Header sizes are typical, "
        f"not measured: {HTTP_REQUEST_HEADER_BYTES} B request headers, "
        f"{HTTP_RESPONSE_BYTES} B response, {TLS_FULL_HANDSHAKE_BYTES} B per full "
        "TLS handshake, 29 B per TLS record."
    )
    return f"{table}\n{note}"


# ---------------------------------------------------------------------------
# 3. Energy budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    name: str
    bytes: int
    per_day: float


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    power: str  # mains | battery
    battery_mah: float | None
    distance_to_router_m: float
    distance_to_gateway_m: float
    cellular_coverage: bool
    sf: int
    command_deadline_s: float
    required_life_days: float | None
    messages: tuple[Message, ...]

    @property
    def messages_per_day(self) -> float:
        return sum(m.per_day for m in self.messages)

    @property
    def uplink_period_s(self) -> float:
        """The period of the most frequent stream: how often the node wakes."""
        return 86400 / max(m.per_day for m in self.messages)

    @property
    def largest_message_bytes(self) -> int:
        return max(m.bytes for m in self.messages)


def load_nodes(path: Path) -> list[Node]:
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = []
    for n in data["nodes"]:
        msgs = tuple(Message(**m) for m in n.pop("messages"))
        nodes.append(Node(messages=msgs, **n))
    return nodes


def load_links(path: Path) -> tuple[float, list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return float(data["usable_battery_fraction"]), list(data["links"])


def message_energy_mas(link: dict[str, Any], node: Node, msg: Message) -> float:
    """Milliamp-seconds one message costs the node on this link."""
    e = link["energy"]
    mas = e["wake_s"] * e["wake_ma"]
    if link["id"] == "lorawan":
        mas += lorawan_airtime_s(msg.bytes, node.sf) * e["tx_ma"]
        mas += e["rx_s"] * e["rx_ma"]
    return mas


def energy_per_day_mah(link: dict[str, Any], node: Node) -> float:
    sleep = link["energy"]["sleep_ua"] / 1000 * 24  # uA -> mA, x 24 h
    active = sum(message_energy_mas(link, node, m) * m.per_day for m in node.messages)
    if link["id"] == "lorawan" and node.power == "mains":
        # class C: receiver on whenever not transmitting
        sleep += link["class_c_rx_ma"] * 24
    return sleep + active / 3600


def battery_life_days(
    link: dict[str, Any], node: Node, usable_fraction: float
) -> float | None:
    if node.battery_mah is None:
        return None
    return node.battery_mah * usable_fraction / energy_per_day_mah(link, node)


def budget_report(nodes: list[Node], links_path: Path, markdown: bool) -> str:
    frac, links = load_links(links_path)
    head = ["node", "power"] + [f"{lk['id']} mAh/day" for lk in links]
    head += [f"{lk['id']} battery days" for lk in links]
    rows = []
    for n in nodes:
        row = [n.name, n.power]
        row += [f"{energy_per_day_mah(lk, n):.2f}" for lk in links]
        for lk in links:
            life = battery_life_days(lk, n, frac)
            row.append("-" if life is None else f"{life:,.0f}")
        rows.append(row)
    note = (
        f"Battery days use {frac:g} of the nameplate mAh. LoRaWAN rows for mains "
        "nodes include a class C receiver; battery nodes are class A at the SF in "
        "nodes.json. Every current and second is an assumption from links.json."
    )
    return "\n".join([_table(head, rows, markdown), note])


# ---------------------------------------------------------------------------
# 4. Selection: five exclusion rules in order, then the cheapest survivor
# ---------------------------------------------------------------------------


def link_max_payload(link: dict[str, Any], node: Node) -> int | None:
    if "max_payload_by_sf" in link:
        return int(link["max_payload_by_sf"][str(node.sf)])
    return link.get("max_payload_bytes")


def downlink_latency_s(link: dict[str, Any], node: Node) -> float:
    """A sleeping node hears nothing until it wakes; class A hears only after an uplink."""
    if node.power == "battery":
        return node.uplink_period_s
    return float(link["downlink_latency_s"])


def airtime_per_day_s(link: dict[str, Any], node: Node) -> float | None:
    if link["id"] != "lorawan":
        return None
    return sum(lorawan_airtime_s(m.bytes, node.sf) * m.per_day for m in node.messages)


def monthly_cost_usd(link: dict[str, Any]) -> float:
    site = link["site_usd"] / (link["site_life_years"] * 12) / link["site_shared_by"]
    return float(link["per_device_usd_month"]) + site


def exclude(link: dict[str, Any], node: Node, usable_fraction: float) -> str | None:
    """Return the first rule that excludes this link for this node, or None."""
    # X1 reach
    if link["id"] == "wifi" and node.distance_to_router_m > link["range_m"]:
        return f"X1 reach: {node.distance_to_router_m:g} m to the router > {link['range_m']} m"
    if link["id"] == "lorawan" and node.distance_to_gateway_m > link["range_m"]:
        return (
            f"X1 reach: {node.distance_to_gateway_m:g} m to the gateway > "
            f"{link['range_m']} m"
        )
    if link["id"] == "cellular" and not node.cellular_coverage:
        return "X1 reach: no cellular coverage at the node"
    # X2 payload
    cap = link_max_payload(link, node)
    if cap is not None and node.largest_message_bytes > cap:
        return f"X2 payload: {node.largest_message_bytes} B > {cap} B at SF{node.sf}"
    # X3 airtime budget
    budget = link.get("airtime_budget_s_per_day")
    airtime = airtime_per_day_s(link, node)
    if budget is not None and airtime is not None and airtime > budget:
        return f"X3 airtime: {airtime:.1f} s/day at SF{node.sf} > {budget:g} s/day"
    # X4 downlink latency
    latency = downlink_latency_s(link, node)
    if latency > node.command_deadline_s:
        return (
            f"X4 downlink: a command waits up to {latency:g} s > "
            f"{node.command_deadline_s:g} s allowed"
        )
    # X5 energy
    life = battery_life_days(link, node, usable_fraction)
    if (
        node.required_life_days is not None
        and life is not None
        and life < node.required_life_days
    ):
        return f"X5 energy: {life:,.0f} days < {node.required_life_days:g} required"
    return None


@dataclass(frozen=True)
class Choice:
    node: Node
    link_id: str | None
    verdicts: dict[str, str]  # link id -> "ok ..." or the excluding rule


def select(nodes: list[Node], links_path: Path) -> list[Choice]:
    frac, links = load_links(links_path)
    out = []
    for n in nodes:
        verdicts: dict[str, str] = {}
        feasible: list[tuple[float, float, str]] = []
        for lk in links:
            why = exclude(lk, n, frac)
            if why:
                verdicts[lk["id"]] = why
                continue
            cost = monthly_cost_usd(lk)
            energy = energy_per_day_mah(lk, n)
            verdicts[lk["id"]] = f"ok: {cost:.2f} USD/month, {energy:.2f} mAh/day"
            feasible.append((cost, energy, lk["id"]))
        chosen = min(feasible)[2] if feasible else None
        out.append(Choice(n, chosen, verdicts))
    return out


def select_report(nodes: list[Node], links_path: Path, markdown: bool) -> str:
    _, links = load_links(links_path)
    ids = [lk["id"] for lk in links]
    choices = select(nodes, links_path)
    head = ["node", *ids, "chosen"]
    rows = []
    for c in choices:
        rows.append([c.node.name, *[c.verdicts[i] for i in ids], c.link_id or "NONE"])
    note = (
        "Rules in order: X1 reach, X2 payload, X3 airtime budget, X4 downlink "
        "latency, X5 battery life; the cheapest survivor per month wins, then the "
        "lowest energy. Costs are illustrative (links.json)."
    )
    return "\n".join([_table(head, rows, markdown), note])


# ---------------------------------------------------------------------------
# 5. Reconnect backoff and the herd
# ---------------------------------------------------------------------------


def backoff_delay(
    attempt: int,
    rng: random.Random | None,
    base_s: float = 1.0,
    factor: float = 2.0,
    cap_s: float = 300.0,
) -> float:
    """Delay before retry number ``attempt`` (0-based); full jitter when ``rng``."""
    ceiling = min(cap_s, base_s * factor**attempt)
    if rng is None:
        return ceiling
    return rng.uniform(0, ceiling)


@dataclass(frozen=True)
class HerdResult:
    nodes: int
    capacity_per_s: int
    jitter: bool
    peak_attempts_per_s: int
    rejected: int
    t50_s: float | None
    t99_s: float | None
    tall_s: float | None


def herd(
    nodes: int,
    capacity_per_s: int,
    jitter: bool,
    outage_s: float = 120.0,
    horizon_s: float = 7200.0,
    seed: int = 1,
    cap_s: float = 300.0,
) -> HerdResult:
    """Every node loses the broker at t = 0; the broker is back at ``outage_s``.

    A node retries on its backoff schedule.  An attempt before the broker is
    back fails.  After that, the broker accepts at most ``capacity_per_s``
    connections in any one second and rejects the rest, which back off again.
    """
    rng = random.Random(seed) if jitter else None
    # (next attempt time, attempt number, node id); start from the first delay
    pending = [(backoff_delay(0, rng, cap_s=cap_s), 0, i) for i in range(nodes)]
    connected_at: list[float] = []
    per_second: dict[int, int] = {}
    accepted_in_second: dict[int, int] = {}
    rejected = 0
    while pending:
        pending.sort()
        t, attempt, i = pending.pop(0)
        if t > horizon_s:
            break
        sec = int(t)
        if t >= outage_s:  # what the broker sees once it is back
            per_second[sec] = per_second.get(sec, 0) + 1
        ok = t >= outage_s and accepted_in_second.get(sec, 0) < capacity_per_s
        if ok:
            accepted_in_second[sec] = accepted_in_second.get(sec, 0) + 1
            connected_at.append(t)
        else:
            if t >= outage_s:
                rejected += 1
            nxt = attempt + 1
            pending.append((t + backoff_delay(nxt, rng, cap_s=cap_s), nxt, i))
    connected_at.sort()

    def at(frac: float) -> float | None:
        k = math.ceil(frac * nodes)
        return connected_at[k - 1] - outage_s if len(connected_at) >= k else None

    return HerdResult(
        nodes,
        capacity_per_s,
        jitter,
        max(per_second.values()) if per_second else 0,
        rejected,
        at(0.5),
        at(0.99),
        at(1.0),
    )


def backoff_report(nodes: int, capacity: int, jitter: bool, seed: int) -> str:
    out = ["Schedule (base 1 s, factor 2, cap 300 s):"]
    rng = random.Random(seed)
    plain = [backoff_delay(k, None) for k in range(10)]
    jit = [backoff_delay(k, rng) for k in range(10)]
    out.append("  attempt     " + " ".join(f"{k:>6d}" for k in range(10)))
    out.append("  no jitter   " + " ".join(f"{d:>6.0f}" for d in plain))
    out.append("  full jitter " + " ".join(f"{d:>6.1f}" for d in jit) + "  (one draw)")
    out.append("")
    out.append(
        f"{nodes} nodes lose the broker at t=0; it is back at t=120 s and accepts "
        f"{capacity} connections per second:"
    )
    variants = [herd(nodes, capacity, False, seed=seed)]
    if jitter:
        variants.append(herd(nodes, capacity, True, seed=seed))
    for r in variants:
        name = "full jitter" if r.jitter else "no jitter  "

        def fmt(x: float | None) -> str:
            return "never (2 h)" if x is None else f"{x:,.0f} s"

        out.append(
            f"  {name}  peak {r.peak_attempts_per_s:>5d} attempts/s, "
            f"{r.rejected:>6d} rejected; 50 % back after {fmt(r.t50_s)}, "
            f"99 % after {fmt(r.t99_s)}, all after {fmt(r.tall_s)}"
        )
    out.append(
        "Times are after the broker returns; seed is fixed, so the run is repeatable."
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _table(head: list[str], rows: list[list[str]], markdown: bool) -> str:
    if markdown:
        lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        lines += ["| " + " | ".join(r) + " |" for r in rows]
        return "\n".join(lines)
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(head)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(head, widths, strict=True))]
    lines += [
        "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("airtime", help="LoRa time on air per spreading factor")
    p.add_argument("--bytes", type=int, default=11, help="application payload bytes")
    p.add_argument("--markdown", action="store_true")

    p = sub.add_parser("protocol", help="bytes per status record by transport")
    p.add_argument("--body", type=int, default=84)
    p.add_argument("--keepalive-s", type=int, default=60)
    p.add_argument("--markdown", action="store_true")

    for name in ("budget", "select"):
        p = sub.add_parser(name)
        p.add_argument("nodes", type=Path)
        p.add_argument("links", type=Path)
        p.add_argument("--markdown", action="store_true")

    p = sub.add_parser("backoff", help="reconnect schedule and the herd")
    p.add_argument("--nodes", type=int, default=1000)
    p.add_argument("--capacity", type=int, default=50)
    p.add_argument("--no-jitter", action="store_true")
    p.add_argument("--seed", type=int, default=1)

    args = ap.parse_args(argv)
    if args.cmd == "airtime":
        print(airtime_report(args.bytes, args.markdown))
    elif args.cmd == "protocol":
        print(protocol_report(args.body, args.keepalive_s, args.markdown))
    elif args.cmd == "budget":
        print(budget_report(load_nodes(args.nodes), args.links, args.markdown))
    elif args.cmd == "select":
        print(select_report(load_nodes(args.nodes), args.links, args.markdown))
    else:
        print(backoff_report(args.nodes, args.capacity, not args.no_jitter, args.seed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
