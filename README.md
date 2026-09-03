# Alpaca paper-trading pilot

This service scans SPY, QQQ, IWM, SMH, and XLE once per minute during regular U.S. market hours. It is intentionally hard-coded to Alpaca's paper endpoint and cannot place live orders.

The pilot is designed around a simulated $406 account even though Alpaca displays $100,000 of paper buying power. It trades at most one position at a time, caps each entry at $75 notional, stops at six entries per day, and will not enter when any unrelated position or order exists.

## Signal

A paper long entry requires all of the following:

- At least 30 current-session one-minute bars.
- Four of the five tracked ETFs above session VWAP.
- Candidate above VWAP with 9 EMA above 20 EMA.
- Break above the prior 15-minute high without being more than 0.20% extended.
- Latest one-minute volume at least 1.5 times the median of the previous 20 bars.

The order is a day limit bracket with a 0.55% stop and 1.00% target (about 1.8:1 planned reward/risk). No new entry is allowed after 3:00 PM ET; tagged positions are flattened from 3:45 PM ET.

## Required Render secrets

Enter these directly in Render. Never send them in chat or commit them to Git:

- `APCA_API_KEY_ID`
- `APCA_API_SECRET_KEY`

Use paper credentials only. If credentials are missing, the service remains fail-closed and reports `paper_credentials_missing` at `/status`.

## Local test

```bash
python -m pip install -r requirements.txt pytest
pytest -q
uvicorn app:app --reload
```

This is an execution and monitoring scaffold, not a promise of profit. Paper fills omit important live-market effects, and any move to live trading should require a separate review of at least 50 paper trades.
