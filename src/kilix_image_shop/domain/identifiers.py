"""Typed, canonical identifiers used by the immutable document model."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import ClassVar, Self


class DomainValidationError(ValueError):
    """A domain value is malformed or violates a closed invariant."""


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True, order=True)
class _UuidIdentifier:
    value: str

    kind: ClassVar[str] = "identifier"

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise DomainValidationError(f"{self.kind} must be a string")
        try:
            parsed = uuid.UUID(self.value)
        except (AttributeError, ValueError) as exc:
            raise DomainValidationError(f"invalid {self.kind}") from exc
        if str(parsed) != self.value:
            raise DomainValidationError(
                f"{self.kind} must use canonical lowercase UUID form"
            )

    @classmethod
    def parse(cls, value: object) -> Self:
        if not isinstance(value, str):
            raise DomainValidationError(f"{cls.kind} must be a string")
        return cls(value)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class DocumentId(_UuidIdentifier):
    kind: ClassVar[str] = "document ID"


@dataclass(frozen=True, slots=True, order=True)
class RevisionId(_UuidIdentifier):
    kind: ClassVar[str] = "revision ID"


@dataclass(frozen=True, slots=True, order=True)
class LayerId(_UuidIdentifier):
    kind: ClassVar[str] = "layer ID"


@dataclass(frozen=True, slots=True, order=True)
class ObjectId:
    """A lowercase SHA-256 content identity."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or _SHA256_RE.fullmatch(self.value) is None:
            raise DomainValidationError(
                "object ID must be exactly 64 lowercase hexadecimal characters"
            )

    @classmethod
    def parse(cls, value: object) -> Self:
        if not isinstance(value, str):
            raise DomainValidationError("object ID must be a string")
        return cls(value)

    @classmethod
    def from_bytes(cls, payload: bytes) -> Self:
        if not isinstance(payload, bytes):
            raise DomainValidationError("content payload must be immutable bytes")
        return cls(hashlib.sha256(payload).hexdigest())

    def __str__(self) -> str:
        return self.value
