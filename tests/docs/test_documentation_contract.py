from pathlib import Path


def test_docs_state_v1_boundaries_and_never_show_special_key_api() -> None:
    all_docs = "\n".join(path.read_text() for path in Path("docs").glob("*.md"))
    assert "termflow new" in all_docs
    assert "/panes/{pane_id}/input" in all_docs
    assert "不持久化" in all_docs
    assert "/keys" not in all_docs
    assert "Kafka 是 V1 必需" not in all_docs
    assert "一个 `termflow new`" in all_docs


def test_readme_links_every_operator_document() -> None:
    readme = Path("README.md").read_text()
    for name in (
        "architecture.md",
        "protocol.md",
        "security.md",
        "api-examples.md",
        "web-client.md",
        "operations.md",
        "troubleshooting.md",
    ):
        assert name in readme


def test_docs_explain_computer_term_and_full_terminal_contracts() -> None:
    all_docs = "\n".join(path.read_text() for path in Path("docs").glob("*.md"))
    for phrase in (
        "Computer",
        "Term",
        "/api/v1/admin/sessions",
        "/api/v1/admin/session",
        "/api/v1/terms/{instance_id}/terminal",
        "A 权威",
        "HttpOnly",
        "Origin",
        "graphite-signal",
        "cloud-cobalt",
        "midnight-indigo",
    ):
        assert phrase in all_docs
    assert "Web C 是 B 的内部页面" not in all_docs


def test_docs_keep_terminal_content_and_disconnect_boundaries_explicit() -> None:
    security = Path("docs/security.md").read_text()
    architecture = Path("docs/architecture.md").read_text()
    assert "B 不持久化终端" in security
    assert "C 不能改变" in architecture
    assert "继续运行" in architecture


def test_operations_docs_define_external_edge_secrets_and_native_toolchains() -> None:
    operations = Path("docs/operations.md").read_text()
    readme = Path("README.md").read_text()
    clients = Path("apps/clients/README.md").read_text()
    env_example = Path(".env.example").read_text()

    assert "[.env.example](.env.example)" in readme
    assert "deploy/env.example" not in readme
    assert "TERMFLOW_PUBLIC_BASE_URL" in env_example
    assert "TERMFLOW_TRUSTED_WEB_ORIGINS" in env_example
    assert "反向代理" in env_example
    for boundary in ("DNS", "反向代理", "TLS", "mTLS", "不属于 TermFlow"):
        assert boundary in operations
    assert "TERMFLOW_TOTP_MASTER_KEY" in operations
    assert "TERMFLOW_TOTP_AUTO_MASTER_KEY_FILE" not in env_example
    assert "多 B" in operations
    assert "0600" in operations
    assert "自动创建" in operations
    assert (
        "docker compose --env-file .env -f deploy/compose.yaml exec control-plane "
        "termflow-control auth totp reset"
    ) in operations
    for platform in ("Linux", "Windows", "macOS", "Android", "iOS"):
        assert platform in clients
    assert "Node 22.23.2" in clients
    assert "不能" in clients and "跨平台" in clients
    assert "TERMFLOW_TOTP_MASTER_KEY=" not in env_example


def test_operations_docs_explain_manual_and_release_client_boundaries() -> None:
    operations = Path("docs/operations.md").read_text()
    for phrase in (
        "Tauri Multi-platform Packages",
        "Actions artifact",
        "GitHub Release",
        "14 天",
        "*-setup.exe",
        "SmartScreen",
        "未知发布者",
        "Control Plane Docker 镜像不包含这个安装包",
        "--bundles nsis",
    ):
        assert phrase in operations


def test_docs_distinguish_test_artifacts_from_permanent_release_assets() -> None:
    readme = Path("README.md").read_text()
    operations = Path("docs/operations.md").read_text()
    troubleshooting = Path("docs/troubleshooting.md").read_text()

    for phrase in (
        "GitHub Release",
        "GHCR",
        "Actions artifact",
        "iOS Simulator",
        "install-termflow-node.sh",
        "docker compose pull",
        "tmux 3.2",
        "systemd",
    ):
        assert phrase in operations
    assert "install-termflow-node.sh" in readme
    assert "TERM_FLOW" not in readme
    assert "SHA256" in troubleshooting
