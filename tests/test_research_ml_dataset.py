from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_ml_dataset import (
    MLDatasetConfig,
    MLFeaturePoint,
    PurgedWalkForwardDatasetBuilder,
    ResearchMLDatasetError,
)

CONFIG = Path("config/research/ml_dataset_v1.yaml")
CLI = CliRunner()
INSTRUMENTS = ("NSE:AAA", "NSE:BBB")
FEATURES = ("momentum", "volatility")


def _config() -> MLDatasetConfig:
    return MLDatasetConfig(
        schema_version=1,
        dataset_id="ml_dataset_test",
        feature_names=FEATURES,
        label_horizon_observations=1,
        minimum_train_observations=5,
        validation_observations=2,
        purge_observations=2,
        embargo_observations=1,
        minimum_folds=2,
        signal_execution_lag_observations=1,
        split_method="expanding_purged_walk_forward",
        selection_window="validation",
        production_order_routing=False,
    )


def _points() -> tuple[MLFeaturePoint, ...]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    result: list[MLFeaturePoint] = []
    for index in range(20):
        for instrument_offset, instrument in enumerate(INSTRUMENTS):
            result.append(
                MLFeaturePoint(
                    start + timedelta(days=index),
                    instrument,
                    {
                        "momentum": Decimal(index + instrument_offset) / Decimal(100),
                        "volatility": Decimal("0.02") + Decimal(instrument_offset) / 100,
                    },
                    Decimal(100 + index + instrument_offset * 10),
                    True,
                )
            )
    return tuple(result)


def test_samples_use_signal_features_and_post_lag_forward_labels() -> None:
    result = PurgedWalkForwardDatasetBuilder(_config()).build(_points())
    sample = result.samples[0]

    assert sample.sample_id == "NSE:AAA|2020-01-01T00:00:00+00:00"
    assert sample.signal_at == datetime(2020, 1, 1, tzinfo=UTC)
    assert sample.label_start_at == datetime(2020, 1, 2, tzinfo=UTC)
    assert sample.label_end_at == datetime(2020, 1, 3, tzinfo=UTC)
    assert sample.forward_return == Decimal(102) / Decimal(101) - Decimal(1)
    assert sample.features == {"momentum": Decimal(0), "volatility": Decimal("0.02")}
    assert result.selection_window == "validation"
    assert result.production_order_routing is False


def test_folds_are_chronological_purged_embargoed_and_disjoint() -> None:
    result = PurgedWalkForwardDatasetBuilder(_config()).build(_points())
    samples = {sample.sample_id: sample for sample in result.samples}

    assert len(result.folds) == 4
    for fold in result.folds:
        assert set(fold.train_sample_ids).isdisjoint(fold.validation_sample_ids)
        assert max(samples[key].label_end_at for key in fold.train_sample_ids) < (
            fold.validation_signal_start
        )
        assert fold.train_signal_end < fold.validation_signal_start
        assert fold.purged_observations == 2
        assert fold.embargoed_observations_after == 1
    for previous, following in zip(result.folds, result.folds[1:], strict=False):
        assert following.validation_signal_start - previous.validation_signal_end == timedelta(
            days=2
        )


def test_future_changes_do_not_alter_earlier_samples_and_hash_is_deterministic() -> None:
    builder = PurgedWalkForwardDatasetBuilder(_config())
    original = builder.build(_points())
    assert builder.build(_points()).sha256 == original.sha256
    assert len(original.sha256) == 64

    changed = list(_points())
    final = changed[-1]
    changed[-1] = MLFeaturePoint(
        final.timestamp,
        final.instrument,
        {**final.features, "momentum": Decimal("999")},
        Decimal("999"),
        final.eligible,
    )
    revised = builder.build(tuple(changed))

    assert revised.samples[0] == original.samples[0]
    assert revised.sha256 != original.sha256


def test_point_in_time_eligibility_and_feature_schema_fail_closed() -> None:
    changed = list(_points())
    point = changed[6]
    changed[6] = MLFeaturePoint(
        point.timestamp, point.instrument, point.features, point.price, False
    )
    result = PurgedWalkForwardDatasetBuilder(_config()).build(tuple(changed))
    affected_id = f"{point.instrument}|{point.timestamp.isoformat()}"
    assert affected_id not in {sample.sample_id for sample in result.samples}

    changed = list(_points())
    point = changed[0]
    changed[0] = MLFeaturePoint(
        point.timestamp,
        point.instrument,
        {"momentum": Decimal(0)},
        point.price,
        point.eligible,
    )
    with pytest.raises(ResearchMLDatasetError) as error:
        PurgedWalkForwardDatasetBuilder(_config()).build(tuple(changed))
    assert error.value.code == "research_ml_features_invalid"


def test_point_panel_fold_and_result_guards() -> None:
    with pytest.raises(ResearchMLDatasetError) as error:
        MLFeaturePoint(datetime(2020, 1, 1), "NSE:AAA", {"momentum": Decimal(0)}, Decimal(1), True)
    assert error.value.code == "research_ml_time_naive"
    with pytest.raises(ResearchMLDatasetError) as error:
        MLFeaturePoint(datetime.now(UTC), "", {"momentum": Decimal(0)}, Decimal(1), True)
    assert error.value.code == "research_ml_instrument_invalid"
    with pytest.raises(ResearchMLDatasetError) as error:
        PurgedWalkForwardDatasetBuilder(_config()).build(tuple(reversed(_points())))
    assert error.value.code == "research_ml_order_invalid"
    with pytest.raises(ResearchMLDatasetError) as error:
        PurgedWalkForwardDatasetBuilder(_config().model_copy(update={"minimum_folds": 10})).build(
            _points()
        )
    assert error.value.code == "research_ml_folds_insufficient"

    result = PurgedWalkForwardDatasetBuilder(_config()).build(_points())
    with pytest.raises(TypeError):
        result.samples[0].features["momentum"] = Decimal(1)  # type: ignore[index]


def test_versioned_config_and_cli_are_read_only(tmp_path: Path) -> None:
    loaded = MLDatasetConfig.load(CONFIG)
    assert loaded.purge_observations == 6
    assert loaded.production_order_routing is False

    invalid_contract = _config().model_dump()
    invalid_contract["purge_observations"] = 1
    with pytest.raises(ValidationError, match="purge"):
        MLDatasetConfig.model_validate(invalid_contract)

    checked = CLI.invoke(app, ["research-ml-dataset-check"])
    assert checked.exit_code == 0
    assert "Split method: expanding_purged_walk_forward" in checked.stdout
    assert "Eligible for operational promotion: NO" in checked.stdout
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: 1\n", encoding="utf-8")
    failed = CLI.invoke(app, ["research-ml-dataset-check", "--config", str(invalid)])
    assert failed.exit_code == 1
    assert "research_ml_config_invalid" in failed.stderr
