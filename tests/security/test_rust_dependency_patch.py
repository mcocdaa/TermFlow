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
