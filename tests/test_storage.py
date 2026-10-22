from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from customer_support.storage import connect, initialize, seed_demo


def ledger(config):
    with connect(config.database_path) as db:
        return (db.execute("SELECT refunded_minor FROM orders WHERE order_id='order-1001'").fetchone()[0],
                db.execute("SELECT count(*) FROM refunds").fetchone()[0])


@pytest.mark.parametrize("amount", ["0", "-1", "0.001", "1.000", "NaN", "Infinity", "1e2", " 1", 1.5, True])
def test_invalid_money_never_changes_ledger(make_tools, config, amount):
    assert make_tools().issue_refund("order-1001", amount, "test")["code"] == "INVALID_AMOUNT"
    assert ledger(config) == (0, 0)


def test_ownership_and_parameterized_lookup(make_tools, config):
    tools = make_tools()
    assert tools.get_customer("customer-blair")["code"] == "ACCESS_DENIED"
    assert tools.get_order("order-2001")["code"] == "ACCESS_DENIED"
    assert tools.issue_refund("order-2001", "1.00", "test")["code"] == "ACCESS_DENIED"
    assert tools.get_order("' OR 1=1 --")["code"] == "NOT_FOUND"
    assert tools.get_customer("customer-alex")["customer"]["email"].endswith("@example.com")
    assert ledger(config) == (0, 0)


def test_policy_rejection_and_full_balance(make_tools, config):
    tools = make_tools()
    assert tools.issue_refund("order-1001", "100.01", "test")["code"] == "POLICY_LIMIT"
    assert tools.issue_refund("order-1001", "75.01", "test")["code"] == "INSUFFICIENT_BALANCE"
    assert tools.issue_refund("order-1001", "1.00", " ")["code"] == "INVALID_INPUT"
    assert tools.issue_refund("order-1001", "75.00", "test")["remaining_minor"] == 0
    assert tools.issue_refund("order-1001", "0.01", "test")["code"] == "INSUFFICIENT_BALANCE"
    assert ledger(config) == (7500, 1)


def test_eligibility_and_currency_precision(make_tools, config):
    with connect(config.database_path) as db:
        db.execute("UPDATE orders SET status='pending',currency='JPY' WHERE order_id='order-1001'")
    tools = make_tools()
    assert tools.issue_refund("order-1001", "1", "test")["code"] == "INELIGIBLE_ORDER"
    with connect(config.database_path) as db:
        db.execute("UPDATE orders SET status='delivered' WHERE order_id='order-1001'")
    assert tools.issue_refund("order-1001", "1.5", "test")["code"] == "INVALID_AMOUNT"
    assert tools.issue_refund("order-1001", "1", "test")["amount_minor"] == 1


def test_concurrent_refunds_cannot_overdraw(make_tools, config):
    def refund(_):
        return make_tools().issue_refund("order-1001", "50.00", "Concurrent requests")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(refund, range(2)))
    assert sorted(r["code"] for r in responses) == ["INSUFFICIENT_BALANCE", "OK"]
    assert ledger(config) == (5000, 1)


def test_failure_after_insert_rolls_back_both_changes(make_tools, config):
    with connect(config.database_path) as db:
        db.execute("""CREATE TRIGGER fail_balance_update BEFORE UPDATE ON orders
                   BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    response = make_tools().issue_refund("order-1001", "10.00", "test")
    assert response["code"] == "OUTCOME_UNCERTAIN"
    assert ledger(config) == (0, 0)


def test_initialization_and_demo_never_reset_refunds(make_tools, config):
    make_tools().issue_refund("order-1001", "10.00", "test")
    initialize(config.database_path, config.interaction_record_path)
    seed_demo(config.database_path)
    assert ledger(config) == (1000, 1)


def test_recording_failure_prevents_write_or_preserves_committed_outcome(make_tools, config, monkeypatch):
    tools = make_tools()
    original = tools.records.append

    def fail(*args):
        raise sqlite3.OperationalError("test")

    monkeypatch.setattr(tools.records, "append", fail)
    assert tools.issue_refund("order-1001", "1.00", "test")["code"] == "RECORDING_FAILED"
    assert ledger(config) == (0, 0)

    def fail_completion(session, invocation_id, name, phase, payload):
        if phase == "completed":
            raise sqlite3.OperationalError("test")
        original(session, invocation_id, name, phase, payload)

    monkeypatch.setattr(tools.records, "append", fail_completion)
    response = tools.issue_refund("order-1001", "1.00", "test")
    assert response["outcome"] == "committed"
    assert response["recording_error"] == "RECORDING_FAILED"
    assert ledger(config) == (100, 1)
