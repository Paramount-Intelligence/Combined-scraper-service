import subprocess
import sys
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

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
    ("MBOPartners", "scrapers/mbopartners/mbop_monitor.py"),
    ("Outsized", "scrapers/outsized/outsized_monitor.py"),
    ("Reed", "scrapers/reed/reed_monitor.py"),
    ("Talmix", "scrapers/talmix/talmix_monitor.py"),
]

SPREADSHEET_SCRIPT = "scrapers/spreadsheet_insert/insert_to_spreadsheet.py"

def send_error_email(errors):
    """Send an SMTP email notification detailing which scripts failed."""
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        print("⚠️ SMTP credentials not set. Cannot send error email alert.")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"❌ Scraper Service Failures - {datetime.now().strftime('%Y-%m-%d')}"
        msg["From"] = SENDER_EMAIL
        msg["To"] = ERROR_RECIPIENT

        # Construct HTML Email body
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
            <div style="max-width: 800px; margin: 20px auto; padding: 20px; border: 1px solid #ecc; border-radius: 8px; background-color: #fffaf0;">
                <h2 style="color: #d9534f; border-bottom: 2px solid #d9534f; padding-bottom: 10px;">⚠️ Scraper Execution Failures</h2>
                <p>The daily scraper service ran into errors. The following script(s) failed to complete successfully:</p>
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
            snippet = error_log[-1000:] if len(error_log) > 1000 else error_log
            html_body += f"""
                        <tr>
                            <td style="padding: 10px; border: 1px solid #ddd; font-weight: bold; vertical-align: top;">{name}</td>
                            <td style="padding: 10px; border: 1px solid #ddd; color: #d9534f; vertical-align: top;">{code}</td>
                            <td style="padding: 10px; border: 1px solid #ddd; background-color: #fdf5f5; font-family: monospace; white-space: pre-wrap; font-size: 12px; vertical-align: top;">{snippet}</td>
                        </tr>
            """
        html_body += """
                    </tbody>
                </table>
                <p style="font-size: 12px; color: #777; margin-top: 30px; border-top: 1px solid #ddd; padding-top: 10px;">
                    This is an automated error report sent from your Railway scraper service.
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

        print(f"📧 Error notification email sent successfully to {ERROR_RECIPIENT}.")
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

    for name, path in SCRAPERS:
        print(f"\n▶️ Running {name} Scraper ({path})...")
        if not os.path.exists(path):
            err_msg = f"Script file not found at {path}"
            print(f"❌ {err_msg}")
            execution_errors.append((name, -1, err_msg))
            continue

        cwd = os.path.dirname(path)
        script_name = os.path.basename(path)

        try:
            # Capture output in case of failure
            result = subprocess.run(
                ["python", "-u", script_name, "--once"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True
            )
            print(f"✅ Finished {name} Scraper successfully.")
        except subprocess.CalledProcessError as e:
            print(f"❌ {name} Scraper failed with exit code {e.returncode}.")
            # Append last 20 lines of stdout/stderr for diagnostic context
            output_log = (e.stdout or "") + "\n" + (e.stderr or "")
            execution_errors.append((name, e.returncode, output_log))

    # Always run spreadsheet insert script, even if some scrapers failed
    print(f"\n▶️ Running Spreadsheet Insertion Script ({SPREADSHEET_SCRIPT})...")
    if os.path.exists(SPREADSHEET_SCRIPT):
        cwd = os.path.dirname(SPREADSHEET_SCRIPT)
        script_name = os.path.basename(SPREADSHEET_SCRIPT)
        try:
            subprocess.run(
                ["python", "-u", script_name],
                cwd=cwd,
                check=True
            )
            print("✅ Finished Spreadsheet Insertion successfully.")
        except subprocess.CalledProcessError as e:
            print(f"❌ Spreadsheet insertion script failed with exit code {e.returncode}.")
            output_log = (e.stdout or "") + "\n" + (e.stderr or "")
            execution_errors.append(("Spreadsheet Insertion", e.returncode, output_log))
    else:
        err_msg = f"Spreadsheet script file not found at {SPREADSHEET_SCRIPT}"
        print(f"❌ {err_msg}")
        execution_errors.append(("Spreadsheet Insertion", -1, err_msg))

    print("\n=========================================")
    print("🏁 Execution Summary")
    print("=========================================")
    if execution_errors:
        print(f"⚠️ Service completed with {len(execution_errors)} error(s). Sending alert email...")
        send_error_email(execution_errors)
        # Exit with error status to report failure to Railway logs
        sys.exit(1)
    else:
        print("🎉 Service completed successfully! All tasks completed without errors.")
        sys.exit(0)

if __name__ == "__main__":
    main()
