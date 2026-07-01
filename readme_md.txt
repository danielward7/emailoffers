# Destination2 Deal Finder

A Streamlit app that generates and browses the **Top 40** hotel deals for the
weekly destination email, from three data files. This replaces the weekly
dashboard app; the differences are deliberate:

- **Top 40** (not Top 20), with slots allocated per destination in proportion
  to booking volume (capped at 3 per destination).
- **No geographic exclusion** — every destination is eligible, including the
  Gulf / Middle East.
- **Offers-only shortlist** — the Top 40 is a *deals* list, so only hotels with
  a live offer are eligible for it. Non-offer hotels still appear in the
  Explorer and Regions tabs.
- **Joins by GIATA** — bookings, prices, and offers are matched on GIATA ID; no
  destination name-mapping table.

## Files required

| File | Format | Notes |
|------|--------|-------|
| Bookings (last 6 weeks) | `.xlsx` | `Hotel Giata`, `Booking Date`, `Revenue`, hotel/destination columns. `boardbasis` optional (enables best-**selling** board). |
| Price snapshot | `.xlsx` | Sheet **D2 Hotel Price Snapshot**; `Giata`, `HotelName`, `Star Rating`, `Cheapest Price`, `Cheapest Board`, `Destination`, `Prices Last Refreshed`. |
| Active offers | `.xlsx` / `.csv` | `Giata`, `Tactical Offer Details`, `Offer Type Name`, `Booking To`, `Deal Status`, etc. |

Only the price snapshot is strictly required; bookings and offers can be
omitted (the app flags the gap in Data Quality Notes and continues).

## Tabs

- **Upload & Generate** — upload 1–3 files, click Generate. Everything is
  processed in Python; no API key or external calls.
- **Top 40** — the shortlist as cards grouped by country, or as a sortable
  table, with CSV export.
- **Destination Explorer** — search or pick any destination; see every hotel
  there ranked best-first (price, board, bookings, offer, rec score).
- **Regions** — macro-region roll-up (Caribbean, Europe, Middle East,
  Asia & Indian Ocean, Africa & Red Sea, Other) with stat strips and top deals.
- **Data Quality** — flags missing files/columns, dropped rows, board-basis
  source, and offers ending within 7 days.

## Local setup

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Deploy to Streamlit Community Cloud

1. Push this repo to GitHub.
2. On [share.streamlit.io](https://share.streamlit.io) → **New app**.
3. Select the repo/branch and set **Main file path** to `streamlit_app.py`.
4. Deploy.

## Repo structure

```
├── streamlit_app.py
├── requirements.txt
├── README.md
└── .streamlit/
    └── config.toml       # 50 MB upload limit + navy/gold theme
```

## Keeping the scoring in sync

The scoring engine (everything in `streamlit_app.py` above the
`STREAMLIT UI` banner) is a **verbatim copy** of the `d2-deals-finder` skill's
`scripts/build.py`. When the skill's `build.py` changes, re-paste that section
so the app's numbers stay identical to the skill's output. The UI below the
banner reads only the engine's payload, so it needs no changes as long as the
`build()` signature and payload shape are unchanged.

## Notes

- Offers with a `Booking To` date in the past are treated as expired and
  excluded, so the live-offer count and shortlist reflect the day you run it.
- `boardbasis` is not yet in the bookings export, so the "Board" column falls
  back to the price-snapshot Cheapest Board. Adding that column switches it to
  the best-selling board per hotel automatically — no code change needed.
