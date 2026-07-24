import subprocess
import sys
import os
import re
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
ERROR_RECIPIENT = os.getenv("ERROR_RECIPIENT", "ziadin.544@gmail.com")

SCRAPERS = [
    ("Aquent", "scrapers/aquent/aquent_monitor.py"),
    ("ConsultingHeads", "scrapers/consultingheads/consultingheads_monitor.py"),
    ("Eond", "scrapers/eond/eond_monitor.py"),
    ("Expert360", "scrapers/expert360/expert360_monitor.py"),
    ("MBOPartners", "scrapers/mbopartners/mbop_monitor.py"),
    ("Outsized", "scrapers/outsized/outsized_monitor.py"),
    ("Outvise", "scrapers/outvise/outvise_monitor.py"),
    ("Reed", "scrapers/reed/reed_monitor.py"),
    ("Talmix", "scrapers/talmix/talmix_monitor.py"),
]

SPREADSHEET_SCRIPT = "scrapers/spreadsheet_insert/insert_to_spreadsheet.py"

def summarize_scraper_failure(name, stdout, stderr, returncode):
    """
    Build a useful failure summary: root exception first, then a limited tail.
    Prefer meaningful Selenium/Chrome lines over native hex stack frames.
    """
    combined = f"{stdout or ''}\n{stderr or ''}"
    non_empty = [ln.strip() for ln in combined.splitlines() if ln.strip()]
    tail = non_empty[-100:]

    priority_markers = [
        "Expert360 fatal error",
        "Expert360 WebDriver failure",
        "WebDriverException",
        "SessionNotCreatedException",
        "session not created",
        "Chrome failed to start",
        "DevToolsActivePort",
        "user data directory is already in use",
        "Chrome not reachable",
        "disconnected",
        "invalid session id",
        "cannot find Chrome binary",
        "no chrome binary",
        "missing shared library",
        "No Chrome or Chromium executable was found",
        "browser start failed",
    ]

    root = None
    for line in non_empty:
        lower = line.lower()
        if any(marker.lower() in lower for marker in priority_markers):
            root = line
            break

    if not root:
        hex_frame = re.compile(r"^(?:#\d+\s+)?0x[0-9a-fA-F]+\b")
        for line in reversed(non_empty):
            if hex_frame.match(line):
                continue
            if line.lower() in {"<unknown>", "unknown"}:
                continue
            if sum(ch.isalpha() for ch in line) < 8:
                continue
            root = line
            break

    parts = []
    if root:
        parts.append(f"Root error: {root}")
    if tail:
        parts.append("Recent log tail:")
        parts.extend(tail[-40:])
    summary = "\n".join(parts) if parts else f"{name} failed with exit code {returncode}"
    return summary


def send_status_email(errors, summaries=None, spreadsheet_status=None):
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
                "PARTIAL": "#f0ad4e", "RUNTIME_GUARD": "#f0ad4e",
            }
            for name, status, detail in summaries:
                color = status_colors.get(status, "#d9534f" if status.startswith("EXIT_") else "#999")
                icon = "&#x2705;" if status == "OK" else "&#x26A0;" if status in ("EMPTY", "AUTH_FAIL", "PARTIAL", "RUNTIME_GUARD") else "&#x274C;"
                detail_short = (detail[:120] + "...") if len(detail) > 120 else detail
                summary_table += f"""
                    <tr>
                        <td style="padding: 8px 10px; border: 1px solid #ddd; font-weight: bold;">{name}</td>
                        <td style="padding: 8px 10px; border: 1px solid #ddd; color: {color}; font-weight: bold;">{icon} {status}</td>
                        <td style="padding: 8px 10px; border: 1px solid #ddd; font-size: 12px; color: #666;">{detail_short}</td>
                    </tr>
                """
            summary_table += "</tbody></table>"

        hard_errors = [
            e for e in (errors or [])
            if not (isinstance(e, tuple) and len(e) >= 2 and e[1] in (10, 11))
        ]
        ss = spreadsheet_status or {}
        ss_label = ss.get("label", "")
        ss_detail = ss.get("detail", "")

        if hard_errors:
            msg["Subject"] = f"❌ Scraper Service Failures - {datetime.now().strftime('%Y-%m-%d')}"
            header_color = "#d9534f"
            header_text = "⚠️ Scraper Execution Failures"
            intro_text = "The daily scraper service ran into errors. The following script(s) failed or produced no results:"
            if ss_label:
                intro_text += f"<br><br><strong>Spreadsheet:</strong> {ss_label}. {ss_detail}"

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
            for name, code, error_log in hard_errors:
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
        elif ss.get("partial"):
            msg["Subject"] = f"⚠️ Scraper Service Partial Success - {datetime.now().strftime('%Y-%m-%d')}"
            header_color = "#f0ad4e"
            header_text = "⚠️ Scraper Execution Partial Success"
            intro_text = ss_detail or (
                "Partial success: some spreadsheet rows were inserted; "
                "remaining AI records were retained for the next run."
            )
            table_content = ""
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

def log_browser_versions():
    """Print Chromium/ChromeDriver versions and resolved paths at startup."""
    import subprocess as _sp
    chrome_bin = os.getenv("CHROME_BIN", "/usr/bin/chromium")
    chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
    print("🌐 Browser Environment:")
    print(f"   CHROME_BIN         : {chrome_bin}")
    print(f"   CHROMEDRIVER_PATH  : {chromedriver_path}")
    try:
        ver = _sp.check_output([chrome_bin, "--version"], stderr=_sp.STDOUT, timeout=10).decode().strip()
        print(f"   Chromium version   : {ver}")
    except Exception as e:
        print(f"   Chromium version   : ⚠️ could not determine ({e})")
    try:
        ver = _sp.check_output([chromedriver_path, "--version"], stderr=_sp.STDOUT, timeout=10).decode().strip()
        print(f"   ChromeDriver version: {ver}")
    except Exception as e:
        print(f"   ChromeDriver version: ⚠️ could not determine ({e})")
    print()

def main():
    print("=========================================")
    print("🚀 Starting Daily Scraper Service Orchestration")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=========================================")
    log_browser_versions()

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
                "failed to load cookies", "re-login failed", "authentication failed",
                "email or password are incorrect",
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
            output_log = summarize_scraper_failure(
                name, e.stdout or "", e.stderr or "", e.returncode
            )
            # Print useful summary + recent lines for Railway logs
            print(f"❌ {name} → EXIT_{e.returncode}")
            for line in output_log.splitlines()[:25]:
                print(f"  [{name}] {line}")
            if e.stdout:
                for line in e.stdout.splitlines()[-20:]:
                    print(f"  [{name}] {line}")
            if e.stderr:
                for line in e.stderr.splitlines()[-10:]:
                    print(f"  [{name} STDERR] {line}")
            execution_errors.append((name, e.returncode, output_log))
            # Prefer root-error line in the compact summary table
            root_line = ""
            for line in output_log.splitlines():
                if line.startswith("Root error:"):
                    root_line = line
                    break
            scraper_summaries.append(
                (name, f"EXIT_{e.returncode}", root_line or output_log[:500])
            )

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
    spreadsheet_status = {"partial": False, "label": "", "detail": ""}
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
                check=False,
                # Parent hard limit ~3600s; insert script self-guards earlier (~3300s).
                timeout=3600
            )
            out = result.stdout or ""
            err = result.stderr or ""
            if out:
                for line in out.splitlines():
                    print(f"  [Spreadsheet] {line}")
            if err:
                for line in err.splitlines():
                    print(f"  [Spreadsheet STDERR] {line}")

            run_status_line = ""
            for line in out.splitlines():
                if line.startswith("RUN_STATUS="):
                    run_status_line = line.split("=", 1)[-1].strip()

            code = result.returncode
            # Exit codes from insert_to_spreadsheet.py:
            # 0 success, 10 partial, 11 runtime guard, 12 permanent config, 13 webhook
            if code == 0:
                print("✅ Finished Spreadsheet Insertion successfully.")
                scraper_summaries.append(("Spreadsheet Insertion", "OK", run_status_line or "success"))
                spreadsheet_status = {
                    "partial": False,
                    "label": "Completed successfully",
                    "detail": "All eligible spreadsheet rows were inserted.",
                }
            elif code == 10:
                print("⚠️ Spreadsheet insertion completed with deferred AI records.")
                scraper_summaries.append(("Spreadsheet Insertion", "PARTIAL", run_status_line or "partial_success"))
                spreadsheet_status = {
                    "partial": True,
                    "label": "Completed with deferred AI records",
                    "detail": (
                        "Partial success: some rows were inserted; "
                        "remaining AI records were retained for the next run."
                    ),
                }
            elif code == 11:
                print("⚠️ Spreadsheet insertion stopped safely due to runtime guard.")
                scraper_summaries.append(("Spreadsheet Insertion", "RUNTIME_GUARD", run_status_line or "runtime_guard"))
                spreadsheet_status = {
                    "partial": True,
                    "label": "Stopped safely because runtime limit approached",
                    "detail": (
                        "Runtime guard activated: completed rows were flushed; "
                        "remaining records stay uninserted for the next run."
                    ),
                }
            elif code == 12:
                detail = "Failed because of permanent Gemini configuration error"
                print(f"❌ {detail}")
                execution_errors.append(("Spreadsheet Insertion", code, out + "\n" + err))
                scraper_summaries.append(("Spreadsheet Insertion", f"EXIT_{code}", detail))
                spreadsheet_status = {
                    "partial": False,
                    "label": detail,
                    "detail": detail,
                }
            elif code == 13:
                detail = "Failed because spreadsheet webhook rejected a batch"
                print(f"❌ {detail}")
                execution_errors.append(("Spreadsheet Insertion", code, out + "\n" + err))
                scraper_summaries.append(("Spreadsheet Insertion", f"EXIT_{code}", detail))
                spreadsheet_status = {
                    "partial": False,
                    "label": detail,
                    "detail": detail,
                }
            else:
                print(f"❌ Spreadsheet insertion script failed with exit code {code}.")
                execution_errors.append(("Spreadsheet Insertion", code, out + "\n" + err))
                scraper_summaries.append(("Spreadsheet Insertion", f"EXIT_{code}", run_status_line))
                spreadsheet_status = {
                    "partial": False,
                    "label": f"Failed with exit code {code}",
                    "detail": run_status_line or f"Exit code {code}",
                }
        except subprocess.TimeoutExpired:
            err_msg = "Spreadsheet insertion timed out after 3600s"
            print(f"❌ {err_msg}")
            execution_errors.append(("Spreadsheet Insertion", -2, err_msg))
            scraper_summaries.append(("Spreadsheet Insertion", "TIMEOUT", err_msg))
            spreadsheet_status = {
                "partial": False,
                "label": "Timed out",
                "detail": err_msg,
            }
    else:
        err_msg = f"Spreadsheet script file not found at {SPREADSHEET_SCRIPT}"
        print(f"❌ {err_msg}")
        execution_errors.append(("Spreadsheet Insertion", -1, err_msg))
        scraper_summaries.append(("Spreadsheet Insertion", "MISSING", err_msg))

    print("\n=========================================")
    print("🏁 Execution Summary")
    print("=========================================")
    print("📧 Sending execution status email...")
    send_status_email(
        execution_errors,
        summaries=scraper_summaries,
        spreadsheet_status=spreadsheet_status,
    )

    hard_errors = [
        e for e in execution_errors
        if not (isinstance(e, tuple) and len(e) >= 2 and e[1] in (10, 11))
    ]
    if hard_errors:
        sys.exit(1)
    if spreadsheet_status.get("partial"):
        print("⚠️ Service completed with partial spreadsheet success (deferred/runtime-guard).")
        sys.exit(0)
    print("🎉 Service completed successfully! All tasks completed without errors.")
    sys.exit(0)

if __name__ == "__main__":
    main()
