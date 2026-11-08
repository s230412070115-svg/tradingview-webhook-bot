const button = document.createElement("button");
button.type = "button";
button.id = "paper-account-launcher";
button.textContent = "Бумажный счёт";
button.addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "open-panel" });
});
document.documentElement.appendChild(button);
