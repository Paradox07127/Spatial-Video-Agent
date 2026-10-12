// POST /api/agent/ask as an SSE stream (CONTRACT §7). handlers: {step, tool_call, tool_result, hits, model_result, ui, claim,
// answer, status, vss_native, done, error}; on site, vss_native arrives after status. Frames whose run_id differs from the stream's first frame, or that arrive after abort(), are dropped.
import { api } from './api.js';

// One SSE block -> {event, data, id}; data lines join with "\n" and lose one leading space each (WHATWG SSE).
export function sseFrame(block) {
  let event = 'message', id = null;
  const data = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('id:')) id = Number(line.slice(3).trim());
    else if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) data.push(line.slice(line.startsWith('data: ') ? 6 : 5));
  }
  return { event, data: data.join('\n'), id };
}

// A throwing frame handler is an app bug, not a broken connection: report it and keep reading.
const deliver = (fn) => { try { fn(); } catch (e) { reportError(e); } };

export function askSSE(body, handlers, options = {}) {
  const ctl = new AbortController();
  let runId = null;
  let finalStatus = false;
  const dispatch = (event, data, id) => {
    if (ctl.signal.aborted) return;
    runId ??= data.run_id;
    if (data.run_id !== runId) return;
    if (event === 'status' && data.final != null) finalStatus = true;
    deliver(() => { handlers[event]?.(data); if (Number.isSafeInteger(id)) options.sequence?.(id); });
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
    options.headers?.(r.headers);
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value.replace(/\r/g, '');
      let k;
      while ((k = buf.indexOf('\n\n')) >= 0) {
        const { event, data, id } = sseFrame(buf.slice(0, k));
        buf = buf.slice(k + 2);
        if (data) dispatch(event, JSON.parse(data), id);
      }
    }
    // Final status completes the answer; site comparison frames can follow it, and dev fixtures may omit done.
    if (!ctl.signal.aborted && !finalStatus) throw new Error('Agent stream ended before a final status. Please retry.');
  })().catch((e) => {
    if (ctl.signal.aborted) return;
    console.error(e);
    if (finalStatus) handlers.comparison_error?.(e);
    else if (!options.disconnect?.(e)) handlers.error?.(e);
  });
  return { abort: () => ctl.abort() };
}


export function jobSSE(jobId, afterSeq, handlers, options = {}) {
  const ctl = new AbortController();
  (async () => {
    const response = await fetch(api.url(`api/agent/jobs/${encodeURIComponent(jobId)}/events?after_seq=${afterSeq}`), { headers:{accept:'text/event-stream'},signal:ctl.signal });
    if(!response.ok)throw new Error(`Job subscription failed (${response.status})`);
    const reader=response.body.pipeThrough(new TextDecoderStream()).getReader();let buffer='',finished=false;
    for(;;){const {value,done}=await reader.read();if(done)break;buffer+=value.replace(/\r/g,'');let end;
      while((end=buffer.indexOf('\n\n'))>=0){const {event,data,id:seq}=sseFrame(buffer.slice(0,end));buffer=buffer.slice(end+2);
        if(!data||ctl.signal.aborted)continue;
        const parsed=JSON.parse(data);deliver(()=>{handlers[event]?.(parsed);if(Number.isSafeInteger(seq))options.sequence?.(seq);});
        if(event==='done'){finished=true;deliver(()=>options.complete?.());}
      }
    }
    if(!finished&&!ctl.signal.aborted)throw new Error('Job subscription ended before completion.');
  })().catch(e=>{if(!ctl.signal.aborted)options.disconnect?.(e);});
  return {abort:()=>ctl.abort()};
}
