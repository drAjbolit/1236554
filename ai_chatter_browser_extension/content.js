'use strict';

const AI_CHATTER_V4_CONTENT_VERSION = '4.0.17';
const AI_CHATTER_V4_PROTOCOL = 'ai_chatter.local_bus.v4';

window.__AI_CHATTER_BRANCH4_PAGE_WORKER = {
  version: AI_CHATTER_V4_CONTENT_VERSION,
  branch: '4',
  protocol: AI_CHATTER_V4_PROTOCOL,
  loadedAt: new Date().toISOString(),
  url: location.href,
  title: document.title
};

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function visible(el) {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
}

function textOf(el) {
  return (el && (el.innerText || el.textContent) || '').replace(/\u00a0/g, ' ').trim();
}

function isEditable(el) {
  if (!el || !visible(el)) return false;
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'textarea') return true;
  if (tag === 'input') return ['text', 'search', ''].includes((el.type || '').toLowerCase());
  if (el.isContentEditable) return true;
  if (el.getAttribute('contenteditable') === 'true') return true;
  return false;
}

function findInput() {
  const selectors = [
    'textarea:not([disabled])',
    'div[contenteditable="true"]',
    '[contenteditable="true"]',
    'input[type="text"]:not([disabled])'
  ];
  const candidates = [];
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      if (isEditable(el)) candidates.push(el);
    }
  }
  candidates.sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
  return candidates[0] || null;
}

function setInputValue(el, text) {
  el.focus();
  if (el.tagName && el.tagName.toLowerCase() === 'textarea') {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
    if (setter) setter.call(el, text); else el.value = text;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return;
  }
  if (el.tagName && el.tagName.toLowerCase() === 'input') {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(el, text); else el.value = text;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return;
  }
  // contenteditable
  document.execCommand('selectAll', false, null);
  document.execCommand('insertText', false, text);
  el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
}

function inputText(el) {
  if (!el) return '';
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'textarea' || tag === 'input') return String(el.value || '').trim();
  return textOf(el);
}

function findSendButton() {
  const selectors = [
    'button[type="submit"]',
    'button[aria-label*="Send" i]',
    'button[aria-label*="send" i]',
    'button[aria-label*="Отправ" i]',
    'button[title*="Send" i]',
    'button[title*="Отправ" i]'
  ];
  for (const sel of selectors) {
    const buttons = Array.from(document.querySelectorAll(sel)).filter(b => visible(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true');
    buttons.sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
    if (buttons[0]) return buttons[0];
  }
  const all = Array.from(document.querySelectorAll('button')).filter(b => visible(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true');
  // Prefer buttons near the lower right with svg/icon and little text.
  all.sort((a, b) => {
    const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
    const ascore = ar.bottom + ar.right + (a.querySelector('svg') ? 5000 : 0) - textOf(a).length * 20;
    const bscore = br.bottom + br.right + (b.querySelector('svg') ? 5000 : 0) - textOf(b).length * 20;
    return bscore - ascore;
  });
  return all[0] || null;
}

function normalized(s) {
  return String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
}

function isPromptEcho(text, prompt) {
  const t = normalized(text);
  const p = normalized(prompt);
  if (!t) return true;
  if (t === p) return true;
  // Only reject exact addressed-prompt echoes such as "Квен, тест 353" -> "тест 353".
  // Do NOT reject arbitrary prompt fragments: for one-word tasks the valid answer may be
  // the target word contained in the prompt, e.g. "ответь одним словом: qwen406" -> "qwen406".
  const stripped = p.replace(/^(qwen|квен|куэн|квэн)[,：:\s-]+/i, '').trim();
  if (stripped && t === stripped) return true;
  return false;
}

function isBoilerplate(text) {
  const t = normalized(text);
  if (!t) return true;
  const bad = [
    'evaluating the input for meaning and context',
    'пропустить',
    'engaging in the collaborative dialogue',
    'participating in the ai chatter circle',
    'responding with the requested word',
    'автоматический',
    'содержимое, созданное ии, может быть неточным',
    'ai-generated content may be inaccurate',
    'ai generated content may be inaccurate',
    'content generated by ai may be inaccurate',
    'мышление',
    'думаю',
    'думаю...',
    'thinking',
    'завершено размышление',
    'finished thinking',
    'reasoning complete',
    'deepthink',
    'deep think',
    'searching',
    'поиск',
    'думает'
  ];
  return bad.some(x => t === x || t.includes(x));
}

function isQwenStatusElement(el) {
  if (!el) return false;
  let node = el;
  for (let depth = 0; node && node.nodeType === 1 && depth < 8; depth++, node = node.parentElement) {
    const cls = String(node.className || '').toLowerCase();
    if (cls.includes('qwen-chat-status-card') || cls.includes('chat-status-card')) return true;
  }
  return false;
}

function elementDomPath(el) {
  const parts = [];
  let node = el;
  for (let depth = 0; node && node.nodeType === 1 && depth < 6; depth++, node = node.parentElement) {
    const tag = (node.tagName || '').toLowerCase();
    const cls = String(node.className || '').split(/\s+/).filter(Boolean).slice(0, 2).join('.');
    parts.push(cls ? `${tag}.${cls}` : tag);
  }
  return parts.reverse().join('>');
}

function blockSignature(text, rect, selector) {
  const t = normalized(text).slice(0, 240);
  const top = Math.round((rect && rect.top || 0) / 8) * 8;
  const left = Math.round((rect && rect.left || 0) / 8) * 8;
  return `${selector || ''}|${top}|${left}|${t}`;
}

function looksLikeAppContainer(el, text) {
  if (!el) return false;
  if (text.length > 8000) return true;
  const r = el.getBoundingClientRect();
  if (r.width > window.innerWidth * 0.75 && r.height > window.innerHeight * 0.65 && text.length > 1000) return true;
  return false;
}

function isUiOrFooterElement(el, text) {
  if (!el) return true;
  if (!visible(el)) return true;
  if (isEditable(el)) return true;
  const r = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  const pos = style.position || '';
  if ((pos === 'fixed' || pos === 'sticky') && r.bottom > window.innerHeight - 180) return true;
  if (r.bottom > window.innerHeight - 28 && text.length < 120) return true;
  const role = (el.getAttribute('role') || '').toLowerCase();
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'button' || role === 'button' || tag === 'nav' || tag === 'aside') return true;
  return false;
}

function candidateAnswerBlocks(prompt, baseline = null) {
  const selectors = [
    '[data-message-author-role="assistant"]',
    '[data-role="assistant"]',
    '.markdown',
    '.prose',
    '[class*="assistant"]',
    '[class*="message"]',
    'article',
    'main [class]',
    'main p',
    'main div'
  ];
  const out = [];
  const seen = new Set();
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      if (!visible(el)) continue;
      if (seen.has(el)) continue;
      seen.add(el);
      const txt = textOf(el);
      if (!txt) continue;
      const r = el.getBoundingClientRect();
      if (isUiOrFooterElement(el, txt)) continue;
      if (looksLikeAppContainer(el, txt)) continue;
      const sig = blockSignature(txt, r, sel);
      const norm = normalized(txt);
      const existedInBaseline = !!(baseline && (baseline.texts.has(norm) || baseline.signatures.has(sig)));
      const newText = !existedInBaseline;
      let score = 0;
      if (newText) score += 1000;
      if (sel.includes('assistant') || sel.includes('markdown') || sel.includes('prose') || sel.includes('article')) score += 200;
      // Prefer central chat area, not left/right sidebars.
      if (r.left > window.innerWidth * 0.18 && r.right < window.innerWidth * 0.92) score += 80;
      // Prefer blocks above the input bar but not in the footer.
      if (r.bottom < window.innerHeight - 120) score += 50;
      // Prefer compact assistant content over parent containers.
      if (txt.length <= 3000) score += 30;
      if (txt.length <= 200) score += 20;
      out.push({ el, text: txt, bottom: r.bottom, top: r.top, left: r.left, right: r.right, selector: sel, signature: sig, newText, score, domPath: elementDomPath(el) });
    }
  }
  out.sort((a, b) => (a.score - b.score) || (a.bottom - b.bottom));
  return out;
}

function takeTextSnapshot() {
  const blocks = candidateAnswerBlocks('', null);
  const texts = new Set();
  const signatures = new Set();
  for (const b of blocks) {
    texts.add(normalized(b.text));
    signatures.add(b.signature);
  }
  return { at: Date.now(), count: blocks.length, texts, signatures };
}

function hasCompletionSignal() {
  const bodyText = textOf(document.body).toLowerCase();
  const reasoningDone = bodyText.includes('завершено размышление') || bodyText.includes('finished thinking') || bodyText.includes('reasoning complete');
  const controls = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible).map(b => ((b.getAttribute('aria-label') || b.getAttribute('title') || textOf(b))).toLowerCase());
  const controlsVisible = controls.some(t => t.includes('copy') || t.includes('копировать') || t.includes('regenerate') || t.includes('повтор') || t.includes('like') || t.includes('dislike'));
  return { ok: reasoningDone || controlsVisible, reasoningDone, controlsVisible };
}

async function waitForInputCleared(input, prompt, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = inputText(input);
    if (!value || !normalized(value).includes(normalized(prompt).slice(0, Math.min(30, normalized(prompt).length)))) {
      return true;
    }
    await sleep(100);
  }
  return false;
}

async function waitForFinalAnswer(prompt, timeoutMs, baseline) {
  const started = Date.now();
  const deadline = started + timeoutMs;
  let best = '';
  let bestMeta = null;
  let completion = { ok: false, reasoningDone: false, controlsVisible: false };
  let lastCandidateDebug = [];
  while (Date.now() < deadline) {
    completion = hasCompletionSignal();
    const candidates = candidateAnswerBlocks(prompt, baseline);
    const useful = [];
    for (let i = candidates.length - 1; i >= 0; i--) {
      const c = candidates[i];
      const txt = c.text;
      let reject = '';
      if (!c.newText) reject = 'baseline';
      else if (isPromptEcho(txt, prompt)) reject = 'prompt_echo';
      else if (isBoilerplate(txt) || isQwenStatusElement(c.el)) reject = 'status_or_boilerplate_ui';
      else if (txt.length < 2) reject = 'too_short';
      else if (txt.length > 10000) reject = 'too_long';
      if (!reject) useful.push(c);
    }
    useful.sort((a, b) => b.score - a.score || b.bottom - a.bottom);
    lastCandidateDebug = candidates.slice(-12).reverse().map(c => ({
      text: c.text.slice(0, 160),
      score: c.score,
      newText: c.newText,
      selector: c.selector,
      top: Math.round(c.top),
      bottom: Math.round(c.bottom),
      domPath: c.domPath,
      qwenStatus: isQwenStatusElement(c.el)
    }));
    if (useful.length) {
      const c = useful[0];
      best = c.text;
      bestMeta = { selector: c.selector, bottom: c.bottom, top: c.top, length: c.text.length, score: c.score, newText: c.newText, domPath: c.domPath };
    }
    if (best && (completion.ok || Date.now() - started > 3000)) {
      await sleep(700);
      completion = hasCompletionSignal();
      const cands2 = candidateAnswerBlocks(prompt, baseline).filter(c => {
        const txt = c.text;
        return c.newText && !isPromptEcho(txt, prompt) && !isBoilerplate(txt) && !isQwenStatusElement(c.el) && txt.length >= 2 && txt.length <= 10000;
      });
      cands2.sort((a, b) => b.score - a.score || b.bottom - a.bottom);
      if (cands2[0] && (cands2[0].score >= (bestMeta && bestMeta.score || 0) || cands2[0].text.length >= best.length)) {
        best = cands2[0].text;
        bestMeta = { selector: cands2[0].selector, bottom: cands2[0].bottom, top: cands2[0].top, length: best.length, score: cands2[0].score, newText: cands2[0].newText, domPath: cands2[0].domPath };
      }
      return {
        ok: true,
        reply: best,
        completion,
        meta: bestMeta,
        elapsed_ms: Date.now() - started,
        snapshotDiff: {
          beforeCount: baseline ? baseline.count : null,
          afterCount: candidateAnswerBlocks(prompt, null).length,
          candidateCount: cands2.length,
          candidates: cands2.slice(0, 10).map(c => ({ text: c.text.slice(0, 200), score: c.score, selector: c.selector, top: Math.round(c.top), bottom: Math.round(c.bottom), domPath: c.domPath, qwenStatus: isQwenStatusElement(c.el) }))
        }
      };
    }
    await sleep(250);
  }
  return {
    ok: false,
    error: 'answer_timeout',
    reply: best,
    completion,
    meta: bestMeta,
    elapsed_ms: Date.now() - started,
    snapshotDiff: { beforeCount: baseline ? baseline.count : null, candidates: lastCandidateDebug }
  };
}

async function runProviderJob(message) {
  const startedAt = Date.now();
  const provider = message.provider || 'qwen';
  const prompt = String(message.text || '');
  const timeoutMs = Math.max(5000, Number(message.timeout_ms || 90000));
  const host = String(location.hostname || '').toLowerCase();
  const providerHosts = {
    qwen: 'qwen.ai',
    deepseek: 'deepseek.com'
  };
  if (!providerHosts[provider]) return { ok: false, code: 'PROVIDER_NOT_SUPPORTED_BY_CONTENT', error: `Unsupported content provider: ${provider}` };
  if (!host.includes(providerHosts[provider])) return { ok: false, code: 'WRONG_PROVIDER_TAB', error: `This tab is not ${provider}: ${location.href}` };

  const baseline = takeTextSnapshot();
  const input = findInput();
  if (!input) return { ok: false, code: 'INPUT_NOT_FOUND', error: `${provider} input was not found` };
  setInputValue(input, prompt);
  await sleep(250);
  const sendButton = findSendButton();
  if (!sendButton) return { ok: false, code: 'SEND_BUTTON_NOT_FOUND', error: `${provider} send button was not found`, diagnostics: { inputText: inputText(input).slice(0, 200) } };
  sendButton.click();

  const cleared = await waitForInputCleared(input, prompt, 5000);
  // Branch 4.0.14: Qwen sometimes keeps text in the editable element even though the
  // message was actually submitted and the assistant answer appears on the page.
  // Do not fail immediately. Treat input clearing as one confirmation signal, but
  // allow the stronger signal: a new assistant reply after our baseline snapshot.
  const sendDiagnostics = {
    inputCleared: !!cleared,
    inputTextAfterSend: inputText(input).slice(0, 200)
  };

  const final = await waitForFinalAnswer(prompt, Math.max(5000, timeoutMs - (Date.now() - startedAt)), baseline);
  if (!final.ok && !final.reply) {
    return { ok: false, code: cleared ? 'ANSWER_TIMEOUT' : 'SEND_NOT_CONFIRMED_NO_ASSISTANT_REPLY', error: final.error || 'No final answer found', diagnostics: { send: sendDiagnostics, completion: final.completion, meta: final.meta } };
  }

  return {
    ok: true,
    reply: final.reply,
    session_id: `${provider}:direct`,
    url: location.href,
    title: document.title,
    diagnostics: {
      provider,
      content_version: AI_CHATTER_V4_CONTENT_VERSION,
      send: sendDiagnostics,
      completion: final.completion,
      answerMeta: final.meta,
      snapshotDiff: final.snapshotDiff || null,
      elapsed_ms: Date.now() - startedAt
    },
    completion_signals: [
      { finish_reason: final.completion && final.completion.ok ? 'stop' : 'best_effort', stop_sequence: null, truncated: false }
    ],
    timings: {
      queued_ms: 0,
      network_ms: 0,
      processing_ms: Date.now() - startedAt,
      total_ms: Date.now() - startedAt
    }
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== 'AI_CHATTER_V4_PROVIDER_JOB') return false;
  runProviderJob(message).then(sendResponse).catch(error => sendResponse({ ok: false, code: 'CONTENT_EXCEPTION', error: String(error && error.stack || error) }));
  return true;
});

chrome.runtime.sendMessage({
  type: 'AI_CHATTER_SET_BADGE',
  state: 'ok',
  text: '4'
}).catch(() => {});
