import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from invoice_maker.store import Store


def payload(sheet_key: str, month_title: str = "Август 2026") -> dict:
    return {
        "sheet_key": sheet_key,
        "month_title": month_title,
        "date": "31.08.2026",
        "due_date": "10.08.2026",
        "rows": [
            {
                "link": "https://example.test/work",
                "name": "Работа",
                "hours": "2",
                "rate": "2500.00",
                "amount": "5000.00",
            }
        ],
        "hours": "2",
        "total": "5000.00",
        "contract": {"id": 1, "number": "DEMO-2026-07", "date": "01.07.2026"},
        "settings": {"payment_day": 10, "service": "Услуги"},
    }


def renderer(render_payload: dict) -> tuple[bytes, bytes]:
    number = render_payload["number"]
    return f"act-{number}".encode(), f"invoice-{number}".encode()


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "state.sqlite3"
        self.defaults = {
            "payment_day": 10,
            "service": "Разработка",
            "company": {"name": "ООО Заказчик"},
            "initial_contract": {
                "number": "DEMO-2026-07",
                "date": "01.07.2026",
            },
        }
        self.store = Store(self.db_path, self.defaults)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_fresh_database_bootstraps_generic_settings_and_initial_contract(self) -> None:
        self.assertEqual(
            self.store.settings(),
            {
                "payment_day": 10,
                "service": "Разработка",
                "company": {"name": "ООО Заказчик"},
                "next_number": 9,
            },
        )
        self.assertEqual(
            self.store.contracts(),
            [{"id": 1, "number": "DEMO-2026-07", "date": "01.07.2026"}],
        )

    def test_saved_settings_survive_reopening_with_changed_defaults(self) -> None:
        self.store.set_setting("payment_day", 17)
        self.store.set_setting("company", {"name": "ООО Новое"})

        reopened = Store(
            self.db_path,
            {
                "payment_day": 3,
                "company": {"name": "Не применять"},
                "new_default": "Не добавлять при перезапуске",
                "initial_contract": {"number": "Другой", "date": "01.08.2026"},
            },
        )

        self.assertEqual(reopened.settings()["payment_day"], 17)
        self.assertEqual(reopened.settings()["company"], {"name": "ООО Новое"})
        self.assertNotIn("new_default", reopened.settings())
        self.assertEqual(len(reopened.contracts()), 1)

    def test_contract_default_is_latest_date_until_selected_manually(self) -> None:
        older = self.store.add_contract("Старый", "15.06.2026")
        newer = self.store.add_contract("Новый", "20.08.2026")

        self.assertEqual([c["number"] for c in self.store.contracts()], ["Новый", "DEMO-2026-07", "Старый"])
        self.assertEqual(self.store.default_contract(), newer)

        self.store.set_default_contract(older["id"])
        newest = self.store.add_contract("Самый новый", "01.09.2026")
        self.assertEqual(self.store.default_contract(), older)

        self.store.set_default_contract(None)
        self.assertEqual(self.store.default_contract(), newest)

    def test_duplicate_contract_and_unknown_default_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_contract("DEMO-2026-07", "01.07.2026")
        with self.assertRaises(ValueError):
            self.store.set_default_contract(999)

    def test_new_sheets_advance_number_and_repeat_sheet_keeps_number(self) -> None:
        first = self.store.issue(payload("sheet-a"), renderer)
        second = self.store.issue(payload("sheet-b", "Сентябрь 2026"), renderer)
        repeated = self.store.issue(payload("sheet-a", "Август 2026, исправление"), renderer)

        self.assertEqual((first["number"], second["number"], repeated["number"]), (9, 10, 9))
        self.assertNotEqual(first["id"], repeated["id"])
        self.assertEqual(repeated["payload"]["number"], 9)
        self.assertEqual(repeated["act"], b"act-9")
        self.assertEqual(repeated["invoice"], b"invoice-9")
        self.assertEqual(self.store.settings()["next_number"], 11)

    def test_renderer_failure_rolls_back_number_and_new_sheet(self) -> None:
        def failing_renderer(render_payload: dict) -> tuple[bytes, bytes]:
            self.assertEqual(render_payload["number"], 9)
            raise RuntimeError("render failed")

        with self.assertRaisesRegex(RuntimeError, "render failed"):
            self.store.issue(payload("failed-sheet"), failing_renderer)

        self.assertIsNone(self.store.latest_for_sheet("failed-sheet"))
        self.assertEqual(self.store.settings()["next_number"], 9)
        issued = self.store.issue(payload("next-sheet"), renderer)
        self.assertEqual(issued["number"], 9)

    def test_next_number_must_be_integer_positive(self) -> None:
        for invalid in (True, 0, -1, 1.5, "12"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.set_setting("next_number", invalid)

        self.store.set_setting("next_number", 15)
        self.assertEqual(self.store.issue(payload("sheet-b"), renderer)["number"], 15)

    def test_next_number_can_decrease_and_reuse_an_issued_number(self) -> None:
        first = self.store.issue(payload("sheet-a"), renderer)
        self.store.set_setting("next_number", 8)
        lower = self.store.issue(payload("sheet-b"), renderer)
        reused = self.store.issue(payload("sheet-c"), renderer)
        self.assertEqual((first["number"], lower["number"], reused["number"]), (9, 8, 9))
        self.assertEqual(self.store.settings()["next_number"], 10)
        self.assertEqual(len(self.store.archive()), 3)
        self.assertEqual(self.store.get_version(first["id"]), first)
        self.assertEqual(self.store.get_version(reused["id"]), reused)
        self.store.set_setting("next_number", 9)
        self.assertEqual(self.store.issue(payload("sheet-d"), renderer)["number"], 9)

    def test_legacy_unique_number_migration_preserves_archive_and_reopens(self) -> None:
        first = self.store.issue(payload("sheet-a"), renderer)
        # Recreate the old schema, including its implicit UNIQUE index.
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript("""
                CREATE TABLE legacy_sheet_numbers (
                    sheet_key TEXT PRIMARY KEY, number INTEGER NOT NULL UNIQUE
                );
                INSERT INTO legacy_sheet_numbers SELECT * FROM sheet_numbers;
                DROP TABLE sheet_numbers;
                ALTER TABLE legacy_sheet_numbers RENAME TO sheet_numbers;
            """)
        migrated = Store(self.db_path, self.defaults)
        self.assertEqual(migrated.get_version(first["id"]), first)
        migrated.set_setting("next_number", 9)
        second = migrated.issue(payload("sheet-b"), renderer)
        reopened = Store(self.db_path, self.defaults)
        self.assertEqual(second["number"], 9)
        self.assertEqual(reopened.get_version(first["id"]), first)
        self.assertEqual(reopened.get_version(second["id"]), second)
        repeated = reopened.issue(payload("sheet-a"), renderer)
        self.assertEqual(repeated["number"], 9)
        self.assertEqual(reopened.settings()["next_number"], 10)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_credentials_are_not_accepted_as_settings(self) -> None:
        with self.assertRaises(ValueError):
            self.store.set_setting("telegram_bot_token", "secret")

    def test_concurrent_new_sheets_receive_distinct_numbers(self) -> None:
        start = threading.Event()

        def issue(sheet_key: str) -> dict:
            start.wait(timeout=2)
            return self.store.issue(payload(sheet_key), renderer)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(issue, "sheet-a"), executor.submit(issue, "sheet-b")]
            start.set()
            results = [future.result(timeout=10) for future in futures]

        self.assertEqual({result["number"] for result in results}, {9, 10})
        self.assertEqual(len(self.store.archive()), 2)

    def test_archive_returns_latest_per_sheet_and_old_versions_remain_downloadable(self) -> None:
        old_a = self.store.issue(payload("sheet-a", "Август, первая версия"), renderer)
        only_b = self.store.issue(payload("sheet-b", "Сентябрь"), renderer)
        new_a = self.store.issue(payload("sheet-a", "Август, исправлено"), renderer)

        self.assertEqual(
            self.store.latest_for_sheet("sheet-a"),
            {"id": new_a["id"], "number": 9, "payload": new_a["payload"]},
        )
        self.assertEqual(
            self.store.archive(),
            [
                {"id": new_a["id"], "number": 9, "payload": new_a["payload"]},
                {"id": only_b["id"], "number": 10, "payload": only_b["payload"]},
            ],
        )
        self.assertEqual(self.store.archive(limit=1, offset=1), [
            {"id": only_b["id"], "number": 10, "payload": only_b["payload"]}
        ])
        self.assertEqual(
            self.store.get_version(old_a["id"]),
            {
                "id": old_a["id"],
                "number": 9,
                "payload": old_a["payload"],
                "act": b"act-9",
                "invoice": b"invoice-9",
            },
        )

    def test_payload_is_snapshotted_and_must_be_json_serializable(self) -> None:
        source = payload("sheet-a")
        issued = self.store.issue(source, renderer)
        source["rows"][0]["name"] = "Изменено после выпуска"

        self.assertEqual(issued["payload"]["rows"][0]["name"], "Работа")
        with self.assertRaises((TypeError, ValueError)):
            self.store.issue({**payload("bad"), "rows": {object()}}, renderer)


if __name__ == "__main__":
    unittest.main()
