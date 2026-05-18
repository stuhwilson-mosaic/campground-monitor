#!/usr/bin/env python3
"""
Yosemite Campsite Availability Monitor
Polls recreation.gov and sends notifications when sites open up.

Usage:
  python monitor.py                                          # use file defaults
  python monitor.py --check-in 2026-04-10 --check-out 2026-04-12
  python monitor.py --interval 2 --no-email --ntfy
  python monitor.py -h                                       # see all options
"""

import argparse
import logging
import sys
import time
import winsound

import requests

from app.monitor_engine import (
    check_campground as _engine_check,
    dates_needed as _engine_dates_needed,
    send_email as _engine_send_email,
    send_ntfy as _engine_send_ntfy,
)

# ─────────────────────────────────────────────────────────────
# NOTIFICATION TOGGLES — flip these to turn each channel on/off
# ─────────────────────────────────────────────────────────────
ENABLE_EMAIL = False
ENABLE_NTFY = True

# ─────────────────────────────────────────────────────────────
# EMAIL CONFIG
# ─────────────────────────────────────────────────────────────
# For Gmail: enable 2FA, then create an App Password at
# https://myaccount.google.com/apppasswords
# The app password is a 16-char code like "abcd efgh ijkl mnop"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_FROM = "stuhwilson@gmail.com"
EMAIL_PASSWORD = ""  # ← PUT YOUR GMAIL APP PASSWORD HERE (see instructions below)
EMAIL_TO = "stuhwilson@gmail.com"

# HOW TO GET AN APP PASSWORD (takes 2 minutes):
# 1. Go to https://myaccount.google.com/apppasswords
# 2. You may need to enable 2-Step Verification first at
#    https://myaccount.google.com/signinonptions/two-step-verification
# 3. Create a new app password (name it anything, e.g. "yosemite monitor")
# 4. Copy the 16-character code and paste it above as EMAIL_PASSWORD
# 5. Your regular Gmail password will NOT work here — Google blocks it

# ─────────────────────────────────────────────────────────────
# NTFY CONFIG — subscribe at https://ntfy.sh/yosemite-camp-stuhw
# ─────────────────────────────────────────────────────────────
# Install the ntfy app on your phone (iOS/Android) and subscribe
# to the topic below to get push notifications.
NTFY_TOPIC = "yosemite-camp-stuhw"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

# ─────────────────────────────────────────────────────────────
# CAMPGROUND CONFIG
# ─────────────────────────────────────────────────────────────

# All Yosemite campgrounds — comment out any you don't want to monitor
CAMPGROUNDS = {
    # ── Yosemite Valley ──
    "232447": "Upper Pines",
    #"232450": "Lower Pines",
    #"232449": "North Pines",
    #"10004152": "Camp 4",
    # ── Along Tioga Road (seasonal, typically Jun–Oct) ──
    #"232448": "Tuolumne Meadows",
    #"10083831": "Porcupine Flat",
    #"10083840": "Yosemite Creek",
    #"10083845": "Tamarack Flat",
    #"10083567": "White Wolf",
    #"232452": "Crane Flat",
    # ── Wawona / Glacier Point Road ──
    #"232446": "Wawona",
    #"232453": "Bridalveil Creek",
    # ── Big Oak Flat Road ──
    #"232451": "Hodgdon Meadow",
    # ── Horse camps ──
    # "10220609": "Wawona Horse Campsites",
    # "10346420": "Tuolumne Horse Campsites",
}

# Check-in Friday March 20, check-out Sunday March 22
# → need nights of March 20 AND March 21 to be "Available"
CHECK_IN = "2026-03-20"
CHECK_OUT = "2026-03-22"

# How often to check (seconds). 5 minutes = 300
POLL_INTERVAL = 30

# ─────────────────────────────────────────────────────────────
# Implementation
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("yosemite")

# Track sites we've already notified about so we don't spam
notified_sites: set[str] = set()


def check_campground(facility_id: str) -> list[dict]:
    """Check a single campground — delegates to engine with current dates."""
    return _engine_check(facility_id, CHECK_IN, CHECK_OUT)


def send_ntfy(title: str, body: str, click_url: str = "") -> bool:
    """Send ntfy push — checks ENABLE_NTFY flag before delegating."""
    if not ENABLE_NTFY or not NTFY_TOPIC:
        return False
    return _engine_send_ntfy(NTFY_TOPIC, title, body, click_url)


def send_email(subject: str, body: str) -> bool:
    """Send email — checks ENABLE_EMAIL flag before delegating."""
    if not ENABLE_EMAIL:
        return False
    if not all([EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO]):
        log.warning("Email not configured — skipping email notification")
        return False
    smtp_config = {
        "server": SMTP_SERVER, "port": SMTP_PORT,
        "from_addr": EMAIL_FROM, "password": EMAIL_PASSWORD,
    }
    return _engine_send_email(EMAIL_TO, subject, body, smtp_config)


def alert(campground_name: str, sites: list[dict]):
    """Send notification about available sites."""
    facility_id = [k for k, v in CAMPGROUNDS.items() if v == campground_name][0]
    book_url = f"https://www.recreation.gov/camping/campgrounds/{facility_id}"

    lines = [
        f"CAMPSITE AVAILABLE - {campground_name}",
        f"Dates: {CHECK_IN} to {CHECK_OUT}",
        "",
    ]
    for s in sites:
        lines.append(
            f"  Site {s['site']} ({s['loop']}) - {s['type']}, up to {s['max_people']} people"
        )
    lines.append("")
    lines.append(f"Book NOW: {book_url}")
    lines.append("")
    lines.append("These go fast - book immediately!")

    body = "\n".join(lines)
    subject = f"CAMPSITE ALERT: {campground_name} - {len(sites)} site(s) available {CHECK_IN} to {CHECK_OUT}"

    # Try ntfy push notification
    send_ntfy(subject, body, click_url=book_url)

    # Try email
    send_email(subject, body)

    # Always log to console
    log.info("=" * 60)
    log.info(subject)
    for s in sites:
        log.info("  Site %s (%s)", s["site"], s["loop"])
    log.info("=" * 60)

    # Windows beep to wake you up if you're nearby
    try:
        for _ in range(5):
            winsound.Beep(1000, 500)
            time.sleep(0.2)
    except Exception:
        pass


def run_check():
    """Check all campgrounds once."""
    log.info("Checking availability for %s to %s ...", CHECK_IN, CHECK_OUT)

    for facility_id, name in CAMPGROUNDS.items():
        try:
            sites = check_campground(facility_id)
            if sites:
                # Filter out already-notified sites
                new_sites = [
                    s for s in sites
                    if f"{facility_id}:{s['site_id']}" not in notified_sites
                ]
                if new_sites:
                    alert(name, new_sites)
                    for s in new_sites:
                        notified_sites.add(f"{facility_id}:{s['site_id']}")
                else:
                    log.info(
                        "  %s: %d site(s) still available (already notified)",
                        name,
                        len(sites),
                    )
            else:
                log.info("  %s: no availability", name)
        except requests.exceptions.HTTPError as e:
            log.error("  %s: HTTP error %s", name, e)
        except Exception as e:
            log.error("  %s: error — %s", name, e)

        # Be polite — short pause between campground requests
        time.sleep(2)


def parse_args():
    """Parse command-line arguments. Anything not specified uses the file defaults."""
    parser = argparse.ArgumentParser(
        description="Yosemite Campsite Availability Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python monitor.py
  python monitor.py --check-in 2026-04-10 --check-out 2026-04-12
  python monitor.py --check-in 2026-07-04 --check-out 2026-07-06 --interval 2
  python monitor.py --no-email --ntfy
""",
    )
    parser.add_argument(
        "--check-in", metavar="YYYY-MM-DD",
        help=f"check-in date (default: {CHECK_IN})",
    )
    parser.add_argument(
        "--check-out", metavar="YYYY-MM-DD",
        help=f"check-out date (default: {CHECK_OUT})",
    )
    parser.add_argument(
        "--interval", type=int, metavar="MIN",
        help=f"polling interval in minutes (default: {POLL_INTERVAL // 60})",
    )
    parser.add_argument(
        "--email", action=argparse.BooleanOptionalAction, default=None,
        help="enable/disable email notifications (default: use file setting)",
    )
    parser.add_argument(
        "--ntfy", action=argparse.BooleanOptionalAction, default=None,
        help="enable/disable ntfy push notifications (default: use file setting)",
    )
    return parser.parse_args()


def main():
    global CHECK_IN, CHECK_OUT, POLL_INTERVAL, ENABLE_EMAIL, ENABLE_NTFY

    args = parse_args()

    # Apply CLI overrides
    if args.check_in:
        CHECK_IN = args.check_in
    if args.check_out:
        CHECK_OUT = args.check_out
    if args.interval is not None:
        POLL_INTERVAL = args.interval * 60
    if args.email is not None:
        ENABLE_EMAIL = args.email
    if args.ntfy is not None:
        ENABLE_NTFY = args.ntfy

    cg_list = ", ".join(CAMPGROUNDS.values())
    notify_channels = []
    if ENABLE_EMAIL:
        notify_channels.append(f"Email ({EMAIL_TO})")
    if ENABLE_NTFY:
        notify_channels.append(f"ntfy ({NTFY_TOPIC})")
    if not notify_channels:
        notify_channels.append("Console + sound only")

    print("""
  ==================================================
    Yosemite Campsite Monitor
    Checking: {campgrounds}
    Dates:    {check_in} -> {check_out}
    Interval: every {interval} minutes
    Notify:   {notify}
  ==================================================
""".format(
        campgrounds=cg_list,
        check_in=CHECK_IN,
        check_out=CHECK_OUT,
        interval=POLL_INTERVAL // 60,
        notify=" + ".join(notify_channels),
    ))

    if ENABLE_EMAIL and not all([EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO]):
        log.warning(
            "EMAIL ENABLED but not fully configured - edit the EMAIL_* variables "
            "at the top of this script, or set ENABLE_EMAIL = False."
        )
        print()

    # Run immediately, then on interval
    while True:
        try:
            run_check()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error("Unexpected error: %s", e)

        log.info(
            "Next check in %d minutes (Ctrl+C to stop)\n",
            POLL_INTERVAL // 60,
        )
        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)


if __name__ == "__main__":
    main()
