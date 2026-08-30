"""Retrieval, proved absent by its type.

The Port has a `status` and nothing else: there is no `query`, no `search`, no
`retrieve`. Counting Retrieval calls at runtime would only ever show zero for the
inputs a test happened to try; a Port with no query method cannot be called at all,
by anyone, on any path. `DisabledRetrieval` is its only implementation, and its
`status` is single-valued, so `retrieval_status` in the public Policy Projection is
read from here rather than spelled as a literal in each adapter.

Lives in its own module purely so `adapters/direct.py` can read it without importing
the `adapters` package it is itself imported by.
"""

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


@runtime_checkable
class RetrievalPort(Protocol):
    @property
    def status(self) -> Literal["disabled"]: ...


@dataclass(frozen=True)
class DisabledRetrieval:
    """`status` is a property, not a field, so "single-valued" is an invariant rather
    than a comment: there is no constructor argument, and `DisabledRetrieval()` is
    the only value that can exist."""

    @property
    def status(self) -> Literal["disabled"]:
        return "disabled"


DISABLED_RETRIEVAL = DisabledRetrieval()
