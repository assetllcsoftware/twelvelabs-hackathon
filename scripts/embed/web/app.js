// Phase A throwaway local search UI. Vanilla JS, no build step.
//
// Three modes (text / image / text+image), one results grid.
// Image input accepts drag-and-drop, click-to-choose, AND clipboard paste.

const els = {
  form: document.getElementById('searchForm'),
  query: document.getElementById('queryInput'),
  topK: document.getElementById('topK'),
  goBtn: document.getElementById('goBtn'),
  spinner: document.getElementById('spinner'),
  err: document.getElementById('errorMsg'),
  results: document.getElementById('results'),
  dropzone: document.getElementById('dropzone'),
  fileInput: document.getElementById('fileInput'),
  dzStatus: document.getElementById('dzStatus'),
  dzPreview: document.getElementById('dzPreview'),
  modeTabs: document.getElementById('modeTabs'),
  modeHint: document.getElementById('modeHint'),
  refreshBtn: document.getElementById('refreshBtn'),
  statSeg: document.getElementById('statSegments'),
  statVid: document.getElementById('statVideos'),
  statBkt: document.getElementById('statBucket').querySelector('.num'),
  corpus: document.getElementById('corpusList'),
};

const MODE_HINT = {
  'text': 'Describe what you want to find. The full corpus will be ranked by cosine similarity.',
  'image': 'Drop a frame and Marengo will rank the corpus by visual similarity.',
  'text-image': 'Combine a text description with a reference image. Both contribute to the query vector.',
};

const state = {
  mode: 'text',
  file: null,
};

// ---------- mode tabs ------------------------------------------------------
els.modeTabs.addEventListener('click', (ev) => {
  const btn = ev.target.closest('.tab');
  if (!btn) return;
  for (const b of els.modeTabs.querySelectorAll('.tab')) b.classList.remove('active');
  btn.classList.add('active');
  state.mode = btn.dataset.mode;
  els.modeHint.textContent = MODE_HINT[state.mode] || '';
  syncModeUI();
});

function syncModeUI() {
  const showText = state.mode === 'text' || state.mode === 'text-image';
  const showImage = state.mode === 'image' || state.mode === 'text-image';
  els.query.parentElement.style.display = showText ? '' : 'none';
  els.dropzone.classList.toggle('hidden', !showImage);
  if (showText) els.query.focus();
}

// ---------- dropzone -------------------------------------------------------
['dragenter', 'dragover'].forEach((evt) => {
  els.dropzone.addEventListener(evt, (ev) => {
    ev.preventDefault();
    els.dropzone.classList.add('over');
  });
});
['dragleave', 'dragend'].forEach((evt) => {
  els.dropzone.addEventListener(evt, () => els.dropzone.classList.remove('over'));
});
els.dropzone.addEventListener('drop', (ev) => {
  ev.preventDefault();
  els.dropzone.classList.remove('over');
  const file = ev.dataTransfer?.files?.[0];
  if (file) handleFile(file);
});
els.dropzone.addEventListener('click', () => els.fileInput.click());
els.fileInput.addEventListener('change', () => {
  if (els.fileInput.files[0]) handleFile(els.fileInput.files[0]);
});
window.addEventListener('paste', (ev) => {
  if (state.mode === 'text') return;
  const items = ev.clipboardData?.items || [];
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) {
        handleFile(file);
        ev.preventDefault();
      }
      return;
    }
  }
});

function handleFile(file) {
  if (!file.type.startsWith('image/')) {
    showError('please drop an image');
    return;
  }
  state.file = file;
  els.dzStatus.textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KiB`;
  const url = URL.createObjectURL(file);
  els.dzPreview.src = url;
  els.dzPreview.hidden = false;
}

// ---------- submit ---------------------------------------------------------
els.form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  hideError();
  setBusy(true);
  try {
    const top_k = Math.max(1, Math.min(50, parseInt(els.topK.value, 10) || 10));
    let resp;
    if (state.mode === 'text') {
      const q = els.query.value.trim();
      if (!q) throw new Error('enter a text query');
      resp = await fetch('/api/search/text', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ q, top_k }),
      });
    } else if (state.mode === 'image') {
      if (!state.file) throw new Error('drop or paste an image first');
      const fd = new FormData();
      fd.append('file', state.file);
      fd.append('top_k', String(top_k));
      resp = await fetch('/api/search/image', { method: 'POST', body: fd });
    } else {
      const q = els.query.value.trim();
      if (!q) throw new Error('enter a text query');
      if (!state.file) throw new Error('drop or paste an image first');
      const fd = new FormData();
      fd.append('q', q);
      fd.append('file', state.file);
      fd.append('top_k', String(top_k));
      resp = await fetch('/api/search/text-image', { method: 'POST', body: fd });
    }
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(detail.detail || `error ${resp.status}`);
    }
    const results = await resp.json();
    renderResults(results);
  } catch (exc) {
    showError(exc.message || String(exc));
  } finally {
    setBusy(false);
  }
});

// ---------- render ---------------------------------------------------------
//
// Cosine on Marengo Embed 3.0 typically lands in these bands. They're rough,
// but useful for telling "ranking is meaningful" from "ranking is noise":
//   > 0.50  very strong match
//   0.30 – 0.50  confident match
//   0.15 – 0.30  weak / corpus-bounded match
//   < 0.15  effectively noise floor (especially for text→video without transcription)
function bandFor(score) {
  if (score >= 0.5) return { tier: 'strong', label: 'STRONG' };
  if (score >= 0.3) return { tier: 'good', label: 'GOOD' };
  if (score >= 0.15) return { tier: 'weak', label: 'WEAK' };
  return { tier: 'noise', label: 'NOISE FLOOR' };
}

function renderResults(results) {
  if (!results.length) {
    els.results.innerHTML = '<div class="placeholder"><p>no matches</p></div>';
    return;
  }
  els.results.innerHTML = '';

  // Confidence banner: based on top score and the spread of the top results.
  const top = Number(results[0].score);
  const last = Number(results[results.length - 1].score);
  const spread = top - last;
  const band = bandFor(top);
  const spreadHint = spread < 0.02
    ? 'Score spread is tiny — ranking among these is barely above noise.'
    : `Score spread across these ${results.length}: ${spread.toFixed(4)}.`;
  const bannerCopy =
    band.tier === 'noise'
      ? `Top score ${formatScore(top)} is at the noise floor. The corpus may not strongly contain this concept — try image mode with a reference frame, or ingest more diverse / narrated videos.`
      : band.tier === 'weak'
      ? `Top score ${formatScore(top)} is weak. ${spreadHint}`
      : `Top score ${formatScore(top)}. ${spreadHint}`;

  const banner = document.createElement('div');
  banner.className = `confidence band-${band.tier}`;
  banner.innerHTML = `<span class="tier">${band.label}</span><span class="msg">${escapeHtml(bannerCopy)}</span>`;
  els.results.appendChild(banner);

  results.forEach((r, i) => {
    const card = document.createElement('article');
    card.className = 'result-card';
    const score = Number(r.score);
    const cardBand = bandFor(score);
    const kind = (r.kind || 'clip').toUpperCase();
    const opt = (r.embedding_option || 'visual').toUpperCase();
    const ts = Number(r.timestamp_sec);

    let context;
    if (r.kind === 'frame') {
      context = `FRAME @ <span class="accent">${ts.toFixed(2)}s</span>`;
    } else if (r.refined_from_frame) {
      context = `CLIP <span class="accent">${formatRange(r.start_sec, r.end_sec)}</span> · best frame @ <span class="accent">${ts.toFixed(2)}s</span>`;
    } else {
      context = `CLIP <span class="accent">${formatRange(r.start_sec, r.end_sec)}</span>`;
    }

    const thumbHtml = r.thumb_url
      ? `<img class="thumb" src="${r.thumb_url}" alt="matched frame" loading="lazy" />`
      : '';

    card.innerHTML = `
      <header class="meta">
        <span class="rank">#${i + 1}</span>
        <span class="score band-${cardBand.tier}" title="${cardBand.label}">${formatScore(score)}</span>
        <span class="kind kind-${r.kind || 'clip'}">${kind}</span>
        <span class="opt">${opt}</span>
      </header>
      ${thumbHtml}
      <video controls preload="metadata" playsinline></video>
      <div class="info">
        <span class="key">${escapeHtml(r.s3_key)}</span>
        <span class="seg">${context}</span>
        <div class="links">
          <a href="${r.presigned_url}" target="_blank" rel="noopener">OPEN ↗</a>
          <a href="#" data-act="copy">COPY URL</a>
        </div>
      </div>
    `;
    const video = card.querySelector('video');
    // Set src AFTER inserting so the #t fragment is honored on metadata load.
    // Browsers honor #t=ss to seek the first frame load to the matched moment.
    video.src = r.presigned_url;

    card.querySelector('[data-act="copy"]').addEventListener('click', (ev) => {
      ev.preventDefault();
      navigator.clipboard.writeText(r.presigned_url);
      ev.target.textContent = 'COPIED ✓';
      setTimeout(() => (ev.target.textContent = 'COPY URL'), 1200);
    });
    els.results.appendChild(card);
  });
}

function formatScore(score) {
  const sign = score >= 0 ? '+' : '';
  return `${sign}${score.toFixed(4)}`;
}

function formatRange(a, b) {
  return `${(+a).toFixed(1)}s — ${(+b).toFixed(1)}s`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ---------- error / busy ---------------------------------------------------
function showError(msg) {
  els.err.textContent = msg;
  els.err.hidden = false;
}
function hideError() {
  els.err.hidden = true;
  els.err.textContent = '';
}
function setBusy(busy) {
  els.goBtn.disabled = busy;
  els.spinner.hidden = !busy;
}

// ---------- stats ----------------------------------------------------------
async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    if (!r.ok) throw new Error((await r.json()).detail || `error ${r.status}`);
    const s = await r.json();
    // Backwards-compat: older API shape used `segments`; new one returns
    // `rows`/`clips`/`frames`.
    const total = s.rows ?? s.segments ?? 0;
    els.statSeg.textContent = String(total).padStart(2, '0');
    els.statVid.textContent = String(s.videos).padStart(2, '0');
    els.statBkt.textContent = s.bucket || '--';
    if (!s.by_video?.length) {
      els.corpus.innerHTML = '<li class="muted">no embeddings — run scripts.embed.embed_videos</li>';
    } else {
      els.corpus.innerHTML = s.by_video
        .map((v) => {
          const clips = v.clips ?? v.segments ?? 0;
          const frames = v.frames ?? 0;
          const breakdown = frames
            ? `${clips}c · ${frames}f`
            : `${clips}`;
          return `<li><span class="key" title="${escapeHtml(v.s3_key)}">${escapeHtml(v.s3_key)}</span><span class="count">${breakdown}</span></li>`;
        })
        .join('');
    }
  } catch (exc) {
    els.corpus.innerHTML = `<li class="muted">stats failed: ${escapeHtml(exc.message)}</li>`;
  }
}

els.refreshBtn.addEventListener('click', async () => {
  els.refreshBtn.disabled = true;
  els.refreshBtn.textContent = 'REFRESHING…';
  try {
    await fetch('/api/refresh', { method: 'POST' });
    await loadStats();
  } finally {
    els.refreshBtn.disabled = false;
    els.refreshBtn.textContent = 'REFRESH';
  }
});

syncModeUI();
loadStats();
