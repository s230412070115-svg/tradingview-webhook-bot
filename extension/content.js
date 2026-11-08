const host = document.createElement("div");
host.id = "paper-overlay-host";
const frame = document.createElement("iframe");
frame.id = "paper-overlay-frame";
frame.src = chrome.runtime.getURL("overlay.html");
frame.title = "Проверка токена";
host.appendChild(frame);
document.documentElement.appendChild(host);

const address = /[1-9A-HJ-NP-Za-km-z]{32,44}/g;
let hidden = false;
let lastMint = "";

function addressesIn(value) {
  return String(value || "").match(address) || [];
}

function findMint() {
  const fromUrl = addressesIn(location.href);
  const pump = fromUrl.find((item) => item.endsWith("pump"));
  if (pump) {
    return pump;
  }
  if (fromUrl.length) {
    return fromUrl.sort((left, right) => right.length - left.length)[0];
  }
  const link = document.querySelector('a[href*="solscan.io/token/"], a[href*="solscan.io/account/"]');
  const fromLink = addressesIn(link?.href);
  return fromLink[0] || "";
}

function publish() {
  const mint = findMint();
  host.classList.toggle("is-hidden", hidden || !mint);
  if (!mint || mint === lastMint) {
    return;
  }
  lastMint = mint;
  frame.contentWindow?.postMessage({ type: "paper-mint", mint }, "*");
}

window.addEventListener("message", (event) => {
  if (event.source !== frame.contentWindow) {
    return;
  }
  if (event.data?.type === "paper-overlay-ready") {
    lastMint = "";
    publish();
  }
  if (event.data?.type === "paper-overlay-close") {
    hidden = true;
    host.classList.add("is-hidden");
  }
  if (event.data?.type === "paper-open") {
    const mint = String(event.data.mint || "");
    if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(mint) || location.href.includes(mint)) {
      return;
    }
    location.assign(`https://axiom.trade/token/${mint}?chain=sol`);
  }
});

setInterval(publish, 800);
