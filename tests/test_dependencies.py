"""requirements.txt pins the exact versions of every package the Docker image installs."""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def pinned() -> dict[str, str]:
    """Each package of requirements.txt, with its version."""
    versions = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            requirement = Requirement(line)
            specifiers = list(requirement.specifier)
            assert len(specifiers) == 1 and specifiers[0].operator == "==", f"not pinned with ==: {line}"
            versions[canonicalize_name(requirement.name)] = specifiers[0].version
    return versions


def test_every_package_is_pinned_once():
    lines = [line for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
    assert len(pinned()) == len(lines) > 0


def test_the_dependencies_are_pinned_within_their_ranges():
    versions = pinned()
    for dependency in PROJECT["project"]["dependencies"] + PROJECT["build-system"]["requires"]:
        wanted = Requirement(dependency)
        name = canonicalize_name(wanted.name)
        assert name in versions, f"{wanted.name} is missing from requirements.txt"
        assert wanted.specifier.contains(versions[name]), f"requirements.txt pins {wanted.name}=={versions[name]}, outside {wanted.specifier}"
