#!/usr/bin/env python3
"""Neon PostgreSQL Database Resetter.

Safely purges all records from the configured Neon PostgreSQL database tables
(people, images, person_images, face_boxes).
Requires explicit user confirmation before truncating any tables.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
from dotenv import load_dotenv

# Configure UTF-8 encoding on Windows to prevent console character artifacts
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent
ENV_PATH = BASE / ".env"
load_dotenv(ENV_PATH)

NEON_DB_URL = os.getenv("NEON_DB_URL", "").strip()

TABLES = ["face_boxes", "person_images", "images", "people"]


def mask_db_url(url: str) -> str:
    """Return DB URL with password masked for safe printing."""
    try:
        parsed = urlparse(url)
        if parsed.password:
            masked_netloc = parsed.netloc.replace(f":{parsed.password}@", ":******@")
            return parsed._replace(netloc=masked_netloc).geturl()
    except Exception:
        pass
    return "Neon PostgreSQL Database"


def get_db_connection():
    if not NEON_DB_URL:
        raise SystemExit("Missing NEON_DB_URL in .env.")
    return psycopg2.connect(NEON_DB_URL)


def count_table_rows(conn, table_name: str) -> int:
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {table_name}")
            row = cur.fetchone()
            return row[0] if row else 0
    except Exception:
        conn.rollback()
        return 0


def reset_neondb():
    print("=" * 65)
    print("NEON POSTGRESQL DATABASE RESET UTILITY")
    print("=" * 65)

    if not NEON_DB_URL:
        print("Error: NEON_DB_URL is not configured in .env", file=sys.stderr)
        sys.exit(1)

    print(f"Target DB     : {mask_db_url(NEON_DB_URL)}")
    print("Connecting to database and scanning tables...\n")

    try:
        conn = get_db_connection()
    except Exception as exc:
        print(f"Error connecting to Neon database: {exc}", file=sys.stderr)
        sys.exit(1)

    # Check which tables exist in public schema
    existing_tables = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            found = {row[0] for row in cur.fetchall()}
            existing_tables = [t for t in TABLES if t in found]
    except Exception as exc:
        print(f"Error querying table schema: {exc}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    if not existing_tables:
        print("No target tables found in database. Nothing to delete.")
        conn.close()
        return

    # Count rows across all target tables
    counts = {}
    total_records = 0
    print("Table Record Counts:")
    print("-" * 35)
    for table in TABLES:
        if table in existing_tables:
            count = count_table_rows(conn, table)
            counts[table] = count
            total_records += count
            print(f"  * {table:<16} : {count:>7,} rows")
        else:
            print(f"  * {table:<16} : (table not found)")
    print("-" * 35)
    print(f"  Total Records    : {total_records:>7,}\n")

    if total_records == 0:
        print("The database is already completely empty. Nothing to delete.")
        conn.close()
        return

    print("!" * 65)
    print(f"WARNING: This will PERMANENTLY DELETE all {total_records:,} record(s)")
    print(f"across tables: {', '.join(existing_tables)}.")
    print("This action cannot be undone!")
    print("!" * 65 + "\n")

    prompt = "To confirm, type 'reset' exactly: "
    try:
        user_input = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        print("\nOperation cancelled.")
        conn.close()
        sys.exit(0)

    if user_input.lower() != "reset":
        print("\nInput did not match 'reset'. Operation CANCELLED.")
        conn.close()
        sys.exit(0)

    print(f"\nConfirmation received. Truncating {len(existing_tables)} table(s)...")

    try:
        with conn.cursor() as cur:
            # Truncate all existing tables in one statement to handle foreign key dependencies cleanly
            tables_clause = ", ".join(f'"{t}"' for t in existing_tables)
            cur.execute(f"TRUNCATE TABLE {tables_clause} RESTART IDENTITY CASCADE;")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"\nError truncating tables: {exc}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Verify zero rows remain
    remaining = 0
    for table in existing_tables:
        remaining += count_table_rows(conn, table)

    conn.close()

    print("\n" + "=" * 65)
    print(f"RESET COMPLETE: Successfully purged {total_records:,} record(s).")
    print(f"Remaining records in database: {remaining}")
    print("=" * 65)


if __name__ == "__main__":
    try:
        reset_neondb()
    except KeyboardInterrupt:
        print("\nOperation cancelled.")
        sys.exit(0)
    except Exception as exc:
        print(f"\nFATAL: {exc}", file=sys.stderr)
        sys.exit(1)
