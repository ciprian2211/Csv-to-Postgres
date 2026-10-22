"""CSV file reading and header-to-schema validation."""

import csv
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
import questionary

console = Console()


class ValidationError(Exception):
    """Raised when CSV structure or headers fail validation against database schema."""
    pass


def normalize_csv_path(raw_path: str | Path) -> Path:
    """Strip surrounding quotes, expand user directory (~), and resolve absolute path."""
    cleaned = str(raw_path).strip().strip("\"'")
    return Path(cleaned).expanduser().resolve()


def read_csv_headers(file_path: str | Path) -> list[str]:
    """Read and normalize CSV headers from a file using utf-8-sig encoding."""
    path = normalize_csv_path(file_path)
    if not path.is_file():
        raise ValidationError(f"CSV file not found: '{file_path}'")
    if path.stat().st_size == 0:
        raise ValidationError(f"CSV file is empty: '{file_path}'")

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            try:
                raw_headers = next(reader)
            except StopIteration:
                raise ValidationError(f"CSV file contains no rows: '{file_path}'")
    except (UnicodeDecodeError, csv.Error) as err:
        raise ValidationError(f"Cannot read CSV file '{file_path}': {err}")

    headers = [h.strip() for h in raw_headers]

    if not headers or any(h == "" for h in headers):
        raise ValidationError("CSV contains empty column header name(s).")

    duplicates = [h for h in headers if headers.count(h) > 1]
    if duplicates:
        unique_dups = sorted(set(duplicates))
        raise ValidationError(f"CSV contains duplicate column header(s): {', '.join(unique_dups)}")

    return headers


def validate_headers(headers: list[str], db_columns: list[dict[str, Any]]) -> None:
    """Validate CSV headers against database column metadata.

    Rejects unknown columns and missing non-nullable columns without defaults/identity.
    """
    if not headers:
        raise ValidationError("CSV header list is empty.")

    db_col_names = {col["name"] for col in db_columns}

    # Unknown columns check
    unknown_cols = [h for h in headers if h not in db_col_names]
    if unknown_cols:
        raise ValidationError(
            f"CSV contains unknown column(s) not present in table: {', '.join(unknown_cols)}"
        )

    # Missing required columns check
    header_set = set(headers)
    missing_required = []
    for col in db_columns:
        col_name = col["name"]
        if col_name in header_set:
            continue

        nullable = col.get("nullable", True)
        default = col.get("default")
        autoincrement = col.get("autoincrement")
        identity = col.get("identity")

        is_auto = (autoincrement is True) or (autoincrement == "auto") or (identity is not None)

        if (nullable is False) and (default is None) and not is_auto:
            missing_required.append(col_name)

    if missing_required:
        raise ValidationError(
            f"CSV is missing required NOT NULL database column(s): {', '.join(missing_required)}"
        )


def prompt_and_validate_csv(db_columns: list[dict[str, Any]]) -> tuple[Path, list[str]]:
    """Prompt user for CSV path with autocompletion and validate headers against DB schema."""
    while True:
        raw_path = questionary.path(
            "Enter path to CSV file:",
            only_directories=False,
        ).ask()

        if raw_path is None:
            console.print("[yellow]Aborted by user.[/yellow]")
            sys.exit(0)

        path = normalize_csv_path(raw_path)
        try:
            headers = read_csv_headers(path)
            validate_headers(headers, db_columns)
            console.print(f"[green]CSV validated successfully ({len(headers)} columns matched).[/green]")
            return path, headers
        except ValidationError as err:
            console.print(
                Panel(
                    f"[bold red]Validation Error:[/bold red]\n{err}",
                    title="CSV Validation Failed",
                    border_style="red",
                )
            )
            retry = questionary.confirm("Would you like to select another CSV file?", default=True).ask()
            if not retry:
                console.print("[yellow]Aborted by user.[/yellow]")
                sys.exit(1)
