# Python Style Guide for Historical Data Projects

This guide is for research, data-processing, document-extraction, and local review-app projects. It favors code that is direct, readable, safe to rerun, and easy to return to months later.

The goal is not to create a formal software architecture. The goal is to keep a growing research project understandable without letting a few scripts become enormous.

## Core principles

1. Prefer straightforward code over clever code.
2. Start with functions and modules. Add classes or abstractions only when they solve a problem that already exists.
3. Keep entry-point scripts small. Put reusable or substantial logic in clearly named modules.
4. Use `pathlib` for paths and file operations.
5. Use Python 3.12+ and modern typing.
6. Keep command-line interfaces small. Do not pass `argv` through the program.
7. Give persistent settings sensible defaults in a configuration file.
8. Separate original sources, generated results, and human corrections.
9. Make expensive or destructive operations explicit.
10. Prefer deterministic, restartable workflows.

## Python version and tools

- Use Python 3.12 or newer.
- Use one virtual environment for the project.
- Declare Python dependencies in `pyproject.toml` rather than scattering several `requirements.txt` files across workflow stages.
- Use Ruff for formatting and linting.
- Use pytest for tests.
- A line length of up to 150 characters is acceptable, but wrap earlier when a line becomes difficult to read.

Modern type syntax is preferred:

```python
def find_page(page_id: str) -> Page | None:
    ...

paths: list[Path] = []
counts: dict[str, int] = {}
```

Avoid older forms when the modern equivalent is available:

```python
# Avoid in new code
Optional[Page]
List[Path]
Dict[str, int]
```

Do not add this import as routine boilerplate:

```python
from __future__ import annotations
```

Python 3.12+ does not require it for modern annotations such as `int | None`, `list[str]`, or `dict[str, int]`. Use it only when postponed annotation evaluation solves a concrete problem, such as numerous forward references, a typing-related circular import, or a library that specifically benefits from it. For an isolated forward reference, a quoted annotation is often simpler:

```python
def parent(self) -> "Page | None":
    ...
```

When a library inspects annotations at runtime, confirm how that library handles postponed or string annotations before enabling the future import.

## Project organization

The template's numbered directories describe stable workflow stages:

```text
code/
├── 1-download/
├── 2-inventory/
├── 3-extraction/
├── 4-standardization/
├── 5-reconciliation/
├── 6-exploration/
└── 7-quality-control/
```

Number the stages, not every execution. A workflow may legitimately return from standardization to manual review.

Use hyphens in descriptive directory names if desired. Use underscores in Python filenames and importable module names:

```text
2-inventory/review_pages.py
src/magazine_pipeline/review_store.py
```

For a project with several scripts or webapps, use a small shared package:

```text
src/
└── magazine_pipeline/
    ├── paths.py
    ├── manifest.py
    ├── extraction_results.py
    ├── review_store.py
    ├── flatten.py
    └── provenance.py
```

Install the package in editable mode during development. Do not modify `sys.path` inside scripts to make imports work.

Do not create a shared package merely because a project has started. Create it when logic is reused, needs focused tests, or is making an entry-point script too large.

## Script and module size

File length is a warning signal, not an absolute rule.

- Entry-point scripts should usually be about 50–150 lines.
- Ordinary implementation modules should usually remain below roughly 400 lines.
- At 500 lines, pause and check whether the file has more than one responsibility.
- A file approaching 800 lines should have a clear reason to remain intact.

Split by real responsibility, not arbitrary line counts. Good boundaries include:

- loading a manifest;
- finding candidate pages;
- storing manual decisions;
- flattening nested records;
- serving a review app;
- reconciling repeated observations;
- writing an export.

Do not split one readable operation into many one-function modules. Do not introduce layers solely to make files shorter.

Large webapps should normally separate:

- Python server and state logic;
- HTML templates;
- JavaScript and CSS, when they become substantial;
- durable review storage.

Tests should also be split by behavior when one test file becomes difficult to navigate.

## Entry points and command-line interfaces

Entry-point scripts intended to be run directly should include a shebang and
have their executable bit set. Use `#!/usr/bin/env python3` when the project
environment will already be active. In uv-managed projects, use
`#!/usr/bin/env -S uv run` so commands such as `./script_name.py` use the
project environment automatically.

Every executable script should have a clear `main()` function:

```python
def main() -> None:
    settings = load_project_settings()
    build_manifest(settings.sources, settings.manifest)


if __name__ == "__main__":
    main()
```

Do not write:

```python
def main(argv: list[str] | None = None) -> None:
    ...
```

Do not manipulate `sys.argv` or pass argument lists through application functions. Tests should call the underlying Python function with ordinary values.

Persistent choices belong in `project.toml` or another small configuration file. Examples include standard paths, publication identifiers, default ports, and default worker counts.

Keep command-line options for choices that genuinely change a particular run, such as:

- selecting a table family;
- limiting a development sample;
- naming one or more pages;
- starting a webapp without opening a browser;
- retrying cached errors;
- explicitly authorizing a full run.

For a script with no meaningful choices, use no command-line parser at all. For a script with several useful options, prefer a small Typer interface over repeated `argparse` and explicit `argv` plumbing.

Avoid numerous positional arguments. Prefer named options whose purpose is visible in the command.

Full-corpus LLM work must require an explicit option such as `--all`. A bounded sample should be the easy default.

## Configuration

Use configuration for stable project facts, not for every implementation detail.

Good configuration values include:

- relative source and output directories;
- publication identifiers;
- supported table identifiers;
- review-app ports;
- safe default sample sizes;
- default model settings;
- reconciliation windows or thresholds.

Do not store API keys, passwords, or authentication tokens in committed configuration files. Use environment variables or provider authentication.

Keep machine-specific paths in a local, ignored file or an environment variable. Keep portable project paths relative to the project root.

Read TOML with the standard-library `tomllib` unless a more elaborate configuration system becomes genuinely necessary.

## Paths and files

Use `Path` everywhere inside Python code:

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCES_DIR = PROJECT_ROOT / "sources"
MANIFEST_PATH = PROJECT_ROOT / "data" / "pages.tsv"
```

Centralize standard project paths in one module rather than recalculating slightly different project roots in many scripts.

Core functions should accept `Path` objects instead of depending entirely on global constants:

```python
def export_records(source: Path, destination: Path) -> int:
    ...
```

Entry points may supply the project defaults. Tests may supply temporary paths.

Prefer these `Path` methods:

```python
path.exists()
path.is_file()
path.mkdir(parents=True, exist_ok=True)
path.read_text(encoding="utf-8")
path.write_text(text, encoding="utf-8")
path.relative_to(root)
```

Avoid `os.path` in new code unless an external API specifically requires strings.

Use stable relative source paths for page and document identity. Do not assume a basename is unique across magazine issues, boxes, folders, or datasets.

## Functions and classes

Functions are the default.

A function should usually do one recognizable operation. It may be long enough to show that operation coherently; it does not need to be fragmented into many tiny helpers.

Separate pure transformations from file or network access when doing so makes testing easier:

```python
def normalize_amount(raw: str) -> Decimal | None:
    ...


def load_records(path: Path) -> list[Record]:
    ...
```

Use a class when there is real persistent state or a natural data object, for example:

- a review store;
- an HTTP server;
- a validated extraction record;
- a small immutable settings object.

Do not introduce classes merely to group related functions.

Avoid speculative architecture:

- abstract base classes with only one implementation;
- factories that simply call a constructor;
- dependency-injection containers;
- service/repository/controller layers for a local research script;
- plugin systems before there is more than one genuine plugin;
- wrapper classes that expose an underlying library unchanged.

A table registry can begin as a dictionary or a small directory loader. It does not need a framework.

Prefer composition to inheritance. Use inheritance only when the shared behavior is substantial and stable.

## Data models and typing

Add type hints to public functions and important internal boundaries. Do not annotate every local variable when the type is obvious.

Use:

- Pydantic models for LLM schemas and validation of external structured data;
- dataclasses for small internal data containers when they improve clarity;
- ordinary dictionaries when the structure is local, short-lived, and obvious.

Do not convert every concept into a model class.

Validate untrusted data near the boundary where it enters the program. After validation, internal code should be able to rely on the stated types.

Use `Any` sparingly. It is reasonable at an unvalidated JSON boundary, but the data should soon be validated or narrowed.

## Naming

Use domain language rather than generic software language.

Good script names describe an action:

```text
build_manifest.py
find_candidates.py
serve_page_review.py
extract_pages.py
export_reviewed.py
build_review_queue.py
link_repeated_values.py
```

Good module names describe a responsibility:

```text
manifest.py
page_identity.py
review_store.py
extraction_results.py
reconciliation.py
```

Avoid vague names when a specific name is available:

```text
utils.py
helpers.py
manager.py
processor.py
common.py
run.py
final.py
final2.py
new_version.py
```

Use verbs for functions and nouns for data:

```python
load_manifest()
find_candidate_pages()
write_reviewed_records()

page_manifest
candidate_pages
reviewed_records
```

Use `is_`, `has_`, or `should_` for booleans.

## Comments and documentation

Keep comments minimal. Comments should explain why something is necessary, a historical complication, or a non-obvious rule. They should not narrate visible Python syntax.

Good:

```python
# A later issue may revise an estimate, so retain both observations.
```

Unhelpful:

```python
# Loop over the records
for record in records:
    ...
```

Use docstrings for public functions or modules whose behavior, assumptions, or output are not obvious from the name and signature.

Do not add large blocks of newly commented-out dead code; Git preserves history. However, do not remove user-authored commented reference code without confirming that it is no longer useful. If reference code must remain, label why it is being kept.

Keep the project-root `README.md` as the single operational runbook. Update its exact command order, expected outputs, current status, principal datasets, known gaps, and QC state whenever behavior changes. Do not create `RUNBOOK.md` or another overlapping run-order document.

## Errors and console output

Fail clearly when an assumption is violated. Do not silently skip records unless skipping is an explicit, reported policy.

Avoid bare or overly broad exception handling:

```python
# Avoid
try:
    ...
except Exception:
    pass
```

Catch expected exceptions near the boundary where recovery is possible. Include useful context such as the page ID, record ID, and path.

For short scripts, clear `print()` output is sufficient. Use `logging` when concurrency, verbosity levels, or persistent logs make it useful. Do not build a custom logging framework.

At completion, report at least:

- how many items were read and written;
- where the output was written;
- how many errors or unresolved items remain;
- whether the run used cached or newly generated results.

When using subprocesses:

- pass arguments as a list;
- use `check=True` when failure should stop the operation;
- avoid `shell=True`;
- show enough of an error to diagnose the failed command.

## Research data and durable state

Keep these project categories separate:

```text
sources/     Source metadata and small original inputs; never rewritten
manual/      Human decisions; never regenerated
temp/        Regenerable intermediate artifacts
output/      Figures, queues, reports, receipts, and release artifacts
data/        Flat coauthor-ready DTA/TSV files only
```

Never make a webapp edit the original LLM response or the source manifest directly. Store manual corrections separately and rebuild the combined result deterministically.

Review queues are generated artifacts. Completed review decisions are durable human work.

Use stable record identifiers. Preserve manifest order unless an output has an explicitly documented alternative order.

Write TSV files with UTF-8, explicit tab delimiters, and a stable column order. Use the `csv` module rather than constructing rows with string concatenation.

Write durable manual files atomically when practical: write a temporary file in the same directory, validate it, and then replace the destination.

Never conflate:

- blank and explicit zero;
- raw and normalized values;
- publication date and reference period;
- an original estimate and a later revision;
- model output and manual correction.

## LLM extraction projects

Treat the prompt and schema as versioned extraction definitions, not incidental strings embedded in a script.

Keep source-specific page scoring in `code/2-inventory/selection_rules.py`. Keep the current extraction definition together:

```text
code/3-extraction/definitions/
├── prompt.md
└── schema.py
```

When sources contain materially different form regimes, use a small explicit regime registry and clearly named definition subdirectories. Do not duplicate a complete extraction runner for each year or table.

Record or hash material extraction inputs, including the prompt, schema, model, reasoning level, media settings, and pipeline version. A changed extraction definition must not silently reuse incompatible cached answers.

Use a stable relative source path as page identity. For multi-page documents, include the ordered page identities in the cache identity.

Write each page result immediately so that an interrupted run can resume.

Keep high-volume caches outside synchronized folders under the configured `/home/sergio/data/<slug>/` root. Keep prompts, schemas, representative fixtures, tests, source metadata, manual decisions, and compact run receipts inside the project.

Production safeguards:

- bounded samples are the default;
- full extraction requires `--all` or an equally explicit authorization;
- expected page count and likely cost are displayed before production;
- cached errors are not retried unless requested;
- outputs can be rebuilt from cache without new model calls.

Export both:

- complete nested JSONL records for audit and future reuse;
- flattened TSV files for analysis.

Keep a compact manually checked gold sample that covers clear pages, difficult pages, revisions, unusual marks, blanks, zeros, and other known risks. Tests and ordinary development must not make real model calls.

## Local review apps

Use separate apps for meaningfully different review tasks, such as page selection and extracted-record correction. Do not copy an entire app for every table family when a shared app can load a different schema or table definition.

Keep the app thin:

- Python serves records and validates saves;
- HTML and JavaScript handle presentation and interaction;
- a review-store module owns durable state;
- table-specific rules live in the table definition or a small adapter.

Bind local review apps to `127.0.0.1` by default.

Saving an unchanged record may be meaningful: it records that a person checked the page. Store that review status explicitly.

If a later validation run raises a new concern, allow the record to be queued again without discarding its earlier manual values.

## Testing

Test the important behavior, not implementation trivia.

High-value tests include:

- manifest ordering and stable page identities;
- precedence of manual decisions over machine suggestions;
- page grouping and continuation rules;
- schema validation;
- flattening nested records;
- preservation of zero versus missing;
- cache signatures and cache-only export;
- atomic manual-review saves;
- review-queue generation;
- revision matching;
- deterministic output ordering.

Use pytest's `tmp_path` for filesystem tests. Pass temporary `Path` objects into core functions rather than patching many global paths.

Mock or replace network and LLM boundaries. Unit tests must not incur cost or require authentication.

Test command-line parsing lightly. Most tests should call ordinary Python functions rather than simulate `argv`.

## Dependencies

Prefer the standard library when it is clear and sufficient. A small, well-maintained dependency is appropriate when it removes substantial code or complexity.

Declare every imported third-party package in the root `pyproject.toml`; do not install an undeclared package globally merely to make one project run. Yachay, Locro, and Banknorm are shared read-only dependencies. Use their configured non-editable sources, record their Git revision or tree hash when they affect results, and do not patch or silently upgrade them from a research project.

Before adding a framework, ask:

1. What concrete problem does it solve now?
2. Is that problem larger than the new dependency and abstraction?
3. Can a short direct implementation remain readable?

Do not rewrite working code merely to adopt a more fashionable architecture or library.

## Final checklist for new code

Before considering a script or feature complete, check:

- Is the purpose obvious from its filename?
- Is the entry-point script small and readable?
- Does shared logic have a real reason to be shared?
- Are paths represented with `Path`?
- Are stable settings in configuration rather than scattered constants?
- Is there any unnecessary `argv`, `sys.path`, or `os.path` handling?
- Are original, generated, and manually edited data kept separate?
- Can the operation be safely rerun?
- Will an interrupted expensive run resume?
- Are full or destructive operations explicit?
- Are errors visible and contextual?
- Are outputs deterministic and clearly reported?
- Are important transformations tested without network calls?
- Could a simpler function or module replace a new class or abstraction?
- Will the code still be understandable after several months away from the project?
