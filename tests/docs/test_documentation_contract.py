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
        "troubleshooting.md",
    ):
        assert name in readme
