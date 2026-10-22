"""Parameterized local records, atomic simulated refunds, and interaction traces."""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4


CURRENCY_DIGITS = {"USD": 2, "EUR": 2, "GBP": 2, "JPY": 0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    address_json TEXT NOT NULL,
    preferences_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    items_json TEXT NOT NULL,
    total_minor INTEGER NOT NULL CHECK(total_minor >= 0),
    currency TEXT NOT NULL CHECK(currency IN ('USD','EUR','GBP','JPY')),
    status TEXT NOT NULL,
    refunded_minor INTEGER NOT NULL DEFAULT 0
        CHECK(refunded_minor >= 0 AND refunded_minor <= total_minor)
);
CREATE TABLE IF NOT EXISTS refunds (
    refund_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    currency TEXT NOT NULL,
    reason TEXT NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    interaction_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refunds_order ON refunds(order_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def result(code: str = "OK", message: str = "", **data) -> dict:
    return {"ok": code in {"OK", "EMPTY", "PARTIAL"}, "code": code,
            "message": message, **data}


def money(minor: int, currency: str) -> str:
    digits = CURRENCY_DIGITS[currency]
    return f"{Decimal(minor).scaleb(-digits):.{digits}f}"


@contextmanager
def connect(path: str):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(database_path: str, interaction_record_path: str) -> None:
    for path in (database_path, interaction_record_path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as db:
        db.executescript(SCHEMA)
    with connect(interaction_record_path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS tool_events (
            event_id INTEGER PRIMARY KEY,
            invocation_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            interaction_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            phase TEXT NOT NULL CHECK(phase IN ('started','completed')),
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS events_interaction ON tool_events(interaction_id)")


def seed_demo(path: str) -> None:
    """Insert fictional records only; never reset an existing order or refund."""
    with connect(path) as db:
        for customer_id, name in (("customer-alex", "Alex Example"),
                                  ("customer-blair", "Blair Example")):
            db.execute("INSERT OR IGNORE INTO customers VALUES (?, ?, ?, ?, ?, ?)", (
                customer_id, name, f"{customer_id}@example.com", "+1-202-555-0100",
                json.dumps({"line1": "123 Example Street", "city": "Example City",
                            "postal_code": "00000", "country": "US"}),
                json.dumps({"contact": "email", "language": "en"}),
            ))
        for order_id, customer_id in (("order-1001", "customer-alex"),
                                      ("order-2001", "customer-blair")):
            db.execute("""INSERT OR IGNORE INTO orders
                (order_id,customer_id,items_json,total_minor,currency,status)
                VALUES (?, ?, ?, ?, ?, ?)""", (
                order_id, customer_id,
                json.dumps([{"sku": "DEMO-HEADSET", "name": "Example headset",
                             "quantity": 1, "unit_price_minor": 7500}]),
                7500, "USD", "delivered",
            ))


class Store:
    def __init__(self, path: str):
        self.path = path

    def get_customer(self, customer_id: str, active_customer: str) -> dict:
        if customer_id != active_customer:
            return result("ACCESS_DENIED", "Customer is outside the active session.")
        with connect(self.path) as db:
            row = db.execute("SELECT * FROM customers WHERE customer_id = ?",
                             (customer_id,)).fetchone()
        if row is None:
            return result("NOT_FOUND", "Customer not found.")
        customer = dict(row)
        customer["address"] = json.loads(customer.pop("address_json"))
        customer["support_preferences"] = json.loads(customer.pop("preferences_json"))
        return result(customer=customer)

    @staticmethod
    def _owned_order(db, order_id: str, active_customer: str):
        row = db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None:
            return None, result("NOT_FOUND", "Order not found.")
        if row["customer_id"] != active_customer:
            return None, result("ACCESS_DENIED", "Order is outside the active session.")
        return row, None

    def get_order(self, order_id: str, active_customer: str) -> dict:
        with connect(self.path) as db:
            # One read transaction provides a consistent balance and refund history.
            db.execute("BEGIN")
            row, error = self._owned_order(db, order_id, active_customer)
            if error:
                return error
            order = dict(row)
            order["items"] = json.loads(order.pop("items_json"))
            order["total"] = money(order["total_minor"], order["currency"])
            order["remaining_minor"] = order["total_minor"] - order["refunded_minor"]
            order["refund_history"] = [dict(r) for r in db.execute(
                "SELECT * FROM refunds WHERE order_id = ? ORDER BY created_at, refund_id",
                (order_id,))]
            customer = db.execute("SELECT name,address_json FROM customers WHERE customer_id = ?",
                                  (active_customer,)).fetchone()
            order["shipping_name"] = customer["name"]
            order["shipping_address"] = json.loads(customer["address_json"])
        return result(order=order)

    def issue_refund(self, order_id, amount, reason, session, invocation_id, config) -> dict:
        # Strings avoid floating-point ambiguity and prohibit exponent/NaN/Infinity inputs.
        if (not isinstance(amount, str) or len(amount) > 32 or
                not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", amount)):
            return result("INVALID_AMOUNT", "Use a positive decimal string, such as '10.00'.")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            return result("INVALID_INPUT", "A reason of 1–1000 characters is required.")
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row, error = self._owned_order(db, order_id, session.customer_id)
            if error:
                return error
            if row["status"] not in config.refund_eligible_statuses:
                return result("INELIGIBLE_ORDER", "Order status is not eligible for a refund.")
            digits = CURRENCY_DIGITS[row["currency"]]
            try:
                value = Decimal(amount)
                minor = value.scaleb(digits)
                if value <= 0 or -value.as_tuple().exponent > digits:
                    return result("INVALID_AMOUNT", f"Amount must be positive with at most {digits} decimals.")
                minor = int(minor)
            except (InvalidOperation, ValueError, OverflowError):
                return result("INVALID_AMOUNT", "Invalid decimal amount.")
            if minor > config.refund_max_minor:
                return result("POLICY_LIMIT", "Amount exceeds the configured per-refund limit.")
            remaining = row["total_minor"] - row["refunded_minor"]
            if minor > remaining:
                return result("INSUFFICIENT_BALANCE", "Amount exceeds the remaining refundable balance.")
            refund_id = str(uuid4())
            db.execute("INSERT INTO refunds VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                refund_id, order_id, minor, row["currency"], reason.strip(),
                session.customer_id, session.interaction_id, invocation_id, utc_now(),
            ))
            db.execute("UPDATE orders SET refunded_minor = refunded_minor + ? WHERE order_id = ?",
                       (minor, order_id))
            response = result(refund_id=refund_id, order_id=order_id, amount_minor=minor,
                              amount=money(minor, row["currency"]), currency=row["currency"],
                              refunded_minor=row["refunded_minor"] + minor,
                              remaining_minor=remaining - minor,
                              remaining_balance=money(remaining - minor, row["currency"]),
                              outcome="committed", simulated=True)
        # Only return success after the connection context commits.
        return response


class InteractionRecords:
    def __init__(self, path: str):
        self.path = path

    def append(self, session, invocation_id, tool_name, phase, payload) -> None:
        with connect(self.path) as db:
            db.execute("""INSERT INTO tool_events
                (invocation_id,customer_id,interaction_id,tool_name,phase,created_at,payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (
                invocation_id, session.customer_id, session.interaction_id,
                tool_name, phase, utc_now(), json.dumps(payload, ensure_ascii=False),
            ))
