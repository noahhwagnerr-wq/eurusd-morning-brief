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


def safe_get(url: str, params: dict = None, timeout: int = 10):
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


def get_ecb_dfr():
    url = "https://data-api.ecb.europa.eu/service/data/FM/B.U2.EUR.RT0.BB.1M.WDFI"
    params = {"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
    data = safe_get(url, params)
    if data:
        try:
            obs = data["dataSets"][0]["series"]["0:0:0:0:0:0:0"]["observations"]
            last_key = sorted(obs.keys(), key=lambda x: int(x))[-1]
            value = obs[last_key][0]
            periods = data["structure"]["dimensions"]["observation"][0]["values"]
            period = periods[int(last_key)]["id"]
            return float(value), period
        except Exception as e:
            print(f"[WARN] ECB DFR parse error: {e}")
    return None, "N/A"


def get_ecb_hicp():
    url = "https://data-api.ecb.europa.eu/service/data/ICP/M.U2.N.000000.4.ANR"
    params = {"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
    data = safe_get(url, params)
    if data:
        try:
            obs = data["dataSets"][0]["series"]["0:0:0:0:0:0"]["observations"]
            last_key = sorted(obs.keys(), key=lambda x: int(x))[-1]
            value = obs[last_key][0]
            periods = data["structure"]["dimensions"]["observation"][0]["values"]
            period = periods[int(last_key)]["id"]
            return float(value), period
        except Exception as e:
            print(f"[WARN] ECB HICP parse error: {e}")
    return None, "N/A"


def get_fred_series(series_id: str):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
        "observation_start": (date.today() - timedelta(days=90)).isoformat()
    }
    data = safe_get(url, params)
    if data and data.get("observations"):
        obs = data["observations"][0]
        raw = obs.get("value", ".")
        if raw == ".":
            return None, obs.get("date", "N/A")
        return float(raw), obs.get("date", "N/A")
    return None, "N/A"


def get_fed_effr():
    return get_fred_series("DFF")


def get_us_cpi():
    return get_fred_series("CPIAUCSL_PC1")


def get_us2y():
    return get_fred_series("DGS2")


def get_de2y():
    url = "https://data-api.ecb.europa.eu/service/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y"
    params = {"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
    data = safe_get(url, params)
    if data:
        try:
            obs = data["dataSets"][0]["series"]["0:0:0:0:0:0:0"]["observations"]
            last_key = sorted(obs.keys(), key=lambda x: int(x))[-1]
            value = obs[last_key][0]
            periods = data["structure"]["dimensions"]["observation"][0]["values"]
            period = periods[int(last_key)]["id"]
            return float(value), period
        except Exception as e:
            print(f"[WARN] DE2Y parse error: {e}")
    return None, "N/A"


def get_cot_eur() -> dict:
    url = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
    params = {
        "$where": "contract_market_code='099741'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 2
    }
    data = safe_get(url, params)
    if data and len(data) > 0:
        current = data[0]
        prev    = data[1] if len(data) > 1 else {}

        def to_int(d, key):
            try: return int(float(d.get(key, 0)))
            except: return 0

        long_cur  = to_int(current, "asset_mgr_positions_long")
        short_cur = to_int(current, "asset_mgr_positions_short")
        long_prv  = to_int(prev,    "asset_mgr_positions_long")
        short_prv = to_int(prev,    "asset_mgr_positions_short")
        oi_cur    = to_int(current, "open_interest_all")

        net_cur   = long_cur - short_cur
        net_prv   = long_prv - short_prv
        delta_net = net_cur - net_prv

        long_pct  = round((long_cur  / oi_cur * 100), 1) if oi_cur > 0 else 0
        short_pct = round((short_cur / oi_cur * 100), 1) if oi_cur > 0 else 0
        net_pct   = round((net_cur   / oi_cur * 100), 1) if oi_cur > 0 else 0

        report_date = current.get("report_date_as_yyyy_mm_dd", "N/A")[:10]

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


def get_next_meetings() -> dict:
    today = date.today()
    fomc_dates = [
        date(2026, 1, 28), date(2026, 3, 18), date(2026, 5, 6),
        date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
        date(2026, 10, 28), date(2026, 12, 9)
    ]
    ecb_dates = [
        date(2026, 1, 30), date(2026, 3, 5),  date(2026, 4, 16),
        date(2026, 6, 5),  date(2026, 7, 23), date(2026, 9, 10),
        date(2026, 10, 22), date(2026, 12, 3)
    ]
    next_fomc = next((d for d in sorted(fomc_dates) if d >= today), None)
    next_ecb  = next((d for d in sorted(ecb_dates)  if d >= today), None)
    fomc_days = (next_fomc - today).days if next_fomc else None
    ecb_days  = (next_ecb  - today).days if next_ecb  else None
    return {
        "fomc_date": next_fomc.strftime("%d.%m.%Y") if next_fomc else "N/A",
        "fomc_days": fomc_days,
        "ecb_date":  next_ecb.strftime("%d.%m.%Y")  if next_ecb  else "N/A",
        "ecb_days":  ecb_days
    }


def compute_signals(ecb_dfr, fed_effr, us2y, de2y, cot) -> dict:
    signals = {}
    if fed_effr is not None and ecb_dfr is not None:
        diff = fed_effr - ecb_dfr
        signals["rate_diff"]   = diff
        signals["rate_signal"] = "🔴 BÄRISCH EUR/USD" if diff > 0 else "🟢 BULLISCH EUR/USD"
    else:
        signals["rate_diff"]   = None
        signals["rate_signal"] = "⚪ N/A"

    if us2y is not None and de2y is not None:
        spread = us2y - de2y
        signals["yield_spread"] = spread
        signals["yield_signal"] = "🔴 USD-Vorteil" if spread > 0 else "🟢 EUR-Vorteil"
    else:
        signals["yield_spread"] = None
        signals["yield_signal"] = "⚪ N/A"

    if cot and cot.get("net") is not None:
        net_pct = cot.get("net_pct", 0)
        if net_pct > 5:
            signals["cot_signal"] = "🟢 NET-LONG"
        elif net_pct < -5:
            signals["cot_signal"] = "🔴 NET-SHORT"
        else:
            signals["cot_signal"] = "⚪ NEUTRAL/FLAT"
    else:
        signals["cot_signal"] = "⚪ N/A"

    return signals


def build_message(ecb_dfr, ecb_dfr_date, ecb_hicp, ecb_hicp_date,
                  fed_effr, fed_effr_date, us_cpi, us_cpi_date,
                  us2y, us2y_date, de2y, de2y_date,
                  cot, meetings, signals) -> str:
    today_str = datetime.now().strftime("%d.%m.%Y %H:%M")

    rate_diff    = signals.get("rate_diff")
    yield_spread = signals.get("yield_spread")
    diff_str   = (f"+{rate_diff:.2f}pp"    if rate_diff is not None    and rate_diff >= 0    else (f"{rate_diff:.2f}pp"    if rate_diff    is not None else "N/A"))
    spread_str = (f"+{yield_spread:.2f}%" if yield_spread is not None and yield_spread >= 0 else (f"{yield_spread:.2f}%" if yield_spread is not None else "N/A"))

    cot_delta_sign = "▲" if cot.get("delta_net", 0) > 0 else "▼"
    cot_delta      = abs(cot.get("delta_net", 0))
    cot_date_str   = cot.get("date", "N/A")

    fomc_info = f"{meetings['fomc_date']} (noch {meetings['fomc_days']}T)" if meetings.get("fomc_days") is not None else meetings.get("fomc_date", "N/A")
    ecb_info  = f"{meetings['ecb_date']} (noch {meetings['ecb_days']}T)"  if meetings.get("ecb_days")  is not None else meetings.get("ecb_date",  "N/A")

    oi = cot.get("oi", 0)
    oi_str  = f"{oi:,}"               if isinstance(oi, int) else "N/A"
    net_str = f"{cot.get('net', 0):,}" if isinstance(cot.get("net"), int) else "N/A"

    msg = (
        f"📊 *EUR/USD Morning Brief* — {today_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🇪🇺 *EZB (Eurozone)*\n"
        f"• Leitzins (DFR): `{fmt(ecb_dfr)}` _(Stand: {ecb_dfr_date})_\n"
        f"• Inflation HVPI YoY: `{fmt(ecb_hicp)}` _(Stand: {ecb_hicp_date})_\n"
        f"• Nächste EZB-Sitzung: {ecb_info}\n\n"
        f"🇺🇸 *Federal Reserve (USA)*\n"
        f"• Leitzins (EFFR): `{fmt(fed_effr)}` _(Stand: {fed_effr_date})_\n"
        f"• Inflation CPI YoY: `{fmt(us_cpi)}` _(Stand: {us_cpi_date})_\n"
        f"• Nächstes FOMC: {fomc_info}\n\n"
        f"📈 *Kapitalfluss & Zinsdifferenz*\n"
        f"• EFFR vs. DFR: `{diff_str}` → {signals['rate_signal']}\n"
        f"• 2Y US: `{fmt(us2y)}` | DE: `{fmt(de2y)}` _(Spread: {spread_str})_ → {signals['yield_signal']}\n"
        f"• Kapitalfluss: {'💵 USD-Zinsvorteil → Kapital in den Dollar' if (rate_diff or 0) > 0 else '💶 EUR-Zinsvorteil → Kapital in den Euro'}\n\n"
        f"📋 *Institutional Sentiment – COT (CME 6E EUR)*\n"
        f"• Stand: {cot_date_str}\n"
        f"• Net-Position: `{net_str}` Kontrakte → {signals['cot_signal']}\n"
        f"• Δ Vorwoche: `{cot_delta_sign} {cot_delta:,}` Kontrakte\n"
        f"• Käufer (Long): `{cot.get('long_pct','N/A')}%` | Verkäufer (Short): `{cot.get('short_pct','N/A')}%`\n"
        f"• Net % OI: `{cot.get('net_pct','N/A')}%` | OI Gesamt: `{oi_str}` Kontrakte\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Gesamt-Bias EUR/USD*\n"
        f"{signals['rate_signal']} | {signals['yield_signal']} | {signals['cot_signal']}\n\n"
        f"_Quellen: ECB SDMX API · FRED API · CFTC Socrata API_"
    )
    return msg


def send_telegram(message: str) -> bool:
    token = TELEGRAM_BOT_TOKEN.strip()
    chat_id = TELEGRAM_CHAT_ID.strip()

    print(f"[DEBUG] Bot-Token (erste 10 Zeichen): {token[:10]}...")
    print(f"[DEBUG] Chat-ID: {chat_id}")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       message,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        print(f"[DEBUG] Telegram HTTP Status: {r.status_code}")
        print(f"[DEBUG] Telegram Antwort: {r.text}")
        r.raise_for_status()
        print(f"[OK] Telegram gesendet (Message-ID: {r.json().get('result', {}).get('message_id')})")
        return True
    except Exception as e:
        print(f"[ERROR] Telegram-Fehler: {e}")
        return False


def run():
    print(f"[{datetime.now().isoformat()}] Starte EUR/USD Morning Brief Agent...")

    print("  → ECB DFR...")
    ecb_dfr, ecb_dfr_date     = get_ecb_dfr()
    print("  → ECB HICP...")
    ecb_hicp, ecb_hicp_date   = get_ecb_hicp()
    print("  → FRED EFFR...")
    fed_effr, fed_effr_date   = get_fed_effr()
    print("  → FRED CPI...")
    us_cpi, us_cpi_date       = get_us_cpi()
    print("  → FRED US 2Y...")
    us2y, us2y_date           = get_us2y()
    print("  → ECB DE 2Y...")
    de2y, de2y_date           = get_de2y()
    print("  → CFTC COT EUR...")
    cot                        = get_cot_eur()

    meetings = get_next_meetings()
    signals  = compute_signals(ecb_dfr, fed_effr, us2y, de2y, cot)

    print(f"  EZB DFR:  {ecb_dfr} ({ecb_dfr_date})")
    print(f"  EZB HICP: {ecb_hicp} ({ecb_hicp_date})")
    print(f"  EFFR:     {fed_effr} ({fed_effr_date})")
    print(f"  US CPI:   {us_cpi} ({us_cpi_date})")
    print(f"  US 2Y:    {us2y} ({us2y_date})")
    print(f"  DE 2Y:    {de2y} ({de2y_date})")
    print(f"  COT:      {cot}")
    print(f"  Signals:  {signals}")

    message = build_message(
        ecb_dfr, ecb_dfr_date, ecb_hicp, ecb_hicp_date,
        fed_effr, fed_effr_date, us_cpi, us_cpi_date,
        us2y, us2y_date, de2y, de2y_date,
        cot, meetings, signals
    )

    print("\n── VORSCHAU ────────────────────────────────────────────")
    print(message)
    print("────────────────────────────────────────────────────────\n")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram(message)
    else:
        print("[INFO] TELEGRAM_BOT_TOKEN/CHAT_ID nicht gesetzt → nur Vorschau")


if __name__ == "__main__":
    run()
