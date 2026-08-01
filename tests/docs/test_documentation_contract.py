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
        "troubleshooting.md",
    ):
        assert name in readme


def test_docs_explain_computer_term_and_full_terminal_contracts() -> None:
    all_docs = "\n".join(path.read_text() for path in Path("docs").glob("*.md"))
    for phrase in (
        "Computer",
        "Term",
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
