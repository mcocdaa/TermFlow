from pathlib import Path


def test_docs_state_v1_boundaries_and_never_show_special_key_api() -> None:
    all_docs = "\n".join(path.read_text() for path in Path("docs").glob("*.md"))
    for boundary in (
        "termflow new",
        "/panes/{pane_id}/input",
        "/api/v1/terms/{instance_id}/terminal",
        "不持久化",
        "A 权威",
        "继续运行",
    ):
        assert boundary in all_docs
    for obsolete in ("/keys", "Kafka 是 V1 必需", "Web C 是 B 的内部页面"):
        assert obsolete not in all_docs


def test_readme_links_every_operator_document() -> None:
    readme = Path("README.md").read_text()
    for name in (
        "architecture.md",
        "protocol.md",
        "security.md",
        "api-examples.md",
        "web-client.md",
        "operations.md",
        "github-actions.md",
        "troubleshooting.md",
    ):
        assert name in readme
    assert "docs/superpowers/README.md" in readme
    assert "[.env.example](.env.example)" in readme


def test_current_operator_docs_do_not_recommend_removed_runtime_variables() -> None:
    current_docs = "\n".join(
        Path(path).read_text()
        for path in (
            "README.md",
            "docs/architecture.md",
            "docs/protocol.md",
            "docs/security.md",
            "docs/api-examples.md",
            "docs/web-client.md",
            "docs/operations.md",
            "docs/troubleshooting.md",
            "apps/clients/README.md",
            ".env.example",
        )
    )
    for obsolete in (
        "TERMFLOW_TRUSTED_WEB_ORIGINS",
        "TERMFLOW_IMAGE",
        "deploy/env.example",
    ):
        assert obsolete not in current_docs
    assert "TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE" not in Path(".env.example").read_text()


def test_operator_docs_keep_current_install_release_and_native_contracts() -> None:
    current_docs = "\n".join(
        Path(path).read_text()
        for path in (
            "README.md",
            "docs/operations.md",
            "docs/github-actions.md",
            "docs/troubleshooting.md",
            "apps/clients/README.md",
        )
    )
    for contract in (
        "docker compose --env-file .env -f deploy/compose.yaml up -d --build",
        "termflow-control auth totp reset",
        "Package A · Linux Node",
        "Package B + Web C · Control Plane",
        "Package C · Native Clients",
        "workflow_dispatch",
        "workflow_call",
        "Actions artifact",
        "GitHub Release",
        "GHCR",
        "termflow-node-linux-x86_64",
        "tmux 3.2",
        "Node 22.23.2",
        "Git Tag > TERMFLOW_BUILD_VERSION > 0.0.1-dev.0",
    ):
        assert contract in current_docs


def test_operator_docs_explain_native_device_authorization_and_windows_replacement() -> None:
    web_client = Path("docs/web-client.md").read_text()
    operations = Path("docs/operations.md").read_text()
    github_actions = Path("docs/github-actions.md").read_text()

    for contract in (
        "申请注册远程控制",
        "在其他设备上授权",
        "15 分钟",
        "二维码",
        "Admin Token 只在 Web C 登录时使用",
    ):
        assert contract in web_client
    for contract in (
        "%LOCALAPPDATA%\\\\termflow\\\\Logs\\\\termflow.log",
        "覆盖安装",
        "15 分钟",
    ):
        assert contract in operations
    for contract in (
        "Package C · Native Clients",
        "Windows x64 · NSIS",
        "GitHub Release",
    ):
        assert contract in github_actions


def test_readme_documents_offline_artifact_install_and_local_image_run() -> None:
    readme = Path("README.md").read_text()

    for phrase in (
        "Path.cwd().as_uri()",
        "docker load -i termflow-control-plane.tar",
        "docker run -d --name termflow-control-plane",
        'IMAGE_NAME="$(docker load -i termflow-control-plane.tar',
        'TERMFLOW_ALLOW_INSECURE_LOOPBACK="${TERMFLOW_ALLOW_INSECURE_LOOPBACK:-true}"',
        "curl -fsS http://127.0.0.1:8765/healthz",
    ):
        assert phrase in readme
