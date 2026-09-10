from pathlib import Path

DESKTOP_ICON = Path("apps/clients/tauri/app-icon.svg")
MOBILE_ICON = Path("apps/clients/tauri/app-icon-mobile.svg")


def test_desktop_icon_keeps_the_original_terminal_glyph_scale() -> None:
    icon = DESKTOP_ICON.read_text()

    assert 'transform="translate(512 512) scale(0.9) translate(-512 -512)"' not in icon


def test_mobile_icon_uses_a_smaller_centered_terminal_glyph() -> None:
    icon = MOBILE_ICON.read_text()

    assert 'transform="translate(512 512) scale(0.9) translate(-512 -512)"' in icon
