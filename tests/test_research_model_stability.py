from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from personal_quant.research_boosted_stumps import (
    BoostedFoldResult,
    BoostedPrediction,
    BoostedStumpsMetrics,
    BoostedStumpsResult,
)
from personal_quant.research_model_stability import (
    ModelStabilityConfig,
    ModelStabilityGate,
    ResearchModelStabilityError,
)
from personal_quant.research_ridge_model import (
    RidgeFoldResult,
    RidgeModelMetrics,
    RidgeModelResult,
    RidgePrediction,
)

COST_CASES = ("1.0x", "1.5x", "2.0x")


def _config(**overrides: object) -> ModelStabilityConfig:
    values: dict[str, object] = {
        "schema_version": 1,
        "gate_id": "stability_test",
        "required_dataset_id": "dataset_test",
        "required_ridge_model_id": "ridge_test",
        "required_boosted_model_id": "boosted_test",
        "minimum_folds": 2,
        "minimum_rmse_improvement_fraction": Decimal("0.10"),
        "minimum_positive_ic_fold_fraction": Decimal("0.50"),
        "maximum_degraded_rmse_fold_fraction": Decimal("0.50"),
        "minimum_mean_information_coefficient": Decimal("0.05"),
        "minimum_selected_return_delta_pct": Decimal("0.10"),
        "required_cost_cases": COST_CASES,
        "selection_window": "validation",
        "holdout_access": False,
        "production_order_routing": False,
    }
    values.update(overrides)
    return ModelStabilityConfig.model_validate(values)


def _ridge() -> RidgeModelResult:
    folds = tuple(
        RidgeFoldResult(
            fold_number=number,
            intercept=Decimal(0),
            standardized_coefficients={},
            feature_means={},
            feature_stds={},
            predictions=(
                RidgePrediction(f"sample-{number}-a", Decimal("0.01"), Decimal("0.02")),
                RidgePrediction(f"sample-{number}-b", Decimal("0.02"), Decimal("0.01")),
            ),
            validation_rmse=rmse,
            baseline_rmse=Decimal("0.05"),
            information_coefficient=ic,
            directional_accuracy=Decimal("0.50"),
        )
        for number, rmse, ic in (
            (1, Decimal("0.03"), Decimal("0.10")),
            (2, Decimal("0.04"), Decimal("0.05")),
        )
    )
    return RidgeModelResult(
        model_id="ridge_test",
        dataset_id="dataset_test",
        dataset_sha256="a" * 64,
        selection_window="validation",
        fold_results=folds,
        metrics=RidgeModelMetrics(
            folds=2,
            positive_information_coefficient_folds=2,
            mean_validation_rmse=Decimal("0.035"),
            mean_baseline_rmse=Decimal("0.05"),
            mean_information_coefficient=Decimal("0.075"),
            mean_selected_forward_return_pct_by_cost={
                "1.0x": Decimal("1.0"),
                "1.5x": Decimal("0.9"),
                "2.0x": Decimal("0.8"),
            },
            mean_equal_weight_forward_return_pct_by_cost=dict.fromkeys(COST_CASES, Decimal(0)),
            excess_return_pct_vs_equal_weight_by_cost={
                "1.0x": Decimal("1.0"),
                "1.5x": Decimal("0.9"),
                "2.0x": Decimal("0.8"),
            },
        ),
    )


def _boosted() -> BoostedStumpsResult:
    folds = tuple(
        BoostedFoldResult(
            fold_number=number,
            base_prediction=Decimal(0),
            stumps=(),
            predictions=(
                BoostedPrediction(f"sample-{number}-a", Decimal("0.01"), Decimal("0.02")),
                BoostedPrediction(f"sample-{number}-b", Decimal("0.02"), Decimal("0.01")),
            ),
            validation_rmse=rmse,
            baseline_rmse=Decimal("0.05"),
            information_coefficient=ic,
            directional_accuracy=Decimal("0.60"),
        )
        for number, rmse, ic in (
            (1, Decimal("0.02"), Decimal("0.20")),
            (2, Decimal("0.03"), Decimal("0.15")),
        )
    )
    return BoostedStumpsResult(
        model_id="boosted_test",
        dataset_id="dataset_test",
        dataset_sha256="a" * 64,
        selection_window="validation",
        fold_results=folds,
        metrics=BoostedStumpsMetrics(
            folds=2,
            positive_information_coefficient_folds=2,
            mean_estimators_fitted=Decimal(5),
            mean_validation_rmse=Decimal("0.025"),
            mean_baseline_rmse=Decimal("0.05"),
            mean_information_coefficient=Decimal("0.175"),
            mean_selected_forward_return_pct_by_cost={
                "1.0x": Decimal("1.5"),
                "1.5x": Decimal("1.4"),
                "2.0x": Decimal("1.3"),
            },
            mean_equal_weight_forward_return_pct_by_cost=dict.fromkeys(COST_CASES, Decimal(0)),
            excess_return_pct_vs_equal_weight_by_cost={
                "1.0x": Decimal("1.5"),
                "1.5x": Decimal("1.4"),
                "2.0x": Decimal("1.3"),
            },
        ),
        required_ridge_model_id="ridge_test",
    )


def test_gate_selects_only_a_validation_candidate_deterministically() -> None:
    gate = ModelStabilityGate(_config())

    first = gate.evaluate(_ridge(), _boosted())
    second = gate.evaluate(_ridge(), _boosted())

    assert first.decision == "BOOSTED_VALIDATION_CANDIDATE"
    assert first.failure_reasons == ()
    assert first.sha256 == second.sha256
    assert len(first.sha256) == 64
    assert first.holdout_access is False
    assert first.eligible_for_operational_promotion is False
    assert first.production_order_routing is False
    with pytest.raises(TypeError):
        first.metrics.selected_return_delta_pct_by_cost["1.0x"] = Decimal(0)  # type: ignore[index]


def test_gate_retains_ridge_and_reports_every_failed_threshold() -> None:
    boosted = _boosted()
    failing_folds = tuple(
        replace(fold, validation_rmse=Decimal("0.05"), information_coefficient=Decimal("-0.01"))
        for fold in boosted.fold_results
    )
    failing_metrics = replace(
        boosted.metrics,
        mean_validation_rmse=Decimal("0.05"),
        mean_information_coefficient=Decimal("-0.01"),
        mean_selected_forward_return_pct_by_cost=dict.fromkeys(COST_CASES, Decimal("0.5")),
    )

    result = ModelStabilityGate(_config()).evaluate(
        _ridge(), replace(boosted, fold_results=failing_folds, metrics=failing_metrics)
    )

    assert result.decision == "RETAIN_RIDGE"
    assert set(result.failure_reasons) == {
        "rmse_improvement_insufficient",
        "positive_ic_fold_fraction_insufficient",
        "degraded_rmse_fold_fraction_excessive",
        "mean_information_coefficient_insufficient",
        "cost_case_improvement_insufficient",
    }


def test_gate_rejects_nonidentical_validation_samples() -> None:
    boosted = _boosted()
    first_fold = boosted.fold_results[0]
    changed_predictions = (
        replace(first_fold.predictions[0], sample_id="different-sample"),
        first_fold.predictions[1],
    )
    changed = replace(
        boosted,
        fold_results=(
            replace(first_fold, predictions=changed_predictions),
            boosted.fold_results[1],
        ),
    )

    result = ModelStabilityGate(_config()).evaluate(_ridge(), changed)

    assert result.decision == "RETAIN_RIDGE"
    assert "validation_samples_mismatch" in result.failure_reasons


@pytest.mark.parametrize(
    ("ridge", "boosted", "code"),
    [
        (
            _ridge(),
            replace(_boosted(), dataset_sha256="b" * 64),
            "research_stability_dataset_mismatch",
        ),
        (_ridge(), replace(_boosted(), model_id="wrong"), "research_stability_model_mismatch"),
        (
            _ridge(),
            replace(_boosted(), fold_results=(_boosted().fold_results[0],)),
            "research_stability_folds_invalid",
        ),
        (
            _ridge(),
            replace(
                _boosted(),
                metrics=replace(
                    _boosted().metrics,
                    mean_selected_forward_return_pct_by_cost={"1.0x": Decimal(1)},
                ),
            ),
            "research_stability_cost_cases_invalid",
        ),
    ],
)
def test_gate_rejects_incompatible_inputs(
    ridge: RidgeModelResult, boosted: BoostedStumpsResult, code: str
) -> None:
    with pytest.raises(ResearchModelStabilityError) as caught:
        ModelStabilityGate(_config()).evaluate(ridge, boosted)

    assert caught.value.code == code


def test_config_load_rejects_an_incomplete_cost_contract(tmp_path: Path) -> None:
    path = tmp_path / "gate.yaml"
    path.write_text("schema_version: 1\nrequired_cost_cases: [1.0x]\n", encoding="utf-8")

    with pytest.raises(ResearchModelStabilityError) as caught:
        ModelStabilityConfig.load(path)

    assert caught.value.code == "research_stability_config_invalid"
