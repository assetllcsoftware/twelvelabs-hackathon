(() => {
  const dataNode = document.getElementById('categories-data');
  const categories = dataNode ? JSON.parse(dataNode.textContent || '[]') : [];

  const els = {
    nav: document.getElementById('categoryNav'),
    toolNav: document.getElementById('toolNav'),
    title: document.getElementById('categoryTitle'),
    description: document.getElementById('categoryDescription'),
    categoryView: document.getElementById('categoryView'),
    youtubeView: document.getElementById('youtubeView'),
    youtubeForm: document.getElementById('youtubeForm'),
    youtubeUrl: document.getElementById('youtubeUrl'),
    youtubeFilename: document.getElementById('youtubeFilename'),
    youtubeSubmit: document.getElementById('youtubeSubmit'),
    youtubeJobList: document.getElementById('youtubeJobList'),
    youtubeEmpty: document.getElementById('youtubeEmpty'),
    dropzone: document.getElementById('dropzone'),
    fileInput: document.getElementById('fileInput'),
    chooseButton: document.getElementById('chooseButton'),
    dropzoneHint: document.getElementById('dropzoneHint'),
    uploadStatus: document.getElementById('uploadStatus'),
    uploadList: document.getElementById('uploadList'),
    grid: document.getElementById('filesGrid'),
    empty: document.getElementById('filesEmpty'),
    filesCount: document.getElementById('filesCount'),
    filesHeading: document.getElementById('filesHeading'),
    refresh: document.getElementById('refreshButton'),
    searchBox: document.getElementById('searchBox'),
    search: document.getElementById('searchInput'),
    toasts: document.getElementById('toastContainer'),
    modal: document.getElementById('modal'),
    modalTitle: document.getElementById('modalTitle'),
    modalSubtitle: document.getElementById('modalSubtitle'),
    modalBody: document.getElementById('modalBody'),
    modalDownload: document.getElementById('modalDownload'),
    missionTimer: document.getElementById('missionTimer'),
    feedFrame: document.getElementById('feedFrame'),
    statTotal: document.getElementById('statTotal'),
    statDetections: document.getElementById('statDetections'),
  };

  const missionStart = performance.now();
  function tickMission() {
    const elapsed = (performance.now() - missionStart) / 1000;
    if (els.missionTimer) {
      els.missionTimer.textContent = `+${elapsed.toFixed(1)}s`;
    }
    if (els.feedFrame) {
      const frame = Math.floor(elapsed * 24) % 1000;
      els.feedFrame.textContent = `F${String(frame).padStart(3, '0')}`;
    }
  }
  setInterval(tickMission, 100);
  tickMission();

  if (!categories.length) {
    els.title.textContent = 'No categories configured';
    return;
  }

  const state = {
    activeView: 'category', // 'category' | 'tool'
    activeCategoryId: categories[0].id,
    activeToolId: null,
    files: [],
    counts: Object.fromEntries(categories.map((c) => [c.id, 0])),
    search: '',
    thumbCache: new Map(),
    framesIndex: null, // Map<basename, file>
    framesIndexedAt: 0,
    activeDownloadKey: null,
    youtube: {
      jobs: new Map(), // id -> job
      pollTimer: null,
      hasPending: false,
    },
  };

  const iconHref = (icon) => `#icon-${icon || 'data'}`;

  function categoryById(id) {
    return categories.find((c) => c.id === id);
  }

  function formatBytes(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const idx = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return `${(bytes / Math.pow(1024, idx)).toFixed(1)} ${units[idx]}`;
  }

  function formatDate(iso) {
    try {
      return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    } catch (_) {
      return iso;
    }
  }

  function showToast({ title, body, variant }) {
    const node = document.createElement('div');
    node.className = `toast ${variant || ''}`.trim();
    node.innerHTML = `<div class="toast-title"></div><div class="toast-body"></div>`;
    node.querySelector('.toast-title').textContent = title;
    node.querySelector('.toast-body').textContent = body || '';
    els.toasts.appendChild(node);
    setTimeout(() => {
      node.style.transition = 'opacity 200ms ease, transform 200ms ease';
      node.style.opacity = '0';
      node.style.transform = 'translateY(6px)';
      setTimeout(() => node.remove(), 220);
    }, 4200);
  }

  function getActiveCategory() {
    return categoryById(state.activeCategoryId);
  }

  function renderSidebar() {
    els.nav.innerHTML = '';
    for (const cat of categories) {
      const button = document.createElement('button');
      button.className = 'category-button';
      button.dataset.categoryId = cat.id;
      if (state.activeView === 'category' && cat.id === state.activeCategoryId) {
        button.classList.add('active');
      }
      const initialCount = String(state.counts[cat.id] || 0).padStart(2, '0');
      button.innerHTML = `
        <span class="icon-wrap"><svg class="icon"><use href="${iconHref(cat.icon)}"/></svg></span>
        <span>
          <span class="label">${cat.label}</span>
          <span class="desc">${cat.description}</span>
        </span>
        <span class="count" data-count-for="${cat.id}">${initialCount}</span>
      `;
      button.addEventListener('click', () => setActiveCategory(cat.id));
      els.nav.appendChild(button);
    }
    if (els.toolNav) {
      const buttons = els.toolNav.querySelectorAll('[data-tool-id]');
      buttons.forEach((btn) => {
        if (state.activeView === 'tool' && btn.dataset.toolId === state.activeToolId) {
          btn.classList.add('active');
        } else {
          btn.classList.remove('active');
        }
      });
    }
  }

  function updateSidebarCounts() {
    for (const cat of categories) {
      const el = els.nav.querySelector(`[data-count-for="${cat.id}"]`);
      if (el) el.textContent = String(state.counts[cat.id] || 0).padStart(2, '0');
    }
    updateGlobalStats();
  }

  function setActiveCategory(id) {
    if (state.activeView === 'category' && state.activeCategoryId === id) return;
    state.activeView = 'category';
    state.activeToolId = null;
    state.activeCategoryId = id;
    state.search = '';
    if (els.search) els.search.value = '';
    state.thumbCache.clear();
    renderSidebar();
    renderHeader();
    renderFiles([]);
    loadFiles();
  }

  function setActiveTool(id) {
    if (state.activeView === 'tool' && state.activeToolId === id) return;
    state.activeView = 'tool';
    state.activeToolId = id;
    state.search = '';
    if (els.search) els.search.value = '';
    renderSidebar();
    renderHeader();
    if (id === 'youtube') {
      refreshYoutubeJobs();
    }
  }

  function renderHeader() {
    const isCategory = state.activeView === 'category';
    if (els.categoryView) els.categoryView.hidden = !isCategory;
    if (els.youtubeView) els.youtubeView.hidden = isCategory || state.activeToolId !== 'youtube';
    if (els.searchBox) els.searchBox.hidden = !isCategory;
    if (els.refresh) {
      els.refresh.hidden = false;
    }

    if (isCategory) {
      const cat = getActiveCategory();
      els.title.textContent = cat.label;
      els.description.textContent = cat.description;
      els.filesHeading.innerHTML = `Registry <em>//</em> ${cat.label}`;
      els.fileInput.accept = cat.accept.join(',');
      const exts = cat.extensions.map((e) => `.${e}`).join(' · ');
      els.dropzoneHint.textContent = `Accepted formats: ${exts}`;
      return;
    }

    if (state.activeToolId === 'youtube') {
      els.title.textContent = 'YouTube Ingest';
      els.description.textContent = 'Pull videos via yt-dlp and stream them into raw-videos/';
    }
  }

  function updateGlobalStats() {
    const total = Object.values(state.counts).reduce((a, b) => a + (b || 0), 0);
    if (els.statTotal) els.statTotal.textContent = String(total);
    if (els.statDetections) els.statDetections.textContent = String(state.counts['detections'] || 0);
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        ...(options.headers || {}),
      },
      ...options,
    });
    if (res.status === 401) {
      window.location.href = '/login';
      throw new Error('Unauthorized');
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (data && data.detail) detail = data.detail;
      } catch (_) {
        try { detail = (await res.text()) || detail; } catch (__) {}
      }
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  async function fetchPresignedDownload(key) {
    const data = await api('/api/files/presign-download', {
      method: 'POST',
      body: JSON.stringify({ key }),
    });
    return data.url;
  }

  async function loadFiles() {
    const cat = getActiveCategory();
    try {
      const data = await api(`/api/files?category=${encodeURIComponent(cat.id)}`);
      state.files = data.files || [];
      state.counts[cat.id] = state.files.length;
      updateSidebarCounts();
      renderFiles(state.files);
    } catch (err) {
      renderFiles([]);
      showToast({ title: 'Failed to load files', body: err.message, variant: 'error' });
    }
  }

  function renderFiles(files) {
    const cat = getActiveCategory();
    const filter = state.search.trim().toLowerCase();
    const filtered = filter ? files.filter((f) => f.name.toLowerCase().includes(filter)) : files;
    els.grid.innerHTML = '';
    els.filesCount.textContent = String(filtered.length);
    els.empty.hidden = filtered.length !== 0;

    for (const file of filtered) {
      const card = document.createElement('article');
      card.className = 'file-card';
      const ext = (file.name.split('.').pop() || '').toLowerCase();
      const overlayIcon =
        cat.kind === 'video' ? 'icon-play' : cat.kind === 'image' ? 'icon-eye' : 'icon-eye';
      card.innerHTML = `
        <div class="file-thumb" data-key="${encodeURIComponent(file.key)}" data-kind="${cat.kind}" title="Preview">
          <span class="crosshair-corners"></span>
          <svg class="icon icon-lg"><use href="${iconHref(cat.icon)}"/></svg>
          <span class="badge">${ext || cat.kind}</span>
          <span class="play-overlay"><svg class="icon icon-lg"><use href="#${overlayIcon}"/></svg></span>
        </div>
        <div class="file-body">
          <div class="file-name" title="${file.name}">${file.name}</div>
          <div class="file-meta">
            <span>${formatBytes(file.size)}</span>
            <span>${formatDate(file.last_modified)}</span>
          </div>
          <div class="file-actions"></div>
        </div>
      `;

      const actions = card.querySelector('.file-actions');
      if (cat.id === 'detections') {
        actions.appendChild(
          makeButton('Visualize', '#icon-target', () => openDetectionVisualizer(file)),
        );
      }
      actions.appendChild(
        makeButton('Preview', '#icon-eye', () => openPreviewForFile(file, cat)),
      );
      actions.appendChild(makeButton('Download', '#icon-download', () => downloadFile(file)));
      const del = makeButton('Delete', '#icon-trash', () => deleteFile(file));
      del.classList.add('danger');
      actions.appendChild(del);

      const thumb = card.querySelector('.file-thumb');
      thumb.addEventListener('click', () => openPreviewForFile(file, cat));
      els.grid.appendChild(card);

      if (cat.kind === 'image') {
        observeThumb(thumb, file);
      }
    }
  }

  function makeButton(label, iconRef, onClick) {
    const button = document.createElement('button');
    button.className = 'ghost-button';
    button.type = 'button';
    button.innerHTML = `<svg class="icon"><use href="${iconRef}"/></svg><span>${label}</span>`;
    button.addEventListener('click', (e) => {
      e.stopPropagation();
      onClick();
    });
    return button;
  }

  let observer;
  function ensureObserver() {
    if (observer) return observer;
    observer = new IntersectionObserver(
      async (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const target = entry.target;
          observer.unobserve(target);
          const key = decodeURIComponent(target.dataset.key);
          try {
            const url = state.thumbCache.get(key) || (await fetchPresignedDownload(key));
            state.thumbCache.set(key, url);
            const img = document.createElement('img');
            img.alt = '';
            img.loading = 'lazy';
            img.src = url;
            target.insertBefore(img, target.firstChild);
          } catch (err) {
            console.warn('thumb load failed', err);
          }
        }
      },
      { rootMargin: '120px' },
    );
    return observer;
  }

  function observeThumb(node) {
    ensureObserver().observe(node);
  }

  async function downloadFile(file) {
    try {
      const url = await fetchPresignedDownload(file.key);
      const link = document.createElement('a');
      link.href = url;
      link.download = file.name;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } catch (err) {
      showToast({ title: 'Download failed', body: err.message, variant: 'error' });
    }
  }

  async function deleteFile(file) {
    if (!confirm(`Delete ${file.name}?`)) return;
    try {
      await api(`/api/files?key=${encodeURIComponent(file.key)}`, { method: 'DELETE' });
      showToast({ title: 'Deleted', body: file.name, variant: 'success' });
      loadFiles();
    } catch (err) {
      showToast({ title: 'Delete failed', body: err.message, variant: 'error' });
    }
  }

  /* ------------------------------------------------------------------ */
  /* Modal                                                              */
  /* ------------------------------------------------------------------ */

  function openModal({ title, subtitle, downloadKey }) {
    els.modalTitle.textContent = title || 'Preview';
    els.modalSubtitle.textContent = subtitle || '';
    els.modalBody.innerHTML = '';
    state.activeDownloadKey = downloadKey || null;
    els.modal.hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    els.modal.hidden = true;
    els.modalBody.innerHTML = '';
    state.activeDownloadKey = null;
    document.body.style.overflow = '';
  }

  function setModalError(message) {
    const div = document.createElement('div');
    div.className = 'modal-error';
    div.textContent = message;
    els.modalBody.appendChild(div);
  }

  async function openPreviewForFile(file, cat) {
    openModal({
      title: file.name,
      subtitle: `${cat.label} · ${formatBytes(file.size)} · ${formatDate(file.last_modified)}`,
      downloadKey: file.key,
    });
    try {
      const url = await fetchPresignedDownload(file.key);
      if (cat.kind === 'video') {
        const video = document.createElement('video');
        video.controls = true;
        video.autoplay = true;
        video.src = url;
        els.modalBody.appendChild(video);
      } else if (cat.kind === 'image') {
        const img = document.createElement('img');
        img.className = 'preview';
        img.alt = file.name;
        img.src = url;
        els.modalBody.appendChild(img);
      } else {
        const text = await fetch(url).then((r) => {
          if (!r.ok) throw new Error(`Failed to fetch (HTTP ${r.status})`);
          return r.text();
        });
        const pretty = prettyPrintIfPossible(text, file.name);
        const pre = document.createElement('pre');
        pre.textContent = pretty;
        els.modalBody.appendChild(pre);
      }
    } catch (err) {
      setModalError(err.message || String(err));
    }
  }

  function prettyPrintIfPossible(text, filename) {
    const isJsonl = /\.jsonl$|\.ndjson$/i.test(filename);
    if (isJsonl) {
      const lines = text
        .split(/\r?\n/)
        .filter((line) => line.trim().length)
        .slice(0, 500);
      return lines
        .map((line) => {
          try {
            return JSON.stringify(JSON.parse(line), null, 2);
          } catch (_) {
            return line;
          }
        })
        .join('\n\n');
    }
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch (_) {
      return text;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Detection visualizer                                               */
  /* ------------------------------------------------------------------ */

  async function ensureFramesIndex(force = false) {
    const fresh = state.framesIndex && Date.now() - state.framesIndexedAt < 30000;
    if (fresh && !force) return state.framesIndex;
    try {
      const data = await api(`/api/files?category=${encodeURIComponent('frames')}`);
      const map = new Map();
      for (const file of data.files || []) {
        map.set(file.name, file);
        const lower = file.name.toLowerCase();
        if (!map.has(lower)) map.set(lower, file);
      }
      state.framesIndex = map;
      state.framesIndexedAt = Date.now();
    } catch (err) {
      state.framesIndex = new Map();
    }
    return state.framesIndex;
  }

  function basename(p) {
    if (!p) return '';
    const norm = String(p).replace(/\\/g, '/');
    return norm.substring(norm.lastIndexOf('/') + 1);
  }

  function pickDetectionList(item) {
    const candidates = ['detections', 'predictions', 'objects', 'boxes', 'annotations', 'instances'];
    for (const key of candidates) {
      if (Array.isArray(item?.[key])) return item[key];
    }
    return null;
  }

  function pickImageRef(item) {
    const candidates = ['image', 'frame', 'filename', 'file', 'name', 'image_path', 'image_name', 'frame_id'];
    for (const key of candidates) {
      if (typeof item?.[key] === 'string' && item[key]) return item[key];
    }
    return '';
  }

  function normalizeDetectionItem(item) {
    const label = item?.label ?? item?.class ?? item?.class_name ?? item?.category ?? item?.name ?? '';
    let score = item?.score ?? item?.confidence ?? item?.conf ?? null;
    if (typeof score === 'string') {
      const parsed = parseFloat(score);
      score = Number.isFinite(parsed) ? parsed : null;
    }
    let coords = item?.bbox ?? item?.box ?? item?.xyxy ?? item?.xywh ?? item?.rect ?? null;
    if (Array.isArray(coords) && coords.length >= 4) {
      coords = coords.slice(0, 4).map(Number);
    } else {
      coords = null;
    }
    return { coords, label: String(label || ''), score, raw: item };
  }

  function parseDetections(text, filename) {
    const isJsonl = /\.jsonl$|\.ndjson$/i.test(filename);
    if (isJsonl) {
      const records = [];
      for (const line of text.split(/\r?\n/)) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        try { records.push(JSON.parse(trimmed)); } catch (_) {}
      }
      return groupRecords(records);
    }
    let parsed;
    try { parsed = JSON.parse(text); }
    catch (err) { throw new Error('Could not parse detection file as JSON'); }
    return groupRecords(parsed);
  }

  function groupRecords(parsed) {
    const groups = new Map();
    const ensure = (ref) => {
      const key = ref || '__unmatched__';
      if (!groups.has(key)) groups.set(key, { imageRef: ref || '', boxes: [] });
      return groups.get(key);
    };

    if (Array.isArray(parsed)) {
      for (const item of parsed) {
        if (!item || typeof item !== 'object') continue;
        const dets = pickDetectionList(item);
        if (dets) {
          const ref = pickImageRef(item);
          const group = ensure(ref);
          for (const det of dets) group.boxes.push(normalizeDetectionItem(det));
        } else {
          const ref = pickImageRef(item);
          ensure(ref).boxes.push(normalizeDetectionItem(item));
        }
      }
    } else if (parsed && typeof parsed === 'object') {
      const dets = pickDetectionList(parsed);
      if (dets) {
        const ref = pickImageRef(parsed);
        const group = ensure(ref);
        for (const det of dets) group.boxes.push(normalizeDetectionItem(det));
      } else {
        for (const [ref, value] of Object.entries(parsed)) {
          if (Array.isArray(value)) {
            const group = ensure(ref);
            for (const det of value) group.boxes.push(normalizeDetectionItem(det));
          }
        }
      }
    }

    return Array.from(groups.values()).filter((g) => g.boxes.length > 0);
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('Failed to load image'));
      img.src = url;
    });
  }

  function colorForLabel(label) {
    const palette = ['#ffcb00', '#ffe98a', '#7dffa1', '#ff4d4d', '#4dd2ff', '#ffa64d', '#f78bff'];
    let hash = 0;
    const text = String(label || '');
    for (let i = 0; i < text.length; i += 1) hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
    return palette[hash % palette.length];
  }

  function drawBoxes(canvas, image, boxes, opts) {
    const ctx = canvas.getContext('2d');
    canvas.width = image.naturalWidth;
    canvas.height = image.naturalHeight;
    ctx.drawImage(image, 0, 0);

    const fontSize = Math.max(14, Math.round(image.naturalWidth / 70));
    ctx.font = `700 ${fontSize}px "JetBrains Mono", ui-monospace, monospace`;
    const lineWidth = Math.max(2, Math.round(image.naturalWidth / 400));

    for (const box of boxes) {
      if (!box.coords) continue;
      let [a, b, c, d] = box.coords;
      if (![a, b, c, d].every(Number.isFinite)) continue;
      const allBelowOne = [a, b, c, d].every((n) => n >= 0 && n <= 1.0);
      const normalized = opts.normalize === 'auto' ? allBelowOne : opts.normalize === 'yes';
      if (normalized) {
        a *= image.naturalWidth;
        c *= image.naturalWidth;
        b *= image.naturalHeight;
        d *= image.naturalHeight;
      }
      let x;
      let y;
      let w;
      let h;
      if (opts.format === 'xyxy') {
        x = Math.min(a, c);
        y = Math.min(b, d);
        w = Math.abs(c - a);
        h = Math.abs(d - b);
      } else if (opts.format === 'cxcywh') {
        x = a - c / 2;
        y = b - d / 2;
        w = c;
        h = d;
      } else {
        x = a;
        y = b;
        w = c;
        h = d;
      }
      if (w <= 0 || h <= 0) continue;

      const color = colorForLabel(box.label);
      ctx.strokeStyle = color;
      ctx.lineWidth = lineWidth;
      ctx.strokeRect(x, y, w, h);

      const labelText = `${(box.label || 'object').toUpperCase()}${
        box.score !== null && box.score !== undefined ? ` ${Math.round(Number(box.score) * 100)}%` : ''
      }`.trim();
      if (labelText) {
        const padding = 6;
        const metrics = ctx.measureText(labelText);
        const labelWidth = metrics.width + padding * 2;
        const labelHeight = fontSize + padding;
        const labelY = y - labelHeight - 2 < 0 ? y + 2 : y - labelHeight - 2;
        ctx.fillStyle = color;
        ctx.fillRect(x, labelY, labelWidth, labelHeight);
        ctx.fillStyle = '#0a0a00';
        ctx.fillText(labelText, x + padding, labelY + fontSize - 2);
      }

      const tickLen = Math.max(8, Math.round(Math.min(w, h) / 6));
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(1, lineWidth - 1);
      ctx.beginPath();
      ctx.moveTo(x, y); ctx.lineTo(x + tickLen, y);
      ctx.moveTo(x, y); ctx.lineTo(x, y + tickLen);
      ctx.moveTo(x + w, y); ctx.lineTo(x + w - tickLen, y);
      ctx.moveTo(x + w, y); ctx.lineTo(x + w, y + tickLen);
      ctx.moveTo(x, y + h); ctx.lineTo(x + tickLen, y + h);
      ctx.moveTo(x, y + h); ctx.lineTo(x, y + h - tickLen);
      ctx.moveTo(x + w, y + h); ctx.lineTo(x + w - tickLen, y + h);
      ctx.moveTo(x + w, y + h); ctx.lineTo(x + w, y + h - tickLen);
      ctx.stroke();
    }
  }

  async function openDetectionVisualizer(file) {
    openModal({
      title: `Visualize · ${file.name}`,
      subtitle: 'Bounding boxes rendered on matching frames',
      downloadKey: file.key,
    });

    const layout = document.createElement('div');
    layout.style.display = 'flex';
    layout.style.flexDirection = 'column';
    layout.style.gap = '0.9rem';
    els.modalBody.appendChild(layout);

    const loading = document.createElement('div');
    loading.className = 'modal-error';
    loading.style.background = 'transparent';
    loading.style.borderColor = 'var(--border)';
    loading.style.color = 'var(--muted)';
    loading.textContent = 'Loading detections...';
    layout.appendChild(loading);

    let groups = [];
    let framesIndex = new Map();
    try {
      const [downloadUrl, framesIdx] = await Promise.all([
        fetchPresignedDownload(file.key),
        ensureFramesIndex(true),
      ]);
      framesIndex = framesIdx;
      const text = await fetch(downloadUrl).then((r) => {
        if (!r.ok) throw new Error(`Failed to fetch detections (HTTP ${r.status})`);
        return r.text();
      });
      groups = parseDetections(text, file.name);
    } catch (err) {
      loading.remove();
      setModalError(err.message || String(err));
      return;
    }
    loading.remove();

    if (!groups.length) {
      setModalError('No detections found in this file.');
      return;
    }

    const toolbar = document.createElement('div');
    toolbar.className = 'viz-toolbar';
    toolbar.innerHTML = `
      <label>Format
        <select data-control="format">
          <option value="xywh">xywh (COCO)</option>
          <option value="xyxy">xyxy (corners)</option>
          <option value="cxcywh">cxcywh (YOLO)</option>
        </select>
      </label>
      <label>Coords
        <select data-control="normalize">
          <option value="auto">auto</option>
          <option value="yes">normalized</option>
          <option value="no">absolute</option>
        </select>
      </label>
      <span class="spacer"></span>
      <div class="pager">
        <button class="ghost-button" type="button" data-action="prev"><svg class="icon"><use href="#icon-arrow-left"/></svg></button>
        <span class="position" data-role="position"></span>
        <button class="ghost-button" type="button" data-action="next"><svg class="icon"><use href="#icon-arrow-right"/></svg></button>
      </div>
    `;
    layout.appendChild(toolbar);

    const stage = document.createElement('div');
    stage.className = 'viz-stage';
    layout.appendChild(stage);

    const meta = document.createElement('div');
    meta.className = 'viz-meta';
    meta.innerHTML = `
      <div class="viz-summary"></div>
      <div class="viz-boxes"><h4>Boxes</h4><ol></ol></div>
    `;
    layout.appendChild(meta);

    const summaryEl = meta.querySelector('.viz-summary');
    const boxesList = meta.querySelector('ol');
    const positionEl = toolbar.querySelector('[data-role="position"]');
    const formatSelect = toolbar.querySelector('[data-control="format"]');
    const normalizeSelect = toolbar.querySelector('[data-control="normalize"]');

    const opts = { format: 'xywh', normalize: 'auto' };
    let cursor = 0;

    function updatePosition() {
      positionEl.textContent = `${cursor + 1} / ${groups.length}`;
    }

    async function renderCurrent() {
      stage.innerHTML = '';
      const canvas = document.createElement('canvas');
      stage.appendChild(canvas);

      const group = groups[cursor];
      const ref = group.imageRef || '';
      const refBase = basename(ref);
      const matchedFrame =
        (refBase && framesIndex.get(refBase)) ||
        (refBase && framesIndex.get(refBase.toLowerCase())) ||
        null;

      summaryEl.innerHTML = '';
      summaryEl.appendChild(makeSummaryRow('Reference', ref || '(none)'));
      summaryEl.appendChild(
        makeSummaryRow('Matched frame', matchedFrame ? matchedFrame.name : '(no match)'),
      );
      summaryEl.appendChild(makeSummaryRow('Boxes', String(group.boxes.length)));

      boxesList.innerHTML = '';
      group.boxes.slice(0, 80).forEach((box, idx) => {
        const li = document.createElement('li');
        const score = box.score !== null && box.score !== undefined ? ` (${Number(box.score).toFixed(2)})` : '';
        const coords = box.coords ? `[${box.coords.map((n) => Number(n).toFixed(1)).join(', ')}]` : '[]';
        li.innerHTML = `<strong>${idx + 1}. ${box.label || 'object'}</strong>${score} ${coords}`;
        boxesList.appendChild(li);
      });
      if (group.boxes.length > 80) {
        const more = document.createElement('li');
        more.textContent = `... ${group.boxes.length - 80} more`;
        boxesList.appendChild(more);
      }

      if (!matchedFrame) {
        stage.innerHTML = '<div class="empty">No matching frame in <code>frames/</code>. Upload one with the referenced name to render the overlay.</div>';
        return;
      }

      try {
        const url = await fetchPresignedDownload(matchedFrame.key);
        const image = await loadImage(url);
        drawBoxes(canvas, image, group.boxes, opts);
      } catch (err) {
        stage.innerHTML = `<div class="empty">${err.message || 'Failed to render image'}</div>`;
      }
    }

    function makeSummaryRow(label, value) {
      const div = document.createElement('div');
      div.innerHTML = `<strong style="color:var(--text)">${label}:</strong> <span style="color:var(--muted)">${value}</span>`;
      return div;
    }

    toolbar.querySelector('[data-action="prev"]').addEventListener('click', () => {
      cursor = (cursor - 1 + groups.length) % groups.length;
      updatePosition();
      renderCurrent();
    });
    toolbar.querySelector('[data-action="next"]').addEventListener('click', () => {
      cursor = (cursor + 1) % groups.length;
      updatePosition();
      renderCurrent();
    });
    formatSelect.addEventListener('change', () => {
      opts.format = formatSelect.value;
      renderCurrent();
    });
    normalizeSelect.addEventListener('change', () => {
      opts.normalize = normalizeSelect.value;
      renderCurrent();
    });

    updatePosition();
    renderCurrent();
  }

  /* ------------------------------------------------------------------ */
  /* Uploads                                                            */
  /* ------------------------------------------------------------------ */

  function makeUploadItem(file) {
    const li = document.createElement('li');
    li.className = 'upload-item progress';
    li.innerHTML = `
      <div class="row">
        <strong></strong>
        <span class="meta">queued</span>
      </div>
      <div class="progress-bar"><span></span></div>
    `;
    li.querySelector('strong').textContent = file.name;
    return li;
  }

  function fileMatchesCategory(cat, file) {
    const ext = (file.name.split('.').pop() || '').toLowerCase();
    return cat.extensions.includes(ext);
  }

  async function uploadFile(file) {
    const cat = getActiveCategory();
    if (!fileMatchesCategory(cat, file)) {
      showToast({
        title: 'Skipped',
        body: `${file.name} is not allowed in ${cat.label}`,
        variant: 'error',
      });
      return;
    }

    const item = makeUploadItem(file);
    els.uploadList.appendChild(item);
    els.uploadStatus.hidden = false;
    const meta = item.querySelector('.meta');
    const bar = item.querySelector('.progress-bar > span');

    try {
      meta.textContent = 'preparing';
      const presign = await api('/api/uploads/presign', {
        method: 'POST',
        body: JSON.stringify({
          category: cat.id,
          filename: file.name,
          content_type: file.type || 'application/octet-stream',
        }),
      });

      meta.textContent = 'uploading 0%';
      await xhrPut(presign.url, file, (percent) => {
        bar.style.width = `${percent}%`;
        meta.textContent = `uploading ${percent}%`;
      });
      bar.style.width = '100%';
      item.classList.remove('progress');
      item.classList.add('done');
      meta.textContent = 'uploaded';
      showToast({ title: 'Upload complete', body: file.name, variant: 'success' });
    } catch (err) {
      item.classList.remove('progress');
      item.classList.add('error');
      meta.textContent = err.message || 'failed';
      showToast({ title: 'Upload failed', body: `${file.name}: ${err.message}`, variant: 'error' });
    }
  }

  function xhrPut(url, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('PUT', url, true);
      xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress(Math.round((event.loaded / event.total) * 100));
        }
      });
      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error(`S3 returned HTTP ${xhr.status}`));
      });
      xhr.addEventListener('error', () => reject(new Error('Network error during upload')));
      xhr.addEventListener('abort', () => reject(new Error('Upload aborted')));
      xhr.send(file);
    });
  }

  async function handleFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;
    for (const file of files) {
      await uploadFile(file);
    }
    state.framesIndex = null;
    loadFiles();
  }

  /* ------------------------------------------------------------------ */
  /* YouTube ingest                                                     */
  /* ------------------------------------------------------------------ */

  const YT_ACTIVE_STATUSES = new Set(['queued', 'downloading', 'processing', 'uploading']);

  async function handleYoutubeSubmit(event) {
    event.preventDefault();
    const url = (els.youtubeUrl.value || '').trim();
    const filename = (els.youtubeFilename.value || '').trim();
    if (!url) return;

    els.youtubeSubmit.disabled = true;
    try {
      const job = await api('/api/youtube/download', {
        method: 'POST',
        body: JSON.stringify({ url, filename: filename || null }),
      });
      els.youtubeUrl.value = '';
      els.youtubeFilename.value = '';
      mergeYoutubeJob(job);
      renderYoutubeJobs();
      ensureYoutubePolling();
      showToast({ title: 'Queued', body: 'Download job started', variant: 'success' });
    } catch (err) {
      showToast({ title: 'Failed to queue', body: err.message, variant: 'error' });
    } finally {
      els.youtubeSubmit.disabled = false;
    }
  }

  function mergeYoutubeJob(job) {
    if (!job || !job.id) return;
    state.youtube.jobs.set(job.id, job);
  }

  async function refreshYoutubeJobs() {
    try {
      const data = await api('/api/youtube/jobs');
      const incoming = new Map();
      for (const job of data.jobs || []) incoming.set(job.id, job);
      state.youtube.jobs = incoming;
      renderYoutubeJobs();
      ensureYoutubePolling();
    } catch (err) {
      showToast({ title: 'Failed to load jobs', body: err.message, variant: 'error' });
    }
  }

  async function pollYoutubeJobs() {
    if (state.activeView !== 'tool' || state.activeToolId !== 'youtube') {
      stopYoutubePolling();
      return;
    }
    try {
      const data = await api('/api/youtube/jobs');
      const incoming = new Map();
      for (const job of data.jobs || []) incoming.set(job.id, job);
      const prev = state.youtube.jobs;
      state.youtube.jobs = incoming;
      for (const [id, job] of incoming.entries()) {
        const before = prev.get(id);
        if (before && before.status !== 'done' && job.status === 'done') {
          showToast({
            title: 'YouTube ready',
            body: `${job.filename || job.title || 'Video'} uploaded`,
            variant: 'success',
          });
          state.framesIndex = null;
          if (state.activeView === 'category' && state.activeCategoryId === job.category) {
            loadFiles();
          }
        } else if (before && before.status !== 'error' && job.status === 'error') {
          showToast({
            title: 'Download failed',
            body: job.error || 'yt-dlp error',
            variant: 'error',
          });
        }
      }
      renderYoutubeJobs();
    } catch (err) {
      // swallow; next tick will retry
      console.warn('youtube poll failed', err);
    }
    if (anyYoutubeActive()) {
      state.youtube.pollTimer = setTimeout(pollYoutubeJobs, 1500);
    } else {
      stopYoutubePolling();
    }
  }

  function anyYoutubeActive() {
    for (const job of state.youtube.jobs.values()) {
      if (YT_ACTIVE_STATUSES.has(job.status)) return true;
    }
    return false;
  }

  function ensureYoutubePolling() {
    if (state.youtube.pollTimer) return;
    if (!anyYoutubeActive()) return;
    state.youtube.pollTimer = setTimeout(pollYoutubeJobs, 1000);
  }

  function stopYoutubePolling() {
    if (state.youtube.pollTimer) {
      clearTimeout(state.youtube.pollTimer);
      state.youtube.pollTimer = null;
    }
  }

  function jobsByRecency() {
    return Array.from(state.youtube.jobs.values()).sort((a, b) => {
      return (b.started_at || '').localeCompare(a.started_at || '');
    });
  }

  function renderYoutubeJobs() {
    if (!els.youtubeJobList) return;
    const jobs = jobsByRecency();
    els.youtubeJobList.innerHTML = '';
    if (els.youtubeEmpty) els.youtubeEmpty.hidden = jobs.length !== 0;
    for (const job of jobs) els.youtubeJobList.appendChild(renderYoutubeRow(job));
  }

  function renderYoutubeRow(job) {
    const li = document.createElement('li');
    li.className = 'upload-item';
    if (job.status === 'done') li.classList.add('done');
    else if (job.status === 'error') li.classList.add('error');
    else li.classList.add('progress');

    const title = document.createElement('strong');
    title.textContent = job.title || job.filename || job.url;
    title.title = job.url;

    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = describeJob(job);

    const row = document.createElement('div');
    row.className = 'row';
    row.appendChild(title);
    row.appendChild(meta);

    const bar = document.createElement('div');
    bar.className = 'progress-bar';
    const fill = document.createElement('span');
    fill.style.width = `${Math.min(100, Math.max(0, Number(job.progress) || 0))}%`;
    bar.appendChild(fill);

    const sub = document.createElement('div');
    sub.className = 'youtube-row-sub';
    const subParts = [];
    if (job.filename) subParts.push(job.filename);
    if (job.total_bytes) {
      subParts.push(
        `${formatBytes(job.downloaded_bytes || 0)} / ${formatBytes(job.total_bytes)}`,
      );
    }
    if (job.speed) subParts.push(`${formatBytes(job.speed)}/s`);
    if (job.eta && job.status === 'downloading') subParts.push(`eta ${formatEta(job.eta)}`);
    sub.textContent = subParts.join('  ·  ');

    li.appendChild(row);
    li.appendChild(bar);
    if (sub.textContent) li.appendChild(sub);

    if (job.status === 'error' && job.error) {
      const errDiv = document.createElement('div');
      errDiv.className = 'youtube-row-error';
      errDiv.textContent = job.error;
      li.appendChild(errDiv);
    }

    const actions = document.createElement('div');
    actions.className = 'youtube-row-actions';

    if (job.status === 'done' && job.key) {
      const openBtn = document.createElement('button');
      openBtn.type = 'button';
      openBtn.className = 'ghost-button';
      openBtn.innerHTML = '<svg class="icon"><use href="#icon-eye"/></svg><span>Preview</span>';
      openBtn.addEventListener('click', () => previewYoutubeJob(job));
      actions.appendChild(openBtn);

      const downloadBtn = document.createElement('button');
      downloadBtn.type = 'button';
      downloadBtn.className = 'ghost-button';
      downloadBtn.innerHTML = '<svg class="icon"><use href="#icon-download"/></svg><span>Download</span>';
      downloadBtn.addEventListener('click', async () => {
        try {
          const url = await fetchPresignedDownload(job.key);
          const link = document.createElement('a');
          link.href = url;
          link.download = job.filename || '';
          document.body.appendChild(link);
          link.click();
          link.remove();
        } catch (err) {
          showToast({ title: 'Download failed', body: err.message, variant: 'error' });
        }
      });
      actions.appendChild(downloadBtn);
    }

    if (!YT_ACTIVE_STATUSES.has(job.status)) {
      const dismissBtn = document.createElement('button');
      dismissBtn.type = 'button';
      dismissBtn.className = 'ghost-button';
      dismissBtn.innerHTML = '<svg class="icon"><use href="#icon-x"/></svg><span>Clear</span>';
      dismissBtn.addEventListener('click', () => dismissYoutubeJob(job));
      actions.appendChild(dismissBtn);
    }

    if (actions.children.length) li.appendChild(actions);
    return li;
  }

  function describeJob(job) {
    const pct = `${Math.round(Number(job.progress) || 0)}%`;
    switch (job.status) {
      case 'queued':
        return 'queued';
      case 'downloading':
        return `downloading ${pct}`;
      case 'processing':
        return 'post-processing';
      case 'uploading':
        return 'uploading to s3';
      case 'done':
        return 'uploaded';
      case 'error':
        return 'error';
      default:
        return job.status;
    }
  }

  function formatEta(seconds) {
    const s = Number(seconds);
    if (!Number.isFinite(s) || s <= 0) return '--';
    if (s < 60) return `${Math.round(s)}s`;
    const m = Math.floor(s / 60);
    const r = Math.round(s % 60);
    return `${m}m${r}s`;
  }

  async function previewYoutubeJob(job) {
    if (!job.key) return;
    const cat = categoryById(job.category) || categories[0];
    openModal({
      title: job.filename || job.title || 'YouTube clip',
      subtitle: `${cat.label} · ${job.url}`,
      downloadKey: job.key,
    });
    try {
      const url = await fetchPresignedDownload(job.key);
      const video = document.createElement('video');
      video.controls = true;
      video.autoplay = true;
      video.src = url;
      els.modalBody.appendChild(video);
    } catch (err) {
      setModalError(err.message || String(err));
    }
  }

  async function dismissYoutubeJob(job) {
    try {
      await api(`/api/youtube/jobs/${encodeURIComponent(job.id)}`, { method: 'DELETE' });
      state.youtube.jobs.delete(job.id);
      renderYoutubeJobs();
    } catch (err) {
      showToast({ title: 'Failed to dismiss', body: err.message, variant: 'error' });
    }
  }

  /* ------------------------------------------------------------------ */
  /* Events                                                             */
  /* ------------------------------------------------------------------ */

  function bindEvents() {
    els.dropzone.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      els.fileInput.click();
    });
    els.chooseButton.addEventListener('click', (e) => {
      e.stopPropagation();
      els.fileInput.click();
    });
    els.fileInput.addEventListener('change', (e) => {
      handleFiles(e.target.files);
      e.target.value = '';
    });

    ['dragenter', 'dragover'].forEach((evt) => {
      els.dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        els.dropzone.classList.add('dragging');
      });
    });
    ['dragleave', 'drop'].forEach((evt) => {
      els.dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (evt === 'drop' || e.target === els.dropzone) {
          els.dropzone.classList.remove('dragging');
        }
      });
    });
    els.dropzone.addEventListener('drop', (e) => {
      const files = e.dataTransfer && e.dataTransfer.files;
      handleFiles(files);
    });

    els.refresh.addEventListener('click', () => {
      if (state.activeView === 'tool' && state.activeToolId === 'youtube') {
        refreshYoutubeJobs();
      } else {
        loadFiles();
      }
    });

    if (els.search) {
      els.search.addEventListener('input', (e) => {
        state.search = e.target.value || '';
        renderFiles(state.files);
      });
    }

    if (els.toolNav) {
      els.toolNav.querySelectorAll('[data-tool-id]').forEach((btn) => {
        btn.addEventListener('click', () => setActiveTool(btn.dataset.toolId));
      });
    }

    if (els.youtubeForm) {
      els.youtubeForm.addEventListener('submit', handleYoutubeSubmit);
    }

    els.modal.addEventListener('click', (e) => {
      const target = e.target.closest('[data-action="close"]');
      if (target) closeModal();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !els.modal.hidden) closeModal();
    });
    els.modalDownload.addEventListener('click', async () => {
      if (!state.activeDownloadKey) return;
      try {
        const url = await fetchPresignedDownload(state.activeDownloadKey);
        const link = document.createElement('a');
        link.href = url;
        link.download = '';
        document.body.appendChild(link);
        link.click();
        link.remove();
      } catch (err) {
        showToast({ title: 'Download failed', body: err.message, variant: 'error' });
      }
    });
  }

  async function loadInitialCounts() {
    await Promise.all(
      categories.map(async (cat) => {
        try {
          const data = await api(`/api/files?category=${encodeURIComponent(cat.id)}`);
          state.counts[cat.id] = (data.files || []).length;
        } catch (_) {}
      }),
    );
    updateSidebarCounts();
  }

  renderSidebar();
  renderHeader();
  bindEvents();
  loadFiles().then(() => loadInitialCounts());
})();
