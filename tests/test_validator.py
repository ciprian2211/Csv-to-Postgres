"""Unit tests for CSV validation, header matching, and transactional rollback."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from pg_csv_loader.discovery import normalize_pg_uri, scan_local_postgres
from pg_csv_loader.inspector import get_available_tables
from pg_csv_loader.uploader import prepare_row, upload_csv
from pg_csv_loader.validator import ValidationError, read_csv_headers, validate_headers


def test_exact_header_match():
    db_columns = [
        {"name": "id", "nullable": False, "default": None, "autoincrement": True},
        {"name": "name", "nullable": False, "default": None, "autoincrement": False},
        {"name": "email", "nullable": True, "default": None, "autoincrement": False},
    ]
    headers = ["id", "name", "email"]
    # Should pass without error
    validate_headers(headers, db_columns)


def test_csv_omitting_autoincrement_or_default_passes():
    db_columns = [
        {"name": "id", "nullable": False, "default": None, "autoincrement": True},
        {"name": "created_at", "nullable": False, "default": "now()", "autoincrement": False},
        {"name": "identity_col", "nullable": False, "default": None, "autoincrement": False, "identity": {}},
        {"name": "name", "nullable": False, "default": None, "autoincrement": False},
    ]
    # Only providing 'name' - id is autoincrement, created_at has default, identity_col has identity
    headers = ["name"]
    validate_headers(headers, db_columns)


def test_csv_omitting_required_not_null_column_fails():
    db_columns = [
        {"name": "id", "nullable": False, "default": None, "autoincrement": True},
        {"name": "required_name", "nullable": False, "default": None, "autoincrement": False},
        {"name": "optional_bio", "nullable": True, "default": None, "autoincrement": False},
    ]
    # CSV omits 'required_name'
    headers = ["id", "optional_bio"]
    with pytest.raises(ValidationError, match="missing required NOT NULL"):
        validate_headers(headers, db_columns)


def test_csv_containing_extra_column_fails():
    db_columns = [
        {"name": "id", "nullable": False, "default": None, "autoincrement": True},
        {"name": "name", "nullable": True, "default": None, "autoincrement": False},
    ]
    headers = ["id", "name", "unknown_extra_field"]
    with pytest.raises(ValidationError, match="unknown column"):
        validate_headers(headers, db_columns)


def test_csv_duplicate_and_empty_headers(tmp_path: Path):
    dup_file = tmp_path / "dup.csv"
    dup_file.write_text("id,name,id\n1,alice,1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="duplicate column header"):
        read_csv_headers(dup_file)

    empty_file = tmp_path / "empty_hdr.csv"
    empty_file.write_text("id,,name\n1,x,alice\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="empty column header"):
        read_csv_headers(empty_file)


def test_read_csv_missing_and_zero_byte_file(tmp_path: Path):
    non_existent = tmp_path / "does_not_exist.csv"
    with pytest.raises(ValidationError, match="CSV file not found"):
        read_csv_headers(non_existent)

    zero_byte = tmp_path / "zero_byte.csv"
    zero_byte.write_bytes(b"")
    with pytest.raises(ValidationError, match="CSV file is empty"):
        read_csv_headers(zero_byte)


def test_csv_bom_and_whitespace_strip(tmp_path: Path):
    bom_file = tmp_path / "bom.csv"
    # Write with BOM and spaces
    bom_file.write_bytes(b"\xef\xbb\xbf id ,  name  \n1,Alice\n")
    headers = read_csv_headers(bom_file)
    assert headers == ["id", "name"]


def test_prepare_row_null_and_empty_string_handling():
    meta = sa.MetaData()
    table = sa.Table(
        "users",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("required_str", sa.String, nullable=False),
        sa.Column("optional_str", sa.String, nullable=True),
        sa.Column("optional_int", sa.Integer, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("is_admin", sa.Boolean, nullable=True),
    )
    raw_row = {
        "id": "1",
        "required_str": "",
        "optional_str": "",
        "optional_int": "",
        "is_active": "true",
        "is_admin": "0",
    }
    cleaned = prepare_row(raw_row, table)
    assert cleaned["id"] == "1"
    assert cleaned["required_str"] == ""  # Non-nullable string preserves empty string
    assert cleaned["optional_str"] is None  # Nullable string becomes None
    assert cleaned["optional_int"] is None  # Non-string empty becomes None
    assert cleaned["is_active"] is True
    assert cleaned["is_admin"] is False


def test_transactional_rollback_on_invalid_data(tmp_path: Path):
    """Verify that if a batch fails mid-import, the entire transaction rolls back to 0 rows."""
    db_file = tmp_path / "test.db"
    engine = sa.create_engine(f"sqlite:///{db_file}")

    meta = sa.MetaData()
    products = sa.Table(
        "products",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("price", sa.Integer, nullable=False),
    )
    meta.create_all(engine)

    # 4 rows: batch 1 has rows 1 & 2 (valid), batch 2 has row 3 (valid) & row 4 (null price on NOT NULL column)
    csv_file = tmp_path / "products.csv"
    csv_file.write_text(
        "id,title,price\n"
        "101,Keyboard,50\n"
        "102,Mouse,25\n"
        "103,Monitor,200\n"
        "104,Headphones,\n",  # empty price -> None -> fails NOT NULL
        encoding="utf-8",
    )

    with pytest.raises(SQLAlchemyError):
        upload_csv(
            engine=engine,
            schema=None,
            table_name="products",
            csv_path=csv_file,
            batch_size=2,
            exit_on_error=False,
        )

    # Verify rollback: 0 rows in database table
    with engine.connect() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(products)).scalar()
        assert count == 0


def test_upload_csv_happy_path(tmp_path: Path):
    """Verify upload_csv successfully commits valid rows in batches and returns row count."""
    db_file = tmp_path / "happy.db"
    engine = sa.create_engine(f"sqlite:///{db_file}")

    meta = sa.MetaData()
    products = sa.Table(
        "products",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("price", sa.Integer, nullable=False),
    )
    meta.create_all(engine)

    csv_file = tmp_path / "valid_products.csv"
    csv_file.write_text(
        "id,title,price\n"
        "1,Keyboard,50\n"
        "2,Mouse,25\n"
        "3,Monitor,200\n",
        encoding="utf-8",
    )

    rows_inserted, table_ref = upload_csv(
        engine=engine,
        schema=None,
        table_name="products",
        csv_path=csv_file,
        batch_size=2,
        exit_on_error=False,
    )
    assert rows_inserted == 3
    assert table_ref.name == "products"

    with engine.connect() as conn:
        count = conn.execute(sa.select(sa.func.count()).select_from(products)).scalar()
        assert count == 3


def test_normalize_pg_uri():
    assert normalize_pg_uri("postgres://u:p@localhost:5432/db") == "postgresql+psycopg://u:p@localhost:5432/db"
    assert normalize_pg_uri("postgresql://u:p@localhost:5432/db") == "postgresql+psycopg://u:p@localhost:5432/db"
    assert normalize_pg_uri("postgresql+psycopg://u:p@localhost:5432/db") == "postgresql+psycopg://u:p@localhost:5432/db"


def test_get_available_tables():
    engine = sa.create_engine("sqlite:///:memory:")
    meta = sa.MetaData()
    sa.Table("audit_items", meta, sa.Column("id", sa.Integer, primary_key=True))
    meta.create_all(engine)

    insp = sa.inspect(engine)
    tables = get_available_tables(engine, inspector=insp)
    assert any(tbl == "audit_items" for _, tbl in tables)


def test_prepare_row_whitespace_cleaning():
    meta = sa.MetaData()
    table = sa.Table(
        "items",
        meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("description", sa.String, nullable=True),
        sa.Column("amount", sa.Numeric, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=True),
    )
    raw_row = {
        "id": " 42 ",
        "name": "Widget",
        "description": "   ",  # Nullable string with spaces -> None
        "amount": "   ",       # Numeric with spaces -> None
        "is_active": "   ",    # Boolean with spaces -> None
    }
    cleaned = prepare_row(raw_row, table)
    assert cleaned["id"] == "42"
    assert cleaned["name"] == "Widget"
    assert cleaned["amount"] is None
    assert cleaned["is_active"] is None


def test_read_csv_invalid_encoding(tmp_path: Path):
    bad_encoding_file = tmp_path / "bad.csv"
    # Write invalid UTF-8 bytes
    bad_encoding_file.write_bytes(b"\xff\xfe\x00\x00invalid")
    with pytest.raises(ValidationError, match="Cannot read CSV file"):
        read_csv_headers(bad_encoding_file)


def test_scan_local_postgres_tcp_detected():
    with patch("socket.create_connection", return_value=MagicMock()):
        found, host, port = scan_local_postgres()
        assert found is True
        assert host == "127.0.0.1"
        assert port == 5432


def test_scan_local_postgres_not_found(tmp_path: Path):
    with patch("socket.create_connection", side_effect=OSError), \
         patch("pg_csv_loader.discovery.Path.exists", return_value=False):
        found, host, port = scan_local_postgres()
        assert found is False
        assert host is None



