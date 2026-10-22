"""SQLAlchemy inspect() wrapper for schemas, tables, and columns."""

import sys
from typing import Any

from rich.console import Console
from rich.panel import Panel
import questionary
from sqlalchemy import Engine, inspect
from sqlalchemy.exc import SQLAlchemyError

console = Console()

EXCLUDED_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}


def get_available_tables(engine: Engine, inspector: Any = None) -> list[tuple[str | None, str]]:
    """Fetch non-system schemas and collect all (schema, table_name) pairs."""
    insp = inspector or inspect(engine)
    try:
        raw_schemas = insp.get_schema_names()
        schemas = [
            s
            for s in raw_schemas
            if s not in EXCLUDED_SCHEMAS
            and not s.startswith("pg_toast")
            and not s.startswith("pg_temp")
        ]
    except NotImplementedError:
        # SQLite or dialect without multiple schema support
        schemas = [None]

    if not schemas:
        schemas = [None]

    tables: list[tuple[str | None, str]] = []
    for schema in schemas:
        for tbl in insp.get_table_names(schema=schema):
            tables.append((schema, tbl))

    return tables


def select_table(engine: Engine) -> tuple[str | None, str, list[dict[str, Any]]]:
    """Interactively select a table and retrieve its column metadata."""
    insp = inspect(engine)
    try:
        tables = get_available_tables(engine, inspector=insp)
    except SQLAlchemyError as err:
        console.print(
            Panel(
                f"[bold red]Failed to inspect database tables:[/bold red]\n{err}",
                title="Database Inspection Error",
                border_style="red",
            )
        )
        sys.exit(1)

    if not tables:
        db_name = engine.url.database or "unknown"
        host = engine.url.host or "localhost"
        port = engine.url.port or 5432
        console.print(
            Panel(
                f"[bold yellow]No user tables found in database '{db_name}'![/bold yellow]\n\n"
                f"• Host & Port: [cyan]{host}:{port}[/cyan]\n"
                f"• Database:    [cyan]{db_name}[/cyan]\n"
                f"• Schemas:     [cyan]public[/cyan] (non-system)\n\n"
                "[dim]Possible reasons:\n"
                "1. If using an ORM (EF Core, Prisma, Django, Alembic), migrations might not be applied yet.\n"
                "   (e.g., run 'dotnet ef database update' or equivalent).\n"
                "2. Your tables may reside in another database on this server.[/dim]",
                title="No Tables Detected",
                border_style="yellow",
            )
        )
        action = questionary.select(
            "What would you like to do?",
            choices=["Retry Table Scan", "Exit"],
        ).ask()
        if action == "Retry Table Scan":
            return select_table(engine)
        console.print("[yellow]Exiting.[/yellow]")
        sys.exit(0)

    choices = [
        questionary.Choice(
            title=f"{schema}.{tbl}" if schema else tbl,
            value=(schema, tbl),
        )
        for schema, tbl in tables
    ]

    selected = questionary.select(
        "Select target table:",
        choices=choices,
    ).ask()

    if selected is None:
        console.print("[yellow]Aborted by user.[/yellow]")
        sys.exit(0)

    schema, table_name = selected
    columns = insp.get_columns(table_name, schema=schema)
    return schema, table_name, columns
