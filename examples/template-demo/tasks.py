"""Повторяемые миграция и засев только учебной базы."""
import argparse
from contextlib import closing
import os
import sqlite3
from pathlib import Path


def apply(action, database):
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as db, db:
        if action == "migrate":
            db.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            db.execute("PRAGMA user_version=1")
        else:
            db.execute("INSERT INTO items(id, name) VALUES (1, 'demo') ON CONFLICT(id) DO NOTHING")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["migrate", "seed"])
    args = parser.parse_args()
    apply(args.action, os.environ.get("DATABASE", "/data/demo.sqlite3"))
    print(args.action + ": OK")
