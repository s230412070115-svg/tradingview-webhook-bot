const QUOTES = ["USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USD", "EUR", "BTC", "ETH"];
const STEP = 100_000_000;

export function floor8(value) {
  if (!Number.isFinite(value) || value < 0) {
    return 0;
  }
  return Math.floor(value * STEP + 1e-9) / STEP;
}

export function fmt(value) {
  const text = floor8(value).toFixed(8).replace(/\.?0+$/, "");
  return text || "0";
}

export function toSymbol(ticker) {
  let upper = String(ticker || "").trim().toUpperCase();
  if (!upper || upper.includes("{{")) {
    throw new Error("Укажи тикер, например BTCUSDT");
  }
  if (upper.includes(":")) {
    upper = upper.split(":")[1];
  }
  if (upper.endsWith(".P") || upper.endsWith("PERP")) {
    throw new Error("Бессрочные контракты здесь не торгуются");
  }
  if (upper.includes("/")) {
    const [base, quote] = upper.split("/");
    if (!base || !quote || quote.includes(":")) {
      throw new Error("Не удалось разобрать тикер");
    }
    return `${base}/${quote}`;
  }
  for (const quote of QUOTES) {
    if (upper.endsWith(quote) && upper.length > quote.length) {
      return `${upper.slice(0, -quote.length)}/${quote}`;
    }
  }
  throw new Error("Используй BTCUSDT или BTC/USDT");
}

export function emptyAccount() {
  return { cash: 10000, positions: {}, events: [] };
}

export function emptyAxiomAccount() {
  return { cash: 10000, positions: {}, events: [] };
}

function remember(account, action, symbol, amount, tradePrice) {
  account.events.unshift({
    time: new Date().toISOString().slice(0, 16).replace("T", " "),
    action,
    symbol,
    amount: fmt(amount),
    price: fmt(tradePrice),
  });
  account.events = account.events.slice(0, 12);
}

export function buy(account, ticker, tradePrice) {
  const symbol = toSymbol(ticker);
  const quote = symbol.split("/")[1];
  if (quote !== "USDT") {
    throw new Error("Бумажный счёт ведётся в USDT");
  }
  const price = Number(tradePrice);
  if (!Number.isFinite(price) || price <= 0) {
    throw new Error("Укажи цену");
  }
  const qty = floor8((account.cash * 0.01) / price);
  const spend = floor8(qty * price);
  if (qty <= 0 || spend <= 0 || spend > account.cash) {
    throw new Error("Размер позиции округлился до нуля");
  }
  account.cash = floor8(account.cash - spend);
  account.positions[symbol] = floor8((account.positions[symbol] || 0) + qty);
  remember(account, "buy", symbol, qty, price);
  return `Куплено ${fmt(qty)} ${symbol.split("/")[0]} по цене ${fmt(price)}`;
}

export function sell(account, ticker, tradePrice) {
  const symbol = toSymbol(ticker);
  const quote = symbol.split("/")[1];
  if (quote !== "USDT") {
    throw new Error("Бумажный счёт ведётся в USDT");
  }
  const price = Number(tradePrice);
  if (!Number.isFinite(price) || price <= 0) {
    throw new Error("Укажи цену");
  }
  const qty = account.positions[symbol] || 0;
  if (qty <= 0) {
    throw new Error("Нет позиции для продажи");
  }
  const proceeds = floor8(qty * price);
  account.cash = floor8(account.cash + proceeds);
  delete account.positions[symbol];
  remember(account, "sell", symbol, qty, price);
  return `Продано ${fmt(qty)} ${symbol.split("/")[0]} по цене ${fmt(price)}`;
}

export function buyMint(account, mint, symbol, tradePrice) {
  const price = Number(tradePrice);
  if (!Number.isFinite(price) || price <= 0) {
    throw new Error("Цена токена ещё не загрузилась");
  }
  const qty = floor8((account.cash * 0.01) / price);
  const spend = floor8(qty * price);
  if (qty <= 0 || spend <= 0 || spend > account.cash) {
    throw new Error("Размер позиции округлился до нуля");
  }
  const current = account.positions[mint]?.amount || 0;
  account.cash = floor8(account.cash - spend);
  account.positions[mint] = { symbol, amount: floor8(current + qty) };
  remember(account, "buy", symbol, qty, price);
  return `Куплено ${fmt(qty)} ${symbol} по цене ${fmt(price)}`;
}

export function sellMint(account, mint, tradePrice) {
  const position = account.positions[mint];
  if (!position || position.amount <= 0) {
    throw new Error("Нет бумажной позиции по этому токену");
  }
  const price = Number(tradePrice);
  if (!Number.isFinite(price) || price <= 0) {
    throw new Error("Цена токена ещё не загрузилась");
  }
  const proceeds = floor8(position.amount * price);
  account.cash = floor8(account.cash + proceeds);
  delete account.positions[mint];
  remember(account, "sell", position.symbol, position.amount, price);
  return `Продано ${fmt(position.amount)} ${position.symbol} по цене ${fmt(price)}`;
}

export function signalFromCloses(closes) {
  if (closes.length < 22) {
    return { signal: "wait", reason: "для подсказки ещё мало свечей" };
  }
  const average = (period, index) => {
    let sum = 0;
    for (let offset = 0; offset < period; offset += 1) {
      sum += closes[index - offset];
    }
    return sum / period;
  };
  const fastNow = average(9, closes.length - 1);
  const fastPrev = average(9, closes.length - 2);
  const slowNow = average(21, closes.length - 1);
  const slowPrev = average(21, closes.length - 2);
  if (fastPrev <= slowPrev && fastNow > slowNow) {
    return { signal: "buy", reason: "быстрая средняя пересекла медленную снизу вверх" };
  }
  if (fastPrev >= slowPrev && fastNow < slowNow) {
    return { signal: "sell", reason: "быстрая средняя пересекла медленную сверху вниз" };
  }
  if (fastNow > slowNow) {
    return { signal: "wait", reason: "быстрая средняя выше медленной, нового пересечения нет" };
  }
  return { signal: "wait", reason: "быстрая средняя ниже медленной, нового пересечения нет" };
}
