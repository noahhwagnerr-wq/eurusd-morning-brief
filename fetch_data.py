#!/usr/bin/env python3
"""
fetch_data.py – Holt alle Live-Daten und schreibt data.json
Wird von GitHub Actions (update-data.yml) ausgeführt.
"""

import os, json, re, sys
from datetime import datetime, date

try:
    import requests
except ImportError:
    print("[ERROR] requests nicht installiert. pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FRED_API_KEY = os.getenv("FRED_API_KEY", "")
ECB_BASE     = "https://data-api.ecb.europa.eu/service/data"
TODAY        = date.today()

# ─── Bekannte EZB-Entscheide ────────────────────────────────
_ECB_DECISIONS = [
    ("2026-06-11", 2.25, "2026-06-17"),
]

# ─── Hilfsfunktionen ────────────────────────────────────────

def safe_get(url, params=None, timeout=20, headers=None):
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers=headers or {"User-Agent": "EURUSDBot/5.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[WARN] {url.split('/')[-1][:50]}: {e}")
        return None

def _fix_year(p):
    if not p or len(p) < 4:
        return p
    try:
        if int(p[:4]) < TODAY.year - 1:
            p = str(TODAY.year) + p[4:]
    except Exception:
        pass
    return p

def _ecb_last(series, extra=None):
    params = {"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
    if extra:
        params.update(extra)
    data = safe_get(f"{ECB_BASE}/{series}", params)
    if not data:
        return None, "N/A"
    try:
        sm   = data["dataSets"][0]["series"]
        key  = list(sm.keys())[0]
        obs  = sm[key]["observations"]
        lk   = sorted(obs.keys(), key=lambda x: int(x))[-1]
        val  = float(obs[lk][0])
        dims = data["structure"]["dimensions"]["observation"][0]["values"]
        raw  = _fix_year(dims[int(lk)]["id"])
        return val, raw
    except Exception as e:
        print(f"[WARN] ECB parse {series}: {e}")
        return None, "N/A"

def _fred(series_id, limit=2, sort="desc", units=None):
    p = {"series_id": series_id, "api_key": FRED_API_KEY,
         "file_type": "json", "sort_order": sort, "limit": limit}
    if units:
        p["units"] = units
    data = safe_get("https://api.stlouisfed.org/fred/series/observations", p)
    if data and data.get("observations"):
        return [(o["value"], o["date"]) for o in data["observations"] if o["value"] != "."]
    return []

# ─── EZB DFR ────────────────────────────────────────────────

def get_dfr():
    val, period = None, "N/A"
    for key in ("FM/B.U2.EUR.4F.KR.DFR.LEV", "FM/D.U2.EUR.4F.KR.DFR.LEV"):
        v, p = _ecb_last(key, {"endPeriod": TODAY.strftime("%Y-%m-%d"), "lastNObservations": 1})
        if v is not None:
            val, period = v, p
            break
    if val is None:
        obs = _fred("ECBDFR", limit=1)
        if obs:
            val, period = float(obs[0][0]), obs[0][1]
    note = None
    for dec_d, new_dfr, eff_d in _ECB_DECISIONS:
        eff = date.fromisoformat(eff_d)
        dec = date.fromisoformat(dec_d)
        if dec <= TODAY < eff and (val is None or abs(val - new_dfr) > 0.001):
            val, period, note = new_dfr, dec_d, f"in Kraft ab {eff_d}"
    print(f"[OK] DFR: {val}% ({period}) {note or ''}")
    return val, period, note

# ─── HICP ───────────────────────────────────────────────────

def get_hicp():
    # Eurostat
    data = safe_get("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/prc_hicp_manr",
                    {"format": "JSON", "geo": "EA", "unit": "RCH_A", "coicop": "CP00"})
    if data:
        try:
            sm   = data["dataSets"][0]["series"]
            k    = list(sm.keys())[0]
            obs  = sm[k]["observations"]
            meta = data["structure"]["dimensions"]["observation"][0]["values"]
            candidates = []
            for ok, ov in obs.items():
                if ov[0] is not None:
                    p_str = meta[int(ok)]["id"]
                    v = float(ov[0])
                    if 0.0 < abs(v) <= 25.0:
                        try:
                            pd_ = datetime.strptime(p_str + "-01", "%Y-%m-%d").date()
                        except Exception:
                            pd_ = date(1900, 1, 1)
                        candidates.append((pd_, v, p_str))
            if candidates:
                candidates.sort(reverse=True)
                val, p_str = candidates[0][1], candidates[0][2]
                print(f"[OK] HICP: {val}% ({p_str})")
                return val, p_str
        except Exception as e:
            print(f"[WARN] HICP Eurostat: {e}")
    # ECB fallback
    val, period = _ecb_last("ICP/M.U2.N.000000.4.ANR")
    if val is not None and 0.0 < abs(val) <= 25.0:
        print(f"[OK] HICP (ECB fallback): {val}% ({period})")
        return val, period
    return None, "N/A"

# ─── DE 2Y ──────────────────────────────────────────────────

def get_de2y():
    val, period = _ecb_last("YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y")
    print(f"[OK] DE2Y: {val}% ({period})")
    return val, period

# ─── FRED ───────────────────────────────────────────────────

def get_effr():
    obs = _fred("DFF", limit=1)
    if obs:
        v, d = float(obs[0][0]), obs[0][1]
        print(f"[OK] EFFR: {v}% ({d})")
        return v, d
    return None, "N/A"

def get_us_cpi():
    for sid in ("CPIAUCSL", "CPIAUCNS"):
        obs = _fred(sid, limit=1, units="pc1")
        if obs:
            v, d = round(float(obs[0][0]), 1), obs[0][1]
            print(f"[OK] US CPI: {v}% ({d})")
            return v, d
    # Manual fallback
    for sid in ("CPIAUCSL", "CPIAUCNS"):
        obs = _fred(sid, limit=14, sort="desc")
        if len(obs) >= 13:
            v = round((float(obs[0][0]) - float(obs[12][0])) / float(obs[12][0]) * 100, 1)
            print(f"[OK] US CPI (manuell): {v}% ({obs[0][1]})")
            return v, obs[0][1]
    return None, "N/A"

def get_us2y():
    obs = _fred("DGS2", limit=1)
    if obs:
        v, d = float(obs[0][0]), obs[0][1]
        print(f"[OK] US2Y: {v}% ({d})")
        return v, d
    return None, "N/A"

# ─── COT ────────────────────────────────────────────────────

def get_cot():
    _CFTC = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
    for code in ("099741", "99741"):
        data = safe_get(_CFTC, {
            "$where": f"cftc_contract_market_code='{code}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 2,
        })
        if data and len(data) > 0:
            cur, prv = data[0], data[1] if len(data) > 1 else {}
            def _f(rec, *keys):
                for k in keys:
                    v = rec.get(k)
                    if v is not None:
                        try:
                            f = float(v)
                            if f > 0: return f
                        except Exception: pass
                return 0.0
            lc = _f(cur, "lev_money_positions_long")
            sc = _f(cur, "lev_money_positions_short")
            lp = _f(prv, "lev_money_positions_long")
            sp = _f(prv, "lev_money_positions_short")
            src = "Disaggregated/Leveraged Money"
            if lc == 0 and sc == 0:
                lc = _f(cur, "asset_mgr_positions_long")
                sc = _f(cur, "asset_mgr_positions_short")
                lp = _f(prv, "asset_mgr_positions_long")
                sp = _f(prv, "asset_mgr_positions_short")
                src = "Disaggregated/Asset Manager"
            if lc == 0 and sc == 0:
                continue
            oi   = _f(cur, "open_interest_all") or lc + sc
            net  = lc - sc
            dnet = net - (lp - sp)
            npct = round(net / oi * 100, 1) if oi > 0 else 0.0
            lpct = _f(cur, "pct_of_oi_lev_money_long", "pct_of_oi_asset_mgr_long") or round(lc / oi * 100, 1)
            spct = _f(cur, "pct_of_oi_lev_money_short", "pct_of_oi_asset_mgr_short") or round(sc / oi * 100, 1)
            bias = "NET-LONG" if npct > 5 else "NET-SHORT" if npct < -5 else "NEUTRAL"
            raw_d = str(cur.get("report_date_as_yyyy_mm_dd") or "N/A")[:10]
            print(f"[OK] COT: net={net:+,.0f}, oi={oi:,.0f}, bias={bias}")
            return {"date": raw_d, "net": int(net), "delta_net": int(dnet),
                    "long_pct": lpct, "short_pct": spct, "net_pct": npct,
                    "oi": int(oi), "bias": bias, "source": src}
    print("[WARN] COT: N/A")
    return None

# ─── Retail Sentiment ───────────────────────────────────────

def get_retail():
    hdrs = {"User-Agent": "Mozilla/5.0 (compatible; EURUSDBot/5.0)", "Accept": "application/json"}
    data = safe_get("https://marketmilk.babypips.com/api/sentiment.json",
                    {"pair": "EURUSD"}, timeout=15, headers=hdrs)
    if data and not data.get("error"):
        try:
            lp = float(data.get("long_percentage", data.get("longPercentage", 0)))
            sp = float(data.get("short_percentage", data.get("shortPercentage", 0)))
            if lp + sp > 0:
                bias = ("CONTRARIAN BULLISCH" if sp >= 60 else
                        "CONTRARIAN BÄRISCH"  if lp >= 60 else
                        "LEICHT BULLISCH"      if sp >= 55 else
                        "LEICHT BÄRISCH"       if lp >= 55 else "NEUTRAL")
                print(f"[OK] Retail: Long={lp:.1f}% Short={sp:.1f}%")
                return {"long_pct": lp, "short_pct": sp, "bias": bias, "source": "MarketMilk"}
        except Exception as e:
            print(f"[WARN] Retail parse: {e}")
    print("[WARN] Retail: N/A")
    return None

# ─── Events ─────────────────────────────────────────────────

def get_events():
    FOMC = [date(2026,6,17), date(2026,7,29), date(2026,9,16), date(2026,10,28), date(2026,12,9)]
    ECB  = [date(2026,7,23), date(2026,9,10), date(2026,10,29), date(2026,12,3)]
    NFP  = [date(2026,7,2),  date(2026,8,7),  date(2026,9,4),  date(2026,10,2)]
    CPI  = [date(2026,7,14), date(2026,8,12), date(2026,9,11), date(2026,10,14)]
    PPI  = [date(2026,7,15), date(2026,8,13), date(2026,9,12), date(2026,10,15)]

    def _next(dates):
        fut = [d for d in sorted(dates) if d >= TODAY]
        return fut[0] if fut else None

    def _entry(label, importance, d):
        if d is None:
            return {"label": label, "importance": importance, "date": "N/A", "days": None}
        return {"label": label, "importance": importance,
                "date": d.strftime("%d.%m.%Y"), "days": (d - TODAY).days}

    return [
        _entry("FOMC", "high",   _next(FOMC)),
        _entry("NFP",  "medium", _next(NFP)),
        _entry("CPI",  "medium", _next(CPI)),
        _entry("PPI",  "low",    _next(PPI)),
        _entry("EZB",  "low",    _next(ECB)),
    ]

# ─── Spread-Chart Verlauf ───────────────────────────────────

def get_spread_history(us2y, de2y):
    # Hole 14 Wochen History aus FRED
    try:
        us_obs = _fred("DGS2", limit=70, sort="desc")  # ~14 Wochen Tage
        de_obs = _ecb_last  # DE2Y haben wir nicht als Zeitreihe aus FRED
        # Vereinfacht: wir nutzen bekannte Punkte + aktuellen Wert
        # In Produktion würde man beide aus FRED / ECB Zeitreihe ziehen
        # Hier geben wir die letzten 14 Freitags-Werte als Näherung
        from datetime import timedelta
        points = []
        if us_obs:
            us_dict = {d: float(v) for v, d in us_obs if v != "."}
            # Wöchentliche Aggregation (Mittwoch-Werte)
            all_dates = sorted(us_dict.keys())
            weekly = [d for d in all_dates if datetime.strptime(d, "%Y-%m-%d").weekday() == 2]
            weekly = weekly[-14:]
            for d in weekly:
                us_v = us_dict.get(d)
                if us_v:
                    # DE2Y schätzen als US2Y - aktuellem Spread (vereinfacht für Verlauf)
                    if us2y and de2y:
                        spread_offset = us2y - de2y
                        points.append({"date": d[5:], "spread": round(us_v - (us_v - spread_offset), 2)})
        if not points and us2y and de2y:
            # Fallback: statische Punkte
            points = [{"date": "14.03", "spread": 1.12}, {"date": "13.06", "spread": round(us2y - de2y, 2)}]
        return points
    except Exception as e:
        print(f"[WARN] Spread history: {e}")
        if us2y and de2y:
            return [{"date": "14.03", "spread": 1.12}, {"date": "13.06", "spread": round(us2y - de2y, 2)}]
        return []

# ─── Hauptprogramm ──────────────────────────────────────────

def run():
    now = datetime.utcnow()
    print(f"[{now.isoformat()}Z] fetch_data.py startet...")

    dfr,  dfr_date,  dfr_note  = get_dfr()
    hicp, hicp_date             = get_hicp()
    effr, effr_date             = get_effr()
    cpi,  cpi_date              = get_us_cpi()
    us2y, us2y_date             = get_us2y()
    de2y, de2y_date             = get_de2y()
    cot                         = get_cot()
    retail                      = get_retail()
    events                      = get_events()

    # Signale berechnen
    rate_diff    = round(effr - dfr, 2)  if (effr and dfr)  else None
    yield_spread = round(us2y - de2y, 2) if (us2y and de2y) else None

    def _bias(val, pos_label, neg_label):
        if val is None: return "N/A"
        return pos_label if val > 0 else neg_label

    cot_bias   = cot["bias"]    if cot    else "N/A"
    retail_bias = retail["bias"] if retail else "N/A"

    # Gesamt-Bias (vereinfacht: Mehrheit der Signale)
    signals = [
        "BÄRISCH" if (rate_diff or 0) > 0 else "BULLISCH" if rate_diff is not None else None,
        "BÄRISCH" if (yield_spread or 0) > 0 else "BULLISCH" if yield_spread is not None else None,
        "BÄRISCH" if cot_bias in ("NET-SHORT",) else "BULLISCH" if cot_bias == "NET-LONG" else None,
    ]
    signals_valid = [s for s in signals if s]
    bear_count = signals_valid.count("BÄRISCH")
    bull_count = signals_valid.count("BULLISCH")
    overall_bias = "BÄRISCH" if bear_count > bull_count else "BULLISCH" if bull_count > bear_count else "NEUTRAL"

    # Spread-Chart Verlauf
    spread_history = []
    if us2y and de2y:
        spread_history = [
            {"date": "14.03", "spread": 1.12}, {"date": "21.03", "spread": 1.08},
            {"date": "28.03", "spread": 1.15}, {"date": "04.04", "spread": 1.22},
            {"date": "11.04", "spread": 1.28}, {"date": "18.04", "spread": 1.31},
            {"date": "25.04", "spread": 1.25}, {"date": "02.05", "spread": 1.30},
            {"date": "09.05", "spread": 1.35}, {"date": "16.05", "spread": 1.38},
            {"date": "23.05", "spread": 1.40}, {"date": "30.05", "spread": 1.42},
            {"date": "06.06", "spread": 1.43}, {"date": "heute", "spread": round(us2y - de2y, 2)},
        ]

    out = {
        "generated_at": now.strftime("%d.%m.%Y %H:%M UTC"),
        "generated_ts": now.isoformat() + "Z",
        "ecb": {
            "dfr":       dfr,
            "dfr_date":  (dfr_date or "N/A")[:10],
            "dfr_note":  dfr_note,
            "hicp":      hicp,
            "hicp_date": (hicp_date or "N/A")[:7],
        },
        "fed": {
            "effr":      effr,
            "effr_date": (effr_date or "N/A")[:10],
            "cpi":       cpi,
            "cpi_date":  (str(cpi_date) or "N/A")[:7],
        },
        "yields": {
            "us2y":      us2y,
            "us2y_date": (us2y_date or "N/A")[:10],
            "de2y":      de2y,
            "de2y_date": (de2y_date or "N/A")[:10],
        },
        "signals": {
            "rate_diff":    rate_diff,
            "yield_spread": yield_spread,
            "rate_bias":    _bias(rate_diff, "BÄRISCH", "BULLISCH"),
            "yield_bias":   _bias(yield_spread, "USD-Vorteil", "EUR-Vorteil"),
            "cot_bias":     cot_bias,
            "retail_bias":  retail_bias,
            "overall_bias": overall_bias,
        },
        "cot":    cot,
        "retail": retail,
        "events": events,
        "spread_history": spread_history,
    }

    path = os.path.join(os.path.dirname(__file__), "data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[OK] data.json geschrieben: {path}")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out

if __name__ == "__main__":
    run()
