#!/usr/bin/env python3
"""Файловый адаптер relational-v1. Подключения задаёт только установка агента."""

import base64
import datetime
import decimal
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid


MAX_BYTES = 2 * 1024 * 1024
MAX_ENVELOPE_BYTES = MAX_BYTES + 64 * 1024
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")


class AdapterError(Exception):
    pass


def require(condition, code="invalid_request"):
    if not condition:
        raise AdapterError(code)


def identifier(value):
    require(isinstance(value, str) and NAME.fullmatch(value), "invalid_identifier")
    return value


def quote(value):
    return '"' + identifier(value) + '"'


def fields(value, required, optional=()):
    require(isinstance(value, dict))
    require(set(required) <= value.keys() <= set(required) | set(optional))


def scalar(value):
    require(value is None or type(value) in (str, int, float, bool), "scalar_required")
    if isinstance(value, str):
        require("\0" not in value, "invalid_string")
    if type(value) is int:
        require(-(2**63) <= value < 2**63, "integer_out_of_range")
    if type(value) is float:
        require(math.isfinite(value), "non_finite_number")
    return value


def json_value(value):
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, float):
        require(math.isfinite(value), "unsupported_result_type")
        return value
    if isinstance(value, decimal.Decimal):
        require(value.is_finite(), "unsupported_result_type")
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"encoding": "base64", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        require(all(isinstance(key, str) for key in value), "unsupported_result_type")
        return {key: json_value(item) for key, item in value.items()}
    raise AdapterError("unsupported_result_type")


def encode(value):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    require(len(raw) <= MAX_BYTES, "result_too_large")
    return raw


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_member")
        result[key] = value
    return result


class Database:
    def __init__(self):
        self.driver = os.environ.get("PORTAL_DATA_DRIVER", "sqlite")
        require(self.driver in ("sqlite", "postgres"), "invalid_configuration")
        self.schema = identifier(os.environ.get("PORTAL_DATA_SCHEMA", "main" if self.driver == "sqlite" else "public"))
        self.allowed_tables = self.allowlist("PORTAL_DATA_ALLOWED_TABLES")
        self.allowed_schemas = self.allowlist("PORTAL_DATA_ALLOWED_SCHEMAS")
        self.connection = None
        self.placeholder = "?" if self.driver == "sqlite" else "%s"

    @staticmethod
    def allowlist(name):
        raw = os.environ.get(name)
        if raw is None:
            return None
        return {item.strip() for item in raw.split(",") if item.strip()}

    def connect(self, inspect):
        if self.driver == "sqlite":
            path = os.environ.get("PORTAL_DATA_SQLITE_PATH")
            require(bool(path), "invalid_configuration")
            # mode=rw/ro исключает случайное создание новой БД.
            uri = Path(path).resolve().as_uri() + ("?mode=ro" if inspect else "?mode=rw")
            self.connection = sqlite3.connect(uri, uri=True, timeout=10)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA trusted_schema=OFF")
            if inspect:
                self.connection.execute("PRAGMA query_only=ON")
            self.connection.execute("BEGIN" if inspect else "BEGIN IMMEDIATE")
        else:
            dsn = os.environ.get("PORTAL_DATA_POSTGRES_DSN")
            require(bool(dsn), "invalid_configuration")
            try:
                import psycopg
            except ImportError:
                raise AdapterError("postgres_driver_unavailable") from None
            self.connection = psycopg.connect(dsn, connect_timeout=10)
            self.connection.execute("BEGIN READ ONLY" if inspect else "BEGIN")
            # Ни одна неявная ссылка на пользовательскую функцию не идёт через search_path.
            self.connection.execute("SET LOCAL search_path TO pg_catalog")

    def table(self, value):
        require(isinstance(value, str), "invalid_identifier")
        parts = value.split(".")
        require(len(parts) in (1, 2), "invalid_identifier")
        schema, name = (self.schema, parts[0]) if len(parts) == 1 else parts
        identifier(schema)
        identifier(name)
        if self.driver == "sqlite":
            require(schema == "main", "schema_not_allowed")
        require(self.allowed_schemas is None or schema in self.allowed_schemas, "schema_not_allowed")
        # В allowlist всегда полное schema.table; сокращение не расширяет права.
        require(self.allowed_tables is None or f"{schema}.{name}" in self.allowed_tables, "table_not_allowed")
        return schema, name, f"{quote(schema)}.{quote(name)}"

    def metadata(self, schema, name):
        db = self.connection
        if self.driver == "sqlite":
            exists = db.execute("SELECT type, sql FROM main.sqlite_schema WHERE name=?", (name,)).fetchone()
            require(exists and exists[0] == "table" and not exists[1].lstrip().upper().startswith("CREATE VIRTUAL"), "table_not_supported")
            require(not name.startswith("sqlite_"), "table_not_supported")
            rows = db.execute(f"PRAGMA main.table_xinfo({quote(name)})").fetchall()
            columns = {row[1]: row[6] == 0 for row in rows}
            primary = tuple(row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5])
            keys = [primary] if primary else []
            for index in db.execute(f"PRAGMA main.index_list({quote(name)})").fetchall():
                if index[2] and not index[4]:
                    # Имя индекса берётся из БД: quote экранирует без ограничения NAME.
                    index_name = '"' + index[1].replace('"', '""') + '"'
                    items = db.execute(f"PRAGMA main.index_info({index_name})").fetchall()
                    if items and all(item[2] is not None for item in items):
                        keys.append(tuple(item[2] for item in items))
            return columns, keys
        exists = db.execute(
            "SELECT c.oid FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s AND c.relkind IN ('r','p')", (schema, name),
        ).fetchone()
        require(exists, "table_not_supported")
        columns = dict(db.execute(
            "SELECT attname, attgenerated='' AND attidentity IN ('','d') FROM pg_catalog.pg_attribute "
            "WHERE attrelid=%s AND attnum>0 AND NOT attisdropped ORDER BY attnum", (exists[0],),
        ).fetchall())
        keys = db.execute(
            "SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_catalog.pg_index i "
            "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(num,ord) "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num "
            "WHERE i.indrelid=%s AND i.indisunique AND i.indisvalid AND i.indisready "
            "AND i.indimmediate AND i.indpred IS NULL AND i.indexprs IS NULL "
            "AND k.ord<=i.indnkeyatts GROUP BY i.indexrelid", (exists[0],),
        ).fetchall()
        return columns, [tuple(row[0]) for row in keys]

    def apply(self, data):
        fields(data, ("tables",))
        require(isinstance(data["tables"], list) and data["tables"], "empty_tables")
        results, seen_tables = [], set()
        for table in data["tables"]:
            fields(table, ("table", "key", "rows"))
            schema, name, sql_table = self.table(table["table"])
            require((schema, name) not in seen_tables, "duplicate_table")
            seen_tables.add((schema, name))
            columns, keys = self.metadata(schema, name)
            key = table["key"]
            require(isinstance(key, list) and key and len(key) == len(set(map(str, key))), "invalid_key")
            for item in key:
                identifier(item)
            require(any(set(key) == set(candidate) for candidate in keys), "key_not_unique")
            require(isinstance(table["rows"], list) and table["rows"], "empty_rows")
            seen_keys = set()
            for row in table["rows"]:
                require(isinstance(row, dict) and row and set(key) <= row.keys(), "invalid_row")
                for column, value in row.items():
                    identifier(column)
                    require(column in columns and columns[column], "column_not_writable")
                    scalar(value)
                require(all(row[column] is not None for column in key), "null_key")
                names = list(row)
                updates = [column for column in names if column not in key]
                conflict = ",".join(map(quote, key))
                action = "DO UPDATE SET " + ",".join(f"{quote(c)}=excluded.{quote(c)}" for c in updates) if updates else "DO NOTHING"
                sql = (f"INSERT INTO {sql_table} ({','.join(map(quote, names))}) VALUES "
                       f"({','.join([self.placeholder] * len(names))}) ON CONFLICT ({conflict}) "
                       f"{action} RETURNING {conflict}")
                actual = self.connection.execute(sql, tuple(row[c] for c in names)).fetchone()
                if actual is None:
                    predicate = " AND ".join(f"{quote(c)}={self.placeholder}" for c in key)
                    actual = self.connection.execute(f"SELECT {conflict} FROM {sql_table} WHERE {predicate}", tuple(row[c] for c in key)).fetchone()
                require(actual is not None, "key_not_returned")
                # RETURNING учитывает приведение типов и collation самой БД.
                fingerprint = encode(json_value(list(actual)))
                require(fingerprint not in seen_keys, "duplicate_row_key")
                seen_keys.add(fingerprint)
            results.append({"table": f"{schema}.{name}", "rows": len(table["rows"])})
        return {"ok": True, "operation": "apply", "tables": results, "rows": sum(t["rows"] for t in results)}

    def inspect(self, query):
        fields(query, ("table", "where"), ("columns", "limit"))
        schema, name, sql_table = self.table(query["table"])
        columns, _ = self.metadata(schema, name)
        selected = query.get("columns", list(columns))
        require(isinstance(selected, list) and selected and all(isinstance(c, str) for c in selected), "invalid_columns")
        require(len(selected) == len(set(selected)), "invalid_columns")
        for column in selected:
            identifier(column)
            require(column in columns, "unknown_column")
        limit = query.get("limit", 100)
        require(type(limit) is int and 1 <= limit <= 1000, "invalid_limit")
        require(isinstance(query["where"], dict), "invalid_where")
        predicates, values = [], []
        for column, value in query["where"].items():
            identifier(column)
            require(column in columns, "unknown_column")
            scalar(value)
            predicates.append(f"{quote(column)} IS NULL" if value is None else f"{quote(column)}={self.placeholder}")
            if value is not None:
                values.append(value)
        sql = f"SELECT {','.join(map(quote, selected))} FROM {sql_table}"
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        sql += f" LIMIT {self.placeholder}"
        values.append(limit + 1)
        cursor = self.connection.execute(sql, tuple(values))
        result = {"ok": True, "operation": "inspect", "table": f"{schema}.{name}", "columns": selected, "rows": [], "truncated": False}
        size = len(encode(result))
        for index, row in enumerate(cursor):
            if index == limit:
                result["truncated"] = True
                break
            item = {column: json_value(value) for column, value in zip(selected, row)}
            size += len(encode(item)) + 1
            require(size <= MAX_BYTES, "result_too_large")
            result["rows"].append(item)
        return result


def main():
    database = None
    result = None
    exit_code = 1
    try:
        input_path = os.environ.get("PORTAL_DATA_INPUT")
        require(input_path and os.environ.get("PORTAL_DATA_OUTPUT"), "missing_protocol_paths")
        with open(input_path, "rb") as source:
            raw = source.read(MAX_ENVELOPE_BYTES + 1)
        require(len(raw) <= MAX_ENVELOPE_BYTES, "request_too_large")
        envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(AdapterError("non_finite_number")))
        fields(envelope, ("protocol", "operation", "project", "stand", "target", "format", "data", "query", "dataset"))
        require(type(envelope["protocol"]) is int and envelope["protocol"] == 1, "unsupported_protocol")
        require(envelope["format"] == "relational-v1", "unsupported_format")
        require(envelope["operation"] in ("apply", "inspect"), "unsupported_operation")
        for key in ("project", "stand", "target"):
            require(isinstance(envelope[key], str) and envelope[key], "invalid_context")
        inspect = envelope["operation"] == "inspect"
        payload = envelope["query"] if inspect else envelope["data"]
        require(len(json.dumps(payload, ensure_ascii=False, allow_nan=False,
                               separators=(",", ":")).encode("utf-8")) <= MAX_BYTES, "request_too_large")
        if inspect:
            require(envelope["data"] is None and envelope["dataset"] is None)
        else:
            require(envelope["query"] is None)
            fields(envelope["dataset"], ("id", "version"))
            require(all(type(v) is int and v > 0 for v in envelope["dataset"].values()))
        database = Database()
        database.connect(inspect)
        result = database.inspect(envelope["query"]) if inspect else database.apply(envelope["data"])
        encode(result)  # Проверить весь результат до COMMIT.
        database.connection.commit()
        exit_code = 0
    except AdapterError as error:
        result = {"ok": False, "error": {"code": str(error)}}
    except (ValueError, UnicodeError, TypeError, OverflowError, RecursionError):
        result = {"ok": False, "error": {"code": "invalid_request"}}
    except Exception:
        # SQL/драйверы включают значения, DSN и имена в сообщения исключений.
        result = {"ok": False, "error": {"code": "database_or_configuration_error"}}
    finally:
        if database and database.connection:
            try:
                if exit_code:
                    database.connection.rollback()
            except Exception:
                pass
            try:
                database.connection.close()
            except Exception:
                pass
    try:
        output = os.environ.get("PORTAL_DATA_OUTPUT")
        require(bool(output), "missing_protocol_paths")
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as destination:
            destination.write(encode(result))
    except Exception:
        return 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
