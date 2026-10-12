# setup_db.py
import psycopg2
from main import DB_CONFIG

SCHEMA = """
CREATE TABLE IF NOT EXISTS currency_rates (
    id SERIAL PRIMARY KEY,
    currency_code VARCHAR(3) NOT NULL,
    rate DECIMAL(10,4) NOT NULL,
    previous_rate DECIMAL(10,4),
    change_pct DECIMAL(5,2),
    rate_date DATE NOT NULL,
    loaded_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (currency_code, rate_date)
);

CREATE TABLE IF NOT EXISTS sync_logs (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    status VARCHAR(20),
    records_count INTEGER,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS error_logs (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP DEFAULT NOW(),
    error_type VARCHAR(50),
    details TEXT,
    resolved BOOLEAN DEFAULT FALSE
);
"""

if __name__ == "__main__":
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()
    conn.close()
    print("✅ Таблицы созданы")
