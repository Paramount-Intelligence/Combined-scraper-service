import subprocess
import sys
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# Ensure UTF-8 output on all platforms (fixes Windows emoji crash)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Load env variables from local file if running locally
load_dotenv()

# Configuration for SMTP notifications (read from environment)
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
ERROR_RECIPIENT = os.getenv("ERROR_RECIPIENT", "irfaanexe@gmail.com")

SCRAPERS = [
    ("Aquent", "scrapers/aquent/aquent_monitor.py"),
    ("Eond", "scrapers/eond/eond_monitor.py"),
    ("Expert360", "scrapers/expert360/expert360_monitor.py"),
    ("MBOPartners", "scrapers/mbopartners/mbop_monitor.py"),
    ("Outsized", "scrapers/outsized/outsized_monitor.py"),
    ("Reed", "scrapers/reed/reed_monitor.py"),
    ("Talmix", "scrapers/talmix/talmix_monitor.py"),
]

SPREADSHEET_SCRIPT = "scrapers/spreadsheet_insert/insert_to_spreadsheet.py"

def send_status_email(errors, summaries=None):
    """Send an SMTP email notification detailing execution status."""
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        print("⚠️ SMTP credentials not set. Cannot send status email alert.")
        return

    try:
        msg = MIMEMultipart("alternative")

        # Build per-scraper summary table (always included)
        summary_table = ""
        if summaries:
            summary_table = """
            <h3 style="color: #555; margin-top: 20px;">Per-Scraper Status</h3>
            <table style="width: 100%; border-collapse: collapse; margin: 10px 0;">
                <thead>
                    <tr style="background-color: #e8e8e8;">
                        <th style="padding: 8px 10px; border: 1px solid #ddd; text-align: left;">Scraper</th>
                        <th style="padding: 8px 10px; border: 1px solid #ddd; text-align: left;">Status</th>
                        <th style="padding: 8px 10px; border: 1px solid #ddd; text-align: left;">Detail</th>
                    </tr>
                </thead>
                <tbody>
            """
            status_colors = {
                "OK": "#5cb85c", "EMPTY": "#f0ad4e", "AUTH_FAIL": "#d9534f",
                "TIMEOUT": "#d9534f", "MISSING": "#d9534f",
            }
            for name, status, detail in summaries:
                color = status_colors.get(status, "#d9534f" if status.startswith("EXIT_") else "#999")
                icon = "&#x2705;" if status == "OK" else "&#x26A0;" if status in ("EMPTY", "AUTH_FAIL") else "&#x274C;"
                detail_short = (detail[:120] + "...") if len(detail) > 120 else detail
                summary_table += f"""
                    <tr>
                        <td style="padding: 8px 10px; border: 1px solid #ddd; font-weight: bold;">{name}</td>
                        <td style="padding: 8px 10px; border: 1px solid #ddd; color: {color}; font-weight: bold;">{icon} {status}</td>
                        <td style="padding: 8px 10px; border: 1px solid #ddd; font-size: 12px; color: #666;">{detail_short}</td>
                    </tr>
                """
            summary_table += "</tbody></table>"

        if errors:
            msg["Subject"] = f"❌ Scraper Service Failures - {datetime.now().strftime('%Y-%m-%d')}"
            header_color = "#d9534f"
            header_text = "⚠️ Scraper Execution Failures"
            intro_text = "The daily scraper service ran into errors. The following script(s) failed or produced no results:"

            table_content = """
            <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                <thead>
                    <tr style="background-color: #f2dede; color: #a94442;">
                        <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Script Name</th>
                        <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Exit Code</th>
                        <th style="padding: 10px; border: 1px solid #ddd; text-align: left;">Error Snippet</th>
                    </tr>
                </thead>
                <tbody>
            """
            for name, code, error_log in errors:
                snippet = error_log[-1500:] if len(error_log) > 1500 else error_log
                snippet = snippet.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                table_content += f"""
                    <tr>
                        <td style="padding: 10px; border: 1px solid #ddd; font-weight: bold; vertical-align: top;">{name}</td>
                        <td style="padding: 10px; border: 1px solid #ddd; color: #d9534f; vertical-align: top;">{code}</td>
                        <td style="padding: 10px; border: 1px solid #ddd; background-color: #fdf5f5; font-family: monospace; white-space: pre-wrap; font-size: 12px; vertical-align: top;">{snippet}</td>
                    </tr>
                """
            table_content += "</tbody></table>"
        else:
            msg["Subject"] = f"✅ Scraper Service Success - {datetime.now().strftime('%Y-%m-%d')}"
            header_color = "#5cb85c"
            header_text = "🎉 Scraper Execution Success"
            intro_text = "The daily scraper service finished successfully. All scrapers and the spreadsheet insertion script ran without any errors."
            table_content = ""

        msg["From"] = SENDER_EMAIL
        msg["To"] = ERROR_RECIPIENT

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
            <div style="max-width: 800px; margin: 20px auto; padding: 20px; border: 1px solid #ddd; border-radius: 8px; background-color: #fffaf0;">
                <h2 style="color: {header_color}; border-bottom: 2px solid {header_color}; padding-bottom: 10px;">{header_text}</h2>
                <p>{intro_text}</p>
                {table_content}
                {summary_table}
                <p style="font-size: 12px; color: #777; margin-top: 30px; border-top: 1px solid #ddd; padding-top: 10px;">
                    This is an automated status report sent from your Railway scraper service.
                </p>
            </div>
        </body>
        </html>
        """
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)

        print(f"📧 Status notification email sent successfully to {ERROR_RECIPIENT}.")
    except Exception as e:
        print(f"❌ Failed to send error notification email: {e}")

def main():
    print("=========================================")
    print("🚀 Starting Daily Scraper Service Orchestration")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=========================================")

    # Enforce email suppression for the scraper monitors
    os.environ["SEND_EMAILS"] = "False"

    execution_errors = []
    scraper_summaries = []

    for name, path in SCRAPERS:
        print(f"\n▶️ Running {name} Scraper ({path})...")
        if not os.path.exists(path):
            err_msg = f"Script file not found at {path}"
            print(f"❌ {err_msg}")
            execution_errors.append((name, -1, err_msg))
            scraper_summaries.append((name, "MISSING", err_msg))
            continue

        cwd = os.path.dirname(path)
        script_name = os.path.basename(path)

        try:
            result = subprocess.run(
                ["python", "-u", script_name, "--once"],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=300
            )
            stdout = result.stdout or ""
            stderr = result.stderr or ""

            # Print the scraper's full output so Railway logs capture it
            if stdout:
                for line in stdout.splitlines():
                    print(f"  [{name}] {line}")
            if stderr:
                for line in stderr.splitlines():
                    print(f"  [{name} STDERR] {line}")

            # Detect silent failures: scraper exited 0 but found nothing
            stdout_lower = stdout.lower()
            has_projects = any(marker in stdout_lower for marker in [
                "extracted", "found", "new project", "new job", "new opportunity",
                "seeding complete", "reconciled", "stats:"
            ])
            login_failed = any(marker in stdout_lower for marker in [
                "failed to authenticate", "failed to establish", "login failed",
                "could not find email", "cookies expired", "session expired",
                "failed to load cookies", "re-login failed"
            ])
            no_projects = any(marker in stdout_lower for marker in [
                "no projects found", "no jobs found", "no opportunities found",
                "timeout waiting"
            ])

            if login_failed:
                warn_msg = f"{name} exited OK but login/auth failed — no data was scraped"
                print(f"⚠️ {warn_msg}")
                execution_errors.append((name, 0, f"AUTH FAILURE\n\n{stdout[-2000:]}\n{stderr[-500:]}"))
                scraper_summaries.append((name, "AUTH_FAIL", warn_msg))
            elif no_projects and not has_projects:
                warn_msg = f"{name} exited OK but found 0 projects — possible selector or page change"
                print(f"⚠️ {warn_msg}")
                execution_errors.append((name, 0, f"ZERO RESULTS\n\n{stdout[-2000:]}\n{stderr[-500:]}"))
                scraper_summaries.append((name, "EMPTY", warn_msg))
            else:
                print(f"✅ Finished {name} Scraper successfully.")
                scraper_summaries.append((name, "OK", ""))

        except subprocess.TimeoutExpired:
            err_msg = f"{name} Scraper timed out after 300s"
            print(f"❌ {err_msg}")
            execution_errors.append((name, -2, err_msg))
            scraper_summaries.append((name, "TIMEOUT", err_msg))
        except subprocess.CalledProcessError as e:
            print(f"❌ {name} Scraper failed with exit code {e.returncode}.")
            output_log = (e.stdout or "") + "\n" + (e.stderr or "")
            # Print output for Railway logs
            if e.stdout:
                for line in e.stdout.splitlines()[-20:]:
                    print(f"  [{name}] {line}")
            if e.stderr:
                for line in e.stderr.splitlines()[-10:]:
                    print(f"  [{name} STDERR] {line}")
            execution_errors.append((name, e.returncode, output_log))
            scraper_summaries.append((name, f"EXIT_{e.returncode}", output_log[-500:]))

    # Print summary table before spreadsheet step
    print(f"\n{'='*50}")
    print("📋 Scraper Results Summary")
    print(f"{'='*50}")
    for name, status, detail in scraper_summaries:
        icon = "✅" if status == "OK" else "⚠️" if status in ("EMPTY", "AUTH_FAIL") else "❌"
        print(f"  {icon} {name:15s} → {status}" + (f" ({detail[:80]})" if detail else ""))
    print(f"{'='*50}")

    # Always run spreadsheet insert script, even if some scrapers failed
    print(f"\n▶️ Running Spreadsheet Insertion Script ({SPREADSHEET_SCRIPT})...")
    if os.path.exists(SPREADSHEET_SCRIPT):
        cwd = os.path.dirname(SPREADSHEET_SCRIPT)
        script_name = os.path.basename(SPREADSHEET_SCRIPT)
        try:
            result = subprocess.run(
                ["python", "-u", script_name],
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=600
            )
            if result.stdout:
                for line in result.stdout.splitlines():
                    print(f"  [Spreadsheet] {line}")
            if result.stderr:
                for line in result.stderr.splitlines():
                    print(f"  [Spreadsheet STDERR] {line}")
            print("✅ Finished Spreadsheet Insertion successfully.")
        except subprocess.TimeoutExpired:
            err_msg = "Spreadsheet insertion timed out after 600s"
            print(f"❌ {err_msg}")
            execution_errors.append(("Spreadsheet Insertion", -2, err_msg))
        except subprocess.CalledProcessError as e:
            print(f"❌ Spreadsheet insertion script failed with exit code {e.returncode}.")
            output_log = (e.stdout or "") + "\n" + (e.stderr or "")
            if e.stdout:
                for line in e.stdout.splitlines()[-20:]:
                    print(f"  [Spreadsheet] {line}")
            if e.stderr:
                for line in e.stderr.splitlines()[-10:]:
                    print(f"  [Spreadsheet STDERR] {line}")
            execution_errors.append(("Spreadsheet Insertion", e.returncode, output_log))
    else:
        err_msg = f"Spreadsheet script file not found at {SPREADSHEET_SCRIPT}"
        print(f"❌ {err_msg}")
        execution_errors.append(("Spreadsheet Insertion", -1, err_msg))

    print("\n=========================================")
    print("🏁 Execution Summary")
    print("=========================================")
    print("📧 Sending execution status email...")
    send_status_email(execution_errors, summaries=scraper_summaries)

    if execution_errors:
        # Exit with error status to report failure to Railway logs
        sys.exit(1)
    else:
        print("🎉 Service completed successfully! All tasks completed without errors.")
        sys.exit(0)

if __name__ == "__main__":
    main()
