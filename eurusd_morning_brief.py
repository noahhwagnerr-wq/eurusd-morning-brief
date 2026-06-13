#!/usr/bin/env python3
"""
=============================================================
  EUR/USD Morning Brief Agent  v4.1
  Täglich live Daten → Telegram-Nachricht

  Quellen:
    ECB SDMX 2.1    data-api.ecb.europa.eu     (DFR, DE2Y)
    Eurostat JSON   ec.europa.eu/eurostat       (HICP Flash)
    FRED API        api.stlouisfed.org          (EFFR, US CPI, US 2Y)
    CFTC Socrata    publicreporting.cftc.gov    (COT – TFF Disaggregated)

  FIX-LOG v4.1  (Hotfixes)
  ─────────────────────────────────────────────────────────
  #3  HICP zeigt noch 1.90% (Dez-2025)
      Ursache: ei_cphi_m liefert MoM, nicht YoY; prc_hicp_manr
               ohne Filter liefert noch immer finalen Dez-Wert.
      Fix:     Hard-pin: Falls heute >= 2026-06-02 und kein
               API-Wert >= 2.0% für 2026-05 vorliegt,
               Eurostat-Flash 3.2% (Mai 2026) verwenden.
               Gilt bis zur finalen Veröffentlichung (~30.06.).

  #4  US CPI Abweichung vom BLS-Headline
      Ursache: CPIAUCSL (saisonbereinigt) ergibt leicht andere
               YoY als der nicht-saisonbereinigte BLS-Headline.
      Fix:     Primär CPIAUCNS (nicht saisonbereinigt, wie BLS
               Headline CPI), Fallback CPIAUCSL.
               Rundung auf 1 Dezimalstelle.

  #5  COT liefert 0/0/0 trotz Legacy-Fallback
      Ursache: Legacy-Dataset (gpe5-46if) existiert unter dem
               Feldnamen 'noncomm_positions_long_all', aber
               pct-Felder sind leer → long_pct/short_pct = 0.0
               korrekt, aber net bleibt 0 wenn Parsing fehlschlägt.
      Fix:     Expliziter None-Check vor Rückgabe.
               Wenn net==0 UND oi==0: leeres Dict zurückgeben
               damit build_message "N/A" zeigt statt "0".
=============================================================
"""

import os
import re
import requests
from datetime import datetime, date
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
FRED_API_KEY       = os.getenv("FRED_API_KEY", "")

ECB_BASE = "https://data-api.ecb.europa.eu/service/data"
TODAY    = date.today()

# Bekannte EZB-Entscheide: (Beschlussdatum, neuer DFR, Inkrafttreten)
_ECB_KNOWN_DECISIONS = [
    ("2026-06-11", 2.25, "2026-06-17"),
]

# Eurostat HICP Flash-Pins (Quelle: offizielle Pressemitteilungen)
# Format: (gültig_ab, gültig_bis_exklusiv, wert, periode_str)
_HICP_FLASH_PINS = [
    (date(2026, 6, 2), date(2026, 7, 1), 3.2, "2026-05"),
]


# ─────────────────────────────────────────────────────────────
#  Hilfsfunktionen
# ─────────────────────────────────────────────────────────────

def safe_get(url: str, params: dict = None, timeout: int = 20):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        label = url.split("?")[0].split("/")[-1][:60]
        print(f"[WARN] {label}: {e}")
        return None


def fmt(value, decimals: int = 2, suffix: str = "%") -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{decimals}f}{suffix}"
    except Exception:
        return str(value)


def esc(text: str) -> str:
    return re.sub(r'([_*\[\]()~`>#+=|{}.!\-])', r'\\\1', str(text))


def _fix_year(period_str: str) -> str:
    if not period_str or len(period_str) < 4:
        return period_str
    try:
        year = int(period_str[:4])
        if year < TODAY.year - 1:
            corrected = str(TODAY.year) + period_str[4:]
            print(f"[FIX] Jahreszahl korrigiert: {period_str} → {corrected}")
            return corrected
    except Exception:
        pass
    return period_str


def _parse_ym(period_str: str) -> date:
    """YYYY-MM oder YYYY-MM-DD → date für Sortierung."""
    try:
        if len(period_str) == 7:
            return datetime.strptime(period_str + "-01", "%Y-%m-%d").date()
        if len(period_str) >= 10:
            return datetime.strptime(period_str[:10], "%Y-%m-%d").date()
    except Exception:
        pass
    return date(1900, 1, 1)


def _ecb_last_obs(series_path: str, extra_params: dict = None):
    """Letzte ECB SDMX Observation. Gibt (wert, periode) zurück."""
    p = {"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
    if extra_params:
        p.update(extra_params)
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
        raw_p   = periods[int(last_k)]["id"]
        if len(raw_p) == 7:
            raw_p += "-01"
        elif len(raw_p) == 4:
            raw_p += "-01-01"
        raw_p = _fix_year(raw_p)
        return val, raw_p
    except Exception as e:
        print(f"[WARN] ECB parse {series_path}: {e}")
        return None, "N/A"


def _fred_obs(series_id: str, limit: int = 2, sort: str = "desc") -> list:
    data = safe_get(
        "https://api.stlouisfed.org/fred/series/observations",
        {
            "series_id":  series_id,
            "api_key":    FRED_API_KEY,
            "file_type":  "json",
            "sort_order": sort,
            "limit":      limit,
        },
    )
    if data and data.get("observations"):
        return [(o["value"], o["date"])
                for o in data["observations"] if o["value"] != "."]
    return []


# ─────────────────────────────────────────────────────────────
#  EZB – Leitzins (DFR)
# ─────────────────────────────────────────────────────────────

def get_ecb_dfr():
    """Gibt (dfr_wert, datum_str, hinweis_str) zurück."""
    val, period = None, "N/A"
    for key in ("FM/B.U2.EUR.4F.KR.DFR.LEV", "FM/D.U2.EUR.4F.KR.DFR.LEV"):
        v, p = _ecb_last_obs(
            key, {"endPeriod": TODAY.strftime("%Y-%m-%d"), "lastNObservations": 1}
        )
        if v is not None:
            val, period = v, p
            print(f"[OK] EZB DFR (ECB SDMX): {val}% ({period})")
            break

    if val is None:
        obs = _fred_obs("ECBDFR", limit=1)
        if obs:
            val, period = float(obs[0][0]), obs[0][1]
            print(f"[OK] EZB DFR (FRED): {val}% ({period}) – evtl. verzögert")

    hinweis = None
    for dec_date, new_dfr, eff_date in _ECB_KNOWN_DECISIONS:
        eff_d = date.fromisoformat(eff_date)
        dec_d = date.fromisoformat(dec_date)
        if dec_d <= TODAY < eff_d and (val is None or abs(val - new_dfr) > 0.001):
            print(f"[INFO] EZB DFR: beschlossen {new_dfr}% ab {eff_date}, "
                  f"aktuell gültig {val}% – zeige beschlossenen Wert")
            val     = new_dfr
            period  = dec_date
            hinweis = f"in Kraft ab {eff_date}"

    return val, period, hinweis


# ─────────────────────────────────────────────────────────────
#  EZB – HICP Inflation YoY
#
#  v4.1 Hotfix #3:
#  Hard-Pin aus Eurostat-Pressemitteilung wenn API-Wert
#  veraltet oder unplausibel ist.
# ─────────────────────────────────────────────────────────────

def get_ecb_hicp():
    # Schritt 1: API-Versuch (prc_hicp_manr, alle Perioden)
    api_val, api_period = _hicp_from_api()

    # Schritt 2: Prüfen ob ein Hard-Pin gilt und API-Wert veraltet ist
    for (valid_from, valid_until, pin_val, pin_period) in _HICP_FLASH_PINS:
        if valid_from <= TODAY < valid_until:
            pin_d = _parse_ym(pin_period)
            api_d = _parse_ym(api_period) if api_period != "N/A" else date(1900, 1, 1)
            if api_d < pin_d:
                print(f"[PIN] HICP: API liefert {api_val}% ({api_period}), "
                      f"Flash-Pin {pin_val}% ({pin_period}) ist aktueller → verwende Pin")
                return pin_val, pin_period
    return api_val, api_period


def _hicp_from_api():
    """Versucht HICP aus Eurostat-API zu holen."""
    eurostat_base = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data"

    # Versuch 1: prc_hicp_manr (finale + Flash-Werte, YoY)
    for params in [
        {"format": "JSON", "geo": "EA", "unit": "RCH_A", "coicop": "CP00"},
        {"format": "JSON", "geo": "EA", "unit": "RCH_A", "coicop": "CP00",
         "startPeriod": "2025-06"},
    ]:
        data = safe_get(f"{eurostat_base}/prc_hicp_manr", params)
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
                        v     = float(ov[0])
                        if 0.0 < abs(v) <= 25.0:
                            candidates.append((_parse_ym(p_str), v, p_str))
                if candidates:
                    candidates.sort(key=lambda x: x[0], reverse=True)
                    val, p_str = candidates[0][1], candidates[0][2]
                    print(f"[OK] HICP (prc_hicp_manr): {val}% ({p_str})")
                    return val, p_str
            except Exception as e:
                print(f"[WARN] Eurostat prc_hicp_manr: {e}")

    # Versuch 2: ECB SDMX ICP
    val, period = _ecb_last_obs("ICP/M.U2.N.000000.4.ANR")
    if val is not None and 0.0 < abs(val) <= 25.0:
        print(f"[OK] HICP (ECB SDMX ICP): {val}% ({period})")
        return val, period

    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  DE 2Y Staatsanleiherendite
# ─────────────────────────────────────────────────────────────

def get_de2y():
    val, period = _ecb_last_obs("YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y")
    if val is not None:
        print(f"[OK] DE2Y: {val}% ({period})")
        return val, period
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  Fed – EFFR
# ─────────────────────────────────────────────────────────────

def get_fed_effr():
    obs = _fred_obs("DFF", limit=1)
    if obs:
        print(f"[OK] EFFR: {obs[0][0]}% ({obs[0][1]})")
        return float(obs[0][0]), obs[0][1]
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  US CPI YoY
#
#  v4.1 Hotfix #4:
#  Primär CPIAUCNS (nicht saisonbereinigt) = BLS Headline.
#  Fallback CPIAUCSL (saisonbereinigt).
#  Beides: (aktuell - vor 12M) / vor 12M * 100, auf 1 Dez.
# ─────────────────────────────────────────────────────────────

def get_us_cpi():
    for series_id in ("CPIAUCNS", "CPIAUCSL"):
        obs = _fred_obs(series_id, limit=14, sort="desc")
        if len(obs) < 13:
            print(f"[WARN] US CPI {series_id}: nur {len(obs)} Obs, brauche 13")
            continue
        try:
            val_now  = float(obs[0][0])
            date_now = obs[0][1]
            val_prev = float(obs[12][0])
            yoy      = round((val_now - val_prev) / val_prev * 100, 1)
            print(f"[OK] US CPI YoY ({series_id}): "
                  f"{val_now}/{val_prev} = {yoy}% ({date_now})")
            return yoy, date_now
        except Exception as e:
            print(f"[WARN] US CPI {series_id} Berechnung: {e}")
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  US 2Y Treasury
# ─────────────────────────────────────────────────────────────

def get_us2y():
    obs = _fred_obs("DGS2", limit=1)
    if obs:
        print(f"[OK] US2Y: {obs[0][0]}% ({obs[0][1]})")
        return float(obs[0][0]), obs[0][1]
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  CFTC COT – CME EUR Futures (TFF Disaggregated)
#
#  v4.1 Hotfix #5:
#  Wenn TFF oder Legacy 0/0/0 zurückgibt: leeres Dict,
#  damit build_message "N/A" zeigt statt 0-Werte.
# ─────────────────────────────────────────────────────────────

_MM_LONG_FIELDS  = [
    "lev_money_long_all",
    "lev_money_positions_long_all",
    "m_money_positions_long_all",
    "managed_money_long",
]
_MM_SHORT_FIELDS = [
    "lev_money_short_all",
    "lev_money_positions_short_all",
    "m_money_positions_short_all",
    "managed_money_short",
]


def _find_field(record: dict, candidates: list) -> tuple:
    for name in candidates:
        if name in record and record[name] is not None:
            try:
                return name, int(float(record[name]))
            except Exception:
                pass
    return None, 0


def get_cot_eur() -> dict:
    url    = "https://publicreporting.cftc.gov/resource/jun7-3zbs.json"
    result = {}

    for market_code in ("099741", "99741"):
        data = safe_get(url, {
            "$where": f"cftc_contract_market_code='{market_code}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 2,
        })
        if data and len(data) > 0:
            result = _parse_tff(data)
            if result:
                return result

    print("[WARN] COT TFF leer oder 0/0 → Legacy-Fallback")
    return _get_cot_legacy()


def _parse_tff(data: list) -> dict:
    if not data:
        return {}
    current = data[0]
    prev    = data[1] if len(data) > 1 else {}

    long_field,  long_cur  = _find_field(current, _MM_LONG_FIELDS)
    short_field, short_cur = _find_field(current, _MM_SHORT_FIELDS)
    _,           long_prv  = _find_field(prev,    _MM_LONG_FIELDS)
    _,           short_prv = _find_field(prev,    _MM_SHORT_FIELDS)

    print(f"[DEBUG] COT TFF: long_field={long_field}, "
          f"long={long_cur}, short={short_cur}")

    oi_cur = int(float(current.get("open_interest_all", 0) or 0))

    # Hotfix: 0/0/0 erkennen → leeres Dict
    if long_cur == 0 and short_cur == 0 and oi_cur == 0:
        print("[WARN] COT TFF: alle Werte 0 → Fallback")
        return {}

    net_cur   = long_cur - short_cur
    net_prv   = long_prv - short_prv
    net_pct   = round((net_cur / oi_cur * 100), 1) if oi_cur > 0 else 0.0
    long_pct  = round(long_cur  / oi_cur * 100, 1) if oi_cur > 0 else 0.0
    short_pct = round(short_cur / oi_cur * 100, 1) if oi_cur > 0 else 0.0

    # Versuche offizielle Prozentfelder
    for lp_f in ("pct_of_oi_lev_money_long_all", "pct_of_oi_m_money_long_all"):
        v = current.get(lp_f)
        if v:
            try: long_pct  = round(float(v), 1); break
            except Exception: pass
    for sp_f in ("pct_of_oi_lev_money_short_all", "pct_of_oi_m_money_short_all"):
        v = current.get(sp_f)
        if v:
            try: short_pct = round(float(v), 1); break
            except Exception: pass

    bias = "NET-LONG" if net_pct > 5 else "NET-SHORT" if net_pct < -5 else "NEUTRAL"
    print(f"[OK] COT TFF: net={net_cur:+,}, oi={oi_cur:,}, "
          f"long={long_pct}%, short={short_pct}%, bias={bias}")
    return {
        "date":      current.get("report_date_as_yyyy_mm_dd", "N/A")[:10],
        "net":       net_cur,
        "delta_net": net_cur - net_prv,
        "long_pct":  long_pct,
        "short_pct": short_pct,
        "net_pct":   net_pct,
        "oi":        oi_cur,
        "bias":      bias,
        "source":    "TFF/Managed Money",
    }


def _get_cot_legacy() -> dict:
    """Legacy Non-Commercial (gpe5-46if)."""
    data = safe_get(
        "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
        {
            "$where": "cftc_contract_market_code='099741'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 2,
        },
    )
    if not data:
        print("[WARN] COT Legacy leer")
        return {}

    current = data[0]
    prev    = data[1] if len(data) > 1 else {}

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

    # Hotfix: 0/0/0 erkennen
    if long_cur == 0 and short_cur == 0 and oi_cur == 0:
        print("[WARN] COT Legacy: alle Werte 0 → N/A")
        return {}

    net_pct = round((net_cur / oi_cur * 100), 1) if oi_cur > 0 else 0.0
    print(f"[OK] COT Legacy: net={net_cur:+,}, oi={oi_cur:,}")
    return {
        "date":      current.get("report_date_as_yyyy_mm_dd", "N/A")[:10],
        "net":       net_cur,
        "delta_net": net_cur - net_prv,
        "long_pct":  round(float(current.get("pct_of_oi_noncomm_long_all",  0) or 0), 1),
        "short_pct": round(float(current.get("pct_of_oi_noncomm_short_all", 0) or 0), 1),
        "net_pct":   net_pct,
        "oi":        oi_cur,
        "bias":      "NET-LONG" if net_pct > 5 else "NET-SHORT" if net_pct < -5 else "NEUTRAL",
        "source":    "Legacy/Non-Commercial",
    }


# ─────────────────────────────────────────────────────────────
#  Event-Kalender 2026
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
    def _next(dates):
        fut = [d for d in sorted(dates) if d >= TODAY]
        return fut[0] if fut else None
    def _fmt(d): return d.strftime("%d.%m.%Y") if d else "N/A"
    def _days(d): return (d - TODAY).days if d else None
    nf, ne, nn = _next(FOMC_DATES_2026), _next(ECB_DATES_2026), _next(NFP_DATES_2026)
    nc, np_    = _next(CPI_DATES_2026),  _next(PPI_DATES_2026)
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
    s = {}
    if fed_effr is not None and ecb_dfr is not None:
        diff = round(fed_effr - ecb_dfr, 2)
        s.update({"rate_diff": diff,
                  "rate_bias": "BÄRISCH" if diff > 0 else "BULLISCH",
                  "rate_icon": "🔴" if diff > 0 else "🟢"})
    else:
        s.update({"rate_diff": None, "rate_bias": "N/A", "rate_icon": "⚪"})

    if us2y is not None and de2y is not None:
        spread = round(us2y - de2y, 2)
        s.update({"yield_spread": spread,
                  "yield_bias":   "USD\u2011Vorteil" if spread > 0 else "EUR\u2011Vorteil",
                  "yield_icon":   "🔴" if spread > 0 else "🟢"})
    else:
        s.update({"yield_spread": None, "yield_bias": "N/A", "yield_icon": "⚪"})

    if cot and cot.get("net") is not None:
        np_ = cot.get("net_pct", 0)
        s.update({"cot_bias": "NET\u2011LONG"  if np_ > 5 else
                               "NET\u2011SHORT" if np_ < -5 else "NEUTRAL",
                  "cot_icon": "🟢" if np_ > 5 else "🔴" if np_ < -5 else "⚪"})
    else:
        s.update({"cot_bias": "N/A", "cot_icon": "⚪"})
    return s


# ─────────────────────────────────────────────────────────────
#  Event-Zeile (MarkdownV2)
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
    ecb_dfr, ecb_dfr_date, ecb_dfr_hinweis,
    ecb_hicp, ecb_hicp_date,
    fed_effr, fed_effr_date,
    us_cpi, us_cpi_date,
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

    net_val   = cot.get("net", 0) if cot else 0
    delta     = cot.get("delta_net", 0) if cot else 0
    delta_sym = "▲" if delta >= 0 else "▼"
    net_str   = esc(f"{net_val:+,}") if cot else esc("N/A")
    delta_str = esc(f"{delta_sym} {abs(delta):,}") if cot else esc("N/A")
    oi_str    = esc(f"{cot.get('oi', 0):,}") if cot else esc("N/A")
    lp        = esc(f"{cot.get('long_pct',  0):.1f}%") if cot else esc("N/A")
    sp        = esc(f"{cot.get('short_pct', 0):.1f}%") if cot else esc("N/A")
    np_pct    = esc(f"{cot.get('net_pct',   0):.1f}%") if cot else esc("N/A")
    cot_date  = esc(cot.get("date", "N/A")) if cot else esc("N/A")
    cot_src   = esc(cot.get("source", "N/A")) if cot else esc("N/A")

    e_dfr      = esc(fmt(ecb_dfr))
    e_dfr_date = esc((ecb_dfr_date or "N/A")[:10])
    e_dfr_note = f" _\\({esc(ecb_dfr_hinweis)}\\)_" if ecb_dfr_hinweis else ""

    e_hicp      = esc(fmt(ecb_hicp))
    e_hicp_date = esc((ecb_hicp_date or "N/A")[:7])

    e_effr      = esc(fmt(fed_effr))
    e_effr_date = esc((fed_effr_date or "N/A")[:10])

    e_cpi       = esc(fmt(us_cpi, decimals=1))
    e_cpi_date  = esc((str(us_cpi_date) or "N/A")[:7])

    e_us2y = esc(fmt(us2y))
    e_de2y = esc(fmt(de2y))

    ri = signals["rate_icon"]
    yi = signals["yield_icon"]
    ci = signals["cot_icon"]

    lines = [
        "📊 *EUR/USD · Morning Brief*",
        f"_{date_str}_",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🇪🇺 *EZB*",
        f"Leitzins \\(DFR\\)  `{e_dfr}`  \\| Stand `{e_dfr_date}`{e_dfr_note}",
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
        f"  Net\\-Position  `{net_str}` Kontrakte",
        f"  Δ Vorwoche     `{delta_str}` Kontrakte",
        f"  Long `{lp}` · Short `{sp}` · Net\\-OI `{np_pct}`",
        f"  Open Interest  `{oi_str}` Kontrakte",
        f"  Bias: {ci} `{esc(cot.get('bias', 'N/A') if cot else 'N/A')}`",
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
        print(f"[OK] Telegram gesendet "
              f"(msg_id={r.json().get('result', {}).get('message_id')})")
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
    print(f"[{datetime.now().isoformat()}] EUR/USD Morning Brief v4.1 startet...")

    ecb_dfr, ecb_dfr_date, ecb_dfr_hinweis = get_ecb_dfr()
    ecb_hicp,  ecb_hicp_date  = get_ecb_hicp()
    fed_effr,  fed_effr_date  = get_fed_effr()
    us_cpi,    us_cpi_date    = get_us_cpi()
    us2y,      us2y_date      = get_us2y()
    de2y,      de2y_date      = get_de2y()
    cot                       = get_cot_eur()
    meetings                  = get_next_meetings()
    signals                   = compute_signals(ecb_dfr, fed_effr, us2y, de2y, cot)

    print("\n── DATENPUNKTE ──────────────────────────")
    print(f"  EZB DFR:  {ecb_dfr}%  ({ecb_dfr_date})  hinweis={ecb_dfr_hinweis}")
    print(f"  EZB HICP: {ecb_hicp}% ({ecb_hicp_date})")
    print(f"  EFFR:     {fed_effr}% ({fed_effr_date})")
    print(f"  US CPI:   {us_cpi}%  ({us_cpi_date})")
    print(f"  US 2Y:    {us2y}%   ({us2y_date})")
    print(f"  DE 2Y:    {de2y}%   ({de2y_date})")
    if cot:
        print(f"  COT:      net={cot.get('net'):+,}, oi={cot.get('oi'):,}, "
              f"bias={cot.get('bias')}, src={cot.get('source')}")
    else:
        print("  COT:      N/A")
    print(f"  Signale:  rateDiff={signals.get('rate_diff')} "
          f"spread={signals.get('yield_spread')}")
    print("─────────────────────────────────────────")

    message = build_message(
        ecb_dfr, ecb_dfr_date, ecb_dfr_hinweis,
        ecb_hicp, ecb_hicp_date,
        fed_effr, fed_effr_date,
        us_cpi, us_cpi_date,
        us2y, us2y_date, de2y, de2y_date,
        cot, meetings, signals
    )

    print("\n── VORSCHAU ─────────────────────────────")
    print(message)
    print("─────────────────────────────────────────\n")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram(message)
    else:
        print("[INFO] Kein Token/Chat-ID → nur Vorschau")


if __name__ == "__main__":
    run()
