"""Chapter 7 - provisioning, identity and secrets.

Standard-library only:

    python identity.py inventory inventory.json        # where each value may live, and the gaps
    python identity.py inventory inventory_real.json   # the two real systems, as their READMEs state them
    python identity.py blast                           # what one compromise exposes, per credential design
    python identity.py provision                       # the claim flow and nine authorisation scenarios
    python identity.py rotate --fleet 1000             # what a rotation costs, per design
    python identity.py scan samples                    # find credentials in a source tree (exit 1 if any)

``inventory`` is chapter 1's checker again: every value a system needs is
classified (public identifier or secret; fleet-, site-, device- or
user-scoped) and five home rules say where it may live.  ``blast`` derives,
from four properties of a credential design, what an attacker gets from each
of six events.  ``provision`` is a model of the claim flow: a server mints a
per-device token, the device exchanges it for a short-lived one, and a rules
function decides every read and write; the tokens are HMAC-signed JSON, a
stand-in for Firebase custom tokens or MQTT client certificates, not a
product.  ``rotate`` is arithmetic on a fleet.  ``scan`` is the grep every
repository should run in CI.  Nothing here talks to a real cloud.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. inventory: what a value is and where it may live
# ---------------------------------------------------------------------------

KINDS = {"identifier", "secret"}
SCOPES = {"fleet", "site", "device", "user"}
HOMES = {"source", "image", "nvs", "secure_element", "label", "server", "phone"}
HOLDERS = {"device", "app", "server", "person"}

# Where a value is readable from, once it is there.  "public" means anyone
# who can see the repository or download a firmware image; "unit" means
# anyone holding that one unit; "operator" means the people who run the server.
EXPOSURE = {
    "source": "public",
    "image": "public",
    "nvs": "unit",
    "secure_element": "unit, not copyable",
    "label": "unit",
    "phone": "that phone",
    "server": "operator",
}

HOME_RULES = {
    "H1": "an identifier may live anywhere; it is not what protects anything",
    "H2": "a secret never lives in source or in a firmware image: both are public",
    "H3": "a secret held by a device is scoped to that device, never to the fleet",
    "H4": "a device-scoped secret lives in NVS or a secure element and arrives by provisioning",
    "H5": "a secret people type (a setup password) is per unit, on its label, not a default",
}


def load_inventory(path: str | Path) -> dict:
    spec = json.loads(Path(path).read_text())
    for item in spec["items"]:
        for key, allowed in (
            ("kind", KINDS),
            ("scope", SCOPES),
            ("lives_in", HOMES),
            ("holder", HOLDERS),
        ):
            if item[key] not in allowed:
                raise ValueError(
                    f"{item['name']}: {key} {item[key]!r} not in {sorted(allowed)}"
                )
    return spec


def judge(item: dict) -> tuple[str, str]:
    """Return (verdict, rule) for one inventory item."""
    if item["kind"] == "identifier":
        return "ok", "H1"
    if item["lives_in"] in ("source", "image"):
        return "gap", "H2"
    if item["holder"] == "device" and item["scope"] == "fleet":
        return "gap", "H3"
    if item["holder"] == "person" and item["scope"] != "device":
        return "gap", "H5"
    if item["holder"] == "device" and item["lives_in"] not in ("nvs", "secure_element"):
        return "gap", "H4"
    return "ok", {"device": "H4", "person": "H5"}.get(item["holder"], "H2")


def inventory_rows(spec: dict) -> list[dict]:
    rows = []
    for item in spec["items"]:
        verdict, rule = judge(item)
        rows.append(
            {
                **item,
                "exposed_to": EXPOSURE[item["lives_in"]],
                "verdict": verdict,
                "rule": rule,
            }
        )
    return rows


def print_inventory(spec: dict) -> int:
    rows = inventory_rows(spec)
    print(f"# {spec['system']}\n")
    print("| value | kind | scope | holder | lives in | readable by | verdict |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        verdict = "ok" if r["verdict"] == "ok" else f"**gap {r['rule']}**"
        print(
            f"| {r['name']} | {r['kind']} | {r['scope']} | {r['holder']} | "
            f"{r['lives_in']} | {r['exposed_to']} | {verdict} |"
        )
    gaps = [r for r in rows if r["verdict"] == "gap"]
    print(f"\n{len(rows)} values, {len(gaps)} gaps")
    for r in gaps:
        print(
            f"  {r['rule']} {r['name']}: {HOME_RULES[r['rule']]}; fix: {r.get('fix', '-')}"
        )
    return len(gaps)


# ---------------------------------------------------------------------------
# 2. blast: what one event exposes, per credential design
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Design:
    id: str
    name: str
    scope: str  # "fleet" or "device": what one credential unlocks
    lives_in: str  # "image", "nvs" or "secure_element"
    bearer: bool  # True if the credential itself crosses the link (token, password)
    tls_verified: bool  # True if the device checks the server's certificate


# fmt: off
DESIGNS = [
    Design("S-image", "shared secret compiled into the firmware, TLS unverified", "fleet", "image", True, False),
    Design("S-image-tls", "shared secret compiled into the firmware, roots pinned", "fleet", "image", True, True),
    Design("S-nvs", "shared secret provisioned into NVS, roots pinned", "fleet", "nvs", True, True),
    Design("T-nvs", "per-device token provisioned into NVS, roots pinned", "device", "nvs", True, True),
    Design("K-se", "per-device key in a secure element, mutual TLS", "device", "secure_element", False, True),
]
# fmt: on

EVENTS = [
    ("repo", "the repository is public or a laptop is lost"),
    ("image", "a firmware image is obtained (OTA download, flash dump)"),
    ("stolen", "one unit is stolen and its flash is read"),
    ("mitm", "the router or DNS is hostile (MITM)"),
    ("app", "one phone with the owner's app is lost"),
    ("server", "the server's signing key or console is taken"),
]


def exposure(design: Design, event: str) -> str:
    """What the attacker holds after the event: nothing, one device, or the fleet."""
    scope = "whole fleet" if design.scope == "fleet" else "one device, until revoked"
    if event in ("repo", "image"):
        return scope if design.lives_in == "image" else "nothing"
    if event == "stolen":
        if design.lives_in == "secure_element":
            return "that unit while it is powered, until revoked"
        return scope
    if event == "mitm":
        if not design.tls_verified and design.bearer:
            return scope
        return "nothing (sees TLS, not the credential)"
    if event == "app":
        return "that user's devices until their session is revoked (independent of the device design)"
    if event == "server":
        return "whole fleet"
    raise ValueError(event)


def recovery(design: Design) -> str:
    if design.scope == "fleet":
        if design.lives_in == "image":
            return "rotate the secret: a new image on every unit, old secret valid until the last one updates"
        return "rotate the secret: re-provision every unit"
    return "revoke that device's token or key; mint one for its replacement"


def print_blast() -> None:
    head = " | ".join(e[0] for e in EVENTS)
    print(f"| design | {head} |")
    print("|---|" + "---|" * len(EVENTS))
    for d in DESIGNS:
        cells = " | ".join(exposure(d, e[0]) for e in EVENTS)
        print(f"| {d.id} {d.name} | {cells} |")
    print()
    for key, text in EVENTS:
        print(f"  {key:6s} {text}")
    print()
    for d in DESIGNS:
        print(f"  {d.id:11s} recovery: {recovery(d)}")


# ---------------------------------------------------------------------------
# 3. provision: a server that mints tokens, a device that uses them, rules that decide
# ---------------------------------------------------------------------------

ID_TOKEN_LIFETIME_S = 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Server:
    """Mints and verifies tokens; the only holder of the signing key.

    A stand-in for Firebase's Admin SDK + Identity Toolkit, or for a CA
    issuing MQTT client certificates.  Custom tokens never expire here and
    are exchanged once; ID tokens expire after an hour and are renewed with a
    refresh token the server can revoke.
    """

    def __init__(self, key: bytes, clock) -> None:
        self._key = key
        self.clock = clock
        self.refresh_tokens: dict[str, dict] = {}
        self.disabled: set[str] = set()
        self.db: dict = {"devices": {}, "users": {}}

    def _sign(self, claims: dict) -> str:
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        sig = hmac.new(self._key, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(sig)}"

    def verify(self, token: str) -> dict | None:
        """Claims if the signature holds and the token has not expired, else None."""
        try:
            payload_b64, sig_b64 = token.split(".")
            payload = _unb64(payload_b64)
        except ValueError:
            return None
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(sig_b64)):
            return None
        claims = json.loads(payload)
        if "exp" in claims and claims["exp"] <= self.clock():
            return None
        return claims

    # --- the claim flow -----------------------------------------------------

    def claim(self, device_id: str, uid: str) -> str:
        """An owner claims a device: record ownership, mint its custom token."""
        node = self.db["devices"].setdefault(device_id, {})
        node.setdefault("owners", {})[uid] = True
        return self._sign(
            {
                "uid": f"dev-{device_id}",
                "device": True,
                "deviceId": device_id,
                "kind": "custom",
            }
        )

    def exchange(self, custom_token: str) -> tuple[str, str]:
        claims = self.verify(custom_token)
        if claims is None or claims.get("kind") != "custom":
            raise PermissionError("bad custom token")
        refresh = _b64(hashlib.sha256(custom_token.encode() + b"refresh").digest())
        self.refresh_tokens[refresh] = {k: v for k, v in claims.items() if k != "kind"}
        return self._id_token(self.refresh_tokens[refresh]), refresh

    def refresh(self, refresh_token: str) -> str:
        claims = self.refresh_tokens.get(refresh_token)
        if claims is None or claims.get("deviceId") in self.disabled:
            raise PermissionError("refresh token revoked")
        return self._id_token(claims)

    def _id_token(self, claims: dict) -> str:
        now = self.clock()
        return self._sign({**claims, "iat": now, "exp": now + ID_TOKEN_LIFETIME_S})

    def revoke_device(self, device_id: str) -> None:
        """Stop a device: no more refreshes, and the rules see ``disabled``."""
        self.disabled.add(device_id)
        self.db["devices"].setdefault(device_id, {})["disabled"] = True

    def user_token(self, uid: str) -> str:
        return self._id_token({"uid": uid})


STATUS_FIELDS = {"ts", "temp", "hum", "heater"}


def rules(auth: dict | None, op: str, path: str, value, db: dict) -> tuple[bool, str]:
    """Least privilege for chapter 3's streams.  Returns (allowed, rule that decided).

    device  : writes /devices/<own id>/status (overwrite) and /history/<ts> (append),
              reads /devices/<own id>/settings/desired, unless the device is disabled
    owner   : reads /devices/<id>/*, writes /devices/<id>/settings/desired
    nobody  : writes owners, disabled, or anything under another id; no root access
    """
    parts = path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "devices":
        return False, "default deny: no rule grants root or other top-level paths"
    device_id, sub = parts[1], parts[2:]
    node = db["devices"].get(device_id, {})
    if auth is None:
        return False, "default deny: anonymous"
    is_device = auth.get("device") is True and auth.get("deviceId") == device_id
    is_owner = node.get("owners", {}).get(auth.get("uid")) is True
    if is_device and node.get("disabled"):
        return False, "device disabled: root.child(devices/$id/disabled) is true"
    if op == "read":
        if is_owner:
            return True, "owner may read the device subtree"
        if is_device and sub[:2] == ["settings", "desired"]:
            return True, "device may read its desired settings"
        return False, "default deny: not an owner, not this device"
    # writes
    if sub == ["status"]:
        if not is_device:
            return False, "only the device itself writes status"
        if not isinstance(value, dict) or set(value) != STATUS_FIELDS:
            return False, ".validate: status must have exactly ts, temp, hum, heater"
        return True, "device overwrites its own status"
    if len(sub) == 2 and sub[0] == "history":
        if not is_device:
            return False, "only the device itself appends history"
        if sub[1] in node.get("history", {}):
            return False, ".validate: history is append-only (!data.exists())"
        return True, "device appends a new history key"
    if sub[:2] == ["settings", "desired"]:
        if is_owner:
            return True, "owner writes desired settings"
        return False, "only an owner writes desired settings"
    if sub[:1] in (["owners"], ["disabled"]):
        return False, "owners and disabled are written by the server only"
    return False, "default deny: no rule for this path"


@dataclass
class Device:
    device_id: str
    nvs: dict = field(
        default_factory=dict
    )  # what survives a reboot: custom + refresh token
    id_token: str | None = None

    def provision(self, custom_token: str, server: Server) -> None:
        self.nvs["custom_token"] = custom_token
        self.id_token, self.nvs["refresh_token"] = server.exchange(custom_token)

    def ensure_token(self, server: Server) -> None:
        if server.verify(self.id_token or "") is None:
            self.id_token = server.refresh(self.nvs["refresh_token"])


def scenarios(clock_start: int = 1_700_000_000) -> list[dict]:
    """The claim flow for two devices, then the reads and writes that matter."""
    now = [clock_start]
    server = Server(secrets.token_bytes(32), lambda: now[0])  # fresh key, never stored
    a, b = Device("hive-1C5A00"), Device("hive-2B7F10")
    a.provision(server.claim(a.device_id, "user-mariam"), server)
    b.provision(server.claim(b.device_id, "user-mariam"), server)
    reading = {"ts": clock_start, "temp": 34.4, "hum": 63.0, "heater": True}
    status_a, status_b = "/devices/hive-1C5A00/status", "/devices/hive-2B7F10/status"
    history_a = f"/devices/hive-1C5A00/history/{clock_start}"
    desired_a = "/devices/hive-1C5A00/settings/desired"
    rows: list[dict] = []

    def attempt(name, token, op, path, value=None, expect=False) -> None:
        auth = server.verify(token) if token else None
        if token and auth is None:
            allowed, why = (
                False,
                "token rejected before the rules: bad signature or expired",
            )
        else:
            allowed, why = rules(auth, op, path, value, server.db)
        if allowed and op == "write":
            _apply(server.db, path, value)
        rows.append(
            {
                "scenario": name,
                "op": op,
                "allowed": allowed,
                "expect": expect,
                "why": why,
            }
        )

    # fmt: off
    attempt("device A writes its own status", a.id_token, "write", status_a, reading, True)
    attempt("device A writes device B's status", a.id_token, "write", status_b, reading, False)
    attempt("device A appends a history row", a.id_token, "write", history_a, reading, True)
    attempt("device A overwrites that history row", a.id_token, "write", history_a, reading, False)
    attempt("device A writes a status with an extra field", a.id_token, "write", status_a, {**reading, "admin": True}, False)
    attempt("device A grants itself a new owner", a.id_token, "write", "/devices/hive-1C5A00/owners/attacker", True, False)
    attempt("anonymous reads device A", None, "read", status_a, None, False)
    attempt("owner writes desired settings for A", server.user_token("user-mariam"), "write", desired_a, {"target": 34.5}, True)
    attempt("device A reads its desired settings", a.id_token, "read", desired_a, None, True)
    now[0] += ID_TOKEN_LIFETIME_S + 1
    attempt("device A writes with an expired token", a.id_token, "write", status_a, reading, False)
    a.ensure_token(server)
    attempt("device A refreshes and writes again", a.id_token, "write", status_a, reading, True)
    tampered = a.id_token.split(".")[0] + "." + _b64(b"\0" * 32)
    attempt("a forged signature on A's claims", tampered, "write", status_a, reading, False)
    b.ensure_token(server)
    server.revoke_device("hive-2B7F10")
    attempt("device B, revoked, writes with its still-valid token", b.id_token, "write", status_b, reading, False)
    # fmt: on
    now[0] += ID_TOKEN_LIFETIME_S + 1
    try:
        b.ensure_token(server)
        refreshed = True
    except PermissionError:
        refreshed = False
    rows.append(
        {
            "scenario": "device B, revoked, asks for a new token",
            "op": "refresh",
            "allowed": refreshed,
            "expect": False,
            "why": "refresh token revoked by the server",
        }
    )
    return rows


def _apply(db: dict, path: str, value) -> None:
    node = db
    parts = path.strip("/").split("/")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def print_provision() -> int:
    print("claim flow (one unit)")
    for step in (
        "1. unit boots with no credential: identity = chip id (efuse MAC), opens its setup hotspot",
        "2. owner's app, signed in, joins the hotspot (per-unit password from the label), reads the id",
        "3. app asks the server to claim <id> for <uid>; server writes owners/<uid> and mints a custom token",
        "4. app sends Wi-Fi credentials + custom token to the unit; unit stores both in NVS, never in flash code",
        "5. unit exchanges the custom token for an ID token (1 h) + refresh token; every request carries the ID token",
        "6. every hour: refresh; on 'revoked': stop writing, show it on the LCD; the rules deny meanwhile",
    ):
        print("  " + step)
    rows = scenarios()
    print("\n| scenario | op | allowed | expected | rule that decided |")
    print("|---|---|---|---|---|")
    bad = 0
    for r in rows:
        mark = "" if r["allowed"] == r["expect"] else " **MISMATCH**"
        bad += r["allowed"] != r["expect"]
        print(
            f"| {r['scenario']} | {r['op']} | {'allow' if r['allowed'] else 'deny'} | {'allow' if r['expect'] else 'deny'}{mark} | {r['why']} |"
        )
    print(f"\n{len(rows)} scenarios, {bad} mismatches")
    return bad


# ---------------------------------------------------------------------------
# 4. rotate: what a rotation costs, per design
# ---------------------------------------------------------------------------


def rotation(
    design: Design, fleet: int, rollout_days: float, visit_days: float, token_h: float
) -> dict:
    """Devices touched, how long the leaked credential keeps working, device-days offline.

    Shared secret in the image: the new secret ships in a new image; units
    update over ``rollout_days`` and the old secret must stay valid until the
    last one has, so the leak stays usable for the whole rollout.  Cut it
    off earlier and every unit not yet updated is offline until it is.
    Shared secret in NVS: someone must visit every unit (``visit_days``).
    Per-device: revoke one; a fleet-wide signing-key rotation with an
    overlap of one token lifetime touches nothing and takes nobody offline.
    """
    if design.scope == "fleet":
        if design.lives_in == "image":
            return {
                "touch": fleet,
                "leak_usable_days": rollout_days,
                "offline_device_days_if_cut_now": fleet * rollout_days / 2,
            }
        return {
            "touch": fleet,
            "leak_usable_days": visit_days,
            "offline_device_days_if_cut_now": fleet * visit_days / 2,
        }
    return {
        "touch": 1,
        "leak_usable_days": token_h / 24 if design.id == "T-nvs" else 0.0,
        "offline_device_days_if_cut_now": 0.0,
    }


def _days(days: float) -> str:
    if days == 0:
        return "0"
    return f"{days:g} days" if days >= 1 else f"{days * 24:g} h"


def print_rotate(
    fleet: int, rollout_days: float, visit_days: float, token_h: float
) -> None:
    print(
        f"fleet {fleet}, OTA rollout {rollout_days:g} days, site visits {visit_days:g} days, "
        f"ID token {token_h:g} h; one credential leaked"
    )
    print(
        "\n| design | devices to touch | leak usable for | device-days offline if cut now |"
    )
    print("|---|---|---|---|")
    for d in DESIGNS:
        r = rotation(d, fleet, rollout_days, visit_days, token_h)
        print(
            f"| {d.id} {d.name} | {r['touch']} | {_days(r['leak_usable_days'])} | {r['offline_device_days_if_cut_now']:g} |"
        )
    print(
        "\nT-nvs's leak window is the ID token lifetime only if the rules do not read `disabled`; "
        "with that rule it is zero, as the provision table shows."
    )


# ---------------------------------------------------------------------------
# 5. scan: find credentials in a tree
# ---------------------------------------------------------------------------

PLACEHOLDER = re.compile(
    r"(?i)your[-_ ]|paste|change[-_ ]?me|example|placeholder|xxx|^<|\.\.\.|^0+$|^$"
)

PATTERNS = [
    ("secret", "auth query parameter", re.compile(r"[?&]auth=([^\s\"'&]+)")),
    (
        "secret",
        "C define",
        re.compile(
            r"#define\s+\w*(?:PASS|PASSWORD|SECRET|TOKEN|AUTH|API_KEY)\w*\s+\"([^\"]*)\""
        ),
    ),
    (
        "secret",
        "assignment",
        re.compile(
            r"(?i)\b\w*(?:password|passwd|secret|token|api[_-]?key)\w*\s*[:=]\s*[\"']?([^\"'\s,;]+)"
        ),
    ),
    (
        "secret",
        "private key block",
        re.compile(r"(-----BEGIN (?:RSA |EC )?PRIVATE KEY-----)"),
    ),
    (
        "identifier",
        "database URL",
        re.compile(r"(https://[\w.-]+\.(?:firebaseio\.com|firebasedatabase\.app))"),
    ),
]

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}


def scan_text(text: str, name: str = "-") -> list[dict]:
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for kind, what, pattern in PATTERNS:
            for m in pattern.finditer(line):
                value = m.group(1)
                if kind == "secret" and PLACEHOLDER.search(value):
                    continue
                findings.append(
                    {
                        "file": name,
                        "line": lineno,
                        "kind": kind,
                        "what": what,
                        "value": value,
                    }
                )
    return findings


def scan_tree(root: str | Path, exclude: tuple[str, ...] = ()) -> list[dict]:
    root = Path(root)
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(
            part in SKIP_DIRS or part in exclude for part in path.parts
        ):
            continue
        try:
            text = path.read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_text(text, str(path.relative_to(root))))
    return findings


def print_scan(root: str, exclude: tuple[str, ...]) -> int:
    findings = scan_tree(root, exclude)
    secrets = [f for f in findings if f["kind"] == "secret"]
    for f in findings:
        shown = f["value"][:6] + "…" if f["kind"] == "secret" else f["value"]
        print(f"{f['kind']:10s} {f['file']}:{f['line']}  {f['what']}: {shown}")
    print(
        f"\n{len(secrets)} secrets, {len(findings) - len(secrets)} identifiers (identifiers are fine in source; the rules protect the data)"
    )
    return len(secrets)


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inventory")
    p.add_argument("spec")
    sub.add_parser("blast")
    sub.add_parser("provision")
    p = sub.add_parser("rotate")
    p.add_argument("--fleet", type=int, default=1000)
    p.add_argument("--rollout-days", type=float, default=14)
    p.add_argument("--visit-days", type=float, default=180)
    p.add_argument("--token-h", type=float, default=1)
    p = sub.add_parser("scan")
    p.add_argument("root")
    p.add_argument("--exclude", action="append", default=[])
    args = ap.parse_args(argv)

    if args.cmd == "inventory":
        print_inventory(load_inventory(args.spec))
        return 0
    if args.cmd == "blast":
        print_blast()
        return 0
    if args.cmd == "provision":
        return 1 if print_provision() else 0
    if args.cmd == "rotate":
        print_rotate(args.fleet, args.rollout_days, args.visit_days, args.token_h)
        return 0
    if args.cmd == "scan":
        return 1 if print_scan(args.root, tuple(args.exclude)) else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
