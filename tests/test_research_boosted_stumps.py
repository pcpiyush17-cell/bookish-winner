from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_boosted_stumps import (
    BoostedStumpsConfig,
    PurgedWalkForwardBoostedStumps,
    ResearchBoostedStumpsError,
)
from personal_quant.research_ml_dataset import (
    MLDatasetConfig,
    MLDatasetResult,
    MLFeaturePoint,
    PurgedWalkForwardDatasetBuilder,
)
from personal_quant.research_ridge_model import (
    PurgedWalkForwardRidgeBaseline,
    RidgeModelConfig,
)

CONFIG = Path("config/research/boosted_stumps_v1.yaml")
CLI = CliRunner()
INSTRUMENTS = ("NSE:AAA", "NSE:BBB", "NSE:CCC")
FEATURES = ("signal", "constant")


def _dataset_config() -> MLDatasetConfig:
    return MLDatasetConfig(
        schema_version=1,
        dataset_id="boosted_dataset_test",
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


def _boosted_config() -> BoostedStumpsConfig:
    return BoostedStumpsConfig(
        schema_version=1,
        model_id="boosted_test",
        required_dataset_id="boosted_dataset_test",
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
        one_way_cost_bps=Decimal("10"),
        cost_multipliers=(Decimal("1.0"), Decimal("1.5"), Decimal("2.0")),
        selection_window="validation",
        fixed_hyperparameters=True,
        production_order_routing=False,
    )


def _ridge_config() -> RidgeModelConfig:
    return RidgeModelConfig(
        schema_version=1,
        model_id="ridge_test",
        required_dataset_id="boosted_dataset_test",
        feature_names=FEATURES,
        ridge_penalty=Decimal("0.1"),
        feature_std_floor=Decimal("0.000001"),
        maximum_absolute_prediction=Decimal("0.10"),
        minimum_train_samples=12,
        selection_fraction=Decimal("0.34"),
        require_positive_prediction=True,
        one_way_cost_bps=Decimal("10"),
        cost_multipliers=(Decimal("1.0"), Decimal("1.5"), Decimal("2.0")),
        selection_window="validation",
        fixed_hyperparameters=True,
        production_order_routing=False,
    )


def _points() -> tuple[MLFeaturePoint, ...]:
    count = 36
    signals = {
        instrument: tuple(
            Decimal(((index + offset * 2) % 7) - 3) / Decimal(10) for index in range(count)
        )
        for offset, instrument in enumerate(INSTRUMENTS)
    }
    targets = {
        instrument: tuple(value**2 - Decimal("0.04") for value in values)
        for instrument, values in signals.items()
    }
    prices: dict[str, list[Decimal]] = {
        instrument: [Decimal(100), Decimal(100)] for instrument in INSTRUMENTS
    }
    for instrument in INSTRUMENTS:
        for end_index in range(2, count):
            prices[instrument].append(
                prices[instrument][-1] * (Decimal(1) + targets[instrument][end_index - 2])
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
        for index in range(count)
        for instrument in INSTRUMENTS
    )


def _dataset() -> MLDatasetResult:
    return PurgedWalkForwardDatasetBuilder(_dataset_config()).build(_points())


def test_boosted_stumps_capture_bounded_nonlinearity_deterministically() -> None:
    evaluator = PurgedWalkForwardBoostedStumps(_boosted_config())
    result = evaluator.evaluate(_dataset())

    assert evaluator.evaluate(_dataset()) == result
    assert result.metrics.mean_validation_rmse < result.metrics.mean_baseline_rmse
    assert result.metrics.mean_information_coefficient > Decimal("0.50")
    assert all(0 < len(fold.stumps) <= _boosted_config().estimators for fold in result.fold_results)
    assert all(stump.feature_name == "signal" for stump in result.fold_results[0].stumps)
    assert result.eligible_for_operational_promotion is False
    assert result.production_order_routing is False


def test_later_feature_change_cannot_change_earlier_fold() -> None:
    evaluator = PurgedWalkForwardBoostedStumps(_boosted_config())
    original = evaluator.evaluate(_dataset())
    changed = list(_points())
    index = 24 * len(INSTRUMENTS)
    point = changed[index]
    changed[index] = MLFeaturePoint(
        point.timestamp,
        point.instrument,
        {**point.features, "signal": Decimal("0.15")},
        point.price,
        point.eligible,
    )
    revised_dataset = PurgedWalkForwardDatasetBuilder(_dataset_config()).build(tuple(changed))
    revised = evaluator.evaluate(revised_dataset)

    assert revised.fold_results[0] == original.fold_results[0]
    assert revised.dataset_sha256 != original.dataset_sha256


def test_same_dataset_comparison_holds_complexity_accountable_to_ridge() -> None:
    dataset = _dataset()
    boosted = PurgedWalkForwardBoostedStumps(_boosted_config()).evaluate(dataset)
    ridge = PurgedWalkForwardRidgeBaseline(_ridge_config()).evaluate(dataset)
    comparison = boosted.compare_to_ridge(ridge)

    assert comparison.rmse_delta < 0
    assert comparison.information_coefficient_delta > 0
    assert comparison.dataset_sha256 == dataset.sha256
    assert comparison.eligible_for_operational_promotion is False

    with pytest.raises(ResearchBoostedStumpsError) as error:
        boosted.compare_to_ridge(replace(ridge, model_id="wrong"))
    assert error.value.code == "research_boosted_ridge_mismatch"


def test_cost_stress_and_comparison_maps_are_immutable() -> None:
    dataset = _dataset()
    boosted = PurgedWalkForwardBoostedStumps(_boosted_config()).evaluate(dataset)
    ridge = PurgedWalkForwardRidgeBaseline(_ridge_config()).evaluate(dataset)
    returns = boosted.metrics.mean_selected_forward_return_pct_by_cost

    assert returns["1.0x"] > returns["1.5x"] > returns["2.0x"]
    with pytest.raises(TypeError):
        returns["1.0x"] = Decimal(0)  # type: ignore[index]
    comparison = boosted.compare_to_ridge(ridge)
    with pytest.raises(TypeError):
        comparison.selected_return_delta_pct_by_cost["1.0x"] = Decimal(0)  # type: ignore[index]


def test_dataset_fold_training_and_prediction_guards_fail_closed() -> None:
    with pytest.raises(ResearchBoostedStumpsError) as error:
        PurgedWalkForwardBoostedStumps(
            _boosted_config().model_copy(update={"required_dataset_id": "wrong"})
        ).evaluate(_dataset())
    assert error.value.code == "research_boosted_dataset_mismatch"

    with pytest.raises(ResearchBoostedStumpsError) as error:
        PurgedWalkForwardBoostedStumps(
            _boosted_config().model_copy(update={"minimum_train_samples": 1000})
        ).evaluate(_dataset())
    assert error.value.code == "research_boosted_train_short"

    dataset = _dataset()
    bad_fold = replace(
        dataset.folds[0], validation_sample_ids=(*dataset.folds[0].validation_sample_ids, "bad")
    )
    with pytest.raises(ResearchBoostedStumpsError) as error:
        PurgedWalkForwardBoostedStumps(_boosted_config()).evaluate(
            replace(dataset, folds=(bad_fold, *dataset.folds[1:]))
        )
    assert error.value.code == "research_boosted_fold_invalid"

    clipped = PurgedWalkForwardBoostedStumps(
        _boosted_config().model_copy(update={"maximum_absolute_prediction": Decimal("0.001")})
    ).evaluate(_dataset())
    assert all(
        abs(prediction.predicted_return) <= Decimal("0.001")
        for fold in clipped.fold_results
        for prediction in fold.predictions
    )


def test_versioned_config_and_cli_are_read_only(tmp_path: Path) -> None:
    loaded = BoostedStumpsConfig.load(CONFIG)
    assert loaded.fixed_hyperparameters is True
    assert loaded.production_order_routing is False

    checked = CLI.invoke(app, ["research-boosted-stumps-check"])
    assert checked.exit_code == 0
    assert "Hyperparameters: fixed; tree depth: 1" in checked.stdout
    assert "Eligible for operational promotion: NO" in checked.stdout
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: 1\n", encoding="utf-8")
    failed = CLI.invoke(app, ["research-boosted-stumps-check", "--config", str(invalid)])
    assert failed.exit_code == 1
    assert "research_boosted_config_invalid" in failed.stderr
