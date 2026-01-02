from datetime import datetime
from pathlib import Path
from typing import Callable
import os

from pydantic import dataclasses
from jinja2 import Environment
from manager.models import Image, Tag, Variant

FONT_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif"


def detect_platforms(tag_path: Path) -> list[str]:
    """Detect available platforms from built artifacts."""
    platforms = []
    if tag_path.exists():
        for d in sorted(tag_path.iterdir()):
            if d.is_dir() and d.name.startswith("linux-"):
                platforms.append(d.name.replace("-", "/"))
    return platforms


def get_platform_size(tag_path: Path, platform: str) -> str:
    """Get the size of a platform's image.tar."""
    plat_dir = platform.replace("/", "-")
    tar_path = tag_path / plat_dir / "image.tar"
    if tar_path.exists():
        size = tar_path.stat().st_size
        if size >= 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024 * 1024):.1f} GB"
        elif size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.0f} MB"
        elif size >= 1024:
            return f"{size / 1024:.0f} KB"
        return f"{size} B"
    return "-"


@dataclasses.dataclass(frozen=True)
class RenderContext:
    image: Image
    tag: Tag
    all: list[Image]
    variant: Variant | None = None
    snapshot_id: str | None = None


def _resolve_base_image(ctx: RenderContext) -> Callable[[str], str]:
    def impl(name: str):
        found = [i for i in ctx.all if i.name == name and i.is_base_image]
        if len(found) == 1:
            base_ref = found[0].full_qualified_base_image_name
            # Append snapshot_id if provided (for MR/branch builds)
            if ctx.snapshot_id:
                base_ref = f"{base_ref}-{ctx.snapshot_id}"
            return base_ref
        else:
            raise RuntimeError(f"Could not resolve base image {name}")

    return impl


def _resolve_version(ctx: RenderContext) -> Callable[[str], str]:
    def impl(name: str):
        # In the new architecture, tags already have merged versions
        # So we just need to check the tag's versions
        version_from_tag = ctx.tag.versions.get(name, None)
        if version_from_tag is not None:
            return version_from_tag

        raise RuntimeError(f"Could not resolve version {name}")

    return impl


def render_test_config(context: RenderContext) -> str:
    env = Environment()
    env.filters["resolve_version"] = _resolve_version(context)

    tpl = env.from_string(context.image.test_config_path.read_text())
    full_qualified_image_name = f"{context.image.name}:{context.tag.name}"
    if context.variant is not None:
        full_qualified_image_name += f"-{context.variant.name}"

    return tpl.render(
        image=context.image,
        tag=context.tag,
        full_qualified_image_name=full_qualified_image_name,
    )


def render_dockerfile(context: RenderContext):
    env = Environment()
    env.filters["resolve_base_image"] = _resolve_base_image(context)
    env.filters["resolve_version"] = _resolve_version(context)

    variant_args = {}

    if context.variant is not None:
        # For variants, need to find the base tag name (without suffix)
        # The variant tag name is like "3.13.7-semantic", we need "3.13.7"
        base_tag_name = context.tag.name
        for base_tag in context.image.tags:
            if context.tag.name.startswith(base_tag.name):
                base_tag_name = base_tag.name
                break

        variant_args = {
            "base_image": f"{context.image.name}:{base_tag_name}",
        }
        tpl_file = context.variant.template_path
    else:
        tpl_file = context.image.dockerfile_template_path

    tpl = env.from_string(tpl_file.read_text())
    return tpl.render(image=context.image, tag=context.tag, **variant_args)


def generate_image_report(images: list[Image], snapshot_id: str | None = None) -> Path:
    """Generate HTML report of available images."""
    dist_path = Path("dist")
    report_path = dist_path / "index.html"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Count tags and aliases separately
    total_tags = sum(len(img.tags) for img in images)
    total_aliases = sum(len(img.aliases) for img in images)
    # Count variant tags and aliases separately
    total_variant_tags = sum(len(v.tags) for img in images for v in img.variants)
    total_variant_aliases = sum(len(v.aliases) for img in images for v in img.variants)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Image Catalog</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: {FONT_STACK}; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        h1, h2 {{ margin-top: 0; color: #333; }}
        h2 {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; }}
        .meta {{ color: #666; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 1px solid #eee; }}
        .stats {{ display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap; }}
        .stat {{ background: #f0f0f0; padding: 15px; border-radius: 4px; min-width: 100px; }}
        .stat-value {{ font-size: 24px; font-weight: bold; color: #333; }}
        .stat-label {{ color: #666; font-size: 14px; }}
        .toc {{ background: #f9f9f9; padding: 15px; border-radius: 4px; margin-bottom: 20px; }}
        .toc ul {{ margin: 0; padding-left: 20px; }}
        .toc li {{ margin: 5px 0; }}
        .image-section {{ margin-bottom: 30px; }}
        .tag-group {{ display: inline-flex; align-items: center; margin: 3px; border-radius: 4px; overflow: hidden; }}
        .tag {{ display: inline-block; background: #e0e7ff; color: #3730a3; padding: 4px 10px; font-size: 13px; font-family: monospace; }}
        .tag-aliases {{ display: inline-block; background: #c7d2fe; color: #3730a3; padding: 4px 8px; font-size: 11px; font-family: monospace; border-left: 1px solid #a5b4fc; }}
        .variant-group {{ display: inline-flex; align-items: center; margin: 3px; border-radius: 4px; overflow: hidden; }}
        .variant {{ display: inline-block; background: #fef3c7; color: #92400e; padding: 4px 10px; font-size: 13px; font-family: monospace; }}
        .variant-aliases {{ display: inline-block; background: #fde68a; color: #92400e; padding: 4px 8px; font-size: 11px; font-family: monospace; border-left: 1px solid #fcd34d; }}
        .base-image {{ display: inline-block; background: #fee2e2; color: #991b1b; padding: 2px 8px; border-radius: 3px; font-size: 11px; margin-left: 10px; }}
        .platform-mini {{ display: inline-block; background: #d1fae5; color: #065f46; padding: 2px 6px; border-radius: 3px; font-size: 10px; margin-left: 4px; }}
        table {{ width: 100%; border-collapse: collapse; margin: 15px 0; }}
        th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #eee; }}
        th {{ background: #f9f9f9; font-weight: 600; }}
        a {{ color: #0066cc; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .tag a, .variant a {{ color: inherit; text-decoration: none; }}
        .tag a:hover, .variant a:hover {{ text-decoration: underline; }}
        .snapshot {{ background: #fef3c7; color: #92400e; padding: 2px 8px; border-radius: 3px; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Image Catalog</h1>
        <div class="meta">
            Generated: {timestamp}
            {f'<span class="snapshot">Snapshot: {snapshot_id}</span>' if snapshot_id else ''}
        </div>
        <div class="stats">
            <div class="stat">
                <div class="stat-value">{len(images)}</div>
                <div class="stat-label">Images</div>
            </div>
            <div class="stat">
                <div class="stat-value">{total_tags}</div>
                <div class="stat-label">Tags</div>
            </div>
            <div class="stat">
                <div class="stat-value">{total_aliases}</div>
                <div class="stat-label">Aliases</div>
            </div>
            <div class="stat">
                <div class="stat-value">{total_variant_tags}</div>
                <div class="stat-label">Variant Tags</div>
            </div>
            <div class="stat">
                <div class="stat-value">{total_variant_aliases}</div>
                <div class="stat-label">Variant Aliases</div>
            </div>
        </div>

        <div class="toc">
            <strong>Images:</strong>
            <ul>
"""

    for img in images:
        base_label = '<span class="base-image">base</span>' if img.is_base_image else ''
        html += f'                <li><a href="#{img.name}">{img.name}</a> ({len(img.tags)} tags){base_label}</li>\n'

    html += """            </ul>
        </div>
"""

    for img in images:
        base_label = '<span class="base-image">base image</span>' if img.is_base_image else ''

        # Build reverse alias mapping: target -> [aliases]
        tag_to_aliases: dict[str, list[str]] = {}
        for alias, target in img.aliases.items():
            tag_to_aliases.setdefault(target, []).append(alias)

        html += f"""
        <div class="image-section">
            <h2 id="{img.name}">{img.name} {base_label}</h2>
            <p><strong>Tags:</strong></p>
            <div>
"""
        for tag in img.tags:
            aliases_for_tag = sorted(tag_to_aliases.get(tag.name, []))
            # Detect platforms for this tag
            tag_path = dist_path / img.name / tag.name
            platforms = detect_platforms(tag_path)
            platform_badges = "".join(f'<span class="platform-mini">{p.split("/")[1]}</span>' for p in platforms)

            if aliases_for_tag:
                aliases_str = ", ".join(aliases_for_tag)
                html += f'                <span class="tag-group"><span class="tag"><a href="{img.name}/{tag.name}/">{tag.name}</a></span><span class="tag-aliases">{aliases_str}</span></span>{platform_badges}\n'
            else:
                html += f'                <span class="tag-group"><span class="tag"><a href="{img.name}/{tag.name}/">{tag.name}</a></span></span>{platform_badges}\n'

        html += "            </div>\n"

        # Variants
        if img.variants:
            html += "            <p><strong>Variants:</strong></p>\n"
            for variant in img.variants:
                # Build reverse alias mapping for variant
                vtag_to_aliases: dict[str, list[str]] = {}
                for alias, target in variant.aliases.items():
                    vtag_to_aliases.setdefault(target, []).append(alias)

                html += f'            <div style="margin-left: 20px; margin-bottom: 10px;">\n'
                html += f'                <strong>{variant.name}</strong><br>\n'
                for vtag in variant.tags:
                    aliases_for_vtag = sorted(vtag_to_aliases.get(vtag.name, []))
                    # Detect platforms for this variant tag
                    vtag_path = dist_path / img.name / vtag.name
                    vplatforms = detect_platforms(vtag_path)
                    vplatform_badges = "".join(f'<span class="platform-mini">{p.split("/")[1]}</span>' for p in vplatforms)

                    if aliases_for_vtag:
                        aliases_str = ", ".join(aliases_for_vtag)
                        html += f'                <span class="variant-group"><span class="variant"><a href="{img.name}/{vtag.name}/">{vtag.name}</a></span><span class="variant-aliases">{aliases_str}</span></span>{vplatform_badges}\n'
                    else:
                        html += f'                <span class="variant-group"><span class="variant"><a href="{img.name}/{vtag.name}/">{vtag.name}</a></span></span>{vplatform_badges}\n'
                html += "            </div>\n"

        # Versions table
        if img.tags and img.tags[0].versions:
            html += """            <p><strong>Versions:</strong></p>
            <table>
                <thead>
                    <tr>
                        <th>Tag</th>
"""
            version_keys = list(img.tags[0].versions.keys())
            for key in version_keys:
                html += f"                        <th>{key}</th>\n"
            html += """                        <th>Platforms</th>
                    </tr>
                </thead>
                <tbody>
"""
            for tag in img.tags:
                html += f'                    <tr>\n                        <td><a href="{img.name}/{tag.name}/">{tag.name}</a></td>\n'
                for key in version_keys:
                    html += f"                        <td>{tag.versions.get(key, '-')}</td>\n"
                # Add platforms column
                tag_path = dist_path / img.name / tag.name
                tag_platforms = detect_platforms(tag_path)
                platforms_str = "".join(f'<span class="platform-mini">{p.split("/")[1]}</span>' for p in tag_platforms) if tag_platforms else '-'
                html += f"                        <td>{platforms_str}</td>\n"
                html += "                    </tr>\n"
            html += """                </tbody>
            </table>
"""

        html += "        </div>\n"

    html += """    </div>
</body>
</html>
"""

    report_path.write_text(html)
    return report_path


def generate_single_image_report(img: Image, snapshot_id: str | None = None) -> Path:
    """Generate HTML report for a single image."""
    dist_path = Path("dist")
    report_path = dist_path / img.name / "index.html"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total_tags = len(img.tags)
    total_aliases = len(img.aliases)
    total_variant_tags = sum(len(v.tags) for v in img.variants)
    total_variant_aliases = sum(len(v.aliases) for v in img.variants)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{img.name} - Image Catalog</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: {FONT_STACK}; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        h1, h2 {{ margin-top: 0; color: #333; }}
        h2 {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; }}
        .meta {{ color: #666; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 1px solid #eee; }}
        .stats {{ display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap; }}
        .stat {{ background: #f0f0f0; padding: 15px; border-radius: 4px; min-width: 100px; }}
        .stat-value {{ font-size: 24px; font-weight: bold; color: #333; }}
        .stat-label {{ color: #666; font-size: 14px; }}
        .tag-group {{ display: inline-flex; align-items: center; margin: 3px; border-radius: 4px; overflow: hidden; }}
        .tag {{ display: inline-block; background: #e0e7ff; color: #3730a3; padding: 4px 10px; font-size: 13px; font-family: monospace; }}
        .tag a {{ color: inherit; text-decoration: none; }}
        .tag a:hover {{ text-decoration: underline; }}
        .tag-aliases {{ display: inline-block; background: #c7d2fe; color: #3730a3; padding: 4px 8px; font-size: 11px; font-family: monospace; border-left: 1px solid #a5b4fc; }}
        .variant-group {{ display: inline-flex; align-items: center; margin: 3px; border-radius: 4px; overflow: hidden; }}
        .variant {{ display: inline-block; background: #fef3c7; color: #92400e; padding: 4px 10px; font-size: 13px; font-family: monospace; }}
        .variant a {{ color: inherit; text-decoration: none; }}
        .variant a:hover {{ text-decoration: underline; }}
        .variant-aliases {{ display: inline-block; background: #fde68a; color: #92400e; padding: 4px 8px; font-size: 11px; font-family: monospace; border-left: 1px solid #fcd34d; }}
        .base-image {{ display: inline-block; background: #fee2e2; color: #991b1b; padding: 2px 8px; border-radius: 3px; font-size: 11px; margin-left: 10px; }}
        .platform-mini {{ display: inline-block; background: #d1fae5; color: #065f46; padding: 2px 6px; border-radius: 3px; font-size: 10px; margin-left: 4px; }}
        table {{ width: 100%; border-collapse: collapse; margin: 15px 0; }}
        th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #eee; }}
        th {{ background: #f9f9f9; font-weight: 600; }}
        a {{ color: #0066cc; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .snapshot {{ background: #fef3c7; color: #92400e; padding: 2px 8px; border-radius: 3px; font-size: 12px; }}
        .breadcrumb {{ margin-bottom: 15px; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="breadcrumb"><a href="../index.html">Image Catalog</a> / {img.name}</div>
        <h1>{img.name} {'<span class="base-image">base image</span>' if img.is_base_image else ''}</h1>
        <div class="meta">
            Generated: {timestamp}
            {f'<span class="snapshot">Snapshot: {snapshot_id}</span>' if snapshot_id else ''}
        </div>
        <div class="stats">
            <div class="stat">
                <div class="stat-value">{total_tags}</div>
                <div class="stat-label">Tags</div>
            </div>
            <div class="stat">
                <div class="stat-value">{total_aliases}</div>
                <div class="stat-label">Aliases</div>
            </div>
            <div class="stat">
                <div class="stat-value">{total_variant_tags}</div>
                <div class="stat-label">Variant Tags</div>
            </div>
            <div class="stat">
                <div class="stat-value">{total_variant_aliases}</div>
                <div class="stat-label">Variant Aliases</div>
            </div>
        </div>

        <h2>Tags</h2>
        <div>
"""

    # Build reverse alias mapping: target -> [aliases]
    tag_to_aliases: dict[str, list[str]] = {}
    for alias, target in img.aliases.items():
        tag_to_aliases.setdefault(target, []).append(alias)

    for tag in img.tags:
        aliases_for_tag = sorted(tag_to_aliases.get(tag.name, []))
        # Detect platforms for this tag
        tag_path = dist_path / img.name / tag.name
        platforms = detect_platforms(tag_path)
        platform_badges = "".join(f'<span class="platform-mini">{p.split("/")[1]}</span>' for p in platforms)

        if aliases_for_tag:
            aliases_str = ", ".join(aliases_for_tag)
            html += f'            <span class="tag-group"><span class="tag"><a href="{tag.name}/">{tag.name}</a></span><span class="tag-aliases">{aliases_str}</span></span>{platform_badges}\n'
        else:
            html += f'            <span class="tag-group"><span class="tag"><a href="{tag.name}/">{tag.name}</a></span></span>{platform_badges}\n'

    html += "        </div>\n"

    # Variants
    if img.variants:
        html += "        <h2>Variants</h2>\n"
        for variant in img.variants:
            # Build reverse alias mapping for variant
            vtag_to_aliases: dict[str, list[str]] = {}
            for alias, target in variant.aliases.items():
                vtag_to_aliases.setdefault(target, []).append(alias)

            html += f'        <div style="margin-bottom: 15px;">\n'
            html += f'            <strong>{variant.name}</strong><br>\n'
            for vtag in variant.tags:
                aliases_for_vtag = sorted(vtag_to_aliases.get(vtag.name, []))
                # Detect platforms for this variant tag
                vtag_path = dist_path / img.name / vtag.name
                vplatforms = detect_platforms(vtag_path)
                vplatform_badges = "".join(f'<span class="platform-mini">{p.split("/")[1]}</span>' for p in vplatforms)

                if aliases_for_vtag:
                    aliases_str = ", ".join(aliases_for_vtag)
                    html += f'            <span class="variant-group"><span class="variant"><a href="{vtag.name}/">{vtag.name}</a></span><span class="variant-aliases">{aliases_str}</span></span>{vplatform_badges}\n'
                else:
                    html += f'            <span class="variant-group"><span class="variant"><a href="{vtag.name}/">{vtag.name}</a></span></span>{vplatform_badges}\n'
            html += "        </div>\n"

    # Versions table
    if img.tags and img.tags[0].versions:
        html += """        <h2>Versions</h2>
        <table>
            <thead>
                <tr>
                    <th>Tag</th>
"""
        version_keys = list(img.tags[0].versions.keys())
        for key in version_keys:
            html += f"                    <th>{key}</th>\n"
        html += """                    <th>Platforms</th>
                </tr>
            </thead>
            <tbody>
"""
        for tag in img.tags:
            html += f"                <tr>\n                    <td><a href=\"{tag.name}/\">{tag.name}</a></td>\n"
            for key in version_keys:
                html += f"                    <td>{tag.versions.get(key, '-')}</td>\n"
            # Add platforms column
            tag_path = dist_path / img.name / tag.name
            tag_platforms = detect_platforms(tag_path)
            platforms_str = "".join(f'<span class="platform-mini">{p.split("/")[1]}</span>' for p in tag_platforms) if tag_platforms else '-'
            html += f"                    <td>{platforms_str}</td>\n"
            html += "                </tr>\n"
        html += """            </tbody>
        </table>
"""

    html += """    </div>
</body>
</html>
"""

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(html)
    return report_path


def generate_tag_report(image_name: str, tag_name: str, snapshot_id: str | None = None) -> Path:
    """Generate HTML report for a specific image tag showing platforms."""
    dist_path = Path("dist")
    tag_path = dist_path / image_name / tag_name
    report_path = tag_path / "index.html"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    platforms = detect_platforms(tag_path)
    image_ref = f"{image_name}:{tag_name}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{image_ref} - Image Catalog</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: {FONT_STACK}; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        h1, h2 {{ margin-top: 0; color: #333; }}
        h2 {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; }}
        .meta {{ color: #666; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 1px solid #eee; }}
        .stats {{ display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap; }}
        .stat {{ background: #f0f0f0; padding: 15px; border-radius: 4px; min-width: 100px; }}
        .stat-value {{ font-size: 24px; font-weight: bold; color: #333; }}
        .stat-label {{ color: #666; font-size: 14px; }}
        .platform-badge {{ display: inline-block; background: #e0e7ff; color: #3730a3; padding: 4px 10px; border-radius: 4px; font-size: 14px; font-weight: 500; margin-right: 8px; }}
        table {{ width: 100%; border-collapse: collapse; margin: 15px 0; }}
        th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #eee; }}
        th {{ background: #f9f9f9; font-weight: 600; }}
        a {{ color: #0066cc; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .snapshot {{ background: #fef3c7; color: #92400e; padding: 2px 8px; border-radius: 3px; font-size: 12px; }}
        .breadcrumb {{ margin-bottom: 15px; font-size: 14px; }}
        .file-link {{ font-family: monospace; font-size: 13px; }}
        .no-artifacts {{ color: #666; font-style: italic; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="breadcrumb">
            <a href="../../index.html">Image Catalog</a> /
            {image_name} /
            {tag_name}
        </div>
        <h1>{image_ref}</h1>
        <div class="meta">
            Generated: {timestamp}
            {f'<span class="snapshot">Snapshot: {snapshot_id}</span>' if snapshot_id else ''}
        </div>
        <div class="stats">
            <div class="stat">
                <div class="stat-value">{len(platforms)}</div>
                <div class="stat-label">Platforms</div>
            </div>
        </div>

        <h2>Platforms</h2>
"""

    if platforms:
        html += """        <table>
            <thead>
                <tr>
                    <th>Platform</th>
                    <th>Size</th>
                    <th>Image</th>
                    <th>SBOM</th>
                </tr>
            </thead>
            <tbody>
"""
        for plat in platforms:
            plat_dir = plat.replace("/", "-")
            size = get_platform_size(tag_path, plat)

            # Check for artifacts
            image_tar = tag_path / plat_dir / "image.tar"
            sbom_report = tag_path / plat_dir / "sbom-report.html"
            sbom_json = tag_path / plat_dir / "sbom.cyclonedx.json"

            image_link = f'<a href="{plat_dir}/image.tar" class="file-link">image.tar</a>' if image_tar.exists() else '-'

            sbom_links = []
            if sbom_report.exists():
                sbom_links.append(f'<a href="{plat_dir}/sbom-report.html">Report</a>')
            if sbom_json.exists():
                sbom_links.append(f'<a href="{plat_dir}/sbom.cyclonedx.json" class="file-link">JSON</a>')
            sbom_link = " | ".join(sbom_links) if sbom_links else '-'

            html += f"""                <tr>
                    <td><span class="platform-badge">{plat}</span></td>
                    <td>{size}</td>
                    <td>{image_link}</td>
                    <td>{sbom_link}</td>
                </tr>
"""
        html += """            </tbody>
        </table>
"""
    else:
        html += '        <p class="no-artifacts">No platform artifacts found. Run <code>image-manager build</code> first.</p>\n'

    # Build artifacts section
    html += """
        <h2>Build Artifacts</h2>
        <table>
            <thead>
                <tr>
                    <th>File</th>
                    <th>Description</th>
                </tr>
            </thead>
            <tbody>
"""

    artifacts = [
        ("Dockerfile", "Build instructions"),
        ("test.yml", "Test configuration"),
        ("image.tar", "Multi-platform manifest (or single platform copy)"),
    ]

    for filename, desc in artifacts:
        filepath = tag_path / filename
        if filepath.exists():
            html += f"""                <tr>
                    <td><a href="{filename}" class="file-link">{filename}</a></td>
                    <td>{desc}</td>
                </tr>
"""

    html += """            </tbody>
        </table>
    </div>
</body>
</html>
"""

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(html)
    return report_path
