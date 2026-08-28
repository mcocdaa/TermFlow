# TermFlow glib 0.18.5 backport

- Advisory: `RUSTSEC-2024-0429`
- Original crates.io archive SHA-256:
  `233daaf6e83ae6a12a52055f568f9d7cf4671dabb78ff9560ab6da230ce00ee5`
- Upstream correction: <https://github.com/gtk-rs/gtk-rs-core/pull/1343>
- Applied: 2026-08-28

The backport changes only the `VariantStrIter::impl_get` out pointer: `p` is
declared mutable and passed to `g_variant_get_child` as `&mut p`. The crate
version remains `0.18.5` so the existing GTK3 dependency graph can consume it.

Remove this vendored crate, the Cargo patch, and the patch-gated audit exception
in the same change once the resolved GTK/Tauri graph accepts `glib >=0.20` for
all Linux WebView consumers.
