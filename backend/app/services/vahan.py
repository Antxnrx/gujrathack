"""Check a plate we read against the vehicle that plate is registered to.

The idea
--------
ANPR gives a string. The national register gives what that string *is*: maker,
model, class, colour, fuel, the RTO that issued it. Putting the two side by side
does three things at once, and only the first is the obvious one.

  1. **It fills the record.** "GJ11CO5760" becomes "GJ 11 CO 5760 — Maruti
     Suzuki S-Cross, white, registered Junagadh 2019", which is what an officer
     needs to write in a case file.
  2. **It checks our own claims.** This system says a vehicle is a white car and
     is measurably unreliable about that (see ``scripts/regrade_appearance.py``).
     The register is authoritative. A disagreement tells an operator which of the
     two to doubt — and because the console already marks *which* of its own
     appearance labels are uncertain, it can say so rather than just flagging a
     conflict.
  3. **It detects cloned plates**, which is the part worth building for. A plate
     registered to a motorcycle, read off a truck, is not a misread colour — it
     is the single strongest automatic signal of a cloned or fraudulent
     registration, and no amount of ANPR accuracy finds it. The vehicle has to be
     compared to its paperwork.

Getting access, honestly
------------------------
There is no public API that answers "who owns this plate", and there should not
be. Two routes exist and only one is ours:

  * **API Setu / DigiLocker** (``https://apisetu.gov.in/transportgj/v3``, path
    ``/rvcer/certificate``) is real and is implemented below — but its required
    parameters are ``reg_no`` **and** ``chasis_no``, ``UID`` (Aadhaar) and
    ``FullName``, plus a digitally signed consent artifact. That is a citizen
    pulling their own RC into DigiLocker. From a plate alone it cannot be
    called, and it is included here so the distinction is documented rather than
    rediscovered.

  * **VAHAN, via the National Transport Repository data-sharing policy.** MoRTH's
    policy names law enforcement explicitly: agencies may receive select Vahan,
    Sarathi and e-Challan parameters "in the interest of prevention, detection,
    investigation or prosecution of any offence". Access is granted to the
    *organisation* on written application to MoRTH/NIC, not to an app. Gujarat
    Police is precisely the category the policy contemplates.

So this module ships the connector, not the credentials. ``VahanEnforcement``
talks to whatever authorised endpoint a deployment is given; until one is
configured the lookup is disabled and says so. Nothing here scrapes the public
portal — that sits behind a captcha and an OTP, and working around those would be
circumventing an access control, whoever is asking.

What is never done
------------------
No invented registrations. The offline provider returns *nothing* and explains
why; it does not manufacture a plausible RC record for the demo. A fabricated
owner name on a police screen is a far worse failure than an empty panel, and
this project has already removed one set of synthetic rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from backend.app.config import settings
from ml import rto

log = logging.getLogger("setu.vahan")


# ─────────────────────────────────────────────────────────────────────────────
# What the register returns
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Registration:
    """One vehicle as the national register holds it.

    ``authoritative`` is the field that matters on a screen. Anything false here
    must not be presented as a fact about a real vehicle, and the console renders
    it differently.
    """

    plate: str
    maker: str | None = None
    model: str | None = None
    vehicle_class: str | None = None      # "MOTOR CAR", "M-CYCLE", "GOODS"…
    colour: str | None = None
    fuel: str | None = None
    registered_on: str | None = None
    rto_name: str | None = None
    status: str | None = None             # ACTIVE / SUSPENDED / SCRAPPED
    insurance_upto: str | None = None
    pucc_upto: str | None = None
    # Owner identity is deliberately not modelled. It is the most sensitive field
    # the register holds, it is not needed to verify a vehicle against its
    # paperwork, and a deployment that is entitled to it can add it where its own
    # authorisation says it may.
    source: str = "none"
    authoritative: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def lookup(self, plate: str) -> Registration | None: ...


# ─────────────────────────────────────────────────────────────────────────────
# Providers
# ─────────────────────────────────────────────────────────────────────────────
class Unconfigured:
    """No authorised source. Says so, in the words a deploying force needs."""

    name = "unconfigured"

    def available(self) -> tuple[bool, str]:
        return False, (
            "No vehicle register is configured. Registration lookup needs "
            "authorised VAHAN access, which MoRTH/NIC grant to an enforcement "
            "agency on written application under the National Transport "
            "Repository data-sharing policy — not to an application. Set "
            "VAHAN_BASE_URL and VAHAN_API_KEY once that access is issued."
        )

    def lookup(self, plate: str) -> Registration | None:
        return None


class VahanEnforcement:
    """An authorised VAHAN endpoint, as an enforcement deployment is given one.

    The response shape is not fixed by a public contract — NIC issues the
    integration per agency — so the mapping is driven by a field map rather than
    hard-coded, and unknown fields are kept in ``raw`` so nothing is silently
    dropped.
    """

    name = "vahan"

    # The names VAHAN/e-Challan payloads use for these fields in the integrations
    # that are documented publicly. A deployment whose contract differs overrides
    # this map rather than editing the parser.
    FIELDS = {
        "maker": ("maker", "makerDesc", "vehicleManufacturerName"),
        "model": ("model", "makerModel", "vehicleModelName"),
        "vehicle_class": ("vehicleClass", "vhClass", "class"),
        "colour": ("color", "colour", "vehicleColour"),
        "fuel": ("fuel", "fuelType", "fuelDesc"),
        "registered_on": ("regDate", "registrationDate", "fromDate"),
        "rto_name": ("rtoName", "officeName", "regAuthority"),
        "status": ("status", "rcStatus", "regStatus"),
        "insurance_upto": ("insuranceUpto", "insuranceValidUpto"),
        "pucc_upto": ("puccUpto", "puccValidUpto"),
    }

    def __init__(self, base_url: str, api_key: str, timeout: float = 8.0):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout

    def available(self) -> tuple[bool, str]:
        if not self._base or not self._key:
            return False, "VAHAN_BASE_URL and VAHAN_API_KEY are not both set."
        return True, f"Authorised VAHAN endpoint at {self._base}"

    def lookup(self, plate: str) -> Registration | None:
        ok, _ = self.available()
        if not ok:
            return None
        try:
            r = httpx.get(
                f"{self._base}/vehicle/{plate}",
                headers={"Authorization": f"Bearer {self._key}",
                         "Accept": "application/json"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            log.warning("VAHAN lookup for %s failed: %s", plate, exc)
            return None
        if r.status_code == 404:
            return Registration(plate=plate, source=self.name, authoritative=True,
                                status="NOT_FOUND")
        if r.status_code != 200:
            log.warning("VAHAN lookup for %s returned %s", plate, r.status_code)
            return None

        body = r.json()
        data = body.get("data", body) if isinstance(body, dict) else {}
        out = Registration(plate=plate, source=self.name, authoritative=True, raw=data)
        for attr, names in self.FIELDS.items():
            for n in names:
                if data.get(n):
                    setattr(out, attr, str(data[n]).strip())
                    break
        return out


class ApiSetuConsent:
    """API Setu's Gujarat RC endpoint — real, and not usable from a plate alone.

    Kept so the limitation is documented in code rather than rediscovered. The
    endpoint requires the chassis number, the owner's Aadhaar and full name, and
    a signed consent artifact; ANPR supplies exactly one of those four. It is
    callable only where a citizen has consented — a counter-service flow, not a
    camera one.
    """

    name = "apisetu-consent"
    ENDPOINT = "https://apisetu.gov.in/transportgj/v3/rvcer/certificate"

    def __init__(self, api_key: str = "", client_id: str = ""):
        self._key, self._client = api_key, client_id

    def available(self) -> tuple[bool, str]:
        return False, (
            "API Setu's RC endpoint needs the chassis number, the owner's "
            "Aadhaar and full name, and a signed consent artifact. A camera "
            "supplies the registration number and nothing else, so this route "
            "cannot answer a plate lookup. It is the citizen's own DigiLocker "
            "flow, not an enforcement one."
        )

    def lookup(self, plate: str) -> Registration | None:
        return None


def build() -> Provider:
    base = getattr(settings, "vahan_base_url", "") or ""
    key = getattr(settings, "vahan_api_key", "") or ""
    if base and key:
        return VahanEnforcement(base, key)
    return Unconfigured()


# ─────────────────────────────────────────────────────────────────────────────
# The comparison — the part that finds cloned plates
# ─────────────────────────────────────────────────────────────────────────────
# What the register calls a vehicle, mapped to what this system's detector calls
# one. VAHAN's vocabulary is regulatory ("M-CYCLE/SCOOTER", "LMV", "GOODS
# CARRIER"); COCO's is visual. Only the coarse buckets are compared, because
# those are the ones where a disagreement means something.
_REGISTER_TO_OBSERVED = {
    "M-CYCLE": {"motorcycle"}, "MOTOR CYCLE": {"motorcycle"}, "SCOOTER": {"motorcycle"},
    "MOPED": {"motorcycle"},
    "MOTOR CAR": {"car"}, "LMV": {"car"}, "LIGHT MOTOR VEHICLE": {"car"},
    "OMNIBUS": {"bus"}, "BUS": {"bus"}, "STAGE CARRIAGE": {"bus"},
    "GOODS": {"truck"}, "GOODS CARRIER": {"truck"}, "TRUCK": {"truck"},
    "LGV": {"truck"}, "HGV": {"truck"}, "TRAILER": {"truck"},
    "E-RICKSHAW": {"auto_rickshaw"}, "THREE WHEELER": {"auto_rickshaw"},
    "AUTO RICKSHAW": {"auto_rickshaw"},
    "TRACTOR": {"tractor"},
}

# Colours the register and a camera can reasonably disagree about without it
# meaning anything. Silver and grey and white are one another under a sodium
# lamp, and the register records the colour at first registration.
_COLOUR_NEIGHBOURS = {
    frozenset({"white", "silver"}), frozenset({"silver", "grey"}),
    frozenset({"grey", "black"}), frozenset({"white", "grey"}),
    frozenset({"blue", "black"}), frozenset({"red", "orange"}),
}


@dataclass
class Finding:
    kind: str            # class_mismatch | colour_mismatch | rto_mismatch | status
    severity: str        # critical | high | medium | low | info
    observed: str | None
    registered: str | None
    means: str


def _observed_class_of(register_class: str) -> set[str]:
    up = (register_class or "").upper()
    for key, vals in _REGISTER_TO_OBSERVED.items():
        if key in up:
            return vals
    return set()


def compare(track: dict, reg: Registration) -> list[Finding]:
    """What the footage says against what the paperwork says.

    ``track`` is a row as the API returns it, so it carries the appearance
    uncertainty flags this system already computes. Those are used rather than
    ignored: a colour disagreement where the console had *already* marked its own
    colour unreliable is a statement about our classifier, and a disagreement
    where it was confident is a statement about the vehicle. Reporting both as
    "colour mismatch" would waste an operator's time on the first and bury the
    second.
    """
    out: list[Finding] = []
    if reg.status == "NOT_FOUND":
        out.append(Finding(
            kind="not_registered", severity="high",
            observed=track.get("plate_text"), registered=None,
            means=("No vehicle is registered under this number. Either the plate "
                   "was misread — check the crop — or it is a fabricated plate."),
        ))
        return out

    # ── Class: the cloned-plate signal ──────────────────────────────────────
    observed_class = (track.get("class") or "").lower()
    expected = _observed_class_of(reg.vehicle_class or "")
    if (observed_class and expected and observed_class not in expected
            and observed_class != "vehicle"):
        contested = bool(track.get("class_uncertain"))
        out.append(Finding(
            kind="class_mismatch",
            severity="medium" if contested else "critical",
            observed=observed_class,
            registered=reg.vehicle_class,
            means=(
                "Our own two classifiers disagreed about this vehicle, so the "
                "conflict may be ours rather than the vehicle's — confirm from "
                "the crop before acting."
                if contested else
                "The vehicle filmed is not the kind of vehicle this plate is "
                "registered to. A registration cannot change category; this is "
                "the strongest automatic indication of a cloned or fabricated "
                "plate and is worth a stop."
            ),
        ))

    # ── Colour: weaker, and we know how weak ────────────────────────────────
    observed_colour = (track.get("dominant_color") or "").lower()
    reg_colour = (reg.colour or "").lower()
    if observed_colour and reg_colour:
        same = observed_colour == reg_colour
        near = frozenset({observed_colour, reg_colour}) in _COLOUR_NEIGHBOURS
        if not same and not near:
            unsure = bool(track.get("colour_uncertain"))
            out.append(Finding(
                kind="colour_mismatch",
                severity="low" if unsure else "medium",
                observed=observed_colour,
                registered=reg.colour,
                means=(
                    "This colour was already marked unreliable by the console "
                    "before the register was consulted, so the disagreement is "
                    "most likely ours."
                    if unsure else
                    "The register records the colour at first registration, so a "
                    "repaint is the ordinary explanation. Worth noting beside a "
                    "class mismatch, not on its own."
                ),
            ))

    # ── The issuing office, which the plate itself names ────────────────────
    plate_rto = rto.describe(track.get("plate_text"))
    if plate_rto and reg.rto_name and rto.district(track.get("plate_text")):
        district = rto.district(track.get("plate_text")) or ""
        if district and district.split()[0].lower() not in reg.rto_name.lower():
            out.append(Finding(
                kind="rto_mismatch", severity="low",
                observed=plate_rto, registered=reg.rto_name,
                means=("The office on the plate and the office in the register "
                       "differ. Usually a transfer of registration; occasionally "
                       "a misread digit in the RTO code."),
            ))

    # ── Paperwork state ─────────────────────────────────────────────────────
    if (reg.status or "").upper() in {"SUSPENDED", "SCRAPPED", "BLACKLISTED", "CANCELLED"}:
        out.append(Finding(
            kind="registration_status", severity="high",
            observed=None, registered=reg.status,
            means="This registration is not in good standing, and the vehicle "
                  "was on the road. Grounds for a stop in its own right.",
        ))

    if not out:
        out.append(Finding(
            kind="consistent", severity="info",
            observed=f"{observed_colour} {observed_class}".strip(),
            registered=f"{reg.colour or '?'} {reg.vehicle_class or '?'}".strip(),
            means="What was filmed matches what is registered.",
        ))
    return out
