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
    val, period = _fm_find("DFR", freq="B", data_type="LEV")
    if val is not None:
        return val, period
    val, period = _fm_find("DFR", freq="D", data_type="LEV")
    if val is not None:
        return val, period
    data = safe_get(
        f"{ECB_BASE}/FM/B.U2.EUR.4F.KR.DFR.LEV",
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
            return val, period
        except Exception as e:
            print(f"[WARN] ECB DFR direct: {e}")
    return None, "N/A"


def get_ecb_hicp():
    data = safe_get(
        f"{ECB_BASE}/ICP/M.U2.N.000000.4.ANR",
        params={"format": "jsondata", "lastNObservations": 1, "detail": "dataonly"}
    )
    if not data:
        return None, "N/A"
    try:
        series_map = data["dataSets"][0]["series"]
        key        = list(series_map.keys())[0]
        obs        = series_map[key]["observations"]
        last_k     = sorted(obs.keys(), key=lambda x: int(x))[-1]
        val        = float(obs[last_k][0])
        periods    = data["structure"]["dimensions"]["observation"][0]["values"]
        period     = periods[int(last_k)]["id"]
        return val, period
    except Exception as e:
        print(f"[WARN] ECB HICP: {e}")
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
    url = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
    params = {
        "$where": "cftc_contract_market_code='399741'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 2
    }
    data = safe_get(url, params)
    if not data or len(data) == 0:
        print("[WARN] CFTC: keine Daten")
        return {}

    current = data[0]
    prev    = data[1] if len(data) > 1 else {}

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

    long_cur  = to_int(current, "asset_mgr_positions_long")
    short_cur = to_int(current, "asset_mgr_positions_short")
    long_prv  = to_int(prev,    "asset_mgr_positions_long")
    short_prv = to_int(prev,    "asset_mgr_positions_short")
    oi_cur    = to_int(current, "open_interest_all")

    net_cur   = long_cur - short_cur
    net_prv   = long_prv - short_prv
    delta_net = net_cur - net_prv

    long_pct  = to_float(current, "pct_of_oi_asset_mgr_long")
    short_pct = to_float(current, "pct_of_oi_asset_mgr_short")
    net_pct   = round((net_cur / oi_cur * 100), 1) if oi_cur > 0 else 0.0

    report_date = current.get("report_date_as_yyyy_mm_dd", "N/A")[:10]

    print(f"[OK] COT: long={long_cur}, short={short_cur}, net={net_cur}, oi={oi_cur}, date={report_date}")

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


# ─────────────────────────────────────────────────────────────
#  Nächste FOMC/EZB-Sitzungen
# ─────────────────────────────────────────────────────────────

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
    return {
        "fomc_date": next_fomc.strftime("%d.%m.%Y") if next_fomc else "N/A",
        "fomc_days": (next_fomc - today).days if next_fomc else None,
        "ecb_date":  next_ecb.strftime("%d.%m.%Y")  if next_ecb  else "N/A",
        "ecb_days":  (next_ecb  - today).days if next_ecb  else None,
    }


# ─────────────────────────────────────────────────────────────
#  Signal-Logik
# ─────────────────────────────────────────────────────────────

def compute_signals(ecb_dfr, fed_effr, us2y, de2y, cot) -> dict:
    signals = {}

    if fed_effr is not None and ecb_dfr is not None:
        diff = fed_effr - ecb_dfr
        signals["rate_diff"]   = diff
        signals["rate_bias"]   = "BÄRISCH" if diff > 0 else "BULLISCH"
        signals["rate_icon"]   = "🔴" if diff > 0 else "🟢"
    else:
        signals["rate_diff"]  = None
        signals["rate_bias"]  = "N/A"
        signals["rate_icon"]  = "⚪"

    if us2y is not None and de2y is not None:
        spread = us2y - de2y
        signals["yield_spread"] = spread
        signals["yield_bias"]   = "USD-Vorteil" if spread > 0 else "EUR-Vorteil"
        signals["yield_icon"]   = "🔴" if spread > 0 else "🟢"
    else:
        signals["yield_spread"] = None
        signals["yield_bias"]   = "N/A"
        signals["yield_icon"]   = "⚪"

    if cot and cot.get("net") is not None:
        net_pct = cot.get("net_pct", 0)
        if net_pct > 5:
            signals["cot_bias"] = "NET-LONG"
            signals["cot_icon"] = "🟢"
        elif net_pct < -5:
            signals["cot_bias"] = "NET-SHORT"
            signals["cot_icon"] = "🔴"
        else:
            signals["cot_bias"] = "NEUTRAL"
            signals["cot_icon"] = "⚪"
    else:
        signals["cot_bias"] = "N/A"
        signals["cot_icon"] = "⚪"

    return signals


# ─────────────────────────────────────────────────────────────
#  Nachricht bauen — elegantes MarkdownV2-Format
#
#  Struktur:
#    HEADER  →  Datum + Wochentag
#    BLOCK 1 →  EZB  |  FED  (kompakt, nebeneinander lesbar)
#    BLOCK 2 →  Zinsdifferenz & Rendite-Spread
#    BLOCK 3 →  COT Institutional Sentiment
#    FOOTER  →  Gesamt-Bias + nächste Sitzungen
# ─────────────────────────────────────────────────────────────

WEEKDAY_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

def build_message(ecb_dfr, ecb_dfr_date, ecb_hicp, ecb_hicp_date,
                  fed_effr, fed_effr_date, us_cpi, us_cpi_date,
                  us2y, us2y_date, de2y, de2y_date,
                  cot, meetings, signals) -> str:

    now = datetime.now()
    weekday = WEEKDAY_DE[now.weekday()]
    date_str = now.strftime(f"{weekday}, %d\\. %m\\. %Y")

    # ── Zinsdifferenz-String
    rd = signals.get("rate_diff")
    rd_str = esc(f"+{rd:.2f}pp" if rd is not None and rd >= 0 else f"{rd:.2f}pp" if rd is not None else "N/A")

    # ── Rendite-Spread-String
    ys = signals.get("yield_spread")
    ys_str = esc(f"+{ys:.2f}pp" if ys is not None and ys >= 0 else f"{ys:.2f}pp" if ys is not None else "N/A")

    # ── COT-Werte
    net_val   = cot.get("net", 0)
    delta     = cot.get("delta_net", 0)
    delta_sym = "▲" if delta >= 0 else "▼"
    net_str   = esc(f"{net_val:+,}")
    delta_str = esc(f"{delta_sym} {abs(delta):,}")
    oi_str    = esc(f"{cot.get('oi', 0):,}")
    lp        = esc(f"{cot.get('long_pct',  0):.1f}%")
    sp        = esc(f"{cot.get('short_pct', 0):.1f}%")
    np_       = esc(f"{cot.get('net_pct',   0):.1f}%")
    cot_date  = esc(cot.get("date", "N/A"))

    # ── Nächste Sitzungen
    fomc_days = meetings.get("fomc_days")
    ecb_days  = meetings.get("ecb_days")
    fomc_str  = esc(f"{meetings['fomc_date']}" + (f" \u00b7 noch {fomc_days}T" if fomc_days is not None else ""))
    ecb_str   = esc(f"{meetings['ecb_date']}"  + (f" \u00b7 noch {ecb_days}T"  if ecb_days  is not None else ""))

    # ── Signal-Zeile Gesamt-Bias
    ri, rb = signals["rate_icon"],  esc(signals["rate_bias"])
    yi, yb = signals["yield_icon"], esc(signals["yield_bias"])
    ci, cb = signals["cot_icon"],   esc(signals["cot_bias"])

    # ── Daten escapen
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

    lines = [
        # ── HEADER
        f"📊 *EUR/USD · Morning Brief*",
        f"_{date_str}_",
        "",
        # ── BLOCK 1: Zentralbanken
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🇪🇺 *EZB*                      🇺🇸 *Federal Reserve*",
        f"Leitzins \u00a0 `{e_dfr}`          Leitzins \u00a0 `{e_effr}`",
        f"_DFR · {e_dfr_date}_          _EFFR · {e_effr_date}_",
        f"Inflation \u00a0`{e_hicp}`         Inflation  `{e_cpi}`",
        f"_HVPI · {e_hicp_date}_         _CPI · {e_cpi_date}_",
        "",
        # ── BLOCK 2: Zinsdiff & Renditen
        "━━━━━━━━━━━━━━━━━━━━━━",
        "📈 *Kapitalfluss · Zinsdifferenz*",
        "",
        f"  EFFR vs\. DFR  `{rd_str}`   {signals['rate_icon']} _{esc(signals['rate_bias'])}_",
        f"  US 2Y `{e_us2y}` · DE 2Y `{e_de2y}`",
        f"  Spread `{ys_str}`            {signals['yield_icon']} _{esc(signals['yield_bias'])}_",
        "",
        # ── BLOCK 3: COT
        "━━━━━━━━━━━━━━━━━━━━━━",
        "📋 *COT · CME EUR Futures 6E*",
        f"_Stand: {cot_date}_",
        "",
        f"  Net\-Position   `{net_str}` Kontrakte",
        f"  Δ Vorwoche      `{delta_str}` Kontrakte",
        f"  Long `{lp}` · Short `{sp}` · Net\-OI `{np_}`",
        f"  Open Interest   `{oi_str}` Kontrakte",
        "",
        # ── FOOTER: Gesamt-Bias + Sitzungen
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯 *Gesamt\-Bias EUR/USD*",
        "",
        f"  Zinsdiff\. {ri} `{rb}`",
        f"  Spread    {yi} `{yb}`",
        f"  COT       {ci} `{cb}`",
        "",
        f"  📅 FOMC  `{fomc_str}`",
        f"  📅 EZB   `{ecb_str}`",
        "",
        "_ECB SDMX · FRED · CFTC_",
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
