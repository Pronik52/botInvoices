"""Transactional SQLite storage for settings and issued document versions."""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple, Union


JsonDict = Dict[str, Any]
Renderer = Callable[[JsonDict], Tuple[bytes, bytes]]


class Store:
    """Persist bot state with one short-lived SQLite connection per operation."""

    _TIMEOUT_SECONDS = 5.0
    _MAX_ARCHIVE_PAGE = 100
    _REQUIRED_PAYLOAD_KEYS = {
        "sheet_key",
        "month_title",
        "date",
        "due_date",
        "rows",
        "hours",
        "total",
        "contract",
        "settings",
    }
    _CREDENTIAL_TERMS = {
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "password",
        "privatekey",
        "secret",
        "token",
    }

    def __init__(self, path: Union[str, Path], defaults: Mapping[str, Any]):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap(defaults)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self._TIMEOUT_SECONDS,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _bootstrap(self, defaults: Mapping[str, Any]) -> None:
        if not isinstance(defaults, Mapping):
            raise TypeError("defaults must be a mapping")

        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contracts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    number TEXT NOT NULL,
                    date TEXT NOT NULL,
                    date_sort TEXT NOT NULL,
                    UNIQUE(number, date)
                );
                CREATE TABLE IF NOT EXISTS sheet_numbers (
                    sheet_key TEXT PRIMARY KEY,
                    number INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sheet_key TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    act BLOB NOT NULL,
                    invoice BLOB NOT NULL,
                    FOREIGN KEY(sheet_key) REFERENCES sheet_numbers(sheet_key)
                );
                CREATE INDEX IF NOT EXISTS versions_sheet_id
                    ON versions(sheet_key, id DESC);
                """
            )
            self._migrate_sheet_numbers(connection)
        finally:
            connection.close()

        with self._transaction(immediate=True) as connection:
            initialized = connection.execute(
                "SELECT 1 FROM meta WHERE key = 'initialized'"
            ).fetchone()
            if initialized:
                return

            for key, value in defaults.items():
                if key == "initial_contract":
                    continue
                self._validate_setting_key(key)
                self._ensure_no_credentials(value)
                connection.execute(
                    "INSERT INTO settings(key, value_json) VALUES (?, ?)",
                    (key, self._json_dump(value)),
                )
            if "next_number" not in defaults:
                connection.execute(
                    "INSERT INTO settings(key, value_json) VALUES ('next_number', '9')"
                )
            else:
                self._validate_next_number(defaults["next_number"])

            initial_contract = defaults.get("initial_contract")
            if initial_contract is not None:
                if not isinstance(initial_contract, Mapping):
                    raise ValueError("initial_contract must be a mapping")
                number = initial_contract.get("number")
                date = initial_contract.get("date")
                self._validate_contract(number, date)
                connection.execute(
                    "INSERT INTO contracts(number, date, date_sort) VALUES (?, ?, ?)",
                    (number, date, self._date_sort(date)),
                )
            connection.execute(
                "INSERT INTO meta(key, value) VALUES ('initialized', '1')"
            )

    @staticmethod
    def _migrate_sheet_numbers(connection: sqlite3.Connection) -> None:
        # SQLite не умеет удалять UNIQUE из определения столбца.
        # Пересоздаём только таблицу номеров; архив ссылается на прежние sheet_key.
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            indexes = connection.execute("PRAGMA index_list(sheet_numbers)").fetchall()
            unique_number = any(
                index["unique"] and
                [row["name"] for row in connection.execute(
                    "SELECT name FROM pragma_index_info(?)", (index["name"],)
                )] == ["number"]
                for index in indexes
            )
            if unique_number:
                connection.execute("""
                    CREATE TABLE sheet_numbers_new (
                        sheet_key TEXT PRIMARY KEY, number INTEGER NOT NULL
                    )
                """)
                connection.execute("INSERT INTO sheet_numbers_new SELECT sheet_key, number FROM sheet_numbers")
                connection.execute("DROP TABLE sheet_numbers")
                connection.execute("ALTER TABLE sheet_numbers_new RENAME TO sheet_numbers")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise sqlite3.IntegrityError("Number migration violated archive references")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _json_dump(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def _validate_setting_key(cls, key: Any) -> None:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("setting key must be a non-empty string")
        cls._reject_credential_key(key)

    @classmethod
    def _reject_credential_key(cls, key: str) -> None:
        words = [word for word in re.split(r"[^a-z0-9]+", key.lower()) if word]
        compact = "".join(words)
        if any(word in cls._CREDENTIAL_TERMS for word in words) or compact in cls._CREDENTIAL_TERMS:
            raise ValueError("credentials must not be stored in settings")

    @classmethod
    def _ensure_no_credentials(cls, value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if isinstance(key, str):
                    cls._reject_credential_key(key)
                cls._ensure_no_credentials(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                cls._ensure_no_credentials(nested)

    @staticmethod
    def _validate_next_number(value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("next_number must be a positive integer")

    @staticmethod
    def _validate_contract(number: Any, date: Any) -> None:
        if not isinstance(number, str) or not number.strip():
            raise ValueError("contract number must be a non-empty string")
        if not isinstance(date, str) or not date.strip():
            raise ValueError("contract date must be a non-empty string")

    @staticmethod
    def _date_sort(value: str) -> str:
        for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, date_format).date().isoformat()
            except ValueError:
                pass
        raise ValueError("contract date must use DD.MM.YYYY or YYYY-MM-DD")

    @staticmethod
    def _contract(row: sqlite3.Row) -> JsonDict:
        return {"id": row["id"], "number": row["number"], "date": row["date"]}

    @staticmethod
    def _version_metadata(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "number": row["number"],
            "payload": json.loads(row["payload_json"]),
        }

    def settings(self) -> JsonDict:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT key, value_json FROM settings").fetchall()
            return {row["key"]: json.loads(row["value_json"]) for row in rows}
        finally:
            connection.close()

    def set_setting(self, key: str, value: Any) -> None:
        self._validate_setting_key(key)
        self._ensure_no_credentials(value)
        encoded = self._json_dump(value)
        with self._transaction(immediate=True) as connection:
            if key == "next_number":
                self._validate_next_number(value)
            connection.execute(
                """
                INSERT INTO settings(key, value_json) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                """,
                (key, encoded),
            )

    def contracts(self) -> List[JsonDict]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, number, date FROM contracts ORDER BY date_sort DESC, id DESC"
            ).fetchall()
            return [self._contract(row) for row in rows]
        finally:
            connection.close()

    def add_contract(self, number: str, date: str) -> JsonDict:
        self._validate_contract(number, date)
        sort_key = self._date_sort(date)
        try:
            with self._transaction(immediate=True) as connection:
                cursor = connection.execute(
                    "INSERT INTO contracts(number, date, date_sort) VALUES (?, ?, ?)",
                    (number, date, sort_key),
                )
                contract_id = cursor.lastrowid
        except sqlite3.IntegrityError as error:
            raise ValueError("a contract with this number and date already exists") from error
        return {"id": contract_id, "number": number, "date": date}

    def default_contract(self) -> Optional[JsonDict]:
        connection = self._connect()
        try:
            selected = connection.execute(
                "SELECT value FROM meta WHERE key = 'default_contract_id'"
            ).fetchone()
            if selected is not None:
                row = connection.execute(
                    "SELECT id, number, date FROM contracts WHERE id = ?",
                    (int(selected["value"]),),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT id, number, date FROM contracts
                    ORDER BY date_sort DESC, id DESC LIMIT 1
                    """
                ).fetchone()
            return self._contract(row) if row is not None else None
        finally:
            connection.close()

    def set_default_contract(self, id_or_none: Optional[int]) -> None:
        if id_or_none is not None and (
            isinstance(id_or_none, bool) or not isinstance(id_or_none, int)
        ):
            raise ValueError("contract id must be an integer or None")
        with self._transaction(immediate=True) as connection:
            if id_or_none is None:
                connection.execute("DELETE FROM meta WHERE key = 'default_contract_id'")
                return
            exists = connection.execute(
                "SELECT 1 FROM contracts WHERE id = ?", (id_or_none,)
            ).fetchone()
            if exists is None:
                raise ValueError("unknown contract id")
            connection.execute(
                """
                INSERT INTO meta(key, value) VALUES ('default_contract_id', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(id_or_none),),
            )

    def issue(self, payload: Mapping[str, Any], renderer: Renderer) -> JsonDict:
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        missing = self._REQUIRED_PAYLOAD_KEYS.difference(payload)
        if missing:
            raise ValueError("payload is missing required fields: " + ", ".join(sorted(missing)))
        sheet_key = payload.get("sheet_key")
        if not isinstance(sheet_key, str) or not sheet_key:
            raise ValueError("sheet_key must be a non-empty string")

        self._ensure_no_credentials(payload)
        snapshot = json.loads(self._json_dump(payload))
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT number FROM sheet_numbers WHERE sheet_key = ?", (sheet_key,)
            ).fetchone()
            if row is None:
                setting = connection.execute(
                    "SELECT value_json FROM settings WHERE key = 'next_number'"
                ).fetchone()
                number = json.loads(setting["value_json"]) if setting is not None else 9
                self._validate_next_number(number)
                connection.execute(
                    "INSERT INTO sheet_numbers(sheet_key, number) VALUES (?, ?)",
                    (sheet_key, number),
                )
                connection.execute(
                    """
                    INSERT INTO settings(key, value_json) VALUES ('next_number', ?)
                    ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                    """,
                    (str(number + 1),),
                )
            else:
                number = row["number"]

            snapshot["number"] = number
            payload_json = self._json_dump(snapshot)
            rendered = renderer(json.loads(payload_json))
            if not isinstance(rendered, tuple) or len(rendered) != 2:
                raise TypeError("renderer must return an (act_bytes, invoice_bytes) tuple")
            act, invoice = rendered
            if not isinstance(act, (bytes, bytearray)) or not isinstance(invoice, (bytes, bytearray)):
                raise TypeError("renderer outputs must be bytes")
            act = bytes(act)
            invoice = bytes(invoice)
            cursor = connection.execute(
                """
                INSERT INTO versions(sheet_key, number, payload_json, act, invoice)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sheet_key, number, payload_json, act, invoice),
            )
            version_id = cursor.lastrowid

        return {
            "id": version_id,
            "number": number,
            "payload": json.loads(payload_json),
            "act": act,
            "invoice": invoice,
        }

    def latest_for_sheet(self, sheet_key: str) -> Optional[JsonDict]:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT id, number, payload_json FROM versions
                WHERE sheet_key = ? ORDER BY id DESC LIMIT 1
                """,
                (sheet_key,),
            ).fetchone()
            return self._version_metadata(row) if row is not None else None
        finally:
            connection.close()

    def archive(self, limit: int = 30, offset: int = 0) -> List[JsonDict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= self._MAX_ARCHIVE_PAGE:
            raise ValueError("limit must be an integer from 0 to 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT current.id, current.number, current.payload_json
                FROM versions AS current
                WHERE current.id = (
                    SELECT MAX(candidate.id)
                    FROM versions AS candidate
                    WHERE candidate.sheet_key = current.sheet_key
                )
                ORDER BY current.id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            return [self._version_metadata(row) for row in rows]
        finally:
            connection.close()

    def get_version(self, version_id: int) -> Optional[JsonDict]:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT id, number, payload_json, act, invoice
                FROM versions WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                return None
            result = self._version_metadata(row)
            result["act"] = bytes(row["act"])
            result["invoice"] = bytes(row["invoice"])
            return result
        finally:
            connection.close()
