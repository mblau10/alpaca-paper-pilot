from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bot import Config, PaperPilot, analyze


def rising_bars(count: int = 35):
    bars = []
    for i in range(count):
        close = 100 + i * 0.05
        bars.append({"o": close - 0.02, "h": close + 0.02, "l": close - 0.03, "c": close, "v": 1000})
    bars[-1]["v"] = 2000
    return bars


def test_signal_requires_trend_breakout_and_volume():
    cfg = Config()
    signal = analyze("SPY", rising_bars(), cfg)
    assert signal is not None
    assert signal["symbol"] == "SPY"


def test_signal_rejects_weak_volume():
    cfg = Config()
    bars = rising_bars()
    bars[-1]["v"] = 1000
    assert analyze("SPY", bars, cfg) is None


def test_config_rejects_oversized_notional(monkeypatch):
    monkeypatch.setenv("MAX_NOTIONAL", "200")
    with pytest.raises(ValueError):
        Config().validate()


def test_order_is_paper_bracket_with_bounded_loss():
    cfg = Config()
    pilot = PaperPilot(cfg)
    payload = pilot._order_payload(
        {"symbol": "XLE", "price": 65.0, "volume_ratio": 2.0},
        datetime(2026, 9, 3, 10, 30, tzinfo=ZoneInfo("America/New_York")),
    )
    assert payload["type"] == "limit"
    assert payload["order_class"] == "bracket"
    assert payload["extended_hours"] is False
    assert payload["client_order_id"].startswith("eli406-")
    assert payload["qty"] == "1"
    notional = float(payload["qty"]) * float(payload["limit_price"])
    planned_loss = float(payload["qty"]) * (
        float(payload["limit_price"]) - float(payload["stop_loss"]["stop_price"])
    )
    assert notional <= cfg.max_notional + 0.01
    assert planned_loss <= cfg.max_daily_loss / 2


def test_expensive_etf_cannot_bypass_notional_cap():
    cfg = Config()
    pilot = PaperPilot(cfg)
    with pytest.raises(ValueError):
        pilot._order_payload(
            {"symbol": "QQQ", "price": 700.0, "volume_ratio": 2.0},
            datetime(2026, 9, 3, 10, 30, tzinfo=ZoneInfo("America/New_York")),
        )
