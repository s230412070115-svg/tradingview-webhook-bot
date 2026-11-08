chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === "open-panel" && sender.tab?.id !== undefined) {
    chrome.sidePanel.open({ tabId: sender.tab.id }).catch(() => {});
  }
});
