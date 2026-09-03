"""Offline tests for page-level rerun calibration, ranking, and cap gates."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import rerun_priority  # noqa: E402


def tsv_bytes(fields: tuple[str, ...], rows: list[dict[str, object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def write_tsv(path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tsv_bytes(fields, rows))


def read_tsv(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8")), delimiter="\t"))


def page_row(page: int, *, record_count: int = 100, eligible: int = 1) -> dict[str, object]:
    return {
        "page_id": f"pdfs/1900-1.pdf#page={page}",
        "source_id": "rand_1900_1",
        "source_sha256": "a" * 64,
        "year": 1900,
        "edition": 1,
        "pdf_part": 0,
        "record_count": record_count,
        "eligible": eligible,
    }


def signal_row(
    page: int,
    *,
    rule_id: str,
    family: str,
    tier: int,
    magnitude: str = "1",
    entity_id: str = "bank-1",
    directness: str = "observed_page",
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "page_id": f"1900-1.pdf#page={page}",
        "rule_id": rule_id,
        "signal_family": family,
        "tier": tier,
        "entity_id": entity_id,
        "directness": directness,
        "magnitude": magnitude,
        "evidence_json": json.dumps(evidence or {"value": page}, sort_keys=True),
    }


def manual_signals() -> list[dict[str, object]]:
    return [
        signal_row(
            1,
            rule_id="documented_failure",
            family="documented",
            tier=1,
            entity_id="manual:1",
            evidence={"evidence_family": "documented_failure", "note": "known failure"},
        ),
        signal_row(
            2,
            rule_id="location_correction_cluster",
            family="identity",
            tier=4,
            entity_id="manual:2",
            evidence={"evidence_family": "location_correction", "note": "manual identity evidence"},
        ),
        signal_row(
            3,
            rule_id="correspondent_parse_error",
            family="correspondent",
            tier=4,
            entity_id="manual:3",
            evidence={"evidence_family": "correspondent_parse", "note": "manual correspondent evidence"},
        ),
        signal_row(
            4,
            rule_id="scope_exclusion",
            family="known_negative",
            tier=4,
            entity_id="",
            evidence={"exclusions": [{"reason": "advertisement"}]},
        ),
    ]


def candidate_signals() -> list[dict[str, object]]:
    return [
        signal_row(
            page,
            rule_id="page_density_collapse" if page == 5 else "capital_factor_10",
            family="structure" if page == 5 else "capital",
            tier=1 if page == 5 else 2,
            magnitude=str(20 - page),
            entity_id=f"bank-{page}",
        )
        for page in range(5, 9)
    ]


def ledger_row(page: int, status: str = "completed") -> dict[str, object]:
    return {
        "page_id": f"1900-1.pdf#page={page}",
        "source_id": "rand_1900_1",
        "year": 1900,
        "edition": 1,
        "physical_page": page,
        "reason": "fixture",
        "status": status,
        "run_id": "run-1",
        "contract_signature": "b" * 64,
        "completed_at": "2030-01-01T00:00:00+00:00",
    }


LEDGER_FIELDS = tuple(ledger_row(1))


def policy(*, trial_max_pages: int = 3, minimum_reviews: int = 4) -> rerun_priority.CalibrationPolicy:
    return rerun_priority.CalibrationPolicy(
        documented_pages=4,
        candidate_pages=4,
        control_pages=4,
        minimum_candidate_reviews=minimum_reviews,
        minimum_observed_precision=0.70,
        minimum_wilson_lower=0.50,
        wilson_z=1.96,
        trial_max_pages=trial_max_pages,
    )


def write_project_config(root: Path) -> None:
    (root / "project.toml").write_text(
        """[restoration]
provisional_page_denominator = 100
provisional_rerun_fraction = 0.05
provisional_rerun_ceiling = 100

[review_prioritization]
paid_ledger = "manual/rerun_pages.tsv"
calibration_decisions = "manual/page_review_calibration.tsv"
calibration_pages = 12
calibration_documented = 4
calibration_candidates = 4
calibration_controls = 4
minimum_candidate_reviews = 4
minimum_observed_precision = 0.70
minimum_wilson_lower_95 = 0.50
trial_max_pages = 3
""",
        encoding="utf-8",
    )


def fixture_inputs(
    root: Path,
    *,
    extra_signals: list[dict[str, object]] | None = None,
    ledger_rows: list[dict[str, object]] | None = None,
    reverse_inputs: bool = False,
) -> tuple[Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    write_project_config(root)
    record_counts = {5: 10, 6: 20, 7: 30, 8: 40, 9: 11, 10: 21, 11: 31, 12: 41, 13: 1}
    pages_rows = [
        page_row(page, record_count=record_counts.get(page, 100), eligible=0 if page == 4 else 1)
        for page in range(1, 101)
    ]
    signals_rows = [*manual_signals(), *candidate_signals(), *(extra_signals or [])]
    if reverse_inputs:
        pages_rows.reverse()
        signals_rows.reverse()
    pages = root / "data" / "rerun_priority_pages.tsv"
    signals = root / "data" / "rerun_priority_signals.tsv"
    ledger = root / "manual" / "rerun_pages.tsv"
    write_tsv(pages, rerun_priority.PAGE_INPUT_FIELDS, pages_rows)
    write_tsv(signals, rerun_priority.SIGNAL_INPUT_FIELDS, signals_rows)
    write_tsv(ledger, LEDGER_FIELDS, ledger_rows or [])
    return pages, signals, ledger


def build(
    root: Path,
    pages: Path,
    signals: Path,
    ledger: Path,
    *,
    labels: Path | None = None,
    calibration_policy: rerun_priority.CalibrationPolicy | None = None,
) -> rerun_priority.RankingArtifacts:
    return rerun_priority.build_ranking(
        project_root=root,
        pages_path=pages,
        signals_path=signals,
        ledger_path=ledger,
        labels_path=labels,
        output_directory=root / "output" / "rerun-ranking",
        cap_policy=rerun_priority.CapPolicy(100, Decimal("0.05"), 100),
        calibration_policy=calibration_policy or policy(),
    )


def write_labels(root: Path, sample: list[dict[str, str]], outcomes: dict[str, str]) -> Path:
    path = root / "manual" / "page_review_calibration.tsv"
    write_tsv(
        path,
        rerun_priority.LABEL_INPUT_FIELDS,
        [
            {
                "page_id": row["page_id"],
                "expected_page_evidence_sha256": row["expected_page_evidence_sha256"],
                "outcome": outcomes[row["page_id"]],
                "notes": "fixture review",
            }
            for row in sample
            if row["page_id"] in outcomes
        ],
    )
    return path


def candidate_outcomes(sample: list[dict[str, str]], outcomes: list[str]) -> dict[str, str]:
    candidates = [row for row in sample if row["stratum"] == "candidate"]
    return {row["page_id"]: outcome for row, outcome in zip(candidates, outcomes, strict=False)}


def test_page_identity_wilson_and_default_policy() -> None:
    assert rerun_priority.canonical_page_id("pdfs/volume/1900-1.pdf#page=7") == (
        "volume/1900-1.pdf#page=7",
        "volume/1900-1.pdf",
        7,
    )
    for invalid in ("/tmp/a.pdf#page=1", "../a.pdf#page=1", "a.pdf#page=0", "a.pdf#page=01", "a.txt#page=1"):
        with pytest.raises(ValueError):
            rerun_priority.canonical_page_id(invalid)
    lower, upper = rerun_priority.wilson_interval(20, 20)
    assert lower == pytest.approx(0.8388699, abs=1e-7)
    assert upper == pytest.approx(1.0)
    defaults = rerun_priority.CalibrationPolicy()
    assert (defaults.documented_pages, defaults.candidate_pages, defaults.control_pages) == (50, 50, 50)
    assert (defaults.minimum_candidate_reviews, defaults.minimum_observed_precision, defaults.minimum_wilson_lower) == (20, 0.70, 0.50)
    assert defaults.trial_max_pages == 100


def test_sample_is_exact_page_level_stratified_and_matched(tmp_path: Path) -> None:
    pages, signals, ledger = fixture_inputs(tmp_path)
    artifacts = build(tmp_path, pages, signals, ledger)
    sample = read_tsv(artifacts.files["calibration_sample.tsv"])
    assert len(sample) == len({row["page_id"] for row in sample}) == 12
    assert [sum(row["stratum"] == name for row in sample) for name in ("documented", "candidate", "control")] == [4, 4, 4]
    documented = [row for row in sample if row["stratum"] == "documented"]
    categories = set().union(*(set(row["manual_evidence_categories"].split(";")) for row in documented))
    assert categories == set(rerun_priority.MANUAL_EVIDENCE_CATEGORIES)
    candidates = [row for row in sample if row["stratum"] == "candidate"]
    assert [row["page_id"] for row in candidates] == [f"1900-1.pdf#page={page}" for page in range(5, 9)]
    controls = [row for row in sample if row["stratum"] == "control"]
    assert [row["page_id"] for row in controls] == [f"1900-1.pdf#page={page}" for page in range(9, 13)]
    assert {row["match_level"] for row in controls} == {"same_issue_part"}
    assert {row["record_count_difference"] for row in controls} == {"1"}
    assert artifacts.receipt["calibration_sample_rows"] == 12
    assert artifacts.receipt["selected_queue_rows"] == 0


def test_sample_does_not_depend_on_input_order(tmp_path: Path) -> None:
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first = build(first_root, *fixture_inputs(first_root))
    second = build(second_root, *fixture_inputs(second_root, reverse_inputs=True))
    assert first.files == second.files


def test_automated_identity_signal_is_candidate_not_manual_evidence(tmp_path: Path) -> None:
    pages, signals, ledger = fixture_inputs(
        tmp_path,
        extra_signals=[
            signal_row(
                13,
                rule_id="raw_identity_field_loss",
                family="identity",
                tier=2,
                magnitude="100",
                evidence={"raw_missing_name": 10},
            )
        ],
    )
    sample = read_tsv(build(tmp_path, pages, signals, ledger).files["calibration_sample.tsv"])
    assert "1900-1.pdf#page=13" in {row["page_id"] for row in sample if row["stratum"] == "candidate"}
    assert "1900-1.pdf#page=13" not in {row["page_id"] for row in sample if row["stratum"] == "documented"}


@pytest.mark.parametrize(
    ("outcomes", "expected_gate"),
    [
        (["confirmed_problem"] * 3, False),
        (["confirmed_problem", "confirmed_problem", "not_problem", "not_problem"], False),
        (["confirmed_problem", "confirmed_problem", "confirmed_problem", "not_problem"], False),
        (["confirmed_problem"] * 4, True),
    ],
)
def test_candidate_gate_requires_reviews_point_precision_and_wilson_lower(
    tmp_path: Path,
    outcomes: list[str],
    expected_gate: bool,
) -> None:
    pages, signals, ledger = fixture_inputs(tmp_path)
    sample = read_tsv(build(tmp_path, pages, signals, ledger).files["calibration_sample.tsv"])
    labels = write_labels(tmp_path, sample, candidate_outcomes(sample, outcomes))
    artifacts = build(tmp_path, pages, signals, ledger, labels=labels)
    assert artifacts.receipt["calibration_gate_passed"] is expected_gate
    assert bool(read_tsv(artifacts.files["selected_pages.tsv"])) is expected_gate


def test_documented_and_control_outcomes_are_diagnostic_only(tmp_path: Path) -> None:
    pages, signals, ledger = fixture_inputs(tmp_path)
    sample = read_tsv(build(tmp_path, pages, signals, ledger).files["calibration_sample.tsv"])
    outcomes = candidate_outcomes(sample, ["confirmed_problem"] * 4)
    outcomes.update({row["page_id"]: "not_problem" for row in sample if row["stratum"] != "candidate"})
    labels = write_labels(tmp_path, sample, outcomes)
    artifacts = build(tmp_path, pages, signals, ledger, labels=labels)
    results = artifacts.receipt["calibration_results"]
    assert artifacts.receipt["calibration_gate_passed"] is True
    assert results["candidate"]["observed_precision"] == 1.0
    assert results["documented"]["observed_precision"] == 0.0
    assert results["control"]["observed_precision"] == 0.0


def test_passing_gate_selects_only_localized_tier_1_to_3_and_obeys_trial_limit(tmp_path: Path) -> None:
    pages, signals, ledger = fixture_inputs(
        tmp_path,
        extra_signals=[
            signal_row(20, rule_id="support_only", family="capital", tier=4, magnitude="999"),
            signal_row(21, rule_id="issue_only", family="support", tier=4, magnitude="999", directness="issue_only"),
        ],
    )
    sample = read_tsv(build(tmp_path, pages, signals, ledger).files["calibration_sample.tsv"])
    labels = write_labels(tmp_path, sample, candidate_outcomes(sample, ["confirmed_problem"] * 4))
    artifacts = build(tmp_path, pages, signals, ledger, labels=labels)
    selected = read_tsv(artifacts.files["selected_pages.tsv"])
    assert len(selected) == 3
    assert "1900-1.pdf#page=1" in {row["page_id"] for row in selected}
    assert {row["best_tier"] for row in selected} <= {"1", "2", "3"}
    excluded = {f"1900-1.pdf#page={page}" for page in (2, 3, 4, 20, 21)}
    assert not excluded & {row["page_id"] for row in selected}
    priority = {row["page_id"]: row for row in read_tsv(artifacts.files["page_priority.tsv"])}
    assert priority["1900-1.pdf#page=20"]["selection_status"] == "tier4_only"
    assert artifacts.receipt["trial_selection_limit"] == 3


def test_remaining_five_percent_capacity_is_tighter_than_trial_limit(tmp_path: Path) -> None:
    pages, signals, ledger = fixture_inputs(tmp_path, ledger_rows=[ledger_row(page) for page in range(97, 101)])
    sample = read_tsv(build(tmp_path, pages, signals, ledger).files["calibration_sample.tsv"])
    labels = write_labels(tmp_path, sample, candidate_outcomes(sample, ["confirmed_problem"] * 4))
    artifacts = build(tmp_path, pages, signals, ledger, labels=labels)
    assert artifacts.receipt["computed_cap"] == 5
    assert artifacts.receipt["prior_paid_unique_pages"] == 4
    assert artifacts.receipt["trial_selection_limit"] == 1
    assert artifacts.receipt["selected_queue_rows"] == 1
    assert artifacts.receipt["remaining_after_selection"] == 0


def test_page_labels_are_evidence_bound_and_not_outside_sample(tmp_path: Path) -> None:
    pages, signals, ledger = fixture_inputs(tmp_path)
    sample = read_tsv(build(tmp_path, pages, signals, ledger).files["calibration_sample.tsv"])
    candidate = next(row for row in sample if row["stratum"] == "candidate")
    labels = write_labels(tmp_path, sample, {candidate["page_id"]: "confirmed_problem"})
    rows = read_tsv(labels.read_bytes())
    rows[0]["expected_page_evidence_sha256"] = "0" * 64
    write_tsv(labels, rerun_priority.LABEL_INPUT_FIELDS, rows)
    with pytest.raises(ValueError, match="Stale calibration label"):
        build(tmp_path, pages, signals, ledger, labels=labels)
    write_tsv(
        labels,
        rerun_priority.LABEL_INPUT_FIELDS,
        [
            {
                "page_id": "1900-1.pdf#page=100",
                "expected_page_evidence_sha256": "0" * 64,
                "outcome": "confirmed_problem",
                "notes": "not sampled",
            }
        ],
    )
    with pytest.raises(ValueError, match="outside the deterministic sample"):
        build(tmp_path, pages, signals, ledger, labels=labels)


def test_receipt_binds_everything_and_queue_loads_in_runner(tmp_path: Path) -> None:
    from histdata_pipeline.config import ProjectConfig

    pages, signals, ledger = fixture_inputs(tmp_path)
    sample = read_tsv(build(tmp_path, pages, signals, ledger).files["calibration_sample.tsv"])
    labels = write_labels(tmp_path, sample, candidate_outcomes(sample, ["confirmed_problem"] * 4))
    artifacts = build(tmp_path, pages, signals, ledger, labels=labels)
    rerun_priority.write_ranking(artifacts)
    receipt_path = tmp_path / "output/rerun-ranking/ranking_receipt.json"
    queue_path = tmp_path / "output/rerun-ranking/selected_pages.tsv"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_signature"}
    assert receipt["receipt_signature"] == rerun_priority.stable_hash(unsigned)
    assert receipt["calibration_sample_sha256"] == hashlib.sha256(artifacts.files["calibration_sample.tsv"]).hexdigest()
    assert receipt["calibration_labels_path"] == "manual/page_review_calibration.tsv"
    assert receipt["calibration_labels_sha256"] == hashlib.sha256(labels.read_bytes()).hexdigest()
    assert receipt["calibration_label_rows"] == 4
    assert receipt["selected_queue_sha256"] == hashlib.sha256(queue_path.read_bytes()).hexdigest()
    assert set(receipt["input_sha256s"]) == {
        "project.toml",
        "data/rerun_priority_pages.tsv",
        "data/rerun_priority_signals.tsv",
        "manual/page_review_calibration.tsv",
        "manual/rerun_pages.tsv",
    }

    runner_path = Path(__file__).resolve().parents[1] / "1-extract-data.py"
    spec = importlib.util.spec_from_file_location("page_calibration_contract_runner", runner_path)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)
    config = ProjectConfig(
        root=tmp_path,
        values={
            "restoration": {
                "provisional_page_denominator": 100,
                "provisional_rerun_fraction": 0.05,
                "provisional_rerun_ceiling": 100,
            },
            "review_prioritization": {
                "calibration_pages": 12,
                "calibration_documented": 4,
                "calibration_candidates": 4,
                "calibration_controls": 4,
                "minimum_candidate_reviews": 4,
                "minimum_observed_precision": 0.70,
                "minimum_wilson_lower_95": 0.50,
                "trial_max_pages": 3,
            },
        },
    )
    evidence, selected = runner.load_signed_queue(config, queue_path, limit=None)
    assert evidence.queue_rows == 3
    assert [row.selection_rank for row in selected] == [1, 2, 3]


def test_sample_shortages_and_input_safety_fail_closed(tmp_path: Path) -> None:
    pages, signals, ledger = fixture_inputs(tmp_path)
    rows = read_tsv(signals.read_bytes())
    write_tsv(signals, rerun_priority.SIGNAL_INPUT_FIELDS, [row for row in rows if row["signal_family"] != "correspondent"])
    with pytest.raises(ValueError, match="correspondent_manual"):
        build(tmp_path, pages, signals, ledger)

    pages, signals, ledger = fixture_inputs(tmp_path)
    with pytest.raises(ValueError, match="inside the V2 output"):
        rerun_priority.build_ranking(
            project_root=tmp_path,
            pages_path=pages,
            signals_path=signals,
            ledger_path=ledger,
            labels_path=None,
            output_directory=tmp_path / "temp/ranking",
            cap_policy=rerun_priority.CapPolicy(100, Decimal("0.05"), 100),
            calibration_policy=policy(),
        )
    with pytest.raises(ValueError, match="0.05"):
        rerun_priority.CapPolicy(100, Decimal("0.051"), 100).validate()


def test_bad_ledger_json_and_denominator_fail_closed(tmp_path: Path) -> None:
    pages, signals, ledger = fixture_inputs(tmp_path, ledger_rows=[ledger_row(99, "compeleted")])
    with pytest.raises(ValueError, match="Unknown ledger status"):
        build(tmp_path, pages, signals, ledger)

    pages, signals, ledger = fixture_inputs(tmp_path)
    rows = read_tsv(signals.read_bytes())
    rows[0]["evidence_json"] = '{"amount":100,"amount":1000}'
    write_tsv(signals, rerun_priority.SIGNAL_INPUT_FIELDS, rows)
    with pytest.raises(ValueError, match="Invalid evidence_json"):
        build(tmp_path, pages, signals, ledger)

    pages, signals, ledger = fixture_inputs(tmp_path)
    with pytest.raises(ValueError, match="does not match denominator"):
        rerun_priority.build_ranking(
            project_root=tmp_path,
            pages_path=pages,
            signals_path=signals,
            ledger_path=ledger,
            labels_path=None,
            output_directory=tmp_path / "output/rerun-ranking",
            cap_policy=rerun_priority.CapPolicy(99, Decimal("0.05"), 100),
            calibration_policy=policy(),
        )
