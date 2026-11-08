import { emptyAxiomAccount, fmt } from "./paper.js";

const balance = document.querySelector("#balance");
const positions = document.querySelector("#positions");

async function render() {
  const stored = await chrome.storage.local.get("axiomPaper");
  const account = stored.axiomPaper || emptyAxiomAccount();
  balance.textContent = `${fmt(account.cash)} USDT`;
  const rows = Object.values(account.positions);
  positions.textContent = rows.length
    ? rows.map((row) => `${row.symbol}: ${fmt(row.amount)}`).join(", ")
    : "Открытых позиций нет.";
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.axiomPaper) {
    render();
  }
});
render();
