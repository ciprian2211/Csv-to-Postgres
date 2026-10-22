"""CLI entry point and interactive flow orchestration for pg-csv-loader."""

import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import sqlalchemy as sa

from pg_csv_loader.discovery import get_engine
from pg_csv_loader.inspector import select_table
from pg_csv_loader.uploader import upload_csv
from pg_csv_loader.validator import prompt_and_validate_csv

console = Console()


def display_summary(
    schema: str | None,
    table_name: str,
    csv_path: Path,
    rows_appended: int,
    total_rows: int,
    elapsed_seconds: float,
) -> None:
    table = Table(title="Import Summary", show_header=True, header_style="bold green")
    table.add_column("Property", style="bold cyan")
    table.add_column("Value", style="white")

    target_display = f"{schema}.{table_name}" if schema else table_name
    table.add_row("Target Table", target_display)
    table.add_row("Source CSV File", str(csv_path))
    table.add_row("Rows Appended in This Run", f"{rows_appended:,}")
    table.add_row("Total Table Rows After Import", f"{total_rows:,}")
    table.add_row("Time Elapsed", f"{elapsed_seconds:.2f}s")

    console.print("\n")
    console.print(table)


def app() -> None:
    """Main CLI entrypoint."""
    console.print(
        Panel(
            "[bold cyan]PostgreSQL CSV Loader (pg-csv-loader)[/bold cyan]\n"
            "Safe discovery, schema validation, and atomic transactional CSV import.",
            border_style="cyan",
        )
    )

    try:
        # Phase 2: Engine Discovery
        engine = get_engine()

        # Phase 3: Table Discovery & Reflection
        schema, table_name, columns = select_table(engine)

        # Phase 4: CSV Selection & Validation
        csv_path, _ = prompt_and_validate_csv(columns)

        # Phase 5: Atomic Batch Upload
        start_time = time.time()
        rows_appended, target_table = upload_csv(
            engine=engine,
            schema=schema,
            table_name=table_name,
            csv_path=csv_path,
            exit_on_error=True,
        )
        elapsed = time.time() - start_time

        # Phase 6: Result Summary
        with engine.connect() as conn:
            total_rows = conn.execute(sa.select(sa.func.count()).select_from(target_table)).scalar() or 0

        display_summary(
            schema=schema,
            table_name=table_name,
            csv_path=csv_path,
            rows_appended=rows_appended,
            total_rows=total_rows,
            elapsed_seconds=elapsed,
        )

    except KeyboardInterrupt:
        console.print("\n[yellow]Operation cancelled by user.[/yellow]")
        sys.exit(0)


if __name__ == "__main__":
    app()
