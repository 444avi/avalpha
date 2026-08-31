from avalpha import watchlist
from avalpha.db import connect


def _add(conn, ticker="NVDA", weight=10.0):
    watchlist.upsert(
        conn,
        ticker=ticker,
        cik="0001045810",
        legal_name="NVIDIA CORP",
        aliases=["Nvidia"],
        products=["GeForce", "CUDA"],
        executives=["Jensen Huang", "Colette Kress"],
        ir_feed_url="https://nvidianews.nvidia.com/rss",
        ir_feed_status="ok",
        weight=weight,
        shares_outstanding=2_440_000_000,
        enrichment_confidence="high",
    )


def test_add_and_get(tmp_path):
    conn = connect(tmp_path / "t.db")
    _add(conn)
    h = watchlist.get(conn, "NVDA")
    assert h is not None
    assert h.aliases == ["Nvidia"]
    assert h.active is True
    assert watchlist.active(conn)[0].ticker == "NVDA"


def test_remove_deactivates_never_deletes(tmp_path):
    conn = connect(tmp_path / "t.db")
    _add(conn)
    assert watchlist.deactivate(conn, "NVDA") is True
    assert watchlist.active(conn) == []
    h = watchlist.get(conn, "NVDA")
    assert h is not None and h.active is False
    # Deactivating twice reports failure.
    assert watchlist.deactivate(conn, "NVDA") is False


def test_readd_reactivates(tmp_path):
    conn = connect(tmp_path / "t.db")
    _add(conn)
    watchlist.deactivate(conn, "NVDA")
    _add(conn, weight=15.0)
    h = watchlist.get(conn, "NVDA")
    assert h.active is True and h.weight == 15.0
