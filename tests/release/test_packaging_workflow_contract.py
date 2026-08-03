from pathlib import Path

import yaml


NODE_WORKFLOW = Path(".github/workflows/package-node.yml")
CONTROL_PLANE_WORKFLOW = Path(".github/workflows/package-control-plane.yml")
CLIENT_WORKFLOW = Path(".github/workflows/tauri-packages.yml")


def _workflow(path: Path) -> dict[object, object]:
    return yaml.safe_load(path.read_text())


def test_node_workflow_is_manual_and_reusable() -> None:
    workflow = _workflow(NODE_WORKFLOW)
    triggers = workflow[True]

    assert workflow["name"] == "Package A · Linux Node"
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    assert triggers["workflow_dispatch"] is None
    assert triggers["workflow_call"]["inputs"]["release_tag"] == {
        "description": "Validated v-prefixed release tag; empty for manual packaging",
        "required": False,
        "default": "",
        "type": "string",
    }
    assert workflow["permissions"] == {"contents": "read"}


def test_node_workflow_owns_names_retention_and_build_commands() -> None:
    text = NODE_WORKFLOW.read_text()

    for required in (
        "termflow-node-linux-x86_64",
        "termflow-${release_tag}-node-linux-x86_64",
        "retention_days=14",
        "retention_days=1",
        "scripts/release/build_node_bundle.sh",
        "scripts/release/render_node_installer.py",
        "scripts/release/verify_node_bundle.sh",
        '--repository "$GITHUB_REPOSITORY"',
        "actions/upload-artifact@v4",
        "release-assets/SHA256SUMS",
    ):
        assert required in text
