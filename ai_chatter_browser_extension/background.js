'use strict';

const VERSION = '4.0.17';
const BRANCH = '4';
const NATIVE_HOST = 'ai_chatter.native_host';
const PROTOCOL = 'ai_chatter.local_bus.v4';

let nativePort = null;
let nativeConnected = false;
let reconnectTimer = null;
let lastNativeError = '';
let lastSeenAt = 0;
const mockJobs = new Map();
const processedEventIds = new Set();
const MAX_PROCESSED_EVENT_IDS = 10000;

function nowIso() {
  return new Date().toISOString();
}

function newId(prefix = 'evt') {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function makeEnvelope(type, payload = {}, extra = {}) {
  return {
    id: extra.id || newId('ext'),
    type,
    source: extra.source || 'chrome_extension',
    target: extra.target || 'native_host',
    timestamp: nowIso(),
    protocol: PROTOCOL,
    branch: BRANCH,
    version: VERSION,
    correlation_id: extra.correlation_id || null,
    job_id: extra.job_id || null,
    payload
  };
}

function eventBaseFrom(message) {
  return {
    target: message.source || 'native_host',
    correlation_id: message.correlation_id || null,
    job_id: message.job_id || null
  };
}

function postNative(message) {
  if (!nativePort || !nativeConnected) return false;
  try {
    nativePort.postMessage(message);
    return true;
  } catch (error) {
    lastNativeError = String(error && error.message || error);
    nativeConnected = false;
    return false;
  }
}

function validateMockJob(message) {
  const payload = message.payload || {};
  if (!message.job_id) return 'missing job_id';
  if (!message.correlation_id) return 'missing correlation_id';
  if (payload.provider !== 'mock') return `unsupported provider for mock MVP: ${payload.provider}`;
  if (typeof payload.text !== 'string' || !payload.text.trim()) return 'missing payload.text';
  return '';
}

function rememberEventId(eventId) {
  if (!eventId) return false;
  if (processedEventIds.has(eventId)) return true;
  processedEventIds.add(eventId);
  if (processedEventIds.size > MAX_PROCESSED_EVENT_IDS) {
    const first = processedEventIds.values().next().value;
    processedEventIds.delete(first);
  }
  return false;
}

function scheduleMockJob(message) {
  const payload = message.payload || {};
  if (rememberEventId(message.id)) {
    // Exact event duplicate: do not emit accepted/already_processing again.
    return null;
  }
  const base = eventBaseFrom(message);
  const jobId = message.job_id;
  const existing = mockJobs.get(jobId);

  if (existing && existing.finalEvent) {
    setTimeout(() => postNative({ ...existing.finalEvent, id: newId('evt_done_replay'), timestamp: nowIso(), payload: { ...existing.finalEvent.payload, replay: true } }), 10);
    return makeEnvelope('provider.job.accepted', {
      queue_position: 0,
      estimated_start_ms: Date.now(),
      status: 'already_completed'
    }, base);
  }

  if (existing && existing.status && existing.status !== 'done' && existing.status !== 'error') {
    return makeEnvelope('provider.job.accepted', {
      queue_position: 0,
      estimated_start_ms: Date.now(),
      status: 'already_processing'
    }, base);
  }

  const bad = validateMockJob(message);
  if (bad) {
    const err = makeEnvelope('provider.job.error', {
      code: 'INVALID_PAYLOAD',
      message: bad,
      recoverable: false,
      retry_recommended: false,
      details: { provider: payload.provider || null }
    }, base);
    mockJobs.set(jobId || newId('unknown'), { status: 'error', finalEvent: err });
    return err;
  }

  const delayMs = Math.max(0, Number(payload.metadata && payload.metadata.mock_delay_ms || 1200));
  const simulateError = Boolean(payload.metadata && payload.metadata.simulate_error);
  const simulateTimeout = Boolean(payload.metadata && payload.metadata.simulate_timeout);
  const simulateDuplicateDone = Boolean(payload.metadata && payload.metadata.simulate_duplicate_done);
  const simulateLateProgress = Boolean(payload.metadata && payload.metadata.simulate_late_progress);
  const simulateOrphanDone = Boolean(payload.metadata && payload.metadata.simulate_orphan_done);
  const reply = payload.metadata && payload.metadata.mock_reply || `Mock reply: ${payload.text}`;
  const startedAt = Date.now();

  if (simulateOrphanDone) {
    const orphanDone = makeEnvelope('provider.job.done', {
      reply,
      provider: 'mock',
      agent_id: payload.agent_id || 'mock_agent',
      session_id: 'mock:direct',
      diagnostics: { model_version: 'mock-provider-v0.1', orphan: true },
      completion_signals: [ { finish_reason: 'stop', stop_sequence: null, truncated: false } ],
      timings: { queued_ms: 0, network_ms: 0, processing_ms: 10, total_ms: 10 }
    }, base);
    mockJobs.set(jobId, { status: 'done', finalEvent: orphanDone, startedAt });
    setTimeout(() => postNative(orphanDone), 50);
    return null;
  }

  mockJobs.set(jobId, { status: 'accepted', finalEvent: null, startedAt });

  const accepted = makeEnvelope('provider.job.accepted', {
    queue_position: 0,
    estimated_start_ms: Date.now() + 50,
    status: 'accepted'
  }, base);

  setTimeout(() => {
    const job = mockJobs.get(jobId);
    if (!job || job.finalEvent) return;
    job.status = 'in_progress';
    postNative(makeEnvelope('provider.job.progress', {
      percent: 15,
      step: 'context_loading',
      hint: 'mock_session_initialized',
      chunk: null
    }, base));
  }, Math.min(300, Math.max(50, delayMs / 4)));

  setTimeout(() => {
    const job = mockJobs.get(jobId);
    if (!job || job.finalEvent) return;
    postNative(makeEnvelope('provider.job.progress', {
      percent: 78,
      step: 'streaming_response',
      hint: 'mock_generation',
      chunk: reply.slice(0, 80)
    }, base));
  }, Math.min(900, Math.max(100, delayMs / 2)));

  if (!simulateTimeout) {
    setTimeout(() => {
      const job = mockJobs.get(jobId);
      if (!job || job.finalEvent) return;
      let finalEvent;
      if (simulateError) {
        finalEvent = makeEnvelope('provider.job.error', {
          code: 'MOCK_PROVIDER_ERROR',
          message: 'Mock provider error requested by metadata.simulate_error',
          recoverable: true,
          retry_recommended: false,
          details: { provider: 'mock' }
        }, base);
        job.status = 'error';
      } else {
        const total = Date.now() - startedAt;
        finalEvent = makeEnvelope('provider.job.done', {
          reply,
          provider: 'mock',
          agent_id: payload.agent_id || 'mock_agent',
          session_id: 'mock:direct',
          diagnostics: {
            model_version: 'mock-provider-v0.1',
            input_tokens: String(payload.text || '').split(/\s+/).filter(Boolean).length,
            output_tokens: String(reply || '').split(/\s+/).filter(Boolean).length,
            cache_hit: false,
            rate_limit_remaining: 100
          },
          completion_signals: [
            { finish_reason: 'stop', stop_sequence: null, truncated: false }
          ],
          timings: {
            queued_ms: 0,
            network_ms: 0,
            processing_ms: total,
            total_ms: total
          }
        }, base);
        job.status = 'done';
      }
      job.finalEvent = finalEvent;
      postNative(finalEvent);
      if (simulateDuplicateDone && finalEvent.type === 'provider.job.done') {
        setTimeout(() => postNative({ ...finalEvent, id: newId('evt_done_duplicate'), timestamp: nowIso(), payload: { ...finalEvent.payload, duplicate_simulation: true } }), 250);
      }
      if (simulateLateProgress) {
        setTimeout(() => postNative(makeEnvelope('provider.job.progress', {
          percent: 99,
          step: 'late_progress_after_final',
          hint: 'should_be_ignored',
          chunk: 'late progress after final'
        }, base)), 300);
      }
    }, delayMs);
  }

  return accepted;
}

async function findProviderTab(provider) {
  const patterns = {
    qwen: ['*://chat.qwen.ai/*', '*://*.qwen.ai/*'],
    deepseek: ['*://chat.deepseek.com/*', '*://*.deepseek.com/*']
  };
  const urls = patterns[provider] || [];
  for (const pattern of urls) {
    const tabs = await chrome.tabs.query({ url: pattern });
    const usable = tabs.filter(t => t.id && !t.discarded);
    if (usable.length) return usable[0];
  }
  return null;
}

async function runWebProviderJob(message, provider) {
  if (rememberEventId(message.id)) {
    return null;
  }
  const payload = message.payload || {};
  const base = eventBaseFrom(message);
  const jobId = message.job_id;
  const bad = (() => {
    if (!jobId) return 'missing job_id';
    if (!message.correlation_id) return 'missing correlation_id';
    if (typeof payload.text !== 'string' || !payload.text.trim()) return 'missing payload.text';
    return '';
  })();
  if (bad) {
    return makeEnvelope('provider.job.error', {
      code: 'INVALID_PAYLOAD',
      message: bad,
      recoverable: false,
      retry_recommended: false,
      details: { provider }
    }, base);
  }

  const accepted = makeEnvelope('provider.job.accepted', {
    queue_position: 0,
    estimated_start_ms: Date.now(),
    status: 'accepted',
    provider: 'qwen',
    transport: 'extension_native_worker_v414'
  }, base);
  postNative(accepted);

  const tab = await findProviderTab(provider);
  if (!tab || !tab.id) {
    return makeEnvelope('provider.job.error', {
      code: 'PROVIDER_TAB_NOT_FOUND',
      message: `${provider} tab was not found. Open the provider page and reload the extension.`,
      recoverable: true,
      retry_recommended: false,
      details: { provider }
    }, base);
  }

  postNative(makeEnvelope('provider.job.progress', {
    percent: 10,
    step: 'tab_found',
    hint: `${provider}_tab_selected`,
    chunk: null,
    diagnostics: { tabId: tab.id, url: tab.url || null, title: tab.title || null }
  }, base));

  try {
    await chrome.tabs.update(tab.id, { active: true });
  } catch (_) {}

  const contentJobMessage = {
    type: 'AI_CHATTER_V4_PROVIDER_JOB',
    protocol: PROTOCOL,
    branch: BRANCH,
    version: VERSION,
    job_id: jobId,
    correlation_id: message.correlation_id,
    provider,
    text: payload.text,
    timeout_ms: Number(payload.timeout_ms || 90000),
    metadata: payload.metadata || {}
  };

  async function sendToContentWorker() {
    return await chrome.tabs.sendMessage(tab.id, contentJobMessage);
  }

  let response;
  try {
    response = await sendToContentWorker();
  } catch (firstError) {
    const firstMessage = String(firstError && firstError.message || firstError);
    postNative(makeEnvelope('provider.job.progress', {
      percent: 20,
      step: 'content_script_missing',
      hint: 'injecting_content_script',
      chunk: null,
      diagnostics: { tabId: tab.id, error: firstMessage }
    }, base));

    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: false },
        files: ['content.js']
      });
      await new Promise(resolve => setTimeout(resolve, 500));
      response = await sendToContentWorker();
    } catch (secondError) {
      response = {
        ok: false,
        error: String(secondError && secondError.message || secondError),
        code: 'CONTENT_SCRIPT_NOT_READY',
        diagnostics: { firstError: firstMessage }
      };
    }
  }

  if (!response || !response.ok) {
    return makeEnvelope('provider.job.error', {
      code: response && response.code || 'QWEN_WORKER_ERROR',
      message: response && response.error || 'Qwen content worker returned no response',
      recoverable: true,
      retry_recommended: false,
      details: { provider, response: response || null, tabId: tab.id }
    }, base);
  }

  const total = response.timings && response.timings.total_ms || null;
  return makeEnvelope('provider.job.done', {
    reply: response.reply || '',
    provider,
    agent_id: payload.agent_id || provider,
    session_id: response.session_id || `${provider}:direct`,
    diagnostics: {
      ...(response.diagnostics || {}),
      tabId: tab.id,
      url: response.url || tab.url || null,
      title: response.title || tab.title || null,
      transport: 'extension_native_worker_v414'
    },
    completion_signals: response.completion_signals || [ { finish_reason: 'stop', stop_sequence: null, truncated: false } ],
    timings: response.timings || { queued_ms: 0, network_ms: 0, processing_ms: total || 0, total_ms: total || 0 }
  }, base);
}

async function handleBusMessage(message) {
  const type = message && message.type;
  const id = message && message.id;

  if (type === 'bus.ping') {
    return makeEnvelope('bus.pong', {
      ok: true,
      received_id: id || null,
      extension: {
        version: VERSION,
        branch: BRANCH,
        connected: nativeConnected,
        lastSeenAt
      }
    }, { id: id || undefined, target: message.source || 'native_host', correlation_id: message.correlation_id || id || null, job_id: message.job_id || null });
  }

  if (type === 'extension.status.request') {
    return makeEnvelope('extension.status', {
      ok: true,
      version: VERSION,
      branch: BRANCH,
      nativeConnected,
      lastNativeError,
      lastSeenAt,
      mockJobs: mockJobs.size,
      processedEventIds: processedEventIds.size
    }, { id: id || undefined, target: message.source || 'native_host', correlation_id: message.correlation_id || null, job_id: message.job_id || null });
  }

  if (type === 'provider.job.run') {
    const provider = message && message.payload && message.payload.provider;
    if (provider === 'mock') return scheduleMockJob(message);
    if (provider === 'qwen' || provider === 'deepseek') return await runWebProviderJob(message, provider);
    return makeEnvelope('provider.job.error', {
      code: 'PROVIDER_NOT_IMPLEMENTED',
      message: `Provider is not implemented in Branch 4.0.17 MVP: ${provider}`,
      recoverable: false,
      retry_recommended: false,
      details: { provider }
    }, eventBaseFrom(message));
  }

  return makeEnvelope('bus.error', {
    ok: false,
    code: 'unknown_message_type',
    message: `Unknown Branch 4 message type: ${type}`,
    recoverable: false
  }, { id: id || undefined, target: message && message.source || 'native_host', correlation_id: message && message.correlation_id || null, job_id: message && message.job_id || null });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectNative();
  }, 3000);
}

function connectNative() {
  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
    nativeConnected = true;
    lastNativeError = '';
    lastSeenAt = Date.now();

    nativePort.onMessage.addListener(async (message) => {
      lastSeenAt = Date.now();
      try {
        const reply = await handleBusMessage(message);
        if (reply) postNative(reply);
      } catch (error) {
        postNative(makeEnvelope('bus.error', {
          ok: false,
          code: 'extension_handler_exception',
          message: String(error && error.stack || error),
          recoverable: true
        }, { id: message && message.id, target: message && message.source || 'native_host', correlation_id: message && message.correlation_id || null, job_id: message && message.job_id || null }));
      }
    });

    nativePort.onDisconnect.addListener(() => {
      nativeConnected = false;
      const err = chrome.runtime.lastError;
      lastNativeError = err ? err.message : 'native host disconnected';
      nativePort = null;
      scheduleReconnect();
    });

    postNative(makeEnvelope('extension.hello', {
      ok: true,
      extension_id: chrome.runtime.id,
      native_host: NATIVE_HOST,
      mock_provider: true
    }));
  } catch (error) {
    nativeConnected = false;
    lastNativeError = String(error && error.message || error);
    nativePort = null;
    scheduleReconnect();
  }
}

chrome.runtime.onInstalled.addListener(() => connectNative());
chrome.runtime.onStartup.addListener(() => connectNative());
connectNative();

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (!message || !message.type) return { ok: false, error: 'empty message' };

    if (message.type === 'AI_CHATTER_BRANCH4_STATUS') {
      return {
        ok: true,
        version: VERSION,
        branch: BRANCH,
        protocol: PROTOCOL,
        nativeConnected,
        lastNativeError,
        lastSeenAt,
        extensionId: chrome.runtime.id,
        mockJobs: mockJobs.size,
        processedEventIds: processedEventIds.size
      };
    }

    if (message.type === 'AI_CHATTER_BRANCH4_RECONNECT_NATIVE') {
      connectNative();
      return { ok: true, nativeConnected, lastNativeError };
    }

    return { ok: false, error: `unknown message: ${message.type}` };
  })().then(sendResponse).catch(error => sendResponse({ ok: false, error: String(error && error.stack || error) }));
  return true;
});
