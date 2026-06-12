# 📊 EUR/USD Morning Brief Agent

Täglicher automatischer Telegram-Report mit live Forex-Makrodaten – vollautomatisch via GitHub Actions.

## Was wird täglich geliefert?

| Datenpunkt | Quelle | Frequenz |
|---|---|---|
| EZB Leitzins (DFR) | ECB SDMX 2.1 API | Täglich |
| Eurozone HVPI YoY | ECB SDMX 2.1 API | Monatlich |
| Fed EFFR | FRED API (DFF) | Täglich |
| US CPI YoY | FRED API (CPIAUCSL_PC1) | Monatlich |
| US 2Y Treasury Yield | FRED API (DGS2) | Täglich |
| DE 2Y Bundesanleihe | ECB SDMX API (YC) | Täglich |
| COT EUR Futures 6E | CFTC Socrata API | Wöchentlich (Fr.) |
| Zinsdifferenz-Signal | berechnet | täglich |
| Rendite-Spread 2Y | berechnet | täglich |
| Nächste FOMC/EZB-Sitzung | Kalender 2026 | – |

## Setup

### 1. FRED API-Key holen (kostenlos)

→ https://fred.stlouisfed.org/docs/api/api_key.html

### 2. Telegram Bot erstellen

1. [@BotFather](https://t.me/BotFather) öffnen → `/newbot`
2. Namen und Username wählen
3. Token kopieren
4. Chat-ID ermitteln: schreib deinem Bot `/start`, dann aufrufen:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   → `message.chat.id` ablesen

### 3. GitHub Secrets hinterlegen

GitHub Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret Name | Wert |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Dein Bot-Token von BotFather |
| `TELEGRAM_CHAT_ID` | Deine persönliche Chat-ID |
| `FRED_API_KEY` | Dein FRED API-Key |

### 4. Fertig!

Der Agent läuft ab sofort jeden Werktag automatisch um ~07:00–08:00 MEZ.

Manuell auslösen: GitHub → Actions → "EUR/USD Morning Brief" → **Run workflow**

## Lokaler Testlauf

```bash
pip install -r requirements.txt
cp .env.example .env
# .env mit echten Werten befüllen
python eurusd_morning_brief.py
```

Ohne gesetztes `TELEGRAM_BOT_TOKEN` wird die Nachricht nur im Terminal angezeigt.

## Datenquellen

- **ECB SDMX 2.1 API** – `data-api.ecb.europa.eu` – kostenlos, kein Key
- **FRED API** – `api.stlouisfed.org` – kostenlos, Key erforderlich
- **CFTC Socrata API** – `publicreporting.cftc.gov` – kostenlos, kein Key
