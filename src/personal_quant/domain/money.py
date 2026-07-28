"""Exact monetary values for ledger-facing domain logic."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Self

_MINOR_UNIT = Decimal("0.01")


class Currency(StrEnum):
    """Currencies supported by the V1 domain."""

    INR = "INR"


@dataclass(frozen=True, slots=True)
class Money:
    """An immutable currency amount rounded to the currency's minor unit."""

    amount: Decimal
    currency: Currency = Currency.INR

    def __post_init__(self) -> None:
        if isinstance(self.amount, (float, bool)) or not isinstance(self.amount, Decimal):
            raise TypeError("Money amount must be a Decimal")
        if not self.amount.is_finite():
            raise ValueError("Money amount must be finite")
        object.__setattr__(self, "amount", self.amount.quantize(_MINOR_UNIT, ROUND_HALF_UP))

    @classmethod
    def from_value(cls, value: Decimal | int | str, currency: Currency = Currency.INR) -> Self:
        """Create money from an exact input; binary floats are deliberately unsupported."""
        if isinstance(value, (float, bool)):
            raise TypeError("Money value must be a Decimal, int, or string")
        try:
            amount = value if isinstance(value, Decimal) else Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"Invalid money value: {value!r}") from error
        return cls(amount=amount, currency=currency)

    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def multiply(self, multiplier: Decimal | int) -> Money:
        """Multiply by an exact scalar and round once to the minor unit."""
        if isinstance(multiplier, (float, bool)) or not isinstance(multiplier, (Decimal, int)):
            raise TypeError("Money multiplier must be a Decimal or int")
        return Money(self.amount * multiplier, self.currency)

    def _require_same_currency(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError("Money arithmetic requires another Money value")
        if self.currency is not other.currency:
            raise ValueError("Cannot combine money values with different currencies")
