"""Generate a minimal deterministic CycloneDX-compatible component inventory."""

import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
components = [{"type": "application", "name": "narang-rider", "version": "0.1.0"}]
for line in (root / "requirements-runtime.lock").read_text().splitlines():
    if line and not line.startswith("#"):
        name, version = line.split("==", 1)
        components.append({"type": "library", "name": name, "version": version, "purl": f"pkg:pypi/{name}@{version}"})
package = json.loads((root / "frontend/package-lock.json").read_text())
for name, item in sorted(package.get("packages", {}).items()):
    if name.startswith("node_modules/"):
        component = name.removeprefix("node_modules/")
        version = item["version"]
        components.append({"type": "library", "name": component, "version": version, "purl": f"pkg:npm/{component}@{version}"})
document = {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1, "components": components}
(root / "build").mkdir(exist_ok=True)
(root / "build" / "sbom.cdx.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
