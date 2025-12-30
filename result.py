"""
Result type for error handling without exceptions.

Provides Ok and Err types for functional error handling patterns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar, Union, Callable

T = TypeVar("T")
U = TypeVar("U")
V = TypeVar("V")


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    """Success variant of Result."""
    value: T

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False

    # Result ergonomics
    def map(self, fn: Callable[[T], U]) -> "Result[U]":
        """Apply fn to the inner value and return a new Ok."""
        try:
            return Ok(fn(self.value))
        except Exception as e:
            return Err(f"map() function raised: {e}")

    def and_then(self, fn: Callable[[T], "Result[U]"]) -> "Result[U]":
        """Chain a function that returns a Result."""
        try:
            return fn(self.value)
        except Exception as e:
            return Err(f"and_then() function raised: {e}")

    def unwrap(self) -> T:
        return self.value

    def unwrap_or(self, default: U) -> T:
        return self.value

    def expect(self, msg: str) -> T:
        return self.value

    def map_err(self, fn: Callable[[str], str]) -> "Result[T]":
        return self

    def match(self, ok_fn: Callable[[T], V], err_fn: Callable[[str], V]) -> V:
        return ok_fn(self.value)

    def __repr__(self) -> str:
        return f"Ok({self.value!r})"


@dataclass(frozen=True, slots=True)
class Err:
    """Error variant of Result."""
    error: str
    status_code: int | None = None

    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True
    
    # Result ergonomics
    def map(self, fn: Callable[[T], U]) -> "Result[U]":
        return self

    def and_then(self, fn: Callable[[T], "Result[U]"]) -> "Result[U]":
        return self

    def unwrap(self) -> T:
        raise RuntimeError(f"Called unwrap() on Err: {self.error}")

    def unwrap_or(self, default: T) -> T:
        return default

    def expect(self, msg: str) -> T:
        raise RuntimeError(f"{msg}: {self.error}")

    def map_err(self, fn: Callable[[str], str]) -> "Result[T]":
        return Err(fn(self.error), status_code=self.status_code)

    def match(self, ok_fn: Callable[[T], V], err_fn: Callable[[str], V]) -> V:
        return err_fn(self.error)

    def __repr__(self) -> str:
        return f"Err({self.error!r}, status_code={self.status_code!r})"


Result = Union[Ok[T], Err]
