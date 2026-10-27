"""Behavior tests for the authentication deployment helper."""

import subprocess
from pathlib import Path


def test_deploy_auth_requires_operator_admin_password(tmp_path):
    (tmp_path / ".env").write_text(
        "SECRET_KEY=test-only-secret\n"
        "ADMIN_LOGIN_ID=admin\n"
        "ADMIN_USERS=admin\n"
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "deploy_auth.sh"

    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ADMIN_PASSWORD" in result.stdout
    assert "docker" not in result.stderr.lower()
