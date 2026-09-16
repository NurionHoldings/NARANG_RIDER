from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Money:
    """Integer Korean won; floating-point money is deliberately unsupported."""

    won: int

    def __post_init__(self) -> None:
        if isinstance(self.won, bool) or not isinstance(self.won, int):
            raise TypeError("MONEY_MUST_BE_INTEGER_WON")

    def __add__(self, other: Money) -> Money:
        return Money(self.won + other.won)

    def __sub__(self, other: Money) -> Money:
        return Money(self.won - other.won)


ZERO = Money(0)
