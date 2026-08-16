from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from personal_quant.cli import app
from personal_quant.domain.identifiers import InstrumentKey
from personal_quant.instruments import InstrumentSnapshotStore
from personal_quant.research_universe import (
    PointInTimeUniverseStore,
    ResearchUniverseError,
    UniverseQualityPolicy,
    discover_snapshot_directories,
)

CLI = CliRunner()
POLICY = Path("config/research/universe_quality_v1.yaml")


def _row(token: int, symbol: str, isin: str | None = None) -> dict[str, object]:
    return {
        "instrument_token": token,
        "exchange_token": str(token),
        "tradingsymbol": symbol,
        "name": symbol,
        "expiry": "",
        "strike": "0",
        "tick_size": "0.05",
        "lot_size": 1,
        "instrument_type": "EQ",
        "segment": "NSE",
        "exchange": "NSE",
        "isin": isin or f"INE-{symbol}",
    }


def _snapshots(root: Path) -> tuple[Path, ...]:
    store = InstrumentSnapshotStore(root)
    store.save(
        rows=[_row(1, "INFY"), _row(2, "OLD")],
        snapshot_date=date(2026, 8, 13),
        downloaded_at=datetime(2026, 8, 13, 4, tzinfo=UTC),
    )
    store.save(
        rows=[_row(3, "INFY"), _row(4, "NEW")],
        snapshot_date=date(2026, 8, 14),
        downloaded_at=datetime(2026, 8, 14, 4, tzinfo=UTC),
    )
    return discover_snapshot_directories(root)


def test_builds_immutable_exact_date_universe_and_transitions(tmp_path: Path) -> None:
    project = tmp_path
    directories = _snapshots(project / "data/reference/instruments")
    store = PointInTimeUniverseStore(project / "research/data/universes")
    universe = store.build(
        snapshot_directories=directories,
        policy=UniverseQualityPolicy.load(POLICY),
        created_at=datetime(2026, 8, 15, tzinfo=UTC),
        project_root=project,
    )

    assert universe.contains(InstrumentKey("NSE:OLD"), observed_on=date(2026, 8, 13))
    assert not universe.contains(InstrumentKey("NSE:OLD"), observed_on=date(2026, 8, 14))
    assert universe.observations[1].added == ("NSE:NEW",)
    assert universe.observations[1].removed == ("NSE:OLD",)
    manifest_path = project / universe.manifest.data_path
    manifest_path = manifest_path.parent / "manifest.json"
    assert store.load(manifest_path, project_root=project) == universe
    assert (
        store.build(
            snapshot_directories=directories,
            policy=UniverseQualityPolicy.load(POLICY),
            created_at=datetime(2026, 8, 16, tzinfo=UTC),
            project_root=project,
        ).manifest.universe_id
        == universe.manifest.universe_id
    )


def test_missing_date_is_not_backfilled(tmp_path: Path) -> None:
    universe = PointInTimeUniverseStore(tmp_path / "research/data/universes").build(
        snapshot_directories=_snapshots(tmp_path / "data/reference/instruments"),
        policy=UniverseQualityPolicy.load(POLICY),
        created_at=datetime.now(UTC),
        project_root=tmp_path,
    )

    with pytest.raises(ResearchUniverseError) as error:
        universe.members_on(date(2026, 8, 12))
    assert error.value.code == "research_universe_date_unobserved"


def test_quality_rejects_short_history_duplicate_isin_and_identity_change(tmp_path: Path) -> None:
    root = tmp_path / "data/reference/instruments"
    store = InstrumentSnapshotStore(root)
    store.save(
        rows=[_row(1, "INFY")],
        snapshot_date=date(2026, 8, 13),
        downloaded_at=datetime.now(UTC),
    )
    builder = PointInTimeUniverseStore(tmp_path / "research/data/universes")
    policy = UniverseQualityPolicy.load(POLICY)
    with pytest.raises(ResearchUniverseError, match="Too few"):
        builder.build(
            snapshot_directories=discover_snapshot_directories(root),
            policy=policy,
            created_at=datetime.now(UTC),
            project_root=tmp_path,
        )

    duplicate_root = tmp_path / "duplicate"
    duplicate_store = InstrumentSnapshotStore(duplicate_root)
    for day in (13, 14):
        duplicate_store.save(
            rows=[_row(1, "AAA", "SAME"), _row(2, "BBB", "SAME")],
            snapshot_date=date(2026, 8, day),
            downloaded_at=datetime.now(UTC),
        )
    with pytest.raises(ResearchUniverseError) as error:
        builder.build(
            snapshot_directories=discover_snapshot_directories(duplicate_root),
            policy=policy,
            created_at=datetime.now(UTC),
            project_root=tmp_path,
        )
    assert error.value.code == "research_universe_isin_duplicate"

    changed_root = tmp_path / "changed"
    changed_store = InstrumentSnapshotStore(changed_root)
    for day, isin in ((13, "ONE"), (14, "TWO")):
        changed_store.save(
            rows=[_row(day, "INFY", isin)],
            snapshot_date=date(2026, 8, day),
            downloaded_at=datetime.now(UTC),
        )
    with pytest.raises(ResearchUniverseError) as error:
        builder.build(
            snapshot_directories=discover_snapshot_directories(changed_root),
            policy=policy,
            created_at=datetime.now(UTC),
            project_root=tmp_path,
        )
    assert error.value.code == "research_universe_identity_changed"


def test_tampering_and_path_escape_are_rejected(tmp_path: Path) -> None:
    store = PointInTimeUniverseStore(tmp_path / "research/data/universes")
    universe = store.build(
        snapshot_directories=_snapshots(tmp_path / "data/reference/instruments"),
        policy=UniverseQualityPolicy.load(POLICY),
        created_at=datetime.now(UTC),
        project_root=tmp_path,
    )
    data_path = tmp_path / universe.manifest.data_path
    manifest_path = data_path.parent / "manifest.json"
    data_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ResearchUniverseError) as error:
        store.load(manifest_path, project_root=tmp_path)
    assert error.value.code == "research_universe_checksum"


def test_manifest_semantics_and_cli_boundaries_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PointInTimeUniverseStore(tmp_path / "research/data/universes")
    universe = store.build(
        snapshot_directories=_snapshots(tmp_path / "data/reference/instruments"),
        policy=UniverseQualityPolicy.load(POLICY),
        created_at=datetime.now(UTC),
        project_root=tmp_path,
    )
    manifest_path = (tmp_path / universe.manifest.data_path).parent / "manifest.json"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            '"membership_semantics": "exact_snapshot"',
            '"membership_semantics": "latest_available"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ResearchUniverseError) as error:
        store.load(manifest_path, project_root=tmp_path)
    assert error.value.code == "research_universe_manifest_invalid"

    monkeypatch_root = tmp_path / "separate"
    monkeypatch_root.mkdir()
    policy_path = Path.cwd() / POLICY
    monkeypatch.chdir(monkeypatch_root)
    result = CLI.invoke(
        app,
        [
            "research-universe-build",
            "--snapshots-root",
            "state/session",
            "--output-root",
            "research/data/universes",
            "--policy",
            str(policy_path),
        ],
    )
    assert result.exit_code == 1
    assert "research_universe_input_prohibited" in result.stderr


def test_cli_builds_and_validates_universe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _snapshots(tmp_path / "data/reference/instruments")
    monkeypatch.chdir(tmp_path)
    policy = Path(__file__).parents[1] / POLICY
    built = CLI.invoke(
        app,
        [
            "research-universe-build",
            "--snapshots-root",
            "data/reference/instruments",
            "--output-root",
            "research/data/universes",
            "--policy",
            str(policy),
        ],
    )
    assert built.exit_code == 0, built.output
    assert "Exact-date membership: enforced" in built.stdout
    manifest = next((tmp_path / "research/data/universes").glob("*/manifest.json"))
    checked = CLI.invoke(app, ["research-universe-check", "--manifest", str(manifest)])
    assert checked.exit_code == 0, checked.output
    assert "Production order routing: disabled" in checked.stdout
