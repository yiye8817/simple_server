(() => {
  let requestSeq = 0;
  const state = { page: 1, pageSize: 40, q: '', topic: '', cefr: '', pages: 0, accent: localStorage.getItem('wordAtlasAccent') || 'uk', style: localStorage.getItem('wordAtlasStyle') || 'atlas', detail: null };
  const $ = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const debounce = (fn, delay = 220) => { let timer; return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); }; };
  function setAccent(accent) { state.accent = accent === 'us' ? 'us' : 'uk'; localStorage.setItem('wordAtlasAccent', state.accent); document.querySelectorAll('.accent').forEach(b => b.classList.toggle('active', b.dataset.accent === state.accent)); if (state.detail) renderDetail(state.detail); }
  function speakWord(entry) { const url = entry.assets?.audio?.[state.accent]; if (url) { const audio = new Audio(url); audio.onerror = () => { window.alert('单词音频加载失败，改用浏览器朗读'); speak(entry.word); }; audio.play().catch(() => { window.alert('单词音频无法播放，改用浏览器朗读'); speak(entry.word); }); } else speak(entry.word); }
  async function playWordById(id) { try { const response = await fetch(`/api/words/${encodeURIComponent(id)}`); const data = await response.json(); if (!response.ok) throw new Error(data.error || '词条不存在'); speakWord(data.item); } catch { speak(id); } }
  function speak(text) { if (!('speechSynthesis' in window)) { window.alert('当前浏览器不支持例句朗读'); return; } window.speechSynthesis.cancel(); const utterance = new SpeechSynthesisUtterance(text); utterance.lang = state.accent === 'uk' ? 'en-GB' : 'en-US'; const voices = window.speechSynthesis.getVoices(); const exact = voices.find(v => v.lang.toLowerCase() === utterance.lang.toLowerCase()); if (exact) utterance.voice = exact; window.speechSynthesis.speak(utterance); }
  async function loadWords() { const seq = ++requestSeq; const params = new URLSearchParams({ page: state.page, page_size: state.pageSize }); if (state.q) params.set('q', state.q); if (state.topic) params.set('topic', state.topic); if (state.cefr) params.set('cefr', state.cefr); $('status').textContent = '正在加载…'; try { const response = await fetch(`/api/words?${params}`); const data = await response.json(); if (seq !== requestSeq) return; if (!response.ok) throw new Error(data.error || '加载失败'); state.pages = data.pages; renderGrid(data.items); $('status').textContent = `${data.total.toLocaleString()} 个词条`; $('page-info').textContent = data.pages ? `${data.page} / ${data.pages}` : '暂无结果'; $('prev').disabled = state.page <= 1; $('next').disabled = !data.pages || state.page >= data.pages; } catch (error) { if (seq !== requestSeq) return; $('word-grid').innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`; $('status').textContent = '加载失败'; } }
  function renderGrid(items) { if (!items.length) { $('word-grid').innerHTML = '<p class="empty">没有找到匹配的词条</p>'; return; } $('word-grid').innerHTML = items.map(entry => `<article class="word-card" data-word-id="${escapeHtml(entry.id)}"><div class="word-card-top"><h3>${escapeHtml(entry.word)}</h3><button class="play" type="button" data-play="${escapeHtml(entry.id)}" aria-label="播放单词">▶</button></div><div class="ipa">${escapeHtml(state.accent === 'uk' ? entry.uk_ipa : entry.us_ipa)}</div><div class="zh">${escapeHtml(entry.zh)}</div><div class="meta"><span class="tag">${escapeHtml(entry.cefr || '—')}</span><span>${escapeHtml(entry.topic || 'general')}</span></div></article>`).join(''); }
  async function openDetail(id, updateHash = true) { $('detail').hidden = false; $('detail-backdrop').hidden = false; $('detail').setAttribute('aria-hidden', 'false'); $('detail-content').innerHTML = '<p>正在读取词条…</p>'; if (updateHash) history.replaceState(null, '', `#/atlas?w=${encodeURIComponent(id)}`); try { const response = await fetch(`/api/words/${encodeURIComponent(id)}`); const data = await response.json(); if (!response.ok) throw new Error(data.error || '词条不存在'); state.detail = data.item; renderDetail(state.detail); } catch (error) { $('detail-content').innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`; } }
  function renderDetail(entry) { const image = entry.assets?.images?.[state.style] || entry.assets?.images?.atlas; $('detail-content').innerHTML = `<div class="detail-hero"><img src="${escapeHtml(image)}" alt="${escapeHtml(entry.word)} 插画" onerror="this.onerror=null;this.removeAttribute('src');this.alt='图片加载失败';this.classList.add('asset-error')"><div><div class="eyebrow">${escapeHtml(entry.cefr)} · ${escapeHtml(entry.topic)}</div><h2>${escapeHtml(entry.word)}</h2><div class="ipa">UK ${escapeHtml(entry.uk_ipa)} · US ${escapeHtml(entry.us_ipa)} <button class="play" data-detail-play type="button" aria-label="播放单词">▶</button></div><div class="detail-zh">${escapeHtml(entry.zh)}</div><div class="style-switch"><button data-style="atlas" class="${state.style === 'atlas' ? 'active' : ''}">atlas 画风</button><button data-style="classic" class="${state.style === 'classic' ? 'active' : ''}">classic 画风</button></div></div></div><section class="section"><h3>释义 Senses</h3>${(entry.senses || []).map(s => `<div class="sense"><span class="pos">${escapeHtml(s.pos)}</span><b>${escapeHtml(s.zh)}</b><span class="en">${escapeHtml(s.en)}</span></div>`).join('')}</section><section class="section"><h3>例句 Examples · ${state.accent.toUpperCase()}</h3>${(entry.examples || []).map(s => `<div class="sentence"><div>${escapeHtml(s.en)}</div><button class="play" data-speak="${escapeHtml(s.en)}" type="button" aria-label="朗读例句">▶</button><div class="zh">${escapeHtml(s.zh)}</div></div>`).join('')}</section>${entry.tip ? `<section class="section"><h3>学习提示 Tip</h3><p>${escapeHtml(entry.tip)}</p></section>` : ''}<section class="section"><h3>搭配 & 词族</h3><div class="chips">${[...(entry.collocations || []), ...(entry.family || [])].map(x => `<span class="chip">${escapeHtml(x)}</span>`).join('') || '<span class="zh">暂无</span>'}</div></section><section class="section"><h3>近义词 Synonyms</h3><div class="chips">${(entry.synonyms || []).map(x => `<span class="chip">${escapeHtml(x)}</span>`).join('') || '<span class="zh">暂无</span>'}</div><h3>反义词 Antonyms</h3><div class="chips">${(entry.antonyms || []).map(x => `<span class="chip">${escapeHtml(x)}</span>`).join('') || '<span class="zh">暂无</span>'}</div></section>`; }
  function closeDetail() { state.detail = null; $('detail').hidden = true; $('detail-backdrop').hidden = true; $('detail').setAttribute('aria-hidden', 'true'); const current = location.hash; if (current.includes('?w=')) history.replaceState(null, '', '#/atlas'); }
  function readHash() { const match = location.hash.match(/[?&]w=([^&]+)/); if (match) { try { openDetail(decodeURIComponent(match[1]), false); } catch { $('detail-content').innerHTML = '<p class="error">词条链接无效</p>'; } } }
  $('search').addEventListener('input', debounce(e => { state.q = e.target.value.trim(); state.page = 1; loadWords(); })); $('topic').addEventListener('input', debounce(e => { state.topic = e.target.value.trim(); state.page = 1; loadWords(); })); $('cefr').addEventListener('change', e => { state.cefr = e.target.value; state.page = 1; loadWords(); }); $('clear').addEventListener('click', () => { state.q = state.topic = state.cefr = ''; state.page = 1; $('search').value = ''; $('topic').value = ''; $('cefr').value = ''; loadWords(); }); $('prev').addEventListener('click', () => { if (state.page > 1) { state.page--; loadWords(); } }); $('next').addEventListener('click', () => { if (state.page < state.pages) { state.page++; loadWords(); } }); document.querySelectorAll('.accent').forEach(b => b.addEventListener('click', () => setAccent(b.dataset.accent))); $('detail-close').addEventListener('click', closeDetail); $('detail-backdrop').addEventListener('click', closeDetail); window.addEventListener('hashchange', readHash); document.addEventListener('click', e => { const card = e.target.closest('.word-card'); const play = e.target.closest('[data-play]'); if (play) { e.stopPropagation(); playWordById(play.dataset.play); return; } if (card) openDetail(card.dataset.wordId); const detailPlay = e.target.closest('[data-detail-play]'); if (detailPlay && state.detail) { e.stopPropagation(); speakWord(state.detail); } const sentence = e.target.closest('[data-speak]'); if (sentence) { e.stopPropagation(); speak(sentence.dataset.speak); } const style = e.target.closest('[data-style]'); if (style && state.detail) { state.style = style.dataset.style; localStorage.setItem('wordAtlasStyle', state.style); renderDetail(state.detail); } });

  const tokenKey = 'devControlToken';
  const managementToken = () => localStorage.getItem(tokenKey) || '';
  const authHeaders = (headers = {}) => {
    const token = managementToken();
    return token ? Object.assign({ Authorization: `Bearer ${token}` }, headers) : headers;
  };
  const responsePayload = async (response) => {
    const type = response.headers.get('content-type') || '';
    if (type.includes('json')) return response.json();
    return { error: await response.text() };
  };
  const showManagementResult = (id, response, data) => {
    const value = { status: response.status, ok: response.ok, data };
    $(id).textContent = JSON.stringify(value, null, 2);
  };
  const showDebugResult = (url, response, data) => {
    $('debug-result').textContent = JSON.stringify({ request: url, status: response.status, ok: response.ok, response: data }, null, 2);
  };
  async function createManagedWord(event) {
    event.preventDefault();
    let payload;
    try { payload = JSON.parse($('create-json').value); } catch (error) { $('create-result').textContent = JSON.stringify({ error: 'JSON 格式无效' }, null, 2); return; }
    try {
      const response = await fetch('/api/words', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(payload) });
      showManagementResult('create-result', response, await responsePayload(response));
    } catch (error) { $('create-result').textContent = JSON.stringify({ error: error.message }, null, 2); }
  }
  async function importManagedWords(event) {
    event.preventDefault();
    const form = new FormData();
    const file = $('import-file').files[0];
    if (file) form.append('file', file, file.name);
    else if ($('import-text').value.trim()) {
      const text = $('import-text').value.trim();
      let filename = 'words.json';
      try { JSON.parse(text); } catch (error) { filename = 'words.jsonl'; }
      form.append('file', new Blob([text], { type: 'application/json' }), filename);
    }
    else { $('import-result').textContent = JSON.stringify({ error: '请选择文件或粘贴 JSON/JSONL 内容' }, null, 2); return; }
    const query = $('import-replace').checked ? '?replace=true' : '';
    try {
      const response = await fetch(`/api/words/import${query}`, { method: 'POST', headers: authHeaders(), body: form });
      showManagementResult('import-result', response, await responsePayload(response));
      if (response.ok) loadWords();
    } catch (error) { $('import-result').textContent = JSON.stringify({ error: error.message }, null, 2); }
  }
  async function debugWord(event) {
    event.preventDefault();
    const id = $('debug-word-id').value.trim();
    if (!id) return;
    const url = `/api/words/${encodeURIComponent(id)}`;
    try { const response = await fetch(url); showDebugResult(url, response, await responsePayload(response)); }
    catch (error) { $('debug-result').textContent = JSON.stringify({ request: url, error: error.message }, null, 2); }
  }
  async function syncManagedAssets(event) {
    event.preventDefault();
    const id = $('asset-sync-id').value.trim();
    const payload = id ? { ids: [id] } : {};
    try {
      const response = await fetch('/api/words/assets/sync', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(payload) });
      showManagementResult('asset-sync-result', response, await responsePayload(response));
    } catch (error) { $('asset-sync-result').textContent = JSON.stringify({ error: error.message }, null, 2); }
  }
  async function debugList(event) {
    event.preventDefault();
    const params = new URLSearchParams();
    [['q', 'debug-q'], ['topic', 'debug-topic'], ['cefr', 'debug-cefr'], ['page', 'debug-page'], ['page_size', 'debug-page-size']].forEach(([key, id]) => { const value = $(id).value.trim(); if (value) params.set(key, value); });
    const url = `/api/words?${params}`;
    try { const response = await fetch(url); showDebugResult(url, response, await responsePayload(response)); }
    catch (error) { $('debug-result').textContent = JSON.stringify({ request: url, error: error.message }, null, 2); }
  }
  async function debugBatch(event) {
    event.preventDefault();
    const words = $('debug-batch-words').value.trim();
    if (!words) return;
    const url = `/api/words/batch?${new URLSearchParams({ words })}`;
    try { const response = await fetch(url); showDebugResult(url, response, await responsePayload(response)); }
    catch (error) { $('debug-result').textContent = JSON.stringify({ request: url, error: error.message }, null, 2); }
  }
  $('create-form').addEventListener('submit', createManagedWord); $('import-form').addEventListener('submit', importManagedWords); $('asset-sync-form').addEventListener('submit', syncManagedAssets); $('debug-word-form').addEventListener('submit', debugWord); $('debug-list-form').addEventListener('submit', debugList); $('debug-batch-form').addEventListener('submit', debugBatch);
  setAccent(state.accent); loadWords(); readHash();
})();
