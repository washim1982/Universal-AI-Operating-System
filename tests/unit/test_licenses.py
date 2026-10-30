"""AIOS Core is Apache-2.0 and its dependencies must stay compatible (ADR-012, docs/editions.md).

A dependency under any other license needs an ADR before it is added to this allowlist.
"""

from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]

PERMISSIVE = frozenset(
    {"0BSD", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MIT", "PSF-2.0", "Zlib"}
)
_CLASSIFIERS = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}


def _license(name: str) -> str | None:
    """The distribution's SPDX license expression, from its metadata or its license file."""
    dist = distribution(name)
    metadata = dist.metadata
    if expression := metadata.get("License-Expression"):
        return expression
    for classifier in metadata.get_all("Classifier") or []:
        if classifier in _CLASSIFIERS:
            return _CLASSIFIERS[classifier]
    if (legacy := (metadata.get("License") or "").strip()) in PERMISSIVE:
        return legacy
    for file in dist.files or []:
        if "LICENSE" in file.name.upper():
            text = file.read_text() or ""
            if "Apache License" in text and "Version 2.0" in text:
                return "Apache-2.0"
            if "Permission is hereby granted, free of charge" in text:
                return "MIT"
    return None


def _allowed(expression: str) -> bool:
    """SPDX: some ``OR`` alternative whose ``AND`` terms are all permissive."""
    return any(
        all(term.strip("() ") in PERMISSIVE for term in alternative.split(" AND "))
        for alternative in expression.split(" OR ")
    )


def _installed_by_aios() -> dict[str, str | None]:
    """Everything ``pip install aios[...]`` brings in, with every optional extra."""
    root = distribution("aios")
    todo = [(root.metadata["Name"], frozenset(root.metadata.get_all("Provides-Extra") or []))]
    licenses: dict[str, str | None] = {}
    while todo:
        name, extras = todo.pop()
        key = name.lower().replace("_", "-")
        if key in licenses:
            continue
        licenses[key] = _license(name)
        for line in distribution(name).requires or []:
            requirement = Requirement(line)
            environments = [{"extra": extra} for extra in extras] or [{"extra": ""}]
            if requirement.marker is None or any(
                requirement.marker.evaluate(env) for env in environments
            ):
                todo.append((requirement.name, frozenset(requirement.extras)))
    return licenses


def test_aios_is_apache_2() -> None:
    metadata = distribution("aios").metadata
    assert metadata["License-Expression"] == "Apache-2.0"
    assert set(metadata.get_all("License-File") or []) == {"LICENSE", "NOTICE"}
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert (ROOT / "NOTICE").read_text(encoding="utf-8").startswith("AIOS Core\n")


def test_dependencies_use_permissive_licenses() -> None:
    licenses = _installed_by_aios()
    assert {"pydantic", "pyyaml", "cedarpy"} <= licenses.keys()
    rejected = {
        name: license
        for name, license in licenses.items()
        if name != "aios" and (license is None or not _allowed(license))
    }
    assert rejected == {}, "needs an ADR first (docs/editions.md)"


def test_license_expressions() -> None:
    assert _allowed("MIT")
    assert _allowed("MIT OR GPL-3.0-only")
    assert _allowed("(Apache-2.0 AND BSD-3-Clause)")
    assert not _allowed("LGPL-3.0-only")
    assert not _allowed("MIT AND GPL-2.0-only")
