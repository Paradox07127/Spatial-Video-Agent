// Durable investigation navigation. A browser subscription is disposable; the server owns jobs.
import { api } from './api.js';
import { esc } from './format.js';
import { jobSSE } from './sse.js';
const KEY = 'sva.investigation';
const terminal = (status) => ['done', 'error', 'failed', 'cancelled', 'canceled', 'aborted'].includes(status);
export function createInvestigations({ restore, attach, reset }) {
  const host = document.getElementById('investigations'), banner = document.getElementById('investigation-status');
  const support = api.get('health').then(h=>({enabled:h.agent_protocols?.includes(2) === true}), error=>({error}));
  async function supported() {
    const result = await support;
    if (result.error) throw new Error(`Could not check persistent investigation support: ${result.error.message}`);
    return result.enabled;
  }
  let current = null, generation = 0, retry = null, opened = false, selectedSession = null;
  try { current = JSON.parse(localStorage.getItem(KEY) ?? 'null'); } catch { /* Browser storage is optional. */ }
  const save = () => { try { current ? localStorage.setItem(KEY, JSON.stringify(current)) : localStorage.removeItem(KEY); } catch { /* Private browsing. */ } };
  const show = (text) => { banner.textContent = text; banner.hidden = !text; };
  const cancelRetry = () => { generation++; clearTimeout(retry); retry = null; };
  async function session(id) { return api.get(`api/agent/sessions/${encodeURIComponent(id)}`); }
  async function refresh() {
    try {
      const { sessions } = await api.get('api/agent/sessions');
      const selected = selectedSession ?? current?.session_id;
      const detail = selected ? await session(selected) : null;
      if (detail?.session_id === current?.session_id) { current.revision = detail.revision; save(); }
      host.innerHTML = `<header><b>Investigations</b><button type="button" data-new>New investigation</button><button type="button" data-close aria-label="Close investigations">Close</button></header>
        <p class="muted">Observations and answers persist on the server. Reopen a turn to continue its investigation.</p>
        <div class="investigation-columns"><nav aria-label="Saved investigations">${sessions.map(s=>`<button type="button" data-session="${esc(s.session_id)}" ${s.session_id===selected?'aria-current="true"':''}>${esc(s.title || s.session_id)}<small>${esc(s.updated ?? s.created ?? '')}</small></button>`).join('') || '<p>No investigations yet.</p>'}</nav>
        <ol aria-label="Investigation turns">${(detail?.turns ?? []).map(t=>`<li><button type="button" data-turn="${esc(t.turn_id)}">${esc(t.q)}<small>${esc(t.status ?? '')}${t.final ? ' · '+esc(t.final) : ''}</small></button></li>`).join('') || '<li>Ask a question to begin.</li>'}</ol></div>`;
    } catch (e) { host.innerHTML = `<p role="alert">Could not load investigations: ${esc(e.message)}</p><button type="button" data-refresh>Retry</button><button type="button" data-close>Close</button>`; }
  }
  function open() { opened = true; selectedSession = current?.session_id ?? null; host.hidden = false; refresh().then(()=>host.querySelector('button')?.focus()); }
  function close() { opened = false; host.hidden = true; document.getElementById('investigations-btn').focus(); }
  document.getElementById('investigations-btn').onclick = open;
  host.onclick = async (e) => {
    const button = e.target.closest('button'); if (!button) return;
    try {
      if (button.hasAttribute('data-close')) return close();
      if (button.hasAttribute('data-refresh')) return refresh();
      if (button.hasAttribute('data-new')) { cancelRetry(); current = null; save(); reset(); show('New investigation · ask a question to begin.'); close(); return; }
      if (button.dataset.session) { selectedSession = button.dataset.session; return refresh(); }
      if (button.dataset.turn) {
        const detail = await session(selectedSession ?? current.session_id), turn = detail.turns.find(t=>t.turn_id===button.dataset.turn);
        if (!turn?.job_id) throw new Error('This turn has no replayable job.');
        current = {session_id:detail.session_id, revision:detail.revision,turn_id:turn.turn_id,job_id:turn.job_id};save();close();
        await resume({ restart:true });
      }
    } catch (e) { show(`Investigation could not open: ${e.message}`); }
  };
  document.addEventListener('keydown',e=>{if(opened && e.key==='Tab'){const buttons=[...host.querySelectorAll('button:not([disabled])')];const first=buttons[0],last=buttons.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}if(opened && e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();close();}},true);
  async function prepare(q, follow) {
    cancelRetry(); const token = generation;
    try {
      if (!(await supported())) { show('Persistent investigations unavailable · legacy connection.'); return {}; }
      if (!current?.session_id) {
        const created = await api.post('api/agent/sessions',{title:q.slice(0,100)});
        if(token!==generation)throw new Error('Superseded investigation request');
        current = {session_id:created.session_id,revision:created.revision}; save();
      } else { const detail = await session(current.session_id); if(token!==generation)throw new Error('Superseded investigation request'); current.revision=detail.revision;save(); }
      show(`Saved investigation · ${current.session_id}`);
      return { protocol_version:2,session_id:current.session_id,parent_turn_id:follow?current.turn_id??null:null,expected_revision:current.revision,request_id:crypto.randomUUID() };
    } catch (e) {
      show(`Could not save the investigation: ${e.message}`); throw e;
    }
  }
  function headers(h) {
    const session_id=h.get('X-SVA-Session'),turn_id=h.get('X-SVA-Turn'),job_id=h.get('X-SVA-Job');
    if (!session_id || !turn_id || !job_id) return;
    current = {...current,session_id,turn_id,job_id,last_seq:0};save();show(`Saved investigation · ${session_id}`);
  }
  function sequence(seq) { if(current && Number.isSafeInteger(seq)){current.last_seq=seq;save();} }
  async function complete() {
    if(current?.session_id) { const id=current.session_id; try {const detail=await session(id);if(current?.session_id!==id)return;current.revision=detail.revision;save();if(opened)await refresh();}catch(e){show(`Answer complete; history refresh failed: ${e.message}`);} }
  }
  async function resume({ restart=false }={}) {
    if(!current?.job_id)return;
    clearTimeout(retry);retry=null;const token=++generation, saved={...current};
    try {
      if(!(await supported())){show('Saved investigation requires a server with persistent investigations enabled.');return;}
      const snapshot=await api.get(`api/agent/jobs/${encodeURIComponent(saved.job_id)}`);
      if(token!==generation)return;
      const detail=await session(saved.session_id),turn=detail.turns.find(t=>t.turn_id===saved.turn_id);
      if(!turn)throw new Error('Saved turn is unavailable.');
      const reset=restart || current.attempt!==snapshot.attempt;
      current={...current,revision:detail.revision,attempt:snapshot.attempt};
      if(reset)current.last_seq=Math.max(0,(snapshot.current_attempt_start_seq??1)-1);
      save();
      const handlers=await restore(turn,{reset,session:detail,snapshot});
      if(token!==generation)return;
      show(terminal(snapshot.status)?`Saved investigation · ${detail.session_id}`:'Reconnected to the running investigation.');
      const active=jobSSE(saved.job_id,current.last_seq??0,handlers,{
        sequence,
        disconnect:e=>{if(token!==generation)return;if(terminal(snapshot.status)){show(`Investigation ${snapshot.status}${snapshot.error ? ': '+(snapshot.error.message ?? snapshot.error) : ''}`);handlers.job_terminal?.(snapshot);complete();return;}show(`Connection interrupted; investigation continues. Reconnecting… ${e?.message??''}`);retry=setTimeout(()=>resume(),1500);},
        complete:()=>complete(),
      });
      attach(active); return active;
    }catch(e){if(token!==generation)return;show(`Investigation connection unavailable; retrying… ${e.message}`);retry=setTimeout(()=>resume({restart}),3000);}
  }
  function disconnected(e) { if(!current?.job_id)return false;show(`Connection interrupted; investigation continues. Reconnecting… ${e.message}`);retry=setTimeout(()=>resume(),1500);return true; }
  async function stop() {
    cancelRetry();if(!current?.job_id)return false;
    await api.post(`api/agent/jobs/${encodeURIComponent(current.job_id)}/cancel`,{});show('Investigation cancellation requested.');return true;
  }
  return {prepare,headers,sequence,complete,resume,disconnected,stop,detach:cancelRetry,current:()=>current,open};
}
