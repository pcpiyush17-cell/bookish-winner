from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_candidate_freeze import (
    CandidateFreezeConfig,
    CandidateFreezeGate,
    CandidateProvenance,
    FrozenArtifact,
    ResearchCandidateFreezeError,
    freeze_file,
    write_candidate_dossier,
)
from personal_quant.research_model_evaluation import ModelEvaluationResult
from tests.test_research_model_evaluation import (
    _boosted_config,
    _dataset,
    _ridge_config,
    _workflow,
)

CLI = CliRunner()
ARTIFACT_NAMES = ("ml_dataset", "ridge", "boosted", "stability", "evaluation")


def _config() -> CandidateFreezeConfig:
    return CandidateFreezeConfig(
        schema_version=1,
        freeze_id="freeze_test",
        required_workflow_id="evaluation_test",
        required_stability_gate_id="gate_test",
        required_stability_decision="BOOSTED_VALIDATION_CANDIDATE",
        required_config_artifacts=ARTIFACT_NAMES,
        selection_window="validation",
        final_holdout_access=False,
        final_holdout_consumed=False,
        eligible_for_operational_promotion=False,
        production_order_routing=False,
    )


def _provenance(names: tuple[str, ...] = ARTIFACT_NAMES) -> CandidateProvenance:
    return CandidateProvenance(
        git_commit_sha="a" * 40,
        uv_lock_sha256="b" * 64,
        config_artifacts=tuple(
            FrozenArtifact(name=name, path=f"config/{name}.yaml", sha256=str(index) * 64)
            for index, name in enumerate(names, start=1)
        ),
    )


def _evaluation() -> ModelEvaluationResult:
    result = _workflow().evaluate(_dataset())
    assert result.stability.decision == "BOOSTED_VALIDATION_CANDIDATE"
    return result


def _gate() -> CandidateFreezeGate:
    return CandidateFreezeGate(_config(), _ridge_config(), _boosted_config())


def test_freeze_creates_deterministic_holdout_ready_dossier() -> None:
    first = _gate().freeze(_evaluation(), _provenance())
    second = _gate().freeze(_evaluation(), _provenance())

    assert first.status == "HOLDOUT_READY"
    assert first.dossier_sha256 == second.dossier_sha256
    assert len(first.dossier_sha256) == 64
    assert first.one_way_cost_bps == Decimal(10)
    assert first.final_holdout_access is False
    assert first.final_holdout_consumed is False
    assert first.eligible_for_operational_promotion is False
    assert first.production_order_routing is False


def test_freeze_rejects_failed_stability_gate() -> None:
    evaluation = _evaluation()
    failed_stability = replace(
        evaluation.stability,
        decision="RETAIN_RIDGE",
        failure_reasons=("rmse_improvement_insufficient",),
    )

    with pytest.raises(ResearchCandidateFreezeError) as caught:
        _gate().freeze(replace(evaluation, stability=failed_stability), _provenance())

    assert caught.value.code == "research_candidate_freeze_gate_failed"


def test_freeze_rejects_tampered_evaluation_report() -> None:
    evaluation = replace(_evaluation(), report_sha256="0" * 64)

    with pytest.raises(ResearchCandidateFreezeError) as caught:
        _gate().freeze(evaluation, _provenance())

    assert caught.value.code == "research_candidate_freeze_report_invalid"


def test_freeze_rejects_incomplete_provenance() -> None:
    with pytest.raises(ResearchCandidateFreezeError) as caught:
        _gate().freeze(_evaluation(), _provenance(ARTIFACT_NAMES[:-1]))

    assert caught.value.code == "research_candidate_freeze_provenance_incomplete"


def test_gate_rejects_mismatched_cost_contract() -> None:
    boosted = _boosted_config().model_copy(update={"one_way_cost_bps": Decimal(11)})

    with pytest.raises(ResearchCandidateFreezeError) as caught:
        CandidateFreezeGate(_config(), _ridge_config(), boosted)

    assert caught.value.code == "research_candidate_freeze_contract_mismatch"


def test_freeze_file_hashes_only_project_files(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    artifact_path = root / "config.yaml"
    artifact_path.write_text("fixed: true\n", encoding="utf-8")

    artifact = freeze_file(artifact_path, "evaluation", root)

    assert artifact.path == "config.yaml"
    assert len(artifact.sha256) == 64
    outside = tmp_path / "outside.yaml"
    outside.write_text("unsafe: true\n", encoding="utf-8")
    with pytest.raises(ResearchCandidateFreezeError) as caught:
        freeze_file(outside, "outside", root)
    assert caught.value.code == "research_candidate_freeze_artifact_invalid"


def test_dossier_write_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    dossier = _gate().freeze(_evaluation(), _provenance())

    path = write_candidate_dossier(dossier, tmp_path)

    assert write_candidate_dossier(dossier, tmp_path) == path
    path.write_text("conflict", encoding="utf-8")
    with pytest.raises(ResearchCandidateFreezeError) as caught:
        write_candidate_dossier(dossier, tmp_path)
    assert caught.value.code == "research_candidate_freeze_dossier_conflict"


def test_cli_validates_candidate_freeze_contract() -> None:
    result = CLI.invoke(app, ["research-candidate-freeze-check"])

    assert result.exit_code == 0
    assert "Research candidate freeze valid: qr_candidate_freeze_v1" in result.stdout
    assert "Final holdout access: disabled; consumed: NO" in result.stdout


def test_config_load_wraps_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: [", encoding="utf-8")

    with pytest.raises(ResearchCandidateFreezeError) as caught:
        CandidateFreezeConfig.load(path)

    assert caught.value.code == "research_candidate_freeze_config_invalid"
