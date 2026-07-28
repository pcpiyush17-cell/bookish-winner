from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from personal_quant.domain.money import Currency, Money


def test_money_accepts_exact_values_and_rounds_half_up() -> None:
    assert Money.from_value("10.005").amount == Decimal("10.01")
    assert Money.from_value(10).amount == Decimal("10.00")


@pytest.mark.parametrize("value", [1.5, True])
def test_money_rejects_inexact_or_boolean_values(value: object) -> None:
    with pytest.raises(TypeError):
        Money.from_value(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["not-money", "NaN", "Infinity"])
def test_money_rejects_invalid_or_non_finite_values(value: str) -> None:
    with pytest.raises(ValueError):
        Money.from_value(value)


def test_money_arithmetic_and_multiplication() -> None:
    left = Money.from_value("10.25")
    right = Money.from_value("1.10")

    assert left + right == Money.from_value("11.35")
    assert left - right == Money.from_value("9.15")
    assert -right == Money.from_value("-1.10")
    assert right.multiply(3) == Money.from_value("3.30")
    assert right.multiply(Decimal("1.5")) == Money.from_value("1.65")


def test_money_rejects_unsupported_arithmetic() -> None:
    money = Money.from_value("1")

    with pytest.raises(TypeError, match="another Money"):
        money + Decimal("1")  # type: ignore[operator]
    with pytest.raises(TypeError, match="multiplier"):
        money.multiply(1.5)  # type: ignore[arg-type]


def test_money_rejects_non_decimal_constructor_input() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        Money("1.00")  # type: ignore[arg-type]


@given(st.integers(min_value=-1_000_000, max_value=1_000_000))
def test_adding_zero_never_changes_money(paise: int) -> None:
    value = Money(Decimal(paise) / Decimal(100))

    assert value + Money.from_value(0) == value


def test_currency_is_explicit() -> None:
    assert Money.from_value("1.00").currency is Currency.INR
