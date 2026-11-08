import { buyMint, emptyAxiomAccount, fmt, sellMint } from "./paper.js";

const title = document.querySelector("#title");
const mintLine = document.querySelector("#mint");
const status = document.querySelector("#status");
const decisionNode = document.querySelector("#decision");
const buyAtNode = document.querySelector("#buy-at");
const sellAtNode = document.querySelector("#sell-at");
const priceNode = document.querySelector("#price");
const liquidityNode = document.querySelector("#liquidity");
const twitterNode = document.querySelector("#twitter");
const risksNode = document.querySelector("#risks");
const creatorNode = document.querySelector("#creator");
const holdersNode = document.querySelector("#holders");
const foundNode = document.querySelector("#found");
const balanceNode = document.querySelector("#balance");
const autoOpen = document.querySelector("#auto-open");

let account = emptyAxiomAccount();
let currentMint = "";
let tokenPrice = 0;
let tokenSymbol = "";
let scanning = false;

function money(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) {
    return "—";
  }
  if (number >= 1_000_000) {
    return `${(number / 1_000_000).toFixed(2)}M`;
  }
  if (number >= 1_000) {
    return `${(number / 1_000).toFixed(1)}K`;
  }
  if (number >= 1) {
    return number.toFixed(2);
  }
  return number.toPrecision(3);
}

function shortKey(value) {
  const text = String(value || "");
  if (text.length < 12) {
    return text || "не указан";
  }
  return `${text.slice(0, 4)}…${text.slice(-4)}`;
}

function allowedOrigin(origin) {
  try {
    const host = new URL(origin).hostname;
    return host === "axiom.trade" || host.endsWith(".axiom.trade");
  } catch {
    return false;
  }
}

async function loadAccount() {
  const stored = await chrome.storage.local.get("axiomPaper");
  account = stored.axiomPaper || emptyAxiomAccount();
  balanceNode.textContent = `${fmt(account.cash)} USDT`;
}

async function saveAccount() {
  await chrome.storage.local.set({ axiomPaper: account });
  balanceNode.textContent = `${fmt(account.cash)} USDT`;
}

function showTab(name) {
  for (const button of document.querySelectorAll(".tab")) {
    button.classList.toggle("on", button.dataset.tab === name);
  }
  document.querySelector("#tab-score").hidden = name !== "score";
  document.querySelector("#tab-wallet").hidden = name !== "wallet";
  document.querySelector("#tab-scan").hidden = name !== "scan";
  if (name === "scan") {
    scan();
  }
}

function fillList(node, rows, emptyText) {
  node.replaceChildren();
  if (!rows.length) {
    const item = document.createElement("li");
    item.textContent = emptyText;
    node.appendChild(item);
    return;
  }
  for (const row of rows) {
    const item = document.createElement("li");
    item.textContent = row;
    node.appendChild(item);
  }
}

function renderWallet(report) {
  const created = Array.isArray(report.creatorTokens) ? report.creatorTokens.length : null;
  const history = created === null ? "история запусков не пришла" : `${created} ${created === 1 ? "токен" : "токенов"} в отчёте`;
  creatorNode.textContent = `Создатель ${shortKey(report.creator)}. ${history}.`;
  const holders = (report.topHolders || []).slice(0, 5).map((holder) => {
    const share = Number(holder.pct);
    const shareText = Number.isFinite(share) ? `${share.toFixed(1)}%` : "доля неизвестна";
    return `${shortKey(holder.owner || holder.address)} · ${shareText}`;
  });
  fillList(holdersNode, holders, "Крупные держатели в отчёте не указаны.");
}

function renderVerdict(report, market) {
  const liquidity = Number(market?.liquidity || report.totalMarketLiquidity) || 0;
  tokenPrice = Number(report.price) || Number(market?.price) || 0;
  const reasons = [];
  if (report.mintAuthority) {
    reasons.push("Создатель может допечатать токены");
  }
  if (report.freezeAuthority) {
    reasons.push("Создатель может заморозить кошельки");
  }
  if (liquidity > 0 && liquidity < 3000) {
    reasons.push("Мало ликвидности");
  }
  if (Number(report.score_normalised) >= 40) {
    reasons.push("Высокая оценка риска в отчёте");
  }
  const largest = report.topHolders?.[0];
  if (largest && Number(largest.pct) >= 20) {
    reasons.push("Один держатель владеет 20% или больше");
  }
  const twitter = market?.twitter || "";
  if (twitter) {
    twitterNode.textContent = "";
    const link = document.createElement("a");
    link.href = twitter;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = twitter.replace(/^https?:\/\/(www\.)?(x|twitter)\.com\//, "@");
    twitterNode.append("X: ", link);
  } else {
    twitterNode.textContent = "В данных пары нет ссылки на X. Ленту Twitter расширение не читает.";
  }
  fillList(risksNode, reasons, "Отдельных предупреждений нет.");
  const blocked = !tokenPrice || report.mintAuthority || report.freezeAuthority || liquidity < 1500 || Number(report.score_normalised) >= 60;
  decisionNode.className = blocked ? "decision bad" : "decision ok";
  document.querySelector("#buy").disabled = blocked;
  if (blocked) {
    decisionNode.textContent = "Не покупать";
    buyAtNode.textContent = "—";
    sellAtNode.textContent = "—";
    status.textContent = reasons[0] || "Недостаточно данных для бумажной покупки.";
    return;
  }
  decisionNode.textContent = "Покупать на бумаге";
  buyAtNode.textContent = `$${money(tokenPrice)}`;
  sellAtNode.textContent = `$${money(tokenPrice * 1.3)}`;
  status.textContent = `Бумажный выход, если цена упадёт до $${money(tokenPrice * 0.8)}. На Axiom ордер не отправляется.`;
}

function renderReport(report, market) {
  const meta = report.tokenMeta || {};
  tokenSymbol = meta.symbol || market?.symbol || "TOKEN";
  const name = meta.name || market?.name || tokenSymbol;
  title.textContent = name === tokenSymbol ? tokenSymbol : `${name} (${tokenSymbol})`;
  priceNode.textContent = tokenPrice ? `$${money(tokenPrice)}` : "—";
  liquidityNode.textContent = money(market?.liquidity || report.totalMarketLiquidity);
  renderVerdict(report, market);
  priceNode.textContent = tokenPrice ? `$${money(tokenPrice)}` : "—";
  renderWallet(report);
}

async function marketQuote(mint) {
  const response = await fetch(`https://api.dexscreener.com/latest/dex/tokens/${mint}`);
  if (!response.ok) {
    return null;
  }
  const payload = await response.json();
  const pairs = (payload.pairs || [])
    .filter((pair) => pair.chainId === "solana")
    .sort((left, right) => (right.liquidity?.usd || 0) - (left.liquidity?.usd || 0));
  const pair = pairs[0];
  if (!pair) {
    return null;
  }
  const socials = pair.info?.socials || [];
  const twitter = socials.find((item) => item.type === "twitter")?.url || "";
  return {
    price: Number(pair.priceUsd),
    liquidity: Number(pair.liquidity?.usd),
    symbol: pair.baseToken?.symbol,
    name: pair.baseToken?.name,
    twitter,
  };
}

async function loadToken(mint) {
  currentMint = mint;
  tokenPrice = 0;
  mintLine.textContent = shortKey(mint);
  title.textContent = "Проверяю токен…";
  status.textContent = "Смотрю отчёт, цену и ссылку на X.";
  risksNode.replaceChildren();
  const [reportResult, marketResult] = await Promise.allSettled([
    fetch(`https://api.rugcheck.xyz/v1/tokens/${mint}/report`).then((response) => {
      if (!response.ok) {
        throw new Error("отчёт не открылся");
      }
      return response.json();
    }),
    marketQuote(mint),
  ]);
  const report = reportResult.status === "fulfilled" ? reportResult.value : {};
  const market = marketResult.status === "fulfilled" ? marketResult.value : null;
  if (!report.tokenMeta && !market) {
    status.textContent = "Не удалось получить отчёт. Обнови страницу токена.";
    return;
  }
  renderReport(report, market);
}

function openMint(mint) {
  window.parent.postMessage({ type: "paper-open", mint }, "*");
}

async function scan() {
  if (scanning) {
    return;
  }
  scanning = true;
  foundNode.replaceChildren();
  const loading = document.createElement("li");
  loading.textContent = "Ищу новые токены Solana…";
  foundNode.appendChild(loading);
  try {
    const response = await fetch("https://api.dexscreener.com/token-profiles/latest/v1");
    const rows = (await response.json()).filter((row) => row.chainId === "solana").slice(0, 8);
    foundNode.replaceChildren();
    if (!rows.length) {
      loading.textContent = "Новых токенов в ленте нет.";
      foundNode.appendChild(loading);
      return;
    }
    for (const row of rows) {
      const item = document.createElement("li");
      const twitter = (row.links || []).find((link) => link.type === "twitter");
      item.textContent = `${shortKey(row.tokenAddress)}${twitter ? " · есть X" : ""}`;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Открыть";
      button.addEventListener("click", () => openMint(row.tokenAddress));
      item.append(" ", button);
      foundNode.appendChild(item);
    }
    if (autoOpen.checked) {
      await openFresh(rows[0].tokenAddress);
    }
  } catch {
    loading.textContent = "Лента токенов сейчас недоступна.";
    foundNode.replaceChildren(loading);
  } finally {
    scanning = false;
  }
}

async function openFresh(mint) {
  const stored = await chrome.storage.session.get("openedAt");
  if (Date.now() - Number(stored.openedAt || 0) < 90_000) {
    status.textContent = "Следующий токен откроется не раньше чем через минуту, чтобы страницы не листались без остановки.";
    return;
  }
  await chrome.storage.session.set({ openedAt: Date.now() });
  openMint(mint);
}

async function trade(action) {
  try {
    const message = action === "buy"
      ? buyMint(account, currentMint, tokenSymbol || "TOKEN", tokenPrice)
      : sellMint(account, currentMint, tokenPrice);
    await saveAccount();
    status.textContent = message;
  } catch (error) {
    status.textContent = error.message || "Команда не выполнена";
  }
}

for (const button of document.querySelectorAll(".tab")) {
  button.addEventListener("click", () => showTab(button.dataset.tab));
}
document.querySelector("#refresh-scan").addEventListener("click", scan);
document.querySelector("#buy").addEventListener("click", () => trade("buy"));
document.querySelector("#sell").addEventListener("click", () => trade("sell"));
document.querySelector("#close").addEventListener("click", () => {
  window.parent.postMessage({ type: "paper-overlay-close" }, "*");
});

window.addEventListener("message", (event) => {
  if (!allowedOrigin(event.origin) || event.data?.type !== "paper-mint") {
    return;
  }
  const mint = String(event.data.mint || "");
  if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(mint) || mint === currentMint) {
    return;
  }
  loadToken(mint);
});

window.parent.postMessage({ type: "paper-overlay-ready" }, "*");
loadAccount();
