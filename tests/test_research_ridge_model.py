from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_ml_dataset import (
    MLDatasetConfig,
    MLDatasetResult,
    MLFeaturePoint,
    PurgedWalkForwardDatasetBuilder,
)
from personal_quant.research_ridge_model import (
    PurgedWalkForwardRidgeBaseline,
    ResearchRidgeModelError,
    RidgeModelConfig,
)

CONFIG = Path("config/research/ridge_baseline_v1.yaml")
CLI = CliRunner()
INSTRUMENTS = ("NSE:AAA", "NSE:BBB", "NSE:CCC")
FEATURES = ("signal", "constant")


def _dataset_config() -> MLDatasetConfig:
    return MLDatasetConfig(
        schema_version=1,
        dataset_id="ridge_dataset_test",
        feature_names=FEATURES,
        label_horizon_observations=1,
        minimum_train_observations=5,
        validation_observations=3,
        purge_observations=2,
        embargo_observations=1,
        minimum_folds=2,
        signal_execution_lag_observations=1,
        split_method="expanding_purged_walk_forward",
        selection_window="validation",
        production_order_routing=False,
    )


def _model_config() -> RidgeModelConfig:
    return RidgeModelConfig(
        schema_version=1,
        model_id="ridge_test",
        required_dataset_id="ridge_dataset_test",
        feature_names=FEATURES,
        ridge_penalty=Decimal("0.1"),
        feature_std_floor=Decimal("0.000001"),
        maximum_absolute_prediction=Decimal("0.10"),
        minimum_train_samples=10,
        selection_fraction=Decimal("0.34"),
        require_positive_prediction=True,
        one_way_cost_bps=Decimal("10"),
        cost_multipliers=(Decimal("1.0"), Decimal("1.5"), Decimal("2.0")),
        selection_window="validation",
        fixed_hyperparameters=True,
        production_order_routing=False,
    )


def _points() -> tuple[MLFeaturePoint, ...]:
    observation_count = 30
    signals = {
        instrument: tuple(
            Decimal(((index + offset * 2) % 7) - 3) / Decimal(100)
            for index in range(observation_count)
        )
        for offset, instrument in enumerate(INSTRUMENTS)
    }
    prices: dict[str, list[Decimal]] = {
        instrument: [Decimal(100), Decimal(100)] for instrument in INSTRUMENTS
    }
    for instrument in INSTRUMENTS:
        for end_index in range(2, observation_count):
            prices[instrument].append(
                prices[instrument][-1] * (Decimal(1) + signals[instrument][end_index - 2])
            )
    start = datetime(2020, 1, 1, tzinfo=UTC)
    return tuple(
        MLFeaturePoint(
            start + timedelta(days=index),
            instrument,
            {"signal": signals[instrument][index], "constant": Decimal(1)},
            prices[instrument][index],
            True,
        )
        for index in range(observation_count)
        for instrument in INSTRUMENTS
    )


def _dataset() -> MLDatasetResult:
    return PurgedWalkForwardDatasetBuilder(_dataset_config()).build(_points())


def test_ridge_learns_signal_with_training_only_standardization() -> None:
    result = PurgedWalkForwardRidgeBaseline(_model_config()).evaluate(_dataset())

    assert result.fold_results[0].standardized_coefficients["signal"] > 0
    assert result.fold_results[0].feature_stds["constant"] == Decimal("0.000001")
    assert result.metrics.mean_validation_rmse < result.metrics.mean_baseline_rmse
    assert result.metrics.mean_information_coefficient > Decimal("0.99")
    assert result.metrics.positive_information_coefficient_folds == result.metrics.folds
    assert result.dataset_sha256 == _dataset().sha256
    assert result.eligible_for_operational_promotion is False
    assert result.production_order_routing is False


def test_later_feature_changes_cannot_change_earlier_fold_fit_or_predictions() -> None:
    baseline = PurgedWalkForwardRidgeBaseline(_model_config())
    original = baseline.evaluate(_dataset())
    changed = list(_points())
    point_index = 20 * len(INSTRUMENTS)
    point = changed[point_index]
    changed[point_index] = MLFeaturePoint(
        point.timestamp,
        point.instrument,
        {**point.features, "signal": Decimal("0.09")},
        point.price,
        point.eligible,
    )
    revised_dataset = PurgedWalkForwardDatasetBuilder(_dataset_config()).build(tuple(changed))
    revised = baseline.evaluate(revised_dataset)

    assert revised.fold_results[0] == original.fold_results[0]
    assert revised.dataset_sha256 != original.dataset_sha256


def test_cost_stress_event_control_and_results_are_immutable() -> None:
    result = PurgedWalkForwardRidgeBaseline(_model_config()).evaluate(_dataset())
    returns = result.metrics.mean_selected_forward_return_pct_by_cost

    assert returns["1.0x"] > returns["1.5x"] > returns["2.0x"]
    assert set(result.metrics.excess_return_pct_vs_equal_weight_by_cost) == {
        "1.0x",
        "1.5x",
        "2.0x",
    }
    with pytest.raises(TypeError):
        returns["1.0x"] = Decimal(0)  # type: ignore[index]
    with pytest.raises(TypeError):
        result.fold_results[0].standardized_coefficients["signal"] = Decimal(0)  # type: ignore[index]


def test_dataset_identity_feature_and_fold_guards_fail_closed() -> None:
    with pytest.raises(ResearchRidgeModelError) as error:
        PurgedWalkForwardRidgeBaseline(
            _model_config().model_copy(update={"required_dataset_id": "wrong"})
        ).evaluate(_dataset())
    assert error.value.code == "research_ridge_dataset_mismatch"

    with pytest.raises(ResearchRidgeModelError) as error:
        PurgedWalkForwardRidgeBaseline(
            _model_config().model_copy(update={"feature_names": ("signal",)})
        ).evaluate(_dataset())
    assert error.value.code == "research_ridge_features_mismatch"

    dataset = _dataset()
    bad_fold = replace(
        dataset.folds[0], train_sample_ids=(*dataset.folds[0].train_sample_ids, "unknown")
    )
    malformed = replace(dataset, folds=(bad_fold, *dataset.folds[1:]))
    with pytest.raises(ResearchRidgeModelError) as error:
        PurgedWalkForwardRidgeBaseline(_model_config()).evaluate(malformed)
    assert error.value.code == "research_ridge_fold_invalid"


def test_short_training_fold_and_prediction_clip_are_enforced() -> None:
    with pytest.raises(ResearchRidgeModelError) as error:
        PurgedWalkForwardRidgeBaseline(
            _model_config().model_copy(update={"minimum_train_samples": 1000})
        ).evaluate(_dataset())
    assert error.value.code == "research_ridge_train_short"

    clipped = PurgedWalkForwardRidgeBaseline(
        _model_config().model_copy(update={"maximum_absolute_prediction": Decimal("0.001")})
    ).evaluate(_dataset())
    assert all(
        abs(prediction.predicted_return) <= Decimal("0.001")
        for fold in clipped.fold_results
        for prediction in fold.predictions
    )


def test_versioned_config_and_cli_are_read_only(tmp_path: Path) -> None:
    loaded = RidgeModelConfig.load(CONFIG)
    assert loaded.fixed_hyperparameters is True
    assert loaded.production_order_routing is False

    checked = CLI.invoke(app, ["research-ridge-model-check"])
    assert checked.exit_code == 0
    assert "Hyperparameters: fixed" in checked.stdout
    assert "Eligible for operational promotion: NO" in checked.stdout
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: 1\n", encoding="utf-8")
    failed = CLI.invoke(app, ["research-ridge-model-check", "--config", str(invalid)])
    assert failed.exit_code == 1
    assert "research_ridge_config_invalid" in failed.stderr
