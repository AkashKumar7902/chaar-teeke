"""One typed error for the stats lane. The API maps `code` and `message` to {"error", "message"}."""

from __future__ import annotations


class StatsError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
