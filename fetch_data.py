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

def get_fx():
    """EUR/USD: offizieller EZB-Referenzkurs (täglich ~16:00 CET)."""
    series = _ecb_series("EXR/D.USD.EUR.SP00.A", n=10)
    if not series:
        print("[WARN] FX: EZB-Referenzkurs nicht verfügbar")
        return None
    d, rate = series[-1]
    prev = series[-2][1] if len(series) >= 2 else None
    chg = round((rate - prev) / prev * 100, 2) if prev else None
    print(f"[OK] EUR/USD (EZB-Referenzkurs): {rate} ({d}), Δ {chg}%")
    return {"rate": round(rate, 4), "date": d,
            "prev": round(prev, 4) if prev else None, "chg_pct": chg}

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
            "$limit": 15,
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
            # Netto-Historie (gleiche Feldpriorität wie die gewählte Quelle)
            if src == "Disaggregated/Leveraged Money":
                lkey, skey = "lev_money_positions_long", "lev_money_positions_short"
            else:
                lkey, skey = "asset_mgr_positions_long", "asset_mgr_positions_short"
            history = []
            for rec in reversed(data):
                l, s = _f(rec, lkey), _f(rec, skey)
                if l == 0 and s == 0:
                    continue
                rd = str(rec.get("report_date_as_yyyy_mm_dd") or "")[:10]
                if len(rd) == 10:
                    history.append({"date": f"{rd[8:10]}.{rd[5:7]}", "net": int(l - s)})
            print(f"[OK] COT: net={net:+,.0f}, npct={npct:+.1f}%, bias={bias}, Historie={len(history)} Wochen")
            return {"date": raw_d, "net": int(net), "delta_net": int(dnet),
                    "long_pct": lpct, "short_pct": spct, "net_pct": npct,
                    "oi": int(oi), "bias": bias, "source": src,
                    "history": history}
    return None

def _retail_bias(lp, sp):
    return ("CONTRARIAN BULLISCH" if sp >= 60 else
            "CONTRARIAN BÄRISCH"  if lp >= 60 else
            "LEICHT BULLISCH"      if sp >= 55 else
            "LEICHT BÄRISCH"       if lp >= 55 else "NEUTRAL")

def get_retail():
    hdrs = {"User-Agent": "Mozilla/5.0 (compatible; EURUSDBot/5.0)", "Accept": "application/json"}
    data = safe_get("https://marketmilk.babypips.com/api/sentiment.json",
                    {"pair": "EURUSD"}, timeout=15, headers=hdrs)
    if data and not data.get("error"):
        try:
            lp = float(data.get("long_percentage", data.get("longPercentage", 0)))
            sp = float(data.get("short_percentage", data.get("shortPercentage", 0)))
            if lp + sp > 0:
                print(f"[OK] Retail (MarketMilk): Long={lp}% Short={sp}%")
                return {"long_pct": lp, "short_pct": sp, "bias": _retail_bias(lp, sp), "source": "MarketMilk"}
        except Exception as e:
            print(f"[WARN] Retail MarketMilk: {e}")
    # Fallback: Oanda Orderbook (wie im Worker)
    data = safe_get("https://www.oanda.com/oanda_fx_sentiment/data/getdata.json",
                    {"instrument": "EUR_USD"}, timeout=15, headers=hdrs)
    if data:
        try:
            items = data.get("data") or data.get("orderBook") or []
            longv  = sum(float(i.get("longCountPercent") or 0) for i in items)
            shortv = sum(float(i.get("shortCountPercent") or 0) for i in items)
            total = longv + shortv
            if total > 0:
                lp = round(longv / total * 100, 1)
                sp = round(shortv / total * 100, 1)
                print(f"[OK] Retail (Oanda): Long={lp}% Short={sp}%")
                return {"long_pct": lp, "short_pct": sp, "bias": _retail_bias(lp, sp), "source": "Oanda Orderbook"}
        except Exception as e:
            print(f"[WARN] Retail Oanda: {e}")
    print("[WARN] Retail: alle Quellen nicht erreichbar")
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

# Fed/EZB publizieren ihre Sitzungskalender nur als HTML/ICS (keine
# Daten-API) – offizieller Sitzungskalender 2026, jährlich zu pflegen.
# Ganzjahresliste: auch vergangene Sitzungen, da Protokolle und
# Release-Ergebnisse daraus abgeleitet werden.
FOMC_MEETINGS = [date(2026,1,29), date(2026,3,18), date(2026,4,29), date(2026,6,17),
                 date(2026,7,29), date(2026,9,16), date(2026,10,28), date(2026,12,9)]
ECB_MEETINGS  = [date(2026,1,30), date(2026,3,19), date(2026,4,30), date(2026,6,11),
                 date(2026,7,23), date(2026,9,10), date(2026,10,29), date(2026,12,3)]
# Protokolle: deterministisch abgeleitet, kein eigener Kalender.
# FOMC-Protokoll: laut Fed-Kommunikationspolitik fix 3 Wochen nach Sitzung.
# EZB-Accounts: laut EZB-Kommunikationspolitik fix 4 Wochen nach Sitzung.
FOMC_MIN_DATES = [d + timedelta(days=21) for d in FOMC_MEETINGS]
ECB_PROT_DATES = [d + timedelta(days=28) for d in ECB_MEETINGS]
# BLS-Termine kommen live aus dem FRED-Release-Kalender; der offizielle
# BLS-Jahresplan dient nur als Ausfall-Fallback.
NFP_DATES = [date(2026,7,2),  date(2026,8,7),  date(2026,9,4),  date(2026,10,2)]
CPI_DATES = [date(2026,7,14), date(2026,8,12), date(2026,9,11), date(2026,10,14)]
PPI_DATES = [date(2026,7,15), date(2026,8,13), date(2026,9,12), date(2026,10,15)]

def get_events():
    FOMC, ECB = FOMC_MEETINGS, ECB_MEETINGS
    FOMC_MIN, ECB_PROT = FOMC_MIN_DATES, ECB_PROT_DATES
    NFP, CPI, PPI = NFP_DATES, CPI_DATES, PPI_DATES
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
        _entry("FOMC-Prot.", "medium", _next(FOMC_MIN)),
        _entry("NFP",  "medium", _live("NFP", "Employment Situation", NFP)),
        _entry("CPI",  "medium", _live("CPI", "Consumer Price Index", CPI)),
        _entry("PPI",  "low",    _live("PPI", "Producer Price Index", PPI)),
        _entry("EZB",  "low",    _next(ECB)),
        _entry("EZB-Prot.", "low", _next(ECB_PROT)),
    ]

def get_actuals():
    """Jüngste Ist-Werte der US-Releases (Abgleich mit Prognosen in der App)."""
    out = {}
    obs = _fred("PAYEMS", limit=1, units="chg")
    if obs:
        out["NFP"] = {"value": round(float(obs[0][0])), "unit": "K", "date": obs[0][1][:7]}
    obs = _fred("CPIAUCSL", limit=1, units="pc1")
    if obs:
        out["CPI"] = {"value": round(float(obs[0][0]), 1), "unit": "%", "date": obs[0][1][:7]}
    obs = _fred("PPIFIS", limit=1, units="pc1")
    if obs:
        out["PPI"] = {"value": round(float(obs[0][0]), 1), "unit": "%", "date": obs[0][1][:7]}
    summary = ", ".join(f"{k}={v['value']}{v['unit']}" for k, v in out.items())
    print(f"[OK] Ist-Werte: {summary or 'keine'}")
    return out

def _load_user_events():
    """events.json aus dem Workdir (gh-pages-Checkout) – Nutzer-Prognosen."""
    try:
        with open("events.json", encoding="utf-8") as f:
            return json.load(f).get("events", [])
    except Exception:
        return []

def _recent_past(dates, week_ago):
    past = [d for d in sorted(dates) if week_ago <= d < TODAY or d == TODAY]
    return past[-1] if past else None

_NUM_RELEASES = {
    # invertierte Richtung: schwächer/niedriger als Referenz = USD-negativ = BULLISCH EUR/USD
    "CPI": {"sid": "CPIAUCSL", "units": "pc1", "unit": "%", "dec": 1},
    "PPI": {"sid": "PPIFIS",   "units": "pc1", "unit": "%", "dec": 1},
}

def _report_month(rel):
    """Berichtsmonat eines Releases = Vormonat des Veröffentlichungsdatums."""
    return (rel.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

def _nfp_result(rel, fore):
    """
    NFP-Interpretation über vier Komponenten statt nur der Headline
    (der Markt-Spike auf die Headline hält oft nur Sekunden):
      1. Headline vs Referenz (Nutzer-Prognose, sonst Vormonat)
      2. Arbeitslosenquote vs Vormonat (fallend = USD-stark)
      3. Stundenlöhne MoM vs Vormonat (steigend = USD-stark)
      4. Zusammensetzung: Privatsektor trägt den Zuwachs (= nachhaltig,
         USD-stark); schrumpfender Privatsektor = USD-schwach, auch bei
         starker Headline (staatsgetriebene Stellen werden oft revidiert).
    Jede Komponente zählt +1 (USD-stark) oder -1 (USD-schwach).
    Score >= +2 → BÄRISCH EUR/USD, <= -2 → BULLISCH, sonst NEUTRAL
    (gemischter Report – kein belastbares Signal).

    Dominanz-Regel: Die Komponenten-Wertung gilt nur, wenn die Headline
    nahe der Referenz liegt. Bei deutlicher Verfehlung/Übertreffung
    (Abweichung >= 50% der Referenz, mindestens 75K) ist die
    Überraschung selbst das Ereignis und bestimmt das Signal direkt.

    Liefert None, solange FRED den Berichtsmonat noch nicht hat.
    """
    month = _report_month(rel)
    payems = _fred("PAYEMS", limit=2, units="chg")
    if len(payems) < 2 or payems[0][1][:7] != month:
        return None
    headline = round(float(payems[0][0]))
    prev_headline = round(float(payems[1][0]))
    ref, ref_label = (fore, "Prog") if fore is not None else (prev_headline, "Vormonat")
    score = 1 if headline > ref else -1 if headline < ref else 0
    parts = [f"NFP {headline:+.0f}K vs {ref_label} {ref:+.0f}K"]

    unrate = _fred("UNRATE", limit=2)
    if len(unrate) >= 2 and unrate[0][1][:7] == month:
        u_now, u_prev = float(unrate[0][0]), float(unrate[1][0])
        score += 1 if u_now < u_prev else -1 if u_now > u_prev else 0
        parts.append(f"ALQ {u_now:.1f}%{'▼' if u_now < u_prev else '▲' if u_now > u_prev else '='}")

    ahe = _fred("CES0500000003", limit=2, units="pch")
    if len(ahe) >= 2 and ahe[0][1][:7] == month:
        w_now, w_prev = float(ahe[0][0]), float(ahe[1][0])
        score += 1 if w_now > w_prev else -1 if w_now < w_prev else 0
        parts.append(f"Löhne {w_now:+.1f}%{'▲' if w_now > w_prev else '▼' if w_now < w_prev else '='}")

    priv = _fred("USPRIV", limit=1, units="chg")
    gov  = _fred("USGOVT", limit=1, units="chg")
    if priv and priv[0][1][:7] == month:
        p = round(float(priv[0][0]))
        g = round(float(gov[0][0])) if gov and gov[0][1][:7] == month else None
        if p > 0 and (g is None or p >= g):
            score += 1
        elif p < 0:
            score -= 1
        parts.append(f"Privat {p:+.0f}K" + (f"/Staat {g:+.0f}K" if g is not None else ""))

    deviation = headline - ref
    if abs(deviation) >= max(75, 0.5 * abs(ref)):
        signal = "BULLISCH" if deviation < 0 else "BÄRISCH"
        parts.append("Headline dominiert")
    else:
        signal = "BÄRISCH" if score >= 2 else "BULLISCH" if score <= -2 else "NEUTRAL"
    return {"label": "NFP", "date": rel.strftime("%d.%m."),
            "detail": " · ".join(parts), "signal": signal}

def get_release_results():
    """
    Ergebnisse der Veröffentlichungen der letzten 7 Tage mit EUR/USD-Signal.

    Quantitative US-Releases (NFP/CPI/PPI): Ist-Wert aus FRED, Referenz ist
    die Nutzer-Prognose aus events.json (sonst der Vormonat). Schwächer als
    Referenz = USD-negativ = BULLISCH.

    Qualitative Berichte (FOMC-/EZB-Protokoll) haben keine Zahl – dort dient
    die Marktreaktion der 2J-Rendite am Veröffentlichungstag als Proxy:
    Rendite rauf = hawkish. Hawkishe Fed = BÄRISCH EUR/USD, hawkishe EZB =
    BULLISCH EUR/USD. |Δ| < 0.03pp gilt als NEUTRAL.
    """
    week_ago = TODAY - timedelta(days=7)
    user_events = _load_user_events()
    results = []

    def _user_forecast(label, d):
        for e in user_events:
            if e.get("label") != label:
                continue
            ed = str(e.get("date", ""))
            if ed in (d.strftime("%d.%m.%Y"), d.isoformat()):
                try:
                    return float(str(e.get("forecast", "")).replace(",", "."))
                except (ValueError, TypeError):
                    return None
        return None

    # NFP: 4-Komponenten-Interpretation (Headline, ALQ, Löhne, Privatsektor)
    rel = _recent_past(NFP_DATES, week_ago)
    if rel is not None:
        r = _nfp_result(rel, _user_forecast("NFP", rel))
        if r is None:
            print("[WARN] Ergebnis NFP: Ist-Wert noch nicht in FRED")
            results.append({"label": "NFP", "date": rel.strftime("%d.%m."),
                            "detail": "Ist-Wert ausstehend", "signal": "AUSSTEHEND"})
        else:
            results.append(r)
            print(f"[OK] Ergebnis NFP: {r['detail']} → {r['signal']}")

    date_lists = {"CPI": CPI_DATES, "PPI": PPI_DATES}
    for label, cfg in _NUM_RELEASES.items():
        rel = _recent_past(date_lists[label], week_ago)
        if rel is None:
            continue
        obs = _fred(cfg["sid"], limit=2, units=cfg["units"])
        # Nur werten, wenn FRED schon den Berichtsmonat (= Vormonat des
        # Release-Datums) liefert – sonst als AUSSTEHEND markieren.
        if len(obs) < 2 or obs[0][1][:7] != _report_month(rel):
            print(f"[WARN] Ergebnis {label}: Ist-Wert noch nicht in FRED")
            results.append({"label": label, "date": rel.strftime("%d.%m."),
                            "detail": "Ist-Wert ausstehend", "signal": "AUSSTEHEND"})
            continue
        actual = round(float(obs[0][0]), cfg["dec"])
        prev   = round(float(obs[1][0]), cfg["dec"])
        fore   = _user_forecast(label, rel)
        ref, ref_label = (fore, "Prog") if fore is not None else (prev, "Vormonat")
        signal = ("NEUTRAL" if actual == ref else
                  "BULLISCH" if actual < ref else "BÄRISCH")
        fmt = (lambda v: f"{v:+.0f}") if cfg["unit"] == "K" else (lambda v: f"{v:.1f}")
        results.append({"label": label, "date": rel.strftime("%d.%m."),
                        "detail": f"Ist {fmt(actual)}{cfg['unit']} vs {ref_label} {fmt(ref)}{cfg['unit']}",
                        "signal": signal})
        print(f"[OK] Ergebnis {label}: {actual}{cfg['unit']} vs {ref_label} {ref} → {signal}")

    def _reaction(label, rel, delta, hawkish_signal, dovish_signal, market):
        if abs(delta) < 0.03:
            signal = "NEUTRAL"
        elif delta > 0:
            signal = hawkish_signal
        else:
            signal = dovish_signal
        results.append({"label": label, "date": rel.strftime("%d.%m."),
                        "detail": f"{market} {delta:+.2f}pp",
                        "signal": signal})
        print(f"[OK] Ergebnis {label}: {market} Δ {delta:+.2f}pp → {signal}")

    # FOMC-Protokoll: Reaktion der US-2Y-Rendite (hawkish Fed = BÄRISCH EUR/USD)
    rel = _recent_past(FOMC_MIN_DATES, week_ago)
    if rel is not None:
        obs = _fred("DGS2", limit=2)
        if len(obs) >= 2 and obs[0][1] >= rel.isoformat():
            _reaction("FOMC-Prot.", rel, float(obs[0][0]) - float(obs[1][0]),
                      "BÄRISCH", "BULLISCH", "US-2Y")
        else:
            print("[WARN] Ergebnis FOMC-Prot.: Marktreaktion noch nicht verfügbar")
            results.append({"label": "FOMC-Prot.", "date": rel.strftime("%d.%m."),
                            "detail": "US-2Y-Schluss ausstehend (~22:15 MESZ)",
                            "signal": "AUSSTEHEND"})

    # EZB-Protokoll: Reaktion der Euro-2Y-Kurve (hawkish EZB = BULLISCH EUR/USD)
    rel = _recent_past(ECB_PROT_DATES, week_ago)
    if rel is not None:
        series = _ecb_series("YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y", n=8)
        if len(series) >= 2 and series[-1][0] >= rel.isoformat():
            _reaction("EZB-Prot.", rel, series[-1][1] - series[-2][1],
                      "BULLISCH", "BÄRISCH", "EUR-2Y")
        else:
            print("[WARN] Ergebnis EZB-Prot.: Marktreaktion noch nicht verfügbar")
            results.append({"label": "EZB-Prot.", "date": rel.strftime("%d.%m."),
                            "detail": "EUR-2Y-Schluss ausstehend",
                            "signal": "AUSSTEHEND"})

    # Zinsentscheide: Änderung des Zielsatzes selbst
    def _last_change_asc(seq):
        """seq: aufsteigend (datum, wert) → (change_date, cur, prev) oder None."""
        for i in range(len(seq) - 1, 0, -1):
            if abs(seq[i][1] - seq[i-1][1]) > 0.001:
                return seq[i][0], seq[i][1], seq[i-1][1]
        return None

    def _rate_decision(label, rel, seq, hike_signal, cut_signal, rate_name):
        if not seq:
            return
        chg = _last_change_asc(seq)
        if chg is not None and chg[0] >= rel.isoformat():
            _, cur, prev = chg
            signal = hike_signal if cur > prev else cut_signal
            detail = f"{rate_name} {cur:.2f}% (zuvor {prev:.2f}%)"
        else:
            signal = "NEUTRAL"
            detail = f"{rate_name} {seq[-1][1]:.2f}% (unverändert)"
        results.append({"label": label, "date": rel.strftime("%d.%m."),
                        "detail": detail, "signal": signal})
        print(f"[OK] Ergebnis {label}: {detail} → {signal}")

    # Fed-Erhöhung = USD-positiv = BÄRISCH EUR/USD
    rel = _recent_past(FOMC_MEETINGS, week_ago)
    if rel is not None:
        obs = _fred("DFEDTARU", limit=15)
        seq = [(dt, float(v)) for v, dt in reversed(obs)]
        _rate_decision("FOMC", rel, seq, "BÄRISCH", "BULLISCH", "Zielband")

    # EZB-Erhöhung = EUR-positiv = BULLISCH EUR/USD
    rel = _recent_past(ECB_MEETINGS, week_ago)
    if rel is not None:
        seq = (_ecb_series("FM/B.U2.EUR.4F.KR.DFR.LEV", n=200)
               or _ecb_series("FM/D.U2.EUR.4F.KR.DFR.LEV", n=200))
        _rate_decision("EZB", rel, seq, "BULLISCH", "BÄRISCH", "DFR")

    return results

def notify_bias_change(old, new):
    """
    Telegram-Alarm, wenn Gesamt-Bias oder ein Teilsignal gegenüber dem
    letzten Lauf kippt. Sendet nur bei Änderung; ohne Telegram-Secrets
    (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) stiller No-Op.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat  = os.getenv("TELEGRAM_CHAT_ID", "")
    labels = [("overall_bias", "Gesamt"), ("rate_bias", "Zinsdiff."),
              ("yield_bias", "2Y-Spread"), ("cot_bias", "COT")]
    changes = []
    for key, lbl in labels:
        o, n = (old or {}).get(key), (new or {}).get(key)
        if o and n and o not in ("N/A",) and n not in ("N/A",) and o != n:
            changes.append(f"{lbl}: {o} → {n}")
    if not changes:
        return
    print(f"[OK] Bias-Wechsel erkannt: {'; '.join(changes)}")
    if not token or not chat:
        print("[WARN] Bias-Alarm: Telegram-Secrets fehlen, kein Versand")
        return
    msg = "⚠️ EUR/USD Bias-Wechsel\n" + "\n".join(changes)
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": msg}, timeout=15)
        r.raise_for_status()
        print("[OK] Telegram Bias-Alarm gesendet")
    except Exception as e:
        print(f"[WARN] Telegram Bias-Alarm: {e}")

def run():
    now = datetime.utcnow()
    print(f"[{now.isoformat()}Z] fetch_data.py startet...")

    dfr,  dfr_date,  dfr_prev  = get_dfr()
    hicp, hicp_date             = get_hicp()
    effr, effr_date             = get_effr()
    cpi,  cpi_date              = get_us_cpi()
    us2y, us2y_date             = get_us2y()
    de2y, de2y_date             = get_de2y()
    fx                          = get_fx()
    cot                         = get_cot()
    retail                      = get_retail()
    events                      = get_events()
    actuals                     = get_actuals()
    release_results             = get_release_results()
    spread_history              = get_spread_history()

    # Vorherige Signale sichern (für Bias-Wechsel-Alarm), bevor überschrieben wird
    prev_signals = {}
    try:
        with open("data.json", encoding="utf-8") as f:
            prev_signals = json.load(f).get("signals", {})
    except Exception:
        pass

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
        "fx": fx, "cot": cot, "retail": retail, "events": events,
        "actuals": actuals, "release_results": release_results,
        "spread_history": spread_history,
    }

    notify_bias_change(prev_signals, out["signals"])

    path = "data.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[OK] data.json geschrieben")
    return out

if __name__ == "__main__":
    run()
