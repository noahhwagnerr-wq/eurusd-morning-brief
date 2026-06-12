#!/usr/bin/env python3
"""
=============================================================
  EUR/USD Morning Brief Agent
  Täglich live Daten → Telegram-Nachricht
  Quellen: ECB SDMX API, FRED API, CFTC Socrata API
=============================================================
"""

import os
import requests
from datetime import datetime, timedelta, date
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
FRED_API_KEY       = os.getenv("FRED_API_KEY")


def safe_get(url: str, params: dict = None, timeout: int = 15):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[WARN] Fehler bei {url}: {e}")
        return None


def fmt(value, decimals=2, suffix="%") -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{decimals}f}{suffix}"
    except Exception:
        return str(value)


# ─────────────────────────────────────────────────────────────
#  1. EZB – Leitzins (DFR)
#     Series: FM/B.U2.EUR.RT0.BB.1M.WDFI → korrekter Key
# ─────────────────────────────────────────────────────────────

def _ecb_sdmx_last(url: str, series_key: str):
    """Generischer ECB SDMX Helper – gibt (value, period) zurück."""
    params = {"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
    data = safe_get(url, params)
    if not data:
        return None, "N/A"
    try:
        series_map = data["dataSets"][0]["series"]
        # Ersten verfügbaren Key nehmen wenn exakter Key nicht passt
        key = series_key if series_key in series_map else list(series_map.keys())[0]
        obs = series_map[key]["observations"]
        last_key = sorted(obs.keys(), key=lambda x: int(x))[-1]
        value = obs[last_key][0]
        periods = data["structure"]["dimensions"]["observation"][0]["values"]
        period = periods[int(last_key)]["id"]
        return float(value), period
    except Exception as e:
        print(f"[WARN] ECB SDMX parse error ({url}): {e}")
        return None, "N/A"


def get_ecb_dfr():
    """EZB Deposit Facility Rate – Key Interest Rate."""
    # Primär: Key Interest Rates – Deposit facility
    url = "https://data-api.ecb.europa.eu/service/data/FM/B.U2.EUR.RT0.BB.1M.WDFI"
    val, period = _ecb_sdmx_last(url, "0:0:0:0:0:0:0")
    if val is not None:
        return val, period
    # Fallback: MRO Rate
    url2 = "https://data-api.ecb.europa.eu/service/data/FM/B.U2.EUR.RT0.BB.1M.WDOI"
    return _ecb_sdmx_last(url2, "0:0:0:0:0:0:0")


def get_ecb_hicp():
    """Eurozone HVPI (HICP) YoY."""
    url = "https://data-api.ecb.europa.eu/service/data/ICP/M.U2.N.000000.4.ANR"
    return _ecb_sdmx_last(url, "0:0:0:0:0:0")


def get_de2y():
    """Deutsche 2Y Bundesanleihe Rendite."""
    url = "https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y"
    return _ecb_sdmx_last(url, "0:0:0:0:0:0:0")


# ─────────────────────────────────────────────────────────────
#  2. Federal Reserve – EFFR, US CPI YoY, US 2Y
#     Quelle: FRED API
# ─────────────────────────────────────────────────────────────

def _fred_obs(series_id: str, limit: int = 1, days_back: int = 90):
    """Letzter N Beobachtungen einer FRED-Zeitreihe."""
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
        "observation_start": (date.today() - timedelta(days=days_back)).isoformat()
    }
    data = safe_get(url, params)
    if data and data.get("observations"):
        return [(o["value"], o["date"]) for o in data["observations"] if o["value"] != "."]
    return []


def get_fed_effr():
    obs = _fred_obs("DFF", limit=1, days_back=30)
    if obs:
        return float(obs[0][0]), obs[0][1]
    return None, "N/A"


def get_us_cpi():
    """US CPI YoY – berechnet aus CPIAUCSL (Indexwert, nicht % direkt).
    Nimmt neuesten Wert und Wert von vor 12 Monaten."""
    # FRED CPIAUCSL_PC1 = direkte YoY %-Reihe
    obs = _fred_obs("CPIAUCSL_PC1", limit=1, days_back=120)
    if obs:
        return float(obs[0][0]), obs[0][1]
    # Fallback: manuell aus CPIAUCSL berechnen
    obs2 = _fred_obs("CPIAUCSL", limit=14, days_back=400)
    if len(obs2) >= 13:
        val_now  = float(obs2[0][0])
        val_year = float(obs2[12][0])
        yoy = (val_now - val_year) / val_year * 100
        return round(yoy, 2), obs2[0][1]
    return None, "N/A"


def get_us2y():
    obs = _fred_obs("DGS2", limit=1, days_back=30)
    if obs:
        return float(obs[0][0]), obs[0][1]
    return None, "N/A"


# ─────────────────────────────────────────────────────────────
#  3. COT-Daten – CME EUR Futures 6E (Non-Commercial = Spekulanten)
#     Quelle: CFTC Socrata Public Reporting API
#     Feldnamen: noncomm_positions_long_all / noncomm_positions_short_all
# ─────────────────────────────────────────────────────────────

def get_cot_eur() -> dict:
    url = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
    params = {
        "$where": "contract_market_code='099741'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 2
    }
    data = safe_get(url, params)
    if not data:
        return {}

    print(f"[DEBUG] COT Felder vorhanden: {list(data[0].keys()) if data else 'leer'}")

    if len(data) > 0:
        current = data[0]
        prev    = data[1] if len(data) > 1 else {}

        def to_int(d, *keys):
            for k in keys:
                try:
                    v = d.get(k)
                    if v is not None:
                        return int(float(v))
                except:
                    pass
            return 0

        # Non-Commercial (Spekulanten/Institutionelle) – primär
        long_cur  = to_int(current, "noncomm_positions_long_all", "asset_mgr_positions_long")
        short_cur = to_int(current, "noncomm_positions_short_all", "asset_mgr_positions_short")
        long_prv  = to_int(prev,    "noncomm_positions_long_all", "asset_mgr_positions_long")
        short_prv = to_int(prev,    "noncomm_positions_short_all", "asset_mgr_positions_short")
        oi_cur    = to_int(current, "open_interest_all")

        net_cur   = long_cur - short_cur
        net_prv   = long_prv - short_prv
        delta_net = net_cur - net_prv

        long_pct  = round(long_cur  / oi_cur * 100, 1) if oi_cur > 0 else 0
        short_pct = round(short_cur / oi_cur * 100, 1) if oi_cur > 0 else 0
        net_pct   = round(net_cur   / oi_cur * 100, 1) if oi_cur > 0 else 0

        report_date = current.get("report_date_as_yyyy_mm_dd", "N/A")[:10]

        print(f"[DEBUG] COT: long={long_cur}, short={short_cur}, oi={oi_cur}, net={net_cur}")

        return {
            "date":      report_date,
            "net":       net_cur,
            "delta_net": delta_net,
            "long_pct":  long_pct,
            "short_pct": short_pct,
            "net_pct":   net_pct,
            "oi":        oi_cur,
            "bias":      "NET-LONG" if net_cur > 0 else "NET-SHORT"
        }
    return {}


# ─────────────────────────────────────────────────────────────
#  4. Nächste FOMC/EZB-Sitzung
# ─────────────────────────────────────────────────────────────

def get_next_meetings() -> dict:
    today = date.today()
    fomc_dates = [
        date(2026, 1, 28), date(2026, 3, 18), date(2026, 5, 6),
        date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
        date(2026, 10, 28), date(2026, 12, 9)
    ]
    ecb_dates = [
        date(2026, 1, 30), date(2026, 3, 5), date(2026, 4, 16),
        date(2026, 6, 5),  date(2026, 7, 23), date(2026, 9, 10),
        date(2026, 10, 22), date(2026, 12, 3)
    ]
    next_fomc = next((d for d in sorted(fomc_dates) if d >= today), None)
    next_ecb  = next((d for d in sorted(ecb_dates)  if d >= today), None)
    return {
        "fomc_date": next_fomc.strftime("%d.%m.%Y") if next_fomc else "N/A",
        "fomc_days": (next_fomc - today).days if next_fomc else None,
        "ecb_date":  next_ecb.strftime("%d.%m.%Y")  if next_ecb  else "N/A",
        "ecb_days":  (next_ecb  - today).days if next_ecb  else None,
    }


# ─────────────────────────────────────────────────────────────
#  5. Signal-Logik
# ─────────────────────────────────────────────────────────────

def compute_signals(ecb_dfr, fed_effr, us2y, de2y, cot) -> dict:
    signals = {}

    if fed_effr is not None and ecb_dfr is not None:
        diff = fed_effr - ecb_dfr
        signals["rate_diff"]   = diff
        signals["rate_signal"] = "\U0001f534 B\u00c4RISCH EUR/USD" if diff > 0 else "\U0001f7e2 BULLISCH EUR/USD"
    else:
        signals["rate_diff"]   = None
        signals["rate_signal"] = "\u26aa N/A"

    if us2y is not None and de2y is not None:
        spread = us2y - de2y
        signals["yield_spread"] = spread
        signals["yield_signal"] = "\U0001f534 USD-Vorteil" if spread > 0 else "\U0001f7e2 EUR-Vorteil"
    else:
        signals["yield_spread"] = None
        signals["yield_signal"] = "\u26aa N/A"

    if cot and cot.get("net") is not None:
        net_pct = cot.get("net_pct", 0)
        if net_pct > 5:
            signals["cot_signal"] = "\U0001f7e2 NET-LONG"
        elif net_pct < -5:
            signals["cot_signal"] = "\U0001f534 NET-SHORT"
        else:
            signals["cot_signal"] = "\u26aa NEUTRAL/FLAT"
    else:
        signals["cot_signal"] = "\u26aa N/A"

    return signals


# ─────────────────────────────────────────────────────────────
#  6. Nachricht bauen
# ─────────────────────────────────────────────────────────────

def build_message(ecb_dfr, ecb_dfr_date, ecb_hicp, ecb_hicp_date,
                  fed_effr, fed_effr_date, us_cpi, us_cpi_date,
                  us2y, us2y_date, de2y, de2y_date,
                  cot, meetings, signals) -> str:
    today_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    rate_diff    = signals.get("rate_diff")
    yield_spread = signals.get("yield_spread")
    diff_str   = (f"+{rate_diff:.2f}pp" if rate_diff is not None and rate_diff >= 0
                  else f"{rate_diff:.2f}pp" if rate_diff is not None else "N/A")
    spread_str = (f"+{yield_spread:.2f}%" if yield_spread is not None and yield_spread >= 0
                  else f"{yield_spread:.2f}%" if yield_spread is not None else "N/A")

    cot_delta_sign = "\u25b2" if cot.get("delta_net", 0) > 0 else "\u25bc"
    cot_delta      = abs(cot.get("delta_net", 0))
    cot_date_str   = cot.get("date", "N/A")
    oi             = cot.get("oi", 0)
    oi_str         = f"{oi:,}" if isinstance(oi, int) else "N/A"
    net_val        = cot.get("net", 0)
    net_str        = f"{net_val:,}" if isinstance(net_val, int) else "N/A"

    fomc_info = (f"{meetings['fomc_date']} (noch {meetings['fomc_days']}T)"
                 if meetings.get("fomc_days") is not None else meetings.get("fomc_date", "N/A"))
    ecb_info  = (f"{meetings['ecb_date']} (noch {meetings['ecb_days']}T)"
                 if meetings.get("ecb_days")  is not None else meetings.get("ecb_date",  "N/A"))

    kapital = ("\U0001f4b5 USD-Zinsvorteil \u2192 Kapital in den Dollar"
               if (rate_diff or 0) > 0
               else "\U0001f4b6 EUR-Zinsvorteil \u2192 Kapital in den Euro")

    return (
        f"\U0001f4ca *EUR/USD Morning Brief* \u2014 {today_str}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
        f"\U0001f1ea\U0001f1fa *EZB (Eurozone)*\n"
        f"\u2022 Leitzins (DFR): `{fmt(ecb_dfr)}` _(Stand: {ecb_dfr_date})_\n"
        f"\u2022 Inflation HVPI YoY: `{fmt(ecb_hicp)}` _(Stand: {ecb_hicp_date})_\n"
        f"\u2022 N\u00e4chste EZB-Sitzung: {ecb_info}\n\n"
        f"\U0001f1fa\U0001f1f8 *Federal Reserve (USA)*\n"
        f"\u2022 Leitzins (EFFR): `{fmt(fed_effr)}` _(Stand: {fed_effr_date})_\n"
        f"\u2022 Inflation CPI YoY: `{fmt(us_cpi)}` _(Stand: {us_cpi_date})_\n"
        f"\u2022 N\u00e4chstes FOMC: {fomc_info}\n\n"
        f"\U0001f4c8 *Kapitalfluss & Zinsdifferenz*\n"
        f"\u2022 EFFR vs. DFR: `{diff_str}` \u2192 {signals['rate_signal']}\n"
        f"\u2022 2Y US: `{fmt(us2y)}` | DE: `{fmt(de2y)}` _(Spread: {spread_str})_ \u2192 {signals['yield_signal']}\n"
        f"\u2022 Kapitalfluss: {kapital}\n\n"
        f"\U0001f4cb *Institutional Sentiment \u2013 COT (CME 6E EUR)*\n"
        f"\u2022 Stand: {cot_date_str}\n"
        f"\u2022 Net-Position: `{net_str}` Kontrakte \u2192 {signals['cot_signal']}\n"
        f"\u2022 \u0394 Vorwoche: `{cot_delta_sign} {cot_delta:,}` Kontrakte\n"
        f"\u2022 K\u00e4ufer (Long): `{cot.get('long_pct','N/A')}%` | Verk\u00e4ufer (Short): `{cot.get('short_pct','N/A')}%`\n"
        f"\u2022 Net % OI: `{cot.get('net_pct','N/A')}%` | OI Gesamt: `{oi_str}` Kontrakte\n\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f3af *Gesamt-Bias EUR/USD*\n"
        f"{signals['rate_signal']} | {signals['yield_signal']} | {signals['cot_signal']}\n\n"
        f"_Quellen: ECB SDMX API \u00b7 FRED API \u00b7 CFTC Socrata API_"
    )


# ─────────────────────────────────────────────────────────────
#  7. Telegram senden
# ─────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    token   = TELEGRAM_BOT_TOKEN.strip()
    chat_id = TELEGRAM_CHAT_ID.strip()
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        print(f"[DEBUG] Telegram Status: {r.status_code} | {r.text[:200]}")
        r.raise_for_status()
        print(f"[OK] Telegram gesendet (ID: {r.json().get('result',{}).get('message_id')})")
        return True
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")
        return False


# ─────────────────────────────────────────────────────────────
#  8. Main
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

    message = build_message(
        ecb_dfr, ecb_dfr_date, ecb_hicp, ecb_hicp_date,
        fed_effr, fed_effr_date, us_cpi, us_cpi_date,
        us2y, us2y_date, de2y, de2y_date,
        cot, meetings, signals
    )

    print("\n── VORSCHAU ──")
    print(message)
    print("─────────────\n")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram(message)
    else:
        print("[INFO] Kein Token/Chat-ID gesetzt – nur Vorschau")


if __name__ == "__main__":
    run()
