"""Unified CLI for image manager."""

import shutil
import sys
from pathlib import Path

from manager.config import ConfigLoader
from manager.models import ModelResolver
from manager.rendering import RenderContext, render_dockerfile, render_test_config, generate_image_report, generate_single_image_report, generate_tag_report
from manager.dependency_graph import sort_images, extract_dependencies, CyclicDependencyError
from manager.rootfs import collect_rootfs_paths, merge_rootfs, has_rootfs_content, warn_sensitive_files
from manager.locking import read_lock_file, read_base_digest, rewrite_apt_install, rewrite_from_digest, extract_base_image


def print_usage() -> None:
    """Print main usage information."""
    print("Usage: image-manager <command> [args]", file=sys.stderr)
    print()
    print("Commands:")
    print("  generate            Generate Dockerfiles and test configs from images/")
    print("  reports             Generate HTML reports for all images and tags")
    print("  lock [target]       Generate packages.lock with pinned versions")
    print("  build [target]      Build an image (or all images if none specified)")
    print("  retag <target>      Apply aliases to existing registry images")
    print("  manifest <target>   Create multi-platform manifest from registry images")
    print("  sbom [target]       Generate SBOM for an image (or all images)")
    print("  test [target]       Test an image (or all images if none specified)")
    print()
    print("  generate-ci         Generate CI configuration")
    print()
    print("Generate CI options:")
    print("  --provider PROV     Use built-in template (gitlab, github)")
    print("  --template DIR      Use custom template directory")
    print("  --output PATH       Output file path (required for --template)")
    print("  --artifacts         Enable artifact passing between jobs (default: off)")
    print("                      --provider and --template are mutually exclusive")
    print()
    print("Target can be:")
    print("  <image>             All tags for image (e.g., 'base', 'dotnet')")
    print("  <image:tag>         Specific tag (e.g., 'base:2025.09', 'dotnet:9.0.100')")
    print()
    print("Options (generate, build, sbom, test):")
    print("  --snapshot-id ID    Use snapshot ID for MR/branch builds")
    print("                      - generate: FROM refs include snapshot suffix")
    print("                      - build: push to registry with snapshot tag only")
    print("                      - sbom/test: log snapshot context")
    print()
    print("Generate options:")
    print("  --no-lock           Skip applying packages.lock (no version/digest pinning)")
    print()
    print("Build options:")
    print("  --no-cache          Disable S3 build cache")
    print("  --platform PLAT     Build for specific platform only (amd64, arm64)")
    print("                      Default: build all platforms + multi-platform manifest")
    print()
    print("Retag options:")
    print("  --snapshot-id ID    Use snapshot ID suffix for registry tags")
    print()
    print("Manifest options:")
    print("  --snapshot-id ID    Use snapshot ID suffix for registry tags")
    print()
    print("SBOM options:")
    print("  --format FORMAT     SBOM format: cyclonedx-json (default), spdx-json, json")
    print()
    print("Test options:")
    print("  --platform PLAT     Test specific platform (default: native)")
    print("  --pull              Pull image from registry instead of loading tar")
    print()
    print("Examples:")
    print("  image-manager generate")
    print("  image-manager build                            # Build all images")
    print("  image-manager build base                       # Build all tags for base image")
    print("  image-manager build base:2025.09               # Build specific tag")
    print("  image-manager build base dotnet                # Build all tags for base and dotnet")
    print("  image-manager build --no-cache                 # Build without cache")
    print("  image-manager retag dotnet:9.0.300             # Apply aliases (9.0, 9) to image")
    print("  image-manager sbom base:2025.09                # Generate SBOM for specific tag")
    print("  image-manager sbom dotnet                      # Generate SBOM for all dotnet tags")
    print("  image-manager test base                        # Test all base image tags")


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


def expand_image_refs(refs: list[str]) -> list[str]:
    """Expand image references, converting image names to all their tags.

    Args:
        refs: List of image refs (can be 'image:tag' or just 'image')

    Returns:
        List of fully qualified image:tag references

    Examples:
        ['base:2025.09'] -> ['base:2025.09']
        ['base'] -> ['base:2025.09', 'base:2025.10', ...]
        ['base', 'dotnet:9.0.100'] -> ['base:2025.09', ..., 'dotnet:9.0.100']
    """
    all_refs = get_all_image_refs()

    # Build a map of image name -> list of refs
    image_to_refs: dict[str, list[str]] = {}
    for ref in all_refs:
        name = ref.split(":")[0]
        if name not in image_to_refs:
            image_to_refs[name] = []
        image_to_refs[name].append(ref)

    expanded = []
    for ref in refs:
        if ":" in ref:
            # Already a full ref
            expanded.append(ref)
        elif ref in image_to_refs:
            # Image name only - expand to all tags
            expanded.extend(image_to_refs[ref])
        else:
            # Unknown image, keep as-is (will fail later with helpful error)
            expanded.append(ref)

    return expanded


def cmd_generate(args: list[str]) -> int:
    """Generate Dockerfiles and test configs."""
    snapshot_id = None
    use_lock = True

    # Parse options
    i = 0
    while i < len(args):
        if args[i] == "--snapshot-id" and i + 1 < len(args):
            snapshot_id = args[i + 1]
            i += 2
        elif args[i] == "--no-lock":
            use_lock = False
            i += 1
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

        # Check lock file once per image
        lock_path = image.path / "packages.lock"
        has_lock = lock_path.exists()
        if use_lock and not has_lock:
            print(f"Warning: No packages.lock for {image.name}, build may not be reproducible", file=sys.stderr)

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

            # Apply lock file if enabled and exists
            if use_lock and has_lock:
                # Determine base ref from rendered Dockerfile
                base_info = extract_base_image(rendered_dockerfile)
                base_ref = f"{base_info[0]}:{base_info[1]}" if base_info else None

                locked_packages = read_lock_file(lock_path, base_ref)
                if locked_packages:
                    rendered_dockerfile = rewrite_apt_install(rendered_dockerfile, locked_packages)

                base_digest_info = read_base_digest(lock_path, base_ref)
                if base_digest_info:
                    original_ref, digest = base_digest_info
                    rendered_dockerfile = rewrite_from_digest(rendered_dockerfile, original_ref, digest)

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

                # Apply lock file if enabled and exists
                # Variants use the same lock file as the base image
                if use_lock and has_lock:
                    # Determine base ref from rendered Dockerfile
                    base_info = extract_base_image(rendered_dockerfile)
                    base_ref = f"{base_info[0]}:{base_info[1]}" if base_info else None

                    locked_packages = read_lock_file(lock_path, base_ref)
                    if locked_packages:
                        rendered_dockerfile = rewrite_apt_install(rendered_dockerfile, locked_packages)

                    base_digest_info = read_base_digest(lock_path, base_ref)
                    if base_digest_info:
                        original_ref, digest = base_digest_info
                        rendered_dockerfile = rewrite_from_digest(rendered_dockerfile, original_ref, digest)

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


def cmd_reports(args: list[str]) -> int:
    """Generate HTML reports for all images and tags."""
    snapshot_id = None

    # Parse options
    i = 0
    while i < len(args):
        if args[i] == "--snapshot-id" and i + 1 < len(args):
            snapshot_id = args[i + 1]
            i += 2
        else:
            i += 1

    # Load all images
    resolver = ModelResolver()
    all_images = []
    for image_yaml in Path("images").glob("**/image.yml"):
        config = ConfigLoader.load(image_yaml)
        image = resolver.resolve(config, image_yaml.parent)
        all_images.append(image)

    sorted_images = sort_images(all_images)

    # Generate main catalog report
    report_path = generate_image_report(sorted_images, snapshot_id)
    print(f"Image catalog: {report_path}")

    # Generate per-image and per-tag reports
    for image in sorted_images:
        # Generate image-level report
        image_report = generate_single_image_report(image, snapshot_id)
        print(f"Image report: {image_report}")

        # Generate tag-level reports
        for tag in image.tags:
            tag_report = generate_tag_report(image.name, tag.name, snapshot_id)
            print(f"Tag report: {tag_report}")

        # Generate variant tag reports
        for variant in image.variants:
            for tag in variant.tags:
                tag_report = generate_tag_report(image.name, tag.name, snapshot_id)
                print(f"Tag report: {tag_report}")

    return 0


def cmd_build(args: list[str]) -> int:
    """Build an image or all images."""
    from manager.building import run_build, ensure_buildkitd

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

    # Expand image names to all their tags, or get all if none specified
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
    else:
        try:
            image_refs = expand_image_refs(image_refs)
            print(f"Building {len(image_refs)} image(s)...")
        except CyclicDependencyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Start buildkitd once for all builds
    if not ensure_buildkitd():
        print("Error: Failed to start buildkitd", file=sys.stderr)
        return 1

    # Build each image
    failed = []
    for image_ref in image_refs:
        print(f"\n{'='*60}")
        print(f"Building {image_ref}")
        print(f"{'='*60}")
        try:
            result = run_build(image_ref, context_path, use_cache=use_cache, snapshot_id=snapshot_id, platforms=platforms)
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


def cmd_retag(args: list[str]) -> int:
    """Apply aliases to existing registry images."""
    from manager.building import tag_aliases, check_image_exists

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
        print("Error: target is required for retag command", file=sys.stderr)
        print("Usage: image-manager retag <image:tag> [--snapshot-id ID]", file=sys.stderr)
        return 1

    # Expand image names to all their tags
    try:
        image_refs = expand_image_refs(image_refs)
        print(f"Retagging {len(image_refs)} image(s)...")
    except CyclicDependencyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Retag each image
    failed = []
    for image_ref in image_refs:
        print(f"\n{'='*60}")
        print(f"Retagging {image_ref}")
        print(f"{'='*60}")

        # Check if image exists first
        if not check_image_exists(image_ref, snapshot_id):
            print(f"Error: Image not found in registry: {image_ref}", file=sys.stderr)
            failed.append(image_ref)
            continue

        try:
            result = tag_aliases(image_ref, snapshot_id=snapshot_id)
            if result != 0:
                failed.append(image_ref)
        except (RuntimeError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            failed.append(image_ref)

    if failed:
        print(f"\nFailed to retag: {', '.join(failed)}", file=sys.stderr)
        return 1

    print(f"\nSuccessfully retagged {len(image_refs)} image(s)")
    return 0


def cmd_manifest(args: list[str]) -> int:
    """Create multi-platform manifest from platform images in registry."""
    from manager.building import create_manifest_from_registry

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
        print("Error: target is required for manifest command", file=sys.stderr)
        print("Usage: image-manager manifest <image|image:tag> [--snapshot-id ID]", file=sys.stderr)
        return 1

    # Expand image names to all their tags
    try:
        image_refs = expand_image_refs(image_refs)
        print(f"Creating manifests for {len(image_refs)} image(s)...")
    except CyclicDependencyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Create manifests
    failed = []
    for image_ref in image_refs:
        print(f"\n{'='*60}")
        print(f"Creating manifest for {image_ref}")
        print(f"{'='*60}")
        try:
            result = create_manifest_from_registry(image_ref, snapshot_id=snapshot_id)
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
    pull = False

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
        elif args[i] == "--pull":
            pull = True
            i += 1
        elif args[i].startswith("--"):
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            return 1
        else:
            image_refs.append(args[i])
            i += 1

    # Expand image names to all their tags, or get all if none specified
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
    else:
        try:
            image_refs = expand_image_refs(image_refs)
            msg = f"Testing {len(image_refs)} image(s)"
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
            result = run_test(image_ref, config_path, auto_start=False, pull=pull, snapshot_id=snapshot_id)
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

    # Expand image names to all their tags, or get all if none specified
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
    else:
        try:
            image_refs = expand_image_refs(image_refs)
            msg = f"Generating SBOMs for {len(image_refs)} image(s)"
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


def cmd_lock(args: list[str]) -> int:
    """Generate packages.lock for an image."""
    from manager.locking import run_lock

    # Group refs by image name
    image_to_refs: dict[str, list[str]] = {}

    if not args:
        # Lock all images - collect all refs grouped by image
        dist_path = Path("dist")
        if not dist_path.exists():
            print("Error: No generated files found. Run 'image-manager generate' first.", file=sys.stderr)
            return 1
        for dockerfile in dist_path.glob("*/*/Dockerfile"):
            name = dockerfile.parent.parent.name
            tag = dockerfile.parent.name
            # Skip alias files
            if dockerfile.stat().st_size > 100:
                if name not in image_to_refs:
                    image_to_refs[name] = []
                image_to_refs[name].append(f"{name}:{tag}")
    else:
        # Expand image names to refs, group by image
        try:
            expanded = expand_image_refs(args)
            for ref in expanded:
                name = ref.split(":")[0]
                if name not in image_to_refs:
                    image_to_refs[name] = []
                image_to_refs[name].append(ref)
        except CyclicDependencyError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    print(f"Locking {len(image_to_refs)} image(s)...")

    images_dir = Path("images")
    dist_dir = Path("dist")

    exit_code = 0
    for image_name, refs in image_to_refs.items():
        print(f"\n{'='*60}")
        print(f"Locking {image_name} ({len(refs)} tags)")
        print('='*60)
        result = run_lock(refs, images_dir, dist_dir)
        if result != 0:
            exit_code = result

    return exit_code


def cmd_generate_ci(args: list[str]) -> int:
    """Generate CI configuration."""
    from manager.ci_generator import generate_gitlab_ci, generate_github_ci, generate_custom_ci
    from manager.config import get_ci_config

    # Parse arguments
    provider = None
    template_dir = None
    output_path = None
    artifacts = None  # None means use config default

    i = 0
    while i < len(args):
        if args[i] == "--provider" and i + 1 < len(args):
            provider = args[i + 1]
            i += 2
        elif args[i] == "--template" and i + 1 < len(args):
            template_dir = args[i + 1]
            i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_path = args[i + 1]
            i += 2
        elif args[i] == "--artifacts":
            artifacts = True
            i += 1
        elif args[i].startswith("--"):
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            return 1
        else:
            print(f"Unexpected argument: {args[i]}", file=sys.stderr)
            return 1

    # Load CI config from .image-manager.yml
    ci_config = get_ci_config()

    # Apply config defaults if not specified via CLI
    if template_dir is None and ci_config.template:
        template_dir = ci_config.template
    if output_path is None and ci_config.output:
        output_path = ci_config.output
    if artifacts is None:
        artifacts = ci_config.artifacts

    # Validate mutually exclusive options
    if provider and template_dir:
        print("Error: --provider and --template are mutually exclusive", file=sys.stderr)
        return 1

    # Default to gitlab provider if neither specified
    if not provider and not template_dir:
        provider = "gitlab"

    # Validate provider if specified
    if provider and provider not in ("gitlab", "github"):
        print(f"Unsupported CI provider: {provider}", file=sys.stderr)
        print("Supported providers: gitlab, github", file=sys.stderr)
        return 1

    # Load and resolve all images
    resolver = ModelResolver()
    all_images = []
    for image_yaml in Path("images").glob("**/image.yml"):
        config = ConfigLoader.load(image_yaml)
        image = resolver.resolve(config, image_yaml.parent)
        all_images.append(image)

    # Sort by dependencies
    sorted_images = sort_images(all_images)

    # Generate CI based on provider or custom template
    ci_image = ci_config.image
    if template_dir:
        # Custom template
        if not output_path:
            print("Error: --output is required when using --template", file=sys.stderr)
            return 1
        try:
            generate_custom_ci(sorted_images, Path(template_dir), Path(output_path), artifacts=artifacts, ci_image=ci_image)
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
    elif provider == "gitlab":
        final_output = Path(output_path) if output_path else Path(".gitlab/ci/images.yml")
        generate_gitlab_ci(sorted_images, final_output, artifacts=artifacts, ci_image=ci_image)
        output_path = str(final_output)
    else:  # github
        final_output = Path(output_path) if output_path else Path(".github/workflows/images.yml")
        generate_github_ci(sorted_images, final_output, artifacts=artifacts, ci_image=ci_image)
        output_path = str(final_output)

    print(f"Generated CI configuration: {output_path}")
    return 0


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
    elif command == "reports":
        sys.exit(cmd_reports(args))
    elif command == "lock":
        sys.exit(cmd_lock(args))
    elif command == "build":
        sys.exit(cmd_build(args))
    elif command == "retag":
        sys.exit(cmd_retag(args))
    elif command == "manifest":
        sys.exit(cmd_manifest(args))
    elif command == "sbom":
        sys.exit(cmd_sbom(args))
    elif command == "test":
        sys.exit(cmd_test(args))
    elif command == "generate-ci":
        sys.exit(cmd_generate_ci(args))
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
