# Secrets & Einrichtung

Dieses Dokument erklärt wie du alle Zugangsdaten sicher hinterlegst –
für GitHub Actions und Cloudflare Workers.

---

## 1 · GitHub Actions Secrets

### Wo einstellen

**[Repository → Settings → Secrets and variables → Actions → New repository secret](https://github.com/noahhwagnerr-wq/eurusd-morning-brief/settings/secrets/actions)**

### Welche Secrets

| Secret Name | Pflicht | Beschreibung |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot-Token von @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Deine persönliche Chat-ID |
| `FRED_API_KEY` | ✅ | FRED API Key (kostenlos) |
| `MYFXBOOK_SESSION` | ⬜ | Myfxbook Session (optional) |

---

## 2 · TELEGRAM_BOT_TOKEN holen

1. Telegram öffnen → [@BotFather](https://t.me/BotFather) suchen
2. `/newbot` senden
3. Bot-Namen eingeben (z.B. `MorningBriefBot`)
4. Username eingeben (muss auf `bot` enden, z.B. `eurusd_morning_bot`)
5. BotFather schickt dir den Token: `7xxxxxxxx:AAF...`
6. Diesen Token als `TELEGRAM_BOT_TOKEN` in GitHub Secrets eintragen

---

## 3 · TELEGRAM_CHAT_ID holen

**Methode A – getUpdates (einfachste Methode):**

1. Schreib deinem Bot irgendeine Nachricht
2. Öffne im Browser (Token ersetzen):
   ```
   https://api.telegram.org/bot<DEIN_TOKEN>/getUpdates
   ```
3. Im JSON siehst du: `"chat":{"id":123456789}`
4. Diese Zahl als `TELEGRAM_CHAT_ID` in GitHub Secrets eintragen

**Methode B – @userinfobot:**

1. [@userinfobot](https://t.me/userinfobot) auf Telegram suchen
2. `/start` senden → Bot antwortet mit deiner Chat-ID

**Hinweis:** Für Gruppen-Chats beginnt die ID mit `-100...`

---

## 4 · FRED_API_KEY holen

1. Gehe zu [https://fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html)
2. Klick auf "Request or view your API keys"
3. Kostenloser Account bei St. Louis Fed erstellen
4. API Key wird sofort generiert (32-stellig)
5. Als `FRED_API_KEY` in GitHub Secrets eintragen

---

## 5 · MYFXBOOK_SESSION (optional)

> **Nicht nötig!** Die Community Outlook Daten funktionieren ohne Session.
> Die Session-ID gibt nur Zugriff auf deine persönlichen Konto-Daten.

Falls du es trotzdem willst:
1. Bei [myfxbook.com](https://www.myfxbook.com) einloggen
2. API-Login: `https://api.myfxbook.com/api/login.json?email=...&password=...`
3. Session-ID aus der Antwort als `MYFXBOOK_SESSION` eintragen

---

## 6 · Cloudflare Workers Secrets

Falls du den Worker unter `worker/` deployest:

```bash
cd worker
npm install

# Secrets setzen (wird sicher in Cloudflare gespeichert, NICHT in wrangler.toml)
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_CHAT_ID
wrangler secret put FRED_API_KEY
wrangler secret put MYFXBOOK_SESSION   # optional

# Deployen
npm run deploy

# Live-Logs überwachen
npm run tail

# Sofort manuell triggern (nach Deploy)
curl https://eurusd-morning-brief.<dein-subdomain>.workers.dev/run
```

### Worker Cron

Der Worker läuft täglich um **05:00 UTC = 07:00 CEST** – auch Samstag und Sonntag.
Definiert in `worker/wrangler.toml`:
```toml
[triggers]
crons = ["0 5 * * *"]
```

---

## 7 · Beide Systeme parallel betreiben?

Ja, das ist möglich. GitHub Actions und Cloudflare Worker sind unabhängig.

**Empfehlung:** Nur eines aktiv lassen, sonst erhältst du doppelte Nachrichten.

| System | Vorteile | Nachteile |
|---|---|---|
| **GitHub Actions** | Kein Deployment nötig, alles in diesem Repo | GitHub kann cron ±30min verzögern |
| **Cloudflare Worker** | Pünktlicher, global, 0ms Cold-Start | `wrangler deploy` nötig |

---

## 8 · Überprüfung

Nach dem Einrichten der Secrets:

1. [Actions → trigger.yml → Run workflow](https://github.com/noahhwagnerr-wq/eurusd-morning-brief/actions/workflows/trigger.yml)
2. Manuell triggern
3. Brief sollte innerhalb 2 Minuten auf Telegram ankommen
