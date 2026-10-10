// POST /api/agent/ask as an SSE stream (CONTRACT §7). handlers: {step, tool_call, tool_result, hits, model_result, ui, claim,
// answer, status, vss_native, done, error}; on site, vss_native arrives after status. Frames whose run_id differs from the stream's first frame, or that arrive after abort(), are dropped.
import { api } from './api.js';

export function askSSE(body, handlers) {
  const ctl = new AbortController();
  let runId = null;
  let finalStatus = false;
  const dispatch = (event, data) => {
    if (ctl.signal.aborted) return;
    runId ??= data.run_id;
    if (data.run_id !== runId) return;
    if (event === 'status' && data.final != null) finalStatus = true;
    handlers[event]?.(data);
  };
  (async () => {
    const r = await fetch(api.url('api/agent/ask'), {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal: ctl.signal,
    });
    if (!r.ok) {
      let detail;
      try { detail = (await r.json()).detail; } catch { /* A proxy can return plain text. */ }
      if (r.status === 422) {
        const messages = Array.isArray(detail) ? detail.map((item) => {
          const field = (item.loc ?? []).filter((part) => part !== 'body').join('.');
          return `${field ? field + ': ' : ''}${item.msg ?? 'Invalid value'}`;
        }).join('; ') : typeof detail === 'string' ? detail : 'Check the question and selected context.';
        throw new Error(`The investigation request is invalid. ${messages}`);
      }
      throw new Error(typeof detail === 'string' ? detail : `Could not start the investigation (${r.status}). Please retry.`);
    }
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value.replace(/\r/g, '');
      let k;
      while ((k = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, k);
        buf = buf.slice(k + 2);
        let event = 'message', data = '';
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (data) dispatch(event, JSON.parse(data));
      }
    }
    // Final status completes the answer; site comparison frames can follow it, and dev fixtures may omit done.
    if (!ctl.signal.aborted && !finalStatus) throw new Error('Agent stream ended before a final status. Please retry.');
  })().catch((e) => {
    if (ctl.signal.aborted) return;
    console.error(e); // a throwing frame handler lands here too; the stack is the only trace of it
    if (finalStatus) handlers.comparison_error?.(e);
    else handlers.error?.(e);
  });
  return { abort: () => ctl.abort() };
}
