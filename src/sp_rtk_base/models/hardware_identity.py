"""Hardware identity resolution — ``HardwareTarget``, confidence tiers, and
the one-line compatibility filter.

Identity resolves through an ordered tier ladder, each tier attached to a
confidence, because a receiver's fallback chain can otherwise store a guess
in the same field as a real read — which would let an unrecognised receiver
be confidently mis-identified as a known one:

| Tier | Source                                                | Confidence |
|------|--------------------------------------------------------|------------|
| 1    | ``MOD=`` in a MON-VER extension                         | confirmed  |
| 2    | an explicit model string in an extension                | confirmed  |
| 3    | ``HW_VERSION_MODEL_MAP[hwVersion]`` (module ID)          | confirmed  |
| 4    | firmware-family heuristic (``"HPG"`` -> ``ZED-F9P``)     | inferred   |
| 5    | ``PROTVER`` generation heuristic (family only)           | inferred   |
| —    | nothing matches                                          | unknown    |

Tiers 1-3 are deterministic reads of something the receiver reports about
itself and can never misidentify one physical unit as another. Tiers 4 and
5 are guesses, which is why ``target`` alone is never enough to satisfy a
specific-model compatibility match — see :func:`is_compatible`.

``HardwareTarget`` is deliberately a plain ``str``, not a closed enum: a
new receiver is supported by dropping a profile YAML tagged with its
``MOD=`` string, not by editing this module (tiers 1-2 pass that string
through verbatim). Only the deterministic/heuristic *fallback* tables
(tiers 3-5) are a fixed catalog, because they exist to cover receivers that
can't report `MOD=` at all.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable

from pydantic import BaseModel, Field

HardwareTarget = str

#: Token meaning "compatible with every receiver". This is the source of
#: truth — ``profile_models`` re-exports it rather than defining its own.
HARDWARE_ANY: HardwareTarget = "any"

#: Sentinel target when nothing at all resolves.
HARDWARE_UNKNOWN: HardwareTarget = "unknown"


class HardwareConfidence(str, enum.Enum):
    """How the resolved :class:`HardwareIdentity` target was obtained."""

    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


#: Deterministic hardware-version (``hwVersion``) -> model lookup (tier 3).
HW_VERSION_MODEL_MAP: dict[str, str] = {
    "00190000": "ZED-F9P",
    "001B0000": "ZED-F9R",
    "00180000": "NEO-M9N",
}

#: Firmware-family substring heuristic (tier 4). A guess, hence ``inferred``.
FIRMWARE_FAMILY_MODEL_MAP: dict[str, str] = {
    "HPG": "ZED-F9P",
    "ADR": "ZED-F9R",
}

#: Chip-generation family each known model belongs to, so a family- or
#: `any`-tagged profile can still match a specifically-identified receiver.
MODEL_FAMILY: dict[str, str] = {
    "ZED-F9P": "gen9",
    "ZED-F9R": "gen9",
    "NEO-M9N": "gen9",
}

#: Last-resort, family-only heuristic (tier 5): the ``PROTVER`` major
#: version alone, tried only once MOD=, an explicit model string,
#: hwVersion, and the firmware-family heuristic have all failed to name a
#: model. As coarse a guess as tier 4 — hence also ``inferred`` — but kept
#: separate because it names a generation, not a module, so it can never
#: satisfy a specific-model match either way.
PROTVER_MAJOR_FAMILY_MAP: dict[int, str] = {
    27: "gen9",
    28: "gen9",
    29: "gen9",
    30: "gen9",
    31: "gen9",
    32: "gen9",
    33: "gen9",
}

#: Every family token any tier above can produce. Only "gen9" is wired up —
#: no shipped receiver or PROTVER range yet backs a "gen10" token, so it's
#: left out rather than added speculatively; adding a gen10 receiver is a
#: one-line addition to the maps above, same as the X20P story.
KNOWN_FAMILY_TOKENS: frozenset[str] = frozenset(MODEL_FAMILY.values()) | frozenset(
    PROTVER_MAJOR_FAMILY_MAP.values()
)

#: Every model this app can confirm or infer by any tier — the catalog
#: ``profile_models.KNOWN_HARDWARE_MODELS`` re-exports, so a ``Profile``
#: can target any model this resolver can actually produce.
KNOWN_MODEL_TOKENS: frozenset[str] = frozenset(MODEL_FAMILY)


class HardwareIdentity(BaseModel):
    """A resolved receiver identity: a target plus how sure we are of it."""

    target: HardwareTarget = Field(
        description="A specific model, a family token, or 'unknown'"
    )
    confidence: HardwareConfidence
    families: frozenset[str] = Field(default_factory=frozenset)

    @property
    def is_specific_model(self) -> bool:
        """Whether ``target`` names an actual model rather than a family."""
        return (
            self.target != HARDWARE_UNKNOWN and self.target not in KNOWN_FAMILY_TOKENS
        )


def identity_from_target(
    target: HardwareTarget, confidence: HardwareConfidence
) -> HardwareIdentity:
    """Build a :class:`HardwareIdentity`, deriving ``families`` from *target*."""
    families: frozenset[str]
    if target in MODEL_FAMILY:
        families = frozenset({MODEL_FAMILY[target]})
    elif target in KNOWN_FAMILY_TOKENS:
        families = frozenset({target})
    else:
        families = frozenset()
    return HardwareIdentity(target=target, confidence=confidence, families=families)


def resolve_hardware_identity(
    *,
    mod: str = "",
    explicit_model: str = "",
    hw_version: str = "",
    firmware: str = "",
    protocol_version: str = "",
) -> HardwareIdentity:
    """Resolve receiver identity from parsed MON-VER fields, per the tier ladder.

    Never raises — an unresolvable receiver comes back as
    ``target="unknown"`` / ``confidence=unknown`` rather than an error, so
    connect is never gated on identity.
    """
    model = mod.strip() or explicit_model.strip()
    if model:
        return identity_from_target(model, HardwareConfidence.CONFIRMED)

    mapped = HW_VERSION_MODEL_MAP.get(hw_version.strip())
    if mapped:
        return identity_from_target(mapped, HardwareConfidence.CONFIRMED)

    for marker, guessed_model in FIRMWARE_FAMILY_MODEL_MAP.items():
        if marker in firmware:
            return identity_from_target(guessed_model, HardwareConfidence.INFERRED)

    family = _family_from_protver(protocol_version)
    if family:
        return identity_from_target(family, HardwareConfidence.INFERRED)

    return identity_from_target(HARDWARE_UNKNOWN, HardwareConfidence.UNKNOWN)


def _family_from_protver(protocol_version: str) -> str | None:
    major = protocol_version.strip().split(".", 1)[0]
    if not major.isdigit():
        return None
    return PROTVER_MAJOR_FAMILY_MAP.get(int(major))


def is_compatible(device: HardwareIdentity, profile_hardware: str) -> bool:
    """The one-line compatibility rule.

    A profile is compatible iff its ``hardware`` tag is the device's
    *confirmed* specific-model target, one of the device's families, or the
    universal ``"any"`` token. A guessed (``inferred``) or absent
    (``unknown``) identity can never satisfy the specific-model branch —
    only ``confirmed`` identity unlocks a specific-model profile.
    """
    specific_match = (
        device.confidence == HardwareConfidence.CONFIRMED
        and device.target == profile_hardware
    )
    return (
        specific_match
        or profile_hardware in device.families
        or profile_hardware == HARDWARE_ANY
    )


def incompatible_reason(device: HardwareIdentity, profile_hardware: str) -> str | None:
    """Human-readable reason a profile is greyed out, or ``None`` if compatible."""
    if is_compatible(device, profile_hardware):
        return None
    if device.confidence != HardwareConfidence.CONFIRMED:
        return (
            "receiver hardware is unconfirmed — only family- or any-tagged "
            "profiles are enabled"
        )
    return f"not for this hardware ({device.target})"


def default_selection(
    device: HardwareIdentity, profiles: Iterable[tuple[str, str]]
) -> str | None:
    """The deterministic default pick, or ``None``.

    ``profiles`` is ``(name, hardware)`` pairs in fixed display order
    (built-ins before customs, alphabetical within each group — the
    caller owns that ordering). Returns the first compatible profile's
    name, or ``None`` whenever confidence is not ``confirmed`` — the
    default is never a guess.
    """
    if device.confidence != HardwareConfidence.CONFIRMED:
        return None
    for name, hardware in profiles:
        if is_compatible(device, hardware):
            return name
    return None
