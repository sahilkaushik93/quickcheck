"""Bounded-memory CSV adapter implemented with ``pandas.read_csv``."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pandas as pd
from pandas.api import types as pdt

from app.understanding.models import LogicalDataType, ProcessingLimits, SourceMetadata, SourceType
from app.understanding.source_adapters.base import (
    SourceAdapter,
    SourceCapabilities,
    SourceColumn,
    SourceConfigurationError,
    SourceInspection,
    SourceInspectionError,
    TabularChunk,
)


@dataclass(frozen=True, slots=True)
class CSVReadOptions:
    """Safe subset of pandas CSV options loaded from profiling configuration."""

    encoding: str = "utf-8"
    encoding_errors: str = "replace"
    delimiter: str = ","
    quote_character: str = '"'
    skip_blank_lines: bool = True
    on_bad_lines: str = "warn"
    infer_types_from_sample_rows: int = 5000

    def __post_init__(self) -> None:
        if not self.delimiter or len(self.delimiter) != 1:
            raise ValueError("delimiter must contain exactly one character")
        if not self.quote_character or len(self.quote_character) != 1:
            raise ValueError("quote_character must contain exactly one character")
        if self.on_bad_lines not in {"error", "warn", "skip"}:
            raise ValueError("on_bad_lines must be one of: error, warn, skip")
        if self.infer_types_from_sample_rows < 0:
            raise ValueError("infer_types_from_sample_rows cannot be negative")

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "CSVReadOptions":
        """Create options from the ``csv_reader`` configuration section."""

        return cls(
            encoding=str(values.get("encoding", "utf-8")),
            encoding_errors=str(values.get("encoding_errors", "replace")),
            delimiter=str(values.get("delimiter", ",")),
            quote_character=str(values.get("quote_character", '"')),
            skip_blank_lines=bool(values.get("skip_blank_lines", True)),
            on_bad_lines=str(values.get("on_bad_lines", "warn")),
            infer_types_from_sample_rows=int(values.get("infer_types_from_sample_rows", 5000)),
        )


@dataclass(slots=True)
class PandasTabularChunk:
    """Non-copying ``TabularChunk`` view over one pandas DataFrame."""

    chunk_index: int
    row_offset: int
    frame: pd.DataFrame

    @property
    def row_count(self) -> int:
        return int(len(self.frame.index))

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(str(column) for column in self.frame.columns)

    def column_values(self, column_name: str) -> Sequence[Any]:
        """Return a zero-based positional view of a CSV chunk column.

        Pandas Series retain original row-index labels across CSV chunks. Execution
        consumes every chunk using local zero-based positions, so returning the
        Series directly can fail on the second chunk.

        ``to_numpy(copy=False)`` provides position-based indexing while avoiding
        an unnecessary copy whenever pandas can expose the underlying array.
        """

        if column_name not in self.frame.columns:
            raise KeyError(f"Unknown CSV column: {column_name!r}")

        return cast(
            Sequence[Any],
            self.frame[column_name].to_numpy(copy=False),
        )


class CSVSourceAdapter(SourceAdapter):
    """Stream a local CSV without loading or concatenating the complete file."""

    def __init__(
        self,
        file_path: str | Path,
        metadata: SourceMetadata,
        limits: ProcessingLimits,
        options: CSVReadOptions | None = None,
    ) -> None:
        super().__init__(metadata=metadata, limits=limits)
        self._path = Path(file_path).expanduser().resolve()
        self._options = options or CSVReadOptions()

        if metadata.source_type != SourceType.CSV.value:
            raise SourceConfigurationError(
                "INVALID_SOURCE_TYPE",
                "CSVSourceAdapter requires source_type='csv'.",
            )
        if not self._path.is_file():
            raise SourceConfigurationError(
                "CSV_NOT_FOUND",
                "The configured CSV source does not exist or is not a regular file.",
            )

    def _reader_arguments(self) -> dict[str, Any]:
        return {
            "encoding": self._options.encoding,
            "encoding_errors": self._options.encoding_errors,
            "sep": self._options.delimiter,
            "quotechar": self._options.quote_character,
            "skip_blank_lines": self._options.skip_blank_lines,
            "on_bad_lines": self._options.on_bad_lines,
            "low_memory": True,
        }

    def _inspect(self) -> SourceInspection:
        sample_rows = max(1, self._options.infer_types_from_sample_rows)
        try:
            sample = pd.read_csv(self._path, nrows=sample_rows, **self._reader_arguments())
        except pd.errors.EmptyDataError as exc:
            raise SourceInspectionError(
                "CSV_HAS_NO_COLUMNS",
                "The CSV source does not contain a readable header.",
            ) from exc

        columns = tuple(
            SourceColumn(
                name=str(name),
                ordinal=ordinal,
                source_data_type=str(sample.dtypes.iloc[ordinal]),
                declared_logical_type=self._logical_type(sample.dtypes.iloc[ordinal]),
            )
            for ordinal, name in enumerate(sample.columns)
        )
        return SourceInspection(
            metadata=self.metadata,
            columns=columns,
            capabilities=SourceCapabilities(
                repeatable_read=True,
                supports_projection=True,
            ),
        )

    def _iter_chunks(self, chunk_size: int) -> Iterator[TabularChunk]:
        row_offset = 0
        reader = pd.read_csv(
            self._path,
            chunksize=chunk_size,
            **self._reader_arguments(),
        )
        emitted_index = 0
        try:
            for frame in reader:
                if frame.empty:
                    continue
                chunk = PandasTabularChunk(
                    chunk_index=emitted_index,
                    row_offset=row_offset,
                    frame=frame,
                )
                yield chunk
                row_offset += chunk.row_count
                emitted_index += 1
        finally:
            reader.close()

    @staticmethod
    def _logical_type(dtype: Any) -> LogicalDataType:
        if pdt.is_bool_dtype(dtype):
            return LogicalDataType.BOOLEAN
        if pdt.is_integer_dtype(dtype):
            return LogicalDataType.INTEGER
        if pdt.is_float_dtype(dtype):
            return LogicalDataType.FLOAT
        if pdt.is_datetime64_any_dtype(dtype):
            return LogicalDataType.DATETIME
        if pdt.is_string_dtype(dtype) or pdt.is_object_dtype(dtype):
            return LogicalDataType.STRING
        return LogicalDataType.UNKNOWN
