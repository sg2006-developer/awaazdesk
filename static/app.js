import {convertRecording} from './audio.mjs';
import {LiveSession} from './live.mjs';

// ------------------------------------------------------------------ helpers
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const statusName = value => ({needs_human: 'Needs human', open: 'Open', resolved: 'Resolved'}[value] || value);
const date = text => new Date(text).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'});
const LANGUAGES = {'en-IN': 'English', 'hi-IN': 'Hindi', 'bn-IN': 'Bengali', 'gu-IN': 'Gujarati', 'kn-IN': 'Kannada',
  'ml-IN': 'Malayalam', 'mr-IN': 'Marathi', 'pa-IN': 'Punjabi', 'ta-IN': 'Tamil', 'te-IN': 'Telugu'};
const languageName = code => LANGUAGES[code] || code || '';
const PREVIEW = {
  'en-IN': 'Hello! Thank you for calling {name}. How can I help you today?',
  'hi-IN': 'नमस्ते! {name} में कॉल करने के लिए धन्यवाद। मैं आपकी क्या मदद कर सकती हूँ?',
  'te-IN': 'నమస్కారం! {name}కి కాల్ చేసినందుకు ధన్యవాదాలు. నేను మీకు ఎలా సహాయం చేయగలను?',
  'ta-IN': 'வணக்கம்! {name}-ஐ அழைத்ததற்கு நன்றி. நான் உங்களுக்கு எப்படி உதவ முடியும்?',
  'kn-IN': 'ನಮಸ್ಕಾರ! {name}ಗೆ ಕರೆ ಮಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಲಿ?',
  'ml-IN': 'നമസ്കാരം! {name}-ലേക്ക് വിളിച്ചതിന് നന്ദി. ഞാൻ എങ്ങനെ സഹായിക്കണം?',
  'mr-IN': 'नमस्कार! {name} ला कॉल केल्याबद्दल धन्यवाद. मी तुमची कशी मदत करू?',
  'bn-IN': 'নমস্কার! {name}-এ ফোন করার জন্য ধন্যবাদ। আমি কীভাবে সাহায্য করতে পারি?',
  'gu-IN': 'નમસ્તે! {name} ને કૉલ કરવા બદલ આભાર. હું તમારી કેવી રીતે મદદ કરી શકું?',
  'pa-IN': 'ਸਤ ਸ੍ਰੀ ਅਕਾਲ! {name} ਨੂੰ ਕਾਲ ਕਰਨ ਲਈ ਧੰਨਵਾਦ। ਮੈਂ ਤੁਹਾਡੀ ਕਿਵੇਂ ਮਦਦ ਕਰ ਸਕਦੀ ਹਾਂ?',
};
const DEFAULT_VOICE = {model: 'timbre-v3.0', voice: 'Shivani', speed: 1.0};

let config, revision, versions = [], calls = [], health, voices;
let currentCall = null, live = null, busy = false, recording = null, speechURL = null, noticeTimer;
const turnMetrics = JSON.parse(sessionStorage.getItem('awaazdesk-metrics') || '{}');  // callId -> [metrics]

async function api(path, options = {}) {
  const response = await fetch('/api' + path, {headers: {'Content-Type': 'application/json'}, ...options});
  const data = await response.json();
  if (response.status === 401 && data.access_required) { showAccessGate(); throw Error(data.detail); }
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : 'Some fields are invalid. Check the form and try again.');
  return data;
}

function showAccessGate() {
  $('#access-gate').hidden = false;
  $('#access-code').focus();
}

$('#access-form').onsubmit = async event => {
  event.preventDefault();
  const button = $('#access-form button');
  button.disabled = true;
  $('#access-error').hidden = true;
  try {
    const response = await fetch('/api/access', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code: $('#access-code').value})});
    if (!response.ok) throw Error((await response.json()).detail);
    location.reload();
  } catch (error) {
    Object.assign($('#access-error'), {textContent: error.message, hidden: false});
  } finally {
    button.disabled = false;
  }
};

function notify(text, error = false) {
  clearTimeout(noticeTimer);
  Object.assign($('#notice'), {textContent: text, className: error ? 'error' : '', hidden: false});
  noticeTimer = setTimeout(() => { $('#notice').hidden = true; }, 14000);
}

async function action(work) {
  try { return await work(); } catch (error) { notify(error.message, true); }
}

function navigate(name) {
  $$('.view').forEach(el => { el.hidden = el.id !== name; });
  $$('nav button').forEach(el => el.classList.toggle('active', el.dataset.view === name));
  $('#page-title').textContent = {overview: 'Overview', studio: 'Agent studio', call: 'Live call', tickets: 'Conversations'}[name];
  window.scrollTo(0, 0);
}
$$('[data-view]').forEach(b => { b.onclick = () => navigate(b.dataset.view); });
$$('[data-go]').forEach(b => { b.onclick = () => navigate(b.dataset.go); });

// ------------------------------------------------------------------ studio
function fillForm() {
  for (const field of ['name', 'description', 'greeting', 'rules']) $('#' + field).value = config[field];
  $('#required-fields').value = config.required_fields.join(', ');
  $('#keyterms').value = (config.keyterms || []).join(', ');
  const voice = {...DEFAULT_VOICE, ...(config.voice || {})};
  $('#voice-model').value = voice.model;
  $('#voice-name').value = voice.voice;
  $('#voice-speed').value = voice.speed;
  syncVoiceFields();
  $('#categories').innerHTML = config.categories.map((c, i) => `
    <div class="category-row" data-index="${i}">
      <div class="row"><input aria-label="Category name ${i + 1}" class="cat-name" value="${esc(c.name)}" maxlength="80"><button class="remove remove-category" aria-label="Remove category ${i + 1}">×</button></div>
      <label>Keywords (also help speech recognition)</label><input class="cat-keywords" aria-label="Keywords ${i + 1}" value="${esc(c.keywords)}">
      <label class="check-label"><input class="cat-escalate" type="checkbox" ${c.escalate ? 'checked' : ''}> Always require human follow-up</label>
    </div>`).join('');
  $('#documents').innerHTML = config.documents.map((d, i) => `
    <div class="doc-row" data-index="${i}">
      <div class="row"><input class="doc-title" aria-label="Document title ${i + 1}" value="${esc(d.title)}"><button class="remove remove-document" aria-label="Remove document ${i + 1}">×</button></div>
      <textarea class="doc-text" aria-label="Document text ${i + 1}" rows="5">${esc(d.text)}</textarea>
    </div>`).join('');
  $$('.remove-category').forEach(b => { b.onclick = () => { readForm(); config.categories.splice(Number(b.closest('[data-index]').dataset.index), 1); fillForm(); }; });
  $$('.remove-document').forEach(b => { b.onclick = () => { readForm(); config.documents.splice(Number(b.closest('[data-index]').dataset.index), 1); fillForm(); }; });
}

function readForm() {
  for (const field of ['name', 'description', 'greeting', 'rules']) config[field] = $('#' + field).value.trim();
  config.required_fields = $('#required-fields').value.split(',').map(x => x.trim()).filter(Boolean);
  config.keyterms = $('#keyterms').value.split(',').map(x => x.trim()).filter(Boolean);
  config.voice = readVoice();
  config.categories = $$('.category-row').map(r => ({name: r.querySelector('.cat-name').value.trim(), keywords: r.querySelector('.cat-keywords').value, escalate: r.querySelector('.cat-escalate').checked}));
  config.documents = $$('.doc-row').map(r => ({title: r.querySelector('.doc-title').value.trim(), text: r.querySelector('.doc-text').value.trim()}));
}

function readVoice() {
  return {model: $('#voice-model').value, voice: $('#voice-name').value || DEFAULT_VOICE.voice, speed: Number($('#voice-speed').value)};
}

function syncVoiceFields() {
  const v25 = $('#voice-model').value === 'timbre-v2.5';
  $('#voice-name-field').hidden = v25;
  $('#voice-v25-note').hidden = !v25;
  $('#speed-value').textContent = Number($('#voice-speed').value).toFixed(2) + '×';
}
$('#voice-model').onchange = syncVoiceFields;
$('#voice-speed').oninput = syncVoiceFields;

function fillVoiceOptions() {
  $('#voice-name').innerHTML = Object.entries(voices['timbre-v3.0']).map(([name, about]) => `<option value="${esc(name)}">${esc(name)} — ${esc(about)}</option>`).join('');
  $('#preview-language').innerHTML = Object.entries(LANGUAGES).map(([code, name]) => `<option value="${code}">${name}</option>`).join('');
}

$('#preview-voice').onclick = () => action(async () => {
  const language = $('#preview-language').value;
  const name = $('#name').value.trim() || 'our company';
  await playText(PREVIEW[language].replace('{name}', name), language, readVoice());
});

function renderVersions() {
  const latest = versions[0]?.id;
  $('#published-badge').textContent = latest ? `Published · v${latest}` : 'Unpublished';
  $('#check-published').textContent = latest ? `Version ${latest} is ready for new calls` : 'No published configuration yet';
  $('#versions').innerHTML = versions.length
    ? versions.map(v => `<div class="version-row"><div><strong>Version ${v.id}</strong><small>${esc(date(v.created_at))}</small></div><button data-restore="${v.id}">Restore draft</button></div>`).join('')
    : '<p class="helper">Publish your first version to begin.</p>';
  $$('[data-restore]').forEach(b => {
    b.onclick = () => action(async () => {
      const data = await api('/versions/' + b.dataset.restore);
      config = data.config;
      fillForm();
      notify(`Version ${data.id} loaded into the editor. Save or publish to apply it.`);
    });
  });
}

async function refreshBusiness() {
  const data = await api('/business');
  ({config, revision, versions} = data);
  $('#business-name').textContent = config.name;
  fillForm();
  renderVersions();
}

async function saveDraft(publish = false) {
  $('#save-draft').disabled = $('#publish').disabled = true;
  try {
    readForm();
    const result = await api('/business', {method: 'PUT', body: JSON.stringify({config, revision})});
    revision = result.revision;
    if (publish) {
      const published = await api('/business/publish', {method: 'POST', body: JSON.stringify({revision})});
      notify(`Version ${published.version} published. New calls will use this configuration.`);
    } else {
      notify('Draft saved. Your published agent has not changed.');
    }
    await refreshBusiness();
  } finally {
    $('#save-draft').disabled = $('#publish').disabled = false;
  }
}
$('#save-draft').onclick = () => action(() => saveDraft());
$('#publish').onclick = () => action(() => saveDraft(true));
$('#add-category').onclick = () => { readForm(); config.categories.push({name: '', keywords: '', escalate: false}); fillForm(); };
$('#add-document').onclick = () => { readForm(); config.documents.push({title: '', text: ''}); fillForm(); };
$('#document-file').onchange = () => action(async () => {
  const file = $('#document-file').files[0];
  if (!file) return;
  if (file.size > 20000) throw Error('Import a text document smaller than 20 KB.');
  readForm();
  config.documents.push({title: file.name, text: await file.text()});
  fillForm();
  $('#document-file').value = '';
  notify('Document imported into the editor. Save and publish to make it available.');
});
$('#propose').onclick = () => action(async () => {
  const text = $('#requirements').value.trim();
  if (!text) throw Error('Describe your business first.');
  $('#propose').disabled = true;
  try {
    const voice = readVoice();
    const data = await api('/business/propose', {method: 'POST', body: JSON.stringify({text})});
    config = {...data.config, voice};
    fillForm();
    $('#questions').textContent = data.questions.length ? 'Still to clarify: ' + data.questions.join(' • ') : 'Review all suggested requirements before publishing.';
    notify('AI proposal ready to review. It has not been saved or published.');
  } finally {
    $('#propose').disabled = false;
  }
});

// ------------------------------------------------------------------ conversations
function callLanguages(call) {
  return [...new Set(call.messages.filter(m => m.role === 'user' && m.language).map(m => m.language).concat(call.ticket.language ? [call.ticket.language] : []))];
}

function rows(items) {
  if (!items.length) return '<div class="empty"><div class="empty-icon">▤</div><h3>No conversations yet</h3><p>Your first test call will appear here.</p></div>';
  return items.map(c => `
    <div class="list-row">
      <div><strong>${esc(c.ticket.category)}</strong><small>${esc(c.ticket.summary.slice(0, 90))}</small></div>
      <span><span class="pill ${c.ticket.status === 'needs_human' ? 'warn' : ''}">${esc(statusName(c.ticket.status))}</span> ${callLanguages(c).map(l => `<span class="pill subtle">${esc(languageName(l))}</span>`).join(' ')}</span>
      <small>v${c.version} · ${esc(date(c.created_at))}</small>
      <button class="text-button" data-inspect="${esc(c.id)}">View call →</button>
    </div>`).join('');
}

function wireInspection() {
  $$('[data-inspect]').forEach(b => {
    b.onclick = () => action(async () => {
      const call = await api('/calls/' + b.dataset.inspect);
      navigate('tickets');
      $('#ticket-detail').hidden = false;
      $('#ticket-detail').innerHTML = `<div class="section-title"><h2>Conversation ${esc(call.id.slice(0, 8))}</h2><span class="pill">Policy v${call.version}</span></div>${ticketHTML(call.ticket)}<h3>Transcript</h3>${messageHTML(call.messages, false, call.id)}`;
      $('#ticket-detail').scrollIntoView({behavior: 'smooth'});
    });
  });
}

function renderTickets() {
  const term = $('#search-tickets').value.toLowerCase();
  const status = $('#filter-status').value;
  const language = $('#filter-language').value;
  const filtered = calls.filter(c => (!status || c.ticket.status === status) && (!language || callLanguages(c).includes(language)) && JSON.stringify(c).toLowerCase().includes(term));
  $('#ticket-list').innerHTML = rows(filtered);
  $('#recent-list').innerHTML = rows(calls.slice(0, 4));
  wireInspection();
}

function renderStats() {
  $('#stat-total').textContent = calls.length;
  $('#ticket-count').textContent = calls.length;
  $('#stat-human').textContent = calls.filter(c => c.ticket.status === 'needs_human').length;
  const languages = [...new Set(calls.flatMap(callLanguages))];
  $('#stat-languages').textContent = languages.length;
  $('#stat-language-list').textContent = languages.length ? languages.map(languageName).join(' · ') : 'Detected by Gnani Prisma';
  const selected = $('#filter-language').value;
  $('#filter-language').innerHTML = '<option value="">All languages</option>' + languages.map(l => `<option value="${esc(l)}">${esc(languageName(l))}</option>`).join('');
  $('#filter-language').value = languages.includes(selected) ? selected : '';
  const responses = Object.values(turnMetrics).flat().map(m => m.response_ms).filter(Number.isFinite);
  $('#stat-latency').textContent = responses.length ? (responses.reduce((a, b) => a + b, 0) / responses.length / 1000).toFixed(1) + 's' : '—';
}

async function refreshCalls() {
  calls = await api('/calls');
  renderStats();
  renderTickets();
}
$('#search-tickets').oninput = renderTickets;
$('#filter-status').onchange = renderTickets;
$('#filter-language').onchange = renderTickets;
$('#refresh-tickets').onclick = () => action(refreshCalls);
$('#export-tickets').onclick = () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(calls, null, 2)], {type: 'application/json'}));
  const a = document.createElement('a');
  a.href = url;
  a.download = 'awaazdesk-conversations.json';
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

function ticketHTML(t) {
  const fields = Object.entries(t.fields);
  const engine = t.engine === 'hosted-llm' ? `Hosted AI reasoning · ${esc(t.model || '')}` : t.engine === 'limited-local' ? 'Limited local lookup · no LLM' : 'Awaiting first message';
  return `
    <div class="ticket-field"><small>Category</small><strong>${esc(t.category)}</strong></div>
    <div class="ticket-field"><small>Status</small><span class="pill ${t.status === 'needs_human' ? 'warn' : ''}">${esc(statusName(t.status))}</span>${t.language ? ` <span class="pill subtle">${esc(languageName(t.language))}</span>` : ''}</div>
    <div class="ticket-field"><small>Summary</small><p>${esc(t.summary)}</p></div>
    <div class="ticket-field"><small>Collected details</small>${fields.length ? fields.map(([k, v]) => `<p>${esc(k.replaceAll('_', ' '))}: <strong>${esc(v)}</strong></p>`).join('') : '<p>Nothing collected yet</p>'}</div>
    <div class="ticket-field"><small>Evidence used</small>${t.sources.length ? t.sources.map(s => `<details><summary>${esc(s.title)}</summary><p>${esc(s.text)}</p></details>`).join('') : '<p>No knowledge passage cited</p>'}</div>
    <p class="helper">${engine}</p>`;
}

function chips(message, index, callId) {
  const parts = [];
  if (message.language) parts.push(languageName(message.language));
  if (message.role === 'user' && message.meta?.stt_ms != null) parts.push(`Prisma ${message.meta.stt_ms} ms`);
  if (message.role === 'assistant') {
    const metric = (turnMetrics[callId] || []).find(m => m.index === index);
    if (metric?.response_ms != null) parts.push(`voice in ${(metric.response_ms / 1000).toFixed(1)} s`);
    else if (message.meta?.llm_ms != null) parts.push(`reasoning ${(message.meta.llm_ms / 1000).toFixed(1)} s`);
  }
  return parts.length ? `<span class="chips">${parts.map(p => `<i>${esc(p)}</i>`).join('')}</span>` : '';
}

function messageHTML(messages, listen = true, callId = currentCall?.id) {
  // No whitespace between tags: .message uses white-space: pre-wrap.
  return messages.map((m, i) => `<div class="message ${m.role === 'user' ? 'user' : ''}">`
    + `<small>${m.role === 'user' ? 'Customer' : 'AwaazDesk'} ${chips(m, i, callId)}</small>`
    + `<div>${esc(m.content)}</div>`
    + (listen && m.role === 'assistant' && !live ? `<button data-speak="${i}">▷ Listen</button>` : '')
    + '</div>').join('');
}

// ------------------------------------------------------------------ call
function callControls() {
  const active = currentCall && !currentCall.ended;
  const textOnly = !live;
  $('#call-text').disabled = !active || busy;
  $('#send-turn').disabled = !active || busy || !!recording;
  $('#record-call').hidden = !!live;
  $('#record-call').disabled = !active || busy || !health?.speech_configured;
  $('#end-call').disabled = !active || !!recording;
  const canResume = active && !live && $('#voice-mode').checked && health?.speech_configured;
  $('#start-call').disabled = busy || !!recording || !!live || (active && !canResume);
  $('#start-call').textContent = canResume ? 'Go live 🎙' : 'Start new call ↗';
  $('#auto-speak-label').hidden = !textOnly;
  $('#barge-in-label').hidden = textOnly;
  $('#live-bar').hidden = !live;
  $('#composer-label').textContent = live ? 'Or type a message' : 'Your message';
}

function renderCall() {
  $('#call-title').textContent = 'Support conversation';
  $('#call-version').textContent = `Policy v${currentCall.version}`;
  $('#call-state').textContent = currentCall.ended ? 'Call ended · ticket saved' : live ? 'Live · hands-free voice call' : 'Connected · text call';
  $('#messages').innerHTML = messageHTML(currentCall.messages);
  $('#messages').scrollTop = $('#messages').scrollHeight;
  $('#live-ticket').innerHTML = ticketHTML(currentCall.ticket);
  $$('[data-speak]').forEach(b => {
    b.onclick = () => action(() => {
      const message = currentCall.messages[Number(b.dataset.speak)];
      return playText(message.spoken || message.content, message.language || 'auto');
    });
  });
  renderLatency();
  callControls();
}

function renderLatency() {
  const metrics = (turnMetrics[currentCall?.id] || []).filter(m => m.llm_ms != null);
  const last = metrics.at(-1);
  if (!last) return;
  const bar = (label, ms, cls) => ms == null ? '' : `<div class="latency-row"><span>${label}</span><b class="${cls}" style="width:${Math.min(100, ms / 40)}%"></b><em>${(ms / 1000).toFixed(2)} s</em></div>`;
  const avg = metrics.map(m => m.response_ms).filter(Number.isFinite);
  $('#latency').innerHTML = `
    ${bar('Prisma STT', last.stt_ms, 'stt')}${bar('Reasoning', last.llm_ms, 'llm')}${bar('Timbre first audio', last.tts_ms, 'tts')}
    <div class="latency-total"><span>You stopped → agent spoke</span><strong>${last.response_ms != null ? (last.response_ms / 1000).toFixed(1) + ' s' : '—'}</strong></div>
    ${avg.length > 1 ? `<p class="helper">Average over ${avg.length} turns: ${(avg.reduce((a, b) => a + b, 0) / avg.length / 1000).toFixed(1)} s</p>` : ''}`;
}

async function playText(text, language, voice) {
  const player = $('#speech-player');
  player.pause();
  const response = await fetch('/api/tts', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text, language, ...(voice ? {voice} : {})})});
  if (!response.ok) {
    const data = await response.json();
    throw Error(`Speech playback: ${data.detail} The text reply is saved.`);
  }
  if (speechURL) URL.revokeObjectURL(speechURL);
  speechURL = URL.createObjectURL(await response.blob());
  player.src = speechURL;
  player.hidden = false;
  try { await player.play(); } catch { notify('Audio is ready. Press Play below the message box.'); }
}

function setLiveStatus(text, state = '') {
  $('#live-status').textContent = text;
  $('#live-status').className = 'live-status ' + state;
}

function onLiveEvent(event) {
  switch (event.type) {
    case 'ready':
      setLiveStatus(event.voice ? 'Listening…' : 'Voice unavailable — type below', event.voice ? 'listening' : '');
      break;
    case 'level':
      $('#mic-meter').style.width = Math.min(100, event.mic * 400) + '%';
      $('#agent-meter').style.width = Math.min(100, event.agent * 400) + '%';
      break;
    case 'speech_start':
      setLiveStatus('Hearing you…', 'hearing');
      break;
    case 'partial':
      $('#caption').hidden = false;
      $('#caption').textContent = event.text;
      break;
    case 'final':
      $('#caption').hidden = false;
      $('#caption').innerHTML = `${esc(event.text)} <span class="pill subtle">${esc(languageName(event.language))}</span>`;
      break;
    case 'thinking':
      setLiveStatus('Thinking…', 'thinking');
      break;
    case 'call':
      currentCall = event.call;
      $('#caption').hidden = true;
      renderCall();
      action(refreshCalls);
      break;
    case 'audio_start':
      setLiveStatus('Speaking…', 'speaking');
      break;
    case 'metrics': {
      if (event.llm_ms == null) break;  // The greeting has no reasoning step.
      const list = turnMetrics[currentCall.id] ||= [];
      list.push({...event, index: currentCall.messages.length - 1});
      sessionStorage.setItem('awaazdesk-metrics', JSON.stringify(turnMetrics));
      renderCall();
      renderStats();
      break;
    }
    case 'audio_end':
    case 'barge_in':
      setLiveStatus('Listening…', 'listening');
      break;
    case 'error':
      notify(event.message, true);
      break;
    case 'ended':
      currentCall = event.call;
      stopLive();
      renderCall();
      action(refreshCalls);
      notify('Call ended. Its transcript and ticket are saved.');
      break;
    case 'closed':
      if (live) { stopLive(); notify(event.reason || 'The live connection closed. Reopen the call to continue.', true); }
      break;
  }
}

function stopLive() {
  if (!live) return;
  const session = live;
  live = null;
  session.close();
  $('#caption').hidden = true;
  callControls();
}

async function goLive() {
  live = new LiveSession(currentCall.id, onLiveEvent);
  live.bargeIn = $('#barge-in').checked;
  callControls();
  try {
    await live.start();
  } catch (error) {
    stopLive();
    throw Error(`${error.message} Continuing as a text call.`);
  }
  renderCall();
}

$('#start-call').onclick = () => action(async () => {
  if (currentCall && !currentCall.ended && !live) return goLive();  // Resume a restored call hands-free.
  busy = true;
  callControls();
  try {
    currentCall = await api('/calls', {method: 'POST'});
    sessionStorage.setItem('awaazdesk-call', currentCall.id);
    renderCall();
    await refreshCalls();
    if ($('#voice-mode').checked && health.speech_configured) {
      busy = false;
      await goLive();
    } else if ($('#auto-speak').checked && health.speech_configured) {
      await playText(currentCall.messages[0].content, 'auto');
    }
  } finally {
    busy = false;
    callControls();
  }
});

async function sendTurn() {
  const text = $('#call-text').value.trim();
  if (!text || !currentCall || busy) return;
  if (live) {
    live.sendText(text);
    $('#call-text').value = '';
    return;
  }
  busy = true;
  callControls();
  $('#call-state').textContent = 'Considering your request…';
  try {
    currentCall = await api(`/calls/${currentCall.id}/turn`, {method: 'POST', body: JSON.stringify({text, revision: currentCall.revision})});
    $('#call-text').value = '';
    renderCall();
    await refreshCalls();
    const reply = currentCall.messages.at(-1);
    if ($('#auto-speak').checked && health.speech_configured) await playText(reply.spoken || reply.content, reply.language || 'auto');
  } finally {
    busy = false;
    callControls();
    if (currentCall && !live) $('#call-state').textContent = currentCall.ended ? 'Call ended' : 'Connected · text call';
  }
}
$('#send-turn').onclick = () => action(sendTurn);
$('#call-text').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); action(sendTurn); } };
$('#end-call').onclick = () => action(async () => {
  if (live) { live.end(); return; }
  currentCall = await api(`/calls/${currentCall.id}/end`, {method: 'POST'});
  $('#speech-player').pause();
  renderCall();
  await refreshCalls();
  notify('Call ended. Its transcript and ticket are saved.');
});
$('#mute').onclick = () => {
  if (!live) return;
  live.setMuted(!live.muted);
  $('#mute').textContent = live.muted ? 'Unmute' : 'Mute';
  $('#mute').classList.toggle('active', live.muted);
};
$('#barge-in').onchange = () => live?.setBargeIn($('#barge-in').checked);
$('#voice-mode').onchange = callControls;
$$('[data-scenario]').forEach(b => {
  b.onclick = () => {
    $('#call-text').value = b.dataset.scenario;
    if (!currentCall || currentCall.ended) notify('Start a new call, then say or send this example.');
  };
});

// ------------------------------------------------------------------ push-to-talk (studio + text calls)
async function toggleRecording(target, button) {
  if (recording) {
    if (recording.button !== button) throw Error('Finish the active recording first.');
    recording.recorder.stop();
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia) throw Error('Microphone access requires localhost or HTTPS in a supported browser.');
  $('#speech-player').pause();
  const stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true}});
  let recorder;
  try { recorder = new MediaRecorder(stream); } catch (error) { stream.getTracks().forEach(t => t.stop()); throw error; }
  const chunks = [], original = button.textContent;
  const limit = (health?.max_recording_seconds || 30) - 1;
  recording = {recorder, button};
  button.textContent = '■ Stop recording';
  button.classList.add('recording');
  callControls();
  const timer = setTimeout(() => { if (recorder.state === 'recording') recorder.stop(); }, limit * 1000);
  recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  recorder.onerror = () => notify('The microphone recorder failed. Please try again.', true);
  recorder.onstop = () => action(async () => {
    clearTimeout(timer);
    stream.getTracks().forEach(t => t.stop());
    button.classList.remove('recording');
    button.textContent = 'Transcribing…';
    button.disabled = true;
    busy = true;
    callControls();
    try {
      const wav = await convertRecording(new Blob(chunks, {type: recorder.mimeType}));
      const query = target === '#call-text' && currentCall ? `?call_id=${encodeURIComponent(currentCall.id)}` : '';
      const result = await api('/stt' + query, {method: 'POST', headers: {'Content-Type': 'audio/wav'}, body: wav});
      $(target).value = result.text;
      notify(`Transcribed with Gnani Prisma${result.language ? ` (${languageName(result.language)})` : ''}. Review the text, then send it.`);
    } finally {
      recording = null;
      busy = false;
      button.disabled = false;
      button.textContent = original;
      callControls();
    }
  });
  try {
    recorder.start();
  } catch (error) {
    clearTimeout(timer);
    stream.getTracks().forEach(t => t.stop());
    recording = null;
    button.textContent = original;
    button.classList.remove('recording');
    callControls();
    throw error;
  }
}
$('#record-call').onclick = () => action(() => toggleRecording('#call-text', $('#record-call')));
$('#record-requirements').onclick = () => action(() => toggleRecording('#requirements', $('#record-requirements')));

// ------------------------------------------------------------------ boot
action(async () => {
  const access = await (await fetch('/api/access')).json();
  if (access.required && !access.granted) { showAccessGate(); return; }
  [health, voices] = await Promise.all([api('/health'), api('/voices')]);
  fillVoiceOptions();
  await Promise.all([refreshBusiness(), refreshCalls()]);
  if (health.reasoning !== 'hosted-llm') {
    const banner = $('#provider-banner');
    banner.hidden = false;
    banner.textContent = 'Reasoning setup needed: add your hosted model settings to awaazdesk/.env and restart. You can edit and publish manually now. Test calls use a limited local knowledge lookup until then; they do not understand arbitrary business rules.';
  }
  if (!health.speech_configured) {
    $('#voice-mode').checked = false;
    $('#voice-mode').disabled = true;
    $('#preview-voice').disabled = true;
  }
  $('#record-requirements').disabled = !health.speech_configured;
  $('#propose').disabled = health.reasoning !== 'hosted-llm';
  const saved = sessionStorage.getItem('awaazdesk-call');
  if (saved) {
    try { currentCall = await api('/calls/' + saved); renderCall(); } catch { sessionStorage.removeItem('awaazdesk-call'); }
  }
  callControls();
});
