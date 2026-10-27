"""``GET /nodes/resources``: the legacy ``memory_free_gb`` field is an alias
of ``memory_available_gb`` (Linux MemAvailable), never Linux MemFree."""

from unittest.mock import MagicMock, patch

import pytest

from app.models import User, db

GIB = 1024 ** 3


@pytest.fixture
def admin_client(app):
    with app.app_context():
        admin = User(login_id='nodes-admin', is_admin=True, session_version=1)
        admin.set_password('Correct-Horse-9')
        db.session.add(admin)
        db.session.commit()
        user_id = admin.id
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = user_id
        session['session_version'] = 1
    return client


def _proxmox(memory):
    proxmox = MagicMock()
    proxmox.nodes.get.return_value = [{'node': 'node1'}]
    proxmox.nodes.return_value.status.get.return_value = {
        'cpu': 0.1, 'uptime': 1000, 'cpuinfo': {'cpus': 8}, 'memory': memory}
    return proxmox


def _get(client, memory):
    with patch('app.routes.node_resources.get_proxmox_connection',
               return_value=_proxmox(memory)):
        response = client.get('/nodes/resources')
    assert response.status_code == 200
    return response.get_json()


def test_memory_free_gb_is_memavailable_not_memfree(admin_client):
    data = _get(admin_client, {
        'total': 64 * GIB, 'used': 20 * GIB,
        'free': 2 * GIB, 'available': 40 * GIB})
    node = data['nodes'][0]
    assert node['memory_free_gb'] == 40.0
    assert node['memory_free_gb'] == node['memory_available_gb']
    assert node['memory_linux_free_gb'] == 2.0


def test_memory_free_gb_uses_total_minus_used_when_available_missing(
        admin_client):
    data = _get(admin_client, {
        'total': 64 * GIB, 'used': 20 * GIB, 'free': 2 * GIB})
    node = data['nodes'][0]
    assert node['memory_available_gb'] == 44.0
    assert node['memory_free_gb'] == 44.0
    assert node['memory_linux_free_gb'] == 2.0


def test_response_declares_memory_free_gb_deprecated(admin_client):
    data = _get(admin_client, {
        'total': 64 * GIB, 'used': 20 * GIB, 'available': 40 * GIB})
    assert data['deprecated_fields'] == {
        'memory_free_gb': 'memory_available_gb'}
