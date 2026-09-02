# Examples

The committed synthetic end-to-end fixture is generated in a temporary directory by
`tests/test_bounded_end_to_end.py`. It creates a two-page PDF, inventories the manual
source, applies reviewed positive and negative page decisions, reconstructs a seeded
contract cache without a model call, runs the Stata 19 stages, and verifies the release
manifest. No generated PDF, cache, or analytical output is retained in this repository.

Run only that offline example with:

```bash
uv run pytest tests/test_bounded_end_to_end.py
```

Project-specific small fixtures may be added beneath `examples/`. Keep them synthetic or
legally redistributable, document their purpose, and never use a fixture to encode a
manual research decision that belongs in `manual/`.
