from dataclasses import replace

from qc_core import (
    Decision,
    QCConfig,
    adjudicate_flags,
    cross_section_checks,
    panel_checks,
    release_status,
    run_checks,
)


def config(shape: str = "panel") -> QCConfig:
    return QCConfig(
        dataset_shape=shape,
        key_fields=("entity_id", "period") if shape == "panel" else ("record_id",),
        entity_fields=("entity_id",) if shape == "panel" else (),
        time_field="period" if shape == "panel" else None,
        value_fields=("amount",),
        record_id_field="record_id",
        source_page_field="page_id",
        provenance_fields=("record_id", "page_id", "source_sha256", "contract_signature"),
        cross_group_fields=("group",) if shape == "cross-section" else (),
    )


def row(record_id: str, amount: str, *, entity: str = "city-a", period: str = "1", page: str = "p1", group: str = "g") -> dict[str, str]:
    return {
        "record_id": record_id,
        "entity_id": entity,
        "period": period,
        "amount": amount,
        "page_id": page,
        "source_sha256": "source-1",
        "contract_signature": "contract-1",
        "group": group,
    }


def test_panel_isolated_reversal_finds_extra_zero_pattern() -> None:
    rows = [
        row("r1", "1.1", period="1", page="p1"),
        row("r2", "12.0", period="2", page="p2"),
        row("r3", "1.3", period="3", page="p3"),
    ]

    flags, coverage = panel_checks(rows, config())

    isolated = [flag for flag in flags if flag["check_type"] == "isolated_reversal"]
    assert len(isolated) == 1
    assert isolated[0]["record_id"] == "r2"
    assert isolated[0]["observed"] == "1.1->12->1.3"
    assert [entry["period"] for entry in coverage] == ["1", "2", "3"]


def test_iso_date_spine_exposes_missing_period_as_blocking() -> None:
    rows = [
        row("r1", "10", period="1900-01-01"),
        row("r2", "11", period="1900-01-03"),
    ]

    flags, coverage = panel_checks(rows, config())

    assert [entry["period"] for entry in coverage] == ["1900-01-01", "1900-01-02", "1900-01-03"]
    missing = next(flag for flag in flags if flag["check_type"] == "zero_coverage_period")
    assert missing["period"] == "1900-01-02"
    assert missing["severity"] == "blocking"


def test_case_identity_is_stable_but_evidence_hash_changes() -> None:
    original = [row("r1", "1.1", period="1"), row("r2", "12", period="2"), row("r3", "1.3", period="3")]
    revised = [row("r1", "1.1", period="1"), row("r2", "13", period="2"), row("r3", "1.3", period="3")]
    first = next(flag for flag in panel_checks(original, config())[0] if flag["check_type"] == "isolated_reversal")
    second = next(flag for flag in panel_checks(revised, config())[0] if flag["check_type"] == "isolated_reversal")

    assert first["case_id"] == second["case_id"]
    assert first["evidence_hash"] != second["evidence_hash"]


def test_provenance_change_invalidates_prior_decision_evidence() -> None:
    original = [row("r1", "100")]
    revised = [dict(original[0], source_sha256="source-2", contract_signature="contract-2")]
    qc_config = replace(config("cross-section"), bounds={"amount": (0, 50)})

    first = next(flag for flag in run_checks(original, qc_config)[0] if flag["check_type"] == "out_of_bounds")
    second = next(flag for flag in run_checks(revised, qc_config)[0] if flag["check_type"] == "out_of_bounds")

    assert first["case_id"] == second["case_id"]
    assert first["evidence_hash"] != second["evidence_hash"]


def test_normal_extraction_status_and_page_type_fields_are_structural() -> None:
    failed = dict(row("r1", "10"), status="error", final_type="excluded")

    flags, _ = run_checks([failed], config("cross-section"))
    check_types = {flag["check_type"] for flag in flags}

    assert "extraction_failure" in check_types
    assert "invalid_page_selection" in check_types


def test_cross_section_robust_outlier_is_advisory() -> None:
    rows = [row(f"r{index}", value, group="same", page=f"p{index}") for index, value in enumerate(["10", "10", "10", "10", "100"], start=1)]

    flags = cross_section_checks(rows, config("cross-section"))

    outlier = next(flag for flag in flags if flag["check_type"] == "robust_cross_section_outlier")
    assert outlier["record_id"] == "r5"
    assert outlier["severity"] == "advisory"


def test_repeated_key_with_disagreement_blocks_release() -> None:
    rows = [row("r1", "10"), row("r2", "11")]
    qc_config = replace(config(), key_fields=("entity_id", "period"))

    flags, _ = run_checks(rows, qc_config)
    disagreement = next(flag for flag in flags if flag["check_type"] == "repeated_vintage_disagreement")
    status, blockers = release_status(flags, qc_config)

    assert disagreement["severity"] == "blocking"
    assert status == "fail"
    assert disagreement["case_id"] in blockers


def test_duplicate_case_id_does_not_depend_on_duplicate_count() -> None:
    qc_config = replace(config(), key_fields=("entity_id", "period"))
    two = [row("r1", "10"), row("r2", "11")]
    three_reordered = [row("r3", "12"), *reversed(two)]

    first = next(flag for flag in run_checks(two, qc_config)[0] if flag["check_type"] == "repeated_vintage_disagreement")
    second = next(flag for flag in run_checks(three_reordered, qc_config)[0] if flag["check_type"] == "repeated_vintage_disagreement")

    assert first["case_id"] == second["case_id"]
    assert first["evidence_hash"] != second["evidence_hash"]


def test_evidence_bound_decision_reviews_a_blocker() -> None:
    qc_config = replace(config("cross-section"), bounds={"amount": (0, 50)})
    flags, _ = run_checks([row("r1", "100")], qc_config)
    bounded = next(flag for flag in flags if flag["check_type"] == "out_of_bounds")
    decision = Decision(
        case_id=bounded["case_id"],
        expected_evidence_hash=bounded["evidence_hash"],
        disposition="source_verified",
        reason="The printed source clearly shows 100.",
        evidence_page="sources/book.pdf#page=4",
    )

    adjudicated, extras = adjudicate_flags(flags, {decision.case_id: decision}, qc_config)
    reviewed = next(flag for flag in adjudicated if flag["case_id"] == bounded["case_id"])
    status, blockers = release_status(adjudicated, qc_config)

    assert not extras
    assert reviewed["decision_status"] == "reviewed"
    assert status == "pass"
    assert not blockers


def test_changed_evidence_makes_decision_stale_and_blocking() -> None:
    qc_config = replace(config("cross-section"), bounds={"amount": (0, 50)})
    flags, _ = run_checks([row("r1", "100")], qc_config)
    bounded = next(flag for flag in flags if flag["check_type"] == "out_of_bounds")
    stale = Decision(
        case_id=bounded["case_id"],
        expected_evidence_hash="old-evidence",
        disposition="source_verified",
        reason="Old review",
        evidence_page="sources/book.pdf#page=4",
    )

    adjudicated, extras = adjudicate_flags(flags, {stale.case_id: stale}, qc_config)

    assert any(flag["check_type"] == "stale_qc_decision" for flag in extras)
    assert release_status(adjudicated, qc_config)[0] == "fail"


def test_corrected_decision_must_match_exact_applied_target() -> None:
    qc_config = replace(config("cross-section"), bounds={"amount": (0, 50)})
    flags, _ = run_checks([row("r1", "100")], qc_config)
    bounded = next(flag for flag in flags if flag["check_type"] == "out_of_bounds")
    decision = Decision(
        case_id=bounded["case_id"],
        expected_evidence_hash=bounded["evidence_hash"],
        disposition="corrected",
        reason="Corrected from the source image.",
        evidence_page="sources/book.pdf#page=4",
        correction_id="fix-1",
    )

    adjudicated, extras = adjudicate_flags(
        flags,
        {decision.case_id: decision},
        qc_config,
        applied_corrections={
            "fix-1": {
                "record_id": "r1",
                "field": "amount",
                "source_hash": "source-1",
                "contract_signature": "contract-1",
                "after": "99",
            }
        },
    )

    assert any(flag["check_type"] == "stale_qc_decision" for flag in extras)
    assert release_status(adjudicated, qc_config)[0] == "fail"


def test_page_cluster_is_blocking_and_keeps_page_identity() -> None:
    rows = [
        row("r1", "1.1", period="1", page="shared-page"),
        row("r2", "12", period="2", page="shared-page"),
        row("r3", "1.3", period="3", page="shared-page"),
    ]

    flags, _ = run_checks(rows, config())
    cluster = next(flag for flag in flags if flag["check_type"] == "clustered_value_anomalies")

    assert cluster["severity"] == "blocking"
    assert cluster["page_id"] == "shared-page"
