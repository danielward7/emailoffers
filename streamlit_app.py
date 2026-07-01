"""Destination2 Deal Finder — single-file Streamlit app.

A drop-in replacement for the weekly-dashboard app. The scoring engine
(everything down to `build`) is copied verbatim from the d2-deals-finder
skill's scripts/build.py; the Streamlit UI is below. Run with:

    pip install -r requirements.txt
    streamlit run streamlit_app.py
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

COLS = {
    "booked_date": ["bookingdate", "bookeddate", "booked"],
    "giata_b": ["hotelgiata", "giata"],
    "hotel_b": ["hotelname", "hotelnames", "hotel"],
    "revenue": ["revenue", "kpirevenue"],
    "board_b": ["boardbasis", "boardtype", "mealplan", "board"],
    "name": ["hotelname", "name"],
    "giata": ["giata", "giataid"],
    "region": ["destination", "name1", "region"],
    "stars": ["starrating", "stars"],
    "price": ["cheapestprice", "price"],
    "cheapboard": ["cheapestboard", "board"],
    "pricedate": ["cheapestpricedate", "pricedate"],
    "refreshed": ["priceslastrefreshed", "lastrefreshed"],
    "giata_o": ["giata", "giataid"],
    "offer_details": ["tacticalofferdetails", "offerdetails", "details"],
    "offer_type": ["offertypename", "offertype", "type"],
    "book_to": ["bookingto", "bookto"],
    "travel_to": ["travelto"],
    "deal_status": ["dealstatus", "status"],
}

BOARD_LABELS = {"ai": "All Inclusive", "uai": "Ultra All Inclusive", "hb": "Half Board",
                "bb": "Bed & Breakfast", "ro": "Room Only", "fb": "Full Board",
                "sc": "Self Catering", "si": "Self Catering"}

MACRO_MIDDLE_EAST = {"unitedarabemirates", "oman", "qatar", "bahrain", "jordan",
                     "saudiarabia", "kuwait"}
MACRO_ASIA = {"thailand", "maldives", "mauritius", "seychelles", "srilanka",
              "india", "indonesia", "malaysia", "singapore", "hongkong",
              "vietnam", "japan", "indianocean"}
MACRO_CARIBBEAN = {"barbados", "saintlucia", "stlucia", "grenada", "jamaica",
                   "bahamas", "antiguaandbarbuda", "antigua", "aruba",
                   "netherlandsantilles", "turksandcaicosislands",
                   "saintkittsandnevis", "trinidadandtobago", "tobago",
                   "dominicanrepublic", "mexico", "cuba", "caribbean", "curacao"}
MACRO_EUROPE = {"greece", "spain", "portugal", "italy", "cyprus", "croatia",
                "malta", "turkey", "bulgaria", "france", "unitedkingdom",
                "canaries", "balearics"}
MACRO_AFRICA = {"egypt", "morocco", "tunisia", "southafrica", "capeverde",
                "capeverdeislands", "tanzania", "kenya", "gambia", "senegal"}
MACRO_ORDER = ["Caribbean", "Europe", "Middle East", "Asia & Indian Ocean",
               "Africa & Red Sea", "Other"]

# Destination-relevant perks: offers carrying these score higher in that area.
PREFERRED_PERKS_MACRO = {
    "Middle East": {"room upgrade", "transfers", "drinks"},
    "Asia & Indian Ocean": {"transfers", "room upgrade"},
    "Caribbean": {"free nights", "board upgrade", "resort credit"},
    "Africa & Red Sea": {"board upgrade", "drinks", "kids free"},
    "Europe": {"free nights", "board upgrade", "kids free"},
}
PREFERRED_DEFAULT = {"free nights", "board upgrade", "discount"}


def preferred_perks(region, macro):
    if "maldives" in norm(region):
        return {"transfers", "room upgrade"}
    return PREFERRED_PERKS_MACRO.get(macro, PREFERRED_DEFAULT)


OFFER_TAG_RULES = [
    ("free nights", [r"free night", r"nights free", r"stay \d+ pay", r"bonus night"]),
    ("discount", [r"discount", r"% off", r"\bsave\b", r"reduction", r"rate break"]),
    ("board upgrade", [r"board upgrade", r"upgrade.*board", r"free.*board", r"board.*free"]),
    ("room upgrade", [r"room upgrade", r"upgrade.*room", r"suite upgrade", r"free.*upgrade"]),
    ("kids free", [r"kids.*free", r"child.*free", r"children.*free", r"free.*child"]),
    ("drinks", [r"drink", r"beverage", r"\bbar\b", r"all.?inclusive upgrade"]),
    ("transfers", [r"transfer"]),
    ("resort credit", [r"resort credit", r"hotel credit", r"\bspend\b", r"credit"]),
]
TAG_BASE = {"free nights": 15, "discount": 8, "board upgrade": 10, "room upgrade": 10,
            "kids free": 6, "drinks": 8, "transfers": 8, "resort credit": 10}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def giata_str(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return s[:-2] if s.endswith(".0") else s


def find_col(df, key):
    wanted = COLS[key]
    lut = {norm(c): c for c in df.columns}
    for w in wanted:
        if norm(w) in lut:
            return lut[norm(w)]
    for nc, real in lut.items():
        for w in wanted:
            if norm(w) and norm(w) in nc:
                return real
    return None


def board_label(raw):
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    return BOARD_LABELS.get(norm(raw), str(raw).strip())


def country_from_region(region):
    return region.split(" - ")[-1].strip() if " - " in region else region.strip()


def macro_region(region, country):
    nr, c = norm(region), norm(country)
    if "caribbean" in nr:
        return "Caribbean"
    if "indianocean" in nr or "zanzibar" in nr:
        return "Asia & Indian Ocean"
    if c in MACRO_MIDDLE_EAST:
        return "Middle East"
    if c in MACRO_AFRICA:
        return "Africa & Red Sea"
    if c in MACRO_ASIA:
        return "Asia & Indian Ocean"
    if c in MACRO_CARIBBEAN:
        return "Caribbean"
    if c in MACRO_EUROPE:
        return "Europe"
    return "Other"


def offer_tags(text):
    t = str(text).lower()
    return [tag for tag, pats in OFFER_TAG_RULES if any(re.search(p, t) for p in pats)]


def clean_text(s):
    s = str(s).strip().strip('"').replace('""', '"').strip()
    return re.sub(r"\s+", " ", s)


def score_offer(offer, region, macro):
    """0-100 offer quality: discount % + perks + destination relevance."""
    if not offer:
        return 0
    tags = offer["tags"]
    text = (offer["summary"] + " " + offer["type"])
    pcts = [int(x) for x in re.findall(r"(\d{1,3})\s*%", text)]
    pct = max([p for p in pcts if p <= 100], default=0)
    pct_score = min(40, pct * 0.6)
    tag_base = min(35, sum(TAG_BASE.get(t, 4) for t in tags))
    pref = preferred_perks(region, macro)
    relevance = min(30, 15 * len(set(tags) & pref))
    return int(min(100, pct_score + tag_base + relevance))


# ---------------------------------------------------------------------------
def load_prices(path, notes):
    if not path:
        notes.append("**Price snapshot missing** — no hotel catalogue; cannot build app.")
        return pd.DataFrame(), ""
    try:
        xl = pd.ExcelFile(path)
        sheet = next((s for s in xl.sheet_names if "price snapshot" in s.lower()), xl.sheet_names[0])
        df = xl.parse(sheet)
    except Exception as e:
        notes.append(f"Price snapshot failed to load: {e}")
        return pd.DataFrame(), ""
    c = {k: find_col(df, k) for k in ("name", "region", "giata", "stars",
                                      "price", "cheapboard", "pricedate", "refreshed")}
    if any(not c[k] for k in ("name", "region", "price")):
        notes.append("Price snapshot missing essential columns (name/region/price); cannot proceed.")
        return pd.DataFrame(), ""
    out = pd.DataFrame({
        "hotel": df[c["name"]].fillna("").astype(str).str.strip(),
        "region": df[c["region"]].fillna("").astype(str).str.strip(),
        "giata": df[c["giata"]].map(giata_str) if c["giata"] else "",
        "stars": pd.to_numeric(df[c["stars"]], errors="coerce") if c["stars"] else 0,
        "price": pd.to_numeric(df[c["price"]], errors="coerce"),
        "cheapboard": df[c["cheapboard"]].fillna("").astype(str).str.strip() if c["cheapboard"] else "",
    })
    out = out[out["price"] > 0].copy()
    # Drop rows with no destination (cannot be placed in a region) and flag them.
    blank_region = out["region"].isin(["", "nan", "NaN", "<NA>"])
    if blank_region.any():
        dropped_names = ", ".join(sorted(out.loc[blank_region, "hotel"].unique())[:5])
        notes.append(f"Dropped {int(blank_region.sum())} price-snapshot row(s) with a blank Destination "
                     f"(cannot place in a region): {dropped_names}.")
        out = out[~blank_region].copy()
    out["country"] = out["region"].map(country_from_region)
    out = out.sort_values(["region", "price"])
    dropped = len(out) - len(out.drop_duplicates(subset=["hotel", "region"]))
    out = out.drop_duplicates(subset=["hotel", "region"], keep="first")
    if dropped:
        notes.append(f"Removed {dropped} duplicate hotel rows from price snapshot (kept cheapest).")
    refreshed = ""
    if c["refreshed"]:
        r = pd.to_datetime(df[c["refreshed"]], errors="coerce").max()
        if pd.notna(r):
            refreshed = r.strftime("%d %b %Y")
    return out, refreshed


def load_bookings(path, notes):
    if not path:
        notes.append("**Bookings export missing** — no demand data.")
        return pd.DataFrame(), False
    try:
        df = pd.ExcelFile(path).parse(0)
    except Exception as e:
        notes.append(f"Bookings export failed to load: {e}")
        return pd.DataFrame(), False
    c = {k: find_col(df, k) for k in ("booked_date", "giata_b", "revenue", "board_b")}
    if not c["giata_b"]:
        notes.append("Bookings has no GIATA column — demand omitted.")
        return pd.DataFrame(), False
    out = pd.DataFrame({
        "giata": df[c["giata_b"]].map(giata_str),
        "date": pd.to_datetime(df[c["booked_date"]], errors="coerce") if c["booked_date"] else pd.NaT,
        "revenue": pd.to_numeric(df[c["revenue"]], errors="coerce").fillna(0) if c["revenue"] else 0,
        "board": df[c["board_b"]] if c["board_b"] else None,
    })
    out = out[out["giata"] != ""].copy()
    has_board = c["board_b"] is not None and out["board"].notna().any()
    if not has_board:
        notes.append("Bookings has no usable `boardbasis` column yet — best board falls back to the "
                     "price-snapshot Cheapest Board. (Add `boardbasis` to enable best-selling board.)")
    return out, has_board


def load_offers(path, notes, today):
    if not path:
        notes.append("Active offers file missing — no offers applied.")
        return {}
    try:
        df = pd.read_excel(path) if str(path).lower().endswith((".xlsx", ".xls")) else pd.read_csv(path)
    except Exception as e:
        notes.append(f"Offers file failed to load: {e}")
        return {}
    c = {k: find_col(df, k) for k in ("giata_o", "offer_details", "offer_type",
                                      "book_to", "travel_to", "deal_status")}
    if not c["giata_o"]:
        notes.append("Offers file has no GIATA column — offers not applied.")
        return {}
    idx, expired = {}, 0
    for _, o in df.iterrows():
        gi = giata_str(o[c["giata_o"]])
        if not gi:
            continue
        if c["deal_status"] and str(o[c["deal_status"]]).strip().lower() not in ("active", "", "nan"):
            continue
        bt = pd.to_datetime(o[c["book_to"]], errors="coerce") if c["book_to"] else pd.NaT
        if pd.notna(bt) and bt.date() < today:
            expired += 1
            continue
        summary = clean_text(o[c["offer_details"]]) if c["offer_details"] else ""
        otype = clean_text(o[c["offer_type"]]) if c["offer_type"] else ""
        tags = list(dict.fromkeys(offer_tags(summary) + offer_tags(otype)))
        tt = pd.to_datetime(o[c["travel_to"]], errors="coerce") if c["travel_to"] else pd.NaT
        display = (summary if summary else otype)[:160]
        cand = {"summary": display, "type": otype, "tags": tags,
                "book_to": bt.strftime("%d %b %Y") if pd.notna(bt) else "",
                "travel_to": tt.strftime("%d %b %Y") if pd.notna(tt) else "",
                "expiring_soon": bool(pd.notna(bt) and (bt.date() - today).days <= 7),
                "_raw_for_score": summary + " " + otype}
        # keep the richer offer if a hotel has several (more tags / longer detail)
        if gi not in idx or len(cand["tags"]) > len(idx[gi]["tags"]):
            idx[gi] = cand
    if expired:
        notes.append(f"Offers: {expired} expired offers skipped.")
    return idx


# ---------------------------------------------------------------------------
def build(args):
    notes = []
    today = datetime.now().date()
    prices, refreshed = load_prices(args.prices, notes)
    bookings, has_board = load_bookings(args.bookings, notes)
    if prices.empty:
        print("\n".join(f"- {n}" for n in notes))
        sys.exit(1)
    offers = load_offers(args.offers, notes, today)

    # demand aggregates by GIATA
    bk_total, bk_6w, bk_rev = Counter(), Counter(), Counter()
    bk_board = defaultdict(Counter)
    if not bookings.empty:
        valid = bookings.dropna(subset=["date"])
        win_start = (valid["date"].max() - timedelta(weeks=6)) if len(valid) else None
        for _, r in bookings.iterrows():
            gi = r["giata"]
            bk_total[gi] += 1
            bk_rev[gi] += float(r["revenue"] or 0)
            if win_start is not None and pd.notna(r["date"]) and r["date"] >= win_start:
                bk_6w[gi] += 1
            if has_board and pd.notna(r["board"]):
                bk_board[gi][norm(r["board"])] += 1
        matched = sum(1 for gi in bk_total if gi in set(prices["giata"]))
        notes.append(f"Bookings: {len(bookings)} rows across {len(bk_total)} hotels; "
                     f"{matched} matched the price snapshot by GIATA.")

    # data-adaptive demand caps (90th percentile, with floors)
    if bk_total:
        cap_cnt = max(float(np.percentile(list(bk_total.values()), 90)) * 1.5, 6)
        cap_rev = max(float(np.percentile([v for v in bk_rev.values() if v > 0] or [0], 90)), 10000)
    else:
        cap_cnt, cap_rev = 6, 10000

    # region-level + region-star price stats; region modal board; region demand
    def pctiles(series):
        return {"n": len(series), "p25": float(series.quantile(.25)),
                "median": float(series.median()), "p75": float(series.quantile(.75))}

    region_price, region_star_price, region_board = {}, {}, {}
    region_vol = Counter()      # destination booking volume -> slot allocation
    for region, g in prices.groupby("region"):
        region_price[region] = pctiles(g["price"])
        for star, gs in g.groupby("stars"):
            if len(gs) >= 3:
                region_star_price[(region, int(star))] = pctiles(gs["price"])
        region_vol[region] = sum(bk_total.get(gi, 0) for gi in g["giata"])
        bc = Counter()
        for gi in g["giata"]:
            bc.update(bk_board.get(gi, {}))
        if bc:
            region_board[region] = bc.most_common(1)[0][0]

    # seller tiers (per region, by booking volume)
    tiers = {}
    for region, g in prices.groupby("region"):
        vol = sorted([(gi, bk_total.get(gi, 0)) for gi in g["giata"] if bk_total.get(gi, 0) > 0],
                     key=lambda x: x[1], reverse=True)
        total = sum(n for _, n in vol)
        cum, t1 = 0, 0
        for gi, n in vol:
            cum += n
            share = cum / total if total else 1
            if share <= 0.40 and t1 < 5:
                tiers[gi], t1 = 1, t1 + 1
            elif share <= 0.75:
                tiers[gi] = 2
            else:
                tiers[gi] = 3

    # assemble hotels
    hotels = []
    for _, h in prices.iterrows():
        region, gi, price = h["region"], h["giata"], float(h["price"])
        star = int(h["stars"]) if pd.notna(h["stars"]) else 0
        macro = macro_region(region, h["country"])

        band = region_star_price.get((region, star)) or region_price[region]
        if band["n"] < 3:
            vscore, vtag = 3, "limited_peers"
        elif price <= band["p25"]:
            vscore, vtag = 5, "exceptional"
        elif price <= band["median"]:
            vscore, vtag = 4, "good_value"
        elif price <= band["p75"]:
            vscore, vtag = 3, "fair"
        else:
            vscore, vtag = 2, "above_typical"

        if bk_board.get(gi) and bk_total.get(gi, 0) >= 3:
            best_raw, src = bk_board[gi].most_common(1)[0][0], "hotel"
        elif region in region_board:
            best_raw, src = region_board[region], "region"
        else:
            best_raw, src = h["cheapboard"], "cheapest"

        offer = offers.get(gi)
        cnt, rev = bk_total.get(gi, 0), bk_rev.get(gi, 0.0)
        demand = 100 * (0.6 * min(1, cnt / cap_cnt) + 0.4 * min(1, rev / cap_rev))
        ofscore = score_offer(offer, region, macro)
        v100 = {2: 25, 3: 50, 4: 75, 5: 100}.get(vscore, 50)
        rec = 0.45 * demand + 0.35 * ofscore + 0.20 * v100

        hotels.append({
            "hotel": h["hotel"], "giata": gi, "region": region, "country": h["country"],
            "macro": macro, "stars": star, "price": round(price, 2),
            "best_board": board_label(best_raw) or "—", "board_source": src,
            "value_score": vscore, "value_tag": vtag,
            "bookings_total": cnt, "revenue": round(rev),
            "region_vol": region_vol.get(region, 0),
            "peer_median": round(band["median"], 2),
            "seller_tier": tiers.get(gi, 0),
            "has_offer": offer is not None, "offer_score": ofscore,
            "offer": {k: v for k, v in offer.items() if k != "_raw_for_score"} if offer else None,
            "rec_score": round(rec, 1),
        })

    src_counts = Counter(h["board_source"] for h in hotels)
    notes.append(f"Best-board source: {src_counts.get('hotel',0)} hotel bookings, "
                 f"{src_counts.get('region',0)} region fallback, {src_counts.get('cheapest',0)} price-snapshot.")

    # ---- Top 40: offers only, slots allocated by destination booking volume
    offer_hotels = [h for h in hotels if h["has_offer"]]
    by_region = defaultdict(list)
    for h in offer_hotels:
        by_region[h["region"]].append(h)
    for r in by_region:
        by_region[r].sort(key=lambda x: x["rec_score"], reverse=True)

    N, CAP = 40, 3
    regions_o = list(by_region)
    W = sum(region_vol.get(r, 0) for r in regions_o)
    raw = {r: (N * region_vol.get(r, 0) / W if W > 0 else 0) for r in regions_o}
    alloc = {r: min(CAP, int(raw[r]), len(by_region[r])) for r in regions_o}

    def cap_left(r):
        return min(CAP, len(by_region[r])) - alloc[r]

    while sum(alloc.values()) < N and any(cap_left(r) > 0 for r in regions_o):
        cands = [r for r in regions_o if cap_left(r) > 0]
        cands.sort(key=lambda r: (raw[r] - int(raw[r]), region_vol.get(r, 0),
                                  by_region[r][alloc[r]]["rec_score"]), reverse=True)
        alloc[cands[0]] += 1

    chosen = []
    for r in regions_o:
        chosen += by_region[r][:alloc[r]]
    chosen.sort(key=lambda x: x["rec_score"], reverse=True)
    shortlist = chosen[:N]
    for i, h in enumerate(shortlist, 1):
        h["rank"] = i
    if len(shortlist) < N:
        notes.append(f"Shortlist shortfall: only {len(shortlist)} offer hotels available.")
    else:
        notes.append(f"Top 40 spans {len({h['region'] for h in shortlist})} destinations; "
                     f"slots allocated by booking volume (max {CAP}/destination).")

    expiring = [h for h in shortlist if h["has_offer"] and h["offer"]["expiring_soon"]]
    if expiring:
        notes.append(f"Note: {len(expiring)} of the Top 40 have offers ending within 7 days "
                     "(flagged in the app, but ranking is by deal quality, not urgency).")

    regions = defaultdict(list)
    for h in hotels:
        regions[h["region"]].append(h)
    for r in regions:
        regions[r].sort(key=lambda x: x["rec_score"], reverse=True)

    present = {h["macro"] for h in hotels}
    macro_order = [m for m in MACRO_ORDER if m in present]
    other = sorted({h["region"] for h in hotels if h["macro"] == "Other"})
    if other:
        notes.append(f"Regions tab — {len(other)} destination(s) still in 'Other' (mostly USA/Americas): "
                     f"{', '.join(other[:12])}{' …' if len(other) > 12 else ''}.")

    payload = {
        "meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                 "cache_refreshed": refreshed, "total_hotels": len(hotels),
                 "total_regions": len(regions),
                 "hotels_with_offers": len(offer_hotels), "macro_order": macro_order},
        "shortlist": shortlist,
        "regions": {r: regions[r] for r in sorted(regions)},
    }
    return payload, notes, shortlist




# ═══════════════════════════════════════════════════════════════════════════
#  STREAMLIT UI  —  Destination2 Deal Finder (Top 40)
#  The engine above is a verbatim copy of the d2-deals-finder skill's
#  scripts/build.py. Keep the two in sync: when the skill's build.py changes,
#  re-paste the section above (everything up to `def build`). Do not edit the
#  scoring logic here.
# ═══════════════════════════════════════════════════════════════════════════
import os
import tempfile
from types import SimpleNamespace

import streamlit as st

st.set_page_config(page_title="D2 Deal Finder", page_icon="🏝️", layout="wide")

st.markdown("""
<style>
:root{--navy:#1B1464;--gold:#D4AF37;--green:#0A7C4E;--amber:#C17A00;--red:#C0392B;}
[data-testid="stAppViewContainer"]{background:#F7F8FC;}
.d2-header{background:linear-gradient(135deg,#1B1464 0%,#2a1f8f 100%);color:#fff;
  padding:20px 26px;border-radius:12px;margin-bottom:10px;}
.d2-header h1{margin:0;font-size:1.7rem;letter-spacing:.3px;}
.d2-header .rule{height:3px;width:64px;background:#D4AF37;margin:10px 0 6px;border-radius:2px;}
.d2-header p{margin:0;opacity:.85;font-size:.92rem;}
.hotel-card{background:#fff;border:1px solid #E8EAF0;border-radius:8px;padding:14px 16px;margin-bottom:10px;}
.hotel-card.tier1{border-left:4px solid #7c3aed;}
.hotel-card.tier2{border-left:4px solid #0e6b85;}
.hotel-card.tier3{border-left:4px solid #4b5563;}
.hotel-card.has-offer{border-top:2px solid #D4AF37;}
.hc-top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;}
.hc-name{font-weight:700;color:#1B1464;font-size:15px;}
.hc-dest{color:#6b7280;font-size:12px;}
.hc-price{color:#0A7C4E;font-weight:700;font-size:15px;white-space:nowrap;}
.hc-peer{color:#9ca3af;font-size:11px;font-weight:400;}
.hc-meta{color:#374151;font-size:12px;margin-top:4px;}
.hc-offer{background:#FBF7E9;border-left:3px solid #D4AF37;padding:6px 10px;border-radius:0 6px 6px 0;
  font-size:12px;color:#5b4a12;margin-top:8px;}
.badge{display:inline-block;font-size:10px;font-weight:600;padding:2px 8px;border-radius:3px;margin-right:4px;}
.badge-rank{background:#1B1464;color:#fff;}
.badge-tier1{background:#F3EEFF;color:#7c3aed;}
.badge-tier2{background:#E6F4F8;color:#0e6b85;}
.badge-tier3{background:#F3F4F6;color:#4b5563;}
.badge-offer{background:#D4AF37;color:#412402;}
.badge-exp{background:#FEF2F2;color:#C0392B;border:1px solid #FCA5A5;}
.badge-score5{background:#F0FBF5;color:#0A7C4E;}
.badge-score4{background:#FFFBEB;color:#C17A00;}
.chip{display:inline-block;background:#EEF0FA;color:#1B1464;font-size:11px;
  padding:3px 10px;border-radius:14px;margin:0 5px 6px 0;}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="d2-header">
  <h1>Destination2 · Deal Finder</h1>
  <div class="rule"></div>
  <p>Weekly Top 40 shortlist · Destination Explorer · Regions roll-up</p>
</div>
""", unsafe_allow_html=True)

# ── session state ─────────────────────────────────────────────────────────────
for k, v in [("payload", None), ("notes", []), ("shortlist", []), ("last_updated", None)]:
    if k not in st.session_state:
        st.session_state[k] = v


# ── UI helpers ──────────────────────────────────────────────────────────────
def _write_tmp(upload):
    if upload is None:
        return None
    suffix = os.path.splitext(upload.name)[1] or ".xlsx"
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(upload.getvalue())
    return path


def run_engine(bk, sn, of):
    """Write uploads to temp files and run the deal-finder engine (build())."""
    paths = [_write_tmp(bk), _write_tmp(sn), _write_tmp(of)]
    try:
        args = SimpleNamespace(bookings=paths[0], prices=paths[1], offers=paths[2])
        try:
            return build(args)                       # engine, defined above
        except SystemExit:
            return None, ["Price snapshot could not be read — it is required. "
                          "Check the file and re-upload."], None
    finally:
        for p in paths:
            if p and os.path.exists(p):
                os.unlink(p)


def offer_summary(h):
    return h["offer"]["summary"] if h.get("has_offer") and h.get("offer") else "—"


def book_by(h):
    if h.get("has_offer") and h.get("offer") and h["offer"].get("book_to"):
        return h["offer"]["book_to"]
    return "—"


def hotel_card(h, rank=None):
    cls = "hotel-card"
    tier = h.get("seller_tier", 0)
    if tier:
        cls += f" tier{tier}"
    if h.get("has_offer"):
        cls += " has-offer"
    badges = []
    if rank:
        badges.append(f'<span class="badge badge-rank">#{rank}</span>')
    if tier:
        badges.append(f'<span class="badge badge-tier{tier}">Tier {tier}</span>')
    vs = h.get("value_score", 0)
    if vs >= 5:
        badges.append('<span class="badge badge-score5">Exceptional value</span>')
    elif vs == 4:
        badges.append('<span class="badge badge-score4">Good value</span>')
    if h.get("has_offer"):
        badges.append(f'<span class="badge badge-offer">Offer {h["offer_score"]}</span>')
        if h["offer"]["expiring_soon"]:
            badges.append('<span class="badge badge-exp">Ends ≤7d</span>')
    peer = f' <span class="hc-peer">peer £{h["peer_median"]:.0f}</span>' if h.get("peer_median") else ""
    offer_block = ""
    if h.get("has_offer"):
        bb = f' · Book by {book_by(h)}' if book_by(h) != "—" else ""
        offer_block = f'<div class="hc-offer">🏷️ {offer_summary(h)}{bb}</div>'
    stars = "★" * int(h.get("stars", 0)) if h.get("stars") else ""
    st.markdown(f"""
<div class="{cls}">
  <div class="hc-top">
    <div>
      <div class="hc-name">{h['hotel']} <span class="hc-dest">{stars}</span></div>
      <div class="hc-dest">{h['region']}</div>
    </div>
    <div class="hc-price">£{h['price']:.0f}{peer}</div>
  </div>
  <div class="hc-meta">{' '.join(badges)}</div>
  <div class="hc-meta">{h['best_board']} · {h['bookings_total']} bookings · rec {h['rec_score']}</div>
  {offer_block}
</div>
""", unsafe_allow_html=True)


def shortlist_dataframe(shortlist):
    return pd.DataFrame([{
        "#": h["rank"], "Hotel": h["hotel"], "Destination": h["region"],
        "Board": h["best_board"], "Price": h["price"],
        "Peer £ (same★)": h["peer_median"], "Bkgs": h["bookings_total"],
        "Offer": offer_summary(h), "Offer score": h["offer_score"],
        "Book By": book_by(h),
        "Ends ≤7d": "⚠️" if (h.get("has_offer") and h["offer"]["expiring_soon"]) else "",
    } for h in shortlist])


# ── tabs ──────────────────────────────────────────────────────────────────────
tab_up, tab_top, tab_explore, tab_regions, tab_dq = st.tabs(
    ["📤 Upload & Generate", "🏆 Top 40", "🔎 Destination Explorer",
     "🌍 Regions", "📋 Data Quality"])

# ── Tab 1: Upload & Generate ────────────────────────────────────────────────
with tab_up:
    st.subheader("Upload the three weekly exports")
    st.caption("The price snapshot is required. Bookings and offers can be "
               "omitted — the app flags the gap and continues. No geographic "
               "exclusion: every destination is eligible, including the Gulf.")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Bookings** (last 6 weeks)")
        bk_file = st.file_uploader("Bookings", type=["xlsx", "xls"],
                                   label_visibility="collapsed", key="bk")
    with c2:
        st.markdown("**Price snapshot**")
        sn_file = st.file_uploader("Snapshot", type=["xlsx", "xls"],
                                   label_visibility="collapsed", key="sn")
    with c3:
        st.markdown("**Active offers**")
        of_file = st.file_uploader("Offers", type=["xlsx", "xls", "csv"],
                                   label_visibility="collapsed", key="of")

    if st.button("⚙️ Generate Deal Finder", type="primary", use_container_width=True):
        if sn_file is None:
            st.error("The price snapshot is required to build the app.")
        else:
            with st.spinner("Scoring hotels and building the Top 40…"):
                payload, notes, shortlist = run_engine(bk_file, sn_file, of_file)
            if payload is None:
                for n in notes:
                    st.error(n)
            else:
                st.session_state.payload = payload
                st.session_state.notes = notes
                st.session_state.shortlist = shortlist
                st.session_state.last_updated = datetime.now().strftime("%d %b %Y %H:%M")
                m = payload["meta"]
                st.success(f"Done — {m['total_hotels']:,} hotels, "
                           f"{m['total_regions']} destinations, "
                           f"{m['hotels_with_offers']} live offers, "
                           f"{len(shortlist)} in the Top 40.")

    if st.session_state.payload:
        st.divider()
        m = st.session_state.payload["meta"]
        cols = st.columns(5)
        cols[0].metric("Priced hotels", f"{m['total_hotels']:,}")
        cols[1].metric("Destinations", f"{m['total_regions']:,}")
        cols[2].metric("Live offers", f"{m['hotels_with_offers']:,}")
        cols[3].metric("Top 40", f"{len(st.session_state.shortlist)}")
        cols[4].metric("Prices refreshed", m.get("cache_refreshed") or "—")
        if st.session_state.last_updated:
            st.caption(f"Last generated: {st.session_state.last_updated}")
        st.download_button("⬇️ JSON payload",
                           data=json.dumps(st.session_state.payload,
                                           ensure_ascii=False, indent=2),
                           file_name="deal_finder_data.json",
                           mime="application/json")

# ── Tab 2: Top 40 ────────────────────────────────────────────────────────────
with tab_top:
    if not st.session_state.shortlist:
        st.info("Generate the Deal Finder to see the Top 40.")
    else:
        sl = st.session_state.shortlist
        exp = sum(1 for h in sl if h.get("has_offer") and h["offer"]["expiring_soon"])
        st.subheader(f"Top {len(sl)} Deal Shortlist")
        st.caption(f"{len(sl)} deals across {len({h['region'] for h in sl})} "
                   f"destinations · {exp} ending within 7 days "
                   f"(⚠️ display only — not ranked higher for urgency).")
        sl_df = shortlist_dataframe(sl)
        st.download_button("⬇️ Shortlist CSV", data=sl_df.to_csv(index=False),
                           file_name="top40_shortlist.csv", mime="text/csv")
        view = st.radio("View", ["Cards (by country)", "Table"],
                        horizontal=True, label_visibility="collapsed")
        if view == "Table":
            st.dataframe(sl_df, hide_index=True, use_container_width=True,
                         column_config={
                             "Price": st.column_config.NumberColumn(format="£%d"),
                             "Peer £ (same★)": st.column_config.NumberColumn(format="£%d"),
                             "Offer score": st.column_config.ProgressColumn(
                                 min_value=0, max_value=100, format="%d")})
        else:
            by_country = {}
            for h in sl:
                by_country.setdefault(h["country"], []).append(h)
            for country in sorted(by_country, key=lambda c: -len(by_country[c])):
                st.markdown(f"#### {country}")
                for h in by_country[country]:
                    hotel_card(h, rank=h["rank"])

# ── Tab 3: Destination Explorer ──────────────────────────────────────────────
with tab_explore:
    if not st.session_state.payload:
        st.info("Generate the Deal Finder to explore destinations.")
    else:
        regions = st.session_state.payload["regions"]
        dest = st.selectbox("Search or pick a destination", sorted(regions.keys()),
                            index=None, placeholder="Type to search…")
        if not dest:
            st.info("Pick a destination to see every hotel there, ranked best-first.")
        else:
            rows = regions[dest]
            offers_n = sum(1 for h in rows if h.get("has_offer"))
            st.caption(f"{len(rows)} hotels in **{dest}** · {offers_n} with a live "
                       f"offer · ranked by recommendation score.")
            key = f"explore_n_{dest}"
            if key not in st.session_state:
                st.session_state[key] = 12
            n = st.session_state[key]
            for h in rows[:n]:
                hotel_card(h)
            if n < len(rows):
                if st.button(f"Show more ({len(rows) - n} left)", key=f"more_{dest}"):
                    st.session_state[key] += 12
                    st.rerun()

# ── Tab 4: Regions ───────────────────────────────────────────────────────────
with tab_regions:
    if not st.session_state.payload:
        st.info("Generate the Deal Finder to see the regions roll-up.")
    else:
        payload = st.session_state.payload
        regions = payload["regions"]
        by_macro = {}
        for r, rows in regions.items():
            for h in rows:
                by_macro.setdefault(h["macro"], []).append(h)
        for m in payload["meta"].get("macro_order", []):
            hotels = sorted(by_macro.get(m, []),
                            key=lambda x: x["rec_score"], reverse=True)
            if not hotels:
                continue
            dests = sorted({h["region"] for h in hotels})
            offers_n = sum(1 for h in hotels if h.get("has_offer"))
            st.markdown(f"### {m}")
            mc = st.columns(3)
            mc[0].metric("Hotels", f"{len(hotels):,}")
            mc[1].metric("Destinations", f"{len(dests):,}")
            mc[2].metric("Live offers", f"{offers_n:,}")
            st.markdown("".join(f'<span class="chip">{d.split(" - ")[0]}</span>'
                                for d in dests), unsafe_allow_html=True)
            st.markdown("**Top deals in this region**")
            for h in hotels[:15]:
                hotel_card(h)
            st.divider()

# ── Tab 5: Data Quality ──────────────────────────────────────────────────────
with tab_dq:
    if not st.session_state.notes:
        st.info("Generate the Deal Finder to see Data Quality Notes.")
    else:
        st.subheader("Data Quality Notes")
        for note in st.session_state.notes:
            st.markdown(f"- {note}")
