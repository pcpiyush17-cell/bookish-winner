import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.research_data_readiness import (
    DataReadinessConfig,
    ResearchDataPackageImporter,
    ResearchDataReadinessError,
    write_readiness_receipt,
)

CLI = CliRunner()


def _config(**updates: object) -> DataReadinessConfig:
    values: dict[str, object] = {
        "schema_version": 1,
        "policy_id": "readiness_test",
        "minimum_calendar_span_days": 3,
        "minimum_instruments": 2,
        "minimum_bars_per_instrument": 4,
        "minimum_eligible_instruments_per_date": 2,
        "required_price_adjustment": "corporate_action_adjusted",
        "required_membership_semantics": "exact_snapshot",
        "require_delisted_securities": True,
        "require_local_research_license": True,
        "selection_window": "validation",
        "final_holdout_access": False,
        "production_order_routing": False,
    }
    values.update(updates)
    return DataReadinessConfig.model_validate(values)


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_package(root: Path, *, universe_days: int = 4) -> tuple[Path, Path, Path]:
    prices = root / "vendor" / "prices.csv"
    universe = root / "vendor" / "universe.csv"
    prices.parent.mkdir(parents=True)
    start = datetime(2020, 1, 1, 15, 30, tzinfo=UTC)
    price_lines = ["instrument,timestamp,available_at,adjusted_close,volume"]
    universe_lines = ["observed_on,instrument,trading_symbol,name,isin"]
    for index in range(4):
        timestamp = start + timedelta(days=index)
        for offset, instrument in enumerate(("NSE:AAA", "NSE:BBB")):
            price_lines.append(
                f"{instrument},{timestamp.isoformat()},"
                f"{(timestamp + timedelta(hours=1)).isoformat()},"
                f"{100 + index + offset},{1000 + index + offset}"
            )
        if index < universe_days:
            observed = (start + timedelta(days=index)).date().isoformat()
            universe_lines.extend(
                (
                    f"{observed},NSE:AAA,AAA,Alpha,INE000A01001",
                    f"{observed},NSE:BBB,BBB,Beta,INE000B01001",
                )
            )
    prices.write_text("\n".join(price_lines) + "\n", encoding="utf-8")
    universe.write_text("\n".join(universe_lines) + "\n", encoding="utf-8")
    manifest = root / "vendor" / "package.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "package_id": "package_test",
                "provider": "licensed_test_vendor",
                "license_id": "license-123",
                "license_allows_local_research": True,
                "acquired_at": "2026-08-18T12:00:00+00:00",
                "price_adjustment": "corporate_action_adjusted",
                "membership_semantics": "exact_snapshot",
                "includes_delisted_securities": True,
                "prices": {
                    "path": "vendor/prices.csv",
                    "sha256": _checksum(prices),
                    "format": "csv",
                },
                "universe": {
                    "path": "vendor/universe.csv",
                    "sha256": _checksum(universe),
                    "format": "csv",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return manifest, prices, universe


def test_import_validates_package_and_is_deterministic(tmp_path: Path) -> None:
    manifest, _, _ = _write_package(tmp_path)
    importer = ResearchDataPackageImporter(_config())

    first = importer.load(manifest, project_root=tmp_path)
    second = importer.load(manifest, project_root=tmp_path)

    assert first.status == "READY_FOR_QR14"
    assert first.package_sha256 == second.package_sha256
    assert len(first.package_sha256) == 64
    assert first.metrics.calendar_span_days == 3
    assert first.metrics.instruments == 2
    assert first.metrics.bars == 8
    assert first.universe.manifest.membership_semantics == "exact_snapshot"
    assert first.final_holdout_access is False
    assert first.final_holdout_consumed is False
    assert first.production_order_routing is False


def test_receipt_is_idempotent_and_conflict_safe(tmp_path: Path) -> None:
    manifest, _, _ = _write_package(tmp_path)
    package = ResearchDataPackageImporter(_config()).load(manifest, project_root=tmp_path)
    output = tmp_path / "receipts"

    path = write_readiness_receipt(package, output)

    assert write_readiness_receipt(package, output) == path
    path.write_text("conflict", encoding="utf-8")
    with pytest.raises(ResearchDataReadinessError) as caught:
        write_readiness_receipt(package, output)
    assert caught.value.code == "research_data_receipt_conflict"


def test_import_rejects_checksum_tampering(tmp_path: Path) -> None:
    manifest, prices, _ = _write_package(tmp_path)
    prices.write_text(prices.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    with pytest.raises(ResearchDataReadinessError) as caught:
        ResearchDataPackageImporter(_config()).load(manifest, project_root=tmp_path)

    assert caught.value.code == "research_data_checksum_mismatch"


def test_import_rejects_universe_gap(tmp_path: Path) -> None:
    manifest, _, _ = _write_package(tmp_path, universe_days=3)

    with pytest.raises(ResearchDataReadinessError) as caught:
        ResearchDataPackageImporter(_config()).load(manifest, project_root=tmp_path)

    assert caught.value.code == "research_data_universe_gap"


def test_import_rejects_insufficient_coverage(tmp_path: Path) -> None:
    manifest, _, _ = _write_package(tmp_path)

    with pytest.raises(ResearchDataReadinessError) as caught:
        ResearchDataPackageImporter(_config(minimum_instruments=3)).load(
            manifest, project_root=tmp_path
        )

    assert caught.value.code == "research_data_coverage_insufficient"


def test_import_rejects_path_outside_project(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    manifest, _, _ = _write_package(root)
    outside = tmp_path / "outside.csv"
    outside.write_text("outside", encoding="utf-8")
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    raw["prices"]["path"] = "../outside.csv"
    raw["prices"]["sha256"] = _checksum(outside)
    manifest.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ResearchDataReadinessError) as caught:
        ResearchDataPackageImporter(_config()).load(manifest, project_root=root)

    assert caught.value.code == "research_data_path_invalid"


def test_cli_imports_explicit_package_and_writes_receipt(tmp_path: Path) -> None:
    manifest, _, _ = _write_package(tmp_path)
    config = tmp_path / "policy.yaml"
    config.write_text(
        yaml.safe_dump(_config().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    output = tmp_path / "receipts"

    result = CLI.invoke(
        app,
        [
            "research-data-package-import",
            "--manifest",
            str(manifest),
            "--project-root",
            str(tmp_path),
            "--output",
            str(output),
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0
    assert "Research data package: READY_FOR_QR14" in result.stdout
    assert len(tuple(output.glob("readiness-*.json"))) == 1


def test_policy_cli_and_invalid_config(tmp_path: Path) -> None:
    result = CLI.invoke(app, ["research-data-readiness-check"])
    assert result.exit_code == 0
    assert "Research data readiness valid: qr_data_readiness_v1" in result.stdout

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("schema_version: [", encoding="utf-8")
    with pytest.raises(ResearchDataReadinessError) as caught:
        DataReadinessConfig.load(invalid)
    assert caught.value.code == "research_data_readiness_config_invalid"
