# Combined Scraper Service

This service consolidates the monitoring and spreadsheet insertion tasks for the 6 scrapers (`aquent`, `eond`, `mbopartners`, `outsized`, `reed`, and `talmix`) into a single execution workflow deployed on Railway.

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
GROQ_API_KEY=your_groq_api_key
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
4. The service will automatically build via the [Dockerfile](Dockerfile) and run on the daily cron schedule defined in [railway.toml](railway.toml).
