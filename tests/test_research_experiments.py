import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_experiments import (
    ExperimentRegistry,
    ExperimentResult,
    ReproducibilityContext,
    ResearchExperimentError,
    SplitMetrics,
    build_walk_forward_folds,
    classify_timestamp,
)
from personal_quant.research_governance import ExperimentManifest

CLI = CliRunner()
COMMIT = "c" * 40


def _write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return hashlib.sha256(content.encode()).hexdigest()


def _manifest(project: Path) -> tuple[Path, ExperimentManifest]:
    universe = project / "research/data/universes/u/manifest.json"
    dataset = project / "research/data/manifests/d.json"
    config = project / "config/strategies/research.yaml"
    lock = project / "uv.lock"
    universe_hash = _write(universe, '{"universe":1}\n')
    dataset_hash = _write(dataset, '{"dataset":1}\n')
    config_hash = _write(config, "strategy: benchmark\n")
    lock_hash = _write(lock, "lock\n")
    raw = {
        "schema_version": 1,
        "experiment_id": "QR-0002-registry-test",
        "created_at": datetime.fromisoformat("2026-08-16T12:00:00+05:30"),
        "hypothesis": "A deterministic registry rejects leaked or mutable experimental inputs.",
        "strategy_family": "benchmark",
        "universe_manifest": {
            "manifest_path": universe.relative_to(project).as_posix(),
            "sha256": universe_hash,
            "as_of": datetime.fromisoformat("2018-01-01T00:00:00+05:30"),
        },
        "datasets": [
            {
                "manifest_path": dataset.relative_to(project).as_posix(),
                "sha256": dataset_hash,
                "as_of": datetime.fromisoformat("2018-01-01T00:00:00+05:30"),
            }
        ],
        "config_path": config.relative_to(project).as_posix(),
        "code_commit": COMMIT,
        "uv_lock_sha256": lock_hash,
        "config_sha256": config_hash,
        "train": {
            "start": datetime.fromisoformat("2015-01-01T00:00:00+05:30"),
            "end": datetime.fromisoformat("2018-01-01T00:00:00+05:30"),
        },
        "validation": {
            "start": datetime.fromisoformat("2018-01-01T00:00:00+05:30"),
            "end": datetime.fromisoformat("2019-01-01T00:00:00+05:30"),
        },
        "holdout": {
            "start": datetime.fromisoformat("2019-01-01T00:00:00+05:30"),
            "end": datetime.fromisoformat("2020-01-01T00:00:00+05:30"),
        },
        "cost_multipliers": ["1.0", "1.5", "2.0"],
        "status": "DRAFT",
        "wp14_isolated": True,
        "production_order_routing": False,
    }
    path = project / "research/experiments/intent.yaml"
    _write(path, yaml.safe_dump(raw, sort_keys=False))
    return path, ExperimentManifest.load(path)


def _metrics(*, days: int = 300) -> SplitMetrics:
    return SplitMetrics(
        observations=1000,
        trading_days=days,
        trades=20,
        net_return_pct_by_cost={
            "1.0x": Decimal("3.0"),
            "1.5x": Decimal("2.5"),
            "2.0x": Decimal("2.0"),
        },
        maximum_drawdown_pct=Decimal("4.0"),
        turnover=Decimal("100000"),
        sharpe=Decimal("0.7"),
        annualized_metrics_headlined=days >= 252,
    )


def _result(checksum: str) -> ExperimentResult:
    return ExperimentResult(
        schema_version=1,
        experiment_id="QR-0002-registry-test",
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
        experiment_manifest_sha256=checksum,
        evaluated_code_commit=COMMIT,
        selection_window="validation",
        holdout_evaluations=1,
        train=_metrics(),
        validation=_metrics(),
        holdout=_metrics(),
        decision="CANDIDATE",
        warnings=(),
        wp14_isolated=True,
        production_order_routing=False,
    )


def test_walk_forward_has_purge_embargo_and_anchored_training() -> None:
    start = datetime(2015, 1, 1, tzinfo=UTC)
    validation_start = datetime(2018, 1, 1, tzinfo=UTC)
    folds = build_walk_forward_folds(
        train_start=start,
        validation_start=validation_start,
        validation_end=datetime(2019, 1, 1, tzinfo=UTC),
        folds=3,
        purge=timedelta(days=5),
        embargo=timedelta(days=2),
    )

    assert len(folds) == 3
    assert all(fold.train.start == start for fold in folds)
    assert all(fold.train.end == fold.validation.start - timedelta(days=5) for fold in folds)
    assert folds[1].validation.start == folds[0].embargo_until
    assert folds[-1].validation.end == datetime(2019, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"folds": 0}, "research_walk_forward_parameters_invalid"),
        (
            {"validation_end": datetime(2017, 1, 1, tzinfo=UTC)},
            "research_walk_forward_window_invalid",
        ),
        ({"folds": 3, "embargo": timedelta(days=200)}, "research_walk_forward_space_short"),
        ({"purge": timedelta(days=1200)}, "research_walk_forward_train_short"),
    ],
)
def test_walk_forward_rejects_unsafe_parameters(kwargs: dict[str, object], code: str) -> None:
    parameters: dict[str, object] = {
        "train_start": datetime(2015, 1, 1, tzinfo=UTC),
        "validation_start": datetime(2018, 1, 1, tzinfo=UTC),
        "validation_end": datetime(2019, 1, 1, tzinfo=UTC),
        "folds": 2,
        "purge": timedelta(days=1),
        "embargo": timedelta(days=1),
    }
    parameters.update(kwargs)
    with pytest.raises(ResearchExperimentError) as error:
        build_walk_forward_folds(**parameters)  # type: ignore[arg-type]
    assert error.value.code == code


def test_naive_research_timestamp_is_rejected(tmp_path: Path) -> None:
    _, manifest = _manifest(tmp_path)
    with pytest.raises(ResearchExperimentError) as error:
        classify_timestamp(manifest, datetime(2018, 1, 1))
    assert error.value.code == "research_experiment_time_naive"


def test_temporal_classification_is_half_open_and_holdout_is_separate(tmp_path: Path) -> None:
    _, manifest = _manifest(tmp_path)

    assert classify_timestamp(manifest, manifest.train.start) == "train"
    assert classify_timestamp(manifest, manifest.train.end) == "validation"
    assert classify_timestamp(manifest, manifest.validation.end) == "holdout"
    assert classify_timestamp(manifest, manifest.holdout.end) is None


def test_short_samples_and_missing_cost_stresses_are_rejected() -> None:
    with pytest.raises(ValidationError, match="annualized metrics"):
        SplitMetrics.model_validate(
            {
                **_metrics(days=100).model_dump(),
                "annualized_metrics_headlined": True,
            }
        )
    with pytest.raises(ValidationError, match="must report"):
        SplitMetrics(
            observations=10,
            trading_days=10,
            trades=0,
            net_return_pct_by_cost={"1.0x": Decimal(0)},
            maximum_drawdown_pct=Decimal(0),
            turnover=Decimal(0),
        )


def test_registry_verifies_inputs_stores_result_and_claims_holdout_once(tmp_path: Path) -> None:
    manifest_path, _ = _manifest(tmp_path)
    registry = ExperimentRegistry(
        tmp_path / "research/results/experiments", tmp_path / "state/research/holdout"
    )
    context = ReproducibilityContext(COMMIT, True, tmp_path / "uv.lock")
    registered = registry.register(manifest_path, project_root=tmp_path, context=context)

    assert registry.register(manifest_path, project_root=tmp_path, context=context) == registered
    result_path = registry.save_result(_result(registered.manifest_sha256), project_root=tmp_path)
    result_manifest = result_path.parent / "result-manifest.json"
    assert registry.verify_result(result_manifest, project_root=tmp_path).decision == "CANDIDATE"
    with pytest.raises(ResearchExperimentError) as error:
        registry.save_result(_result(registered.manifest_sha256), project_root=tmp_path)
    assert error.value.code == "research_result_exists"
    assert (tmp_path / "state/research/holdout/QR-0002-registry-test.json").exists()


def test_registry_rejects_dirty_git_bad_input_roots_and_tampering(tmp_path: Path) -> None:
    manifest_path, _ = _manifest(tmp_path)
    registry = ExperimentRegistry(
        tmp_path / "research/results/experiments", tmp_path / "state/research/holdout"
    )
    with pytest.raises(ResearchExperimentError) as error:
        registry.register(
            manifest_path,
            project_root=tmp_path,
            context=ReproducibilityContext(COMMIT, False, tmp_path / "uv.lock"),
        )
    assert error.value.code == "research_experiment_git_mismatch"

    (tmp_path / "uv.lock").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ResearchExperimentError) as error:
        registry.register(
            manifest_path,
            project_root=tmp_path,
            context=ReproducibilityContext(COMMIT, True, tmp_path / "uv.lock"),
        )
    assert error.value.code == "research_experiment_input_mismatch"

    prohibited = ExperimentRegistry(
        tmp_path / "state/replay/experiments", tmp_path / "state/research/holdout"
    )
    with pytest.raises(ResearchExperimentError) as error:
        prohibited.register(
            manifest_path,
            project_root=tmp_path,
            context=ReproducibilityContext(COMMIT, True, tmp_path / "uv.lock"),
        )
    assert error.value.code == "research_registry_path_prohibited"


def test_registry_rejects_conflicts_reused_holdout_and_result_tampering(tmp_path: Path) -> None:
    manifest_path, _ = _manifest(tmp_path)
    registry = ExperimentRegistry(
        tmp_path / "research/results/experiments", tmp_path / "state/research/holdout"
    )
    context = ReproducibilityContext(COMMIT, True, tmp_path / "uv.lock")
    registered = registry.register(manifest_path, project_root=tmp_path, context=context)
    registered.manifest_path.write_text("conflict\n", encoding="utf-8")
    with pytest.raises(ResearchExperimentError) as error:
        registry.register(manifest_path, project_root=tmp_path, context=context)
    assert error.value.code == "research_experiment_conflict"

    # Restore a fresh project so the remaining assertions exercise independent guards.
    second = tmp_path / "second"
    second_manifest, _ = _manifest(second)
    second_registry = ExperimentRegistry(
        second / "research/results/experiments", second / "state/research/holdout"
    )
    second_registered = second_registry.register(
        second_manifest,
        project_root=second,
        context=ReproducibilityContext(COMMIT, True, second / "uv.lock"),
    )
    result_path = second_registry.save_result(
        _result(second_registered.manifest_sha256), project_root=second
    )
    result_manifest = result_path.parent / "result-manifest.json"
    result_path.unlink()
    with pytest.raises(ResearchExperimentError) as error:
        second_registry.save_result(_result(second_registered.manifest_sha256), project_root=second)
    assert error.value.code == "research_holdout_already_used"

    result_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ResearchExperimentError) as error:
        second_registry.verify_result(result_manifest, project_root=second)
    assert error.value.code == "research_result_checksum"

    raw = yaml.safe_load(result_manifest.read_text(encoding="utf-8"))
    raw["result_path"] = "state/replay/trading.sqlite"
    result_manifest.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ResearchExperimentError) as error:
        second_registry.verify_result(result_manifest, project_root=second)
    assert error.value.code == "research_result_path_escape"


def test_unregistered_and_invalid_results_are_rejected(tmp_path: Path) -> None:
    registry = ExperimentRegistry(
        tmp_path / "research/results/experiments", tmp_path / "state/research/holdout"
    )
    with pytest.raises(ResearchExperimentError) as error:
        registry.save_result(_result("a" * 64), project_root=tmp_path)
    assert error.value.code == "research_result_manifest_mismatch"

    invalid = tmp_path / "research/results/experiments/invalid.json"
    _write(invalid, "not json\n")
    with pytest.raises(ResearchExperimentError) as error:
        ExperimentResult.load(invalid)
    assert error.value.code == "research_result_invalid"


def test_result_commit_and_manifest_identity_must_match(tmp_path: Path) -> None:
    manifest_path, _ = _manifest(tmp_path)
    registry = ExperimentRegistry(
        tmp_path / "research/results/experiments", tmp_path / "state/research/holdout"
    )
    registered = registry.register(
        manifest_path,
        project_root=tmp_path,
        context=ReproducibilityContext(COMMIT, True, tmp_path / "uv.lock"),
    )
    wrong_commit = _result(registered.manifest_sha256).model_copy(
        update={"evaluated_code_commit": "d" * 40}
    )
    with pytest.raises(ResearchExperimentError) as error:
        registry.save_result(wrong_commit, project_root=tmp_path)
    assert error.value.code == "research_result_identity_mismatch"

    result_path = registry.save_result(_result(registered.manifest_sha256), project_root=tmp_path)
    result_manifest = result_path.parent / "result-manifest.json"
    raw = json.loads(result_manifest.read_text(encoding="utf-8"))
    raw["experiment_id"] = "QR-9999-wrong"
    result_manifest.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ResearchExperimentError) as error:
        registry.verify_result(result_manifest, project_root=tmp_path)
    assert error.value.code == "research_result_manifest_identity"


def test_result_cli_verifies_checksum_without_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, _ = _manifest(tmp_path)
    registry = ExperimentRegistry(
        tmp_path / "research/results/experiments", tmp_path / "state/research/holdout"
    )
    registered = registry.register(
        manifest_path,
        project_root=tmp_path,
        context=ReproducibilityContext(COMMIT, True, tmp_path / "uv.lock"),
    )
    result = registry.save_result(_result(registered.manifest_sha256), project_root=tmp_path)
    monkeypatch.chdir(tmp_path)
    checked = CLI.invoke(
        app,
        ["research-result-check", "--manifest", str(result.parent / "result-manifest.json")],
    )
    assert checked.exit_code == 0, checked.output
    assert "Eligible for operational promotion: NO" in checked.stdout
