"""CycloneDX 1.6 SBOM describing what a target's artifacts contain.

One ``<name>-<version>-<target>.cdx.json`` is written next to the artifacts of
each target. It lists the installed distributions (with the sha256 of the
exact wheel installed and the license declared in its metadata), the
python-build-standalone interpreter and the wheelship launcher when they are
part of the bundle. The document is deterministic: the timestamp comes from
``SOURCE_DATE_EPOCH`` and the serial number is derived from the content.
"""

from __future__ import annotations

import email.message
import email.parser
import hashlib
import json
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from installer.sources import WheelFile

from . import __version__, fetch
from .reproducible import source_date_epoch

if TYPE_CHECKING:
    from .bundle import BuildContext
    from .wheels import Wheel

SPEC_VERSION = "1.6"
_LICENSE_CLASSIFIER = "License :: "


def _purl_pypi(name: str, version: str) -> str:
    return f"pkg:pypi/{urllib.parse.quote(name)}@{urllib.parse.quote(version)}"


def _licenses(metadata: email.message.Message) -> list[dict[str, Any]]:
    """License information from core metadata, most precise source first."""
    if expression := metadata.get("License-Expression"):
        return [{"expression": expression.strip()}]
    classifiers = [
        c.split(" :: ")[-1]
        for c in metadata.get_all("Classifier") or []
        if c.startswith(_LICENSE_CLASSIFIER) and c.count(" :: ") >= 2
    ]
    if classifiers:
        return [{"license": {"name": name}} for name in dict.fromkeys(classifiers)]
    text = (metadata.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 100:  # a name, not a full license text
        return [{"license": {"name": text}}]
    return []


def _wheel_component(wheel: Wheel) -> dict[str, Any]:
    with WheelFile.open(wheel.path) as source:
        metadata = email.parser.Parser().parsestr(source.read_dist_info("METADATA"))
    name, version = metadata["Name"], metadata["Version"]
    component: dict[str, Any] = {
        "type": "library",
        "bom-ref": _purl_pypi(name, version),
        "name": name,
        "version": version,
        "purl": _purl_pypi(name, version),
        "hashes": [{"alg": "SHA-256", "content": fetch.file_hash(wheel.path, "sha256")}],
    }
    if summary := metadata.get("Summary"):
        component["description"] = summary
    if licenses := _licenses(metadata):
        component["licenses"] = licenses
    if wheel.artifact.url:
        component["externalReferences"] = [{"type": "distribution", "url": wheel.artifact.url}]
    return component


def build(ctx: BuildContext) -> dict[str, Any]:
    app = ctx.app
    app_ref = _purl_pypi(ctx.cfg.name, ctx.version)
    components = [_wheel_component(w) for w in ctx.dependencies]
    if ctx.pbs_asset is not None:
        asset = ctx.pbs_asset
        version = ".".join(map(str, asset.version))
        components.append(
            {
                "type": "application",
                "bom-ref": f"python-build-standalone:{asset.name}",
                "name": "cpython",
                "version": version,
                "description": f"python-build-standalone {asset.release} ({asset.triple})",
                "purl": f"pkg:generic/python-build-standalone/cpython@{version}"
                f"?download_url={urllib.parse.quote(asset.url, safe='')}",
                "hashes": [{"alg": "SHA-256", "content": asset.sha256}],
                "licenses": [{"license": {"id": "PSF-2.0"}}],
            }
        )
    for kind, path in sorted(ctx.launchers_used.items()):
        components.append(
            {
                "type": "application",
                "bom-ref": f"wheelship-launcher:{kind}",
                "name": f"wheelship-launcher-{kind}" if kind != "console" else "wheelship-launcher",
                "version": __version__,
                "hashes": [{"alg": "SHA-256", "content": fetch.file_hash(path, "sha256")}],
                "licenses": [{"expression": "MIT OR Apache-2.0"}],
            }
        )
    components.sort(key=lambda c: c["bom-ref"])

    doc: dict[str, Any] = {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "version": 1,
        "metadata": {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(source_date_epoch())),
            "tools": {
                "components": [{"type": "application", "name": "wheelship", "version": __version__}]
            },
            "component": {
                "type": "application",
                "bom-ref": app_ref,
                "name": ctx.cfg.name,
                "version": ctx.version,
                "purl": app_ref,
                "hashes": [{"alg": "SHA-256", "content": fetch.file_hash(app.path, "sha256")}],
            },
            "properties": [
                {"name": "wheelship:target", "value": str(ctx.target)},
                {"name": "wheelship:interpreter", "value": ctx.tc.interpreter},
            ],
        },
        "components": components,
        "dependencies": [{"ref": app_ref, "dependsOn": [c["bom-ref"] for c in components]}],
    }
    digest = hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()
    doc["serialNumber"] = uuid.uuid5(uuid.NAMESPACE_URL, f"wheelship:{digest}").urn
    return doc


def write(ctx: BuildContext) -> Path:
    out = ctx.out_dir / f"{ctx.artifact_stem}.cdx.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build(ctx), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
