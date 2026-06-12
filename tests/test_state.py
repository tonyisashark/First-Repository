from kalshi_bot.state import StateStore


def test_kv_round_trip(state: StateStore):
    assert state.kv_get("missing") is None
    state.kv_set("k", 42)
    assert state.kv_get_int("k") == 42
    state.kv_delete("k")
    assert state.kv_get("k") is None


def test_orders_and_attribution(state: StateStore):
    state.record_order("o1", "TICK-A", "arb", "bid", 500_000, 10, "resting",
                       event_ticker="EV", series_ticker="S")
    state.record_order("o2", "TICK-A", "mm", "ask", 600_000, 5, "resting")
    assert state.strategy_of_order("o1") == "arb"
    # latest order wins attribution
    assert state.latest_strategy_by_ticker()["TICK-A"] == "mm"
    state.set_order_status("o1", "canceled")
    assert state.order_row("o1")[6] == "canceled"


def test_fills_are_idempotent(state: StateStore):
    state.record_order("o1", "T", "longshot", "bid", 950_000, 5, "executed")
    assert state.record_fill("f1", "o1", "T", "bid", 5, 950_000, 10_000, True, 123)
    assert not state.record_fill("f1", "o1", "T", "bid", 5, 950_000, 10_000, True, 123)
    assert state.fees_paid_since(0) == 10_000
    # strategy backfilled from the order
    row = state.conn.execute("SELECT strategy FROM fills WHERE fill_id='f1'").fetchone()
    assert row[0] == "longshot"


def test_snapshots(state: StateStore):
    state.save_snapshot(100, 1, 2, 3, 6)
    state.save_snapshot(200, 2, 2, 3, 7)
    assert state.latest_snapshot()[0] == 200
    assert state.snapshots_since(150) == [(200, 7)]


def test_snapshots_scoped_by_mode(state: StateStore):
    state.save_snapshot(100, 0, 0, 0, 1000, mode="paper:demo")
    state.save_snapshot(200, 0, 0, 0, 90, mode="live:prod")
    assert state.snapshots_since(0, "paper:demo") == [(100, 1000)]
    assert state.snapshots_since(0, "live:prod") == [(200, 90)]
    assert state.latest_snapshot("paper:demo")[4] == 1000
    assert state.latest_snapshot("missing:mode") is None
    assert len(state.snapshots_since(0)) == 2      # unfiltered sees both


def test_migration_adds_mode_column_to_old_databases(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE snapshots (ts INTEGER PRIMARY KEY, cash_micro INTEGER,"
                 " mtm_micro INTEGER, resting_micro INTEGER, equity_micro INTEGER,"
                 " note TEXT DEFAULT '')")
    conn.execute("INSERT INTO snapshots VALUES (100, 0, 0, 0, 1000, '')")
    conn.commit()
    conn.close()

    migrated = StateStore(path)
    migrated.save_snapshot(200, 0, 0, 0, 90, mode="live:prod")
    assert migrated.snapshots_since(0, "live:prod") == [(200, 90)]
    assert len(migrated.snapshots_since(0)) == 2   # old row survives, mode=''
    migrated.close()


def test_paper_reset_clears_paper_risk_state_only(state: StateStore):
    state.kv_set("paper:demo/risk_hwm_equity", 999)
    state.kv_set("live:prod/risk_hwm_equity", 111)
    state.save_snapshot(100, 0, 0, 0, 1000, mode="paper:demo")
    state.save_snapshot(200, 0, 0, 0, 90, mode="live:prod")
    state.paper_reset()
    assert state.kv_get("paper:demo/risk_hwm_equity") is None
    assert state.kv_get_int("live:prod/risk_hwm_equity") == 111
    assert state.snapshots_since(0, "paper:demo") == []
    assert state.snapshots_since(0, "live:prod") == [(200, 90)]


def test_paper_tables(state: StateStore):
    state.paper_set_position("T", 5, 100)
    assert state.paper_positions() == {"T": (5, 100)}
    state.paper_set_position("T", 0, 0)
    assert state.paper_positions() == {}

    state.paper_save_order("p1", "T", "bid", 400_000, 10, True, False, "mm")
    assert len(state.paper_orders()) == 1
    state.paper_update_order("p1", 4)
    assert state.paper_orders()[0][4] == 4
    state.paper_update_order("p1", 0)
    assert state.paper_orders() == []

    state.kv_set("paper_cash_micro", 123)
    state.paper_set_position("T", 5, 100)
    state.paper_reset()
    assert state.paper_positions() == {}
    assert state.kv_get("paper_cash_micro") is None
