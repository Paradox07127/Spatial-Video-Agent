const { test, expect } = require('@playwright/test');
const { ASKS } = require('./mockapi');

const frame = (event, data = {}, run_id = 'r_current') => ({ event, data: { run_id, ...data } });
const encode = (frames) => frames.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');

test.beforeEach(async ({ page }) => {
  // Import the real SSE module without starting the shell or issuing any application API requests.
  await page.route('**/sse-functional.html', (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>SSE regression</title>' }));
  await page.goto('sse-functional.html');
});

async function replay(page, text, { abort = false, reject = false } = {}) {
  return page.evaluate(async ({ text, abort, reject }) => {
    const { askSSE } = await import(new URL('kernel/sse.js', document.baseURI).href);
    const events = [], errors = [], requests = [];
    let controller;
    const originalFetch = window.fetch;
    window.fetch = async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body) });
      if (reject) throw new DOMException('Request aborted', 'AbortError');
      return new Response(new ReadableStream({ start(c) { controller = c; } }), { headers: { 'content-type': 'text/event-stream' } });
    };
    try {
      const handlers = Object.fromEntries(['step', 'status', 'vss_native', 'done', 'answer'].map((event) => [event, (data) => events.push({ event, data })]));
      handlers.error = (error) => errors.push(error.message);
      const stream = askSSE({ q: 'Mocked query', context: { workspace: 'warehouse' } }, handlers);
      if (abort) stream.abort();
      if (controller) {
        // Split every frame across read boundaries and use CRLF, as a network stream may do.
        const bytes = new TextEncoder().encode(text.replace(/\n/g, '\r\n'));
        for (let i = 0; i < bytes.length; i += 17) controller.enqueue(bytes.slice(i, i + 17));
        controller.close();
      }
      // The finite mocked stream is consumed in microtasks before the next rendering turns.
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      return { events, errors, requests };
    } finally {
      window.fetch = originalFetch;
    }
  }, { text, abort, reject });
}

test('EOF after a step reports an incomplete answer', async ({ page }) => {
  const result = await replay(page, encode([frame('step', { state: 'parse' })]));
  expect(result.events.map((f) => f.event)).toEqual(['step']);
  expect(result.errors).toEqual(['Agent stream ended before a final status. Please retry.']);
});

test('empty EOF reports an incomplete answer', async ({ page }) => {
  expect((await replay(page, '')).errors).toEqual(['Agent stream ended before a final status. Please retry.']);
});

test('status without a final verdict cannot certify EOF completion', async ({ page }) => {
  expect((await replay(page, encode([frame('status', { cached: false })]))).errors).toHaveLength(1);
});

test('done without final status still reports an incomplete answer', async ({ page }) => {
  expect((await replay(page, encode([frame('step'), frame('done')]))).errors).toHaveLength(1);
});

test('a truncated final status cannot certify EOF completion', async ({ page }) => {
  const text = encode([frame('step')]) + encode([frame('status', { final: 'SUPPORTED' })]).trimEnd();
  const result = await replay(page, text);
  expect(result.events.map((f) => f.event)).toEqual(['step']);
  expect(result.errors).toHaveLength(1);
});

test('final status permits EOF without a done frame', async ({ page }) => {
  const result = await replay(page, encode([frame('status', { final: 'UNRESOLVED' })]));
  expect(result.events.map((f) => f.event)).toEqual(['status']);
  expect(result.errors).toEqual([]);
});

test('site comparison and done remain deliverable after final status', async ({ page }) => {
  const frames = [frame('step'), frame('status', { final: 'SUPPORTED' }), frame('vss_native', { status: 'ok', hits: [] }), frame('done')];
  const result = await replay(page, encode(frames));
  expect(result.events).toEqual(frames);
  expect(result.errors).toEqual([]);
  expect(result.requests[0].url).toBe(new URL('api/agent/ask', page.url()).href);
});

test('another run status and done cannot certify completion', async ({ page }) => {
  const result = await replay(page, encode([frame('step'), frame('status', { final: 'SUPPORTED' }, 'r_stale'), frame('done', {}, 'r_stale')]));
  expect(result.events.map((f) => f.event)).toEqual(['step']);
  expect(result.errors).toHaveLength(1);
});

test('abort drops later frames and suppresses the EOF error', async ({ page }) => {
  const result = await replay(page, encode([frame('step'), frame('status', { final: 'SUPPORTED' }), frame('done')]), { abort: true });
  expect(result.events).toEqual([]);
  expect(result.errors).toEqual([]);
});

test('aborted fetch rejection does not report an error', async ({ page }) => {
  const result = await replay(page, '', { abort: true, reject: true });
  expect(result.events).toEqual([]);
  expect(result.errors).toEqual([]);
});

test('existing dev and site fixtures still complete without errors', async ({ page }) => {
  for (const askId of ['wh-main', 'wh-refuse', 'wh-vss']) {
    const source = ASKS.find((a) => a.ask_id === askId).events;
    const result = await replay(page, encode(source));
    expect(result.errors, askId).toEqual([]);
    expect(result.events.some((f) => f.event === 'status'), askId).toBe(true);
  }
});
