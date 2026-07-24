# Combined Scraper Service

This service consolidates the monitoring and spreadsheet insertion tasks for the scrapers (`aquent`, `consultingheads`, `eond`, `expert360`, `mbopartners`, `outsized`, `outvise`, `reed`, and `talmix`) into a single execution workflow deployed on Railway.

Instead of running continuous loops, the service runs once daily on a schedule to fetch new projects, save them to MongoDB, post them to your spreadsheet webhook, and shutdown cleanly to minimize costs.

---

## 📅 Schedule
* **Execution Time**: **11:30 PM PKT** (Pakistan Standard Time) daily.
* **Cron Expression**: `30 18 * * *` (18:30 UTC / 6:30 PM UTC) configured inside [railway.toml](railway.toml).

---

## 🛠️ How It Works

1. **Sequential Execution**: [run_all.py](run_all.py) runs each scraper in `--once` mode sequentially. Running sequentially avoids CPU/RAM spikes in a single container.
2. **Email Suppression**: Email alerts for individual new opportunities are bypassed via the environment flag `SEND_EMAILS=False`.
3. **Spreadsheet Sync**: After all scrapers complete, [insert_to_spreadsheet.py](scrapers/spreadsheet_insert/insert_to_spreadsheet.py) executes to push newly detected records to the spreadsheet webhook.
4. **Failure Alerts**: If any scraper script fails or crashes, the orchestrator catches the failure, proceeds with the remaining scripts, and sends a single summary email listing error logs to your recipient email at the end.

---

## ⚙️ Configuration (`.env`)

Create a `.env` file at the root directory (based on the template below) or set these variables under your Railway service's **Variables** tab:

```env
MONGO_URI=your_mongodb_connection_uri
GEMINI_API_KEY=your_gemini_api_key
GEMINI_PRIMARY_MODEL=gemini-3.5-flash-lite
GEMINI_FALLBACK_MODEL=gemini-3.6-flash
ENABLE_MODEL_FALLBACK=true
CATEGORY_CONFIDENCE_THRESHOLD=0.70
AI_ATTEMPTS_PER_MODEL=2
AI_REQUEST_DELAY_SECONDS=2
RECORD_RETRY_ROUNDS=2
SPREADSHEET_CHUNK_SIZE=5
MAX_RUN_SECONDS=3300
SHUTDOWN_RESERVE_SECONDS=180
GEMINI_TIMEOUT_MS=90000
WEBHOOK_URL=your_google_sheets_webhook_url

SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SENDER_EMAIL=your_sender_gmail
SENDER_PASSWORD=your_sender_gmail_app_password

# Email recipient for execution errors
ERROR_RECIPIENT=irfaanexe@gmail.com

# Platform-specific logins (used for cookie refreshes / sessions)
EOND_EMAIL=your_eond_email
EOND_PASSWORD=your_eond_password
TALMIX_EMAIL=your_talmix_email
TALMIX_PASSWORD=your_talmix_password
OUTVISE_EMAIL=your_outvise_email
OUTVISE_PASSWORD=your_outvise_password
# Optional: set after you know the logged-in opportunities URL
# OUTVISE_TARGET_URL=https://www.outvise.com/...
CONSULTINGHEADS_EMAIL=your_consultingheads_email
CONSULTINGHEADS_PASSWORD=your_consultingheads_password

# Expert360 browser (local defaults). Railway image sets these automatically:
# EXPERT360_HEADLESS=true
# EXPERT360_USE_PERSISTENT_PROFILE=false
# EXPERT360_BROWSER_START_ATTEMPTS=2
EXPERT360_HEADLESS=false
EXPERT360_USE_PERSISTENT_PROFILE=true
EXPERT360_BROWSER_START_ATTEMPTS=2
```

---

## 🚀 Deployment on Railway

1. **Commit and push** the combined service code to a new Git repository:
   ```bash
   git init
   git add .
   git commit -m "feat: initial commit for combined daily scraper service"
   git remote add origin <your-github-repo-url>
   git branch -M main
   git push -u origin main
   ```
2. **Create a project** in Railway and link it to your GitHub repository.
3. **Configure Variables** in the Railway Dashboard using the keys listed in the `.env` section.
   For Expert360 on Railway, ensure (or rely on Dockerfile defaults):
   ```env
   EXPERT360_HEADLESS=true
   EXPERT360_USE_PERSISTENT_PROFILE=false
   EXPERT360_BROWSER_START_ATTEMPTS=2
   ```
4. The service will automatically build via the [Dockerfile](Dockerfile) and run on the daily cron schedule defined in [railway.toml](railway.toml).
