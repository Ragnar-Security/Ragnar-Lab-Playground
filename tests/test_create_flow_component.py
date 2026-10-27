"""Component coverage for the student VM creation flow.

Only the external Proxmox constructor is replaced; node selection, template
loading, orchestration, and database persistence all run through production
code.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.models import User, VMRecord, db
from app.proxmox_service import (
    _clear_node_reservations,
    _clear_vmid_reservations,
)


GROUP_NAME = "Windows Lab"
TEMPLATES_FIXTURE = Path(__file__).parent / "fixtures" / "templates.yaml"
GIB = 1024**3


class ProxmoxBoundary:
    """Deterministic Proxmox API double with realistic node responses."""

    def __init__(self, statuses):
        self.api = MagicMock(name="ProxmoxAPI")
        self.node_apis = {}
        self.qemu_endpoints = {}
        self.api.nodes.get.return_value = [
            {"node": "node1", "status": "online"},
            {"node": "node2", "status": "online"},
        ]
        self.api.nodes.side_effect = lambda node: self.node_apis[node]

        template_vmids = {"node1": 7101, "node2": 7201}
        self.api.cluster.resources.get.return_value = [
            {
                "vmid": vmid,
                "name": "windows-workstation",
                "node": node,
                "type": "qemu",
                "status": "stopped",
                "mem": 0,
            }
            for node, vmid in template_vmids.items()
        ]
        for node, template_vmid in template_vmids.items():
            node_api = MagicMock(name=f"node-{node}")
            node_api.status.get.return_value = statuses[node]
            node_api.qemu.get.return_value = [
                {"vmid": template_vmid, "name": "windows-workstation"}
            ]
            node_api.lxc.get.return_value = []
            node_api.qemu.side_effect = (
                lambda vmid, node=node: self.qemu_endpoint(node, vmid)
            )
            node_api.tasks.side_effect = lambda _upid: self.successful_task()
            self.node_apis[node] = node_api

    @staticmethod
    def successful_task():
        task = MagicMock()
        task.status.get.return_value = {"status": "stopped", "exitstatus": "OK"}
        return task

    def qemu_endpoint(self, node, vmid):
        key = (node, vmid)
        if key not in self.qemu_endpoints:
            endpoint = MagicMock(name=f"qemu-{node}-{vmid}")
            endpoint.clone.post.return_value = f"UPID:{node}:{vmid}"
            endpoint.status.start.post.return_value = f"UPID:start:{node}:{vmid}"
            self.qemu_endpoints[key] = endpoint
        return self.qemu_endpoints[key]


@pytest.fixture
def create_client(monkeypatch):
    _clear_node_reservations()
    _clear_vmid_reservations()
    monkeypatch.setattr(
        "app.template_manager.TEMPLATES_FILE", str(TEMPLATES_FIXTURE)
    )
    app = create_app(testing=True)
    with app.app_context():
        db.create_all()
        user = User(
            login_id="componentstudent",
            full_name="Component Student",
            entry_vlan_tag=111,
            internal_vlan_tag=211,
        )
        user.set_password("password123")
        db.session.add(user)
        db.session.commit()

    client = app.test_client()
    response = client.post(
        "/auth/login",
        data={"login_id": "componentstudent", "password": "password123"},
    )
    assert response.status_code in (200, 302)

    yield app, client

    with app.app_context():
        db.session.remove()
        db.drop_all()
    _clear_node_reservations()
    _clear_vmid_reservations()


def node_status(*, cpu, available):
    total = 92 * GIB
    return {
        "cpu": cpu,
        "memory": {
            "total": total,
            "used": total - available,
            # MemFree is intentionally below the 6 GiB VM requirement; the
            # kernel can reclaim cache/ARC represented by MemAvailable.
            "free": 5 * GIB,
            "available": available,
        },
        "uptime": 86400,
        "cpuinfo": {"cpus": 24},
    }


@pytest.mark.parametrize(
    ("chosen_node", "source_vmid", "storage"),
    [
        ("node1", 7101, "local-zfs"),
        ("node2", 7201, "local-zfs"),
    ],
)
def test_create_uses_available_memory_and_node_specific_clone_settings(
    create_client, chosen_node, source_vmid, storage
):
    app, client = create_client
    other_node = "node2" if chosen_node == "node1" else "node1"
    boundary = ProxmoxBoundary(
        {
            chosen_node: node_status(cpu=0.10, available=56 * GIB),
            # Both nodes report reclaimable memory; CPU pressure makes the
            # expected node the sole suitable target, keeping selection stable.
            other_node: node_status(cpu=0.95, available=63 * GIB),
        }
    )

    with patch("app.proxmox_service.ProxmoxAPI", return_value=boundary.api):
        response = client.post("/create", data={"deployment_group": GROUP_NAME})

    assert response.status_code == 200
    assert response.get_json()["success"] is True

    endpoint = boundary.qemu_endpoint(chosen_node, source_vmid)
    endpoint.clone.post.assert_called_once_with(
        newid=7202,
        name="student-componentstudent-windows-workstation",
        full=1,
        storage=storage,
    )

    with app.app_context():
        records = VMRecord.query.all()
        assert len(records) == 1
        record = records[0]
        assert record.vmid == 7202
        assert record.node == chosen_node
        assert record.template_name == f"{GROUP_NAME}/windows-workstation"
        assert record.student_name == "componentstudent"
        assert record.memory == 6144
        assert record.cores == 4
        assert record.destroyed is False


def test_create_rejects_nodes_without_reclaimable_memory(create_client):
    app, client = create_client
    boundary = ProxmoxBoundary(
        {
            "node1": node_status(cpu=0.10, available=4 * GIB),
            "node2": node_status(cpu=0.10, available=3 * GIB),
        }
    )

    with patch("app.proxmox_service.ProxmoxAPI", return_value=boundary.api):
        response = client.post("/create", data={"deployment_group": GROUP_NAME})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is False
    assert "No suitable nodes available" in payload["error"]
    assert all(
        endpoint.clone.post.call_count == 0
        for endpoint in boundary.qemu_endpoints.values()
    )
    with app.app_context():
        assert VMRecord.query.count() == 0
