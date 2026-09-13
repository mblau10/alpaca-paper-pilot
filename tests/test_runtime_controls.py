import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from bot import Alpaca, Config, NY, PAPER_TRADING_URL, PaperPilot, completed_bars


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize('field,value', [('max_notional', float('nan')), ('max_daily_loss', -1), ('scan_seconds', 0), ('cooldown_minutes', 0), ('stop_pct', 1)])
def test_invalid_limits_fail_closed(field, value):
    with pytest.raises(ValueError):
        Config(**{field: value}).validate()


def test_bar_pagination_collects_later_symbols():
    broker = Alpaca(Config())
    broker._request = AsyncMock(side_effect=[{'bars': {'SPY': [{'t': 'one'}]}, 'next_page_token': 'p2'}, {'bars': {'XLE': [{'t': 'two'}]}, 'next_page_token': None}])
    result = run(broker.bars(datetime.now(NY), datetime.now(NY)))
    assert set(result) == {'SPY', 'XLE'}
    assert broker._request.await_count == 2
    run(broker.close())


def test_pagination_loop_fails_closed():
    broker = Alpaca(Config())
    broker._request = AsyncMock(return_value={'bars': {}, 'next_page_token': 'same'})
    with pytest.raises(ValueError, match='repeated'):
        run(broker.bars(datetime.now(NY), datetime.now(NY)))
    run(broker.close())


def test_completed_bars_excludes_forming_bar_and_rejects_stale():
    now = datetime(2026, 9, 14, 10, 30, 15, tzinfo=NY)
    def bar(minutes):
        return {'t': (now.replace(second=0) - timedelta(minutes=minutes)).isoformat(), 'o': 100, 'h': 101, 'l': 99, 'c': 100, 'v': 100}
    start = now.replace(hour=9, minute=30, second=0)
    assert completed_bars([bar(1), bar(0)], start, now) == [bar(1)]
    with pytest.raises(ValueError, match='stale'):
        completed_bars([bar(3)], start, now)


def parent(status='filled'):
    return {'id': 'entry', 'symbol': 'XLE', 'side': 'buy', 'client_order_id': datetime.now(NY).strftime('eli406-%Y%m%d-1'), 'status': status, 'filled_qty': '2', 'filled_avg_price': '100', 'legs': [{'id': 'stop', 'symbol': 'XLE', 'side': 'sell', 'status': 'canceled', 'filled_qty': '0'}]}


def test_partial_canceled_entry_and_standalone_exit_pnl():
    pilot = PaperPilot()
    entry = parent('canceled')
    exit_order = {'id': 'exit', 'symbol': 'XLE', 'side': 'sell', 'client_order_id': 'eli406exit-test', 'status': 'filled', 'filled_qty': '2', 'filled_avg_price': '99'}
    assert pilot._tagged_pnl([entry], [], [entry, exit_order]) == -2
    run(pilot.alpaca.close())


def test_pending_cancel_never_sends_flatten():
    pilot = PaperPilot()
    entry = parent('pending_cancel')
    pilot.alpaca.cancel_order = AsyncMock()
    pilot.alpaca.orders = AsyncMock(return_value=[entry])
    pilot.alpaca.submit_flatten = AsyncMock()
    with pytest.raises(ValueError, match='cancellation'):
        run(pilot._flatten_tagged([entry], [], [entry]))
    pilot.alpaca.submit_flatten.assert_not_awaited()
    run(pilot.alpaca.close())


def test_flatten_reloads_position_and_accounts_for_previous_exit():
    pilot = PaperPilot()
    entry = parent()
    previous = {'id': 'exit', 'symbol': 'XLE', 'side': 'sell', 'client_order_id': 'eli406exit-test', 'status': 'canceled', 'filled_qty': '1', 'filled_avg_price': '99'}
    pilot.alpaca.cancel_order = AsyncMock()
    pilot.alpaca.orders = AsyncMock(return_value=[])
    pilot.alpaca.today_orders = AsyncMock(return_value=[entry, previous])
    pilot.alpaca.positions = AsyncMock(return_value=[{'symbol': 'XLE', 'qty': '1'}])
    pilot.alpaca.submit_flatten = AsyncMock()
    run(pilot._flatten_tagged([entry], [{'symbol': 'XLE', 'qty': '2'}], []))
    args = pilot.alpaca.submit_flatten.await_args.args
    assert args[0:2] == ('XLE', 1)
    run(pilot.alpaca.close())


def test_stable_order_id_across_restart():
    pilot = PaperPilot()
    now = datetime(2026, 9, 14, 10, 30, tzinfo=NY)
    a = pilot._order_payload({'symbol': 'XLE', 'price': 100}, now)
    b = pilot._order_payload({'symbol': 'XLE', 'price': 100}, now + timedelta(seconds=50))
    assert a['client_order_id'] == b['client_order_id']
    assert PAPER_TRADING_URL == 'https://paper-api.alpaca.markets'
    run(pilot.alpaca.close())


def test_early_close_flattens_before_normal_window(monkeypatch):
    import bot
    fixed = datetime(2026, 11, 27, 12, 46, tzinfo=NY)
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(bot, 'datetime', FixedDateTime)
    pilot = PaperPilot(Config(api_key='test', api_secret='test'))
    pilot.alpaca.account = AsyncMock(return_value={'trading_blocked': False})
    pilot.alpaca.clock = AsyncMock(return_value={'is_open': True, 'next_close': fixed.replace(hour=13, minute=0).isoformat()})
    pilot.alpaca.positions = AsyncMock(return_value=[])
    pilot.alpaca.orders = AsyncMock(return_value=[])
    pilot.alpaca.today_orders = AsyncMock(return_value=[])
    pilot._flatten_tagged = AsyncMock()
    run(pilot.scan_once())
    pilot._flatten_tagged.assert_awaited_once()
    run(pilot.alpaca.close())
