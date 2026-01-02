"""Unified CLI for image manager."""

import shutil
import sys
from pathlib import Path

from manager.config import ConfigLoader
from manager.models import ModelResolver
from manager.rendering import RenderContext, render_dockerfile, render_test_config, generate_image_report
from manager.dependency_graph import sort_images, extract_dependencies, CyclicDependencyError
from manager.rootfs import collect_rootfs_paths, merge_rootfs, has_rootfs_content, warn_sensitive_files


def print_usage() -> None:
    """Print main usage information."""
    print("Usage: image-manager <command> [args]", file=sys.stderr)
    print()
    print("Commands:")
    print("  generate            Generate Dockerfiles and test configs from images/")
    print("  build [image:tag]   Build an image (or all images if none specified)")
    print("  manifest <image:tag> Create multi-platform manifest from registry images")
    print("  sbom [image:tag]    Generate SBOM for an image (or all images)")
    print("  test [image:tag]    Test an image (or all images if none specified)")
    print("  start [daemon]      Start daemons (buildkitd, registry, garage, dind, or all)")
    print("  stop [daemon]       Stop daemons (buildkitd, registry, garage, dind, or all)")
    print("  status [daemon]     Check daemon status")
    print()
    print("Options (generate, build, sbom, test):")
    print("  --snapshot-id ID    Use snapshot ID for MR/branch builds")
    print("                      - generate: FROM refs include snapshot suffix")
    print("                      - build: push to registry with snapshot tag only")
    print("                      - sbom/test: log snapshot context")
    print()
    print("Build options:")
    print("  --no-cache          Disable S3 build cache")
    print("  --platform PLAT     Build for specific platform only (amd64, arm64)")
    print("                      Default: build all platforms + multi-platform manifest")
    print()
    print("Manifest options:")
    print("  --snapshot-id ID    Use snapshot ID suffix for registry tags")
    print()
    print("SBOM options:")
    print("  --format FORMAT     SBOM format: cyclonedx-json (default), spdx-json, json")
    print()
    print("Test options:")
    print("  --platform PLAT     Test specific platform (default: native)")
    print()
    print("Examples:")
    print("  image-manager generate")
    print("  image-manager build                            # Build for main (release tags)")
    print("  image-manager build --no-cache                 # Build without cache")
    print("  image-manager sbom base:2025.09                # Generate SBOM for image")
    print("  image-manager sbom --format spdx-json          # Generate SPDX SBOM")
    print("  image-manager generate --snapshot-id mr-123    # Generate for MR")
    print("  image-manager build --snapshot-id mr-123       # Build for MR (snapshot tags)")
    print("  image-manager test --snapshot-id mr-123        # Test MR build")
    print("  image-manager start")
    print("  image-manager status")


def get_all_image_refs() -> list[str]:
    """Get all image references from dist/ directory in dependency order.

    Returns list of image:tag strings for all generated images.
    """
    # Load and resolve all images to get dependency order
    resolver = ModelResolver()
    all_images = []
    for image_yaml in Path("images").glob("**/image.yml"):
        config = ConfigLoader.load(image_yaml)
        image = resolver.resolve(config, image_yaml.parent)
        all_images.append(image)

    sorted_images = sort_images(all_images)

    # Collect all image:tag references
    refs = []
    for image in sorted_images:
        for tag in image.tags:
            refs.append(f"{image.name}:{tag.name}")
        for variant in image.variants:
            for variant_tag in variant.tags:
                refs.append(f"{image.name}:{variant_tag.name}")

    return refs


def cmd_generate(args: list[str]) -> int:
    """Generate Dockerfiles and test configs."""
    snapshot_id = None

    # Parse options
    i = 0
    while i < len(args):
        if args[i] == "--snapshot-id" and i + 1 < len(args):
            snapshot_id = args[i + 1]
            i += 2
        elif args[i].startswith("--"):
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            return 1
        else:
            i += 1

    dist_path = Path("dist")
    # Don't clear dist - preserve built artifacts (image.tar, sbom, etc.)
    # Just overwrite Dockerfile, test.yml, index.html, aliases

    # Load and resolve all images
    resolver = ModelResolver()
    all_images = []
    for image_yaml in Path("images").glob("**/image.yml"):
        config = ConfigLoader.load(image_yaml)
        image = resolver.resolve(config, image_yaml.parent)
        all_images.append(image)

    # Sort images by dependencies to ensure correct build order
    try:
        sorted_images = sort_images(all_images)
    except CyclicDependencyError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("\nCannot generate images due to circular dependencies.", file=sys.stderr)
        print("Please review your image configurations and remove any circular references.", file=sys.stderr)
        return 1

    # Log the build order with dependencies
    print("Build order (dependencies resolved):")
    dependencies = extract_dependencies(all_images)
    for i, image in enumerate(sorted_images, 1):
        deps = dependencies.get(image.name, set())
        if deps:
            deps_str = ", ".join(sorted(deps))
            print(f"  {i}. {image.name} (depends on: {deps_str})")
        else:
            print(f"  {i}. {image.name} (no dependencies)")
    print()

    for image in sorted_images:
        image_out_path = dist_path.joinpath(image.name)

        # Render base tags
        for tag in image.tags:
            tag_out_path = image_out_path.joinpath(tag.name)
            tag_out_path.mkdir(parents=True, exist_ok=True)

            # Collect and merge rootfs
            rootfs_paths = collect_rootfs_paths(
                image_path=image.path.parent,  # images/python
                version_path=image.path,        # images/python/3
                variant_name=None
            )
            has_rootfs = has_rootfs_content(rootfs_paths)

            if has_rootfs:
                merged_rootfs = tag_out_path / "rootfs"
                merge_rootfs(rootfs_paths, merged_rootfs)
                # Warn about sensitive files
                for warning in warn_sensitive_files(merged_rootfs):
                    print(warning, file=sys.stderr)

            ctx = RenderContext(
                image=image,
                all=sorted_images,
                tag=tag,
                variant=None,
                snapshot_id=snapshot_id,
                has_rootfs=has_rootfs
            )

            rendered_dockerfile = render_dockerfile(ctx)
            tag_out_path.joinpath("Dockerfile").write_text(rendered_dockerfile)

            rendered_test_config = render_test_config(ctx)
            tag_out_path.joinpath("test.yml").write_text(rendered_test_config)

        # Render variant tags
        for variant in image.variants:
            for variant_tag in variant.tags:
                variant_out_path = image_out_path.joinpath(variant_tag.name)
                variant_out_path.mkdir(parents=True, exist_ok=True)

                # Collect and merge rootfs (including variant-specific)
                rootfs_paths = collect_rootfs_paths(
                    image_path=image.path.parent,
                    version_path=image.path,
                    variant_name=variant.name
                )
                has_rootfs = has_rootfs_content(rootfs_paths)

                if has_rootfs:
                    merged_rootfs = variant_out_path / "rootfs"
                    merge_rootfs(rootfs_paths, merged_rootfs)
                    for warning in warn_sensitive_files(merged_rootfs):
                        print(warning, file=sys.stderr)

                ctx = RenderContext(
                    image=image,
                    all=sorted_images,
                    tag=variant_tag,
                    variant=variant,
                    snapshot_id=snapshot_id,
                    has_rootfs=has_rootfs
                )

                rendered_dockerfile = render_dockerfile(ctx)
                variant_out_path.joinpath("Dockerfile").write_text(rendered_dockerfile)

                rendered_test_config = render_test_config(ctx)
                variant_out_path.joinpath("test.yml").write_text(rendered_test_config)

        # Write base aliases
        for alias, tag_name in image.aliases.items():
            alias_out_path = image_out_path.joinpath(alias)
            alias_out_path.write_text(tag_name)

        # Write variant aliases
        for variant in image.variants:
            for alias, tag_name in variant.aliases.items():
                alias_out_path = image_out_path.joinpath(alias)
                alias_out_path.write_text(tag_name)

    # Generate image catalog report
    report_path = generate_image_report(sorted_images, snapshot_id)
    print(f"Image catalog: {report_path}")

    return 0


def cmd_build(args: list[str]) -> int:
    """Build an image or all images."""
    from manager.building import run_build, ensure_buildkitd, ensure_registry, ensure_garage

    context_path = None
    image_refs = []
    use_cache = True
    snapshot_id = None
    platforms = None

    # Parse options and image refs
    i = 0
    while i < len(args):
        if args[i] == "--context" and i + 1 < len(args):
            context_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--no-cache":
            use_cache = False
            i += 1
        elif args[i] == "--snapshot-id" and i + 1 < len(args):
            snapshot_id = args[i + 1]
            i += 2
        elif args[i] == "--platform" and i + 1 < len(args):
            platforms = [args[i + 1]]
            i += 2
        elif args[i].startswith("--"):
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            return 1
        else:
            image_refs.append(args[i])
            i += 1

    # If no image specified, build all
    if not image_refs:
        try:
            image_refs = get_all_image_refs()
            if not image_refs:
                print("No images found. Run 'image-manager generate' first.", file=sys.stderr)
                return 1
            print(f"Building all images ({len(image_refs)} total)...")
        except CyclicDependencyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Start buildkitd and registry once for all builds
    if not ensure_buildkitd():
        print("Error: Failed to start buildkitd", file=sys.stderr)
        return 1
    if not ensure_registry():
        print("Error: Failed to start registry", file=sys.stderr)
        return 1
    if use_cache and not ensure_garage():
        print("Warning: Failed to start garage, building without cache", file=sys.stderr)
        use_cache = False

    # Build each image
    failed = []
    for image_ref in image_refs:
        print(f"\n{'='*60}")
        print(f"Building {image_ref}")
        print(f"{'='*60}")
        try:
            result = run_build(image_ref, context_path, auto_start=False, use_cache=use_cache, snapshot_id=snapshot_id, platforms=platforms)
            if result != 0:
                failed.append(image_ref)
        except (RuntimeError, FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            failed.append(image_ref)

    if failed:
        print(f"\nFailed to build: {', '.join(failed)}", file=sys.stderr)
        return 1

    print(f"\nSuccessfully built {len(image_refs)} image(s)")
    return 0


def cmd_manifest(args: list[str]) -> int:
    """Create multi-platform manifest from platform images in registry."""
    from manager.building import create_manifest_from_registry, ensure_registry

    image_refs = []
    snapshot_id = None

    # Parse options and image refs
    i = 0
    while i < len(args):
        if args[i] == "--snapshot-id" and i + 1 < len(args):
            snapshot_id = args[i + 1]
            i += 2
        elif args[i].startswith("--"):
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            return 1
        else:
            image_refs.append(args[i])
            i += 1

    if not image_refs:
        print("Error: image:tag is required for manifest command", file=sys.stderr)
        print("Usage: image-manager manifest <image:tag> [--snapshot-id ID]", file=sys.stderr)
        return 1

    # Start registry
    if not ensure_registry():
        print("Error: Failed to start registry", file=sys.stderr)
        return 1

    # Create manifests
    failed = []
    for image_ref in image_refs:
        print(f"\n{'='*60}")
        print(f"Creating manifest for {image_ref}")
        print(f"{'='*60}")
        try:
            result = create_manifest_from_registry(image_ref, snapshot_id=snapshot_id, auto_start=False)
            if result != 0:
                failed.append(image_ref)
        except (RuntimeError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            failed.append(image_ref)

    if failed:
        print(f"\nFailed to create manifest: {', '.join(failed)}", file=sys.stderr)
        return 1

    print(f"\nSuccessfully created {len(image_refs)} manifest(s)")
    return 0


def cmd_test(args: list[str]) -> int:
    """Test an image or all images."""
    from manager.testing import run_test, ensure_dind
    from manager.building import get_native_platform, platform_to_path

    config_path = None
    image_refs = []
    snapshot_id = None
    platform = None  # Will default to native

    # Parse options and image refs
    i = 0
    while i < len(args):
        if args[i] == "--config" and i + 1 < len(args):
            config_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--snapshot-id" and i + 1 < len(args):
            snapshot_id = args[i + 1]
            i += 2
        elif args[i] == "--platform" and i + 1 < len(args):
            platform = args[i + 1]
            i += 2
        elif args[i].startswith("--"):
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            return 1
        else:
            image_refs.append(args[i])
            i += 1

    # If no image specified, test all
    if not image_refs:
        try:
            image_refs = get_all_image_refs()
            if not image_refs:
                print("No images found. Run 'image-manager generate' first.", file=sys.stderr)
                return 1
            msg = f"Testing all images ({len(image_refs)} total)"
            if snapshot_id:
                msg += f" [snapshot: {snapshot_id}]"
            print(f"{msg}...")
        except CyclicDependencyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Start dind once for all tests
    if not ensure_dind():
        print("Error: Failed to start dind container", file=sys.stderr)
        return 1

    # Test each image
    failed = []
    for image_ref in image_refs:
        print(f"\n{'='*60}")
        print(f"Testing {image_ref}")
        print(f"{'='*60}")
        try:
            result = run_test(image_ref, config_path, auto_start=False)
            if result != 0:
                failed.append(image_ref)
        except (RuntimeError, FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            failed.append(image_ref)

    if failed:
        print(f"\nFailed tests: {', '.join(failed)}", file=sys.stderr)
        return 1

    print(f"\nAll {len(image_refs)} image(s) passed tests")
    return 0


def cmd_sbom(args: list[str]) -> int:
    """Generate SBOM for an image or all images."""
    from manager.sbom import run_sbom

    image_refs = []
    format = "cyclonedx-json"
    snapshot_id = None

    # Parse options and image refs
    i = 0
    while i < len(args):
        if args[i] == "--format" and i + 1 < len(args):
            format = args[i + 1]
            i += 2
        elif args[i] == "--snapshot-id" and i + 1 < len(args):
            snapshot_id = args[i + 1]
            i += 2
        elif args[i].startswith("--"):
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            return 1
        else:
            image_refs.append(args[i])
            i += 1

    # If no image specified, generate for all
    if not image_refs:
        try:
            image_refs = get_all_image_refs()
            if not image_refs:
                print("No images found. Run 'image-manager generate' first.", file=sys.stderr)
                return 1
            msg = f"Generating SBOMs for all images ({len(image_refs)} total)"
            if snapshot_id:
                msg += f" [snapshot: {snapshot_id}]"
            print(f"{msg}...")
        except CyclicDependencyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Generate SBOM for each image
    failed = []
    for image_ref in image_refs:
        print(f"\n{'='*60}")
        print(f"SBOM {image_ref}")
        print(f"{'='*60}")
        try:
            result = run_sbom(image_ref, format)
            if result != 0:
                failed.append(image_ref)
        except (RuntimeError, FileNotFoundError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            failed.append(image_ref)

    if failed:
        print(f"\nFailed to generate SBOM: {', '.join(failed)}", file=sys.stderr)
        return 1

    print(f"\nGenerated SBOMs for {len(image_refs)} image(s)")
    return 0


def cmd_start(args: list[str]) -> int:
    """Start daemons."""
    from manager.building import start_buildkitd, start_registry, start_garage
    from manager.testing import start_dind

    daemon = args[0] if args else "all"
    valid_daemons = ("all", "buildkitd", "registry", "garage", "dind")

    if daemon not in valid_daemons:
        print(f"Unknown daemon: {daemon}", file=sys.stderr)
        print(f"Available: {', '.join(valid_daemons)}", file=sys.stderr)
        return 1

    if daemon in ("all", "buildkitd"):
        result = start_buildkitd()
        if result != 0 and daemon == "buildkitd":
            return result

    if daemon in ("all", "registry"):
        result = start_registry()
        if result != 0 and daemon == "registry":
            return result

    if daemon in ("all", "garage"):
        result = start_garage()
        if result != 0 and daemon == "garage":
            return result

    if daemon in ("all", "dind"):
        result = start_dind()
        if result != 0 and daemon == "dind":
            return result

    return 0


def cmd_stop(args: list[str]) -> int:
    """Stop daemons."""
    from manager.building import stop_buildkitd, stop_registry, stop_garage
    from manager.testing import stop_dind

    daemon = args[0] if args else "all"
    valid_daemons = ("all", "buildkitd", "registry", "garage", "dind")

    if daemon not in valid_daemons:
        print(f"Unknown daemon: {daemon}", file=sys.stderr)
        print(f"Available: {', '.join(valid_daemons)}", file=sys.stderr)
        return 1

    if daemon in ("all", "buildkitd"):
        stop_buildkitd()

    if daemon in ("all", "registry"):
        stop_registry()

    if daemon in ("all", "garage"):
        stop_garage()

    if daemon in ("all", "dind"):
        stop_dind()

    return 0


def cmd_status(args: list[str]) -> int:
    """Check daemon status."""
    from manager.building import is_buildkitd_running, get_socket_addr, is_registry_running, get_registry_addr, is_garage_running, get_garage_s3_endpoint
    from manager.testing import is_dind_running, get_docker_host

    daemon = args[0] if args else "all"
    valid_daemons = ("all", "buildkitd", "registry", "garage", "dind")
    all_running = True

    if daemon not in valid_daemons:
        print(f"Unknown daemon: {daemon}", file=sys.stderr)
        print(f"Available: {', '.join(valid_daemons)}", file=sys.stderr)
        return 1

    if daemon in ("all", "buildkitd"):
        if is_buildkitd_running():
            print(f"buildkitd: running (addr: {get_socket_addr()})")
        else:
            print("buildkitd: not running")
            all_running = False

    if daemon in ("all", "registry"):
        if is_registry_running():
            print(f"registry: running (addr: {get_registry_addr()})")
        else:
            print("registry: not running")
            all_running = False

    if daemon in ("all", "garage"):
        if is_garage_running():
            print(f"garage: running (addr: {get_garage_s3_endpoint()})")
        else:
            print("garage: not running")
            all_running = False

    if daemon in ("all", "dind"):
        if is_dind_running():
            print(f"dind: running (addr: {get_docker_host()})")
        else:
            print("dind: not running")
            all_running = False

    return 0 if all_running else 1


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    if command in ("--help", "-h"):
        print_usage()
        sys.exit(0)
    elif command == "generate":
        sys.exit(cmd_generate(args))
    elif command == "build":
        sys.exit(cmd_build(args))
    elif command == "manifest":
        sys.exit(cmd_manifest(args))
    elif command == "sbom":
        sys.exit(cmd_sbom(args))
    elif command == "test":
        sys.exit(cmd_test(args))
    elif command == "start":
        sys.exit(cmd_start(args))
    elif command == "stop":
        sys.exit(cmd_stop(args))
    elif command == "status":
        sys.exit(cmd_status(args))
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
