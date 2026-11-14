"""Slot-to-subnet arithmetic shared by the cloud backends, the Headscale
policy renderer and config validation. Pure: no app.config import."""
import ipaddress
import re
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional, Sequence

SLOT_PREFIXLEN = 26
SLOT_MAX = 500

_SLOT_SIZE = 2 ** (32 - SLOT_PREFIXLEN)
# Proxmox lab space, the tailnet CGNAT range, and Docker's default bridge.
_RESERVED = ('10.0.0.0/8', '100.64.0.0/10', '172.17.0.0/16')


def cloud_slot_cidr(base_cidr: str, slot: int) -> str:
    base = ipaddress.IPv4Network(base_cidr, strict=True)
    if not isinstance(slot, int) or not 0 <= slot <= SLOT_MAX:
        raise ValueError(f'slot {slot!r} is outside 0..{SLOT_MAX}')
    # Judged against the highest slot, not the requested one, so a base that
    # cannot hold slot 500 is refused for every slot.
    if base.num_addresses < (SLOT_MAX + 1) * _SLOT_SIZE:
        raise ValueError(f'{base_cidr} is too small to hold slot {SLOT_MAX}')
    start = int(base.network_address) + slot * _SLOT_SIZE
    return f'{ipaddress.IPv4Address(start)}/{SLOT_PREFIXLEN}'


def validate_lab_cidr(backend: str, cidr: str, *, others: Mapping[str, str]) -> None:
    name = f'{backend.upper()}_LAB_CIDR'
    try:
        network = ipaddress.IPv4Network(cidr)
    except ValueError:
        raise ValueError(f"{name} {cidr!r} is not an IPv4 network address/prefix") from None
    for reserved in _RESERVED:
        if network.overlaps(ipaddress.IPv4Network(reserved)):
            raise ValueError(f"{name} {cidr} overlaps reserved range {reserved}")
    needed = (SLOT_MAX + 1) * _SLOT_SIZE
    if network.num_addresses < needed:
        raise ValueError(
            f"{name} {cidr} is too small for slot {SLOT_MAX}: slots 0-{SLOT_MAX} as "
            f"/{SLOT_PREFIXLEN}s need {needed} addresses, a /17 or larger")
    for other, other_cidr in others.items():
        try:
            other_network = ipaddress.IPv4Network(other_cidr)
        except ValueError:
            continue  # reported when that backend's own base is validated
        if network.overlaps(other_network):
            raise ValueError(f"{name} {cidr} overlaps {other} lab CIDR {other_cidr}")


@dataclass(frozen=True)
class VlanBand:
    name: str
    first: int
    last: int
    offset: int
    third: int


def parse_vlan_bands(spec: str) -> List[VlanBand]:
    """Same grammar and safety checks as scripts/jumpbox_netgen.parse_bands."""
    bands = []
    for item in spec.split():
        match = re.fullmatch(r"([a-z][a-z0-9-]*):(\d+)-(\d+):(\d+):(\d+)", item)
        if not match:
            raise ValueError(f"bad VLAN band {item!r}; expected name:first-last:offset:third")
        name = match.group(1)
        first, last, offset, third = (int(match.group(i)) for i in range(2, 6))
        if not 2 <= first <= last <= 4094:
            raise ValueError(f"band {name}: VLAN tags must satisfy 2 <= first <= last <= 4094")
        if not (0 <= first - offset and last - offset <= 255 and 0 <= third <= 255):
            raise ValueError(f"band {name}: 10.<tag-{offset}>.{third}.0/24 is not an address for every tag")
        bands.append(VlanBand(name, first, last, offset, third))
    if not bands:
        raise ValueError("no VLAN bands configured")
    seen_tags, seen_nets = {}, {}
    for band in bands:
        for tag in range(band.first, band.last + 1):
            net = (tag - band.offset, band.third)
            if tag in seen_tags:
                raise ValueError(f"VLAN {tag} is in bands {seen_tags[tag]} and {band.name}")
            if net in seen_nets:
                raise ValueError(f"VLAN {tag} and VLAN {seen_nets[net]} map to the same subnet")
            seen_tags[tag], seen_nets[net] = band.name, tag
    return bands


def proxmox_slot_cidr(slot: int, bands: Sequence[VlanBand]) -> Optional[str]:
    for band in bands:
        if band.first <= slot <= band.last:
            return f'10.{slot - band.offset}.{band.third}.0/24'
    return None


def routed_slots(slots: Iterable[int], bands: Sequence[VlanBand]) -> List[int]:
    return sorted({s for s in slots if proxmox_slot_cidr(s, bands) is not None})
