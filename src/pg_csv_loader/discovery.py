"""Safe local PostgreSQL detection and SQLAlchemy Engine creation."""

import os
import socket
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
import questionary
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

console = Console()


def get_default_username() -> str:
    """Resolve default username checking PGUSER, USER (Unix), and USERNAME (Windows)."""
    return (
        os.environ.get("PGUSER")
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "postgres"
    )


def _get_unix_socket_dirs() -> list[str | Path]:
    """Return platform-specific Unix domain socket search paths."""
    if sys.platform == "win32":
        return []
    if sys.platform == "darwin":
        return ["/tmp", "/opt/homebrew/var/postgresql", "/usr/local/var/postgres", "/var/run/postgresql"]
    # Linux, FreeBSD, OpenBSD, and others
    return ["/var/run/postgresql", "/run/postgresql", "/tmp", "/var/tmp"]


def scan_local_postgres() -> tuple[bool, str | None, int]:
    """Check if 127.0.0.1:5432 or local Unix domain sockets are accepting connections.

    Safe mode: no port scanning, no credential brute-forcing.
    """
    # 1. Check TCP 127.0.0.1:5432 and localhost:5432
    for host in ("127.0.0.1", "localhost"):
        try:
            with socket.create_connection((host, 5432), timeout=0.5):
                return True, host, 5432
        except OSError:
            pass

    # 2. Check Unix domain socket locations
    for sock_dir in _get_unix_socket_dirs():
        p = Path(sock_dir)
        if p.exists() and p.is_dir():
            for sock in sorted(p.glob(".s.PGSQL.*")):
                if sock.is_socket():
                    port_str = sock.name.rsplit(".", 1)[-1]
                    port = int(port_str) if port_str.isdigit() else 5432
                    return True, str(sock_dir), port

    return False, None, 5432


def normalize_pg_uri(uri: str) -> str:
    """Normalize postgresql:// or postgres:// URI to postgresql+psycopg://."""
    stripped = uri.strip()
    if stripped.startswith("postgres://"):
        return "postgresql+psycopg://" + stripped[len("postgres://"):]
    if stripped.startswith("postgresql://") and not stripped.startswith("postgresql+"):
        return "postgresql+psycopg://" + stripped[len("postgresql://"):]
    return stripped


def _connect_manual() -> Engine | None:
    while True:
        uri = questionary.text("Enter PostgreSQL connection URI (e.g. postgresql://user:pass@localhost:5432/dbname):").ask()
        if uri is None:
            return None
        normalized = normalize_pg_uri(uri)
        try:
            engine = create_engine(normalized)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            console.print("[green]Successfully connected to database.[/green]")
            return engine
        except SQLAlchemyError as err:
            console.print(
                Panel(
                    f"[bold red]Connection failed:[/bold red]\n{err}",
                    title="Database Connection Error",
                    border_style="red",
                )
            )
            retry = questionary.confirm("Would you like to try entering the connection string again?", default=True).ask()
            if not retry:
                return None


def _connect_local(host: str, port: int = 5432, initial_db: str | None = None) -> Engine | None:
    current_user = get_default_username()
    current_pwd = os.environ.get("PGPASSWORD")
    if not initial_db:
        initial_db = os.environ.get("PGDATABASE") or "postgres"

    # Prompt upfront for credentials (crucial for Docker/custom Postgres setups)
    username = questionary.text("PostgreSQL username:", default=current_user).ask()
    if username is None:
        return None
    pwd = questionary.password("PostgreSQL password (press Enter if none):").ask()
    if pwd is None:
        return None
    current_user = username.strip() or "postgres"
    current_pwd = pwd if pwd else os.environ.get("PGPASSWORD")

    while True:
        url = URL.create(
            drivername="postgresql+psycopg",
            username=current_user,
            password=current_pwd,
            host=host,
            port=port,
            database=initial_db,
        )
        try:
            init_engine = create_engine(url)
            with init_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            break
        except SQLAlchemyError as err:
            console.print(
                Panel(
                    f"[bold red]Connection failed for user '{current_user}' on host '{host}:{port}':[/bold red]\n{err}",
                    title="Database Connection Error",
                    border_style="red",
                )
            )
            retry = questionary.confirm("Would you like to re-enter username, password, or initial DB?", default=True).ask()
            if not retry:
                return None
            username = questionary.text("PostgreSQL username:", default=current_user).ask()
            if username is None:
                return None
            pwd = questionary.password("PostgreSQL password (press Enter if none):").ask()
            if pwd is None:
                return None
            db_input = questionary.text("Initial Database name:", default=initial_db).ask()
            if db_input is None:
                return None
            current_user = username.strip() or "postgres"
            current_pwd = pwd if pwd else os.environ.get("PGPASSWORD")
            initial_db = db_input.strip() or "postgres"

    # Discover accessible user databases
    databases: list[str] = []
    try:
        with init_engine.connect() as conn:
            res = conn.execute(
                text(
                    "SELECT datname FROM pg_database "
                    "WHERE datistemplate = false AND datallowconn = true "
                    "AND has_database_privilege(current_user, datname, 'CONNECT') "
                    "ORDER BY datname;"
                )
            )
            databases = [r[0] for r in res.fetchall()]
    except Exception:
        databases = [initial_db]

    if not databases:
        databases = [initial_db]

    if len(databases) > 1:
        # Prioritize application database over default system 'postgres'
        non_admin = [d for d in databases if d != "postgres"]
        default_target = non_admin[0] if non_admin else "postgres"
        target_db = questionary.select(
            "Select target database:",
            choices=databases,
            default=default_target,
        ).ask()
        if target_db is None:
            init_engine.dispose()
            return None
    else:
        target_db = databases[0]

    final_url = init_engine.url.set(database=target_db)
    init_engine.dispose()
    final_engine = create_engine(final_url)
    try:
        with final_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as err:
        console.print(
            Panel(
                f"[bold red]Cannot connect to target database '{target_db}':[/bold red]\n{err}",
                title="Target Database Connection Error",
                border_style="red",
            )
        )
        final_engine.dispose()
        return None

    console.print(
        Panel(
            f"[bold green]Connected successfully![/bold green]\n"
            f"• Target Database: [bold cyan]{target_db}[/bold cyan]\n"
            f"• Host & Port:     [cyan]{host}:{port}[/cyan]\n"
            f"• User:            [cyan]{current_user}[/cyan]",
            title="PostgreSQL Connection Established",
            border_style="green",
        )
    )
    return final_engine


def _connect_params() -> Engine | None:
    host = questionary.text("PostgreSQL host (e.g. localhost, 127.0.0.1):", default="localhost").ask()
    if host is None:
        return None
    port_str = questionary.text("PostgreSQL port:", default="5432").ask()
    if port_str is None:
        return None
    port = int(port_str.strip()) if port_str.strip().isdigit() else 5432
    db_default = os.environ.get("PGDATABASE") or "postgres"
    db = questionary.text("Database name:", default=db_default).ask()
    if db is None:
        return None
    return _connect_local(host=host.strip(), port=port, initial_db=db.strip() or "postgres")


def get_engine() -> Engine:
    """Prompt user to discover local PostgreSQL or enter connection URI manually."""
    while True:
        choice = questionary.select(
            "How would you like to connect to PostgreSQL?",
            choices=[
                "Scan for Local PostgreSQL Instances (Safe Mode)",
                "Enter Connection Parameters (Host, Port, User, DB)",
                "Enter Connection String Manually (URI)",
                "Exit",
            ],
        ).ask()

        if choice is None or choice == "Exit":
            console.print("[yellow]Aborted by user.[/yellow]")
            sys.exit(0)

        if choice == "Scan for Local PostgreSQL Instances (Safe Mode)":
            found, host, port = scan_local_postgres()
            if not found or host is None:
                console.print(
                    Panel(
                        "No local PostgreSQL instance detected on 127.0.0.1:5432 or standard Unix domain sockets (/var/run/postgresql, /tmp).",
                        title="Local Scan Result",
                        border_style="yellow",
                    )
                )
                fallback = questionary.select(
                    "What would you like to do?",
                    choices=[
                        "Enter Connection Parameters (Host, Port, User, DB)",
                        "Enter Connection String Manually (URI)",
                        "Retry Local Scan",
                        "Exit",
                    ],
                ).ask()
                if fallback == "Enter Connection Parameters (Host, Port, User, DB)":
                    engine = _connect_params()
                    if engine:
                        return engine
                elif fallback == "Enter Connection String Manually (URI)":
                    engine = _connect_manual()
                    if engine:
                        return engine
                elif fallback == "Exit" or fallback is None:
                    console.print("[yellow]Aborted by user.[/yellow]")
                    sys.exit(0)
                continue

            console.print(f"[green]Detected local PostgreSQL instance via '{host}:{port}'.[/green]")
            engine = _connect_local(host, port)
            if engine:
                return engine

        elif choice == "Enter Connection Parameters (Host, Port, User, DB)":
            engine = _connect_params()
            if engine:
                return engine

        elif choice == "Enter Connection String Manually (URI)":
            engine = _connect_manual()
            if engine:
                return engine
