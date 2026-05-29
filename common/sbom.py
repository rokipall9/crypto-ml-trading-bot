"""
sbom.py — Snyk-style Software Bill of Materials generator.

Outputs CycloneDX-format SBOM listing every Python module, every external
dependency, every system-level package the bot depends on. Required for:
  - Enterprise procurement (compliance reviewers ask for SBOMs)
  - Vulnerability scanning (Dependabot reads it)
  - Audit trail (which versions were running on day X)

Run periodically; output to /home/ubuntu/common/sbom.json + git-able format.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("sbom")

OUTPUT_FILE = "/home/ubuntu/common/sbom.json"
COMMON_DIR = "/home/ubuntu/common"


def _hash_file(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def _scan_python_modules() -> List[dict]:
    out = []
    if not os.path.isdir(COMMON_DIR):
        return out
    for p in Path(COMMON_DIR).glob("*.py"):
        if p.name.startswith("__"):
            continue
        out.append({
            "type": "internal-module",
            "name": p.stem,
            "path": str(p),
            "size_bytes": p.stat().st_size,
            "sha256": _hash_file(str(p)),
            "modified": datetime.fromtimestamp(
                p.stat().st_mtime, timezone.utc).isoformat(),
        })
    return sorted(out, key=lambda x: x["name"])


def _scan_external_deps() -> List[dict]:
    """Read requirements.txt + pip list to enumerate."""
    out = []
    req_files = ["/home/ubuntu/srs/requirements.txt"]
    for req in req_files:
        if not os.path.exists(req):
            continue
        with open(req, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Parse "name>=1.0,<2.0" → name+constraint
                parts = (line.replace(">=", " ").replace("<=", " ")
                         .replace("==", " ").replace("<", " ")
                         .replace(">", " ").replace(",", " ").split())
                if parts:
                    out.append({
                        "type": "python-package-required",
                        "name": parts[0],
                        "constraint": line,
                        "source_file": req,
                    })
    # Also enumerate installed via pip
    try:
        import subprocess
        result = subprocess.run(
            ["pip", "list", "--format=json"],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            installed = json.loads(result.stdout)
            for pkg in installed:
                out.append({
                    "type": "python-package-installed",
                    "name": pkg["name"],
                    "version": pkg["version"],
                })
    except Exception:
        pass
    return out


def _scan_system_components() -> List[dict]:
    """OS-level deps the system relies on."""
    components = []
    # Caddy (TLS)
    try:
        import subprocess
        r = subprocess.run(["caddy", "version"], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0:
            components.append({"type": "system-binary", "name": "caddy",
                               "version": r.stdout.strip()})
    except Exception:
        pass
    # cloudflared
    try:
        import subprocess
        r = subprocess.run(["cloudflared", "--version"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            components.append({"type": "system-binary",
                               "name": "cloudflared",
                               "version": r.stdout.strip().split("\n")[0]})
    except Exception:
        pass
    # Python itself
    components.append({
        "type": "runtime", "name": "python",
        "version": f"{sys.version_info.major}.{sys.version_info.minor}."
                  f"{sys.version_info.micro}",
    })
    return components


def generate() -> dict:
    """Build the full SBOM."""
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "serialNumber": "urn:uuid:" + hashlib.sha256(
            str(datetime.now()).encode()).hexdigest()[:32],
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tools": [{"name": "srs-sbom", "version": "1.0"}],
            "component": {
                "type": "application",
                "name": "srs-pro-signal-bot",
                "version": "1.3.0",
            },
        },
        "components": (
            _scan_python_modules()
            + _scan_external_deps()
            + _scan_system_components()
        ),
    }
    sbom["component_count"] = len(sbom["components"])
    return sbom


def write_to_disk() -> str:
    sbom = generate()
    tmp = OUTPUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sbom, f, indent=2, default=str)
    os.replace(tmp, OUTPUT_FILE)
    log.info("sbom_written", path=OUTPUT_FILE,
             components=sbom["component_count"])
    return OUTPUT_FILE


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "write":
        path = write_to_disk()
        print(f"wrote {path}")
    else:
        print(json.dumps(generate(), indent=2, default=str))
