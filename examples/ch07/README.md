# Chapter 7 example - provisioning, identity and secrets

```bash
python identity.py inventory inventory.json        # the farm monitor's values: where each may live (0 gaps)
python identity.py inventory inventory_real.json   # the two real systems as their READMEs state them (8 gaps)
python identity.py blast                           # five credential designs x six events: what is exposed
python identity.py provision                       # the claim flow, then 14 authorisation scenarios
python identity.py rotate --fleet 1000             # what one leaked credential costs to rotate, per design
python identity.py scan samples                    # the CI grep; exits 1 on the four planted secrets
python -m pytest -q                                # forty-nine tests
```

`inventory` reads a list of values a system needs (each with a kind,
identifier or secret; a scope, fleet / site / device / user; a holder; and
where it lives) and applies [chapter 7](../../ch07-provisioning-identity-secrets.md)'s
five home rules H1–H5. `inventory_real.json` is built only from what the two
real repositories' READMEs and secrets templates say; nothing in it was
measured or read out of a device.

`blast` derives, from four properties of a credential design (fleet- or
device-scoped, where it lives, whether it crosses the link, whether the
device verifies TLS), what an attacker holds after each of six events and what
recovery costs.

`provision` is a model of the claim flow. `Server` is the only holder of a
signing key and mints HMAC-signed JSON tokens: a long-lived custom token per
device at claim time, exchanged once for a one-hour ID token and a refresh
token the server can revoke. `rules()` is least privilege for chapter 3's
streams (a device writes its own `status`, appends its own `history`, reads
its `settings/desired`; an owner writes `settings/desired`; nobody writes
`owners`; a disabled device is denied even with a valid token). The tokens
stand in for Firebase custom tokens or MQTT client certificates; they are not
a product and nothing here talks to a cloud.

`rotate` is arithmetic: devices to touch, how long a leaked credential stays
usable, and device-days offline if it is cut off at once, for a fleet size,
an OTA rollout time, a site-visit interval and a token lifetime you pass in.

`scan` walks a tree for `?auth=` parameters, credential-named `#define`s and
assignments, private-key blocks and database URLs, ignores placeholder
values (`YOUR_…`, `change-me`, `<…>`), reports database URLs as identifiers
and exits 1 if any secret remains. `samples/` holds three files named
`*.example.*`: a clean template and two with planted, obviously fake values.
