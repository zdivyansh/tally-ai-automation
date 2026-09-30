"""Which invoice files were already handled (stored in the audit database)."""

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Status = Literal["posted", "duplicate", "ignored", "unsupported", "failed"]
FINAL: set[str] = {"posted", "duplicate", "ignored", "unsupported"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS purchase_invoices (
    file_path       TEXT NOT NULL,
    file_mtime      REAL NOT NULL,
    invoice_number  TEXT,
    supplier_gstin  TEXT,
    status          TEXT NOT NULL,
    voucher_number  TEXT,
    message         TEXT,
    at              TEXT NOT NULL,
    PRIMARY KEY (file_path, file_mtime)
);
CREATE INDEX IF NOT EXISTS purchase_invoice_number ON purchase_invoices(supplier_gstin, invoice_number);
"""


class PurchaseState:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def record(
        self,
        file: Path,
        status: Status,
        *,
        invoice_number: str | None = None,
        supplier_gstin: str | None = None,
        voucher_number: str | None = None,
        message: str | None = None,
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT OR REPLACE INTO purchase_invoices
                   (file_path, file_mtime, invoice_number, supplier_gstin, status, voucher_number,
                    message, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(file.resolve()),
                    file.stat().st_mtime,
                    invoice_number,
                    supplier_gstin,
                    status,
                    voucher_number,
                    message,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )

    def is_final(self, file: Path) -> bool:
        """True if this exact file (same path and modification time) needs no more attention."""
        row = self._db.execute(
            "SELECT status FROM purchase_invoices WHERE file_path = ? AND file_mtime = ?",
            (str(file.resolve()), file.stat().st_mtime),
        ).fetchone()
        return row is not None and row[0] in FINAL

    def posted_before(self, supplier_gstin: str, invoice_number: str) -> str | None:
        """Voucher number if this invoice was posted by the agent (from any file)."""
        row = self._db.execute(
            """SELECT voucher_number FROM purchase_invoices
               WHERE supplier_gstin = ? AND invoice_number = ? AND status = 'posted'""",
            (supplier_gstin, invoice_number),
        ).fetchone()
        return (row[0] or "?") if row else None
