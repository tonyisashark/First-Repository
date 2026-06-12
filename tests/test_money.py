from kalshi_bot.money import (
    cents,
    count_to_fp,
    field_count,
    field_micro,
    fp_to_count,
    micro_to_usd_str,
    usd_to_micro,
)


def test_usd_round_trip():
    assert usd_to_micro("0.56") == 560_000
    assert usd_to_micro("0.5600") == 560_000
    assert usd_to_micro("1") == 1_000_000
    assert usd_to_micro("0.005") == 5_000
    assert usd_to_micro(None) is None
    assert usd_to_micro("garbage") is None


def test_micro_to_usd_str():
    assert micro_to_usd_str(560_000) == "0.56"
    assert micro_to_usd_str(1_000_000) == "1.00"
    assert micro_to_usd_str(5_000) == "0.005"
    assert micro_to_usd_str(0) == "0.00"
    assert micro_to_usd_str(990_000) == "0.99"
    # values that fit the 6-decimal wire format survive a round trip
    assert usd_to_micro(micro_to_usd_str(123_456)) == 123_456


def test_cents_helper():
    assert cents(90) == 900_000
    assert cents(0.6) == 6_000


def test_counts():
    assert fp_to_count("10.00") == 10
    assert fp_to_count("-10.00") == -10
    assert fp_to_count("10.99") == 10          # truncate toward zero
    assert fp_to_count("-10.99") == -10
    assert fp_to_count(None) == 0
    assert count_to_fp(7) == "7.00"


def test_field_readers_prefer_modern_variants():
    payload = {"yes_bid_dollars": "0.41", "yes_bid": 99, "volume_fp": "12.00", "volume": 5}
    assert field_micro(payload, "yes_bid") == 410_000
    assert field_count(payload, "volume") == 12
    legacy = {"yes_bid": 41, "volume": 12}
    assert field_micro(legacy, "yes_bid") == 410_000
    assert field_count(legacy, "volume") == 12
    assert field_micro({}, "yes_bid") is None
