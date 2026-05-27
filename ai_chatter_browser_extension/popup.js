'use strict';
async function refresh() {
  const out = document.getElementById('out');
  try {
    const status = await chrome.runtime.sendMessage({ type: 'AI_CHATTER_BRANCH4_STATUS' });
    out.textContent = JSON.stringify(status, null, 2);
  } catch (error) {
    out.textContent = String(error && error.message || error);
  }
}
document.getElementById('refresh').addEventListener('click', refresh);
refresh();
