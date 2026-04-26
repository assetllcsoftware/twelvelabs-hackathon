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
  detPanel: document.getElementById('detectionsPanel'),
  detMaster: document.getElementById('detMasterToggle'),
  detMasterHint: document.getElementById('detMasterHint'),
  detClasses: document.getElementById('detClasses'),
  detEmptyHint: document.getElementById('detEmptyHint'),
};

const MODE_HINT = {
  'text': 'Describe what you want to find. The full corpus will be ranked by cosine similarity.',
  'image': 'Drop a frame and Marengo will rank the corpus by visual similarity.',
  'text-image': 'Combine a text description with a reference image. Both contribute to the query vector.',
};

const state = {
  mode: 'text',
  file: null,
  pegasusPresets: [],
  // s3_key -> AbortController for any currently-running describe stream so
  // a quick double-click cancels the previous run instead of stacking two.
  describeAborts: new Map(),
  // YOLO detection overlay state. ``classKey`` is "<model>::<class>" so we
  // can disambiguate same-named classes across models.
  detections: {
    masterOn: true,
    catalog: [], // [{name, model, color, count}], sorted by count desc.
    colorByKey: new Map(), // classKey -> hex color
    visibleByKey: new Map(), // classKey -> bool
    // model_name -> bool. When true, that model's detections render as a
    // polygon only (no bbox, no per-detection label). Set by the cache
    // model spec; we also seed obvious cases (power_line) here so old
    // caches without the flag still look right.
    maskOnlyByModel: new Map([['pldm-power-line', true]]),
  },
};

const DETECTION_PALETTE_FALLBACK = [
  '#ff8c00', '#00e0ff', '#ff5cc6', '#a4ff5c', '#ffd166', '#9b8cff',
];

function classKey(modelName, className) {
  return `${modelName || 'unknown'}::${className || 'unknown'}`;
}

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
// Cosine on Marengo Embed 3.0 typically lands in these bands. We treat any
// positive score as "ranked"; we used to flag scores < 0.15 as a "noise
// floor" but it just made small corpora look broken even when results were
// fine, so the banding is purely cosmetic now.
//   > 0.50  very strong match
//   0.30 – 0.50  confident match
//   < 0.30  weaker but still ranked
function bandFor(score) {
  if (score >= 0.5) return { tier: 'strong', label: 'STRONG' };
  if (score >= 0.3) return { tier: 'good', label: 'GOOD' };
  return { tier: 'weak', label: 'RANKED' };
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
  const bannerCopy = `Top score ${formatScore(top)}. ${spreadHint}`;

  const banner = document.createElement('div');
  banner.className = `confidence band-${band.tier}`;
  banner.innerHTML = `<span class="tier">${band.label}</span><span class="msg">${escapeHtml(bannerCopy)}</span>`;
  els.results.appendChild(banner);

  // Make sure any classes that show up in fresh results get represented in
  // the toggle bar even when they weren't in /api/detection-classes (e.g.
  // running detections live without a page reload).
  ingestResultDetections(results);

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
      ? `<div class="thumb-wrap">
           <img class="thumb" src="${r.thumb_url}" alt="matched frame" loading="lazy" />
           ${detectionOverlayHtml(r)}
         </div>`
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
        ${detectionLegendHtml(r)}
        <div class="links">
          <a href="${r.presigned_url}" target="_blank" rel="noopener">OPEN ↗</a>
          <a href="#" data-act="copy">COPY URL</a>
        </div>
      </div>
      ${pegasusInlineHtml(r)}
      ${pegasusPanelHtml()}
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

    wirePegasusPanel(card, r);

    els.results.appendChild(card);
  });

  // Toggle visibility now that the cards are in the DOM.
  applyDetectionVisibility();
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

// ---------- yolo detection overlay -----------------------------------------
//
// Each result card with a thumb gets an absolutely-positioned SVG that
// covers the image. The SVG uses a 1000x1000 viewBox with
// preserveAspectRatio="none" so normalized polygon coords (in [0,1])
// stretch naturally to whatever pixel size the thumb is rendered at.
// vector-effect="non-scaling-stroke" keeps stroke-width constant in CSS
// pixels regardless of the SVG transform.

const DET_VB = 1000;

function colorForKey(key) {
  const c = state.detections.colorByKey.get(key);
  if (c) return c;
  // Stable fallback color from the palette based on insertion order.
  const idx = state.detections.colorByKey.size % DETECTION_PALETTE_FALLBACK.length;
  const fallback = DETECTION_PALETTE_FALLBACK[idx];
  state.detections.colorByKey.set(key, fallback);
  return fallback;
}

function ingestResultDetections(results) {
  // Walk every detection in the result set and make sure its (model,
  // class) pair shows up in the toggle bar with a stable color.
  const counts = new Map();
  for (const r of results || []) {
    for (const d of r.detections || []) {
      const key = classKey(d.model_name, d.class_name);
      counts.set(key, (counts.get(key) || 0) + 1);
      if (!state.detections.colorByKey.has(key)) {
        state.detections.colorByKey.set(
          key,
          DETECTION_PALETTE_FALLBACK[
            state.detections.colorByKey.size % DETECTION_PALETTE_FALLBACK.length
          ],
        );
      }
      if (!state.detections.visibleByKey.has(key)) {
        state.detections.visibleByKey.set(key, true);
      }
    }
  }
  // Merge new entries into the catalog if they weren't in /api/detection-classes.
  const known = new Set(
    state.detections.catalog.map((c) => classKey(c.model, c.name)),
  );
  let changed = false;
  for (const [key, count] of counts.entries()) {
    if (known.has(key)) continue;
    const [model, name] = key.split('::');
    state.detections.catalog.push({
      name,
      model,
      color: colorForKey(key),
      count,
    });
    changed = true;
  }
  if (changed) renderDetectionPanel();
}

function detectionOverlayHtml(result) {
  const dets = result.detections || [];
  if (!dets.length) return '';
  const groups = dets
    .map((d) => {
      const key = classKey(d.model_name, d.class_name);
      const color = colorForKey(key);
      const maskOnly = state.detections.maskOnlyByModel.get(d.model_name) === true;
      const poly = (d.polygon_xy || [])
        .map((v) => (v * DET_VB).toFixed(1))
        .reduce((acc, v, i) => {
          if (i % 2 === 0) acc.push(v);
          else acc[acc.length - 1] += `,${v}`;
          return acc;
        }, [])
        .join(' ');
      const [bx1, by1, bx2, by2] = (d.bbox_xyxy || [0, 0, 0, 0]).map((v) => v * DET_VB);
      const conf = (Number(d.confidence) || 0).toFixed(2);
      const labelX = bx1.toFixed(1);
      const labelY = Math.max(0, by1 - 8).toFixed(1);
      const w = (bx2 - bx1).toFixed(1);
      const h = (by2 - by1).toFixed(1);
      const titleText = `${d.class_name || 'unknown'} · ${conf} · ${d.model_name || ''}`;
      // For mask-only models (e.g. power lines): no bbox, no label, and
      // no fallback rectangle when the polygon is missing — drawing an
      // axis-aligned rect there would re-introduce the exact "boxes
      // around power lines" we're trying to avoid.
      if (maskOnly) {
        if (!poly) return '';
        return `
          <g class="det mask-only" data-cls-key="${escapeHtml(key)}" style="--det-color: ${color}">
            <title>${escapeHtml(titleText)}</title>
            <polygon class="det-poly" points="${poly}"></polygon>
          </g>
        `;
      }
      const polyShape = poly
        ? `<polygon class="det-poly" points="${poly}"></polygon>`
        : `<rect class="det-poly" x="${bx1.toFixed(1)}" y="${by1.toFixed(1)}" width="${w}" height="${h}"></rect>`;
      return `
        <g class="det" data-cls-key="${escapeHtml(key)}" style="--det-color: ${color}">
          <title>${escapeHtml(titleText)}</title>
          ${polyShape}
          <rect class="det-box" x="${bx1.toFixed(1)}" y="${by1.toFixed(1)}" width="${w}" height="${h}"></rect>
          <text class="det-lbl" x="${labelX}" y="${labelY}">${escapeHtml(d.class_name || '?')} ${conf}</text>
        </g>
      `;
    })
    .join('');
  return `
    <svg class="detect-overlay" viewBox="0 0 ${DET_VB} ${DET_VB}" preserveAspectRatio="none" aria-hidden="true">
      ${groups}
    </svg>
  `;
}

function detectionLegendHtml(result) {
  const cats = result.detection_classes || [];
  if (!cats.length) return '';
  const chips = cats
    .map((c) => {
      const key = classKey(c.model, c.name);
      const color = colorForKey(key);
      return `<span class="det-chip" data-cls-key="${escapeHtml(key)}" style="--det-color: ${color}"><span class="dot"></span>${escapeHtml(c.name)} ×${c.count}</span>`;
    })
    .join('');
  return `<div class="det-legend">${chips}</div>`;
}

function renderDetectionPanel() {
  const cat = state.detections.catalog;
  if (!cat.length) {
    els.detEmptyHint.hidden = false;
    els.detClasses.innerHTML = '';
    els.detMasterHint.textContent = 'no cache';
    els.detPanel.hidden = false;
    return;
  }
  els.detPanel.hidden = false;
  els.detEmptyHint.hidden = true;
  els.detMasterHint.textContent = `${cat.length} class${cat.length === 1 ? '' : 'es'}`;
  els.detClasses.innerHTML = cat
    .map((c) => {
      const key = classKey(c.model, c.name);
      const visible = state.detections.visibleByKey.get(key) !== false;
      return `
        <li class="det-class" data-cls-key="${escapeHtml(key)}">
          <label>
            <input type="checkbox" ${visible ? 'checked' : ''}>
            <span class="dot" style="background:${colorForKey(key)};border-color:${colorForKey(key)}"></span>
            <span class="cls-name">${escapeHtml(c.name)}</span>
            <span class="cls-meta">${escapeHtml(c.model)} · ${c.count}</span>
          </label>
        </li>
      `;
    })
    .join('');
  for (const li of els.detClasses.querySelectorAll('.det-class')) {
    const key = li.dataset.clsKey;
    const cb = li.querySelector('input[type="checkbox"]');
    cb.addEventListener('change', () => {
      state.detections.visibleByKey.set(key, cb.checked);
      applyDetectionVisibility();
    });
  }
}

function applyDetectionVisibility() {
  const masterOn = state.detections.masterOn;
  els.results.classList.toggle('detections-off', !masterOn);
  for (const node of els.results.querySelectorAll('[data-cls-key]')) {
    const key = node.dataset.clsKey;
    const visible = state.detections.visibleByKey.get(key) !== false;
    node.classList.toggle('cls-hidden', !visible);
  }
}

async function loadDetectionClasses() {
  try {
    const r = await fetch('/api/detection-classes');
    if (!r.ok) return;
    const data = await r.json();
    state.detections.catalog = Array.isArray(data.classes) ? data.classes : [];
    for (const c of state.detections.catalog) {
      const key = classKey(c.model, c.name);
      if (c.color) state.detections.colorByKey.set(key, c.color);
      if (!state.detections.visibleByKey.has(key)) {
        state.detections.visibleByKey.set(key, true);
      }
    }
    for (const m of (data.models || [])) {
      if (m && m.name) {
        state.detections.maskOnlyByModel.set(m.name, !!m.mask_only);
      }
    }
    renderDetectionPanel();
  } catch (_) {
    /* silent: panel just stays hidden */
  }
}

els.detMaster.addEventListener('change', () => {
  state.detections.masterOn = els.detMaster.checked;
  applyDetectionVisibility();
});

// ---------- pegasus (video-to-text per result card) ------------------------
//
// Each result card gets a small panel with:
//   - a preset <select> (populated from /api/describe/presets)
//   - an optional editable prompt textarea (overrides the preset)
//   - a DESCRIBE button + status badge (Cached ✓ / Generating… / Failed)
//   - a streaming output area
//
// Streaming wire format is NDJSON (one JSON object per line). Each line is
// either {type:"meta",...}, {type:"delta",content,cached}, {type:"done",
// cached,model}, or {type:"error",message}. We use fetch + ReadableStream
// instead of EventSource because EventSource is GET-only.

function pegasusInlineHtml(result) {
  const peg = result.pegasus;
  if (!peg || !peg.text) return '';
  const presetLbl = (peg.preset || 'preset').toUpperCase();
  const inherited = peg.inherited
    ? `<span class="muted"> · inherited from clip ${(+peg.clip_start_sec).toFixed(1)}–${(+peg.clip_end_sec).toFixed(1)}s</span>`
    : '';
  return `
    <section class="pegasus-inline" data-pegasus-inline>
      <header>
        <span class="badge cached">CACHED ✓</span>
        <span class="muted">${escapeHtml(presetLbl)}</span>${inherited}
      </header>
      <pre>${escapeHtml(peg.text.trim())}</pre>
    </section>
  `;
}

function pegasusPanelHtml() {
  if (!state.pegasusPresets.length) {
    // Server hasn't told us about presets yet — render a minimal panel
    // with a default DESCRIBE button. The wiring fn will refuse to fire
    // until we know about at least one preset.
    return `
      <div class="pegasus" data-pegasus="pending">
        <div class="pegasus-row">
          <span class="pegasus-lbl">PEGASUS</span>
          <span class="pegasus-hint muted">loading prompts…</span>
        </div>
      </div>
    `;
  }
  const options = state.pegasusPresets
    .map(
      (p, idx) =>
        `<option value="${escapeHtml(p.id)}"${idx === 0 ? ' selected' : ''}>${escapeHtml(p.label)}</option>`,
    )
    .join('');
  return `
    <div class="pegasus" data-pegasus="ready">
      <div class="pegasus-row">
        <span class="pegasus-lbl">PEGASUS</span>
        <select class="pegasus-preset" data-act="preset">${options}</select>
        <button type="button" class="pegasus-go" data-act="describe">DESCRIBE</button>
      </div>
      <details class="pegasus-advanced">
        <summary>EDIT PROMPT</summary>
        <textarea class="pegasus-prompt" rows="4" spellcheck="false" data-act="prompt"></textarea>
        <p class="pegasus-hint muted">Edit and click DESCRIBE to override the preset for this card.</p>
      </details>
      <div class="pegasus-status" data-act="status" hidden></div>
      <pre class="pegasus-output" data-act="output" hidden></pre>
    </div>
  `;
}

function wirePegasusPanel(card, result) {
  const panel = card.querySelector('.pegasus');
  if (!panel || panel.dataset.pegasus !== 'ready') return;

  const presetSel = panel.querySelector('[data-act="preset"]');
  const promptTa = panel.querySelector('[data-act="prompt"]');
  const goBtn = panel.querySelector('[data-act="describe"]');
  const statusEl = panel.querySelector('[data-act="status"]');
  const outputEl = panel.querySelector('[data-act="output"]');
  const advanced = panel.querySelector('.pegasus-advanced');

  // Seed the textarea with the currently-selected preset's prompt so the
  // user can edit from a sensible starting point.
  const fillPromptFromPreset = () => {
    const preset = state.pegasusPresets.find((p) => p.id === presetSel.value);
    promptTa.value = preset?.prompt || '';
  };
  fillPromptFromPreset();
  presetSel.addEventListener('change', fillPromptFromPreset);

  goBtn.addEventListener('click', () => {
    const customPrompt = promptTa.value.trim();
    const presetId = presetSel.value;
    const isCustom = advanced.open && customPrompt && customPrompt !== presetForId(presetId)?.prompt;
    runDescribe({
      card,
      result,
      panel,
      goBtn,
      statusEl,
      outputEl,
      body: isCustom
        ? { s3_key: result.s3_key, prompt: customPrompt }
        : { s3_key: result.s3_key, preset: presetId },
      labelForBadge: isCustom
        ? 'CUSTOM PROMPT'
        : (presetForId(presetId)?.label || presetId).toUpperCase(),
      forceMode: false,
    });
  });
}

function presetForId(id) {
  return state.pegasusPresets.find((p) => p.id === id);
}

async function runDescribe({
  card,
  result,
  panel,
  goBtn,
  statusEl,
  outputEl,
  body,
  labelForBadge,
  forceMode,
}) {
  // Cancel any in-flight stream on this card.
  const key = result.s3_key + '|' + (panel.dataset.runId || '');
  const prevAbort = state.describeAborts.get(key);
  if (prevAbort) prevAbort.abort();

  const controller = new AbortController();
  state.describeAborts.set(key, controller);

  panel.classList.add('busy');
  goBtn.disabled = true;
  goBtn.textContent = 'STREAMING…';
  outputEl.hidden = false;
  outputEl.textContent = '';
  statusEl.hidden = false;
  statusEl.className = 'pegasus-status running';
  statusEl.textContent = `${labelForBadge} · streaming…`;

  let cached = false;
  let gotAny = false;

  try {
    const resp = await fetch('/api/describe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, force: forceMode }),
      signal: controller.signal,
    });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(detail.detail || `error ${resp.status}`);
    }

    await readNdjsonStream(resp, (line) => {
      if (line.type === 'delta' && typeof line.content === 'string') {
        outputEl.textContent += line.content;
        gotAny = true;
        if (line.cached) cached = true;
      } else if (line.type === 'done') {
        cached = !!line.cached;
      } else if (line.type === 'error') {
        throw new Error(line.message || 'pegasus error');
      } else if (line.type === 'meta') {
        // Currently nothing to render; metadata is logged for debugging.
        if (line.model) panel.dataset.model = line.model;
      }
    });

    statusEl.className = 'pegasus-status ' + (cached ? 'cached' : 'fresh');
    statusEl.innerHTML = cached
      ? `<span class="badge cached">CACHED ✓</span> <span class="muted">${escapeHtml(labelForBadge)}</span> · <a href="#" data-act="regen">REGENERATE</a>`
      : `<span class="badge fresh">GENERATED</span> <span class="muted">${escapeHtml(labelForBadge)}</span> · <a href="#" data-act="regen">REGENERATE</a>`;
    const regen = statusEl.querySelector('[data-act="regen"]');
    if (regen) {
      regen.addEventListener('click', (ev) => {
        ev.preventDefault();
        runDescribe({
          card,
          result,
          panel,
          goBtn,
          statusEl,
          outputEl,
          body,
          labelForBadge,
          forceMode: true,
        });
      });
    }

    if (!gotAny) outputEl.textContent = '(no output)';
  } catch (exc) {
    if (exc.name === 'AbortError') {
      statusEl.className = 'pegasus-status';
      statusEl.textContent = 'cancelled';
    } else {
      statusEl.className = 'pegasus-status error';
      statusEl.textContent = `failed: ${exc.message || exc}`;
    }
  } finally {
    panel.classList.remove('busy');
    goBtn.disabled = false;
    goBtn.textContent = 'DESCRIBE';
    state.describeAborts.delete(key);
  }
}

async function readNdjsonStream(resp, onLine) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nlIdx;
    while ((nlIdx = buf.indexOf('\n')) >= 0) {
      const raw = buf.slice(0, nlIdx).trim();
      buf = buf.slice(nlIdx + 1);
      if (!raw) continue;
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (_) {
        // tolerate stray lines (shouldn't happen, but don't crash the UI)
        continue;
      }
      onLine(parsed);
    }
  }
  // Flush any trailing line that didn't end with a newline.
  const tail = buf.trim();
  if (tail) {
    try {
      onLine(JSON.parse(tail));
    } catch (_) {
      /* ignore */
    }
  }
}

async function loadPegasusPresets() {
  try {
    const r = await fetch('/api/describe/presets');
    if (!r.ok) return;
    const data = await r.json();
    state.pegasusPresets = Array.isArray(data.presets) ? data.presets : [];
  } catch (_) {
    /* leave empty; cards will render the muted placeholder */
  }
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
loadPegasusPresets();
loadDetectionClasses();
