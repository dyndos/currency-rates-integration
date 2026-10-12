"""
CBR FX Rates Integration
========================
Fetches official XML from cbr.ru, stores in PostgreSQL,
calculates change_pct vs previous business day,
sends alerts on threshold breach.

Author:  Evgeny Dynda
Version: 1.2
"""

import logging
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import psycopg2
import requests
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
THRESHOLD_PERCENT = Decimal(os.getenv("ALERT_THRESHOLD", "2.0"))

DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "dbname":   os.getenv("DB_NAME",     "currency_db"),
    "user":     os.getenv("DB_USER",     "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "port":     int(os.getenv("DB_PORT", "5432")),
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/sync.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_connection():
    """Open a new DB connection using .env config."""
    return psycopg2.connect(**DB_CONFIG)


def log_sync_start(conn) -> int:
    """Insert a new sync_logs row with status STARTED; return its id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_logs (started_at, status) "
            "VALUES (NOW(), 'STARTED') RETURNING id"
        )
        sync_id = cur.fetchone()[0]
    conn.commit()
    return sync_id


def log_sync_finish(conn, sync_id: int, status: str,
                    records_count: int, error_message: str = None):
    """Update sync_logs row when job finishes (success or failure)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sync_logs
               SET finished_at    = NOW(),
                   status         = %s,
                   records_count  = %s,
                   error_message  = %s
             WHERE id = %s
            """,
            (status, records_count, error_message, sync_id),
        )
    conn.commit()


def log_error(error_type: str, details: str):
    """Write to error_logs table (opens its own short-lived connection)."""
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO error_logs (error_type, details) VALUES (%s, %s)",
                (error_type, details),
            )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error(f"Could not write error_log: {exc}")


def log_alert(conn, record: dict, change_pct: Decimal, result: dict):
    """Write triggered alert to alert_history table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO alert_history
                (currency_code, rate_date, change_pct, threshold, channel, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                record["currency_code"],
                record["rate_date"],
                change_pct,
                THRESHOLD_PERCENT,
                result.get("channel", "telegram"),
                result.get("status", "sent"),
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# CBR API
# ---------------------------------------------------------------------------

def fetch_cbr_data(request_date: date) -> bytes:
    """
    GET XML from CBR for a given date.
    CBR expects DD/MM/YYYY in the date_req query param.
    """
    params = {"date_req": request_date.strftime("%d/%m/%Y")}
    response = requests.get(CBR_URL, params=params, timeout=10)
    response.raise_for_status()
    return response.content


def parse_xml(xml_data: bytes) -> tuple[date, list[dict]]:
    """
    Parse CBR XML response.

    Key points:
    - rate_date is taken from the XML attribute ValCurs.Date,
      NOT from date.today(). On weekends / holidays CBR returns
      the most recent business-day date, so we must use that value.
    - Numbers use a comma as the decimal separator → replace before Decimal().
    - Nominal can be > 1 (e.g. JPY is quoted per 100 units) →
      divide value by nominal to get the per-unit rate.
    - CBR XML encoding is windows-1251; ElementTree handles it automatically
      when the <?xml ... encoding="windows-1251"?> declaration is present.

    Returns (actual_rate_date, list_of_rate_dicts).
    """
    root = ET.fromstring(xml_data)

    # Actual business date from CBR (may differ from the requested date)
    rate_date = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()

    records = []
    for valute in root.findall("Valute"):
        try:
            char_code = valute.find("CharCode").text
            nominal   = int(valute.find("Nominal").text)
            raw_value = valute.find("Value").text.replace(",", ".")
            rate      = Decimal(raw_value) / nominal   # per-unit rate

            records.append({
                "currency_code": char_code,
                "rate":          rate,
                "nominal":       nominal,
                "rate_date":     rate_date,
            })
        except (AttributeError, InvalidOperation, ValueError) as exc:
            logger.error(f"Parse error for valute element: {exc}")
            log_error("PARSE_ERROR", str(exc))

    return rate_date, records


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

def get_previous_rate(conn, currency_code: str, current_date: date):
    """
    Return the most recent rate stored in the DB that is BEFORE current_date.
    Using the DB (not the API) automatically handles weekends and holidays:
    SELECT ... ORDER BY rate_date DESC LIMIT 1 always returns the last
    real business day regardless of calendar gaps.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rate FROM currency_rates
             WHERE currency_code = %s
               AND rate_date < %s
             ORDER BY rate_date DESC
             LIMIT 1
            """,
            (currency_code, current_date),
        )
        row = cur.fetchone()
    return row[0] if row else None


def upsert_rate(conn, record: dict) -> Decimal | None:
    """
    UPSERT one rate row.
    - previous_rate: fetched from DB (last business day before rate_date).
    - change_pct:    computed here and stored alongside the rate.
    Returns change_pct (or None if no previous row exists yet).
    """
    previous_rate = get_previous_rate(
        conn, record["currency_code"], record["rate_date"]
    )

    change_pct = None
    if previous_rate is not None and previous_rate != 0:
        change_pct = (
            (record["rate"] - previous_rate) / previous_rate * 100
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO currency_rates
                (currency_code, rate, previous_rate, change_pct, rate_date, loaded_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (currency_code, rate_date)
            DO UPDATE SET
                rate          = EXCLUDED.rate,
                previous_rate = EXCLUDED.previous_rate,
                change_pct    = EXCLUDED.change_pct,
                loaded_at     = NOW()
            """,
            (
                record["currency_code"],
                record["rate"],
                previous_rate,
                change_pct,
                record["rate_date"],
            ),
        )
    conn.commit()
    return change_pct


def send_notification(currency_code: str,
                      change_pct: Decimal,
                      channel: str = "telegram") -> dict:
    """
    Send an alert when |change_pct| > THRESHOLD_PERCENT.

    Currently logs the alert; replace the commented block with a real
    Telegram Bot API call once BOT_TOKEN and CHAT_ID are configured.
    """
    direction = "📈" if change_pct > 0 else "📉"
    message = (
        f"{direction} ALERT | {currency_code}: {change_pct:+.2f}% "
        f"(threshold ±{THRESHOLD_PERCENT}%)"
    )
    logger.warning(f"ALERT: {message}")

    # ── Telegram (uncomment and set env vars to activate) ──────────────
    # bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    # chat_id   = os.getenv("TELEGRAM_CHAT_ID")
    # if bot_token and chat_id:
    #     requests.post(
    #         f"https://api.telegram.org/bot{bot_token}/sendMessage",
    #         data={"chat_id": chat_id, "text": message},
    #         timeout=5,
    #     )
    # ────────────────────────────────────────────────────────────────────

    return {"status": "sent", "channel": channel}


# ---------------------------------------------------------------------------
# Main sync job
# ---------------------------------------------------------------------------

def run_sync(request_date: date = None):
    """
    End-to-end sync job:
      1. Fetch XML from CBR for request_date (defaults to today).
      2. Parse — actual rate_date may differ (weekend / holiday).
      3. UPSERT each rate; compute change_pct vs previous business day.
      4. Fire alerts for currencies that exceed THRESHOLD_PERCENT.
      5. Write sync result to sync_logs.
    """
    request_date = request_date or date.today()
    conn = get_connection()
    sync_id = log_sync_start(conn)

    try:
        logger.info(f"Sync started (request date: {request_date})")

        xml_data = fetch_cbr_data(request_date)
        actual_date, records = parse_xml(xml_data)

        # Inform when CBR returns a different date (weekend / public holiday)
        if actual_date != request_date:
            logger.info(
                f"CBR returned data for {actual_date} "
                f"(requested {request_date} — weekend or holiday)"
            )

        alerts_fired = 0
        for record in records:
            change_pct = upsert_rate(conn, record)

            if change_pct is not None and abs(change_pct) > THRESHOLD_PERCENT:
                result = send_notification(
                    record["currency_code"], change_pct
                )
                log_alert(conn, record, change_pct, result)
                alerts_fired += 1

        log_sync_finish(conn, sync_id, "SUCCESS", len(records))
        logger.info(
            f"Sync finished. "
            f"Rate date: {actual_date} | "
            f"Records: {len(records)} | "
            f"Alerts: {alerts_fired}"
        )

    except requests.RequestException as exc:
        logger.error(f"Network error: {exc}")
        log_error("NETWORK_ERROR", str(exc))
        log_sync_finish(conn, sync_id, "FAILED", 0, str(exc))

    except Exception as exc:
        logger.error(f"Unexpected error: {exc}")
        log_error("UNKNOWN_ERROR", str(exc))
        log_sync_finish(conn, sync_id, "FAILED", 0, str(exc))

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run_sync()
