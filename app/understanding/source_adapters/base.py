"""Source-independent streaming contracts for the Understanding Layer.

Concrete adapters translate CSV files, BigQuery tables, databases, or in-memory
frames into bounded tabular chunks.  This module intentionally has no pandas or
cloud-SDK dependency: profiling code consumes the small protocol defined here
without knowing which technology produced a chunk.

The adapter validates chunk size, ordering, and schema stability without
copying rows or retaining chunks.  A source is single-pass by default so an
accidental second profiling pass cannot silently double I/O and runtime.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from app.understanding.models import LogicalDataType, ProcessingLimits, SourceMetadata


class SourceAdapterError(RuntimeError):
    """Base exception carrying a safe code and non-sensitive message."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, str | bool]:
        """Return a log/API-safe representation without source values."""

        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


class SourceConfigurationError(SourceAdapterError):
    """Raised when an adapter or processing limit is invalid."""


class SourceInspectionError(SourceAdapterError):
    """Raised when source metadata or schema cannot be inspected safely."""


class SourceReadError(SourceAdapterError):
    """Raised when bounded chunk iteration fails."""


class SourceSchemaChangedError(SourceAdapterError):
    """Raised when a source schema changes during a profiling run."""


class AdapterState(str, Enum):
    """Lifecycle states used to prevent invalid adapter reuse."""

    CREATED = "created"
    INSPECTED = "inspected"
    STREAMING = "streaming"
    CONSUMED = "consumed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    """Features exposed by a concrete adapter.

    Capability flags let later components delegate operations such as exact row
    counts or aggregate pushdown to BigQuery without special-casing BigQuery in
    the profiler.
    """

    repeatable_read: bool = False
    supports_projection: bool = False
    supports_filter_pushdown: bool = False
    supports_aggregate_pushdown: bool = False
    supports_exact_row_count: bool = False
    supports_parallel_partitions: bool = False


@dataclass(frozen=True, slots=True)
class SourceColumn:
    """One inspected source column, before statistical type inference."""

    name: str
    ordinal: int
    source_data_type: str | None = None
    declared_logical_type: LogicalDataType = LogicalDataType.UNKNOWN
    nullable: bool | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("source column name cannot be blank")
        if self.ordinal < 0:
            raise ValueError("source column ordinal cannot be negative")


@dataclass(frozen=True, slots=True)
class SourceInspection:
    """Small immutable result of source/schema inspection."""

    metadata: SourceMetadata
    columns: tuple[SourceColumn, ...]
    capabilities: SourceCapabilities
    estimated_row_count: int | None = None
    exact_row_count: int | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.estimated_row_count is not None and self.estimated_row_count < 0:
            raise ValueError("estimated_row_count cannot be negative")
        if self.exact_row_count is not None and self.exact_row_count < 0:
            raise ValueError("exact_row_count cannot be negative")
        if not self.columns:
            raise ValueError("source inspection must contain at least one column")

        ordinals = [column.ordinal for column in self.columns]
        if ordinals != list(range(len(self.columns))):
            raise ValueError("source column ordinals must be contiguous and zero-based")

        normalized_names = [column.name.strip().casefold() for column in self.columns]
        if len(normalized_names) != len(set(normalized_names)):
            raise ValueError("source column names must be unique, ignoring case")

        if self.exact_row_count is not None and not self.capabilities.supports_exact_row_count:
            raise ValueError(
                "exact_row_count requires the supports_exact_row_count capability"
            )

    @property
    def column_names(self) -> tuple[str, ...]:
        """Return the canonical source column order."""

        return tuple(column.name for column in self.columns)


@runtime_checkable
class TabularChunk(Protocol):
    """Zero-copy view of one bounded source chunk.

    A CSV implementation may wrap a pandas ``DataFrame`` while a BigQuery
    implementation may wrap an Arrow record batch.  ``column_values`` must
    return a sequence-like view; it must not materialize a second full copy.
    The view is valid only until the iterator advances to the next chunk.
    """

    @property
    def chunk_index(self) -> int:
        """Zero-based index within this source scan."""

        ...

    @property
    def row_offset(self) -> int:
        """Zero-based source row offset for the first row in this chunk."""

        ...

    @property
    def row_count(self) -> int:
        """Number of rows exposed by this chunk."""

        ...

    @property
    def column_names(self) -> tuple[str, ...]:
        """Column names in stable source order."""

        ...

    def column_values(self, column_name: str) -> Sequence[Any]:
        """Return a non-copying, sequence-like view for one column."""

        ...


class SourceAdapter(ABC):
    """Abstract, validated, single-pass source adapter.

    Subclasses implement only ``_inspect``, ``_iter_chunks`` and ``_close``.
    The public methods centralize lifecycle, bounded-memory and schema checks so
    all source technologies behave consistently.

    Instances are not thread-safe.  Parallelism belongs above this abstraction
    and must remain bounded by ``ProcessingLimits`` and adapter capabilities.
    """

    def __init__(self, metadata: SourceMetadata, limits: ProcessingLimits) -> None:
        if limits.chunk_size < 1:
            raise SourceConfigurationError(
                "INVALID_CHUNK_SIZE",
                "Processing chunk size must be greater than zero.",
            )

        self._metadata = metadata
        self._limits = limits
        self._state = AdapterState.CREATED
        self._inspection: SourceInspection | None = None

    @property
    def metadata(self) -> SourceMetadata:
        """Return non-sensitive source metadata."""

        return self._metadata

    @property
    def limits(self) -> ProcessingLimits:
        """Return immutable-by-convention effective processing limits."""

        return self._limits

    @property
    def state(self) -> AdapterState:
        """Return the current adapter lifecycle state."""

        return self._state

    def inspect(self) -> SourceInspection:
        """Inspect source schema once and cache only the compact result."""

        self._ensure_open()
        if self._inspection is not None:
            return self._inspection

        try:
            inspection = self._inspect()
        except SourceAdapterError:
            raise
        except Exception as exc:
            raise SourceInspectionError(
                "SOURCE_INSPECTION_FAILED",
                f"Source inspection failed ({type(exc).__name__}).",
                retryable=False,
            ) from exc

        if inspection.metadata.source_id != self._metadata.source_id:
            raise SourceInspectionError(
                "SOURCE_ID_MISMATCH",
                "Inspection metadata does not match the configured source.",
            )

        self._inspection = inspection
        self._state = AdapterState.INSPECTED
        return inspection

    def iter_chunks(self) -> Iterator[TabularChunk]:
        """Yield validated chunks without retaining or concatenating them.

        A consumed adapter can be scanned again only when the concrete adapter
        explicitly advertises ``repeatable_read``.  This prevents accidental
        repeated calculations for files and forward-only database cursors.
        """

        self._ensure_open()
        inspection = self.inspect()

        if self._state == AdapterState.STREAMING:
            raise SourceReadError(
                "CONCURRENT_ITERATION_NOT_SUPPORTED",
                "The source adapter is already being consumed.",
            )
        if self._state == AdapterState.CONSUMED and not inspection.capabilities.repeatable_read:
            raise SourceReadError(
                "SOURCE_ALREADY_CONSUMED",
                "This source is single-pass and has already been consumed.",
            )

        expected_columns = inspection.column_names
        expected_offset = 0
        expected_chunk_index = 0
        self._state = AdapterState.STREAMING

        try:
            for chunk in self._iter_chunks(self._limits.chunk_size):
                self._validate_chunk(
                    chunk=chunk,
                    expected_columns=expected_columns,
                    expected_chunk_index=expected_chunk_index,
                    expected_row_offset=expected_offset,
                )
                yield chunk
                expected_offset += chunk.row_count
                expected_chunk_index += 1
        except GeneratorExit:
            raise
        except SourceAdapterError:
            raise
        except Exception as exc:
            raise SourceReadError(
                "SOURCE_READ_FAILED",
                f"Source streaming failed ({type(exc).__name__}).",
                retryable=True,
            ) from exc
        finally:
            if self._state != AdapterState.CLOSED:
                self._state = AdapterState.CONSUMED

    def close(self) -> None:
        """Release source resources; repeated calls are safe."""

        if self._state == AdapterState.CLOSED:
            return
        try:
            self._close()
        except SourceAdapterError:
            raise
        except Exception as exc:
            raise SourceAdapterError(
                "SOURCE_CLOSE_FAILED",
                f"Source cleanup failed ({type(exc).__name__}).",
            ) from exc
        finally:
            self._state = AdapterState.CLOSED

    def __enter__(self) -> "SourceAdapter":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._state == AdapterState.CLOSED:
            raise SourceAdapterError(
                "SOURCE_ADAPTER_CLOSED",
                "The source adapter has already been closed.",
            )

    def _validate_chunk(
        self,
        *,
        chunk: TabularChunk,
        expected_columns: tuple[str, ...],
        expected_chunk_index: int,
        expected_row_offset: int,
    ) -> None:
        """Validate metadata and column lengths without iterating cell values."""

        if chunk.chunk_index != expected_chunk_index:
            raise SourceReadError(
                "NON_SEQUENTIAL_CHUNK_INDEX",
                "Source chunks must have contiguous zero-based indexes.",
            )
        if chunk.row_offset != expected_row_offset:
            raise SourceReadError(
                "NON_CONTIGUOUS_ROW_OFFSET",
                "Source chunk row offsets must be contiguous.",
            )
        if chunk.row_count <= 0:
            raise SourceReadError(
                "EMPTY_CHUNK",
                "Adapters must not emit empty chunks.",
            )
        if chunk.row_count > self._limits.chunk_size:
            raise SourceReadError(
                "CHUNK_LIMIT_EXCEEDED",
                "Adapter emitted a chunk larger than the configured limit.",
            )
        if tuple(chunk.column_names) != expected_columns:
            raise SourceSchemaChangedError(
                "SOURCE_SCHEMA_CHANGED",
                "Source columns changed during streaming.",
            )

        for column_name in expected_columns:
            values = chunk.column_values(column_name)
            if len(values) != chunk.row_count:
                raise SourceReadError(
                    "COLUMN_LENGTH_MISMATCH",
                    f"Column {column_name!r} does not match the chunk row count.",
                )

    @abstractmethod
    def _inspect(self) -> SourceInspection:
        """Return compact schema/source metadata without reading the full source."""

    @abstractmethod
    def _iter_chunks(self, chunk_size: int) -> Iterator[TabularChunk]:
        """Yield technology-specific zero-copy chunk views."""

    def _close(self) -> None:
        """Release resources owned by the concrete adapter, if any."""

