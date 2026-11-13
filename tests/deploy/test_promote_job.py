"""The portfolio workflow is read-only and cannot promote or deploy code."""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "gate.yml"


def test_workflow_has_only_the_offline_gate_and_read_only_permissions() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert set(workflow["jobs"]) == {"gate"}
    assert workflow["permissions"] == {"contents": "read"}
    assert not workflow["jobs"]["gate"].get("permissions", {}).get("contents") == "write"


def test_workflow_cannot_publish_or_promote_a_git_ref() -> None:
    text = WORKFLOW.read_text()
    assert "git push" not in text
    assert "deploy/green" not in text
    assert "contents: write" not in text
    assert "uv run pytest -q" in text
