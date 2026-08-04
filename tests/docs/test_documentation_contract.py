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
        "github-actions.md",
        "troubleshooting.md",
    ):
        assert name in readme
    assert "docs/superpowers/README.md" in readme


def test_github_actions_guide_names_current_workflows_and_artifacts() -> None:
    guide = Path("docs/github-actions.md").read_text()
    for workflow in (
        "ci.yml",
        "package-node.yml",
        "package-control-plane.yml",
        "tauri-packages.yml",
        "release.yml",
    ):
        assert f"{workflow}" in guide
    for marker in (
        "workflow_dispatch",
        "workflow_call",
        "termflow-node-linux-x86_64",
        "termflow-control-plane.tar",
        "termflow-windows-x64-nsis",
        "14 天",
        "1 天",
        "GitHub Release",
        "不会创建 GitHub Release",
        "ghcr.io/<仓库所有者>/termflow-control-plane",
        "TERMFLOW_BUILD_VERSION",
        "0.0.1-dev.0",
    ):
        assert marker in guide


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
        )
    )
    assert "TERMFLOW_TRUSTED_WEB_ORIGINS" not in current_docs
    assert "TERMFLOW_IMAGE" not in current_docs


def test_api_and_native_docs_match_current_auth_transport_boundaries() -> None:
    api_examples = Path("docs/api-examples.md").read_text()
    clients = Path("apps/clients/README.md").read_text()
    operations = Path("docs/operations.md").read_text()

    assert api_examples.count('Origin: $TERMFLOW_URL') == 2
    assert 'urlsplit(os.environ["TERMFLOW_URL"])' in api_examples
    assert 'ws_scheme = "wss" if base.scheme == "https" else "ws"' in api_examples
    assert "access token in memory" in clients
    assert "refresh token plus its device" in clients
    assert 'TERMFLOW_HOST_PORT:-8765' in operations
    assert "v1.2.3+build.5` → `v1.2.3_build.5" in operations


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
    assert "TERMFLOW_IMAGE" not in env_example
    assert "TERMFLOW_TRUSTED_WEB_ORIGINS" not in env_example
    assert "反向代理" in env_example
    assert (
        "# TERMFLOW_TOTP_MASTER_KEY=replace-with-generated-base64url-key"
        in env_example
    )
    for explanation in (
        "8 小时",
        "浏览器会话",
        "一次性注册码",
        "64 KiB",
        "256 KiB/s",
        "256 条",
        "1 MiB",
        "30 秒",
    ):
        assert explanation in env_example
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
    assert "TERMFLOW_IMAGE" not in readme
    assert "TERMFLOW_IMAGE" not in Path("docs/troubleshooting.md").read_text()
    assert (
        "docker compose --env-file .env -f deploy/compose.yaml up -d --build"
        in operations
    )


def test_operations_docs_explain_manual_and_release_client_boundaries() -> None:
    operations = Path("docs/operations.md").read_text()
    for phrase in (
        "Package A · Linux Node",
        "Package B + Web C · Control Plane",
        "Package C · Native Clients",
        "Actions artifact",
        "GitHub Release",
        "14 天",
        "termflow-control-plane.tar",
        "docker load",
        'TERMFLOW_RELEASE_BASE_URL="file://$PWD"',
        "workflow_call",
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
        "up -d --build",
        "tmux 3.2",
        "systemd",
    ):
        assert phrase in operations
    assert "install-termflow-node.sh" in readme
    assert "termflow-node-linux-x86_64.tar.gz" in readme
    assert "deb/AppImage" in readme
    assert "只有 Tag Release 才会推送 GHCR" in readme
    assert "TERM_FLOW" not in readme
    assert "SHA256" in troubleshooting
    assert "手动 A" in troubleshooting
    assert "手动 B" in troubleshooting


def test_release_docs_explain_tag_environment_and_default_version_precedence() -> None:
    readme = Path("README.md").read_text()
    operations = Path("docs/operations.md").read_text()
    troubleshooting = Path("docs/troubleshooting.md").read_text()
    combined = "\n".join((readme, operations, troubleshooting))

    assert "Git Tag > TERMFLOW_BUILD_VERSION > 0.0.1-dev.0" in combined
    assert "TERMFLOW_BUILD_VERSION=1.2.3" in combined
    assert "不能触发 GHCR" in operations
    assert "不能覆盖 Tag" in operations
    assert "scripts/release/prepare_version.py" in operations
    assert "versionCode" in operations
    assert "完整逻辑版本" in operations
    assert "先让根 `package.json`" not in operations
