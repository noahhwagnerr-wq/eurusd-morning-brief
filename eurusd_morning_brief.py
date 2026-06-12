#!/usr/bin/env python3
"""
=============================================================
  EUR/USD Morning Brief Agent
  Täglich live Daten → Telegram-Nachricht

  Quellen:
    ECB SDMX 2.1    data-api.ecb.europa.eu     (DFR, HICP, DE2Y)
    Eurostat JSON   ec.europa.eu/eurostat       (HICP Flash-Schätzung)
    FRED API        api.stlouisfed.org          (EFFR, US CPI, US 2Y)
    CFTC Socrata    publicreporting.cftc.gov    (COT – Disaggregated)

  FIX-HISTORY
  ───────────────────────────────────────────────────────────
  v3.0  Bug #1  DFR-Datum: ECB SDMX lieferte Jahreszahl 2025 statt 2026
                → Datumsformat jetzt explizit als YYYY-MM-DD normiert
        Bug #2  DFR-Wert: FRED-Fallback 1-2T verzögert nach EZB-Entscheid
                → ECB SDMX Key-Direktabfrage hat Priorität
        Bug #3  HICP: ECB SDMX lieferte Dez-2025 (1,9%) statt Mai-2026 (3,2%)
                → Eurostat Flash-Schätzung (prc_hicp_manr) als Primärquelle
                → ECB SDMX ICP/M.U2.N.000000.4.ANR als Fallback
        Bug #4  US CPI: eigene YoY-Berechnung ergab 4,25% statt offiziellem 4,2%
                → FRED CPIAUCSL_PC1 (offizielle YoY-Serie) als Primärquelle
        Bug #5  COT: falsches Dataset (gpe5-46if = Financials/Legacy-Format)
                OI 25.845 statt 842.424, Richtung falsch (Short statt Long)
                → Disaggregated TFF Report (jun7-3zbs, code 099741)
                → Managed Money Long/Short statt Asset Manager
=============================================================
"""

import os
import re
import requests
from datetime import datetime, timedelta, date
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
FRED_API_KEY       = os.getenv("FRED_API_KEY", "")

ECB_BASE = "https://data-api.ecb.europa.eu/service/data"
TODAY    = date.today()


# ─────────────────────────────────────────────────────────────
#  Hilfsfunktionen
# ─────────────────────────────────────────────────────────────

def safe_get(url: str, params: dict = None, timeout: int = 20) -> dict | None:
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[WARN] {url.split('?')[0].split('/')[-1]}: {e}")
        return None


def fmt(value, decimals: int = 2, suffix: str = "%") -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{decimals}f}{suffix}"
    except Exception:
        return str(value)


def esc(text: str) -> str:
    """Escape für Telegram MarkdownV2."""
    return re.sub(r'([_*\[\]()~`>#+=|{}.!\-])', r'\\\1', str(text))


def _ecb_last_obs(series_path: str, params: dict = None) -> tuple[float | None, str]:
    """
    Hilfsfunktion: letzte ECB SDMX-Observation holen.
    Gibt (wert, periode_als_string) zurück.
    BUG #1-FIX: period wird explizit auf YYYY-MM-DD normiert.
    """
    p = {"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
    if params:
        p.update(params)
    data = safe_get(f"{ECB_BASE}/{series_path}", p)
    if not data:
        return None, "N/A"
    try:
        sm      = data["dataSets"][0]["series"]
        key     = list(sm.keys())[0]
        obs     = sm[key]["observations"]
        last_k  = sorted(obs.keys(), key=lambda x: int(x))[-1]
        val     = float(obs[last_k][0])
        periods = data["structure"]["dimensions"]["observation"][0]["values"]
        raw_p   = periods[int(last_k)]["id"]  # z.B. "2026-06-17" oder "2026-06"
        # Normierung: immer YYYY-MM-DD ausgeben
        if len(raw_p) == 7:   # YYYY-MM → ersten Tag nehmen
            norm_p = raw_p + "-01"
        elif len(raw_p) == 4: # YYYY → 01-01
            norm_p = raw_p + "-01-01"
        else:
            norm_p = raw_p     # bereits YYYY-MM-DD
        return val, norm_p
    except Exception as e:
        print(f"[WARN] ECB parse {series_path}: {e}")
        return None, "N/A"


def _fred_obs(series_id: str, limit: int = 1, sort: str = "desc") -> list:
    url = "https://api.stlouisfed.org/fred/series/observations"
    data = safe_get(url, {
        "series_id":  series_id,
        "api_key":    FRED_API_KEY,
        "file_type":  "json",
        "sort_order": sort,
        "limit":      limit,
    })
    if data and data.get("observations"):
        return [(o["value"], o["date"]) for o in data["observations"] if o["value"] != "."]
    return []


# ─────────────────────────────────────────────────────────────
#  EZB – Leitzins (DFR)
#
#  BUG #1 + #2 FIX:
#  Prio 1: ECB SDMX direkter Key FM/B.U2.EUR.4F.KR.DFR.LEV
#          → wird am Beschlusstag selbst aktualisiert (11.06.2026)
#          → Datum-Normierung YYYY-MM-DD behebt Jahreszahlenfehler
#  Prio 2: ECB SDMX FM-Dataflow
#  Prio 3: FRED ECBDFR (Fallback, 1-2T Verzögerung)
# ─────────────────────────────────────────────────────────────

def get_ecb_dfr() -> tuple[float | None, str]:
    # Prio 1: direkter ECB-Key (Business- und Daily-Frequenz versuchen)
    for key in ("FM/B.U2.EUR.4F.KR.DFR.LEV", "FM/D.U2.EUR.4F.KR.DFR.LEV"):
        val, period = _ecb_last_obs(
            key,
            {"endPeriod": TODAY.strftime("%Y-%m-%d"), "lastNObservations": 1}
        )
        if val is not None:
            print(f"[OK] EZB DFR: {val}% ({period}) via {key}")
            return val, period

    # Prio 2: FRED ECBDFR
    print("[WARN] EZB DFR: ECB-API kein Treffer → FRED-Fallback (evtl. verzögert!)")
    obs = _fred_obs("ECBDFR", limit=1)
    if obs:
        val, dt = float(obs[0][0]), obs[0][1]
        print(f"[OK] EZB DFR (FRED): {val}% ({dt}) – ACHTUNG evtl. 1-2T verzögert")
        return val, dt

    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  EZB – HICP Inflation YoY
#
#  BUG #3 FIX:
#  Das Problem: ECB SDMX lieferte Dec-2025 (1,9%) statt Mai-2026 (3,2%).
#  Ursache: Die ECB veröffentlicht HICP mit ~30T Verzögerung;
#           Flash-Schätzungen von Eurostat erscheinen ~2 Wochen früher.
#
#  Prio 1: Eurostat prc_hicp_manr (Flash + finaler Wert, Euroraum EA)
#          → aktuellste verfügbare offizielle Zahl (2. Juni 2026 = 3,2%)
#  Prio 2: ECB SDMX ICP/M.U2.N.000000.4.ANR (Annual Rate of Change)
# ─────────────────────────────────────────────────────────────

def get_ecb_hicp() -> tuple[float | None, str]:
    # Prio 1: Eurostat SDMX-JSON
    data = safe_get(
        "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/prc_hicp_manr",
        {"format": "JSON", "geo": "EA", "unit": "RCH_A", "coicop": "CP00",
         "lastTimePeriod": 1}
    )
    if data:
        try:
            sm     = data["dataSets"][0]["series"]
            k      = list(sm.keys())[0]
            obs    = sm[k]["observations"]
            last_k = sorted(obs.keys(), key=lambda x: int(x))[-1]
            val    = float(obs[last_k][0])
            period = data["structure"]["dimensions"]["observation"][0]["values"][int(last_k)]["id"]
            if 0.0 < abs(val) <= 25.0:
                print(f"[OK] HICP Eurostat Flash: {val}% ({period})")
                return val, period
        except Exception as e:
            print(f"[WARN] Eurostat HICP: {e}")

    # Prio 2: ECB SDMX
    print("[WARN] Eurostat HICP fehlgeschlagen → ECB SDMX ICP")
    val, period = _ecb_last_obs("ICP/M.U2.N.000000.4.ANR")
    if val is not None and 0.0 < abs(val) <= 25.0:
        print(f"[OK] HICP ECB SDMX: {val}% ({period})")
        return val, period

    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  DE 2Y Staatsanleiherendite
# ─────────────────────────────────────────────────────────────

def get_de2y() -> tuple[float | None, str]:
    val, period = _ecb_last_obs("YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y")
    if val is not None:
        print(f"[OK] DE2Y: {val}% ({period})")
        return val, period
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  Fed – EFFR
# ─────────────────────────────────────────────────────────────

def get_fed_effr() -> tuple[float | None, str]:
    obs = _fred_obs("DFF", limit=1)
    if obs:
        print(f"[OK] EFFR: {obs[0][0]}% ({obs[0][1]})")
        return float(obs[0][0]), obs[0][1]
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  US CPI YoY
#
#  BUG #4 FIX:
#  Problem: eigene (val_now - val_prev)/val_prev Berechnung ergab 4,25%
#           statt dem offiziellen BLS-Wert 4,2% (Rundungsfehler +0,05pp)
#
#  Fix: FRED CPIAUCSL_PC1 = offizielle BLS 12-Monats-Veränderungsrate
#       Diese Serie IST der veröffentlichte CPI YoY-Wert, keine Berechnung.
#       Fallback: CPIAUCNS_PC1 (nicht-saisonbereinigt, ebenfalls offiziell)
# ─────────────────────────────────────────────────────────────

def get_us_cpi() -> tuple[float | None, str]:
    # Direktserie: offizieller YoY-Prozentwert (keine eigene Berechnung!)
    for series_id in ("CPIAUCSL_PC1", "CPIAUCNS_PC1"):
        obs = _fred_obs(series_id, limit=1)
        if obs:
            val, dt = float(obs[0][0]), obs[0][1]
            print(f"[OK] US CPI YoY ({series_id}): {val}% ({dt})")
            return val, dt
    print("[WARN] US CPI: alle FRED-YoY-Serien fehlgeschlagen")
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  US 2Y Treasury
# ─────────────────────────────────────────────────────────────

def get_us2y() -> tuple[float | None, str]:
    obs = _fred_obs("DGS2", limit=1)
    if obs:
        print(f"[OK] US2Y: {obs[0][0]}% ({obs[0][1]})")
        return float(obs[0][0]), obs[0][1]
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  CFTC COT – CME EUR Futures (Disaggregated / TFF)
#
#  BUG #5 FIX – 3 Fehler in einem:
#  ─────────────────────────────────────────────────────────────
#  Fehler A: Falsches Dataset
#    gpe5-46if = "Legacy Futures" (nur 3 Trader-Klassen, altes Format)
#    Korrekt:   jun7-3zbs = "Traders in Financial Futures (TFF)" –
#               Disaggregated Report mit 6 Trader-Klassen
#
#  Fehler B: Falscher Markt-Code
#    Code 399741 = falsch für EUR FX
#    Korrekt:    099741 = "EURO FX" im TFF-Disaggregated-Report (CME)
#
#  Fehler C: Falsche Trader-Kategorie
#    Asset Manager ≠ spekulativ für FX-Märkte
#    Korrekt:    "Managed Money" (lev_money_positions_long/short)
#                = Hedge Funds & CTAs, die engste Entsprechung zu
#                  "Non-Commercial/Spekulativ" im Legacy-Format
#
#  COT-Veröffentlichungslogik:
#    Report erscheint jeden Freitag 15:30 ET mit Daten per DIENSTAG.
#    Ein Morning Brief am Freitag kann maximal Dienstag-Daten des
#    VORHERIGEN Berichts enthalten (Veröffentlichung erst 15:30 ET).
#    Daher: immer neuesten verfügbaren Bericht nehmen, Datum anzeigen.
# ─────────────────────────────────────────────────────────────

def get_cot_eur() -> dict:
    url = "https://publicreporting.cftc.gov/resource/jun7-3zbs.json"
    params = {
        # Code 099741 = EURO FX im TFF Disaggregated Report
        "$where": "cftc_contract_market_code='099741'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 2,
        "$select": (
            "report_date_as_yyyy_mm_dd,"
            "open_interest_all,"
            "lev_money_positions_long_all,"
            "lev_money_positions_short_all,"
            "pct_of_oi_lev_money_long_all,"
            "pct_of_oi_lev_money_short_all,"
            "change_in_lev_money_long,"
            "change_in_lev_money_short"
        )
    }
    data = safe_get(url, params)

    if not data:
        # Backup: Legacy-Format als letzter Ausweg
        print("[WARN] COT TFF-Dataset fehlgeschlagen → Legacy-Fallback")
        return _get_cot_legacy()

    if len(data) == 0:
        print("[WARN] COT: TFF-Dataset leer für code 099741")
        return _get_cot_legacy()

    current = data[0]
    prev    = data[1] if len(data) > 1 else {}

    print(f"[DEBUG] COT raw (TFF/jun7-3zbs): {current}")

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

    long_cur  = to_int(current, "lev_money_positions_long_all")
    short_cur = to_int(current, "lev_money_positions_short_all")
    long_prv  = to_int(prev,    "lev_money_positions_long_all")
    short_prv = to_int(prev,    "lev_money_positions_short_all")
    oi_cur    = to_int(current, "open_interest_all")

    net_cur   = long_cur - short_cur
    net_prv   = long_prv - short_prv
    delta_net = net_cur - net_prv

    long_pct  = to_float(current, "pct_of_oi_lev_money_long_all")
    short_pct = to_float(current, "pct_of_oi_lev_money_short_all")
    net_pct   = round((net_cur / oi_cur * 100), 1) if oi_cur > 0 else 0.0

    report_date = current.get("report_date_as_yyyy_mm_dd", "N/A")[:10]

    print(
        f"[OK] COT (TFF Managed Money): long={long_cur:,}, short={short_cur:,}, "
        f"net={net_cur:+,}, oi={oi_cur:,}, net%={net_pct}%, date={report_date}"
    )

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
        "source":    "TFF/Managed Money",
    }


def _get_cot_legacy() -> dict:
    """Legacy-Fallback: gpe5-46if, Non-Commercial (spekulativ)."""
    url = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
    params = {
        "$where": "cftc_contract_market_code='099741'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 2,
        "$select": (
            "report_date_as_yyyy_mm_dd,open_interest_all,"
            "noncomm_positions_long_all,noncomm_positions_short_all,"
            "pct_of_oi_noncomm_long_all,pct_of_oi_noncomm_short_all"
        )
    }
    data = safe_get(url, params)
    if not data or len(data) == 0:
        print("[WARN] COT Legacy auch fehlgeschlagen")
        return {}

    current = data[0]
    prev    = data[1] if len(data) > 1 else {}
    print(f"[DEBUG] COT raw (Legacy): {current}")

    def to_int(d, k):
        try:
            v = d.get(k)
            return int(float(v)) if v else 0
        except Exception:
            return 0

    long_cur  = to_int(current, "noncomm_positions_long_all")
    short_cur = to_int(current, "noncomm_positions_short_all")
    long_prv  = to_int(prev,    "noncomm_positions_long_all")
    short_prv = to_int(prev,    "noncomm_positions_short_all")
    oi_cur    = to_int(current, "open_interest_all")
    net_cur   = long_cur - short_cur
    net_prv   = long_prv - short_prv
    net_pct   = round((net_cur / oi_cur * 100), 1) if oi_cur > 0 else 0.0

    return {
        "date":      current.get("report_date_as_yyyy_mm_dd", "N/A")[:10],
        "net":       net_cur,
        "delta_net": net_cur - net_prv,
        "long_pct":  round(float(current.get("pct_of_oi_noncomm_long_all", 0) or 0), 1),
        "short_pct": round(float(current.get("pct_of_oi_noncomm_short_all", 0) or 0), 1),
        "net_pct":   net_pct,
        "oi":        oi_cur,
        "bias":      "NET-LONG" if net_pct > 5 else "NET-SHORT" if net_pct < -5 else "NEUTRAL",
        "source":    "Legacy/Non-Commercial",
    }


# ─────────────────────────────────────────────────────────────
#  Event-Kalender 2026 — Fed / EZB / NFP / CPI / PPI
# ─────────────────────────────────────────────────────────────

FOMC_DATES_2026 = [
    date(2026,  1, 29), date(2026,  3, 18), date(2026,  4, 29),
    date(2026,  6, 17), date(2026,  7, 29), date(2026,  9, 16),
    date(2026, 10, 28), date(2026, 12,  9),
]
ECB_DATES_2026 = [
    date(2026,  1, 30), date(2026,  3, 19), date(2026,  4, 30),
    date(2026,  6, 11), date(2026,  7, 23), date(2026,  9, 10),
    date(2026, 10, 29), date(2026, 12,  3),
]
NFP_DATES_2026 = [
    date(2026,  2, 11), date(2026,  3,  6), date(2026,  4,  3),
    date(2026,  5,  8), date(2026,  6,  5), date(2026,  7,  2),
    date(2026,  8,  7), date(2026,  9,  4), date(2026, 10,  2),
    date(2026, 11,  6), date(2026, 12,  4),
]
CPI_DATES_2026 = [
    date(2026,  1, 13), date(2026,  2, 11), date(2026,  3, 11),
    date(2026,  4, 10), date(2026,  5, 12), date(2026,  6, 10),
    date(2026,  7, 14), date(2026,  8, 12), date(2026,  9, 11),
    date(2026, 10, 14), date(2026, 11, 10), date(2026, 12, 10),
]
PPI_DATES_2026 = [
    date(2026,  1, 14), date(2026,  2, 12), date(2026,  3, 18),
    date(2026,  4, 14), date(2026,  5, 13), date(2026,  6, 11),
    date(2026,  7, 15), date(2026,  8, 13), date(2026,  9, 12),
    date(2026, 10, 15), date(2026, 11, 12), date(2026, 12, 11),
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

    nf, ne  = _next(FOMC_DATES_2026), _next(ECB_DATES_2026)
    nn, nc  = _next(NFP_DATES_2026),  _next(CPI_DATES_2026)
    np_     = _next(PPI_DATES_2026)

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
        diff = round(fed_effr - ecb_dfr, 2)
        signals.update({
            "rate_diff": diff,
            "rate_bias": "BÄRISCH" if diff > 0 else "BULLISCH",
            "rate_icon": "🔴" if diff > 0 else "🟢",
        })
    else:
        signals.update({"rate_diff": None, "rate_bias": "N/A", "rate_icon": "⚪"})

    if us2y is not None and de2y is not None:
        spread = round(us2y - de2y, 2)
        signals.update({
            "yield_spread": spread,
            "yield_bias":   "USD\u2011Vorteil" if spread > 0 else "EUR\u2011Vorteil",
            "yield_icon":   "🔴" if spread > 0 else "🟢",
        })
    else:
        signals.update({"yield_spread": None, "yield_bias": "N/A", "yield_icon": "⚪"})

    if cot and cot.get("net") is not None:
        net_pct = cot.get("net_pct", 0)
        signals.update({
            "cot_bias": "NET\u2011LONG" if net_pct > 5 else
                        "NET\u2011SHORT" if net_pct < -5 else "NEUTRAL",
            "cot_icon": "🟢" if net_pct > 5 else "🔴" if net_pct < -5 else "⚪",
        })
    else:
        signals.update({"cot_bias": "N/A", "cot_icon": "⚪"})

    return signals


# ─────────────────────────────────────────────────────────────
#  Event-Zeile mit Farbcountdown
# ─────────────────────────────────────────────────────────────

def _event_line(label: str, days, date_str: str) -> str:
    if days is None:
        return f"  {esc(label):<14} `N/A`"
    if days == 0:
        icon, cd = "🔴", "HEUTE"
    elif days == 1:
        icon, cd = "🟠", "morgen"
    elif days <= 5:
        icon, cd = "🟡", f"in {days}T"
    else:
        icon, cd = "⚫", f"in {days}T"
    return f"  {icon} {esc(label):<10} `{esc(date_str)}` _{esc(cd)}_"


# ─────────────────────────────────────────────────────────────
#  Nachricht bauen – MarkdownV2
# ─────────────────────────────────────────────────────────────

WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def build_message(
    ecb_dfr, ecb_dfr_date, ecb_hicp, ecb_hicp_date,
    fed_effr, fed_effr_date, us_cpi, us_cpi_date,
    us2y, us2y_date, de2y, de2y_date,
    cot, meetings, signals
) -> str:

    now      = datetime.now()
    weekday  = WEEKDAY_DE[now.weekday()]
    date_str = now.strftime(f"{weekday}, %d\\. %m\\. %Y")

    rd     = signals.get("rate_diff")
    rd_str = esc(f"+{rd:.2f}pp" if rd is not None and rd >= 0
                 else f"{rd:.2f}pp" if rd is not None else "N/A")
    ys     = signals.get("yield_spread")
    ys_str = esc(f"+{ys:.2f}pp" if ys is not None and ys >= 0
                 else f"{ys:.2f}pp" if ys is not None else "N/A")

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
    cot_src   = esc(cot.get("source", "N/A"))

    e_dfr       = esc(fmt(ecb_dfr))
    e_dfr_date  = esc(ecb_dfr_date[:10] if ecb_dfr_date and len(ecb_dfr_date) >= 10
                      else ecb_dfr_date or "N/A")
    e_hicp      = esc(fmt(ecb_hicp))
    e_hicp_date = esc(ecb_hicp_date[:7] if ecb_hicp_date and len(ecb_hicp_date) >= 7
                      else ecb_hicp_date or "N/A")
    e_effr      = esc(fmt(fed_effr))
    e_effr_date = esc(fed_effr_date[:10] if fed_effr_date and len(fed_effr_date) >= 10
                      else fed_effr_date or "N/A")
    e_cpi       = esc(fmt(us_cpi))
    e_cpi_date  = esc(us_cpi_date[:7] if us_cpi_date and len(str(us_cpi_date)) >= 7
                      else str(us_cpi_date) if us_cpi_date else "N/A")
    e_us2y      = esc(fmt(us2y))
    e_de2y      = esc(fmt(de2y))

    ri = signals["rate_icon"]
    yi = signals["yield_icon"]
    ci = signals["cot_icon"]

    lines = [
        "📊 *EUR/USD · Morning Brief*",
        f"_{date_str}_",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🇪🇺 *EZB*",
        f"Leitzins \\(DFR\\)  `{e_dfr}`  \\| Stand `{e_dfr_date}`",
        f"Inflation HVPI   `{e_hicp}` \\| Stand `{e_hicp_date}`",
        "",
        "🇺🇸 *Federal Reserve*",
        f"Leitzins \\(EFFR\\) `{e_effr}`  \\| Stand `{e_effr_date}`",
        f"Inflation CPI    `{e_cpi}` \\| Stand `{e_cpi_date}`",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "📈 *Kapitalfluss · Zinsdifferenz*",
        "",
        f"  EFFR vs\\. DFR  `{rd_str}`  {ri} _{esc(signals['rate_bias'])}_",
        f"  US 2Y `{e_us2y}` · DE 2Y `{e_de2y}`",
        f"  2Y\\-Spread   `{ys_str}`  {yi} _{esc(signals['yield_bias'])}_",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "📋 *COT · CME EUR Futures 6E*",
        f"_Stand: {cot_date} · Quelle: {cot_src}_",
        "",
        f"  Net\\-Position \\(MM\\)  `{net_str}` Kontrakte",
        f"  Δ Vorwoche            `{delta_str}` Kontrakte",
        f"  Long `{lp}` · Short `{sp}` · Net\\-OI `{np_pct}`",
        f"  Open Interest         `{oi_str}` Kontrakte",
        f"  Bias: {ci} `{esc(cot.get('bias', 'N/A'))}`",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🗓 *Nächste High\\-Impact Events*",
        "",
        _event_line("FOMC", meetings.get("fomc_days"), meetings["fomc_date"]),
        _event_line("EZB",  meetings.get("ecb_days"),  meetings["ecb_date"]),
        _event_line("NFP",  meetings.get("nfp_days"),  meetings["nfp_date"]),
        _event_line("CPI",  meetings.get("cpi_days"),  meetings["cpi_date"]),
        _event_line("PPI",  meetings.get("ppi_days"),  meetings["ppi_date"]),
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🎯 *Gesamt\\-Bias EUR/USD*",
        "",
        f"  Zinsdiff\\. {ri} `{esc(signals['rate_bias'])}`",
        f"  2Y\\-Spread {yi} `{esc(signals['yield_bias'])}`",
        f"  COT        {ci} `{esc(signals['cot_bias'])}`",
        "",
        "_ECB SDMX · Eurostat · FRED · CFTC TFF_",
    ]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
#  Telegram senden
# ─────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN.strip()}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID.strip(),
        "text":       message,
        "parse_mode": "MarkdownV2",
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        print(f"[OK] Telegram gesendet (msg_id={r.json().get('result', {}).get('message_id')})")
        return True
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")
        if hasattr(e, "response") and e.response is not None:
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

    print("\n── DATENPUNKTE ───────────────────────")
    print(f"  EZB DFR:  {ecb_dfr}% ({ecb_dfr_date})")
    print(f"  EZB HICP: {ecb_hicp}% ({ecb_hicp_date})")
    print(f"  EFFR:     {fed_effr}% ({fed_effr_date})")
    print(f"  US CPI:   {us_cpi}% ({us_cpi_date})")
    print(f"  US 2Y:    {us2y}% ({us2y_date})")
    print(f"  DE 2Y:    {de2y}% ({de2y_date})")
    print(f"  COT:      net={cot.get('net')}, oi={cot.get('oi')}, bias={cot.get('bias')}, src={cot.get('source')}")
    print(f"  Signale:  rateDiff={signals.get('rate_diff')} spread={signals.get('yield_spread')}")
    print("──────────────────────────────────────")

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
        print("[INFO] Kein Token/Chat-ID → nur Vorschau")


if __name__ == "__main__":
    run()
