"""
Интеграция курсов валют ЦБ РФ
Получение данных, сохранение в БД, расчёт изменений, уведомления
"""

import os
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("sync.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
THRESHOLD_PERCENT = Decimal(os.getenv("ALERT_THRESHOLD", "2.0"))

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "dbname": os.getenv("DB_NAME", "currency_db"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "port": os.getenv("DB_PORT", "5432"),
}


def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def fetch_cbr_data(request_date: date) -> bytes:
    """Получить XML от ЦБ РФ за указанную дату (формат DD/MM/YYYY)"""
    params = {"date_req": request_date.strftime("%d/%m/%Y")}
    response = requests.get(CBR_URL, params=params, timeout=10)
    response.raise_for_status()
    return response.content


def parse_xml(xml_data: bytes) -> list[dict]:
    """
    Парсинг XML ответа ЦБ РФ.
    ЦБ отдаёт кодировку windows-1251, числа через запятую.
    """
    root = ET.fromstring(xml_data)
    rate_date_str = root.get("Date")  # формат: DD.MM.YYYY
    rate_date = datetime.strptime(rate_date_str, "%d.%m.%Y").date()

    currencies = []
    for valute in root.findall("Valute"):
        try:
            char_code = valute.find("CharCode").text
            nominal = int(valute.find("Nominal").text)
            raw_value = valute.find("Value").text.replace(",", ".")
            value = Decimal(raw_value) / nominal

            currencies.append({
                "currency_code": char_code,
                "rate": value,
                "nominal": nominal,
                "rate_date": rate_date,
            })
        except (AttributeError, InvalidOperation, ValueError) as e:
            logger.error(f"Ошибка парсинга валюты: {e}")
            log_error("PARSE_ERROR", str(e))

    return currencies


def get_previous_rate(conn, currency_code: str, current_date: date):
    """Previous_rate нет в API ЦБ — берём из своей БД (последняя запись до текущей даты)"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rate FROM currency_rates
            WHERE currency_code = %s AND rate_date < %s
            ORDER BY rate_date DESC LIMIT 1
            """,
            (currency_code, current_date)
        )
        row = cur.fetchone()
        return row[0] if row else None


def upsert_rate(conn, record: dict):
    """UPSERT с расчётом change_pct"""
    previous_rate = get_previous_rate(conn, record["currency_code"], record["rate_date"])
    change_pct = None
    if previous_rate:
        change_pct = ((record["rate"] - previous_rate) / previous_rate) * 100

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO currency_rates
                (currency_code, rate, previous_rate, change_pct, rate_date, loaded_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (currency_code, rate_date)
            DO UPDATE SET
                rate = EXCLUDED.rate,
                previous_rate = EXCLUDED.previous_rate,
                change_pct = EXCLUDED.change_pct,
                loaded_at = NOW()
            """,
            (record["currency_code"], record["rate"], previous_rate,
             change_pct, record["rate_date"])
        )
    conn.commit()
    return change_pct


def send_notification(currency_code: str, change_pct: Decimal, channel="telegram"):
    """Заглушка уведомления. Для реального Telegram нужен python-telegram-bot + BOT_TOKEN."""
    message = f"⚠️ {currency_code}: изменение курса {change_pct:.2f}%"
    logger.warning(f"ALERT: {message}")
    # Реальная отправка (пример для Telegram):
    # bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    # chat_id = os.getenv("TELEGRAM_CHAT_ID")
    # requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
    #               data={"chat_id": chat_id, "text": message})
    return {"status": "sent", "channel": channel}


def log_sync_start(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_logs (started_at, status) VALUES (NOW(), 'STARTED') RETURNING id"
        )
        sync_id = cur.fetchone()[0]
    conn.commit()
    return sync_id


def log_sync_finish(conn, sync_id, status, records_count, error_message=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE sync_logs
            SET finished_at = NOW(), status = %s, records_count = %s, error_message = %s
            WHERE id = %s
            """,
            (status, records_count, error_message, sync_id)
        )
    conn.commit()


def log_error(error_type: str, details: str):
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO error_logs (error_type, details) VALUES (%s, %s)",
                (error_type, details)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Не удалось записать error_log: {e}")


def run_sync(request_date: date = None):
    request_date = request_date or date.today()
    conn = get_connection()
    sync_id = log_sync_start(conn)

    try:
        logger.info(f"Запуск синхронизации на {request_date}")
        xml_data = fetch_cbr_data(request_date)
        records = parse_xml(xml_data)

        for record in records:
            change_pct = upsert_rate(conn, record)

            if change_pct is not None and abs(change_pct) > THRESHOLD_PERCENT:
                send_notification(record["currency_code"], change_pct)

        log_sync_finish(conn, sync_id, "SUCCESS", len(records))
        logger.info(f"Синхронизация завершена успешно. Записей: {len(records)}")

    except requests.RequestException as e:
        logger.error(f"Ошибка сети: {e}")
        log_error("NETWORK_ERROR", str(e))
        log_sync_finish(conn, sync_id, "FAILED", 0, str(e))

    except Exception as e:
        logger.error(f"Непредвиденная ошибка: {e}")
        log_error("UNKNOWN_ERROR", str(e))
        log_sync_finish(conn, sync_id, "FAILED", 0, str(e))

    finally:
        conn.close()


if __name__ == "__main__":
    run_sync()
