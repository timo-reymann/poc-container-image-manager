"""SBOM (Software Bill of Materials) generation using syft."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from manager.building import get_bin_path, get_image_tar_path

# System font stack for HTML reports
FONT_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif"


def get_syft_path() -> Path:
    """Get the path to the syft binary."""
    binary = get_bin_path() / "syft"
    if not binary.exists():
        raise RuntimeError(f"syft binary not found: {binary}")
    return binary


def get_sbom_path(image_ref: str, format: str = "cyclonedx-json") -> Path:
    """Get the output path for an SBOM file.

    Args:
        image_ref: Image reference in format 'name:tag'
        format: SBOM format (spdx-json, cyclonedx-json, etc.)

    Returns:
        Path to the SBOM output file
    """
    if ":" not in image_ref:
        raise ValueError(f"Invalid image reference '{image_ref}', expected format: name:tag")

    name, tag = image_ref.split(":", 1)

    # Map format to file extension
    ext_map = {
        "spdx-json": "spdx.json",
        "spdx": "spdx",
        "cyclonedx-json": "cyclonedx.json",
        "cyclonedx": "cyclonedx.xml",
        "json": "syft.json",
    }
    ext = ext_map.get(format, f"{format}.json")

    return Path("dist") / name / tag / f"sbom.{ext}"


def run_sbom(
    image_ref: str,
    format: str = "cyclonedx-json",
) -> int:
    """Generate SBOM for a built image.

    Args:
        image_ref: Image reference in format 'name:tag'
        format: Output format (spdx-json, cyclonedx-json, json, etc.)

    Returns:
        Exit code from syft
    """
    tar_path = get_image_tar_path(image_ref)

    if not tar_path.exists():
        print(f"Error: Image tar not found: {tar_path}", file=sys.stderr)
        print(f"Run 'image-manager build {image_ref}' first.", file=sys.stderr)
        return 1

    syft = get_syft_path()
    sbom_path = get_sbom_path(image_ref, format)

    cmd = [
        str(syft),
        "scan",
        f"docker-archive:{tar_path}",
        "-o", f"{format}={sbom_path}",
    ]

    print(f"Generating SBOM ({format}) for {image_ref}...")
    print(f"Running: {' '.join(cmd)}")

    result = subprocess.run(cmd)

    if result.returncode == 0:
        print(f"SBOM saved to: {sbom_path}")
        # Generate HTML report
        report_path = generate_html_report(image_ref, sbom_path)
        if report_path:
            print(f"HTML report: {report_path}")
    else:
        print(f"Failed to generate SBOM", file=sys.stderr)

    return result.returncode


def parse_cyclonedx(sbom_path: Path) -> dict:
    """Parse CycloneDX JSON and extract package information."""
    with open(sbom_path) as f:
        data = json.load(f)

    components = data.get("components", [])
    packages = []

    # Only include actual packages (library type), not individual files
    for comp in components:
        if comp.get("type") != "library":
            continue

        licenses = []
        for lic in comp.get("licenses", []):
            if "license" in lic:
                lic_info = lic["license"]
                licenses.append(lic_info.get("id") or lic_info.get("name", "Unknown"))

        packages.append({
            "name": comp.get("name", ""),
            "version": comp.get("version", ""),
            "type": comp.get("type", ""),
            "licenses": licenses,
            "purl": comp.get("purl", ""),
        })

    # Sort by name
    packages.sort(key=lambda p: p["name"].lower())

    return {
        "metadata": data.get("metadata", {}),
        "packages": packages,
        "total": len(packages),
    }


def generate_html_report(image_ref: str, sbom_path: Path) -> Path | None:
    """Generate HTML report for a single image."""
    if not sbom_path.exists():
        return None

    try:
        data = parse_cyclonedx(sbom_path)
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Warning: Could not parse SBOM for HTML report: {e}", file=sys.stderr)
        return None

    name, tag = image_ref.split(":", 1)
    report_path = sbom_path.parent / "sbom-report.html"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SBOM Report - {image_ref}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: {FONT_STACK}; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        h1 {{ margin-top: 0; color: #333; }}
        .meta {{ color: #666; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 1px solid #eee; }}
        .stats {{ display: flex; gap: 20px; margin-bottom: 20px; }}
        .stat {{ background: #f0f0f0; padding: 15px; border-radius: 4px; }}
        .stat-value {{ font-size: 24px; font-weight: bold; color: #333; }}
        .stat-label {{ color: #666; font-size: 14px; }}
        .package-list {{ column-count: 2; column-gap: 40px; }}
        .package {{ break-inside: avoid; padding: 6px 0; border-bottom: 1px solid #f0f0f0; }}
        .package-name {{ font-weight: 500; color: #333; }}
        .package-version {{ color: #666; font-size: 13px; margin-left: 8px; }}
        a {{ color: #0066cc; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        @media (max-width: 700px) {{ .package-list {{ column-count: 1; }} }}
    </style>
</head>
<body>
    <div class="container">
        <h1>SBOM: {image_ref}</h1>
        <div class="meta">
            Generated: {timestamp} |
            <a href="sbom.cyclonedx.json">Download CycloneDX JSON</a>
        </div>
        <div class="stats">
            <div class="stat">
                <div class="stat-value">{data['total']}</div>
                <div class="stat-label">Packages</div>
            </div>
        </div>
        <div class="package-list">
"""

    for pkg in data["packages"]:
        html += f'            <div class="package"><span class="package-name">{pkg["name"]}</span><span class="package-version">{pkg["version"]}</span></div>\n'

    html += """        </div>
    </div>
</body>
</html>
"""

    report_path.write_text(html)
    return report_path


