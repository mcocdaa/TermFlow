from __future__ import annotations

import pytest

from scripts.release.build_version import (
    DEFAULT_BUILD_VERSION,
    BuildVersion,
    resolve_build_version,
    validate_version,
)


def test_tag_wins_over_environment() -> None:
    resolved = resolve_build_version(
        tag="v1.2.3-rc.1",
        environment={"TERMFLOW_BUILD_VERSION": "9.9.9"},
    )

    assert resolved == BuildVersion(
        version="1.2.3-rc.1",
        tag="v1.2.3-rc.1",
        is_release=True,
    )


def test_environment_wins_without_tag() -> None:
    resolved = resolve_build_version(
        tag=None,
        environment={"TERMFLOW_BUILD_VERSION": "2.3.4"},
    )

    assert resolved == BuildVersion("2.3.4", "v2.3.4", False)


def test_default_is_used_without_tag_or_environment() -> None:
    assert DEFAULT_BUILD_VERSION == "0.0.0-dev.0"
    assert resolve_build_version(tag=None, environment={}) == BuildVersion(
        "0.0.0-dev.0",
        "v0.0.0-dev.0",
        False,
    )
    assert resolve_build_version(
        tag="",
        environment={"TERMFLOW_BUILD_VERSION": ""},
    ) == BuildVersion("0.0.0-dev.0", "v0.0.0-dev.0", False)


@pytest.mark.parametrize(
    "version",
    [
        "1.2.3",
        "1.2.3-dev.4",
        "1.2.3-alpha.1",
        "1.2.3-beta.2",
        "1.2.3-rc.3",
        "1.2.3+build.7",
        "1.2.3-rc.3+build.7",
    ],
)
def test_cross_ecosystem_versions_are_accepted(version: str) -> None:
    assert validate_version(version) == version


@pytest.mark.parametrize(
    "tag",
    ["v1", "1.2.3", "v1.2.3-foo.1", "latest", " v1.2.3", "v1.2.3 "],
)
def test_invalid_tags_are_rejected(tag: str) -> None:
    with pytest.raises(ValueError, match="v-prefixed"):
        resolve_build_version(tag=tag, environment={})


@pytest.mark.parametrize(
    "version",
    ["v1.2.3", "1", "1.2", "1.2.3-foo.1", "latest", " 1.2.3", "1.2.3 "],
)
def test_invalid_environment_versions_do_not_fall_back(version: str) -> None:
    with pytest.raises(ValueError, match="build version"):
        resolve_build_version(
            tag=None,
            environment={"TERMFLOW_BUILD_VERSION": version},
        )
