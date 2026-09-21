"""Load and normalize the LUMI metadata dictionary without source-row access."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.understanding.models import LogicalDataType, MetadataKnowledgeBase


class MetadataLoadError(RuntimeError):
    """Safe failure raised when dictionary ingestion or normalization fails."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class MetadataRecord:
    """Normalized metadata for one source column."""

    column_name: str
    business_name: str | None
    description: str | None
    source_type: str | None
    logical_type: LogicalDataType
    mandatory: bool | None
    sensitive: bool | None
    lineage: str | None
    business_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LoadedMetadata:
    """Detailed records plus the canonical compact knowledge-base model."""

    records: tuple[MetadataRecord, ...]
    knowledge_base: MetadataKnowledgeBase

    @property
    def by_column(self) -> dict[str, MetadataRecord]:
        return {record.column_name.casefold(): record for record in self.records}


class LumiMetadataLoader:
    """Read XLSX/CSV metadata and normalize known LUMI dictionary headings."""

    _ALIASES = {
        "name": "name",
        "column": "name",
        "column_name": "name",
        "field": "name",
        "field_name": "name",
        "attribute_name": "name",
        "attribute_business_name": "business_name",
        "business_name": "business_name",
        "display_name": "business_name",
        "description": "description",
        "column_description": "description",
        "business_description": "description",
        "type": "source_type",
        "data_type": "source_type",
        "datatype": "source_type",
        "logical_type": "source_type",
        "mandatory": "mandatory",
        "is_mandatory": "mandatory",
        "required": "mandatory",
        "nullable": "nullable",
        "sde": "sensitive",
        "sensitive": "sensitive",
        "is_sensitive": "sensitive",
        "contains_pii": "sensitive",
        "lineage": "lineage",
        "source": "lineage",
    }
    _TYPE_MAP = {
        "string": LogicalDataType.STRING,
        "str": LogicalDataType.STRING,
        "int": LogicalDataType.INTEGER,
        "int64": LogicalDataType.INTEGER,
        "integer": LogicalDataType.INTEGER,
        "float": LogicalDataType.FLOAT,
        "float64": LogicalDataType.FLOAT,
        "double": LogicalDataType.FLOAT,
        "boolean": LogicalDataType.BOOLEAN,
        "bool": LogicalDataType.BOOLEAN,
        "date": LogicalDataType.DATE,
        "datetime": LogicalDataType.DATETIME,
        "timestamp": LogicalDataType.DATETIME,
        "json": LogicalDataType.JSON,
        "array": LogicalDataType.ARRAY,
    }

    def load(
        self,
        dictionary_path: str | Path,
        source_columns: set[str] | None = None,
        *,
        dictionary_version: str | None = None,
    ) -> LoadedMetadata:
        path = Path(dictionary_path).expanduser().resolve()
        if not path.is_file():
            raise MetadataLoadError("METADATA_NOT_FOUND", "Metadata dictionary was not found.")
        try:
            frame = self._read(path)
            normalized_headers = {
                column: self._ALIASES.get(self._normalize_heading(str(column)))
                for column in frame.columns
            }
            name_columns = [column for column, target in normalized_headers.items() if target == "name"]
            if not name_columns:
                raise MetadataLoadError(
                    "METADATA_NAME_COLUMN_MISSING",
                    "Metadata dictionary must contain a NAME column.",
                )
            records, warnings = self._normalize_rows(frame, normalized_headers, name_columns[0])
        except MetadataLoadError:
            raise
        except Exception as exc:
            raise MetadataLoadError(
                "METADATA_READ_FAILED",
                f"Metadata dictionary could not be read ({type(exc).__name__}).",
            ) from exc

        metadata_names = {record.column_name.casefold() for record in records}
        supplied_names = {name.casefold() for name in (source_columns or set())}
        unmatched = sorted(source_columns or set(), key=str.casefold) if source_columns is not None else []
        unmatched = [name for name in unmatched if name.casefold() not in metadata_names]
        dictionary_only = sorted(
            (record.column_name for record in records if supplied_names and record.column_name.casefold() not in supplied_names),
            key=str.casefold,
        )
        descriptions = {
            record.column_name: record.description
            for record in records
            if record.description
        }
        declared_types = {
            record.column_name: record.logical_type.value
            for record in records
        }
        business_terms = {
            record.column_name: list(record.business_terms)
            for record in records
            if record.business_terms
        }
        knowledge_base = MetadataKnowledgeBase(
            dictionary_name=path.name,
            dictionary_version=dictionary_version,
            column_descriptions=descriptions,
            declared_types=declared_types,
            business_terms=business_terms,
            unmatched_source_columns=unmatched,
            dictionary_only_columns=dictionary_only,
            warnings=warnings,
        )
        return LoadedMetadata(records=tuple(records), knowledge_base=knowledge_base)

    @staticmethod
    def _read(path: Path) -> pd.DataFrame:
        suffix = path.suffix.casefold()
        if suffix in {".xlsx", ".xlsm", ".xls"}:
            return pd.read_excel(path, dtype=object)
        if suffix == ".csv":
            return pd.read_csv(path, dtype=object, low_memory=True)
        raise MetadataLoadError(
            "UNSUPPORTED_METADATA_FORMAT",
            "Metadata dictionary must be an XLSX, XLS, XLSM or CSV file.",
        )

    def _normalize_rows(
        self,
        frame: pd.DataFrame,
        header_map: dict[Any, str | None],
        name_column: Any,
    ) -> tuple[list[MetadataRecord], list[str]]:
        records: list[MetadataRecord] = []
        warnings: list[str] = []
        seen: set[str] = set()
        reverse = {target: source for source, target in header_map.items() if target}
        for _, row in frame.iterrows():
            raw_name = self._clean(row.get(name_column))
            if not raw_name or raw_name.casefold().startswith("source:"):
                continue
            key = raw_name.casefold()
            if key in seen:
                warnings.append(f"Duplicate metadata entry ignored for column {raw_name!r}.")
                continue
            seen.add(key)
            business_name = self._value(row, reverse, "business_name")
            description = self._value(row, reverse, "description")
            source_type = self._value(row, reverse, "source_type")
            records.append(
                MetadataRecord(
                    column_name=raw_name,
                    business_name=business_name,
                    description=description,
                    source_type=source_type,
                    logical_type=self._TYPE_MAP.get((source_type or "").casefold(), LogicalDataType.UNKNOWN),
                    mandatory=self._mandatory(row, reverse),
                    sensitive=self._boolean(self._value(row, reverse, "sensitive")),
                    lineage=self._value(row, reverse, "lineage"),
                    business_terms=self._terms(business_name, description),
                )
            )
        if not records:
            raise MetadataLoadError("METADATA_EMPTY", "No valid column definitions were found.")
        return records, warnings

    def _mandatory(self, row: pd.Series, reverse: dict[str, Any]) -> bool | None:
        explicit = self._boolean(self._value(row, reverse, "mandatory"))
        if explicit is not None:
            return explicit
        nullable = self._boolean(self._value(row, reverse, "nullable"))
        return None if nullable is None else not nullable

    @staticmethod
    def _normalize_heading(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")

    @staticmethod
    def _clean(value: Any) -> str | None:
        if value is None or pd.isna(value):
            return None
        cleaned = str(value).strip()
        return cleaned or None

    def _value(self, row: pd.Series, reverse: dict[str, Any], name: str) -> str | None:
        column = reverse.get(name)
        return self._clean(row.get(column)) if column is not None else None

    @staticmethod
    def _boolean(value: str | None) -> bool | None:
        if value is None:
            return None
        if value.casefold() in {"yes", "y", "true", "1"}:
            return True
        if value.casefold() in {"no", "n", "false", "0"}:
            return False
        return None

    @staticmethod
    def _terms(*values: str | None) -> tuple[str, ...]:
        stop = {"the", "a", "an", "of", "and", "for", "in", "to", "is", "this"}
        tokens = {
            token
            for value in values
            if value
            for token in re.findall(r"[a-z0-9]+", value.casefold())
            if len(token) > 1 and token not in stop
        }
        return tuple(sorted(tokens))
