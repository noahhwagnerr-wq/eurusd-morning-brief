#!/usr/bin/env python3
"""
=============================================================
  debug_cot.py  –  CFTC COT Rohdaten-Diagnose

  Zweck:
    Gibt alle Feldnamen + Werte der neuesten EUR-Futures
    Records aus beiden CFTC-Datensätzen aus.
    Kein Telegram, keine Verarbeitung – nur rohe API-Antwort.

  Ausführen:
    python debug_cot.py
=============================================================
"""

import json
import requests

_CFTC_TFF_URL    = "https://publicreporting.cftc.gov/resource/jun7-3zbs.json"
_CFTC_LEGACY_URL = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"
_EUR_CODES        = ("099741", "99741", "6E")


def fetch(url: str, code: str, limit: int = 2):
    try:
        r = requests.get(url, params={
            "$where": f"cftc_contract_market_code='{code}'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": limit,
        }, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [FEHLER] {e}")
        return []


def fetch_latest(url: str, limit: int = 10):
    """Holt die neuesten Einträge ohne Filter und sucht EUR."""
    try:
        r = requests.get(url, params={
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 200,
        }, timeout=25)
        r.raise_for_status()
        data = r.json()
        eur_kw = ("euro", "eur", "6e", "099741", "99741")
        found = []
        for rec in data:
            m = (
                str(rec.get("market_and_exchange_names", "")).lower()
                + str(rec.get("contract_market_name", "")).lower()
                + str(rec.get("cftc_contract_market_code", "")).lower()
            )
            if any(kw in m for kw in eur_kw):
                found.append(rec)
        return found
    except Exception as e:
        print(f"  [FEHLER] {e}")
        return []


def print_record(rec: dict, label: str):
    print(f"\n  {'─'*60}")
    print(f"  {label}")
    print(f"  {'─'*60}")
    for k, v in sorted(rec.items()):
        if v is not None and str(v).strip() != "":
            try:
                fv = float(v)
                if "long" in k.lower() or "short" in k.lower() or "interest" in k.lower():
                    print(f"  *** {k:<55} = {v}")
                else:
                    print(f"      {k:<55} = {v}")
            except Exception:
                print(f"      {k:<55} = {v}")


def main():
    print("="*70)
    print("  CFTC COT Rohdaten-Diagnose")
    print("="*70)

    for label, url in [("TFF Disaggregated (jun7-3zbs)", _CFTC_TFF_URL),
                       ("Legacy (gpe5-46if)", _CFTC_LEGACY_URL)]:
        print(f"\n{'━'*70}")
        print(f"  Datensatz: {label}")
        print(f"{'━'*70}")

        found_any = False
        for code in _EUR_CODES:
            print(f"\n  >> Suche mit market_code='{code}'...")
            rows = fetch(url, code)
            if rows:
                found_any = True
                print(f"  >> {len(rows)} Record(s) gefunden")
                for i, rec in enumerate(rows):
                    print_record(rec, f"Record {i+1} (code={code})")
                break
            else:
                print(f"  >> Keine Treffer für code='{code}'")

        if not found_any:
            print(f"\n  >> Kein Treffer mit allen Codes – versuche freie EUR-Suche...")
            rows = fetch_latest(url)
            if rows:
                print(f"  >> {len(rows)} EUR-Record(s) via freie Suche gefunden")
                for i, rec in enumerate(rows[:2]):
                    print_record(rec, f"EUR-Record {i+1} (frei gesucht)")
            else:
                print("  >> KEIN EUR-Record gefunden – API evtl. nicht erreichbar")

    print("\n" + "="*70)
    print("  Diagnose abgeschlossen")
    print("="*70)


if __name__ == "__main__":
    main()
