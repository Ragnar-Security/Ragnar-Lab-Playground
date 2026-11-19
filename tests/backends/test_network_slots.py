import pytest

from app.backends.network_slots import (
    CLIENT_CIDR_MIN_PREFIXLEN, CLIENT_CIDRS_MAX, SLOT_MAX, SLOT_PREFIXLEN,
    TAILNET_CIDR, VlanBand, cloud_slot_cidr, parse_client_cidrs, parse_vlan_bands,
    proxmox_slot_cidr, routed_slots, validate_lab_cidr,
)

DEFAULT_BANDS = 'entry:100-199:0:0 internal:200-299:100:1 group-entry:300-399:200:2 group-internal:400-500:300:4'


# Catches off-by-one subnet reuse between adjacent students.
def test_cloud_slots_advance_by_exactly_one_26():
    assert cloud_slot_cidr('172.20.0.0/16', 0) == '172.20.0.0/26'
    assert cloud_slot_cidr('172.20.0.0/16', 1) == '172.20.0.64/26'
    assert cloud_slot_cidr('172.20.0.0/16', 500) == '172.20.125.0/26'
    assert (SLOT_PREFIXLEN, SLOT_MAX) == (26, 500)


# Catches accepting a network that cannot represent the highest allocated slot.
def test_cloud_slot_rejects_out_of_range_or_small_base():
    with pytest.raises(ValueError):
        cloud_slot_cidr('172.20.0.0/24', 500)
    with pytest.raises(ValueError):
        cloud_slot_cidr('172.20.0.0/16', 501)
    with pytest.raises(ValueError):
        cloud_slot_cidr('172.20.0.0/16', -1)
    with pytest.raises(ValueError):  # capacity is the base's, not the slot's
        cloud_slot_cidr('172.20.0.0/24', 0)
    with pytest.raises(ValueError):  # host bits set: strict parsing
        cloud_slot_cidr('172.20.0.1/16', 0)


# Catches Headscale policy disagreeing with the jumpbox's slot mapping.
def test_default_vlan_bands_map_documented_slots():
    bands = parse_vlan_bands(DEFAULT_BANDS)
    assert proxmox_slot_cidr(100, bands) == '10.100.0.0/24'
    assert proxmox_slot_cidr(199, bands) == '10.199.0.0/24'
    assert proxmox_slot_cidr(200, bands) == '10.100.1.0/24'
    assert proxmox_slot_cidr(250, bands) == '10.150.1.0/24'
    assert proxmox_slot_cidr(300, bands) == '10.100.2.0/24'
    assert proxmox_slot_cidr(400, bands) == '10.100.4.0/24'
    assert proxmox_slot_cidr(600, bands) is None
    assert routed_slots([600, 400, 250, 100], bands) == [100, 250, 400]


# Catches nondeterministic or duplicated policy rows.
def test_routed_slots_sorted_and_unique():
    bands = parse_vlan_bands(DEFAULT_BANDS)
    assert routed_slots([400, 100, 400, 100, 999], bands) == [100, 400]
    assert routed_slots([], bands) == []


def test_parse_vlan_bands_returns_frozen_bands():
    bands = parse_vlan_bands('entry:100-200:0:0')
    assert bands == [VlanBand('entry', 100, 200, 0, 0)]
    with pytest.raises(Exception):
        bands[0].first = 1


# Catches a malformed or colliding band spec being accepted and mis-routing slots.
@pytest.mark.parametrize('spec', [
    '', 'entry', 'Entry:100-200:0:0', 'entry:100-200:0',
    'entry:1-200:0:0', 'entry:100-4095:0:0', 'entry:200-100:0:0',
    'entry:100-200:0:256', 'entry:100-400:0:0', 'entry:100-200:101:0',
    'a:100-200:0:0 b:150-250:0:1',      # duplicate tag
    'a:100-200:0:0 b:300-400:200:0',    # duplicate resulting subnet
])
def test_parse_vlan_bands_rejects_bad_specs(spec):
    with pytest.raises(ValueError):
        parse_vlan_bands(spec)


# Catches cloud routes shadowing Proxmox, tailnet, Docker, or each other.
@pytest.mark.parametrize('cidr,others,conflict', [
    ('10.20.0.0/16', {}, '10.0.0.0/8'),
    ('100.64.0.0/16', {}, '100.64.0.0/10'),
    ('172.17.0.0/16', {}, '172.17.0.0/16'),
    ('172.20.0.0/16', {'gcp': '172.20.128.0/17'}, 'gcp'),
    ('172.20.0.0/24', {}, 'slot 500'),
    ('nonsense', {}, 'not an IPv4'),
])
def test_validate_lab_cidr_rejects_reserved_and_peer_overlaps(cidr, others, conflict):
    with pytest.raises(ValueError, match=conflict):
        validate_lab_cidr('aws', cidr, others=others)


def test_validate_lab_cidr_peer_message_is_exact():
    with pytest.raises(ValueError) as exc:
        validate_lab_cidr('aws', '172.20.0.0/16', others={'gcp': '172.20.128.0/17'})
    assert str(exc.value) == 'AWS_LAB_CIDR 172.20.0.0/16 overlaps gcp lab CIDR 172.20.128.0/17'


def test_validate_lab_cidr_accepts_disjoint_bases():
    validate_lab_cidr('aws', '172.20.0.0/16', others={'gcp': '172.21.0.0/16'})


# Catches a /17 being refused at either end of the slot range.
@pytest.mark.parametrize('slot', [0, 500])
def test_cloud_slot_accepts_smallest_base_at_both_ends(slot):
    cloud_slot_cidr('172.20.0.0/17', slot)


# ============ client networks (AWS_CLIENT_CIDRS) ============

# Catches a valid VPN pool list being refused or reordered.
def test_parse_client_cidrs_normalizes_in_order():
    assert parse_client_cidrs(' 100.64.0.0/10 , 192.0.2.0/24,198.51.100.7') == (
        '100.64.0.0/10', '192.0.2.0/24', '198.51.100.7/32')


# Catches an allow-everyone, malformed or oversized list reaching a security group.
@pytest.mark.parametrize('raw,message', [
    ('', 'at least one'),
    ('0.0.0.0/0', 'entry 1 admits every IPv4 address'),
    ('192.0.2.0/24,0.0.0.0/0', 'entry 2 admits every IPv4 address'),
    ('64.0.0.0/2', 'entry 1 is broader than a /8'),
    ('10.0.0.0/7', 'entry 1 is broader than a /8'),
    ('not-a-cidr', 'entry 1 is not an IPv4 network'),
    ('::/0', 'entry 1 is not an IPv4 network'),
    ('fd00::/8', 'entry 1 is not an IPv4 network'),
    ('192.0.2.1/24', 'host bits'),
    ('192.0.2.0/24,,198.51.100.0/24', 'entry 2 is empty'),
    ('192.0.2.0/24,192.0.2.128/25', 'entries 1 and 2 overlap'),
    (','.join(f'192.0.{i}.0/24' for i in range(CLIENT_CIDRS_MAX + 1)), 'at most 8'),
])
def test_parse_client_cidrs_refuses(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_client_cidrs(raw)


# Catches a message that echoes the value (the host preflight never prints values).
def test_parse_client_cidrs_messages_never_echo_the_value():
    with pytest.raises(ValueError) as exc:
        parse_client_cidrs('192.0.2.0/24,203.0.113.99/16')
    assert '203.0.113' not in str(exc.value)


def test_client_cidr_limits_are_the_documented_ones():
    assert (CLIENT_CIDRS_MAX, CLIENT_CIDR_MIN_PREFIXLEN, TAILNET_CIDR) == (
        8, 8, '100.64.0.0/10')
    parse_client_cidrs(','.join(f'192.0.{i}.0/24' for i in range(CLIENT_CIDRS_MAX)))
    parse_client_cidrs('11.0.0.0/8')


# Catches the tailnet staying reserved for a site that does not use it, or
# being released for one that does.
def test_tailnet_is_reserved_only_while_in_use():
    with pytest.raises(ValueError, match='100.64.0.0/10'):
        validate_lab_cidr('aws', '100.64.0.0/16', others={})
    validate_lab_cidr('aws', '100.64.0.0/16', others={}, tailnet=False)
    for always in ('10.20.0.0/16', '172.17.0.0/16'):
        with pytest.raises(ValueError, match='reserved range'):
            validate_lab_cidr('aws', always, others={}, tailnet=False)


# Catches a slot base that overlaps the networks its own labs admit.
def test_lab_cidr_overlapping_a_client_network_is_refused():
    with pytest.raises(ValueError) as exc:
        validate_lab_cidr('aws', '172.20.0.0/16', others={},
                          clients=('192.0.2.0/24', '172.20.200.0/24'))
    assert str(exc.value) == (
        'AWS_LAB_CIDR 172.20.0.0/16 overlaps AWS_CLIENT_CIDRS network 172.20.200.0/24')
    validate_lab_cidr('aws', '172.20.0.0/16', others={}, clients=('172.21.0.0/24',))
