from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_candidate_freeze import (
    CandidateFreezeConfig,
    CandidateFreezeGate,
    CandidateProvenance,
    FrozenArtifact,
)
from personal_quant.research_ml_dataset import MLDatasetConfig
from personal_quant.research_model_evaluation import ModelEvaluationWorkflow
from personal_quant.research_real_validation import (
    AdjustedDailyBar,
    RealHistoricalValidationRunner,
    RealValidationConfig,
    ResearchRealValidationError,
    assemble_real_feature_points,
)
from personal_quant.research_universe import (
    PointInTimeUniverse,
    UniverseManifest,
    UniverseMember,
    UniverseObservation,
)
from tests.test_research_model_evaluation import (
    _boosted_config,
    _evaluation_config,
    _ridge_config,
    _stability_config,
)

CLI = CliRunner()
FEATURES = (
    "momentum_20d",
    "reversal_5d",
    "volatility_20d",
    "dollar_volume_rank",
)
INSTRUMENTS = ("NSE:AAA", "NSE:BBB", "NSE:CCC")


def _config(**updates: object) -> RealValidationConfig:
    values: dict[str, object] = {
        "schema_version": 1,
        "runner_id": "real_validation_test",
        "momentum_lookback_observations": 2,
        "reversal_lookback_observations": 1,
        "volatility_lookback_observations": 2,
        "minimum_instruments_per_observation": 2,
        "required_price_adjustment": "corporate_action_adjusted",
        "required_interval": "day",
        "universe_membership_semantics": "exact_snapshot",
        "selection_window": "validation",
        "final_holdout_access": False,
        "production_order_routing": False,
    }
    values.update(updates)
    return RealValidationConfig.model_validate(values)


def _universe(days: int = 36, members: tuple[str, ...] = INSTRUMENTS) -> PointInTimeUniverse:
    start = date(2020, 1, 1)
    observations = tuple(
        UniverseObservation(
            start + timedelta(days=index),
            f"snapshots/{index}.json",
            f"{(index % 9) + 1}" * 64,
            tuple(UniverseMember(key, key.split(":")[1], key, None) for key in members),
            members if index == 0 else (),
            (),
        )
        for index in range(days)
    )
    return PointInTimeUniverse(
        UniverseManifest(
            1,
            "universe_test",
            "policy_test",
            "exact_snapshot",
            datetime(2020, 2, 10, tzinfo=UTC),
            observations[0].observed_on,
            observations[-1].observed_on,
            len(observations),
            "research/universe.jsonl",
            "a" * 64,
        ),
        observations,
    )


def _bars(days: int = 36) -> tuple[AdjustedDailyBar, ...]:
    start = datetime(2020, 1, 1, 15, 30, tzinfo=UTC)
    return tuple(
        AdjustedDailyBar(
            instrument,
            start + timedelta(days=index),
            start + timedelta(days=index, hours=1),
            Decimal(100 + index + offset * 3) + Decimal((index + offset) % 3) / Decimal(10),
            1_000 + index * 10 + offset,
            f"data/{instrument}.parquet",
            f"{offset + 1}" * 64,
        )
        for index in range(days)
        for offset, instrument in enumerate(INSTRUMENTS)
    )


def _runner() -> RealHistoricalValidationRunner:
    dataset = MLDatasetConfig(
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
    ridge = _ridge_config().model_copy(update={"feature_names": FEATURES})
    boosted = _boosted_config().model_copy(update={"feature_names": FEATURES})
    workflow = ModelEvaluationWorkflow(_evaluation_config(), ridge, boosted, _stability_config())
    freeze_config = CandidateFreezeConfig(
        schema_version=1,
        freeze_id="freeze_test",
        required_workflow_id="evaluation_test",
        required_stability_gate_id="gate_test",
        required_stability_decision="BOOSTED_VALIDATION_CANDIDATE",
        required_config_artifacts=(
            "ml_dataset",
            "ridge",
            "boosted",
            "stability",
            "evaluation",
        ),
        selection_window="validation",
        final_holdout_access=False,
        final_holdout_consumed=False,
        eligible_for_operational_promotion=False,
        production_order_routing=False,
    )
    return RealHistoricalValidationRunner(
        _config(), dataset, workflow, CandidateFreezeGate(freeze_config, ridge, boosted)
    )


def _provenance() -> CandidateProvenance:
    names = ("ml_dataset", "ridge", "boosted", "stability", "evaluation")
    return CandidateProvenance(
        git_commit_sha="a" * 40,
        uv_lock_sha256="b" * 64,
        config_artifacts=tuple(
            FrozenArtifact(name=name, path=f"config/{name}.yaml", sha256=str(index) * 64)
            for index, name in enumerate(names, start=1)
        ),
    )


def test_assembler_calculates_fixed_features_and_preserves_order() -> None:
    points = assemble_real_feature_points(_bars(5), _universe(5), _config())

    assert len(points) == 9
    assert tuple(points) == tuple(
        sorted(points, key=lambda item: (item.timestamp, item.instrument))
    )
    assert tuple(points[0].features) == FEATURES
    assert points[0].timestamp.hour == 16
    assert points[0].features["momentum_20d"] > 0
    assert points[0].features["reversal_5d"] < 0
    assert points[0].features["volatility_20d"] >= 0
    assert Decimal(0) <= points[0].features["dollar_volume_rank"] <= Decimal(1)


def test_runner_executes_controlled_real_validation() -> None:
    result = _runner().run(_bars(), _universe(), _provenance())

    assert result.status in ("HOLDOUT_READY", "VALIDATION_REJECTED")
    assert result.feature_points == 102
    assert result.universe_id == "universe_test"
    assert result.universe_sha256 == "a" * 64
    assert len(result.source_artifacts) == 3
    assert result.final_holdout_access is False
    assert result.final_holdout_consumed is False
    assert result.production_order_routing is False
    assert (result.dossier is not None) == (result.status == "HOLDOUT_READY")


def test_assembler_rejects_missing_exact_universe_date() -> None:
    universe = _universe(5)
    missing = PointInTimeUniverse(universe.manifest, universe.observations[:-1])

    with pytest.raises(ResearchRealValidationError) as caught:
        assemble_real_feature_points(_bars(5), missing, _config())

    assert caught.value.code == "research_real_validation_universe_gap"


def test_assembler_rejects_unadjusted_input() -> None:
    bar = _bars(1)[0]
    unsafe = AdjustedDailyBar(
        bar.instrument,
        bar.timestamp,
        bar.available_at,
        bar.adjusted_close,
        bar.volume,
        bar.source_manifest,
        bar.source_sha256,
        price_adjustment=cast(Literal["corporate_action_adjusted"], "unadjusted"),
    )

    with pytest.raises(ResearchRealValidationError) as caught:
        assemble_real_feature_points((unsafe,), _universe(1), _config())

    assert caught.value.code == "research_real_validation_adjustment_required"


def test_assembler_rejects_insufficient_breadth() -> None:
    with pytest.raises(ResearchRealValidationError) as caught:
        assemble_real_feature_points(_bars(5), _universe(5, ("NSE:AAA",)), _config())

    assert caught.value.code == "research_real_validation_breadth_insufficient"


def test_cli_validates_real_data_contract() -> None:
    result = CLI.invoke(app, ["research-real-validation-check"])

    assert result.exit_code == 0
    assert "Research real validation valid: qr_real_validation_v1" in result.stdout
    assert "Final holdout access: disabled; consumed: NO" in result.stdout


def test_config_load_wraps_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: [", encoding="utf-8")

    with pytest.raises(ResearchRealValidationError) as caught:
        RealValidationConfig.load(path)

    assert caught.value.code == "research_real_validation_config_invalid"
