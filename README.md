# invest-data

Free daily static feed for [投資寶典](https://invest-book-657.web.app).

GitHub Actions runs twice a day (14:30 and 22:30 Taipei) and writes JSON under `docs/data/`:

- `data/nav/<code>.json`, `data/hold/<code>.json`, `data/meta.json`, `data/latest.json` — Fubon Life investment-linked NAV and holdings
- `data/prices/tw.json`, `data/prices/us.json`, `data/prices/meta.json` — TWSE, TPEx, and US watchlist closes

Published at https://skw5g86wp7-maker.github.io/invest-data/
