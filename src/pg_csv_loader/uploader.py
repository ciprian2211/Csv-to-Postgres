"""Reflected Table transactional batch insert & rollback handling."""

import csv
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from sqlalchemy import Engine, MetaData, Table, insert, types
from sqlalchemy.exc import SQLAlchemyError

console = Console()


def prepare_row(raw_row: dict[str, str | None], target_table: Table) -> dict[str, Any]:
    """Clean CSV row values: convert empty strings to None based on column type & nullability."""
    # shortcut: relies on DB driver type coercion for non-empty string values, upgrade to explicit parser if needed
    row: dict[str, Any] = {}
    for raw_k, val in raw_row.items():
        if raw_k is None:
            continue
        col_name = raw_k.strip()
        if col_name not in target_table.columns:
            continue

        col = target_table.columns[col_name]
        is_string = isinstance(col.type, (types.String, types.Text))
        val_clean = val if is_string else (val.strip() if isinstance(val, str) else val)

        if val_clean is None or val_clean == "":
            if not col.nullable and is_string and val == "":
                row[col_name] = ""
            else:
                row[col_name] = None
        elif isinstance(col.type, types.Boolean) and isinstance(val_clean, str):
            s = val_clean.lower()
            if s in ("true", "t", "1", "yes", "y"):
                row[col_name] = True
            elif s in ("false", "f", "0", "no", "n"):
                row[col_name] = False
            else:
                row[col_name] = val_clean
        else:
            row[col_name] = val_clean
    return row


def upload_csv(
    engine: Engine,
    schema: str | None,
    table_name: str,
    csv_path: str | Path,
    batch_size: int = 2000,
    exit_on_error: bool = False,
) -> tuple[int, Table]:
    """Reflect table, batch stream rows from CSV, and atomically commit or rollback."""
    metadata = MetaData()
    target_table = Table(table_name, metadata, schema=schema, autoload_with=engine)

    path = Path(csv_path)
    # Estimate total rows for progress bar (excluding header)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        total_rows = max(0, sum(1 for _ in f) - 1)

    total_inserted = 0

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"[cyan]Importing into '{table_name}'...", total=total_rows)

            with engine.begin() as conn:
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.DictReader(f)
                    batch: list[dict[str, Any]] = []

                    for raw_row in reader:
                        batch.append(prepare_row(raw_row, target_table))
                        if len(batch) >= batch_size:
                            conn.execute(insert(target_table), batch)
                            total_inserted += len(batch)
                            progress.update(task, completed=total_inserted)
                            batch.clear()

                    if batch:
                        conn.execute(insert(target_table), batch)
                        total_inserted += len(batch)
                        progress.update(task, completed=total_inserted)

    except (SQLAlchemyError, UnicodeDecodeError, csv.Error) as err:
        console.print(
            Panel(
                f"[bold red]Import error during batch insert:[/bold red]\n{err}",
                title="Import Failed - Transaction Rolled Back",
                border_style="red",
            )
        )
        if exit_on_error:
            sys.exit(1)
        raise

    return total_inserted, target_table
