"""Load the one project-level TOML configuration and resolve storage paths."""

import tomllib
from dataclasses import dataclass
from pathlib import Path


def find_project_root(start: Path | None = None) -> Path:
    """Find the nearest parent containing project.toml."""
    candidate = (start or Path.cwd()).resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / "project.toml").is_file():
            return directory
    raise FileNotFoundError(f"No project.toml found above {candidate}")


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Validated access to project settings and standard paths."""

    root: Path
    values: dict[str, object]

    def table(self, name: str) -> dict[str, object]:
        value = self.values.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"project.toml [{name}] must be a table")
        return value

    @property
    def slug(self) -> str:
        slug = str(self.table("project").get("slug", "")).strip()
        if not slug:
            raise ValueError("project.toml project.slug is required")
        return slug

    @property
    def external_root(self) -> Path:
        value = str(self.table("storage").get("external_data_root", "")).strip()
        if not value:
            raise ValueError("project.toml storage.external_data_root is required")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("storage.external_data_root must be absolute")
        return path.resolve()

    @property
    def pdf_directory(self) -> Path:
        storage = self.table("storage")
        mode = str(storage.get("pdf_storage", ""))
        if mode == "project":
            configured = Path(str(storage.get("local_pdf_directory", "sources/pdfs")))
            if configured.is_absolute():
                raise ValueError("storage.local_pdf_directory must be project-relative")
            path = (self.root / configured).resolve()
            if not path.is_relative_to(self.root):
                raise ValueError("storage.local_pdf_directory escapes the project root")
            return path
        if mode == "external":
            return self.external_path("external_pdf_subdirectory", "pdfs")
        raise ValueError("storage.pdf_storage must be 'project' or 'external'")

    def external_path(self, setting: str, default: str) -> Path:
        configured = Path(str(self.table("storage").get(setting, default)))
        if configured.is_absolute():
            raise ValueError(f"storage.{setting} must be relative to external_data_root")
        path = (self.external_root / configured).resolve()
        if not path.is_relative_to(self.external_root):
            raise ValueError(f"storage.{setting} escapes external_data_root")
        return path

    def project_path(self, value: str | Path) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()


def load_project_config(start: Path | None = None, *, require_initialized: bool = True) -> ProjectConfig:
    root = find_project_root(start)
    with (root / "project.toml").open("rb") as source:
        values = tomllib.load(source)
    if require_initialized:
        template = values.get("template", {})
        if not isinstance(template, dict) or template.get("initialized") is not True:
            raise RuntimeError("This is an uninitialized template copy. Run tools/initialize_project.py first.")
    return ProjectConfig(root=root, values=values)
