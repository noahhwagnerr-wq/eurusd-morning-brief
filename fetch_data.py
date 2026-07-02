#!/usr/bin/env python3
"""
fetch_data.py – Holt alle Live-Daten und schreibt data.json auf gh-pages.
Wird von GitHub Actions (update-data.yml) ausgeführt.
"""

import os, json, sys
from datetime import datetime, date, timedelta

try:
    import requests
except ImportError:
    print("[ERROR] requests nicht installiert.")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

FRED_API_KEY = os.getenv("FRED_API_KEY", "")
ECB_BASE     = "https://data-api.ecb.europa.eu/service/data"
BUBA_BASE    = "https://api.bundesbank.de/service/data"
TODAY        = date.today()
_MIN_HICP_DATE = date(TODAY.year - 1, 1, 1)

def safe_get(url, params=None, timeout=20, headers=None):
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers=headers or {"User-Agent": "EURUSDBot/5.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[WARN] {url.split('/')[-1][:50]}: {e}")
        return None

def _parse_period(raw):
    if not raw or len(raw) < 4:
        return None
    try:
        return datetime.strptime(raw[:7] + "-01", "%Y-%m-%d").date()
    except Exception:
        pass
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except Exception:
        return None

def _ecb_last(series, extra=None, base=None):
    params = {"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
    if extra:
        params.update(extra)
    data = safe_get(f"{base or ECB_BASE}/{series}", params)
    if not data:
        return None, "N/A"
    try:
        sm   = data["dataSets"][0]["series"]
        key  = list(sm.keys())[0]
        obs  = sm[key]["observations"]
        lk   = sorted(obs.keys(), key=lambda x: int(x))[-1]
        val  = float(obs[lk][0])
        dims = data["structure"]["dimensions"]["observation"][0]["values"]
        raw  = dims[int(lk)]["id"]
        return val, raw
    except Exception as e:
        print(f"[WARN] ECB parse {series}: {e}")
        return None, "N/A"

def _ecb_series(series, n=120):
    since = (TODAY - timedelta(days=n * 2)).strftime("%Y-%m-%d")
    params = {"format": "jsondata", "startPeriod": since, "detail": "dataonly"}
    data = safe_get(f"{ECB_BASE}/{series}", params)
    if not data:
        params2 = {"format": "jsondata", "lastNObservations": n, "detail": "dataonly"}
        data = safe_get(f"{ECB_BASE}/{series}", params2)
    if not data:
        return []
    try:
        sm   = data["dataSets"][0]["series"]
        key  = list(sm.keys())[0]
        obs  = sm[key]["observations"]
        dims = data["structure"]["dimensions"]["observation"][0]["values"]
        result = []
        for k, v in obs.items():
            if v[0] is None:
                continue
            raw = dims[int(k)]["id"]
            result.append((raw, float(v[0])))
        result.sort(key=lambda x: x[0])
        return result
    except Exception as e:
        print(f"[WARN] ECB series {series}: {e}")
        return []

def _fred(series_id, limit=2, sort="desc", units=None):
    p = {"series_id": series_id, "api_key": FRED_API_KEY,
         "file_type": "json", "sort_order": sort, "limit": limit}
    if units:
        p["units"] = units
    data = safe_get("https://api.stlouisfed.org/fred/series/observations", p)
    if data and data.get("observations"):
        return [(o["value"], o["date"]) for o in data["observations"] if o["value"] != "."]
    return []

def _parse_sdmx_candidates(data):
    """
    Extrahiert den neuesten gültigen (value, period) aus SDMX JSON.
    Filtert Werte außerhalb (0, 25] und Perioden vor _MIN_HICP_DATE heraus.
    """
    try:
        sm   = data["dataSets"][0]["series"]
        key  = list(sm.keys())[0]
        obs  = sm[key]["observations"]
        dims = data["structure"]["dimensions"]["observation"][0]["values"]
        candidates = []
        for ok, ov in obs.items():
            if ov[0] is None:
                continue
            v = float(ov[0])
            if not (0.0 < abs(v) <= 25.0):
                continue
            raw = dims[int(ok)]["id"]
            pd_ = _parse_period(raw)
            if pd_ is None or pd_ < _MIN_HICP_DATE:
                continue
            candidates.append((pd_, v, raw[:7]))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1], candidates[0][2]
    except Exception as e:
        print(f"[WARN] SDMX parse: {e}")
    return None, None

def get_hicp():
    """
    5-Quellen-Kaskade für Eurozone HICP (jährliche Veränderungsrate).

    WICHTIG: Das alte ECB ICP-Dataset (ICP/M.U2.N.000000.4.ANR) wurde
    ab Februar 2026 durch das neue HICP-Dataset abgelöst und enthält
    keine Daten mehr ab 2026. Primäre Quelle ist daher:
      HICP/M.U2.N.000000.4D0.ANR  (neues ECB HICP-Dataset ab Feb 2026)

    Alle Quellen: lastNObservations statt startPeriod (Eurostat HTTP-400-Bug
    bei aktuellen YYYY-MM Perioden).
    """

    # 1) Neues ECB HICP-Dataset (aktiv seit Feb 2026, löst ICP ab)
    d1 = safe_get(
        f"{ECB_BASE}/HICP/M.U2.N.000000.4D0.ANR",
        {"format": "jsondata", "lastNObservations": 4, "detail": "dataonly"}
    )
    if d1:
        val, period = _parse_sdmx_candidates(d1)
        if val is not None:
            print(f"[OK] HICP (ECB HICP 4D0): {val}% ({period})")
            return val, period

    # 2) Altes ECB ICP-Dataset (Fallback für Lücken)
    d2 = safe_get(
        f"{ECB_BASE}/ICP/M.U2.N.000000.4.ANR",
        {"format": "jsondata", "lastNObservations": 4, "detail": "dataonly"}
    )
    if d2:
        val, period = _parse_sdmx_candidates(d2)
        if val is not None:
            print(f"[OK] HICP (ECB ICP legacy): {val}% ({period})")
            return val, period

    # 3) Eurostat SDMX 2.1 — ohne Datumsfilter
    d3 = safe_get(
        "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/prc_hicp_manr/M.RCH_A.CP00.EA",
        {"format": "jsondata", "lastNObservations": 4}
    )
    if d3:
        val, period = _parse_sdmx_candidates(d3)
        if val is not None:
            print(f"[OK] HICP (Eurostat v2): {val}% ({period})")
            return val, period

    # 4) Bundesbank SDMX
    d4 = safe_get(
        "https://api.bundesbank.de/service/data/BBK_ICP/M.DE.N.000000.4.ANR",
        {"format": "jsondata", "lastNObservations": 4, "detail": "dataonly"}
    )
    if d4:
        val, period = _parse_sdmx_candidates(d4)
        if val is not None:
            print(f"[OK] HICP (Bundesbank): {val}% ({period})")
            return val, period

    # 5) FRED CP0000EZ17M086NEST (pc1 = prozentuale Jahresänderung)
    obs = _fred("CP0000EZ17M086NEST", limit=4, units="pc1")
    if obs:
        for raw_v, raw_d in obs:
            try:
                v = round(float(raw_v), 1)
                d_parsed = date.fromisoformat(raw_d)
                if d_parsed < _MIN_HICP_DATE:
                    continue
                if not (0.1 <= abs(v) <= 25.0):
                    continue
                period = raw_d[:7]
                print(f"[OK] HICP (FRED): {v}% ({period})")
                return v, period
            except Exception:
                continue

    print("[WARN] HICP: alle Quellen erschoepft")
    return None, "N/A"

def get_dfr():
    """
    DFR live von der ECB (FM-Dataset). Ermittelt aus der Historie zusätzlich
    den vorherigen Zinssatz (für das Delta-Badge im Frontend).
    """
    for key in ("FM/B.U2.EUR.4F.KR.DFR.LEV", "FM/D.U2.EUR.4F.KR.DFR.LEV"):
        series = _ecb_series(key, n=400)
        if series:
            period, val = series[-1]
            prev = next((v for _, v in reversed(series) if abs(v - val) > 0.001), None)
            print(f"[OK] DFR: {val}% ({period}), zuvor: {prev}%")
            return val, period, prev
    obs = _fred("ECBDFR", limit=400)
    if obs:
        val, period = float(obs[0][0]), obs[0][1]
        prev = next((float(v) for v, _ in obs if abs(float(v) - val) > 0.001), None)
        print(f"[OK] DFR (FRED): {val}% ({period}), zuvor: {prev}%")
        return val, period, prev
    print("[WARN] DFR: alle Quellen erschoepft")
    return None, "N/A", None

def _load_previous_de2y():
    """
    Liest den zuletzt veröffentlichten DE2Y-Wert aus dem bestehenden data.json
    (liegt im Workdir, da der Workflow zuvor gh-pages auscheckt). Dient als
    Übergangs-Fallback, wenn Bundesbank und ECB die Tageskurve noch nicht
    veröffentlicht haben (z.B. bei sehr frühen Morgen-Runs).
    """
    try:
        with open("data.json", encoding="utf-8") as f:
            prev = json.load(f)
        val = prev.get("yields", {}).get("de2y")
        period = prev.get("yields", {}).get("de2y_date")
        if val is not None and period:
            return float(val), period
    except Exception:
        pass
    return None, "N/A"

def get_de2y():
    # Bundesbank YC: DE-spezifische Rendite, direkter als ECB-Gesamteuropa-Kurve
    val, period = _ecb_last("BBK_YC/B.DE.EUR.4F.G_N_A.SV_C_YM.SR_2Y", base=BUBA_BASE)
    if val is not None:
        print(f"[OK] DE2Y (Bundesbank): {val}% ({period})")
        return val, period
    # Fallback: ECB Euroraum AAA-Renditekurve
    val, period = _ecb_last("YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y")
    if val is not None:
        print(f"[OK] DE2Y (ECB YC Fallback): {val}% ({period})")
        return val, period
    # Übergangs-Fallback: letzter veröffentlichter Wert (Tageskurve noch nicht publiziert)
    val, period = _load_previous_de2y()
    if val is not None:
        print(f"[WARN] DE2Y: beide Live-Quellen leer, verwende letzten Stand {val}% ({period})")
        return val, period
    print("[WARN] DE2Y: alle Quellen erschoepft")
    return None, "N/A"

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
    for sid in ("CPIAUCSL", "CPIAUCNS"):
        obs = _fred(sid, limit=14, sort="desc")
        if len(obs) >= 13:
            v = round((float(obs[0][0]) - float(obs[12][0])) / float(obs[12][0]) * 100, 1)
            return v, obs[0][1]
    return None, "N/A"

def get_us2y():
    obs = _fred("DGS2", limit=1)
    if obs:
        v, d = float(obs[0][0]), obs[0][1]
        print(f"[OK] US2Y: {v}% ({d})")
        return v, d
    return None, "N/A"

def get_spread_history():
    since = (TODAY - timedelta(weeks=18)).strftime("%Y-%m-%d")

    us_raw_desc = _fred("DGS2", limit=120, sort="desc")
    us_raw = list(reversed(us_raw_desc))
    us_obs = [(d, float(v)) for v, d in us_raw if d >= since]

    de_raw = _ecb_series("YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y", n=120)
    de_obs = [(d, v) for d, v in de_raw if d >= since]

    if not us_obs or not de_obs:
        print(f"[WARN] Spread-History: US={len(us_obs)} DE={len(de_obs)} Punkte")
        return []

    de_map = {d: v for d, v in de_obs}

    result = []
    seen_weeks = set()
    for d_str, us_v in us_obs:
        d = date.fromisoformat(d_str)
        week = d.isocalendar()[:2]
        if week in seen_weeks:
            continue
        seen_weeks.add(week)
        closest_de = min(de_map.keys(), key=lambda x: abs(date.fromisoformat(x) - d), default=None)
        if closest_de is None:
            continue
        spread = round(us_v - de_map[closest_de], 2)
        result.append({"date": d.strftime("%d.%m"), "spread": spread})

    result = result[-14:]
    print(f"[OK] Spread-History: {len(result)} Datenpunkte")
    return result

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
            oi    = _f(cur, "open_interest_all") or lc + sc
            net   = lc - sc
            dnet  = net - (lp - sp)
            npct  = round(net / oi * 100, 1) if oi > 0 else 0.0
            lpct  = _f(cur, "pct_of_oi_lev_money_long", "pct_of_oi_asset_mgr_long") or round(lc / oi * 100, 1)
            spct  = _f(cur, "pct_of_oi_lev_money_short", "pct_of_oi_asset_mgr_short") or round(sc / oi * 100, 1)
            if npct > 5 or net > 10000:
                bias = "NET-LONG"
            elif npct < -5 or net < -10000:
                bias = "NET-SHORT"
            else:
                bias = "NEUTRAL"
            raw_d = str(cur.get("report_date_as_yyyy_mm_dd") or "N/A")[:10]
            print(f"[OK] COT: net={net:+,.0f}, npct={npct:+.1f}%, bias={bias}")
            return {"date": raw_d, "net": int(net), "delta_net": int(dnet),
                    "long_pct": lpct, "short_pct": spct, "net_pct": npct,
                    "oi": int(oi), "bias": bias, "source": src}
    return None

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
                return {"long_pct": lp, "short_pct": sp, "bias": bias, "source": "MarketMilk"}
        except Exception as e:
            print(f"[WARN] Retail: {e}")
    return None

def _fred_release_calendar():
    """
    Künftige Veröffentlichungstermine aus dem FRED-Release-Kalender
    (spiegelt den offiziellen BLS-Zeitplan). Liefert {release_name: date}
    mit dem jeweils nächsten Termin ab heute.
    """
    p = {"api_key": FRED_API_KEY, "file_type": "json",
         "include_release_dates_with_no_data": "true",
         "realtime_start": TODAY.strftime("%Y-%m-%d"),
         "realtime_end": "9999-12-31",
         "sort_order": "asc", "limit": 1000}
    data = safe_get("https://api.stlouisfed.org/fred/releases/dates", p)
    cal = {}
    for row in (data or {}).get("release_dates", []):
        try:
            d = date.fromisoformat(str(row.get("date")))
        except (ValueError, TypeError):
            continue
        if d < TODAY:
            continue
        name = row.get("release_name", "")
        if name and (name not in cal or d < cal[name]):
            cal[name] = d
    if cal:
        print(f"[OK] FRED-Release-Kalender: {len(cal)} Releases mit künftigen Terminen")
    else:
        print("[WARN] FRED-Release-Kalender: keine Termine erhalten")
    return cal

def get_events():
    # Fed/EZB publizieren ihre Sitzungskalender nur als HTML/ICS (keine
    # Daten-API) – offizieller Sitzungskalender 2026, jährlich zu pflegen.
    FOMC = [date(2026,7,29), date(2026,9,16), date(2026,10,28), date(2026,12,9)]
    ECB  = [date(2026,7,23), date(2026,9,10), date(2026,10,29), date(2026,12,3)]
    # BLS-Termine kommen live aus dem FRED-Release-Kalender; der offizielle
    # BLS-Jahresplan dient nur als Ausfall-Fallback.
    NFP  = [date(2026,7,2),  date(2026,8,7),  date(2026,9,4),  date(2026,10,2)]
    CPI  = [date(2026,7,14), date(2026,8,12), date(2026,9,11), date(2026,10,14)]
    PPI  = [date(2026,7,15), date(2026,8,13), date(2026,9,12), date(2026,10,15)]
    cal = _fred_release_calendar()
    def _next(dates):
        # >= statt >: Event bleibt am Tag selbst sichtbar (days=0 → "HEUTE")
        fut = [d for d in sorted(dates) if d >= TODAY]
        return fut[0] if fut else None
    def _live(label, release_name, fallback):
        d = cal.get(release_name)
        if d is not None:
            print(f"[OK] {label}-Termin (FRED-Kalender): {d}")
            return d
        print(f"[WARN] {label}-Termin: nicht im FRED-Kalender, nutze BLS-Jahresplan")
        return _next(fallback)
    def _entry(label, importance, d):
        if d is None:
            return {"label": label, "importance": importance, "date": "N/A", "days": None}
        return {"label": label, "importance": importance,
                "date": d.strftime("%d.%m.%Y"), "days": (d - TODAY).days}
    return [
        _entry("FOMC", "high",   _next(FOMC)),
        _entry("NFP",  "medium", _live("NFP", "Employment Situation", NFP)),
        _entry("CPI",  "medium", _live("CPI", "Consumer Price Index", CPI)),
        _entry("PPI",  "low",    _live("PPI", "Producer Price Index", PPI)),
        _entry("EZB",  "low",    _next(ECB)),
    ]

def run():
    now = datetime.utcnow()
    print(f"[{now.isoformat()}Z] fetch_data.py startet...")

    dfr,  dfr_date,  dfr_prev  = get_dfr()
    hicp, hicp_date             = get_hicp()
    effr, effr_date             = get_effr()
    cpi,  cpi_date              = get_us_cpi()
    us2y, us2y_date             = get_us2y()
    de2y, de2y_date             = get_de2y()
    cot                         = get_cot()
    retail                      = get_retail()
    events                      = get_events()
    spread_history              = get_spread_history()

    rate_diff    = round(effr - dfr, 2)  if (effr and dfr)  else None
    yield_spread = round(us2y - de2y, 2) if (us2y and de2y) else None

    def _bias(val, pos_label, neg_label):
        if val is None: return "N/A"
        return pos_label if val > 0 else neg_label

    cot_bias    = cot["bias"]    if cot    else "N/A"
    retail_bias = retail["bias"] if retail else "N/A"

    signals_list = [
        "BÄRISCH" if (rate_diff or 0) > 0 else "BULLISCH" if rate_diff is not None else None,
        "BÄRISCH" if (yield_spread or 0) > 0 else "BULLISCH" if yield_spread is not None else None,
        "BÄRISCH" if cot_bias == "NET-SHORT" else "BULLISCH" if cot_bias == "NET-LONG" else None,
    ]
    sv = [s for s in signals_list if s]
    overall_bias = "BÄRISCH" if sv.count("BÄRISCH") > sv.count("BULLISCH") else \
                   "BULLISCH" if sv.count("BULLISCH") > sv.count("BÄRISCH") else "NEUTRAL"

    out = {
        "generated_at": now.strftime("%d.%m.%Y %H:%M UTC"),
        "generated_ts": now.isoformat() + "Z",
        "ecb": {"dfr": dfr, "dfr_date": (dfr_date or "N/A")[:10], "dfr_prev": dfr_prev,
                "hicp": hicp, "hicp_date": (hicp_date or "N/A")[:7]},
        "fed": {"effr": effr, "effr_date": (effr_date or "N/A")[:10],
                "cpi": cpi, "cpi_date": (str(cpi_date) or "N/A")[:7]},
        "yields": {"us2y": us2y, "us2y_date": (us2y_date or "N/A")[:10],
                   "de2y": de2y, "de2y_date": (de2y_date or "N/A")[:10]},
        "signals": {"rate_diff": rate_diff, "yield_spread": yield_spread,
                    "rate_bias": _bias(rate_diff, "BÄRISCH", "BULLISCH"),
                    "yield_bias": _bias(yield_spread, "USD-Vorteil", "EUR-Vorteil"),
                    "cot_bias": cot_bias, "retail_bias": retail_bias,
                    "overall_bias": overall_bias},
        "cot": cot, "retail": retail, "events": events,
        "spread_history": spread_history,
    }

    path = "data.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[OK] data.json geschrieben")
    return out

if __name__ == "__main__":
    run()
