"""Sanity-check that every `backticked_identifier` mentioned in each
``prompt_<variant>.md`` corresponds to a real field on the matching
``schema_<variant>.Bank`` model.

Variants are auto-discovered from ``schema_*.py`` files in this directory.
Identifiers that aren't Bank fields but are valid tokens (``null``, ``None``,
``Correspondent`` sub-fields, ``Page`` fields, etc.) are ignored.

Exits with a non-zero status if any prompt references an identifier that does
not exist on its Bank model, making this script suitable as a pre-commit check.
"""

import importlib
import re
import sys
import typing
from pathlib import Path


CODE_DIR = Path(__file__).parent

# Non-field tokens that commonly appear inside backticks and are expected.
KNOWN_NON_FIELDS = {
    "null",
    "None",
    "true",
    "True",
    "false",
    "False",
    "is_advertisment",
    "banks",
    # Inline grammar examples in correspondents-parsing rules
    "and",
}

BACKTICK_IDENT_RE = re.compile(r"`([a-z_][a-z0-9_]*)`")


def discover_variants() -> list[str]:
    """Return sorted variant names (e.g. ['1879', '1888', ...])."""
    return sorted(
        p.stem.removeprefix("schema_")
        for p in CODE_DIR.glob("schema_*.py")
    )


def collect_literal_values(model_cls) -> set[str]:
    """Return all string values used in any Literal[...] field annotation on the model."""
    out: set[str] = set()
    for field in model_cls.model_fields.values():
        annotation = field.annotation
        if typing.get_origin(annotation) is typing.Literal:
            for value in typing.get_args(annotation):
                if isinstance(value, str):
                    out.add(value)
    return out


def check_variant(variant: str) -> list[str]:
    """Return identifiers referenced in the prompt that are not Bank fields."""
    mod = importlib.import_module(f"schema_{variant}")
    bank_fields = set(mod.Bank.model_fields.keys())
    corr_fields = set(mod.Correspondent.model_fields.keys())
    literal_values = collect_literal_values(mod.Bank) | collect_literal_values(mod.Correspondent)

    prompt_path = CODE_DIR / f"prompt_{variant}.md"
    text = prompt_path.read_text(encoding="utf-8")
    referenced = set(BACKTICK_IDENT_RE.findall(text))

    allowed = bank_fields | corr_fields | literal_values | KNOWN_NON_FIELDS
    return sorted(referenced - allowed)


def main() -> int:
    variants = discover_variants()
    any_unknown = False

    for variant in variants:
        unknown = check_variant(variant)
        if unknown:
            any_unknown = True
            print(f"schema_{variant}: UNKNOWN identifiers: {unknown}")
        else:
            print(f"schema_{variant}: OK")

    return 1 if any_unknown else 0


if __name__ == "__main__":
    sys.exit(main())
