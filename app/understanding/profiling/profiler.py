"""Single-pass bounded-memory profiler for any ``SourceAdapter``."""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from app.understanding.models import DatasetProfile, ProcessingLimits
from app.understanding.profiling.accumulators import AccumulatorOptions, DatasetAccumulator
from app.understanding.source_adapters.base import SourceAdapter, SourceAdapterError, SourceInspection, TabularChunk


class ProfilingError(RuntimeError):
    """Safe profiling failure that excludes source values and transcript text."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, str | bool]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


@dataclass(frozen=True, slots=True)
class ProfilerOptions:
    """Execution controls loaded from profiling configuration."""

    max_worker_threads: int = 1
    max_in_flight_chunks: int = 1

    def __post_init__(self) -> None:
        if self.max_worker_threads < 1:
            raise ValueError("max_worker_threads must be positive")
        if self.max_in_flight_chunks < 1:
            raise ValueError("max_in_flight_chunks must be positive")

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "ProfilerOptions":
        return cls(
            max_worker_threads=int(values.get("max_worker_threads", 1)),
            max_in_flight_chunks=int(values.get("max_in_flight_chunks", 1)),
        )


class StreamingProfiler:
    """Create one ``DatasetProfile`` while reading the source exactly once."""

    def __init__(
        self,
        limits: ProcessingLimits,
        accumulator_options: AccumulatorOptions,
        options: ProfilerOptions | None = None,
    ) -> None:
        self._limits = limits
        self._accumulator_options = accumulator_options
        self._options = options or ProfilerOptions()

    def profile(self, adapter: SourceAdapter) -> DatasetProfile:
        """Profile an adapter with deterministic, bounded merge order."""

        started_at = datetime.now(timezone.utc)
        timer = perf_counter()
        try:
            inspection = adapter.inspect()
            aggregate = DatasetAccumulator(inspection.columns, self._accumulator_options)
            if self._options.max_worker_threads == 1:
                for chunk in adapter.iter_chunks():
                    aggregate.update_chunk(chunk)
            else:
                self._profile_parallel(adapter, inspection, aggregate)

            if inspection.exact_row_count is not None and aggregate.row_count != inspection.exact_row_count:
                raise ProfilingError(
                    "ROW_COUNT_MISMATCH",
                    "Profiled row count differs from the source inspection count.",
                )

            completed_at = datetime.now(timezone.utc)
            return DatasetProfile(
                row_count=aggregate.row_count,
                column_count=len(inspection.columns),
                chunks_processed=aggregate.chunks_processed,
                started_at=started_at,
                completed_at=completed_at,
                elapsed_seconds=perf_counter() - timer,
                limits=self._limits,
                columns=aggregate.profiles(),
                duplicate_count_mode="not_computed",
                warnings=list(inspection.warnings),
            )
        except (ProfilingError, SourceAdapterError):
            raise
        except Exception as exc:
            raise ProfilingError(
                "PROFILING_FAILED",
                f"Dataset profiling failed ({type(exc).__name__}).",
                retryable=False,
            ) from exc

    def _profile_parallel(
        self,
        adapter: SourceAdapter,
        inspection: SourceInspection,
        aggregate: DatasetAccumulator,
    ) -> None:
        """Process bounded chunks concurrently and merge in source order."""

        pending: deque[Future[DatasetAccumulator]] = deque()
        with ThreadPoolExecutor(
            max_workers=self._options.max_worker_threads,
            thread_name_prefix="undq-profile",
        ) as executor:
            try:
                for chunk in adapter.iter_chunks():
                    pending.append(executor.submit(self._profile_chunk, inspection, chunk))
                    if len(pending) >= self._options.max_in_flight_chunks:
                        aggregate.merge(pending.popleft().result())
                while pending:
                    aggregate.merge(pending.popleft().result())
            except Exception:
                for future in pending:
                    future.cancel()
                raise

    def _profile_chunk(
        self,
        inspection: SourceInspection,
        chunk: TabularChunk,
    ) -> DatasetAccumulator:
        partial = DatasetAccumulator(inspection.columns, self._accumulator_options)
        partial.update_chunk(chunk)
        return partial
