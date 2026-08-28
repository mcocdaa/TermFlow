from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATCHED_SOURCE = ROOT / "vendor/glib-0.18.5/src/variant_iter.rs"


def test_glib_variant_string_iterator_uses_mutable_out_pointer() -> None:
    source = PATCHED_SOURCE.read_text(encoding="utf-8")
    assert "let mut p: *mut libc::c_char = std::ptr::null_mut();" in source
    assert "                &mut p," in source
    assert "                &p," not in source


def test_tauri_cargo_uses_the_patched_glib_source() -> None:
    cargo = (ROOT / "apps/clients/tauri/src-tauri/Cargo.toml").read_text(
        encoding="utf-8"
    )
    assert "[patch.crates-io]" in cargo
    assert 'glib = { path = "../../../../vendor/glib-0.18.5" }' in cargo


def test_rust_dependency_audit_is_patch_gated_locally_and_in_ci() -> None:
    audit_script = (
        ROOT / "scripts/security/verify-rust-dependencies.sh"
    ).read_text(encoding="utf-8")
    local_verify = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for required_check in (
        "let mut p: *mut libc::c_char = std::ptr::null_mut();",
        "&mut p,",
        "variant_iter::tests::test_variant_iter_array",
        "test_variant_str_iter_nth",
        "cargo tree",
        "vendor/glib-0.18.5",
    ):
        assert required_check in audit_script
        assert audit_script.index(required_check) < audit_script.index("cargo audit")
    assert "--ignore RUSTSEC-2024-0429" in audit_script
    assert "https://codeload.github.com/RustSec/advisory-db/" in audit_script
    assert "--proto '=https' --proto-redir '=https' --tlsv1.2" in audit_script
    assert "--no-fetch" in audit_script
    assert "git@" not in audit_script
    assert "scripts/security/verify-rust-dependencies.sh" in local_verify
    assert "cargo install cargo-audit --locked --version 0.22.2" in ci
    assert "Verify Rust dependency advisories" in ci
    assert "scripts/security/verify-rust-dependencies.sh" in ci
