from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_boosted_stumps import BoostedStumpsConfig
from personal_quant.research_ml_dataset import (
    MLDatasetConfig,
    MLDatasetResult,
    MLFeaturePoint,
    PurgedWalkForwardDatasetBuilder,
)
from personal_quant.research_model_evaluation import (
    ModelEvaluationConfig,
    ModelEvaluationWorkflow,
    ResearchModelEvaluationError,
    write_model_evaluation_report,
)
from personal_quant.research_model_stability import ModelStabilityConfig
from personal_quant.research_ridge_model import RidgeModelConfig

FEATURES = ("signal", "constant")
COST_MULTIPLIERS = (Decimal("1.0"), Decimal("1.5"), Decimal("2.0"))
CLI = CliRunner()


def _evaluation_config() -> ModelEvaluationConfig:
    return ModelEvaluationConfig(
        schema_version=1,
        workflow_id="evaluation_test",
        required_dataset_id="dataset_test",
        required_ridge_model_id="ridge_test",
        required_boosted_model_id="boosted_test",
        required_stability_gate_id="gate_test",
        selection_window="validation",
        final_holdout_access=False,
        eligible_for_operational_promotion=False,
        production_order_routing=False,
    )


def _ridge_config() -> RidgeModelConfig:
    return RidgeModelConfig(
        schema_version=1,
        model_id="ridge_test",
        required_dataset_id="dataset_test",
        feature_names=FEATURES,
        ridge_penalty=Decimal("0.1"),
        feature_std_floor=Decimal("0.000001"),
        maximum_absolute_prediction=Decimal("0.10"),
        minimum_train_samples=12,
        selection_fraction=Decimal("0.34"),
        require_positive_prediction=True,
        one_way_cost_bps=Decimal(10),
        cost_multipliers=COST_MULTIPLIERS,
        selection_window="validation",
        fixed_hyperparameters=True,
        production_order_routing=False,
    )


def _boosted_config() -> BoostedStumpsConfig:
    return BoostedStumpsConfig(
        schema_version=1,
        model_id="boosted_test",
        required_dataset_id="dataset_test",
        required_ridge_model_id="ridge_test",
        feature_names=FEATURES,
        estimators=10,
        learning_rate=Decimal("0.30"),
        minimum_leaf_samples=3,
        maximum_threshold_candidates=10,
        maximum_absolute_prediction=Decimal("0.10"),
        minimum_train_samples=12,
        selection_fraction=Decimal("0.34"),
        require_positive_prediction=True,
        one_way_cost_bps=Decimal(10),
        cost_multipliers=COST_MULTIPLIERS,
        selection_window="validation",
        fixed_hyperparameters=True,
        production_order_routing=False,
    )


def _stability_config() -> ModelStabilityConfig:
    return ModelStabilityConfig(
        schema_version=1,
        gate_id="gate_test",
        required_dataset_id="dataset_test",
        required_ridge_model_id="ridge_test",
        required_boosted_model_id="boosted_test",
        minimum_folds=2,
        minimum_rmse_improvement_fraction=Decimal(0),
        minimum_positive_ic_fold_fraction=Decimal("0.01"),
        maximum_degraded_rmse_fold_fraction=Decimal("0.99"),
        minimum_mean_information_coefficient=Decimal(0),
        minimum_selected_return_delta_pct=Decimal(0),
        required_cost_cases=("1.0x", "1.5x", "2.0x"),
        selection_window="validation",
        holdout_access=False,
        production_order_routing=False,
    )


def _workflow() -> ModelEvaluationWorkflow:
    return ModelEvaluationWorkflow(
        _evaluation_config(), _ridge_config(), _boosted_config(), _stability_config()
    )


def _dataset() -> MLDatasetResult:
    config = MLDatasetConfig(
        schema_version=1,
        dataset_id="dataset_test",
        feature_names=FEATURES,
        label_horizon_observations=1,
        minimum_train_observations=6,
        validation_observations=3,
        purge_observations=2,
        embargo_observations=1,
        minimum_folds=2,
        signal_execution_lag_observations=1,
        split_method="expanding_purged_walk_forward",
        selection_window="validation",
        production_order_routing=False,
    )
    instruments = ("NSE:AAA", "NSE:BBB", "NSE:CCC")
    count = 36
    signals = {
        instrument: tuple(
            Decimal(((index + offset * 2) % 7) - 3) / Decimal(10) for index in range(count)
        )
        for offset, instrument in enumerate(instruments)
    }
    targets = {
        instrument: tuple(value**2 - Decimal("0.04") for value in values)
        for instrument, values in signals.items()
    }
    prices = {instrument: [Decimal(100), Decimal(100)] for instrument in instruments}
    for instrument in instruments:
        for end_index in range(2, count):
            prices[instrument].append(
                prices[instrument][-1] * (Decimal(1) + targets[instrument][end_index - 2])
            )
    start = datetime(2020, 1, 1, tzinfo=UTC)
    points = tuple(
        MLFeaturePoint(
            start + timedelta(days=index),
            instrument,
            {"signal": signals[instrument][index], "constant": Decimal(1)},
            prices[instrument][index],
            True,
        )
        for index in range(count)
        for instrument in instruments
    )
    return PurgedWalkForwardDatasetBuilder(config).build(points)


def test_workflow_runs_models_and_gate_deterministically() -> None:
    dataset = _dataset()

    first = _workflow().evaluate(dataset)
    second = _workflow().evaluate(dataset)

    assert first.report_sha256 == second.report_sha256
    assert len(first.report_sha256) == 64
    assert first.dataset_sha256 == dataset.sha256
    assert first.ridge.dataset_sha256 == first.boosted.dataset_sha256 == dataset.sha256
    assert first.stability.dataset_sha256 == dataset.sha256
    assert first.final_holdout_access is False
    assert first.eligible_for_operational_promotion is False
    assert first.production_order_routing is False


def test_report_is_idempotent_and_rejects_conflicting_content(tmp_path: Path) -> None:
    result = _workflow().evaluate(_dataset())

    path = write_model_evaluation_report(result, tmp_path)

    assert write_model_evaluation_report(result, tmp_path) == path
    assert result.report_sha256 in path.name
    path.write_text("conflict", encoding="utf-8")
    with pytest.raises(ResearchModelEvaluationError) as caught:
        write_model_evaluation_report(result, tmp_path)
    assert caught.value.code == "research_model_evaluation_report_conflict"


def test_workflow_rejects_mismatched_component_identity() -> None:
    with pytest.raises(ResearchModelEvaluationError) as caught:
        ModelEvaluationWorkflow(
            _evaluation_config(),
            _ridge_config(),
            _boosted_config().model_copy(update={"required_ridge_model_id": "other"}),
            _stability_config(),
        )

    assert caught.value.code == "research_model_evaluation_identity_mismatch"


def test_workflow_rejects_wrong_dataset() -> None:
    with pytest.raises(ResearchModelEvaluationError) as caught:
        _workflow().evaluate(replace(_dataset(), dataset_id="wrong"))

    assert caught.value.code == "research_model_evaluation_dataset_invalid"


def test_cli_validates_versioned_workflow_contract() -> None:
    result = CLI.invoke(app, ["research-model-evaluation-check"])

    assert result.exit_code == 0
    assert "Research model evaluation valid: qr_model_evaluation_v1" in result.stdout
    assert "Final holdout access: disabled" in result.stdout


def test_config_load_wraps_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: [", encoding="utf-8")

    with pytest.raises(ResearchModelEvaluationError) as caught:
        ModelEvaluationConfig.load(path)

    assert caught.value.code == "research_model_evaluation_config_invalid"
