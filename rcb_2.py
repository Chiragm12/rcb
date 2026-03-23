"""
RCB Ticket Monitor — Enhanced Edition
======================================
Watches shop.royalchallengers.com for ticket releases.

Detection methods:
  1. Nav bar change  — a new button appears beside "Merchandise"
  2. Shop dropdown   — clicking SHOP reveals a match/ticket option
  3. DOM scan        — buttons/links with ticket keywords anywhere on page
  4. Network monitor — ticket/event API calls intercepted
  5. Screenshot diff — pixel-level change in the nav area (catches image banners)

Notification channels (configure in Config below):
  - Desktop popup  (plyer — always on)
  - Audible beep
  - Telegram       (free, instant — RECOMMENDED)
  - WhatsApp       (via Twilio — paid but easy)
  - SMS            (via Twilio — paid)
  - Email          (via Gmail SMTP — free)

Setup
-----
  pip install playwright plyer requests twilio
  playwright install chromium

Telegram (free & easiest):
  1. Message @BotFather → /newbot → copy token
  2. Message your bot once, then visit:
     https://api.telegram.org/bot<TOKEN>/getUpdates
     Copy the "id" from result.message.chat.id

Usage:
  python rcb_monitor.py                          # default settings
  python rcb_monitor.py --headless               # no browser window
  python rcb_monitor.py --interval 3000          # scan every 3 s
  python rcb_monitor.py --click-shop             # also click SHOP nav item
"""

import time
import logging
import argparse
import smtplib
import hashlib
import base64
import re
from io import BytesIO
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from playwright.sync_api import sync_playwright, Page, Response, TimeoutError as PWTimeout

# ── Optional deps ─────────────────────────────────────────────────────────────
try:
    from plyer import notification as plyer_notification
    PLYER_AVAILABLE = True
except ImportError:
    PLYER_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False


# ═════════════════════════════════════════════════════════════════════════════
# ██  CONFIGURE YOUR NOTIFICATIONS HERE
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class NotificationConfig:
    # ── Telegram (RECOMMENDED — free & instant) ───────────────────────────────
    # Get token: message @BotFather on Telegram → /newbot
    # Get chat_id: send any message to your bot, then open
    #   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
    telegram_token: str   = "8090470907:AAG7mSCqqVJGvsLOBxIJ5EV25NGpawFEaw0"          # e.g. "7123456789:AAF..."
    telegram_chat_id: str = "6040363635"          # e.g. "123456789"

    # ── WhatsApp via Twilio (optional, paid) ──────────────────────────────────
    twilio_account_sid: str = ""        # from console.twilio.com
    twilio_auth_token: str  = ""
    twilio_from_whatsapp: str = ""      # e.g. "whatsapp:+14155238886"
    twilio_to_whatsapp: str   = ""      # e.g. "whatsapp:+919876543210"

    # ── SMS via Twilio (optional, paid) ───────────────────────────────────────
    twilio_from_sms: str = ""           # your Twilio number e.g. "+12025551234"
    twilio_to_sms: str   = ""           # your number e.g. "+919876543210"

    # ── Email via Gmail SMTP (optional, free) ─────────────────────────────────
    # Use an App Password (not your real password):
    #   Google Account → Security → 2-Step → App passwords
    gmail_sender: str   = ""            # e.g. "yourname@gmail.com"
    gmail_password: str = ""            # 16-char app password
    gmail_to: str       = ""            # where to send alert


# ═════════════════════════════════════════════════════════════════════════════
# Configuration
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Config:
    url: str = "https://shop.royalchallengers.com/merchandise"
    shop_url: str = "https://shop.royalchallengers.com"

    notifications: NotificationConfig = field(default_factory=NotificationConfig)

    # ── Network: alert on responses from these domains ────────────────────────
    watch_domains: list[str] = field(default_factory=lambda: [
        "ticketgenie", "rcbscaleapi", "ticketmaster",
        "bookmyshow", "paytminsider", "insider.in",
        "district.in",                 # common IPL ticketing platforms
    ])

    api_keywords: list[str] = field(default_factory=lambda: [
        "ticket", "event", "match", "seat", "inventory", "book",
    ])

    # ── DOM: high-confidence ticket keywords ──────────────────────────────────
    ticket_keywords: list[str] = field(default_factory=lambda: [
        "ticket", "tkt", "book now", "buy now", "get now", "grab now",
        "book match", "buy match", "register now", "reserve now",
        "entry pass", "match pass", "game pass", "match day",
        "shop now",
        # Match patterns like "RCB vs MI", "RCB vs CSK" etc.
        " vs ",
    ])

    loose_keywords: list[str] = field(default_factory=lambda: [
        "ipl 2026", "stadium", "available now", "get yours", "home game",
    ])

    # ── hrefs to always skip ──────────────────────────────────────────────────
    href_ignore: list[str] = field(default_factory=lambda: [
        "apple.com", "play.google.com", "facebook.com", "instagram.com",
        "twitter.com", "x.com", "youtube.com", "linkedin.com",
        "whatsapp.com", "privacy", "terms", "faq", "contact", "about",
        "mailto:", "tel:", "/merchandise", "/news", "/team", "/fixtures",
        "/rcb-tv", "/echo-of-fans", "/rcb-bar",
    ])

    # ── Nav area to screenshot for visual diff ────────────────────────────────
    # Clip = {x, y, width, height} of the nav/button bar area
    nav_clip: dict = field(default_factory=lambda: {
        "x": 0, "y": 250, "width": 1280, "height": 80
    })

    # Pixel-change threshold to trigger visual alert (0–1, fraction of pixels)
    visual_diff_threshold: float = 0.02   # 2% of pixels changed

    # ── Timing ────────────────────────────────────────────────────────────────
    render_wait_ms: int    = 3_000
    poll_interval_ms: int  = 5_000

    # Re-alert every N cycles when tickets are found (so you don't miss it)
    alert_repeat_cycles: int = 3

    max_consecutive_errors: int = 10

    headless: bool = False
    beep: bool     = True
    click_shop: bool = False      # also click SHOP nav to check dropdown


# ═════════════════════════════════════════════════════════════════════════════
# Logging
# ═════════════════════════════════════════════════════════════════════════════

def setup_logging(verbose: bool = False) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("rcb_monitor")

log = setup_logging()


# ═════════════════════════════════════════════════════════════════════════════
# Notification dispatcher
# ═════════════════════════════════════════════════════════════════════════════

class Notifier:
    def __init__(self, cfg: Config) -> None:
        self.cfg  = cfg
        self.ncfg = cfg.notifications

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _telegram(self, title: str, message: str) -> None:
        nc = self.ncfg
        if not (nc.telegram_token and nc.telegram_chat_id):
            return
        if not REQUESTS_AVAILABLE:
            log.warning("Telegram: 'requests' not installed.")
            return
        text = f"🚨 *{title}*\n\n{message}\n\n🔗 {self.cfg.url}"
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{nc.telegram_token}/sendMessage",
                json={"chat_id": nc.telegram_chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            if resp.ok:
                log.info("✅ Telegram notification sent.")
            else:
                log.warning("Telegram error: %s", resp.text)
        except Exception as exc:
            log.warning("Telegram failed: %s", exc)

    def _whatsapp(self, title: str, message: str) -> None:
        nc = self.ncfg
        if not (nc.twilio_account_sid and nc.twilio_from_whatsapp and nc.twilio_to_whatsapp):
            return
        if not TWILIO_AVAILABLE:
            log.warning("WhatsApp: 'twilio' not installed.")
            return
        try:
            client = TwilioClient(nc.twilio_account_sid, nc.twilio_auth_token)
            body = f"🚨 {title}\n{message}\n{self.cfg.url}"
            client.messages.create(body=body, from_=nc.twilio_from_whatsapp, to=nc.twilio_to_whatsapp)
            log.info("✅ WhatsApp notification sent.")
        except Exception as exc:
            log.warning("WhatsApp failed: %s", exc)

    def _sms(self, title: str, message: str) -> None:
        nc = self.ncfg
        if not (nc.twilio_account_sid and nc.twilio_from_sms and nc.twilio_to_sms):
            return
        if not TWILIO_AVAILABLE:
            log.warning("SMS: 'twilio' not installed.")
            return
        try:
            client = TwilioClient(nc.twilio_account_sid, nc.twilio_auth_token)
            body = f"RCB TICKETS! {message} {self.cfg.url}"
            client.messages.create(body=body, from_=nc.twilio_from_sms, to=nc.twilio_to_sms)
            log.info("✅ SMS notification sent.")
        except Exception as exc:
            log.warning("SMS failed: %s", exc)

    def _email(self, title: str, message: str) -> None:
        nc = self.ncfg
        if not (nc.gmail_sender and nc.gmail_password and nc.gmail_to):
            return
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"🏏 {title}"
            msg["From"]    = nc.gmail_sender
            msg["To"]      = nc.gmail_to
            html = f"""
            <h2 style="color:red">🚨 {title}</h2>
            <p>{message}</p>
            <p><a href="{self.cfg.url}" style="background:red;color:white;padding:10px 20px;
               text-decoration:none;border-radius:5px">🎟️ Open RCB Shop NOW</a></p>
            <hr><small>RCB Ticket Monitor • {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small>
            """
            msg.attach(MIMEText(html, "html"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
                smtp.login(nc.gmail_sender, nc.gmail_password)
                smtp.sendmail(nc.gmail_sender, nc.gmail_to, msg.as_string())
            log.info("✅ Email notification sent.")
        except Exception as exc:
            log.warning("Email failed: %s", exc)

    def _desktop(self, title: str, message: str) -> None:
        if PLYER_AVAILABLE:
            try:
                plyer_notification.notify(title=title, message=message, timeout=30)
            except Exception as exc:
                log.debug("Desktop notification failed: %s", exc)

    # ── Public ────────────────────────────────────────────────────────────────

    def send(self, title: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        log.warning("🚨 ALERT [%s] %s — %s", timestamp, title, message)
        self._desktop(title, message)
        self._telegram(title, message)
        self._whatsapp(title, message)
        self._sms(title, message)
        self._email(title, message)
        if self.cfg.beep:
            print("\a\a\a", end="", flush=True)


# ═════════════════════════════════════════════════════════════════════════════
# Network monitor
# ═════════════════════════════════════════════════════════════════════════════

class NetworkMonitor:
    def __init__(self, cfg: Config, notifier: Notifier) -> None:
        self.cfg      = cfg
        self.notifier = notifier
        self._seen: set[str] = set()

    def handle_response(self, response: Response) -> None:
        url = response.url.lower()
        if not any(d in url for d in self.cfg.watch_domains):
            return
        if url in self._seen:
            return
        self._seen.add(url)
        log.debug("Watched-domain response: %s [%s]", url, response.status)
        if any(kw in url for kw in self.cfg.api_keywords):
            self.notifier.send(
                "RCB Ticket API Detected 🎟️",
                f"Ticket-related API call intercepted!\n{response.url}",
            )
        else:
            log.info("Watched domain hit (no ticket keyword): %s", url)


# ═════════════════════════════════════════════════════════════════════════════
# Visual diff (screenshot-based)
# ═════════════════════════════════════════════════════════════════════════════

class VisualMonitor:
    """
    Takes a screenshot of the nav/button bar area and compares pixel hashes.
    Catches image banners / buttons that don't emit DOM text or network calls.
    """
    def __init__(self, cfg: Config) -> None:
        self.cfg        = cfg
        self._last_hash: Optional[str] = None
        self._last_img:  Optional[bytes] = None

    def _hash(self, data: bytes) -> str:
        return hashlib.md5(data).hexdigest()

    def check(self, page: Page) -> bool:
        """Returns True if the nav area changed significantly."""
        try:
            img_bytes = page.screenshot(clip=self.cfg.nav_clip)
        except Exception:
            return False

        h = self._hash(img_bytes)
        if self._last_hash is None:
            self._last_hash = h
            self._last_img  = img_bytes
            return False

        if h == self._last_hash:
            return False

        # Changed — do a pixel-level diff to filter tiny rendering noise
        changed = self._pixel_diff(self._last_img, img_bytes)
        if changed >= self.cfg.visual_diff_threshold:
            log.info("Visual change detected: %.1f%% pixels differ", changed * 100)
            self._last_hash = h
            self._last_img  = img_bytes
            return True

        self._last_hash = h
        self._last_img  = img_bytes
        return False

    @staticmethod
    def _pixel_diff(img_a: bytes, img_b: bytes) -> float:
        """Returns fraction of pixels that differ between two PNG screenshots."""
        try:
            from PIL import Image
            import numpy as np
            a = np.array(Image.open(BytesIO(img_a)).convert("RGB"))
            b = np.array(Image.open(BytesIO(img_b)).convert("RGB"))
            if a.shape != b.shape:
                return 1.0
            diff = np.any(np.abs(a.astype(int) - b.astype(int)) > 10, axis=2)
            return float(diff.mean())
        except ImportError:
            # Pillow/numpy not installed — just treat any hash change as a diff
            return 1.0


# ═════════════════════════════════════════════════════════════════════════════
# DOM scanner
# ═════════════════════════════════════════════════════════════════════════════

def _contains(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(kw in t for kw in keywords)

def _href_ignored(href: str, ignore_list: list[str]) -> bool:
    h = href.lower()
    return any(skip in h for skip in ignore_list)


def scan_for_ticket_button(page: Page, cfg: Config) -> Optional[str]:
    """
    Scans the full page DOM for ticket indicators.
    Returns a description string if found, else None.
    """
    strict = cfg.ticket_keywords
    loose  = cfg.loose_keywords

    # Page title
    try:
        title = page.title()
        if _contains(title, strict):
            return f"[page title] {title}"
    except Exception:
        pass

    # All interactive elements
    locator = page.locator("a, button, [role='button'], [role='link'], .nav-item, .menu-item")
    try:
        count = locator.count()
    except Exception:
        return None

    for i in range(count):
        try:
            el = locator.nth(i)
            if not el.is_visible():
                continue

            text = (el.inner_text(timeout=300) or "").strip()
            if text:
                if _contains(text, strict) or _contains(text, loose):
                    # Skip if it's clearly a nav item we know about
                    if text.lower() in ("merchandise", "shop", "more"):
                        continue
                    return f"[button/link text] '{text}'"

            label = el.get_attribute("aria-label") or ""
            if label and _contains(label, strict):
                return f"[aria-label] '{label}'"

            href = el.get_attribute("href") or ""
            if href and not _href_ignored(href, cfg.href_ignore):
                if _contains(href, strict):
                    return f"[link href] {href}"

        except Exception:
            continue

    # Also scan raw page text for match patterns like "RCB vs MI"
    try:
        body_text = page.locator("body").inner_text(timeout=2000)
        match = re.search(r"RCB\s+vs\s+\w+", body_text, re.IGNORECASE)
        if match:
            return f"[page text match] '{match.group()}'"
    except Exception:
        pass

    return None


def check_shop_dropdown(page: Page, cfg: Config) -> Optional[str]:
    """
    Clicks the SHOP nav item and scans the dropdown for ticket/match items.
    Closes the dropdown afterwards.
    """
    try:
        shop_btn = page.locator("text=SHOP").first
        if not shop_btn.is_visible():
            return None
        shop_btn.click()
        page.wait_for_timeout(1500)

        # Scan dropdown content
        result = scan_for_ticket_button(page, cfg)

        # Close dropdown by pressing Escape
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        return result
    except Exception as exc:
        log.debug("Shop dropdown check failed: %s", exc)
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Page helpers
# ═════════════════════════════════════════════════════════════════════════════

def load_page(page: Page, url: str, render_wait_ms: int) -> bool:
    try:
        page.goto(url, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(render_wait_ms)
        return True
    except PWTimeout:
        log.warning("Page load timed out.")
    except Exception as exc:
        log.warning("Page load error: %s", exc)
    return False


# ═════════════════════════════════════════════════════════════════════════════
# Main loop
# ═════════════════════════════════════════════════════════════════════════════

def run(cfg: Config) -> None:
    notifier      = Notifier(cfg)
    net_monitor   = NetworkMonitor(cfg, notifier)
    visual_monitor = VisualMonitor(cfg)

    consecutive_errors = 0
    check_count        = 0
    alert_count        = 0   # total alerts fired

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.headless)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.on("response", net_monitor.handle_response)

        log.info("=" * 60)
        log.info("  RCB Ticket Monitor — Enhanced Edition")
        log.info("  Target: %s", cfg.url)
        log.info("  Interval: %d ms | Render wait: %d ms", cfg.poll_interval_ms, cfg.render_wait_ms)
        log.info("  Visual diff: ON | Shop click: %s", "ON" if cfg.click_shop else "OFF")
        log.info("=" * 60)

        log.info("Loading page...")
        if not load_page(page, cfg.url, cfg.render_wait_ms):
            log.error("Could not load page. Check your internet connection.")
            browser.close()
            return

        # Seed visual baseline
        visual_monitor.check(page)
        log.info("Baseline captured. Monitoring started. Press Ctrl+C to stop.\n")

        try:
            while True:
                check_count += 1
                now = datetime.now().strftime("%H:%M:%S")

                try:
                    found_reason = None

                    # 1. DOM scan — main page
                    found_reason = scan_for_ticket_button(page, cfg)
                    if found_reason:
                        log.info("[%s] DOM hit: %s", now, found_reason)

                    # 2. SHOP dropdown (optional)
                    if not found_reason and cfg.click_shop:
                        found_reason = check_shop_dropdown(page, cfg)
                        if found_reason:
                            log.info("[%s] Shop dropdown hit: %s", now, found_reason)

                    # 3. Visual diff — nav bar area
                    if not found_reason:
                        if visual_monitor.check(page):
                            found_reason = "[visual] Nav bar area changed — possible new button!"

                    # ── Alert ──────────────────────────────────────────────
                    if found_reason:
                        alert_count += 1
                        notifier.send(
                            "🏏 RCB TICKETS ARE OUT!",
                            f"Detected: {found_reason}\nScan #{check_count} at {now}",
                        )
                        log.warning(">>> OPEN YOUR BROWSER NOW: %s <<<", cfg.url)
                        # Repeat alerts so you don't miss it
                        time.sleep(cfg.poll_interval_ms * cfg.alert_repeat_cycles / 1000)
                    else:
                        log.info("[%s] Scan #%d — no tickets yet.", now, check_count)

                    consecutive_errors = 0

                except PWTimeout:
                    consecutive_errors += 1
                    log.warning("Scan timed out (%d). Reloading...", consecutive_errors)
                    load_page(page, cfg.url, cfg.render_wait_ms)
                    visual_monitor.check(page)  # reset baseline after reload

                except Exception as exc:
                    consecutive_errors += 1
                    log.error("Unexpected error: %s", exc)

                finally:
                    if cfg.max_consecutive_errors and consecutive_errors >= cfg.max_consecutive_errors:
                        log.critical("Too many consecutive errors (%d). Stopping.", consecutive_errors)
                        break

                page.wait_for_timeout(cfg.poll_interval_ms)

        except KeyboardInterrupt:
            log.info("\nStopped by user after %d scans, %d alerts.", check_count, alert_count)
        finally:
            browser.close()


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="RCB Ticket Availability Monitor — Enhanced")
    parser.add_argument("--url", default=Config.url)
    parser.add_argument("--interval", type=int, default=5000, metavar="MS",
                        help="Poll interval in milliseconds (default: 5000)")
    parser.add_argument("--render-wait", type=int, default=3000, metavar="MS",
                        help="Extra wait after page load for JS to render (default: 3000)")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser without a visible window")
    parser.add_argument("--no-beep", dest="beep", action="store_false")
    parser.add_argument("--click-shop", action="store_true",
                        help="Also click the SHOP nav item to check its dropdown")
    parser.add_argument("--max-errors", type=int, default=10)
    parser.add_argument("--verbose", action="store_true")

    # Notification shortcuts
    parser.add_argument("--telegram-token",   default="", metavar="TOKEN")
    parser.add_argument("--telegram-chat-id", default="", metavar="CHAT_ID")

    args = parser.parse_args()
    if args.verbose:
        setup_logging(verbose=True)

    cfg = Config()
    cfg.url              = args.url
    cfg.poll_interval_ms = args.interval
    cfg.render_wait_ms   = args.render_wait
    cfg.headless         = args.headless
    cfg.beep             = args.beep
    cfg.click_shop       = args.click_shop
    cfg.max_consecutive_errors = args.max_errors

    if args.telegram_token:
        cfg.notifications.telegram_token   = args.telegram_token
        cfg.notifications.telegram_chat_id = args.telegram_chat_id

    return cfg


if __name__ == "__main__":
    cfg = parse_args()
    run(cfg)