#!/usr/bin/env python3
"""Initialize a copied extraction scaffold without copying template Git metadata."""

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LOCAL_DIRECTORIES = ("data", "examples", "input", "manual", "output", "sources", "temp")
EXTERNAL_DIRECTORY_SETTINGS = (
    ("acquisition_run_subdirectory", "acquisition-runs"),
    ("selection_cache_subdirectory", "page-selection-cache"),
    ("page_review_image_subdirectory", "page-review-images"),
    ("ocr_subdirectory", "ocr"),
    ("render_subdirectory", "rendered-pages"),
    ("cache_subdirectory", "data-extraction/cache"),
    ("export_subdirectory", "data-extraction/exports"),
    ("alternate_export_subdirectory", "data-extraction/alternate-exports"),
    ("banknorm_cache_subdirectory", "banknorm-cache"),
)
EXTERNAL_SUBDIRECTORIES = tuple(default for _, default in EXTERNAL_DIRECTORY_SETTINGS)


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def replace_setting(text: str, section: str, key: str, value: str) -> str:
    lines = text.splitlines(keepends=True)
    header = f"[{section}]"
    in_section = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == header
            continue
        if in_section and re.match(rf"^{re.escape(key)}\s*=", stripped):
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = f"{key} = {value}{ending}"
            return "".join(lines)
    raise ValueError(f"Could not find [{section}] {key}")


def replace_project_field(text: str, key: str, value: str) -> str:
    return replace_setting(text, "project", key, value)


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def configured_pdf_directory(
    root: Path,
    external_root: Path,
    pdf_storage: str,
    storage: dict[str, object],
) -> Path:
    if pdf_storage == "project":
        configured = Path(str(storage.get("local_pdf_directory", "sources/pdfs"))).expanduser()
        if configured.is_absolute():
            raise ValueError("storage.local_pdf_directory must be project-relative")
        path = (root / configured).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("storage.local_pdf_directory escapes the project root")
        return path
    configured = Path(str(storage.get("external_pdf_subdirectory", "pdfs"))).expanduser()
    if configured.is_absolute():
        raise ValueError("storage.external_pdf_subdirectory must be relative to external_data_root")
    path = (external_root / configured).resolve()
    if not path.is_relative_to(external_root.resolve()):
        raise ValueError("storage.external_pdf_subdirectory escapes external_data_root")
    return path


def render_files(
    root: Path,
    *,
    name: str,
    slug: str,
    description: str,
    dataset_shape: str,
    pdf_storage: str,
    external_root: Path,
) -> dict[Path, str]:
    project_path = root / "project.toml"
    pyproject_path = root / "pyproject.toml"
    readme_template_path = root / "templates" / "PROJECT_README.md"
    for required in (project_path, pyproject_path, readme_template_path):
        if not required.is_file():
            raise FileNotFoundError(required)

    project_text = project_path.read_text(encoding="utf-8")
    if "initialized = false" not in project_text:
        raise RuntimeError("project.toml is not an uninitialized template")
    project_text = replace_setting(project_text, "template", "initialized", "true")
    project_text = replace_setting(project_text, "project", "name", toml_string(name))
    project_text = replace_setting(project_text, "project", "slug", toml_string(slug))
    project_text = replace_setting(project_text, "project", "description", toml_string(description))
    project_text = replace_setting(project_text, "storage", "pdf_storage", toml_string(pdf_storage))
    project_text = replace_setting(project_text, "storage", "external_data_root", toml_string(str(external_root)))
    project_text = replace_setting(project_text, "dataset", "shape", toml_string(dataset_shape))
    project_text = replace_setting(project_text, "dataset", "primary_output", toml_string(f"data/{slug}.tsv"))
    project_text = replace_setting(project_text, "quality", "input_tsv", toml_string(f"data/{slug}.tsv"))

    pyproject_text = pyproject_path.read_text(encoding="utf-8")
    pyproject_text = replace_project_field(pyproject_text, "name", toml_string(slug))
    pyproject_text = replace_project_field(pyproject_text, "description", toml_string(description))

    rendered_project = tomllib.loads(project_text)
    storage = rendered_project.get("storage", {})
    if not isinstance(storage, dict):
        raise ValueError("project.toml [storage] must be a table")
    pdf_directory = configured_pdf_directory(root, external_root, pdf_storage, storage)
    tokens = {
        "{{PROJECT_NAME}}": name,
        "{{PROJECT_SLUG}}": slug,
        "{{PROJECT_DESCRIPTION}}": description,
        "{{DATASET_SHAPE}}": dataset_shape,
        "{{PDF_DIRECTORY}}": str(pdf_directory),
        "{{EXTERNAL_DATA_ROOT}}": str(external_root),
    }
    readme_text = readme_template_path.read_text(encoding="utf-8")
    for token, value in tokens.items():
        readme_text = readme_text.replace(token, value)
    unresolved = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", readme_text)))
    if unresolved:
        raise ValueError(f"Unresolved README template tokens: {', '.join(unresolved)}")

    return {
        project_path: project_text,
        pyproject_path: pyproject_text,
        root / "README.md": readme_text,
    }


def initialize_git(root: Path, git_directory: Path) -> None:
    git_directory.parent.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        [
            "git",
            "init",
            "--initial-branch=main",
            f"--separate-git-dir={git_directory}",
            str(root),
        ],
        check=True,
    )
    subprocess.run(
        ["git", f"--git-dir={git_directory}", "config", "core.worktree", str(root)],
        check=True,
    )


def validate_external_root(external_root: Path) -> None:
    """Refuse to mix a new project with any pre-existing external data."""

    if not os.path.lexists(external_root):
        return
    if external_root.is_symlink():
        raise ValueError(f"External project root must not be a symbolic link: {external_root}")
    if not external_root.is_dir():
        raise ValueError(f"External project root is not a directory: {external_root}")
    entries = sorted(path.name for path in external_root.iterdir())
    if entries:
        preview = ", ".join(entries[:5])
        suffix = " ..." if len(entries) > 5 else ""
        raise ValueError(
            f"External project root is not empty; refusing to mix or delete existing data: "
            f"{external_root} ({preview}{suffix})"
        )


def external_directories(
    external_root: Path,
    pdf_storage: str,
    storage: dict[str, object] | None = None,
) -> tuple[Path, ...]:
    settings = storage or {}

    def child(setting: str, default: str) -> Path:
        configured = Path(str(settings.get(setting, default))).expanduser()
        if configured.is_absolute():
            raise ValueError(f"storage.{setting} must be relative to external_data_root")
        path = (external_root / configured).resolve()
        if not path.is_relative_to(external_root.resolve()):
            raise ValueError(f"storage.{setting} escapes external_data_root")
        return path

    directories = [child(setting, default) for setting, default in EXTERNAL_DIRECTORY_SETTINGS]
    if pdf_storage == "external":
        directories.append(child("external_pdf_subdirectory", "pdfs"))
    return tuple(directories)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Human-readable project name.")
    parser.add_argument("--slug", required=True, help="Lowercase hyphenated project and repository identifier.")
    parser.add_argument("--description", required=True, help="One-sentence research-data description.")
    parser.add_argument("--dataset-shape", choices=("panel", "cross-section"), required=True)
    parser.add_argument("--pdf-storage", choices=("project", "external"), required=True)
    parser.add_argument("--dry-run", action="store_true", help="Validate and report actions without writing.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    parser.add_argument("--git-base", type=Path, default=Path("/home/sergio/git"), help=argparse.SUPPRESS)
    parser.add_argument("--external-base", type=Path, default=Path("/home/sergio/data"), help=argparse.SUPPRESS)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    root = args.root.resolve()
    if not SLUG_PATTERN.fullmatch(args.slug):
        raise SystemExit("--slug must contain lowercase letters/digits separated by single hyphens")
    if not args.name.strip() or not args.description.strip():
        raise SystemExit("--name and --description must contain non-whitespace text")
    if not root.is_dir():
        raise SystemExit(f"Project root does not exist: {root}")
    if os.path.lexists(root / ".git"):
        raise SystemExit("Refusing to initialize: .git exists. Copy the template without its .git pointer.")
    inherited_worktree = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if inherited_worktree.returncode == 0:
        raise SystemExit(
            "Refusing to initialize inside an existing Git worktree: " + inherited_worktree.stdout.strip()
        )

    git_directory = args.git_base.expanduser().resolve() / args.slug / ".git"
    if git_directory.exists() or git_directory.parent.exists():
        raise SystemExit(f"Refusing to overwrite Git metadata: {git_directory.parent}")
    external_root = args.external_base.expanduser().resolve() / args.slug
    try:
        validate_external_root(external_root)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Refusing unsafe external storage: {error}") from error
    rendered = render_files(
        root,
        name=args.name.strip(),
        slug=args.slug,
        description=args.description.strip(),
        dataset_shape=args.dataset_shape,
        pdf_storage=args.pdf_storage,
        external_root=external_root,
    )

    rendered_project = tomllib.loads(rendered[root / "project.toml"])
    storage = rendered_project.get("storage", {})
    if not isinstance(storage, dict):
        raise SystemExit("Rendered project.toml [storage] must be a table")
    pdf_directory = configured_pdf_directory(root, external_root, args.pdf_storage, storage)
    external_paths = external_directories(external_root, args.pdf_storage, storage)
    print(f"Project: {args.name} ({args.slug})")
    print(f"Root: {root}")
    print(f"PDFs: {pdf_directory}")
    print(f"External data: {external_root}")
    print(f"Git metadata: {git_directory}")
    if args.dry_run:
        print("Dry run complete; no files or directories were changed.")
        return

    initialize_git(root, git_directory)
    for directory in LOCAL_DIRECTORIES:
        (root / directory).mkdir(parents=True, exist_ok=True)
    pdf_directory.mkdir(parents=True, exist_ok=True)
    for directory in external_paths:
        directory.mkdir(parents=True, exist_ok=True)
    for path, text in rendered.items():
        atomic_write(path, text)

    print("Initialization complete.")
    print("Research decisions still required before data work:")
    print("- source provider/catalog, corpus scope, and immutable source manifest")
    print("- analytical unit, panel/cross-section keys, time spine, and value fields")
    print("- page-selection evidence and source-specific rules")
    print("- flat raw-plus-normalized schema, units, and blank/dash/zero meanings")
    print("- risk-based positive/negative page fixtures and complete extraction gold")
    print("- identity normalization, correction/reconciliation rules, and QC thresholds")
    print("- calibrated model/service settings, request ceiling, and dated pricing")
    print("Then run: uv lock && uv sync --locked")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
