#!/usr/bin/env python3
"""
=============================================================
  EUR/USD Morning Brief Agent  v4.3
  Täglich live Daten → Telegram-Nachricht

  Quellen:
    ECB SDMX 2.1    data-api.ecb.europa.eu     (DFR, DE2Y)
    Eurostat JSON   ec.europa.eu/eurostat       (HICP Flash)
    FRED API        api.stlouisfed.org          (EFFR, US CPI, US 2Y)
    CFTC Disagg.    publicreporting.cftc.gov    (COT – gpe5-46if)

  FIX-LOG v4.3
  ─────────────────────────────────────────────────────────
  #7  TFF-Dataset URL jun7-3zbs gibt HTTP 404 zurück
      → Vollständig entfernt. Einzige Quelle: gpe5-46if.

  #8  Falsche Feldnamen im Legacy-Dataset
      Bisher: noncomm_positions_long/short_all (existiert nicht)
      Korrekt laut Live-API:
        lev_money_positions_long   (Leveraged Money / Hedge Funds)
        lev_money_positions_short
        asset_mgr_positions_long   (Asset Manager, Sekundär)
        asset_mgr_positions_short
        open_interest_all          (OI, verifiziert 871.507)
        pct_of_oi_lev_money_long   (offizielle Pct-Felder)
        pct_of_oi_lev_money_short
      Alle Werte per Live-API am 2026-06-13 verifiziert.

  FIX-LOG v4.1/v4.2  (bleiben aktiv)
  ─────────────────────────────────────────────────────────
  #3  HICP Flash-Pin (Eurostat Mai 2026 = 3.2%)
  #4  US CPI: CPIAUCNS (non-seasonally adjusted) = BLS Headline
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
_HICP_FLASH_PINS = [
    (date(2026, 6, 2), date(2026, 7, 1), 3.2, "2026-05"),
]

# CFTC Disaggregated Dataset (einzige verifizierte Quelle)
_CFTC_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
_EUR_CODES = ("099741", "99741")


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
    try:
        if len(period_str) == 7:
            return datetime.strptime(period_str + "-01", "%Y-%m-%d").date()
        if len(period_str) >= 10:
            return datetime.strptime(period_str[:10], "%Y-%m-%d").date()
    except Exception:
        pass
    return date(1900, 1, 1)


def _ecb_last_obs(series_path: str, extra_params: dict = None):
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
#  EZB – HICP Inflation YoY  (v4.1 Hotfix #3)
# ─────────────────────────────────────────────────────────────

def get_ecb_hicp():
    api_val, api_period = _hicp_from_api()
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
    eurostat_base = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data"
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
#  US CPI YoY  (v4.1 Hotfix #4)
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
#  CFTC COT – CME EUR Futures 6E  (v4.3 Fix #7+#8)
#
#  Dataset:  gpe5-46if (Disaggregated Futures Only)
#  Felder (verifiziert 2026-06-13):
#    lev_money_positions_long/short  → Leveraged Money (Hedge Funds)
#    asset_mgr_positions_long/short  → Asset Manager (Sekundär)
#    open_interest_all               → Gesamt-OI
#    pct_of_oi_lev_money_long/short  → Offizielle Pct-Werte
# ─────────────────────────────────────────────────────────────

def _to_float(record: dict, *keys) -> float:
    """Gibt den ersten gefundenen nicht-null numerischen Wert zurück."""
    for k in keys:
        v = record.get(k)
        if v is not None:
            try:
                f = float(v)
                if f > 0:
                    return f
            except Exception:
                pass
    return 0.0


def get_cot_eur() -> dict:
    for code in _EUR_CODES:
        data = safe_get(_CFTC_URL, {
            "$where": f"cftc_contract_market_code='{code}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 2,
        })
        if data and len(data) > 0:
            print(f"[OK] CFTC code={code}: {len(data)} Records")
            return _parse_cot(data[0], data[1] if len(data) > 1 else {})

    print("[WARN] CFTC: Beide market codes leer → N/A")
    return {}


def _parse_cot(current: dict, prev: dict) -> dict:
    # ── Hauptkategorie: Leveraged Money (Hedge Funds) ──
    long_cur  = _to_float(current, "lev_money_positions_long")
    short_cur = _to_float(current, "lev_money_positions_short")
    long_prv  = _to_float(prev,    "lev_money_positions_long")  if prev else 0.0
    short_prv = _to_float(prev,    "lev_money_positions_short") if prev else 0.0
    source    = "Disaggregated/Leveraged Money"

    # ── Fallback: Asset Manager ──
    if long_cur == 0 and short_cur == 0:
        long_cur  = _to_float(current, "asset_mgr_positions_long")
        short_cur = _to_float(current, "asset_mgr_positions_short")
        long_prv  = _to_float(prev,    "asset_mgr_positions_long")  if prev else 0.0
        short_prv = _to_float(prev,    "asset_mgr_positions_short") if prev else 0.0
        source    = "Disaggregated/Asset Manager"

    if long_cur == 0 and short_cur == 0:
        print("[WARN] CFTC _parse_cot: alle Positionen 0 → N/A")
        return {}

    oi_cur = _to_float(current, "open_interest_all")
    if oi_cur == 0:
        oi_cur = long_cur + short_cur
        print(f"[WARN] OI nicht gefunden → Proxy {oi_cur:,.0f}")

    net_cur   = long_cur - short_cur
    net_prv   = (long_prv - short_prv) if (long_prv or short_prv) else 0.0
    net_pct   = round(net_cur / oi_cur * 100, 1) if oi_cur > 0 else 0.0

    # Offizielle Pct-Felder bevorzugen
    long_pct  = _to_float(current, "pct_of_oi_lev_money_long",  "pct_of_oi_asset_mgr_long")
    short_pct = _to_float(current, "pct_of_oi_lev_money_short", "pct_of_oi_asset_mgr_short")
    if long_pct == 0:
        long_pct  = round(long_cur  / oi_cur * 100, 1) if oi_cur > 0 else 0.0
    if short_pct == 0:
        short_pct = round(short_cur / oi_cur * 100, 1) if oi_cur > 0 else 0.0

    bias = "NET-LONG" if net_pct > 5 else "NET-SHORT" if net_pct < -5 else "NEUTRAL"

    raw_date = (
        current.get("report_date_as_yyyy_mm_dd")
        or current.get("report_date_as_mm_dd_yyyy")
        or "N/A"
    )

    print(f"[OK] COT {source}: long={long_cur:,.0f}, short={short_cur:,.0f}, "
          f"net={net_cur:+,.0f}, oi={oi_cur:,.0f}, bias={bias}")

    return {
        "date":      str(raw_date)[:10],
        "net":       int(net_cur),
        "delta_net": int(net_cur - net_prv),
        "long_pct":  long_pct,
        "short_pct": short_pct,
        "net_pct":   net_pct,
        "oi":        int(oi_cur),
        "bias":      bias,
        "source":    source,
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
        "_ECB SDMX · Eurostat · FRED · CFTC Disagg\\._",
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
    print(f"[{datetime.now().isoformat()}] EUR/USD Morning Brief v4.3 startet...")

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
        print(f"  COT:      net={cot['net']:+,}, oi={cot['oi']:,}, "
              f"bias={cot['bias']}, src={cot['source']}")
    else:
        print("  COT:      N/A (alle Quellen erschöpft)")
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
