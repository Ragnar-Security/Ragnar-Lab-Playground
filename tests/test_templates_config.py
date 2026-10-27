"""Validation tests for the production VM deployment catalog."""

from pathlib import Path
import pytest

import yaml
from app.template_manager import validate_template_config


REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATHS = [
    REPO_ROOT / "tests" / "fixtures" / "templates.yaml",
    REPO_ROOT / "templates.example.yaml",
]


@pytest.mark.parametrize("catalog_path", CATALOG_PATHS, ids=["test-fixture", "example"])
def test_enabled_groups_have_consistent_node_maps_and_positive_resources(catalog_path):
    config = yaml.safe_load(catalog_path.read_text())
    errors, warnings = validate_template_config(config)
    assert errors == []
    assert warnings == []

    for group in config["deployment_groups"]:
        if not group.get("enabled", False):
            continue
        for template in group.get("templates", []):
            template_nodes = set(template.get("template_locations", {}))
            storage_nodes = set(template.get("storage_locations", {}))
            assert template_nodes == storage_nodes, (
                f"{group['display_name']}/{template.get('name')} has mismatched "
                f"template nodes {sorted(template_nodes)} and storage nodes "
                f"{sorted(storage_nodes)}"
            )
            for field in ("memory", "cores"):
                value = template.get(field)
                assert type(value) is int and value > 0, (
                    f"{group['display_name']}/{template.get('name')} {field} "
                    f"must be a positive integer, got {value!r}"
                )
