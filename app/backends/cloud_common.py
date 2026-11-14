"""Names, tags and lock keys shared by the AWS and GCP backends.

Pure functions only: nothing here may import app.config, so the backends
package can be imported while config is still initialising.
"""
import re
from datetime import datetime

TAG_SITE = 'lab-playground-site'
TAG_VMID = 'lab-playground-vmid'
TAG_DEPLOYMENT = 'lab-playground-deployment'
TAG_EXPIRES = 'lab-playground-expires'
TAG_SLOT = 'lab-playground-slot'
TAG_KIND = 'lab-playground-kind'
# Epoch seconds as a decimal string, set on AWS security groups and subnets
# at creation so a leaked one still has an age the sweeper can judge.
TAG_CREATED = 'lab-playground-created'

_SITE_PATTERN = re.compile(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?')
_GCE_NAME_PATTERN = re.compile(r'[a-z]([-a-z0-9]*[a-z0-9])?')
_GCE_NAME_MAX = 63
_DEPLOYMENT_KEY_LEN = 16


def validate_site_id(site: str) -> None:
    if (not isinstance(site, str) or not 1 <= len(site) <= 20
            or not _SITE_PATTERN.fullmatch(site)):
        raise ValueError(
            f"site id {site!r} must be 1-20 characters of a-z, 0-9 and '-', "
            "not starting or ending with '-'")


def _checked_name(name: str) -> str:
    if len(name) > _GCE_NAME_MAX or not _GCE_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"resource name {name!r} must match [a-z]([-a-z0-9]*[a-z0-9])? "
            f"and be at most {_GCE_NAME_MAX} characters")
    return name


def site_prefix(site: str) -> str:
    validate_site_id(site)
    return f'lp-{site}-'


def instance_name(site: str, vmid: int) -> str:
    return _checked_name(f'{site_prefix(site)}{vmid}')


def deployment_key(deployment_id: str) -> str:
    """The 16-character deployment identity. GCP firewalls carry only this in
    their name, so it is the one id both clouds can report for a deployment
    network; truncating an already-truncated key is a no-op."""
    return deployment_id[:_DEPLOYMENT_KEY_LEN]


def deployment_network_name(site: str, deployment_id: str) -> str:
    return _checked_name(f'{site_prefix(site)}d-{deployment_key(deployment_id)}')


def slot_network_name(site: str, slot: int) -> str:
    return _checked_name(f'{site_prefix(site)}s-{slot}')


def expires_epoch(expires_at: datetime) -> int:
    # Naive local datetimes (repo convention); .timestamp() reads them as local.
    return int(expires_at.timestamp())


_LOCK_BASE = {'aws': 1, 'gcp': 2}


def _lock_base(backend: str) -> int:
    try:
        return _LOCK_BASE[backend]
    except KeyError:
        raise ValueError(f'no lock namespace for backend {backend!r}') from None


# Negative keys never collide with positive VMIDs in vm_lifecycle_lock.
def router_lock_key(backend: str) -> int:
    return -_lock_base(backend)


def slot_lock_key(backend: str, slot: int) -> int:
    return -(_lock_base(backend) * 10_000 + slot)
