from typer.testing import CliRunner

from personal_quant.cli import app

runner = CliRunner()


def test_cost_estimate_shows_all_stress_scenarios_and_version() -> None:
    result = runner.invoke(
        app,
        [
            "cost-estimate",
            "--quantity",
            "100",
            "--buy-price",
            "100",
            "--sell-price",
            "110",
            "--spread-bps",
            "10",
            "--slippage-bps",
            "5",
        ],
    )
    assert result.exit_code == 0
    assert "base: costs=INR 70.12" in result.stdout
    assert "1.5x: costs=INR 85.87" in result.stdout
    assert "2.0x: costs=INR 101.62" in result.stdout
    assert "zerodha_nse_delivery_2026-07-28_v1" in result.stdout


def test_break_even_command_matches_manual_example() -> None:
    result = runner.invoke(
        app,
        ["break-even", "--capital", "100000", "--variable-costs", "100", "--target-profit", "1000"],
    )
    assert result.exit_code == 0
    assert "INR 1600.00" in result.stdout
    assert "1.6000%" in result.stdout
