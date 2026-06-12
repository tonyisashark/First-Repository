from kalshi_bot.fees import fill_fee_micro, per_contract_fee_micro
from kalshi_bot.money import cents


def test_published_examples():
    # 1 contract at 50c, taker: 0.07 * 0.5 * 0.5 = $0.0175 -> rounds up to $0.02
    assert fill_fee_micro(cents(50), 1, 700) == 20_000
    # 100 contracts at 95c: 0.07 * 100 * 0.95 * 0.05 = $0.3325 -> $0.34
    assert fill_fee_micro(cents(95), 100, 700) == 340_000
    # maker quarter rate: 0.0175 * 100 * 0.95 * 0.05 = $0.0831 -> $0.09
    assert fill_fee_micro(cents(95), 100, 175) == 90_000


def test_symmetry_and_bounds():
    assert fill_fee_micro(cents(30), 10, 700) == fill_fee_micro(cents(70), 10, 700)
    assert fill_fee_micro(cents(50), 0, 700) == 0
    assert fill_fee_micro(0, 10, 700) == 0
    assert fill_fee_micro(1_000_000, 10, 700) == 0
    assert fill_fee_micro(-5, 10, 700) == 0


def test_zero_rate_is_free():
    assert fill_fee_micro(cents(50), 1000, 0) == 0


def test_per_contract_unrounded():
    # 0.07 * 0.95 * 0.05 = $0.003325 per contract
    assert abs(per_contract_fee_micro(cents(95), 700) - 3_325) < 1e-6
