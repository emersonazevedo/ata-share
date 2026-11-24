const app = document.getElementById("app");
const toastEl = document.getElementById("toast");
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pad = n => String(n).padStart(2, "0");
const hms = s => `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(Math.floor(s % 60))}`;
const fmtDate = v => { try { return new Date(v).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }); } catch { return v; } };
const api = async (path, opts) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.status === 204 ? null : r.json();
};
let toastT;
const toast = m => { toastEl.textContent = m; toastEl.hidden = false; clearTimeout(toastT); toastT = setTimeout(() => toastEl.hidden = true, 4000); };

let sse = null;
const closeSse = () => { if (sse) { sse.close(); sse = null; } };

// ---------- health ----------
async function refreshHealth() {
  const el = document.getElementById("health");
  try {
    const h = await api("/api/health");
    const o = h.ollama || {};
    const dev = h.devices || {};
    const parts = [
      `ASR <b>${esc(h.asr_model)}</b>`,
      o.up ? (o.configured_present ? `Ollama <b>${esc(o.configured)}</b>` : `Ollama <span class="bad">sem ${esc(o.configured)}</span>`) : `Ollama <span class="bad">offline</span>`,
      dev.available ? `áudio ok` : `<span class="bad">áudio indisponível</span>`,
    ];
    el.innerHTML = parts.join(" · ");
  } catch { el.innerHTML = `<span class="bad">servidor?</span>`; }
}

// ---------- home ----------
async function home() {
  closeSse();
  const meetings = await api("/api/meetings");
  app.innerHTML = `
    <div class="hero">
      <div><div class="eyebrow">SUAS REUNIÕES</div><h1>Registrado localmente.</h1>
        <div class="muted">Transcrição ao vivo com o modelo oficial. Ata em segundos após o Stop.</div></div>
      <button id="new" class="primary">＋ Nova reunião</button>
    </div>
    <div id="newform"></div>
    <div class="search"><input id="q" placeholder="Buscar em todas as reuniões…" autocomplete="off"></div>
    <div id="results"></div>
    <div class="list" id="list">
      ${meetings.length ? meetings.map(rowHtml).join("") : '<div class="muted card">Nenhuma reunião ainda.</div>'}
    </div>`;
  document.getElementById("new").onclick = showNewForm;
  document.getElementById("list").onclick = onListClick;
  const q = document.getElementById("q");
  let t;
  q.oninput = () => { clearTimeout(t); t = setTimeout(() => runSearch(q.value), 250); };
}

const rowHtml = m => `
  <div class="item" data-id="${esc(m.id)}">
    <div><div class="title">${esc(m.title)}</div><div class="meta">${esc(m.id.slice(0, 8))} · ${fmtDate(m.started_at)}</div></div>
    <div class="meta">${m.duration_seconds == null ? "—" : hms(m.duration_seconds)}</div>
    <span class="status ${m.status}">${m.status}</span>
    <button class="del" data-del="${esc(m.id)}" title="Excluir">×</button>
  </div>`;

async function onListClick(e) {
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    if (!confirm("Excluir esta reunião, áudios e transcrição?")) return;
    try { await api(`/api/meetings/${del.dataset.del}`, { method: "DELETE" }); home(); }
    catch (err) { toast(err.message); }
    return;
  }
  const item = e.target.closest(".item");
  if (item) location.hash = `#/m/${item.dataset.id}`;
}

async function runSearch(term) {
  const box = document.getElementById("results");
  if (!term.trim()) { box.innerHTML = ""; return; }
  const hits = await api(`/api/search?q=${encodeURIComponent(term)}`);
  box.innerHTML = hits.length
    ? `<div class="card"><h2>${hits.length} trecho(s)</h2>${hits.map(h => `
        <div class="line" data-goto="${esc(h.meeting_id)}" style="cursor:pointer">
          <div class="who">${esc(h.title)} · ${h.source === "system" ? "SYSTEM" : "ME"} · [${hms(h.start)}]</div>
          <p>${esc(h.snippet)}</p></div>`).join("")}</div>`
    : `<div class="card muted">Nada encontrado.</div>`;
  box.querySelectorAll("[data-goto]").forEach(el => el.onclick = () => location.hash = `#/m/${el.dataset.goto}`);
}

function deviceOptions(list) {
  return list.map(d => `<option value="${d.index}" ${d.is_default ? "selected" : ""}>${esc(d.name)}${d.is_default ? " (padrão)" : ""}</option>`).join("");
}

async function showNewForm() {
  const host = document.getElementById("newform");
  host.innerHTML = `<div class="card">
    <div class="row"><input id="t" placeholder="Título (opcional)" style="flex:1">
      <select id="lang" style="width:auto"><option value="pt">PT-BR</option><option value="en">EN-US</option><option value="auto">auto</option></select>
      <button id="go" class="primary">Iniciar</button></div>
    <div class="row" style="margin-top:8px">
      <div style="flex:1"><label class="muted" style="font-size:12px">Microfone</label>
        <select id="micdev"><option value="">carregando…</option></select></div>
      <div style="flex:1"><label class="muted" style="font-size:12px">Áudio do sistema</label>
        <select id="sysdev"><option value="">carregando…</option></select></div>
    </div>
    <div class="row" style="margin-top:8px">
      <label class="muted" style="font-size:12px"><input type="checkbox" id="hasproj"> Relacionada a um projeto?</label>
    </div>
    <div id="projfields" hidden class="row" style="margin-top:8px;flex-wrap:wrap">
      <input id="pf-ticket" placeholder="Ticket (opcional)" style="flex:1;min-width:120px">
      <input id="pf-project" placeholder="Projeto / demanda" style="flex:1;min-width:160px">
      <input id="pf-client" placeholder="Cliente (opcional)" style="flex:1;min-width:140px">
      <input id="pf-cloud" placeholder="Cloud (opcional)" style="flex:1;min-width:120px">
    </div></div>`;
  document.getElementById("t").focus();
  document.getElementById("hasproj").onchange = e => { document.getElementById("projfields").hidden = !e.target.checked; };

  (async () => {
    const micSel = document.getElementById("micdev");
    const sysSel = document.getElementById("sysdev");
    try {
      const dev = await api("/api/devices");
      if (!dev.available) throw new Error(dev.error || "indisponível");
      micSel.innerHTML = dev.mics.length ? deviceOptions(dev.mics) : `<option value="">nenhum encontrado</option>`;
      sysSel.innerHTML = dev.systems.length ? deviceOptions(dev.systems) : `<option value="">nenhum encontrado</option>`;
    } catch (err) {
      micSel.innerHTML = `<option value="">padrão do sistema</option>`;
      sysSel.innerHTML = `<option value="">padrão do sistema</option>`;
      toast("Não consegui listar dispositivos de áudio: " + err.message);
    }
  })();

  document.getElementById("go").onclick = async () => {
    try {
      const micVal = document.getElementById("micdev").value;
      const sysVal = document.getElementById("sysdev").value;
      const body = {
        title: document.getElementById("t").value,
        language: document.getElementById("lang").value,
        mic_device_index: micVal === "" ? null : Number(micVal),
        system_device_index: sysVal === "" ? null : Number(sysVal),
      };
      if (document.getElementById("hasproj").checked) {
        body.ticket = document.getElementById("pf-ticket").value.trim() || null;
        body.project_name = document.getElementById("pf-project").value.trim() || null;
        body.client = document.getElementById("pf-client").value.trim() || null;
        body.cloud = document.getElementById("pf-cloud").value.trim() || null;
      }
      const m = await api("/api/meetings", { method: "POST", body: JSON.stringify(body) });
      location.hash = `#/live/${m.id}`;
    } catch (err) { toast(err.message); }
  };
}

// ---------- live ----------
async function live(id) {
  closeSse();
  const finals = [];
  const interim = new Map();
  const started = Date.now();
  app.innerHTML = `
    <div class="hero"><div><div class="eyebrow">REUNIÃO ATIVA</div><h1 id="mt">…</h1></div></div>
    <div class="grid2">
      <div class="card">
        <div class="row" style="justify-content:space-between">
          <div class="rec"><span class="dot"></span> Gravando</div>
          <div class="timer" id="tm">00:00:00</div>
        </div>
        <div class="muted" style="margin:8px 0">Texto oficial (large-v3-turbo), ~20s atrás do tempo real.</div>
        <div class="transcript" id="box"><div class="muted">Aguardando fala…</div></div>
      </div>
      <div class="card">
        <h2>Controles</h2>
        <div id="limitbox" class="limitbox" hidden>
          <div><b>⏱ Limite de gravação</b> — encerra em <b id="limitleft">--</b>s</div>
          <button id="keep" class="primary">Continuar gravando</button>
        </div>
        <button id="stop" class="danger" style="border-color:var(--danger);color:var(--danger)">■ Parar reunião</button>
        <div class="steps" id="steps"></div>
      </div>
    </div>`;
  api(`/api/meetings/${id}`).then(d => { document.getElementById("mt").textContent = d.meeting.title; }).catch(() => {});
  const timer = setInterval(() => {
    const el = document.getElementById("tm"); if (el) el.textContent = hms((Date.now() - started) / 1000);
  }, 500);

  const render = () => {
    const box = document.getElementById("box"); if (!box) return;
    const items = [...finals, ...interim.values()].sort((a, b) => a.start - b.start);
    box.innerHTML = items.length ? items.map(s => `
      <div class="line ${s.final === false ? "interim" : ""}">
        <div class="who">[${hms(s.start)}] ${s.source === "system" ? "SYSTEM" : "ME"}</div>
        <p>${esc(s.text)}</p></div>`).join("") : '<div class="muted">Aguardando fala…</div>';
    box.scrollTop = box.scrollHeight;
  };
  const setSteps = (stage) => {
    const el = document.getElementById("steps"); if (!el) return;
    const mk = (k, label) => `<div class="step ${stage === k ? "current" : (order.indexOf(stage) > order.indexOf(k) ? "done" : "")}">${stage === k ? "⏳" : (order.indexOf(stage) > order.indexOf(k) ? "✓" : "○")} ${label}</div>`;
    const order = ["recording", "transcribing", "summarizing", "done"];
    el.innerHTML = mk("transcribing", "Drenando transcrição") + mk("summarizing", "Gerando ata") + mk("done", "Pronto");
  };

  let limitT = null;
  const hideLimit = () => { clearInterval(limitT); const b = document.getElementById("limitbox"); if (b) b.hidden = true; };
  const showLimit = deadline => {
    const box = document.getElementById("limitbox"); if (!box) return;
    const tick = () => {
      const left = Math.max(0, Math.ceil(deadline - Date.now() / 1000));
      const el = document.getElementById("limitleft"); if (el) el.textContent = left;
      if (left <= 0) clearInterval(limitT);
    };
    clearInterval(limitT); tick(); limitT = setInterval(tick, 250); box.hidden = false;
  };
  document.getElementById("keep").onclick = async () => {
    try { await api(`/api/meetings/${id}/cancel-auto-stop`, { method: "POST" }); hideLimit(); toast("Corte automático cancelado — gravação continua."); }
    catch (err) { toast(err.message); }
  };

  document.getElementById("stop").onclick = async () => {
    const b = document.getElementById("stop"); b.disabled = true; b.textContent = "Finalizando…";
    try { await api(`/api/meetings/${id}/stop`, { method: "POST" }); setSteps("transcribing"); }
    catch (err) { toast(err.message); b.disabled = false; }
  };

  const seen = new Set();  // evita duplicar se o SSE reconectar e repetir o histórico
  sse = new EventSource(`/api/meetings/${id}/events`);
  sse.onmessage = ev => {
    let e;
    try { e = JSON.parse(ev.data); } catch { return; }
    if (e.type === "segment") {
      if (e.final === false) { interim.set(e.source, e); }
      else {
        const key = `${e.source}|${e.start}|${e.text}`;
        if (!seen.has(key)) { seen.add(key); finals.push(e); }
        interim.delete(e.source);
      }
      render();
    } else if (e.type === "warn") toast(e.message);
    else if (e.type === "limit_warning") showLimit(e.deadline);
    else if (e.type === "limit_cancelled") hideLimit();
    else if (e.type === "limit_reached") { hideLimit(); toast(`Limite de ${e.minutes} min atingido — encerrando a gravação.`); }
    else if (e.type === "transcript_ready") {
      // transcrição já disponível — vai pro detalhe; a ata segue em 2º plano
      clearInterval(timer); hideLimit(); closeSse();
      if (e.dropped) toast(`${e.dropped} janela(s) descartada(s) por falha do ASR.`);
      location.hash = `#/m/${id}`;
    } else if (e.type === "summary_progress") {
      const el = document.getElementById("steps");
      if (el && e.stage === "map") el.querySelector(".step.current")?.replaceChildren(document.createTextNode(`⏳ Gerando ata (${e.done}/${e.total} janelas)`));
    } else if (e.type === "status") {
      setSteps(e.status);
      if (e.status === "done" || e.status === "failed") { clearInterval(timer); closeSse(); location.hash = `#/m/${id}`; }
    }
  };
  sse.onerror = () => {};
}

// ---------- detail ----------
const PROCESSING = ["recording", "transcribing", "summarizing"];

async function detail(id) {
  closeSse();
  const fetchAndRender = async () => {
    const d = await api(`/api/meetings/${id}`);
    renderDetail(d);
    return d.meeting.status;
  };
  const status = await fetchAndRender();
  if (!PROCESSING.includes(status)) return;

  // UMA conexão SSE, sem replay e sem reabrir (o loop infinito vinha daqui).
  sse = new EventSource(`/api/meetings/${id}/events?replay=0`);
  let busy = false;
  sse.onmessage = async ev => {
    let e;
    try { e = JSON.parse(ev.data); } catch { return; }
    const tag = document.getElementById("proctag");
    if (e.type === "summary_progress" && tag) {
      tag.textContent = e.stage === "reduce" ? "consolidando ata…"
        : `gerando ata (${e.done || 0}/${e.total || "?"} janelas)`;
    } else if (e.type === "asr_progress" && tag && e.fraction != null) {
      tag.textContent = `transcrevendo ${Math.round(e.fraction * 100)}%`;
    } else if (e.type === "status") {
      if (["done", "failed"].includes(e.status)) { closeSse(); await fetchAndRender(); }
      else if (!busy) { busy = true; try { await fetchAndRender(); } finally { busy = false; } }
    }
  };
  sse.onerror = () => {};   // o navegador reconecta sozinho; não fazemos nada aqui
}

function renderDetail(d) {
  const m = d.meeting, s = d.summary || {};
  const seg = d.segments || [];
  const A = arr => (arr && arr.length) ? `<ul class="clean">${arr.map(x => `<li>${esc(typeof x === "string" ? x : (x.task || ""))}${x && x.owner ? ` <span class="kv">— ${esc(x.owner)}${x.deadline ? " — " + esc(x.deadline) : ""}</span>` : ""}</li>`).join("")}</ul>` : `<div class="muted">—</div>`;
  app.innerHTML = `
    <div class="hero">
      <div><div class="eyebrow">${esc(m.status.toUpperCase())}</div><h1>${esc(m.title)}</h1>
        <div class="muted">${fmtDate(m.started_at)} · ${hms(m.duration_seconds || 0)}</div></div>
      <div class="row">
        <button id="reproc">Reprocessar (beam 5)</button>
        <button id="folder">Abrir pasta</button>
        <button id="exp" class="primary">Exportar</button>
        <button id="expObs">Exportar p/ Obsidian</button>
      </div>
    </div>
    ${PROCESSING.includes(m.status)
      ? `<div class="card"><span class="dot"></span> Processando… <span id="proctag" class="muted">preparando</span></div>` : ""}
    ${m.obsidian_export_status === "failed"
      ? `<div class="card" style="border-color:var(--warn)">⚠ Falha ao exportar para o Obsidian — ${esc(m.obsidian_export_error || "erro desconhecido")}. Corrija a configuração e use “Exportar p/ Obsidian”.</div>` : ""}
    <div class="tabbar"><span class="tab active" data-t="ata">Ata</span><span class="tab" data-t="tr">Transcrição</span><span class="tab" data-t="pj">Projeto</span><span class="tab" data-t="nt">Notas</span></div>
    <div id="pane"></div>`;

  const dg = s._diagnostics || {};
  const ataNotice = () => {
    const parts = [];
    if (dg.status === "partial")
      parts.push(`<div class="card" style="border-color:var(--warn)">⚠ Ata parcial — ${esc(dg.reason || "cobre só parte da reunião")}. Rode “Reprocessar”.</div>`);
    else if (dg.status && !["ok", "skipped"].includes(dg.status))
      parts.push(`<div class="card muted">Ata indisponível: ${esc(dg.reason || "sem conteúdo")}. Tente “Reprocessar”.</div>`);
    if (dg.model_fallback)
      parts.push(`<div class="card muted">Gerada com <b>${esc(dg.model_fallback.used)}</b> (o modelo configurado <b>${esc(dg.model_fallback.requested)}</b> não está instalado).</div>`);
    return parts.join("");
  };
  const panes = {
    ata: () => ataNotice() + ataBody(s, A),
    tr: () => `<div class="transcript">${seg.length ? seg.map(x => `<div class="line"><div class="who">[${hms(x.start)}] ${x.source === "system" ? "SYSTEM" : "ME"}</div><p>${esc(x.text)}</p></div>`).join("") : '<div class="muted">Sem transcrição.</div>'}</div>`,
    pj: () => `<div class="card">
      <div class="muted" style="margin-bottom:8px">Vínculo opcional com um projeto/demanda — usado na exportação para o Obsidian.</div>
      <div class="row" style="flex-wrap:wrap;gap:8px">
        <input id="pj-ticket" placeholder="Ticket" value="${esc(m.ticket || "")}" style="flex:1;min-width:120px">
        <input id="pj-project" placeholder="Projeto / demanda" value="${esc(m.project_name || "")}" style="flex:1;min-width:160px">
        <input id="pj-client" placeholder="Cliente" value="${esc(m.client || "")}" style="flex:1;min-width:140px">
        <input id="pj-cloud" placeholder="Cloud" value="${esc(m.cloud || "")}" style="flex:1;min-width:120px">
      </div>
      <div class="row" style="margin-top:8px"><button id="savepj" class="primary">Salvar</button></div>
    </div>`,
    nt: () => `<div class="card"><textarea id="nt">${esc(d.notes || "")}</textarea><div class="row" style="margin-top:8px"><button id="savent" class="primary">Salvar notas</button></div></div>`,
  };
  const pane = document.getElementById("pane");
  const show = t => {
    document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x.dataset.t === t));
    pane.innerHTML = panes[t]();
    if (t === "nt") document.getElementById("savent").onclick = async () => {
      try { await api(`/api/meetings/${m.id}/notes`, { method: "PUT", body: JSON.stringify({ text: document.getElementById("nt").value }) }); toast("Notas salvas"); }
      catch (err) { toast(err.message); }
    };
    if (t === "pj") document.getElementById("savepj").onclick = async () => {
      try {
        await api(`/api/meetings/${m.id}/project`, {
          method: "PUT", body: JSON.stringify({
            ticket: document.getElementById("pj-ticket").value.trim(),
            project_name: document.getElementById("pj-project").value.trim(),
            client: document.getElementById("pj-client").value.trim(),
            cloud: document.getElementById("pj-cloud").value.trim(),
          })
        });
        toast("Projeto salvo");
      } catch (err) { toast(err.message); }
    };
  };
  document.querySelectorAll(".tab").forEach(x => x.onclick = () => show(x.dataset.t));
  show("ata");
  document.getElementById("exp").onclick = async () => {
    try { const r = await api(`/api/meetings/${m.id}/export`, { method: "POST" }); toast("Salvo em " + r.folder); }
    catch (err) { toast(err.message); }
  };
  document.getElementById("expObs").onclick = async () => {
    try { const r = await api(`/api/meetings/${m.id}/export-obsidian`, { method: "POST" }); toast("Exportado para " + r.path); detail(m.id); }
    catch (err) { toast(err.message); }
  };
  document.getElementById("folder").onclick = async () => {
    try { const r = await api(`/api/meetings/${m.id}/open-folder`, { method: "POST" }); toast("Abrindo " + r.folder); }
    catch (err) { toast(err.message); }
  };
  document.getElementById("reproc").onclick = async () => {
    if (!confirm("Reprocessar do WAV com beam 5? Mais lento, mais preciso.")) return;
    try { await api(`/api/meetings/${m.id}/reprocess`, { method: "POST", body: JSON.stringify({ beam_size: 5 }) }); toast("Reprocessando…"); detail(m.id); }
    catch (err) { toast(err.message); }
  };
}

const ataBody = (s, A) => {
  const agenda = (s.agenda_items && s.agenda_items.length)
    ? s.agenda_items.map(it => `<div class="agenda-item"><h3>${esc(it.topic || "")}</h3><p>${esc(it.discussion || "")}</p></div>`).join("")
    : `<div class="muted">—</div>`;
  return `
  <div class="card"><h2>Resumo executivo</h2><p>${esc(s.summary || "—")}</p></div>
  <div class="card"><h2>Itens da Agenda</h2>${agenda}</div>
  <div class="grid2">
    <div class="card"><h2>Itens de Ação</h2>${A(s.action_items)}</div>
    <div class="card"><h2>Próximos Passos</h2><p>${esc(s.next_steps || "—")}</p></div>
  </div>
  <div class="card"><h2>Outras Observações</h2>${A(s.other_notes)}</div>`;
};

// ---------- router ----------
function route() {
  const h = location.hash || "#/";
  const mLive = h.match(/^#\/live\/([a-f0-9]+)/i);
  const mDet = h.match(/^#\/m\/([a-f0-9]+)/i);
  if (mLive) return live(mLive[1]).catch(err => toast(err.message));
  if (mDet) return detail(mDet[1]).catch(err => toast(err.message));
  return home().catch(err => { app.innerHTML = `<div class="card">Erro: ${esc(err.message)}</div>`; });
}
document.getElementById("brand").onclick = () => location.hash = "#/";
window.addEventListener("hashchange", route);
refreshHealth();
setInterval(refreshHealth, 15000);
route();
