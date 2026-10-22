# PostgreSQL CSV Loader (`pg-csv-loader`)

A resilient, interactive CLI tool for safely discovering PostgreSQL databases, selecting schemas and tables, validating CSV headers, and performing transactional batch imports using SQLAlchemy 2.0 and `psycopg` (v3).

## Features

- **Isolated & Safe Environment**: Managed exclusively with `uv` (`.venv`).
- **Safe Database Discovery & Docker Support**:
  - Checks `127.0.0.1:5432` with a 0.5s connection timeout and detects Unix domain sockets in `/var/run/postgresql` and `/tmp` (no network port scans, no brute-forcing).
  - Explicit interactive prompts for Username, Password, and Database Name (ideal for Docker containers where users differ from host OS usernames).
  - Flexible connection options: Local Scan, Individual Parameters (Host, Port, User, DB), or URI.
- **Interactive Schema & Table Discovery**:
  - Automatically queries non-system schemas and tables using SQLAlchemy's `inspect()`.
  - Presents interactive arrow-key selection via `questionary`.
- **Strict CSV Header & Schema Validation**:
  - Strips Excel UTF-8 BOM (`utf-8-sig`) and trims whitespace.
  - Rejects unknown columns and duplicate/empty headers.
  - Verifies presence of required `NOT NULL` columns (while permitting omitted columns with defaults, identity, or auto-increment).
- **Atomic Transactional Ingestion**:
  - Streams rows in configurable chunks (default: 2,000) using `engine.begin()`.
  - Formats empty strings to `None` for nullable and non-string columns.
  - Real-time `rich.progress` bar.
  - Complete rollback on error, guaranteeing zero partial rows.
- **Execution Summary**: Displays a formatted `rich` summary table with rows inserted, final table count, and elapsed time.

## Quick Start

### Installation & Environment Setup

```bash
# Clone or enter the project directory
cd pg-csv-loader

# Initialize environment and install dependencies
uv venv
uv sync
```

### Running the CLI

Run the tool interactively:

```bash
uv run pg-csv-loader
```

To view help:

```bash
uv run pg-csv-loader --help
```

### Running Tests

Run the test suite with `pytest`:

```bash
uv run pytest
```
