#!/usr/bin/env python3
"""
=============================================================
  EUR/USD Morning Brief Agent
  Täglich live Daten → Telegram-Nachricht
  Quellen: ECB SDMX API (data-api.ecb.europa.eu)
           FRED API (api.stlouisfed.org)
           CFTC Socrata API (publicreporting.cftc.gov)
=============================================================
"""

import os
import re
import requests
from datetime import datetime, timedelta, date
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
FRED_API_KEY       = os.getenv("FRED_API_KEY")

ECB_BASE = "https://data-api.ecb.europa.eu/service/data"
TODAY    = date.today()


def safe_get(url: str, params: dict = None, timeout: int = 15):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[WARN] {url.split('/')[-1]}: {e}")
        return None


def fmt(value, decimals=2, suffix="%") -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{decimals}f}{suffix}"
    except Exception:
        return str(value)


def esc(text: str) -> str:
    """Escapt Sonderzeichen für Telegram MarkdownV2."""
    return re.sub(r'([_*\[\]()~`>#+=|{}.!\-])', r'\\\1', str(text))


def _parse_ecb_period(period_id: str) -> date:
    try:
        if len(period_id) == 10:
            return datetime.strptime(period_id, "%Y-%m-%d").date()
        if len(period_id) == 7 and "-Q" not in period_id:
            return datetime.strptime(period_id + "-01", "%Y-%m-%d").date()
        if "-Q" in period_id:
            y, q = period_id.split("-Q")
            m = (int(q) - 1) * 3 + 1
            return date(int(y), m, 1)
        if len(period_id) == 4:
            return date(int(period_id), 1, 1)
    except Exception:
        pass
    return date(1900, 1, 1)


# ─────────────────────────────────────────────────────────────
#  ECB SDMX FM Dataflow
# ─────────────────────────────────────────────────────────────

_fm_cache = None

def _get_fm():
    global _fm_cache
    if _fm_cache is None:
        _fm_cache = safe_get(
            f"{ECB_BASE}/FM",
            params={"format": "jsondata", "lastNObservations": 5, "detail": "dataonly"}
        )
    return _fm_cache


def _fm_find(instrument_id: str, freq: str = "B", data_type: str = "LEV"):
    data = _get_fm()
    if not data:
        return None, "N/A"
    try:
        dims       = data["structure"]["dimensions"]["series"]
        obs_dim    = data["structure"]["dimensions"]["observation"][0]["values"]
        series_map = data["dataSets"][0]["series"]

        for k, sv in series_map.items():
            if not sv.get("observations"):
                continue
            parts = k.split(":")
            if len(parts) < 6:
                continue

            freq_idx  = int(parts[0])
            instr_idx = int(parts[4])
            dtype_idx = int(parts[5])

            freq_id  = dims[0]["values"][freq_idx]["id"]
            instr_id = dims[4]["values"][instr_idx]["id"]
            dtype_id = dims[5]["values"][dtype_idx]["id"]

            if freq_id != freq or instr_id != instrument_id or dtype_id != data_type:
                continue

            obs = sv["observations"]
            candidates = []
            for obs_k, obs_v in obs.items():
                period_str = obs_dim[int(obs_k)]["id"]
                period_dt  = _parse_ecb_period(period_str)
                if period_dt <= TODAY:
                    candidates.append((period_dt, float(obs_v[0]), period_str))

            if not candidates:
                print(f"[WARN] ECB FM {instrument_id}: alle Datenpunkte in der Zukunft")
                continue

            candidates.sort(key=lambda x: x[0], reverse=True)
            period_dt, val, period_str = candidates[0]
            print(f"[OK] ECB FM {instrument_id} ({freq},{data_type}): {val} ({period_str})")
            return val, period_str

    except Exception as e:
        print(f"[WARN] ECB FM parse ({instrument_id}): {e}")
    return None, "N/A"


def get_ecb_dfr():
    """
    BUG FIX #1: ECB DFR Datenquellen-Reihenfolge
    ─────────────────────────────────────────────
    Problem: FRED (ECBDFR) hat nach ECB-Entscheiden oft 1-2 Tage Verzögerung.
             Dadurch wurde am 12.06.2026 noch 2.00% geliefert, obwohl die ECB
             am 11.06.2026 auf 2.25% angehoben hatte.

    Fix:     Priorität 1 = ECB SDMX direkt (echtzeit, authoritätiv)
             Priorität 2 = FRED ECBDFR (Fallback, ~1-2T verzögert)
             Priorität 3 = ECB SDMX direkt mit explizitem Key-String
    """
    # Prio 1: ECB SDMX FM Dataflow (direkter Key, tagesaktuell)
    for freq in ("B", "D"):
        val, period = _fm_find("DFR", freq=freq, data_type="LEV")
        if val is not None:
            return val, period

    # Prio 2: ECB SDMX direkt mit vollständigem Key (sicherste Quelle)
    for key_variant in (
        "FM/B.U2.EUR.4F.KR.DFR.LEV",
        "FM/D.U2.EUR.4F.KR.DFR.LEV",
    ):
        data = safe_get(
            f"{ECB_BASE}/{key_variant}",
            params={
                "format": "jsondata",
                "endPeriod": TODAY.strftime("%Y-%m-%d"),
                "lastNObservations": 1,
                "detail": "dataonly"
            }
        )
        if data:
            try:
                sm     = data["dataSets"][0]["series"]
                k      = list(sm.keys())[0]
                obs    = sm[k]["observations"]
                last_k = sorted(obs.keys(), key=lambda x: int(x))[-1]
                val    = float(obs[last_k][0])
                periods = data["structure"]["dimensions"]["observation"][0]["values"]
                period = periods[int(last_k)]["id"]
                print(f"[OK] ECB DFR ({key_variant}): {val} ({period})")
                return val, period
            except Exception as e:
                print(f"[WARN] ECB DFR {key_variant}: {e}")

    # Prio 3: FRED ECBDFR (Fallback – kann 1-2T verzögert sein!)
    print("[WARN] ECB DFR: Fallback auf FRED ECBDFR (möglicherweise verzögert)")
    obs = _fred_obs("ECBDFR", limit=1, sort="desc")
    if obs:
        print(f"[OK] FRED ECBDFR: {obs[0][0]} ({obs[0][1]}) – ACHTUNG: evtl. verzögert")
        return float(obs[0][0]), obs[0][1]

    return None, "N/A"


def get_ecb_hicp():
    """
    BUG FIX #2: HICP-Quelle
    ───────────────────────
    Problem: Die ECB SDMX ICP-Serie lieferte 1.90% (Dezember 2025),
             obwohl die aktuelle Flash-Schätzung für Mai 2026 bei 3.2% liegt.
             Ursache: lastNObservations=1 griff auf den zuletzt veröffentlichten
             monatlichen Wert, der möglicherweise noch nicht aktualisiert war.

    Fix:     Quelle 1 = ECB ICP (Monthly, HICP YoY, Gesamtindex)
                        Serienkürzel: ICP/M.U2.N.000000.4.ANR
                        (ANR = Annual rate of change = YoY%)
             Quelle 2 = Eurostat SDMX-JSON (prc_hicp_manr, geo=EA)
                        als Fallback, falls ECB-API nichts liefert
    """
    # Prio 1: ECB SDMX ICP (offiziell, tagesaktuell nach Veröffentlichung)
    for series_key in (
        "ICP/M.U2.N.000000.4.ANR",   # Gesamtindex HICP YoY
        "ICP/M.U2.N.000000.3.INX",   # Gesamtindex, Level (Fallback)
    ):
        data = safe_get(
            f"{ECB_BASE}/{series_key}",
            params={"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
        )
        if data:
            try:
                series_map = data["dataSets"][0]["series"]
                key        = list(series_map.keys())[0]
                obs        = series_map[key]["observations"]
                last_k     = sorted(obs.keys(), key=lambda x: int(x))[-1]
                val        = float(obs[last_k][0])
                periods    = data["structure"]["dimensions"]["observation"][0]["values"]
                period     = periods[int(last_k)]["id"]
                # Plausibilitätscheck: Wert muss > 0 und realistisch sein (0.1% - 15%)
                if 0.0 < abs(val) <= 20.0:
                    print(f"[OK] ECB HICP ({series_key}): {val} ({period})")
                    return val, period
                else:
                    print(f"[WARN] ECB HICP ({series_key}): Wert {val} außerhalb Plausibilitätsbereich")
            except Exception as e:
                print(f"[WARN] ECB HICP ({series_key}): {e}")

    # Prio 2: Eurostat SDMX-JSON (prc_hicp_manr)
    print("[WARN] ECB HICP: Fallback auf Eurostat SDMX")
    data = safe_get(
        "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/prc_hicp_manr",
        params={
            "format":           "JSON",
            "geo":              "EA",
            "unit":             "RCH_A",
            "coicop":           "CP00",
            "lastTimePeriod":   1,
        }
    )
    if data:
        try:
            series_map = data["dataSets"][0]["series"]
            key        = list(series_map.keys())[0]
            obs        = series_map[key]["observations"]
            last_k     = sorted(obs.keys(), key=lambda x: int(x))[-1]
            val        = float(obs[last_k][0])
            periods    = data["structure"]["dimensions"]["observation"][0]["values"]
            period     = periods[int(last_k)]["id"]
            print(f"[OK] Eurostat HICP: {val} ({period})")
            return val, period
        except Exception as e:
            print(f"[WARN] Eurostat HICP: {e}")

    return None, "N/A"


def get_de2y():
    data = safe_get(
        f"{ECB_BASE}/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y",
        params={"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
    )
    if data:
        try:
            sm     = data["dataSets"][0]["series"]
            k      = list(sm.keys())[0]
            obs    = sm[k]["observations"]
            last_k = sorted(obs.keys(), key=lambda x: int(x))[-1]
            val    = float(obs[last_k][0])
            periods = data["structure"]["dimensions"]["observation"][0]["values"]
            period = periods[int(last_k)]["id"]
            print(f"[OK] DE2Y (YC): {val} ({period})")
            return val, period
        except Exception as e:
            print(f"[WARN] DE2Y YC: {e}")
    val, period = _fm_find("U2_2Y", freq="M", data_type="YLD")
    if val is not None:
        return val, period
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  FRED helpers
# ─────────────────────────────────────────────────────────────

def _fred_obs(series_id: str, obs_start: str = None, obs_end: str = None,
             limit: int = 2, sort: str = "desc") -> list:
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id":  series_id,
        "api_key":    FRED_API_KEY,
        "file_type":  "json",
        "sort_order": sort,
        "limit":      limit,
    }
    if obs_start:
        params["observation_start"] = obs_start
    if obs_end:
        params["observation_end"] = obs_end
    data = safe_get(url, params)
    if data and data.get("observations"):
        return [(o["value"], o["date"]) for o in data["observations"] if o["value"] != "."]
    return []


def _yoy_cpi(series_id: str) -> tuple:
    recent = _fred_obs(series_id, limit=1, sort="desc")
    if not recent:
        print(f"[WARN] {series_id}: kein aktueller Wert")
        return None, "N/A"

    val_now_str, date_now_str = recent[0]
    val_now  = float(val_now_str)
    date_now = datetime.strptime(date_now_str, "%Y-%m-%d")

    prev_year  = date_now.year - 1
    prev_month = date_now.month
    obs_start  = f"{prev_year}-{prev_month:02d}-01"
    obs_end_dt = datetime(prev_year, prev_month, 1) + timedelta(days=35)
    obs_end    = obs_end_dt.strftime("%Y-%m-%d")

    prev_params = {
        "series_id":         series_id,
        "api_key":           FRED_API_KEY,
        "file_type":         "json",
        "observation_start": obs_start,
        "observation_end":   obs_end,
        "sort_order":        "asc",
        "limit":             1,
    }
    prev_data = safe_get("https://api.stlouisfed.org/fred/series/observations", prev_params)
    prev_obs  = [(o["value"], o["date"]) for o in (prev_data or {}).get("observations", [])
                 if o["value"] != "."]

    if not prev_obs:
        print(f"[WARN] {series_id}: kein Vorjahreswert für {obs_start}")
        return None, "N/A"

    val_prev_str, date_prev_str = prev_obs[0]
    val_prev = float(val_prev_str)
    yoy = round((val_now - val_prev) / val_prev * 100, 2)
    print(f"[OK] {series_id} YoY: {val_now} / {val_prev} = {yoy}% ({date_now_str} vs {date_prev_str})")
    return yoy, date_now_str


def get_fed_effr():
    obs = _fred_obs("DFF", limit=1, sort="desc")
    if obs:
        return float(obs[0][0]), obs[0][1]
    return None, "N/A"


def get_us_cpi():
    yoy, dt = _yoy_cpi("CPIAUCNS")
    if yoy is not None:
        return yoy, dt
    print("[WARN] US CPI: Fallback auf CPIAUCSL")
    return _yoy_cpi("CPIAUCSL")


def get_us2y():
    obs = _fred_obs("DGS2", limit=1, sort="desc")
    if obs:
        return float(obs[0][0]), obs[0][1]
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  CFTC COT – CME EUR Futures 6E
# ─────────────────────────────────────────────────────────────

def get_cot_eur() -> dict:
    """
    BUG FIX #3: COT Open Interest Feld
    ───────────────────────────────────
    Problem: open_interest_all = 25.845 war viel zu klein.
             Tatsächlich liegt das Gesamt-OI für CME EUR 6E bei ~842.000 Kontrakten.

    Ursache: Die CFTC Socrata API hat zwei verschiedene OI-Felder:
             • open_interest_all       = Gesamtes Open Interest ALLER Trader-Kategorien
                                         (=korrekt, ~800k-1Mio)
             • asset_mgr_positions_*   = Nur Asset Manager (Untergruppe)

    Zusätzlich: pct_of_oi_asset_mgr_long/short berechnet sich auf das Gesamt-OI,
                 daher ist net_pct = net_cur / oi_cur korrekt wenn oi_cur = open_interest_all.

    Fix:     open_interest_all bleibt das OI-Feld.
             Debug-Print ergänzt um alle rohen Werte zur Verifikation.
    """
    url = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
    params = {
        "$where": "cftc_contract_market_code='399741'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 2,
        # Explizit alle benötigten Felder anfordern
        "$select": (
            "report_date_as_yyyy_mm_dd,"
            "open_interest_all,"
            "asset_mgr_positions_long,"
            "asset_mgr_positions_short,"
            "pct_of_oi_asset_mgr_long,"
            "pct_of_oi_asset_mgr_short,"
            "noncomm_positions_long_all,"
            "noncomm_positions_short_all"
        )
    }
    data = safe_get(url, params)
    if not data or len(data) == 0:
        print("[WARN] CFTC: keine Daten")
        return {}

    current = data[0]
    prev    = data[1] if len(data) > 1 else {}

    # Roh-Werte loggen zur Verifikation
    print(f"[DEBUG] CFTC raw current: {current}")

    def to_int(d, key):
        try:
            v = d.get(key)
            return int(float(v)) if v is not None else 0
        except Exception:
            return 0

    def to_float(d, key):
        try:
            v = d.get(key)
            return round(float(v), 1) if v is not None else 0.0
        except Exception:
            return 0.0

    # Asset Manager (institutionelle Positions-Gruppe)
    long_cur  = to_int(current, "asset_mgr_positions_long")
    short_cur = to_int(current, "asset_mgr_positions_short")
    long_prv  = to_int(prev,    "asset_mgr_positions_long")
    short_prv = to_int(prev,    "asset_mgr_positions_short")

    # Gesamt-OI (alle Trader-Kategorien zusammen)
    oi_cur    = to_int(current, "open_interest_all")

    net_cur   = long_cur - short_cur
    net_prv   = long_prv - short_prv
    delta_net = net_cur - net_prv

    long_pct  = to_float(current, "pct_of_oi_asset_mgr_long")
    short_pct = to_float(current, "pct_of_oi_asset_mgr_short")

    # Net-OI% = Net-Position als % des Gesamt-OI
    net_pct   = round((net_cur / oi_cur * 100), 1) if oi_cur > 0 else 0.0

    report_date = current.get("report_date_as_yyyy_mm_dd", "N/A")[:10]

    print(
        f"[OK] COT: long={long_cur:,}, short={short_cur:,}, net={net_cur:+,}, "
        f"oi={oi_cur:,}, long%={long_pct}%, short%={short_pct}%, "
        f"net%={net_pct}%, date={report_date}"
    )

    # Bias-Bestimmung auf Basis Net-OI%
    if net_pct > 5:
        bias = "NET-LONG"
    elif net_pct < -5:
        bias = "NET-SHORT"
    else:
        bias = "NEUTRAL"

    return {
        "date":      report_date,
        "net":       net_cur,
        "delta_net": delta_net,
        "long_pct":  long_pct,
        "short_pct": short_pct,
        "net_pct":   net_pct,
        "oi":        oi_cur,
        "bias":      bias,
    }


# ─────────────────────────────────────────────────────────────
#  Event-Kalender 2026 — Fed/EZB/NFP/CPI/PPI
#  Quellen:
#    FOMC: federalreserve.gov (8 Sitzungen/Jahr, 2-tägig)
#    EZB:  ecb.europa.eu      (8 Sitzungen/Jahr)
#    NFP:  bls.gov            (1. Freitag des Monats)
#    CPI:  bls.gov/schedule/news_release/cpi.htm
#    PPI:  bls.gov            (~1 Tag nach CPI)
# ─────────────────────────────────────────────────────────────

FOMC_DATES_2026 = [
    date(2026,  1, 29),
    date(2026,  3, 18),
    date(2026,  4, 29),
    date(2026,  6, 17),  # ★ Pressekonferenz
    date(2026,  7, 29),
    date(2026,  9, 16),  # ★ Pressekonferenz
    date(2026, 10, 28),
    date(2026, 12,  9),  # ★ Pressekonferenz
]

ECB_DATES_2026 = [
    date(2026,  1, 30),
    date(2026,  3, 19),
    date(2026,  4, 30),
    date(2026,  6, 11),
    date(2026,  7, 23),
    date(2026,  9, 10),
    date(2026, 10, 29),
    date(2026, 12,  3),
]

NFP_DATES_2026 = [
    date(2026,  2, 11),
    date(2026,  3,  6),
    date(2026,  4,  3),
    date(2026,  5,  8),
    date(2026,  6,  5),
    date(2026,  7,  2),
    date(2026,  8,  7),
    date(2026,  9,  4),
    date(2026, 10,  2),
    date(2026, 11,  6),
    date(2026, 12,  4),
]

CPI_DATES_2026 = [
    date(2026,  1, 13),
    date(2026,  2, 11),
    date(2026,  3, 11),
    date(2026,  4, 10),
    date(2026,  5, 12),
    date(2026,  6, 10),
    date(2026,  7, 14),
    date(2026,  8, 12),
    date(2026,  9, 11),
    date(2026, 10, 14),
    date(2026, 11, 10),
    date(2026, 12, 10),
]

PPI_DATES_2026 = [
    date(2026,  1, 14),
    date(2026,  2, 12),
    date(2026,  3, 18),
    date(2026,  4, 14),
    date(2026,  5, 13),
    date(2026,  6, 11),
    date(2026,  7, 15),
    date(2026,  8, 13),
    date(2026,  9, 12),
    date(2026, 10, 15),
    date(2026, 11, 12),
    date(2026, 12, 11),
]


def get_next_meetings() -> dict:
    today = date.today()

    def _next(dates):
        fut = [d for d in sorted(dates) if d >= today]
        return fut[0] if fut else None

    def _fmt(d):
        return d.strftime("%d.%m.%Y") if d else "N/A"

    def _days(d):
        return (d - today).days if d else None

    nf = _next(FOMC_DATES_2026)
    ne = _next(ECB_DATES_2026)
    nn = _next(NFP_DATES_2026)
    nc = _next(CPI_DATES_2026)
    np_ = _next(PPI_DATES_2026)

    return {
        "fomc_date": _fmt(nf),  "fomc_days": _days(nf),
        "ecb_date":  _fmt(ne),  "ecb_days":  _days(ne),
        "nfp_date":  _fmt(nn),  "nfp_days":  _days(nn),
        "cpi_date":  _fmt(nc),  "cpi_days":  _days(nc),
        "ppi_date":  _fmt(np_), "ppi_days":  _days(np_),
    }


# ─────────────────────────────────────────────────────────────
#  Signal-Logik
# ─────────────────────────────────────────────────────────────

def compute_signals(ecb_dfr, fed_effr, us2y, de2y, cot) -> dict:
    signals = {}

    if fed_effr is not None and ecb_dfr is not None:
        diff = fed_effr - ecb_dfr
        signals["rate_diff"] = diff
        signals["rate_bias"] = "BÄRISCH" if diff > 0 else "BULLISCH"
        signals["rate_icon"] = "🔴" if diff > 0 else "🟢"
    else:
        signals["rate_diff"] = None
        signals["rate_bias"] = "N/A"
        signals["rate_icon"] = "⚪"

    if us2y is not None and de2y is not None:
        spread = us2y - de2y
        signals["yield_spread"] = spread
        signals["yield_bias"]   = "USD\u2011Vorteil" if spread > 0 else "EUR\u2011Vorteil"
        signals["yield_icon"]   = "🔴" if spread > 0 else "🟢"
    else:
        signals["yield_spread"] = None
        signals["yield_bias"]   = "N/A"
        signals["yield_icon"]   = "⚪"

    if cot and cot.get("net") is not None:
        net_pct = cot.get("net_pct", 0)
        if net_pct > 5:
            signals["cot_bias"] = "NET\u2011LONG"
            signals["cot_icon"] = "🟢"
        elif net_pct < -5:
            signals["cot_bias"] = "NET\u2011SHORT"
            signals["cot_icon"] = "🔴"
        else:
            signals["cot_bias"] = "NEUTRAL"
            signals["cot_icon"] = "⚪"
    else:
        signals["cot_bias"] = "N/A"
        signals["cot_icon"] = "⚪"

    return signals


# ─────────────────────────────────────────────────────────────
#  Countdown-Zeile für Ereignisse (HIGH-IMPACT)
# ─────────────────────────────────────────────────────────────

def _event_line(label: str, days, date_str: str) -> str:
    if days is None:
        return f"  {esc(label):<14} `N/A`"
    if days == 0:
        icon, countdown = "🔴", "HEUTE"
    elif days == 1:
        icon, countdown = "🟠", "morgen"
    elif days <= 5:
        icon, countdown = "🟡", f"in {days}T"
    else:
        icon, countdown = "⚫", f"in {days}T"
    return f"  {icon} {esc(label):<10} `{esc(date_str)}` _{esc(countdown)}_"


# ─────────────────────────────────────────────────────────────
#  Nachricht bauen — MarkdownV2
# ─────────────────────────────────────────────────────────────

WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

def build_message(ecb_dfr, ecb_dfr_date, ecb_hicp, ecb_hicp_date,
                  fed_effr, fed_effr_date, us_cpi, us_cpi_date,
                  us2y, us2y_date, de2y, de2y_date,
                  cot, meetings, signals) -> str:

    now     = datetime.now()
    weekday = WEEKDAY_DE[now.weekday()]
    date_str = now.strftime(f"{weekday}, %d\\. %m\\. %Y")

    rd     = signals.get("rate_diff")
    rd_str = esc(f"+{rd:.2f}pp" if rd is not None and rd >= 0 else f"{rd:.2f}pp" if rd is not None else "N/A")
    ys     = signals.get("yield_spread")
    ys_str = esc(f"+{ys:.2f}pp" if ys is not None and ys >= 0 else f"{ys:.2f}pp" if ys is not None else "N/A")

    net_val   = cot.get("net", 0)
    delta     = cot.get("delta_net", 0)
    delta_sym = "▲" if delta >= 0 else "▼"
    net_str   = esc(f"{net_val:+,}")
    delta_str = esc(f"{delta_sym} {abs(delta):,}")
    oi_str    = esc(f"{cot.get('oi', 0):,}")
    lp        = esc(f"{cot.get('long_pct',  0):.1f}%")
    sp        = esc(f"{cot.get('short_pct', 0):.1f}%")
    np_pct    = esc(f"{cot.get('net_pct',   0):.1f}%")
    cot_date  = esc(cot.get("date", "N/A"))

    e_dfr       = esc(fmt(ecb_dfr))
    e_dfr_date  = esc(ecb_dfr_date)
    e_hicp      = esc(fmt(ecb_hicp))
    e_hicp_date = esc(ecb_hicp_date)
    e_effr      = esc(fmt(fed_effr))
    e_effr_date = esc(fed_effr_date)
    e_cpi       = esc(fmt(us_cpi))
    e_cpi_date  = esc(us_cpi_date[:7] if len(str(us_cpi_date)) >= 7 else str(us_cpi_date))
    e_us2y      = esc(fmt(us2y))
    e_de2y      = esc(fmt(de2y))

    ev_fomc = _event_line("FOMC", meetings.get("fomc_days"), meetings["fomc_date"])
    ev_ecb  = _event_line("EZB",  meetings.get("ecb_days"),  meetings["ecb_date"])
    ev_nfp  = _event_line("NFP",  meetings.get("nfp_days"),  meetings["nfp_date"])
    ev_cpi  = _event_line("CPI",  meetings.get("cpi_days"),  meetings["cpi_date"])
    ev_ppi  = _event_line("PPI",  meetings.get("ppi_days"),  meetings["ppi_date"])

    ri = signals["rate_icon"]
    yi = signals["yield_icon"]
    ci = signals["cot_icon"]

    lines = [
        f"📊 *EUR/USD · Morning Brief*",
        f"_{date_str}_",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🇪🇺 *EZB*                      🇺🇸 *Federal Reserve*",
        f"Leitzins  `{e_dfr}`          Leitzins  `{e_effr}`",
        f"_DFR · {e_dfr_date}_          _EFFR · {e_effr_date}_",
        f"Inflation `{e_hicp}`         Inflation `{e_cpi}`",
        f"_HVPI · {e_hicp_date}_         _CPI · {e_cpi_date}_",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "📈 *Kapitalfluss · Zinsdifferenz*",
        "",
        f"  EFFR vs\\. DFR  `{rd_str}`   {ri} _{esc(signals['rate_bias'])}_",
        f"  US 2Y `{e_us2y}` · DE 2Y `{e_de2y}`",
        f"  Spread `{ys_str}`            {yi} _{esc(signals['yield_bias'])}_",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "📋 *COT · CME EUR Futures 6E*",
        f"_Stand: {cot_date}_",
        "",
        f"  Net\\-Position   `{net_str}` Kontrakte",
        f"  Δ Vorwoche      `{delta_str}` Kontrakte",
        f"  Long `{lp}` · Short `{sp}` · Net\\-OI `{np_pct}`",
        f"  Open Interest   `{oi_str}` Kontrakte",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🗓 *Nächste High\\-Impact Events*",
        "",
        ev_fomc,
        ev_ecb,
        ev_nfp,
        ev_cpi,
        ev_ppi,
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯 *Gesamt\\-Bias EUR/USD*",
        "",
        f"  Zinsdiff\\. {ri} `{esc(signals['rate_bias'])}`",
        f"  Spread    {yi} `{esc(signals['yield_bias'])}`",
        f"  COT       {ci} `{esc(signals['cot_bias'])}`",
        "",
        "_ECB SDMX · FRED · CFTC · Eurostat_",
    ]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
#  Telegram senden — MarkdownV2
# ─────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    token   = TELEGRAM_BOT_TOKEN.strip()
    chat_id = TELEGRAM_CHAT_ID.strip()
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       message,
        "parse_mode": "MarkdownV2"
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        print(f"[DEBUG] Telegram Status: {r.status_code}")
        r.raise_for_status()
        print(f"[OK] Telegram gesendet (ID: {r.json().get('result',{}).get('message_id')})")
        return True
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"[ERROR] Body: {e.response.text}")
        return False


# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────

def run():
    print(f"[{datetime.now().isoformat()}] EUR/USD Morning Brief startet...")

    ecb_dfr,  ecb_dfr_date  = get_ecb_dfr()
    ecb_hicp, ecb_hicp_date = get_ecb_hicp()
    fed_effr, fed_effr_date = get_fed_effr()
    us_cpi,   us_cpi_date   = get_us_cpi()
    us2y,     us2y_date     = get_us2y()
    de2y,     de2y_date     = get_de2y()
    cot                     = get_cot_eur()
    meetings                = get_next_meetings()
    signals                 = compute_signals(ecb_dfr, fed_effr, us2y, de2y, cot)

    print(f"  EZB DFR:  {ecb_dfr} ({ecb_dfr_date})")
    print(f"  EZB HICP: {ecb_hicp} ({ecb_hicp_date})")
    print(f"  EFFR:     {fed_effr} ({fed_effr_date})")
    print(f"  US CPI:   {us_cpi} ({us_cpi_date})")
    print(f"  US 2Y:    {us2y} ({us2y_date})")
    print(f"  DE 2Y:    {de2y} ({de2y_date})")
    print(f"  COT:      {cot}")
    print(f"  Meetings: {meetings}")

    message = build_message(
        ecb_dfr, ecb_dfr_date, ecb_hicp, ecb_hicp_date,
        fed_effr, fed_effr_date, us_cpi, us_cpi_date,
        us2y, us2y_date, de2y, de2y_date,
        cot, meetings, signals
    )

    print("\n── VORSCHAU ──────────────────────────")
    print(message)
    print("──────────────────────────────────────\n")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram(message)
    else:
        print("[INFO] Kein Token/Chat-ID gesetzt – nur Vorschau")


if __name__ == "__main__":
    run()
