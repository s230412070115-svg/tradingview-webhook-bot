const bot = "http://127.0.0.1:5000";
const connection = document.querySelector("#connection");
const balance = document.querySelector("#balance");
const positions = document.querySelector("#positions");
const events = document.querySelector("#events");
const ticker = document.querySelector("#ticker");
const price = document.querySelector("#price");
const signal = document.querySelector("#signal");

function showConnection(text) {
  connection.textContent = text;
}

async function refresh() {
  try {
    const response = await fetch(`${bot}/api/state`);
    if (!response.ok) {
      showConnection("Бот ответил ошибкой. Открой панель только на этом компьютере.");
      return;
    }
    const state = await response.json();
    showConnection(`Режим ${String(state.mode || "").toUpperCase()}. Биржа Axiom не вызывается.`);
    balance.textContent = `${state.quote_amount} ${state.quote}`;
    positions.textContent = state.positions?.length
      ? state.positions.map((row) => `${row.symbol}: ${row.amount}`).join(", ")
      : "Открытых позиций нет.";
    events.replaceChildren();
    for (const event of (state.events || []).slice(0, 5)) {
      const item = document.createElement("li");
      item.textContent = `${event.action} ${event.symbol} · ${event.amount || "—"} · ${event.status}`;
      events.appendChild(item);
    }
  } catch {
    showConnection("Бот не запущен. В окне PowerShell выполни python app.py и обнови панель.");
  }
}

async function showHint() {
  signal.textContent = "Считаю подсказку…";
  try {
    const response = await fetch(`${bot}/market?ticker=${encodeURIComponent(ticker.value)}`);
    const data = await response.json();
    if (!response.ok) {
      signal.textContent = data.error || "Подсказка недоступна";
      return;
    }
    price.value = data.price;
    const names = { buy: "покупка", sell: "продажа", wait: "ждать" };
    signal.textContent = `${names[data.signal] || data.signal}: ${data.reason}. Цена ${data.price}.`;
  } catch {
    signal.textContent = "Котировки не загрузились. Проверь, что бот запущен.";
  }
}

async function send(action) {
  showConnection("Отправляю бумажную команду…");
  try {
    const response = await fetch(`${bot}/api/trade`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ticker: ticker.value, price: price.value }),
    });
    const data = await response.json();
    showConnection(data.message || data.error || "Команда обработана");
  } catch {
    showConnection("Команда не дошла до бота.");
  }
  await refresh();
}

document.querySelector("#hint").addEventListener("click", showHint);
document.querySelector("#buy").addEventListener("click", () => send("buy"));
document.querySelector("#sell").addEventListener("click", () => send("sell"));
refresh();
