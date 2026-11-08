"""Приёмник вебхуков TradingView и исполнитель спотовых ордеров.

Режимы:
  paper   — локальная симуляция, ключи биржи не нужны;
  testnet — реальные вызовы ccxt в sandbox (по умолчанию Binance Spot Testnet);
  live    — боевой счёт. Нужны TRADING_MODE=live и CONFIRM_LIVE=YES_I_UNDERSTAND.

Покупка тратит POSITION_SIZE_PCT свободного депозита. Продажа закрывает только
ту позицию, которую открыл сам бот, и не трогает остальные монеты на счёте.
Шорты и бессрочные контракты не поддерживаются.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parent
PLACEHOLDER_SECRET = "change-me-please-123"
LIVE_CONFIRMATION = "YES_I_UNDERSTAND"
QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USD", "EUR", "BTC", "ETH")
QTY_STEP = Decimal("0.00000001")

log = logging.getLogger("bot")


class RiskError(Exception):
    """Сигнал понят, но ордер сознательно не отправляется."""


class ExchangeFailure(Exception):
    """Биржа не ответила или отвергла запрос на транспортном уровне."""


class Attempt:
    def __init__(self) -> None:
        self.sent = False


@dataclass(frozen=True)
class Config:
    secret: str
    mode: str
    confirm_live: str
    exchange_id: str
    api_key: str
    api_secret: str
    position_size_pct: Decimal
    max_deviation_pct: Decimal
    paper_quote: str
    paper_balance: Decimal
    host: str
    port: int
    db_path: Path
    log_path: Path


@dataclass(frozen=True)
class Command:
    action: str
    symbol: str
    price: Decimal | None
    signal_time: str | None
    alert_id: str | None


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    base: str
    amount: Decimal
    price: Decimal


def dec(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RiskError("некорректное число в расчёте ордера") from exc
    if not number.is_finite():
        raise RiskError("некорректное число в расчёте ордера")
    return number


def fmt_num(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def money(value: Decimal) -> str:
    return format(value, "f")


def secret_matches(provided: str, expected: str) -> bool:
    left = hashlib.sha256(provided.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def split_symbol(symbol: str) -> tuple[str, str]:
    base, quote = symbol.split("/", 1)
    return base, quote


def to_ccxt_symbol(ticker: object) -> str:
    if not isinstance(ticker, str):
        raise ValueError("ticker должен быть строкой")
    upper = ticker.strip().upper()
    if not upper or "{{" in upper or len(upper) > 64:
        raise ValueError("ticker пустой или шаблон TradingView не подставился")
    if ":" in upper:
        upper = upper.split(":", 1)[1]
    if upper.endswith(".P") or upper.endswith("PERP"):
        raise ValueError("бессрочные контракты в этом боте не торгуются, нужен спотовый тикер")
    if "/" in upper:
        base, quote = upper.split("/", 1)
        if not base or not quote or ":" in quote:
            raise ValueError("не удалось разобрать ticker")
        return f"{base}/{quote}"
    for quote in QUOTES:
        if upper.endswith(quote) and len(upper) > len(quote):
            return f"{upper[:-len(quote)]}/{quote}"
    raise ValueError("не удалось разобрать ticker, используйте BTCUSDT или BTC/USDT")


def parse_price(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and "{{" in value:
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("price должен быть числом") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError("price должен быть положительным числом")
    return price


def clean_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "{{" in text or len(text) > limit:
        return None
    return text


def parse_command(data: dict) -> Command:
    action_raw = data.get("action")
    if not isinstance(action_raw, str) or "{{" in action_raw:
        raise ValueError("action пустой или шаблон TradingView не подставился")
    action = action_raw.strip().lower()
    if action not in {"buy", "sell", "close"}:
        raise ValueError("action должен быть buy, sell или close")
    return Command(
        action=action,
        symbol=to_ccxt_symbol(data.get("ticker")),
        price=parse_price(data.get("price")),
        signal_time=clean_text(data.get("time"), 64),
        alert_id=clean_text(data.get("alert_id"), 128),
    )


def load_config() -> Config:
    import os

    load_dotenv(ROOT / ".env")
    mode = os.getenv("TRADING_MODE", "paper").strip().lower()
    if mode not in {"paper", "testnet", "live"}:
        raise SystemExit("TRADING_MODE должен быть paper, testnet или live")

    secret = os.getenv("WEBHOOK_SECRET", "").strip()
    if len(secret) < 12:
        raise SystemExit("WEBHOOK_SECRET должен быть не короче 12 символов")
    if secret == PLACEHOLDER_SECRET and mode != "paper":
        raise SystemExit("Замените WEBHOOK_SECRET перед testnet или live")

    confirm_live = os.getenv("CONFIRM_LIVE", "").strip()
    if mode == "live" and confirm_live != LIVE_CONFIRMATION:
        raise SystemExit(
            "Боевая торговля заблокирована. Чтобы включить её, задайте "
            "TRADING_MODE=live и CONFIRM_LIVE=YES_I_UNDERSTAND"
        )

    try:
        pct = Decimal(os.getenv("POSITION_SIZE_PCT", "1").strip())
        deviation = Decimal(os.getenv("MAX_DEVIATION_PCT", "2").strip())
        paper_balance = Decimal(os.getenv("PAPER_BALANCE", "10000").strip())
        port = int(os.getenv("PORT", "5000"))
    except (InvalidOperation, ValueError) as exc:
        raise SystemExit("Проверьте числовые настройки в .env") from exc

    if not pct.is_finite() or not (Decimal("0") < pct <= Decimal("100")):
        raise SystemExit("POSITION_SIZE_PCT должен быть в диапазоне от 0 до 100")
    if not deviation.is_finite() or deviation < 0:
        raise SystemExit("MAX_DEVIATION_PCT не может быть отрицательным")
    if not paper_balance.is_finite() or paper_balance <= 0:
        raise SystemExit("PAPER_BALANCE должен быть больше нуля")

    paper_quote = os.getenv("PAPER_QUOTE", "USDT").strip().upper() or "USDT"
    exchange_id = os.getenv("EXCHANGE_ID", "binance").strip().lower()
    if not exchange_id.isidentifier():
        raise SystemExit("EXCHANGE_ID содержит недопустимые символы")

    return Config(
        secret=secret,
        mode=mode,
        confirm_live=confirm_live,
        exchange_id=exchange_id,
        api_key=os.getenv("API_KEY", "").strip(),
        api_secret=os.getenv("API_SECRET", "").strip(),
        position_size_pct=pct,
        max_deviation_pct=deviation,
        paper_quote=paper_quote,
        paper_balance=paper_balance,
        host=os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=port,
        db_path=ROOT / os.getenv("DB_PATH", "trades.sqlite3"),
        log_path=ROOT / os.getenv("LOG_PATH", "bot.log"),
    )


def setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    alert_id TEXT,
    body_hash TEXT NOT NULL UNIQUE,
    symbol TEXT,
    action TEXT,
    signal_price TEXT,
    signal_time TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    order_id TEXT,
    amount TEXT,
    fill_price TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_alert
    ON events(alert_id) WHERE alert_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS cash (
    currency TEXT PRIMARY KEY,
    amount TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory (
    symbol TEXT PRIMARY KEY,
    amount TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def ensure_cash(self, currency: str, amount: Decimal) -> None:
        with self._lock, self._conn() as con:
            row = con.execute("SELECT 1 FROM cash WHERE currency = ?", (currency,)).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO cash(currency, amount) VALUES (?, ?)",
                    (currency, money(amount)),
                )

    def claim(self, command: Command, body_hash: str) -> int | None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._lock, self._conn() as con:
                cur = con.execute(
                    """
                    INSERT INTO events (
                        created_at, alert_id, body_hash, symbol, action,
                        signal_price, signal_time, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        now,
                        command.alert_id,
                        body_hash,
                        command.symbol,
                        command.action,
                        fmt_num(command.price) if command.price is not None else None,
                        command.signal_time,
                    ),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def finish(
        self,
        event_id: int,
        status: str,
        detail: str,
        order_id: str | None,
        amount: str | None,
        fill_price: str | None,
    ) -> None:
        with self._lock, self._conn() as con:
            con.execute(
                """
                UPDATE events
                SET status = ?, detail = ?, order_id = ?, amount = ?, fill_price = ?
                WHERE id = ?
                """,
                (status, detail, order_id, amount, fill_price, event_id),
            )

    def release(self, event_id: int) -> None:
        with self._lock, self._conn() as con:
            con.execute("DELETE FROM events WHERE id = ? AND status = 'pending'", (event_id,))

    def inventory(self, symbol: str) -> Decimal:
        with self._lock, self._conn() as con:
            return self._inventory(con, symbol)

    def cash_balance(self, currency: str) -> Decimal:
        with self._lock, self._conn() as con:
            return self._cash(con, currency)

    def balances(self) -> list[dict[str, str]]:
        with self._lock, self._conn() as con:
            rows = con.execute("SELECT currency, amount FROM cash ORDER BY currency").fetchall()
        return [{"currency": row["currency"], "amount": fmt_num(dec(row["amount"]))} for row in rows]

    def positions(self) -> list[dict[str, str]]:
        with self._lock, self._conn() as con:
            rows = con.execute("SELECT symbol, amount FROM inventory ORDER BY symbol").fetchall()
        return [{"symbol": row["symbol"], "amount": fmt_num(dec(row["amount"]))} for row in rows]

    def recent_events(self, limit: int = 12) -> list[dict[str, str | None]]:
        with self._lock, self._conn() as con:
            rows = con.execute(
                """
                SELECT created_at, action, symbol, status, amount, fill_price, detail
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _inventory(self, con: sqlite3.Connection, symbol: str) -> Decimal:
        row = con.execute("SELECT amount FROM inventory WHERE symbol = ?", (symbol,)).fetchone()
        return dec(row["amount"]) if row else Decimal("0")

    def _cash(self, con: sqlite3.Connection, currency: str) -> Decimal:
        row = con.execute("SELECT amount FROM cash WHERE currency = ?", (currency,)).fetchone()
        return dec(row["amount"]) if row else Decimal("0")

    def add_inventory(self, symbol: str, qty: Decimal) -> None:
        with self._lock, self._conn() as con:
            updated = self._inventory(con, symbol) + qty
            con.execute(
                """
                INSERT INTO inventory(symbol, amount) VALUES (?, ?)
                ON CONFLICT(symbol) DO UPDATE SET amount = excluded.amount
                """,
                (symbol, money(updated)),
            )

    def reduce_inventory(self, symbol: str, qty: Decimal) -> None:
        with self._lock, self._conn() as con:
            current = self._inventory(con, symbol)
            if qty > current:
                log.error(
                    "Исполнено больше учтённой позиции по %s: sold=%s tracked=%s",
                    symbol,
                    money(qty),
                    money(current),
                )
                updated = Decimal("0")
            else:
                updated = current - qty
            if updated <= QTY_STEP:
                con.execute("DELETE FROM inventory WHERE symbol = ?", (symbol,))
            else:
                con.execute(
                    "UPDATE inventory SET amount = ? WHERE symbol = ?",
                    (money(updated), symbol),
                )

    def paper_buy(self, quote: str, symbol: str, spend: Decimal, qty: Decimal) -> None:
        with self._lock, self._conn() as con:
            cash = self._cash(con, quote)
            if cash < spend:
                raise RiskError("недостаточно бумажного депозита")
            updated_cash = cash - spend
            updated_qty = self._inventory(con, symbol) + qty
            con.execute(
                "UPDATE cash SET amount = ? WHERE currency = ?",
                (money(updated_cash), quote),
            )
            con.execute(
                """
                INSERT INTO inventory(symbol, amount) VALUES (?, ?)
                ON CONFLICT(symbol) DO UPDATE SET amount = excluded.amount
                """,
                (symbol, money(updated_qty)),
            )

    def paper_sell(self, quote: str, symbol: str, price: Decimal) -> Decimal:
        with self._lock, self._conn() as con:
            qty = self._inventory(con, symbol)
            if qty <= 0:
                raise RiskError("нет позиции бота для продажи")
            proceeds = (qty * price).quantize(QTY_STEP, rounding=ROUND_DOWN)
            updated_cash = self._cash(con, quote) + proceeds
            con.execute("DELETE FROM inventory WHERE symbol = ?", (symbol,))
            con.execute(
                "UPDATE cash SET amount = ? WHERE currency = ?",
                (money(updated_cash), quote),
            )
            return qty


class PaperBroker:
    def __init__(self, store: Store, quote: str, balance: Decimal) -> None:
        self.store = store
        self.quote = quote
        self.store.ensure_cash(quote, balance)

    def enter_long(
        self,
        symbol: str,
        pct: Decimal,
        signal_price: Decimal | None,
        attempt: Attempt,
    ) -> Fill:
        base, quote = split_symbol(symbol)
        if quote != self.quote:
            raise RiskError(f"бумажный счёт ведётся в {self.quote}, а символ {symbol}")
        if signal_price is None:
            raise RiskError("в бумажном режиме поле price обязательно")
        cash = self.store.cash_balance(self.quote)
        qty = (cash * pct / Decimal("100") / signal_price).quantize(QTY_STEP, rounding=ROUND_DOWN)
        spend = (qty * signal_price).quantize(QTY_STEP, rounding=ROUND_DOWN)
        if qty <= 0 or spend <= 0:
            raise RiskError("размер позиции округлился до нуля")
        attempt.sent = True
        self.store.paper_buy(quote, symbol, spend, qty)
        return Fill(f"paper-{int(datetime.now().timestamp() * 1000)}", symbol, base, qty, signal_price)

    def exit_long(self, symbol: str, signal_price: Decimal | None, attempt: Attempt) -> Fill:
        base, quote = split_symbol(symbol)
        if quote != self.quote:
            raise RiskError(f"бумажный счёт ведётся в {self.quote}, а символ {symbol}")
        if signal_price is None:
            raise RiskError("в бумажном режиме поле price обязательно")
        attempt.sent = True
        qty = self.store.paper_sell(quote, symbol, signal_price)
        return Fill(f"paper-{int(datetime.now().timestamp() * 1000)}", symbol, base, qty, signal_price)


class CcxtBroker:
    def __init__(self, cfg: Config, store: Store) -> None:
        import ccxt

        if not cfg.api_key or not cfg.api_secret:
            raise SystemExit("Для testnet и live заполните API_KEY и API_SECRET")
        if not hasattr(ccxt, cfg.exchange_id):
            raise SystemExit(f"Биржа {cfg.exchange_id} не найдена в ccxt")

        exchange_cls = getattr(ccxt, cfg.exchange_id)
        self.exchange = exchange_cls(
            {
                "apiKey": cfg.api_key,
                "secret": cfg.api_secret,
                "enableRateLimit": True,
                "timeout": 20000,
                "options": {"defaultType": "spot", "adjustForTimeDifference": True},
            }
        )
        self.store = store
        self.max_deviation_pct = cfg.max_deviation_pct
        if cfg.mode == "testnet":
            if not (getattr(self.exchange, "urls", {}) or {}).get("test"):
                raise SystemExit(f"У {cfg.exchange_id} в ccxt нет sandbox-режима")
            self.exchange.set_sandbox_mode(True)
        try:
            self.exchange.load_markets()
        except ccxt.BaseError as exc:
            log.exception("Биржа недоступна при старте")
            raise SystemExit("Биржа недоступна при старте, ордера не принимаются") from exc

    def enter_long(
        self,
        symbol: str,
        pct: Decimal,
        signal_price: Decimal | None,
        attempt: Attempt,
    ) -> Fill:
        base, quote = split_symbol(symbol)
        self._ensure_symbol(symbol)
        last = self._last_price(symbol)
        self._check_deviation(last, signal_price)
        free_quote = dec(self._free(quote))
        raw_qty = free_quote * pct / Decimal("100") / last
        qty = self._fit_amount(symbol, raw_qty, last)
        self._check_limits(symbol, qty, last)
        attempt.sent = True
        order = self._create("buy", symbol, qty)
        filled, price = self._fill_from(order, qty, last)
        self.store.add_inventory(symbol, filled)
        return Fill(str(order.get("id") or ""), symbol, base, filled, price)

    def exit_long(self, symbol: str, signal_price: Decimal | None, attempt: Attempt) -> Fill:
        base, _quote = split_symbol(symbol)
        self._ensure_symbol(symbol)
        last = self._last_price(symbol)
        self._check_deviation(last, signal_price)
        tracked = self.store.inventory(symbol)
        if tracked <= 0:
            raise RiskError("нет позиции бота для продажи")
        free_base = dec(self._free(base))
        raw_qty = tracked if tracked <= free_base else free_base
        if raw_qty <= 0:
            raise RiskError("на бирже нет свободного остатка, купленного ботом")
        qty = self._fit_amount(symbol, raw_qty, last)
        if qty > tracked:
            qty = self._fit_amount(symbol, tracked * Decimal("0.999999"), last)
        self._check_limits(symbol, qty, last)
        attempt.sent = True
        order = self._create("sell", symbol, qty)
        filled, price = self._fill_from(order, qty, last)
        self.store.reduce_inventory(symbol, filled)
        return Fill(str(order.get("id") or ""), symbol, base, filled, price)

    def _ensure_symbol(self, symbol: str) -> None:
        if symbol not in self.exchange.markets:
            raise RiskError(f"символ {symbol} не найден на спотовом рынке")

    def _last_price(self, symbol: str) -> Decimal:
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return dec(ticker["last"])
        except Exception as exc:
            self._raise_exchange(exc)

    def _free(self, currency: str) -> float:
        try:
            balance = self.exchange.fetch_balance()
        except Exception as exc:
            self._raise_exchange(exc)
        entry = balance.get(currency) or {}
        free = entry.get("free")
        if free is None:
            free = (balance.get("free") or {}).get(currency) or 0
        return float(free or 0)

    def _create(self, side: str, symbol: str, qty: Decimal) -> dict:
        import ccxt

        try:
            return self.exchange.create_market_order(symbol, side, float(qty))
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder) as exc:
            log.exception("Биржа отклонила ордер")
            raise RiskError("биржа отклонила ордер: проверьте размер, шаг лота и баланс") from exc
        except Exception as exc:
            self._raise_exchange(exc)

    def _raise_exchange(self, exc: Exception) -> None:
        import ccxt

        if isinstance(exc, ccxt.BaseError):
            raise ExchangeFailure() from exc
        raise exc

    def _check_deviation(self, last: Decimal, signal_price: Decimal | None) -> None:
        if signal_price is None or self.max_deviation_pct <= 0:
            return
        deviation = abs(last - signal_price) / signal_price * Decimal("100")
        if deviation > self.max_deviation_pct:
            raise RiskError(
                f"цена ушла на {deviation.quantize(Decimal('0.01'))}% от сигнала, ордер не отправлен"
            )

    def _fit_amount(self, symbol: str, amount: Decimal, price: Decimal) -> Decimal:
        if amount <= 0:
            raise RiskError("размер позиции округлился до нуля")
        floored = dec(floor_amount(self.exchange, symbol, float(amount)))
        if floored <= 0 or floored > amount:
            raise RiskError("размер позиции меньше минимального шага лота")
        if floored * price <= 0:
            raise RiskError("размер позиции округлился до нуля")
        return floored

    def _check_limits(self, symbol: str, amount: Decimal, price: Decimal) -> None:
        limits = (self.exchange.market(symbol).get("limits") or {})
        min_amount = dec((limits.get("amount") or {}).get("min") or 0)
        min_cost = dec((limits.get("cost") or {}).get("min") or 0)
        if min_amount > 0 and amount < min_amount:
            raise RiskError("объём меньше минимального количества на бирже")
        notional = amount * price
        if min_cost > 0 and notional < min_cost:
            raise RiskError(
                f"объём {fmt_num(notional)} меньше минимального номинала биржи {fmt_num(min_cost)}"
            )

    @staticmethod
    def _fill_from(order: dict, fallback_amount: Decimal, fallback_price: Decimal) -> tuple[Decimal, Decimal]:
        filled = order.get("filled")
        average = order.get("average") or order.get("price")
        amount = dec(filled) if filled else fallback_amount
        price = dec(average) if average else fallback_price
        if amount <= 0 or price <= 0:
            raise ExchangeFailure()
        return amount, price


def floor_amount(exchange, symbol: str, amount: float) -> float:
    import ccxt

    market = exchange.market(symbol)
    precision = (market.get("precision") or {}).get("amount")
    if precision is None:
        return amount
    number = Decimal(str(amount))
    if exchange.precisionMode == ccxt.TICK_SIZE:
        tick = dec(precision)
        if tick <= 0:
            return amount
        return float((number / tick).to_integral_value(rounding=ROUND_DOWN) * tick)
    if exchange.precisionMode == ccxt.DECIMAL_PLACES:
        quant = Decimal("1").scaleb(-int(precision))
        return float(number.quantize(quant, rounding=ROUND_DOWN))
    floored = float(exchange.amount_to_precision(symbol, amount))
    if floored > amount:
        floored = float(exchange.amount_to_precision(symbol, amount * 0.999999))
    return floored


def build_broker(cfg: Config, store: Store):
    if cfg.mode == "paper":
        return PaperBroker(store, cfg.paper_quote, cfg.paper_balance)
    return CcxtBroker(cfg, store)


def moving_average(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    window = sum(values[:period])
    result[period - 1] = window / period
    for index in range(period, len(values)):
        window += values[index] - values[index - period]
        result[index] = window / period
    return result


def signal_from_closes(closes: list[float]) -> tuple[str, str, list[float | None], list[float | None]]:
    if len(closes) < 22:
        blank: list[float | None] = [None] * len(closes)
        return "wait", "для подсказки ещё мало свечей", blank, list(blank)
    fast = moving_average(closes, 9)
    slow = moving_average(closes, 21)
    if None in (fast[-1], slow[-1], fast[-2], slow[-2]):
        return "wait", "для подсказки ещё мало свечей", fast, slow
    assert fast[-1] is not None and slow[-1] is not None
    assert fast[-2] is not None and slow[-2] is not None
    if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
        return "buy", "быстрая средняя пересекла медленную снизу вверх", fast, slow
    if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
        return "sell", "быстрая средняя пересекла медленную сверху вниз", fast, slow
    if fast[-1] > slow[-1]:
        return "wait", "быстрая средняя выше медленной, нового пересечения нет", fast, slow
    return "wait", "быстрая средняя ниже медленной, нового пересечения нет", fast, slow


def _read_json(url: str) -> dict:
    request_obj = urllib.request.Request(url, headers={"User-Agent": "tradingview-webhook-bot"})
    with urllib.request.urlopen(request_obj, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_closes(base: str, quote: str) -> list[tuple[int, float]]:
    errors: list[str] = []
    try:
        payload = _read_json(
            f"https://www.okx.com/api/v5/market/candles?instId={base}-{quote}&bar=1m&limit=120"
        )
        rows = list(reversed(payload["data"]))
        if not rows:
            raise ValueError("пустой ответ")
        return [(int(row[0]), float(row[4])) for row in rows]
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"okx: {exc}")
    kraken_base = {"BTC": "XBT"}.get(base, base)
    try:
        payload = _read_json(
            f"https://api.kraken.com/0/public/OHLC?pair={kraken_base}{quote}&interval=1"
        )
        result = payload["result"]
        rows = next(value for key, value in result.items() if key != "last")
        return [(int(float(row[0]) * 1000), float(row[4])) for row in rows][-120:]
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError, StopIteration) as exc:
        errors.append(f"kraken: {exc}")
    raise ExchangeFailure("; ".join(errors))


_market_cache: dict[str, tuple[float, dict]] = {}
_market_cache_lock = threading.Lock()


def market_view(ticker: str) -> dict:
    symbol = to_ccxt_symbol(ticker)
    base, quote = split_symbol(symbol)
    now = time.monotonic()
    with _market_cache_lock:
        cached = _market_cache.get(symbol)
        if cached and now - cached[0] < 2:
            return cached[1]
    candles = fetch_closes(base, quote)
    closes = [price for _time, price in candles]
    signal, reason, fast, slow = signal_from_closes(closes)
    payload = {
        "ok": True,
        "symbol": symbol,
        "price": fmt_num(dec(closes[-1])),
        "signal": signal,
        "reason": reason,
        "candles": [{"time": stamp, "close": price} for stamp, price in candles],
        "fast": fast,
        "slow": slow,
    }
    with _market_cache_lock:
        _market_cache[symbol] = (now, payload)
    return payload


def local_request() -> bool:
    host = request.host.split(":")[0].strip("[]").lower()
    return host in {"127.0.0.1", "localhost"}


def csrf_token() -> str:
    token = session.get("csrf")
    if not isinstance(token, str):
        token = secrets.token_hex(16)
        session["csrf"] = token
    return token


def create_app(cfg: Config, broker, store: Store) -> Flask:
    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    app.json.ensure_ascii = False
    app.secret_key = hashlib.sha256(b"panel:" + cfg.secret.encode("utf-8")).hexdigest()
    exec_lock = threading.Lock()

    def panel_context(notice: str | None = None) -> dict:
        quote_rows = [row for row in store.balances() if row["currency"] == cfg.paper_quote]
        quote_amount = quote_rows[0]["amount"] if quote_rows else "0"
        return {
            "mode": cfg.mode,
            "quote": cfg.paper_quote,
            "quote_amount": quote_amount,
            "position_size_pct": fmt_num(cfg.position_size_pct),
            "positions": store.positions(),
            "events": store.recent_events(),
            "csrf": csrf_token(),
            "notice": notice,
        }

    @app.get("/")
    def dashboard():
        if not local_request():
            return render_template("closed.html"), 403
        return render_template("dashboard.html", **panel_context())

    def run_trade(payload: dict) -> tuple[dict, str]:
        try:
            command = parse_command(payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, str(exc)
        if cfg.mode == "paper" and command.price is None:
            return {"ok": False, "error": "Укажи цену"}, "Укажи цену"
        body_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        with exec_lock:
            response, _status = execute(cfg, broker, store, command, body_hash)
        result = response.get_json(silent=True) or {}
        if result.get("status") == "filled":
            verb = {"buy": "Куплено", "sell": "Продано", "close": "Закрыто"}.get(command.action, "Готово")
            message = f"{verb} {result.get('amount')} {result.get('base')} по цене {result.get('price')}"
        elif result.get("status") == "rejected":
            message = str(result.get("reason") or "Ордер отклонён")
        else:
            message = str(result.get("error") or "Не удалось выполнить команду")
        return result, message

    def trade_payload(source: dict, prefix: str) -> dict:
        return {
            "action": source.get("action", ""),
            "ticker": source.get("ticker", ""),
            "price": source.get("price", ""),
            "alert_id": prefix + uuid.uuid4().hex,
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @app.post("/trade")
    def trade():
        if not local_request():
            return jsonify({"ok": False, "error": "панель доступна только с этого компьютера"}), 403
        if not hmac.compare_digest(str(request.form.get("csrf", "")), csrf_token()):
            flash("Страница устарела. Обнови её и повтори.")
            return redirect(url_for("dashboard"))
        _result, message = run_trade(trade_payload(request.form, "panel-"))
        flash(message)
        return redirect(url_for("dashboard"))

    @app.get("/api/state")
    def api_state():
        if not local_request():
            return jsonify({"ok": False, "error": "панель доступна только с этого компьютера"}), 403
        state = panel_context()
        state.pop("csrf", None)
        state["ok"] = True
        return jsonify(state)

    @app.post("/api/trade")
    def api_trade():
        if not local_request():
            return jsonify({"ok": False, "error": "панель доступна только с этого компьютера"}), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "неверный JSON"}), 400
        result, message = run_trade(trade_payload(data, "extension-"))
        status = 200 if result.get("ok") else 400
        return jsonify({**result, "message": message}), status

    @app.get("/market")
    def market():
        if not local_request():
            return jsonify({"ok": False, "error": "график доступен только с этого компьютера"}), 403
        try:
            return jsonify(market_view(str(request.args.get("ticker") or "BTCUSDT")))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except ExchangeFailure:
            log.exception("Котировки недоступны")
            return jsonify({"ok": False, "error": "котировки сейчас недоступны"}), 502

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "mode": cfg.mode})

    @app.post("/webhook")
    def webhook():
        raw_bytes = request.get_data(cache=True) or b""
        if len(raw_bytes) > 16384:
            return jsonify({"ok": False, "error": "неверный JSON"}), 400
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            log.warning("Отклонён запрос: тело не в UTF-8")
            return jsonify({"ok": False, "error": "неверный JSON"}), 400

        log.info("POST /webhook bytes=%s content_type=%s", len(raw_bytes), request.content_type)
        try:
            data = None if not raw.strip() else json.loads(raw)
        except ValueError:
            log.warning("Отклонён запрос: не JSON")
            return jsonify({"ok": False, "error": "неверный JSON"}), 400
        if not isinstance(data, dict):
            log.warning("Отклонён запрос: JSON не объект")
            return jsonify({"ok": False, "error": "неверный JSON"}), 400

        provided = data.get("secret")
        if not isinstance(provided, str) or not secret_matches(provided, cfg.secret):
            log.warning("Отклонён запрос: неверный секрет")
            return jsonify({"ok": False, "error": "неверный секретный ключ"}), 403

        try:
            command = parse_command(data)
        except ValueError as exc:
            log.info("Отклонён запрос: %s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 400

        if cfg.mode == "paper" and command.price is None:
            return jsonify({"ok": False, "error": "в бумажном режиме поле price обязательно"}), 400

        body_hash = hashlib.sha256(raw_bytes).hexdigest()
        with exec_lock:
            return execute(cfg, broker, store, command, body_hash)

    return app


def execute(cfg: Config, broker, store: Store, command: Command, body_hash: str):
    event_id = store.claim(command, body_hash)
    if event_id is None:
        log.info("Дубликат сигнала %s %s", command.action, command.symbol)
        return jsonify({"ok": True, "status": "duplicate"}), 200

    attempt = Attempt()
    try:
        if command.action == "buy":
            fill = broker.enter_long(command.symbol, cfg.position_size_pct, command.price, attempt)
            verb = "Куплено"
        else:
            fill = broker.exit_long(command.symbol, command.price, attempt)
            verb = "Закрыто" if command.action == "close" else "Продано"
    except RiskError as exc:
        store.finish(event_id, "rejected", str(exc), None, None, None)
        log.info("[%s] Отклонено %s %s: %s", cfg.mode.upper(), command.action, command.symbol, exc)
        return jsonify({"ok": True, "status": "rejected", "reason": str(exc)}), 200
    except ExchangeFailure:
        log.exception("Биржа недоступна")
        if attempt.sent:
            store.finish(event_id, "error", "биржа недоступна", None, None, None)
        else:
            store.release(event_id)
        return jsonify({"ok": False, "error": "биржа недоступна"}), 500
    except Exception:
        log.exception("Сбой во время обработки сигнала")
        if attempt.sent:
            store.finish(event_id, "error", "биржа недоступна", None, None, None)
        else:
            store.release(event_id)
        return jsonify({"ok": False, "error": "биржа недоступна"}), 500

    log.info(
        "[%s] %s %s %s по цене %s",
        cfg.mode.upper(),
        verb,
        fmt_num(fill.amount),
        fill.base,
        fmt_num(fill.price),
    )
    store.finish(
        event_id,
        "filled",
        "ok",
        fill.order_id,
        fmt_num(fill.amount),
        fmt_num(fill.price),
    )
    return jsonify(
        {
            "ok": True,
            "status": "filled",
            "mode": cfg.mode,
            "symbol": fill.symbol,
            "action": command.action,
            "amount": fmt_num(fill.amount),
            "base": fill.base,
            "price": fmt_num(fill.price),
            "order_id": fill.order_id,
        }
    ), 200


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_path)
    if cfg.secret == PLACEHOLDER_SECRET:
        log.warning("Используется пример WEBHOOK_SECRET. Замените его до публикации URL.")
    if cfg.position_size_pct > 5:
        log.warning("POSITION_SIZE_PCT=%s. На одну покупку уходит больше 5%% депозита.", cfg.position_size_pct)
    if cfg.mode == "live":
        log.warning("ВКЛЮЧЁН LIVE. Бот может тратить реальные деньги на спотовом рынке.")

    store = Store(cfg.db_path)
    broker = build_broker(cfg, store)
    app = create_app(cfg, broker, store)
    log.info(
        "Режим %s. Панель: http://%s:%s/  Webhook: http://%s:%s/webhook",
        cfg.mode.upper(),
        cfg.host,
        cfg.port,
        cfg.host,
        cfg.port,
    )
    app.run(host=cfg.host, port=cfg.port, threaded=True, use_reloader=False)


def self_test() -> None:
    import json
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    cfg = Config(
        secret=PLACEHOLDER_SECRET,
        mode="paper",
        confirm_live="",
        exchange_id="binance",
        api_key="",
        api_secret="",
        position_size_pct=Decimal("1"),
        max_deviation_pct=Decimal("2"),
        paper_quote="USDT",
        paper_balance=Decimal("10000"),
        host="127.0.0.1",
        port=5000,
        db_path=tmp / "trades.sqlite3",
        log_path=tmp / "bot.log",
    )
    setup_logging(cfg.log_path)
    store = Store(cfg.db_path)
    app = create_app(cfg, PaperBroker(store, cfg.paper_quote, cfg.paper_balance), store)
    client = app.test_client()

    rejected = client.post("/webhook", data="не json", content_type="text/plain")
    assert rejected.status_code == 400, rejected.data

    forbidden = client.post("/webhook", json={"secret": "wrong-secret-value", "action": "buy", "ticker": "BTCUSDT"})
    assert forbidden.status_code == 403, forbidden.data

    placeholder = client.post(
        "/webhook",
        json={"secret": cfg.secret, "action": "{{strategy.order.action}}", "ticker": "BTCUSDT", "price": "1"},
    )
    assert placeholder.status_code == 400, placeholder.data

    perpetual = client.post(
        "/webhook",
        json={"secret": cfg.secret, "action": "buy", "ticker": "BINANCE:BTCUSDT.P", "price": "65000"},
    )
    assert perpetual.status_code == 400, perpetual.data

    payload = {
        "secret": cfg.secret,
        "alert_id": "self-test-1",
        "action": "buy",
        "ticker": "BINANCE:BTCUSDT",
        "price": "65000",
        "time": "2026-11-06T12:00:00Z",
    }
    raw = json.dumps(payload)
    bought = client.post("/webhook", data=raw, content_type="text/plain")
    assert bought.status_code == 200, bought.data
    body = bought.get_json()
    assert body["status"] == "filled", body
    assert body["amount"] == "0.00153846", body
    assert body["price"] == "65000", body
    assert body["base"] == "BTC", body

    duplicate = client.post("/webhook", data=raw, content_type="text/plain")
    assert duplicate.status_code == 200, duplicate.data
    assert duplicate.get_json()["status"] == "duplicate"

    sold = client.post(
        "/webhook",
        json={
            "secret": cfg.secret,
            "alert_id": "self-test-2",
            "action": "sell",
            "ticker": "BTCUSDT",
            "price": "65000",
        },
    )
    assert sold.status_code == 200, sold.data
    assert sold.get_json()["status"] == "filled", sold.get_json()
    assert sold.get_json()["amount"] == "0.00153846"

    flat = client.post(
        "/webhook",
        json={
            "secret": cfg.secret,
            "alert_id": "self-test-3",
            "action": "close",
            "ticker": "BTC/USDT",
            "price": "65000",
        },
    )
    assert flat.status_code == 200, flat.data
    assert flat.get_json()["status"] == "rejected"

    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["mode"] == "paper"
    page = client.get("/")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Купить" in html and "Продать" in html
    hidden = client.get("/", headers={"Host": "example.com"})
    assert hidden.status_code == 403
    signal, _reason, _fast, _slow = signal_from_closes([100.0] * 30 + [110.0])
    assert signal == "buy"
    journal = cfg.log_path.read_text(encoding="utf-8")
    assert "[PAPER] Куплено 0.00153846 BTC по цене 65000" in journal
    assert "[PAPER] Продано 0.00153846 BTC по цене 65000" in journal
    api_buy = client.post("/api/trade", json={"action": "buy", "ticker": "ETHUSDT", "price": "3000"})
    assert api_buy.status_code == 200, api_buy.data
    assert api_buy.get_json()["status"] == "filled"
    api_state = client.get("/api/state")
    assert api_state.status_code == 200
    assert any(row["symbol"] == "ETH/USDT" for row in api_state.get_json()["positions"])
    blocked = client.post(
        "/api/trade",
        json={"action": "sell", "ticker": "ETHUSDT", "price": "3000"},
        headers={"Host": "example.com"},
    )
    assert blocked.status_code == 403
    print("self-test: ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        main()
