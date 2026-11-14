from datetime import datetime

import pytest

from app.backends.cloud_common import (
    TAG_CREATED, TAG_DEPLOYMENT, TAG_EXPIRES, TAG_KIND, TAG_SITE, TAG_SLOT,
    TAG_VMID, deployment_key, deployment_network_name, expires_epoch,
    instance_name, router_lock_key, site_prefix, slot_lock_key,
    slot_network_name, validate_site_id,
)


# Catches provider names exceeding GCE's syntax/length boundary.
def test_names_and_site_validation_are_stable():
    validate_site_id('site-a')
    assert instance_name('site-a', 123) == 'lp-site-a-123'
    assert deployment_network_name('site-a', 'a' * 32) == 'lp-site-a-d-' + 'a' * 16
    assert slot_network_name('site-a', 101) == 'lp-site-a-s-101'
    assert site_prefix('site-a') == 'lp-site-a-'


# Catches site ids that would produce invalid or colliding provider names.
@pytest.mark.parametrize('site', ['', 'A', '-a', 'a-', 'a_b', 'a' * 21, 'a b'])
def test_invalid_site_ids_are_refused(site):
    with pytest.raises(ValueError):
        validate_site_id(site)
    with pytest.raises(ValueError):
        site_prefix(site)


def test_longest_site_still_fits_gce_name_limit():
    site = 'a' * 20
    assert len(deployment_network_name(site, 'f' * 32)) <= 63
    assert len(instance_name(site, 99999999)) <= 63


# Catches lock collisions between VMIDs, routers, and backend slots.
def test_reserved_lock_keys_are_disjoint():
    assert router_lock_key('aws') == -1
    assert router_lock_key('gcp') == -2
    assert slot_lock_key('aws', 101) == -10101
    assert slot_lock_key('gcp', 101) == -20101
    keys = [router_lock_key(b) for b in ('aws', 'gcp')] + [
        slot_lock_key(b, s) for b in ('aws', 'gcp') for s in range(501)]
    assert len(set(keys)) == len(keys) and all(k < 0 for k in keys)


def test_lock_keys_refuse_unknown_backend():
    with pytest.raises(ValueError):
        router_lock_key('proxmox')
    with pytest.raises(ValueError):
        slot_lock_key('proxmox', 1)


# Catches the sweeper comparing a 16-character firewall key with 32-character ids (C1).
def test_deployment_key_is_the_name_suffix_and_idempotent():
    assert deployment_key('0123456789abcdef' * 2) == '0123456789abcdef'
    assert deployment_key(deployment_key('f' * 32)) == 'f' * 16
    assert deployment_network_name('site-a', 'f' * 32).endswith('-d-' + deployment_key('f' * 32))


# Catches leaked SGs/subnets being unsweepable for lack of a creation time (C2).
def test_created_tag_key_is_fixed():
    assert TAG_CREATED == 'lab-playground-created'


# Catches a tag rename silently orphaning resources tagged under the old key.
def test_tag_keys_are_fixed():
    assert (TAG_SITE, TAG_VMID, TAG_DEPLOYMENT, TAG_EXPIRES, TAG_SLOT, TAG_KIND) == (
        'lab-playground-site', 'lab-playground-vmid', 'lab-playground-deployment',
        'lab-playground-expires', 'lab-playground-slot', 'lab-playground-kind')


# Catches expiry being converted as UTC instead of naive local time.
def test_expires_epoch_reads_naive_datetimes_as_local():
    dt = datetime(2026, 11, 4, 12, 30, 15)
    assert expires_epoch(dt) == int(dt.timestamp())
    assert isinstance(expires_epoch(dt), int)
