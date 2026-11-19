/* Auto-DJ — UI logique. Vanilla ES2020+, aucun framework, aucun réseau externe.
   Contrat d'endpoints : docs/CONTRATS.md § app/server.py.
   Anti-crash : tout fetch sous try/catch, badge "perte de liaison" après 6 polls
   échoués, erreurs JS → toast rouge + console.error (plus rien de silencieux). */
'use strict';
const App = (() => {
  /* Cartographie des IDs par SCAN querySelectorAll : immunisé contre un getElementById /
     querySelector monkeypatché par une extension navigateur (bug réel constaté chez
     l'utilisateur : les 45 éléments existent, getElementById renvoie null pour 37 d'entre eux). */
  const idMap = new Map();
  const scanIds = () => document.querySelectorAll('[id]').forEach((n) => { if (n.id && !idMap.has(n.id)) idMap.set(n.id, n); });
  scanIds();
  const $ = (id) => {
    let n = idMap.get(id);
    if (!n) { scanIds(); n = idMap.get(id) || document.getElementById(id) || document.querySelector('[id="' + id + '"]'); }
    return n || null;
  };
  const missing = ['queue', 'bank', 'now', 'np-title', 'np-time', 'np-fade', 'np-duck', 'preview-badge',
    'search', 'url', 'btn-dl', 'dl-form', 'jobs', 'btn-play', 'ico-play', 'ico-pause',
    'btn-skip', 'wave', 'wave-flat', 'wave-flat-fill', 'time', 'vol', 'vol-val',
    'btn-duck', 'xf', 'xf-val', 'toasts', 'menu', 'modal-wrap', 'modal-sub',
    'btn-restore-yes', 'btn-restore-no', 'link-badge', 'col-bank', 'btn-import', 'file-input', 'import-status',
    'btn-auto', 'btn-fill', 'queue-stats', 'np-next', 'btn-clear',
    'tip-auto', 'tip-curve', 'tip-duck', 'tip-bank',
    'deck', 'np-label', 'preview-notice', 'btn-back-queue', 'xf-curve',
    'btn-bank-clear', 'btn-quit', 'shutdown', 'btn-reload']
    .filter((id) => !$(id)); // ⚠ $() sans argument = getElementById(undefined) → tout "manquant" → IIFE morte
  if (missing.length) {
    const all = [...document.querySelectorAll('[id]')];
    const sample = all.slice(0, 8).map((n) => n.tagName.toLowerCase() + '#' + (n.id || '∅')).join(' ');
    const diag = 'readyState=' + document.readyState +
      ' bodyChildren=' + (document.body ? document.body.childElementCount : -1) +
      ' idsInDom=' + all.length + ' sample=[' + sample + ']' +
      ' url=' + location.href;
    const msg = 'DOM incomplet (' + diag + ') — ' + missing.length + ' IDs manquants, liste en console (F12)';
    console.error('Auto-DJ: DOM incomplet (' + diag + ') IDs manquants:', missing.join(', '));
    const bb = $('boot-banner');
    if (bb) { bb.hidden = false; bb.textContent = msg; } // panne persistante = visible, pas un toast de 4 s
    return null;
  }

  /* Toute panne JS devient visible : toast rouge + bandeau de boot tant que l'init n'a pas réussi. */
  const bootFail = (m) => {
    const bb = $('boot-banner');
    if (bb && !window.AutoDJReady) { bb.hidden = false; bb.textContent = m; }
  };
  window.addEventListener('error', (e) => { const m = '⚠️ Erreur : ' + (e.message || e.type); toast(m, 'err'); bootFail(m); });
  window.addEventListener('unhandledrejection', (e) => { const m = '⚠️ Erreur réseau : ' + (e.reason?.message || e.reason); toast(m, 'err'); bootFail(m); });

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const fmt = (s) => {
    s = Math.max(0, Math.floor(Number(s) || 0));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  };
  const clamp01 = (v) => Math.max(0, Math.min(1, Number(v) || 0));
  const isDownload = (t) => /download/i.test(String(t && t.source)) ||
    String((t && t.path) || '').includes('/downloads/');
  const baseName = (p) => String(p || '').split(/[\\/]/).pop();

  /* ---------- État global ---------- */
  let S = { playing: null, view: null, queue: [], bank: [], downloading: [], settings: {}, preview: null };
  let dead = false;                // serveur arrêté (POST /api/quit) : plus aucun réseau
  let lastUiSig = '', lastBankSig = '', lastQSig = '';
  let failStreak = 0;
  const waveCache = new Map();     // path -> list[float] | [] (échec)
  let wavePath = '';
  let drag = null;                 // { path, from: 'queue'|'bank' }
  let query = '';
  let volTouch = 0, xfTouch = 0, curveTouch = 0;
  let volT = 0, xfT = 0, refreshT = 0;
  let restoreDecided = false;
  let restoreFocus = null;         // élément focalisé avant la modale (rendu après décision)
  let queueSet = new Set();        // chemins présents dans la file (badge banque, O(1))
  let prevQueue = new Set();       // chemins du rendu précédent (animation d'apparition)
  let lastNpPath = '';
  let posAt = 0;                   // instant du dernier pos serveur (extrapolation lissée)
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const autoQueue = () => Object.assign({ enabled: false, min: 3, fill: 10 }, S.settings && S.settings.auto_queue);
  const inQueue = (p) => S.queue.some((r) => r.path === p);

  const queueList = $('queue'), bankList = $('bank'), jobsBox = $('jobs'), toastsBox = $('toasts');
  const menu = $('menu'), modal = $('modal-wrap');
  const linkBadge = $('link-badge');

  /* ---------- Réseau ---------- */
  async function api(path, body) {
    try {
      const opt = body === undefined ? undefined : {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      };
      const r = await fetch(path, opt);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = r.status === 204 ? null : await r.json().catch(() => null);
      if (d && d.error) { // refus serveur (ex. file vide) : VISIBLE, jamais un silence trompeur
        toast(d.error === 'empty' ? 'File vide — ajoute des morceaux depuis la banque' : String(d.error), 'warn');
        return null;
      }
      return d;
    } catch (e) {
      console.error('Auto-DJ API', path, e);
      throw e;
    }
  }

  function toast(msg, kind) {
    if (!msg) return;
    const t = el('div', 'toast' + (kind ? ' ' + kind : ''), String(msg));
    toastsBox.appendChild(t);
    // max 4 visibles : les plus vieux sortent en fondu puis sont retirés du DOM
    [...toastsBox.children].slice(0, -4).forEach((old) => {
      if (old.classList.contains('out')) return;
      old.classList.add('out');
      setTimeout(() => old.remove(), 180);
    });
    setTimeout(() => {
      t.classList.add('out');
      setTimeout(() => t.remove(), 200);
    }, 4000);
  }

  const refresh = () => poll();

  /* ---------- Polling /api/state (1 s) ---------- */
  let polling = false, pollT = 0;
  async function poll() {
    if (dead || polling) return;
    polling = true;
    const wasOffline = failStreak > 0;
    try {
      const s = await api('/api/state');
      if (dead) return;              // arrêt demandé pendant la requête : ne rien réafficher
      failStreak = 0;
      linkBadge.hidden = true;
      applyState(s || {});
      if (wasOffline) lastUiSig = lastBankSig = lastQSig = ''; // re-render forcé après reprise
    } catch (_) {
      if (++failStreak >= 6) linkBadge.hidden = false; // badge discret, UI figée mais vivante
    } finally {
      polling = false;
    }
  }

  function applyState(s) {
    S = {
      playing: s.playing || null,
      queue: Array.isArray(s.queue) ? s.queue : [],
      bank: Array.isArray(s.bank) ? s.bank : [],
      downloading: Array.isArray(s.downloading) ? s.downloading : [],
      settings: s.settings || {},
      preview: (s.preview && s.preview.path) ? s.preview : null,
      restore_offer: s.restore_offer || null,
    };
    /* Vue d'affichage du deck. Pendant un crossfade naturel, `playing` décrit encore le
       SORTANT alors que l'entrant joue déjà : le deck doit montrer l'entrant (titre, temps,
       progression, waveform) et garder le badge FADE. `incoming` absent (backend ancien)
       → vue = playing, comportement inchangé. */
    S.view = (S.playing && S.playing.fading && S.playing.incoming)
      ? Object.assign({}, S.playing.incoming, { playing: S.playing.playing, fading: true })
      : S.playing;
    // Deux signatures : la banque (954 rangées) ne se reconstruit que si elle change
    // ou si la recherche s'applique — jamais pour un volume, un crossfade, un
    // téléchargement ou une file qui bouge (sinon ~9 000 nœuds + le focus clavier
    // repartaient à chaque tick). Le repère « déjà en file » est mis à jour en place.
    const qPaths = S.queue.map((t) => t.path);
    const qSig = JSON.stringify(qPaths);
    if (qSig !== lastQSig) { lastQSig = qSig; queueSet = new Set(qPaths); syncQueueBadges(); }
    const bankSig = JSON.stringify(S.bank);
    if (bankSig !== lastBankSig) {
      lastBankSig = bankSig;
      renderBank();
      renderBankClear();
    }
    const uiSig = JSON.stringify([S.queue, S.downloading, !!autoQueue().enabled,
      S.preview ? S.preview.path : '']);   // audition : la file rendue change d'état
    if (uiSig !== lastUiSig) {
      lastUiSig = uiSig;
      renderQueue();
      renderJobs();
      renderStats();
      renderBankClear();
    }

    const st = S.settings;
    const t = Date.now();
    if (typeof st.volume === 'number' && t - volTouch > 1500 && document.activeElement !== $('vol'))
      $('vol').value = Math.round(clamp01(st.volume) * 100);
    if (typeof st.crossfade === 'number' && t - xfTouch > 1500 && document.activeElement !== $('xf'))
      $('xf').value = Math.max(2, Math.min(16, st.crossfade));
    if (t - curveTouch > 1500 && document.activeElement !== $('xf-curve'))
      setCurveSelect(st.crossfade_curve);   // jamais écrasé pendant que l'utilisateur choisit

    $('vol-val').textContent = $('vol').value + '%';
    $('xf-val').textContent = Number($('xf').value).toFixed(1) + ' s';
    $('btn-duck').classList.toggle('on', !!st.ducking);
    renderAuto();

    // switch icône PLAY/PAUSE : playing = objet non nul (bool interne si fourni)
    const p = S.playing;
    const on = !!p && (typeof p.playing === 'boolean' ? p.playing : typeof p.path === 'string');
    renderDeckMode(on);
    // ⚠ SVGElement n'a PAS de propriété .hidden (IDL HTMLElement) → toggleAttribute obligatoire
    $('ico-play').toggleAttribute('hidden', on);   // en lecture → montrer l'icône pause
    $('ico-pause').toggleAttribute('hidden', !on);

    posAt = performance.now();   // origine de l'extrapolation de progression
    updateNowPlaying();
    if (!reduceMotion && p && p.path && p.playing !== false && !rafId) rafId = requestAnimationFrame(loopPaint);
    maybeRestore();
  }

  /* ---------- Rendu des listes ---------- */
  function trackOf(path) {
    if (!path) return { path: '', title: '' };
    return S.bank.find((t) => t.path === path) || S.queue.find((t) => t.path === path) ||
      { path, title: baseName(path).replace(/\.[^.]+$/, ''), artist: '', dur: 0 };
  }

  function titleBlock(t) {
    const main = el('div', 't-main');
    main.appendChild(el('div', 't-title', t.title || baseName(t.path).replace(/\.[^.]+$/, '') || t.path));
    if (t.artist) main.appendChild(el('div', 't-artist', t.artist));
    return main;
  }

  function rowMenuBtn(t, from) {
    const b = el('button', 'row-btn', '⋮');
    b.type = 'button';
    b.setAttribute('aria-label', 'Actions pour ' + (t.title || t.path));
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      const r = b.getBoundingClientRect();
      openRowMenu(t, from, r.right, r.bottom + 2);
    });
    b.addEventListener('pointerdown', (e) => e.stopPropagation());
    return b;
  }

  function queueRow(t, i) {
    const li = el('li', 'row');
    li.draggable = true;
    li.tabIndex = 0;
    li.dataset.path = t.path;
    li.setAttribute('role', 'button');
    li.setAttribute('aria-label', 'File ' + (i + 1) + ' : ' + (t.title || t.path));

    li.append(el('span', 'q-num', String(i + 1)), titleBlock(t), el('span', 't-dur', fmt(t.dur)));
    li.appendChild(rowMenuBtn(t, 'queue'));

    const x = el('button', 'row-btn x', '✕');
    x.type = 'button';
    x.setAttribute('aria-label', 'Retirer de la file : ' + (t.title || t.path));
    x.title = 'Retirer de la file (ou Suppr sur la rangée)';
    x.addEventListener('click', (e) => { e.stopPropagation(); removeFromQueue(t.path); });
    x.addEventListener('pointerdown', (e) => e.stopPropagation());
    li.appendChild(x);

    if (t.path === flashPath && performance.now() - flashAt < 1200) li.classList.add('flash');

    bindDrag(li, t, 'queue');
    bindContext(li, t, 'queue');
    return li;
  }

  function bankRow(t) {
    const li = el('li', 'row bank-row');
    li.draggable = true;
    li.tabIndex = 0;
    li.dataset.path = t.path;
    li.setAttribute('role', 'button');
    li.setAttribute('aria-label', 'Banque : ' + (t.title || t.path));
    // Geste premier-clic : clic = insérer juste après le titre en cours (différé pour ne pas doubler un double-clic = écoute)
    li.title = 'Clic : ajouter juste après le titre en cours · Double-clic : écouter';

    const title = t.title || baseName(t.path).replace(/\.[^.]+$/, '') || t.path;
    const line = el('div', 't-line');
    const tt = el('span', 't-t', title);
    tt.title = title;
    line.appendChild(tt);
    if (t.artist) {
      line.appendChild(el('span', 't-sep', ' — '));
      const ta = el('span', 't-a', t.artist);
      ta.title = t.artist;
      line.appendChild(ta);
    }
    li.appendChild(line);

    if (isDownload(t)) {
      const b = el('span', 'badge-src', '🌐');
      b.title = 'Téléchargé (downloads/)';
      li.appendChild(b);
    }
    li.append(el('span', 't-dur', fmt(t.dur)));
    if (queueSet.has(t.path)) {          // déjà en file : repère discret, pas de re-render dédié
      const ok = el('span', 'in-q', '✓');
      ok.title = 'Déjà dans la file';
      li.appendChild(ok);
    }

    const add = el('button', 'row-btn append', '＋ File');
    add.type = 'button';
    add.title = 'Ajouter en fin de file';
    add.setAttribute('aria-label', 'Ajouter à la file : ' + title);
    add.addEventListener('click', (e) => { e.stopPropagation(); closeMenu(); appendQueue(t.path); });
    add.addEventListener('pointerdown', (e) => e.stopPropagation());

    const lk = el('button', 'row-btn listen', '▶');
    lk.type = 'button';
    lk.title = 'Écouter (la file reprend après)';
    lk.setAttribute('aria-label', 'Écouter : ' + title);
    lk.addEventListener('click', (e) => { e.stopPropagation(); closeMenu(); previewTrack(t); });
    lk.addEventListener('pointerdown', (e) => e.stopPropagation());

    li.append(add, lk);
    li.appendChild(rowMenuBtn(t, 'bank'));

    // les boutons stopPropagent ; clic nu = insertion après le titre courant (annulée par dblclick)
    let clickT = 0;
    li.addEventListener('click', (e) => {
      if (e.button !== 0 || (e.target.closest && e.target.closest('.row-btn'))) return;
      clearTimeout(clickT);
      clickT = setTimeout(() => { addNext(t); }, 240);
    });
    li.addEventListener('dblclick', (e) => {
      if (e.target.closest && e.target.closest('.row-btn')) return;
      clearTimeout(clickT);
      previewTrack(t);
    });

    bindDrag(li, t, 'bank');
    bindContext(li, t, 'bank');
    return li;
  }

  function renderQueue() {
    queueList.textContent = '';
    const before = prevQueue;     // diff léger : seules les nouvelles rangées s'animent
    if (S.preview && S.preview.path) {
      // Audition : la file est suspendue côté moteur (le serveur renvoie queue=[]).
      // On l'affiche comme telle — jamais comme une file vide — avec le retour direct.
      prevQueue = new Set();
      const e = el('li', 'empty-state suspended');
      e.appendChild(el('div', 'empty-txt', 'File suspendue pendant l\'écoute.'));
      const back = el('button', 'ghost-btn', '← Revenir à la file');
      back.type = 'button';
      back.title = 'Arrêter l\'écoute et restaurer la file (Échap)';
      back.addEventListener('click', backToQueue);
      e.appendChild(back);
      queueList.appendChild(e);
      return;
    }
    if (!S.queue.length) {
      prevQueue = new Set();
      const e = el('li', 'empty-state');
      e.appendChild(el('div', 'empty-txt', 'File vide — cliquez un morceau de la banque (ou glissez-le ici).'));
      const btns = el('div', 'empty-btns');
      if (S.bank.length) {
        const b = el('button', 'ghost-btn', 'Remplir la file');
        b.type = 'button';
        b.title = 'Ajoute 10 morceaux choisis dans la banque';
        b.addEventListener('click', () => fillQueue());
        btns.appendChild(b);
      }
      const imp = el('button', 'ghost-btn', 'Importer');
      imp.type = 'button';
      imp.title = 'Importer des fichiers audio locaux';
      imp.addEventListener('click', () => $('btn-import').click());
      btns.appendChild(imp);
      e.appendChild(btns);
      if (autoQueue().enabled) e.appendChild(el('div', 'empty-auto', 'Auto : la file se remplira au démarrage'));
      queueList.appendChild(e);
      return;
    }
    S.queue.forEach((t, i) => {
      const li = queueRow(t, i);
      if (!before.has(t.path) && !reduceMotion) li.classList.add('row-in');
      queueList.appendChild(li);
    });
    prevQueue = new Set(S.queue.map((t) => t.path));
  }

  function renderBank() {
    bankList.textContent = '';
    const q = query.toLowerCase();
    const list = S.bank.filter((t) =>
      !q || String(t.title || '').toLowerCase().includes(q) || String(t.artist || '').toLowerCase().includes(q));
    if (!list.length) {
      const e = el('li', 'empty-state');
      e.textContent = S.bank.length ? 'Aucun résultat pour cette recherche.' : 'Banque vide — cliquez « ＋ Importer » ou collez une URL YouTube.';
      bankList.appendChild(e);
      return;
    }
    list.forEach((t) => bankList.appendChild(bankRow(t)));
  }

  /* Repère « déjà dans la file » : mise à jour en place (pas de reconstruction de
     la banque quand la file bouge — 954 rangées × ~9 nœuds à chaque tick). */
  function syncQueueBadges() {
    for (const li of bankList.children) {
      const p = li.dataset.path;
      if (!p) continue;
      const on = queueSet.has(p);
      const b = li.querySelector('.in-q');
      if (on === !!b) continue;
      if (!on) { b.remove(); continue; }
      const mark = el('span', 'in-q', '✓');
      mark.title = 'Déjà dans la file';
      const btn = li.querySelector('.row-btn');
      li.insertBefore(mark, btn);
    }
  }

  function renderJobs() {    // Bande fine : une seule ligne, hauteur figée par CSS (repliée quand inactive)
    jobsBox.textContent = '';
    const js = S.downloading;
    jobsBox.hidden = !js.length;
    if (!js.length) return;
    jobsBox.appendChild(el('span', 'spin'));
    const f = js[0];
    const txt = js.length === 1
      ? (f.title || f.url || 'téléchargement') + (f.status ? ' · ' + f.status : '')
      : js.length + ' téléchargements en cours…';
    const s = el('span', 'job-text', txt);
    s.title = txt;
    jobsBox.appendChild(s);
  }

  /* ---------- Stats de file + auto-file ---------- */
  function renderStats() {
    if (S.preview && S.preview.path) {           // audition : la file existe, elle est juste en attente
      $('queue-stats').textContent = 'suspendue pendant l\'écoute';
      return;
    }
    const n = S.queue.length;
    if (!n) { $('queue-stats').textContent = 'file vide'; return; }
    const mins = Math.round(S.queue.reduce((a, t) => a + (Number(t.dur) || 0), 0) / 60);
    const h = Math.floor(mins / 60), m = mins % 60;
    const dur = h ? h + ' h' + (m ? ' ' + String(m).padStart(2, '0') : '') : Math.max(1, m) + ' min';
    $('queue-stats').textContent = n + ' morceau' + (n > 1 ? 'x' : '') + ' · ' + dur;
  }

  function renderAuto() {
    const on = !!autoQueue().enabled;
    const b = $('btn-auto');
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
    $('btn-fill').disabled = !S.bank.length;
  }

  /* ---------- Deck : direct / audition ---------- */
  const CURVES = { linear: 'Linéaire', equal_power: 'Égal-puissance', smooth: 'Douce', cut: 'Coupe' };

  function setCurveSelect(v) {
    const sel = $('xf-curve');
    const val = CURVES[v] ? v : 'equal_power';
    if (sel.value !== val) sel.value = val;
  }

  function renderDeckMode(playing) {
    const audition = !!(S.preview && S.preview.path);
    const deck = $('deck');
    deck.classList.toggle('audition', audition);
    deck.classList.toggle('playing', !!playing && !audition);
    $('np-label').textContent = audition ? 'AUDITION' : 'EN DIRECT';
    $('btn-back-queue').hidden = !audition;
    $('preview-notice').hidden = !audition;
  }

  function fillQueue() {
    api('/api/queue/auto', { count: 10 })
      .then((d) => {
        if (!d) return;                       // refus serveur : déjà toasté par api()
        toast('+' + (Number(d.added) || 0) + ' morceaux ajoutés');
        refresh();
      })
      .catch(() => toast('Remplissage auto indisponible (serveur à mettre à jour)', 'warn'));
  }

  function actAuto() {
    const on = !autoQueue().enabled;
    api('/api/settings', { auto_queue: { enabled: on } })
      .then((d) => {
        if (!d) return;
        S.settings.auto_queue = Object.assign(autoQueue(), { enabled: on });
        renderAuto();
        refresh();
      })
      .catch(() => toast('Auto-file indisponible (serveur à mettre à jour)', 'warn'));
  }

  /* ---------- Drag & drop ---------- */
  function clearDropMarks() {
    for (const n of document.querySelectorAll('.drop-above, .drop-below')) n.classList.remove('drop-above', 'drop-below');
    queueList.classList.remove('drop-end');
  }

  function postQueue(paths) {
    // Audition : S.queue est vide (file suspendue côté serveur) — un POST ici
    // écraserait la vraie file. On renvoie d'abord vers elle.
    if (S.preview && S.preview.path) {
      toast('Audition en cours — « Revenir à la file » d\'abord', 'warn');
      return false;
    }
    // feedback immédiat, le serveur fait foi ensuite
    S.queue = paths.map(trackOf);
    renderQueue();
    api('/api/queue', { paths }).then(refresh)
      .catch(() => { flashPath = ''; toast('Impossible de mettre à jour la file', 'warn'); refresh(); });
    return true;
  }

  let flashPath = '', flashAt = 0;
  const flash = (p) => { flashPath = p; flashAt = performance.now(); };
  const appendQueue = (p) => {
    if (inQueue(p)) { toast('Déjà dans la file : ' + (trackOf(p).title || baseName(p))); return; }
    flash(p);                                       // AVANT postQueue : le rendu optimiste flashe
    if (!postQueue([...S.queue.map((r) => r.path), p])) { flashPath = ''; return; } // refus déjà toasté
    toast('+ Fin de file : ' + (trackOf(p).title || baseName(p)));
  };

  function removeFromQueue(path) {
    if (S.preview && S.preview.path) {           // file suspendue : on ne touche à rien
      toast('Audition en cours — revenez à la file d\'abord', 'warn');
      return;
    }
    const li = [...queueList.children].find((n) => n.dataset.path === path);
    const anim = li && !reduceMotion;
    if (anim) li.classList.add('row-out');          // fondu + repli, purement visuel
    api('/api/queue/remove', { path })
      .then(() => {
        toast('Retiré de la file : ' + (trackOf(path).title || baseName(path)));
        setTimeout(refresh, anim ? 150 : 0);
      })
      .catch(() => {
        if (li) li.classList.remove('row-out');
        toast('Impossible de retirer le morceau', 'warn');
      });
  }

  /* ---------- Vider la file : confirmation en deux temps (3 s) ---------- */
  let clearArmed = 0, clearT = 0;
  function disarmClear() {
    clearArmed = 0;
    clearTimeout(clearT);
    const b = $('btn-clear');
    b.classList.remove('armed');
    b.textContent = 'Vider';
  }

  function clearQueue() {
    if (S.preview && S.preview.path) {           // file suspendue : ne JAMAIS vider
      toast('Audition en cours — revenez à la file d\'abord', 'warn');
      return;
    }
    if (!S.queue.length) { toast('La file est déjà vide'); return; }
    S.queue = [];                                // feedback immédiat, le serveur fait foi
    renderQueue();
    renderStats();
    api('/api/queue', { paths: [] })              // une seule requête
      .then(() => {
        toast('File vidée');
        if (autoQueue().enabled) toast('AUTO la re-remplira au prochain morceau');
        refresh();
      })
      .catch(() => { toast('Impossible de vider la file', 'warn'); refresh(); });
  }

  $('btn-clear').addEventListener('click', () => {
    if (S.preview && S.preview.path) {
      toast('Audition en cours — revenez à la file d\'abord', 'warn');
      return;
    }
    if (clearArmed) { disarmClear(); clearQueue(); return; }
    if (!S.queue.length) { toast('La file est déjà vide'); return; }
    clearArmed = 1;
    const b = $('btn-clear');
    b.classList.add('armed');
    b.textContent = 'Confirmer ?';
    clearT = setTimeout(disarmClear, 3000);
  });

  /* ---------- Vider les imports (downloads/) : confirmation en deux temps (3 s) ---------- */
  let bankArmed = 0, bankT = 0;
  function disarmBankClear() {
    bankArmed = 0;
    clearTimeout(bankT);
    const b = $('btn-bank-clear');
    if (!b) return;
    b.classList.remove('armed');
    b.textContent = 'Vider imports';
  }

  /* Rien à vider ou téléchargement en cours → bouton neutralisé, raison dans le title. */
  function renderBankClear() {
    const b = $('btn-bank-clear');
    if (!b) return;
    const busy = S.downloading.length > 0, empty = !S.bank.length;
    b.disabled = busy || empty;
    b.title = busy
      ? 'Téléchargement en cours — attendez la fin avant de vider les imports'
      : empty
        ? 'Banque vide — aucun import à supprimer'
        : 'Supprime les fichiers importés et téléchargés (dossier downloads/). Votre dossier musique personnel n\'est jamais modifié.';
    if (bankArmed && b.disabled) disarmBankClear();
  }

  /* Pas via api() : un 409 doit afficher le message du serveur, pas « HTTP 409 ». */
  function clearBank() {
    const audition = !!(S.preview && S.preview.path);
    fetch('/api/bank/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .then(async (r) => {
        const d = await r.json().catch(() => null);
        if (r.status === 409) { toast((d && d.error) || 'Téléchargement en cours', 'warn'); return; }
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const n = Number(d && d.removed) || 0;
        toast('Banque vidée — ' + n + ' fichier(s) supprimé(s)' +
          (audition ? ' · audition coupée si son fichier était un import' : ''));
        refresh();
      })
      .catch((e) => {
        console.error('Auto-DJ bank/clear', e);
        toast('Impossible de vider la banque', 'warn');
      });
  }

  $('btn-bank-clear').addEventListener('click', () => {
    const b = $('btn-bank-clear');
    if (b.disabled) return;
    if (bankArmed) { disarmBankClear(); clearBank(); return; }   // une seule requête
    bankArmed = 1;
    b.classList.add('armed');
    b.textContent = 'Confirmer ?';
    bankT = setTimeout(disarmBankClear, 3000);
  });

  /* Insertion juste après le titre en cours. Résout true si le serveur a accepté. */
  function insertNext(path) {
    flash(path);
    return api('/api/queue/insert-next', { path })
      .then((d) => {
        if (!d) { flashPath = ''; return false; }   // refus serveur déjà toasté
        refresh();
        return true;
      })
      .catch(() => { flashPath = ''; toast('Impossible de placer la piste', 'warn'); return false; });
  }

  /* Clic banque : insère après le titre courant, avec anti-doublon et toast explicite. */
  function addNext(t) {
    const title = t.title || baseName(t.path) || t.path;
    if (inQueue(t.path)) { toast('Déjà dans la file : ' + title); return; }
    const empty = !S.queue.length;
    insertNext(t.path).then((ok) => {
      if (ok) toast((empty ? 'Ajouté en tête de file : ' : 'Ajouté juste après le titre en cours : ') + title);
    });
  }

  function bindDrag(li, t, from) {
    li.addEventListener('dragstart', (e) => {
      drag = { path: t.path, from };
      try {
        e.dataTransfer.setData('text/plain', t.path);
        e.dataTransfer.effectAllowed = 'move';
      } catch (_) { /* certains navigateurs restrictifs : drag interne marche quand même */ }
      li.classList.add('dragging');
    });
    li.addEventListener('dragend', () => {
      drag = null;
      li.classList.remove('dragging');
      clearDropMarks();
    });

    if (from !== 'queue') return;
    // Les rangées de la file ne sont des cibles que pour un réordonnancement interne.
    li.addEventListener('dragover', (e) => {
      if (!drag || drag.from !== 'queue' || drag.path === t.path) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const r = li.getBoundingClientRect();
      const before = e.clientY < r.top + r.height / 2;
      li.classList.toggle('drop-above', before);
      li.classList.toggle('drop-below', !before);
    });
    li.addEventListener('dragleave', () => li.classList.remove('drop-above', 'drop-below'));
    li.addEventListener('drop', (e) => {
      if (!drag || drag.from !== 'queue' || drag.path === t.path) return;
      e.preventDefault();
      e.stopPropagation(); // ne pas remonter au conteneur (append)
      const r = li.getBoundingClientRect();
      const before = e.clientY < r.top + r.height / 2;
      const paths = S.queue.map((x) => x.path);
      const at = paths.indexOf(drag.path);
      if (at < 0) return;
      paths.splice(at, 1);
      let to = paths.indexOf(t.path);
      if (to < 0) return;
      if (!before) to += 1;
      paths.splice(to, 0, drag.path);
      postQueue(paths);
    });
  }

  // Conteneur file : cible pour l'append (bank→file) et le move-to-end (file dans le vide).
  queueList.addEventListener('dragover', (e) => {
    if (!drag) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    queueList.classList.toggle('drop-end', drag.from === 'bank');
  });
  queueList.addEventListener('dragleave', () => queueList.classList.remove('drop-end'));
  queueList.addEventListener('drop', (e) => {
    e.preventDefault();
    clearDropMarks();
    if (!drag) return;
    const d = drag; drag = null;
    if (d.from === 'bank') {
      appendQueue(d.path); // append via paths complet, ordre conservé
      return;
    }
    if (!S.queue.length) return;
    const paths = S.queue.map((r) => r.path);
    const at = paths.indexOf(d.path);
    if (at < 0) return;
    paths.splice(at, 1);
    paths.push(d.path);
    postQueue(paths);
  });

  /* ---------- Menu contextuel ---------- */
  function openRowMenu(t, from, x, y) {
    menu.textContent = '';
    const mk = (label, act) => {
      const b = el('button', 'menu-item', label);
      b.type = 'button';
      b.setAttribute('role', 'menuitem');
      b.addEventListener('click', () => { closeMenu(); act(); });
      menu.appendChild(b);
    };
    mk('Placer en piste suivante', () => insertNext(t.path));
    if (from === 'bank') {
      mk('Ajouter à la fin de la file', () => appendQueue(t.path));
      mk('Écouter (double-clic)', () => previewTrack(t));
    }
    if (from === 'queue') mk('Retirer de la file', () => removeFromQueue(t.path));
    menu.hidden = false;
    menu.style.left = Math.min(x, innerWidth - menu.offsetWidth - 8) + 'px';
    menu.style.top = Math.min(y, innerHeight - menu.offsetHeight - 8) + 'px';
    const first = menu.querySelector('button');
    if (first) first.focus();
  }
  function closeMenu() { menu.hidden = true; }
  document.addEventListener('click', (e) => {
    if (!menu.hidden && !menu.contains(e.target)) closeMenu();
  });
  menu.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMenu(); });

  function bindContext(li, t, from) {
    li.addEventListener('contextmenu', (e) => {
      if (drag) return;
      e.preventDefault();
      closeMenu();
      openRowMenu(t, from, e.clientX, e.clientY);
    });
    li.addEventListener('keydown', (e) => {
      if (from === 'queue' && e.key === 'Delete') {   // Suppr sur une rangée focalisée
        e.preventDefault();
        e.stopPropagation();
        removeFromQueue(t.path);
        return;
      }
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const r = li.getBoundingClientRect();
      openRowMenu(t, from, r.right - 16, r.top + 4);
    });
  }

  /* ---------- NOW PLAYING + waveform ---------- */
  function setNpTitle(text) {
    const n = $('np-title');
    if (n.textContent === text) return;
    n.textContent = text;
    if (reduceMotion) return;
    n.classList.remove('swap');
    void n.offsetWidth;              // relance l'animation (un seul changement à la fois)
    n.classList.add('swap');
  }

  function setNpNext() {
    const box = $('np-next');
    if (S.preview && S.preview.path) { box.hidden = true; return; }  // file suspendue : pas de « à suivre »
    // Pendant un fade la piste entrante est déjà affichée sur le deck mais reste S.queue[0] :
    // on montre celle d'après, sinon « À suivre » répète le titre du deck.
    const shown = S.view || S.playing;
    let next = S.queue[0];
    if (next && shown && next.path === shown.path) next = S.queue[1];
    box.hidden = !next;
    box.textContent = 'À suivre : ' + (next ? (next.title || baseName(next.path)) : '—');
  }

  /* Scroll doux vers la rangée du titre courant, uniquement si elle sort de la vue. */
  function scrollToPlaying(path) {
    const li = [...queueList.children].find((n) => n.dataset.path === path);
    if (!li) return;
    const r = li.getBoundingClientRect(), box = queueList.getBoundingClientRect();
    if (r.top >= box.top && r.bottom <= box.bottom) return;   // déjà visible : aucun scroll forcé
    li.scrollIntoView({ block: 'nearest', behavior: reduceMotion ? 'auto' : 'smooth' });
  }

  function updateNowPlaying() {
    const np = S.playing;          // état moteur (audition, badge)
    const v = S.view || S.playing;  // état AFFICHÉ (entrant pendant un fade)
    const npTime = $('np-time');
    const fade = $('np-fade'), duckB = $('np-duck');
    setNpNext();

    /* Audition : le deck montre le morceau écouté, pas la piste sortante. */
    const pvPath = S.preview && S.preview.path ? S.preview.path : '';
    if (pvPath) {
      const pvInfo = trackOf(pvPath);
      setNpTitle(pvInfo.title || baseName(pvPath));
      fade.hidden = true;
      duckB.hidden = !S.settings.ducking;
      $('preview-badge').hidden = false;
      if (!np || np.path !== pvPath) {
        // pas encore la piste moteur (fade sortant) : aucune progression à afficher
        npTime.textContent = 'audition…';
        $('time').textContent = '0:00 · 0:00';
        wavePath = '';
        waveFlat(true, 0);
        drawWave(0);
        return;
      }
      lastNpPath = pvPath;
    }

    if (!v || !v.path) {
      setNpTitle('Aucune lecture');
      npTime.textContent = '— silence assuré';
      fade.hidden = duckB.hidden = true;
      $('preview-badge').hidden = !(S.preview && S.preview.path);
      wavePath = '';
      waveFlat(true, 0);            // ligne de base visible : le deck reste lisible à l'arrêt
      $('time').textContent = '0:00 · 0:00';
      drawWave(0);
      lastNpPath = '';
      return;
    }
    const info = trackOf(v.path);
    setNpTitle(info.title || baseName(v.path));
    if (v.path !== lastNpPath) { lastNpPath = v.path; scrollToPlaying(v.path); }
    const pos = posNow(), dur = Number(v.dur) || 0;
    npTime.textContent = fmt(pos) + ' / ' + fmt(dur);
    $('time').textContent = fmt(pos) + ' · ' + fmt(dur);
    fade.hidden = !v.fading;
    duckB.hidden = !S.settings.ducking;
    $('preview-badge').hidden = !(S.preview && S.preview.path);

    if (v.path !== wavePath) {
      wavePath = v.path;
      if (!waveCache.has(wavePath)) fetchWave(wavePath);
    }
    const data = waveCache.get(wavePath);
    const frac = clamp01(dur > 0 ? pos / dur : 0);
    if (data && data.length) waveFlat(false, frac); else waveFlat(true, frac);
    drawWave(frac);
  }

  /* Progression lissée entre 2 ticks SSE : extrapolation ~11 fps, coupée sans lecture.
     Aucune fuite : la boucle rend la main dès que la vue disparaît.
     Bornée à [0, dur] : entre deux polls, jamais de barre à 100 % par extrapolation. */
  let rafId = 0, paintAt = 0;
  function posNow() {
    const v = S.view;
    if (!v || !v.path) return 0;
    const dur = Number(v.dur) || 0;
    const base = Math.max(0, Number(v.pos) || 0);
    if (v.playing === false) return dur > 0 ? Math.min(dur, base) : base;   // pause : pas d'extrapolation
    const p = base + (performance.now() - posAt) / 1000;
    return dur > 0 ? Math.max(0, Math.min(dur, p)) : Math.max(0, p);
  }
  function paintProgress() {
    const v = S.view;
    if (!v || !v.path) return;
    const dur = Number(v.dur) || 0;
    const frac = clamp01(dur > 0 ? posNow() / dur : 0);
    const data = waveCache.get(wavePath);
    if (data && data.length) waveFlat(false, frac); else waveFlat(true, frac);
    drawWave(frac);
  }
  function loopPaint() {
    rafId = 0;
    const v = S.view;
    if (!v || !v.path || v.playing === false) return;  // arrêt / pause → boucle rendue
    const now = performance.now();
    if (now - paintAt >= 90) { paintAt = now; paintProgress(); }
    rafId = requestAnimationFrame(loopPaint);
  }

  async function fetchWave(path) {
    try {
      const r = await fetch('/api/waveform?path=' + encodeURIComponent(path));
      if (!r.ok) throw new Error('HTTP ' + r.status);
      let arr = await r.json();
      if (!Array.isArray(arr)) arr = arr && Array.isArray(arr.data) ? arr.data : []; // tolérance d'enveloppe
      if (waveCache.size > 200) waveCache.delete(waveCache.keys().next().value);
      waveCache.set(path, arr);
      if (path === wavePath) updateNowPlaying();
    } catch (e) {
      console.error('Auto-DJ waveform', e);
      waveCache.set(path, []); // fallback barre plate
      if (path === wavePath) updateNowPlaying();
    }
  }

  function waveFlat(on, frac) {
    $('wave-flat').hidden = !on;
    if (on) $('wave-flat-fill').style.width = (clamp01(frac) * 100).toFixed(1) + '%';
  }

  function drawWave(frac) {
    const c = $('wave');
    const w = c.clientWidth, h = c.clientHeight;
    if (w < 8 || h < 8) return;
    const dpr = window.devicePixelRatio || 1;
    const pw = Math.round(w * dpr), ph = Math.round(h * dpr);
    if (c.width !== pw || c.height !== ph) { c.width = pw; c.height = ph; }
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const data = waveCache.get(wavePath);
    const v = S.view;
    if (!data || !data.length || !v || !v.path) return;

    const dur = Number(v.dur) || 0;
    const f = frac != null ? clamp01(frac) : clamp01(dur > 0 ? (Number(v.pos) || 0) / dur : 0);
    const gap = 2, bw = (w - gap) / Math.max(1, data.length), mid = h / 2;
    const bars = (color) => {
      ctx.fillStyle = color;
      for (let i = 0; i < data.length; i++) {
        const v = clamp01(data[i]);
        const bh = Math.max(1.5, v * (h - 5));
        ctx.fillRect(i * bw + gap / 2, mid - bh / 2, Math.max(1, bw - gap), bh);
      }
    };
    bars('#3a4048');          // a venir
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, f * w, h); ctx.clip();
    bars('#34d399');          // joue / joué
    ctx.restore();
    if (f > 0 && f < 1) {
      ctx.fillStyle = '#e6e9ee';
      ctx.fillRect(f * w - 1, 0, 2, h);
    }
  }
  addEventListener('resize', () => drawWave());

  /* ---------- Contrôles ---------- */
  const actPlay = () => api('/api/play', {}).catch(() => toast('Serveur injoignable', 'warn'));
  const actSkip = () => api('/api/skip', {}).catch(() => toast('Serveur injoignable', 'warn'));
  const actDuck = () => api('/api/duck', {}).catch(() => toast('Serveur injoignable', 'warn'));
  $('btn-play').addEventListener('click', actPlay);
  $('btn-skip').addEventListener('click', actSkip);
  $('btn-duck').addEventListener('click', actDuck);
  $('btn-auto').addEventListener('click', actAuto);
  $('btn-fill').addEventListener('click', fillQueue);

  $('vol').addEventListener('input', () => {
    $('vol-val').textContent = $('vol').value + '%';
    volTouch = Date.now();
    clearTimeout(volT);
    volT = setTimeout(() => api('/api/volume', { value: Number($('vol').value) / 100 }).catch(() => {}), 100);
  });

  $('xf').addEventListener('input', () => {
    const v = Number($('xf').value);
    $('xf-val').textContent = v.toFixed(1) + ' s';
    xfTouch = Date.now();
    clearTimeout(xfT);
    xfT = setTimeout(() => api('/api/settings', { crossfade: Math.round(v * 10) / 10 }).catch(() => {}), 200);
  });

  /* Courbe de crossfade : appliquée côté moteur (le serveur renvoie la valeur retenue). */
  $('xf-curve').addEventListener('change', () => {
    const v = $('xf-curve').value;
    curveTouch = Date.now();
    api('/api/settings', { crossfade_curve: v })
      .then((d) => {
        if (!d) return;
        toast('Courbe : ' + (CURVES[d.crossfade_curve] || CURVES[v] || v));
        refresh();
      })
      .catch(() => toast('Courbe indisponible (serveur à mettre à jour)', 'warn'));
  });

  /* ---------- Téléchargement ---------- */
  function submitDownload() {
    const input = $('url');
    const v = input.value.trim();
    if (!v) {
      input.classList.remove('shake');
      void input.offsetWidth; // reflow pour relancer l'animation
      input.classList.add('shake');
      toast('Colle d\'abord un lien YouTube dans le champ, puis Télécharger');
      input.focus();
      return;
    }
    api('/api/download', { url: v })
      .then(() => { input.value = ''; toast('Téléchargement lancé'); refresh(); })
      .catch(() => toast('Échec du lancement du téléchargement', 'warn'));
  }
  $('dl-form').addEventListener('submit', (e) => { e.preventDefault(); submitDownload(); });
  $('url').addEventListener('input', () => $('url').classList.remove('shake'));

  /* ---------- Recherche ---------- */
  $('search').addEventListener('input', () => {
    query = $('search').value.trim();
    renderBank();
  });

  /* ---------- Écoute (preview) : POST /api/preview {path} ----------
     La file repart toute seule à la fin côté serveur ; ici : toast + badge. */
  function previewTrack(t) {
    api('/api/preview', { path: t.path })
      .then((d) => {
        if (!d) return;
        toast('Écoute : ' + (t.title || baseName(t.path)));
        refresh();                          // le deck passe en AUDITION sans attendre le poll
      })
      .catch(() => { /* endpoint absent/échec : silencieux, le badge n'apparaît pas */ });
  }

  /* ---------- Retour à la file (fin d'audition) ----------
     POST /api/preview/stop restaure la vraie file ; `started` = la lecture a repris. */
  let backBusy = false;
  function backToQueue() {
    if (backBusy) return;
    backBusy = true;
    api('/api/preview/stop', {})
      .then((d) => {
        if (!d) return;
        toast(d.started ? 'Retour à la file — lecture reprise' : 'Retour à la file');
        refresh();
      })
      .catch(() => toast('Retour à la file indisponible (serveur à mettre à jour)', 'warn'))
      .finally(() => { backBusy = false; });
  }
  $('btn-back-queue').addEventListener('click', backToQueue);

  /* ---------- Import local : POST /api/import (FormData files[]) ----------
     Bouton ＋ Importer + drop-zone banque (fichiers OS ≠ drags internes). */
  const AUDIO_RE = /\.(mp3|wav|flac|m4a|aac|ogg|opus|oga|webm)$/i;
  let importing = false;

  async function importFiles(files) {
    const list = [...(files || [])].filter((f) => AUDIO_RE.test(f.name) || String(f.type || '').startsWith('audio/'));
    if (!list.length) { toast('Aucun fichier audio reconnu', 'warn'); return; }
    if (importing) return; // un upload à la fois
    importing = true;
    const btn = $('btn-import'), st = $('import-status');
    btn.disabled = true;
    st.hidden = false;
    try {
      const fd = new FormData();
      for (const f of list) fd.append('files[]', f, f.name);
      const r = await fetch('/api/import', { method: 'POST', body: fd });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json().catch(() => ({}));
      // Nouveau contrat : added + skipped (doublons byte-identiques ignorés).
      // Champs absents (vieux serveur) → on retombe sur le nombre de fichiers envoyés.
      const num = (k) => (j && typeof j[k] === 'number' ? j[k] : null);
      const added = num('added'), skipped = num('skipped');
      if (added === null && skipped === null) toast('Importés : ' + list.length);
      else if (added) toast('Importés : ' + added + (skipped ? ' · déjà présents : ' + skipped : ''));
      else toast(skipped ? 'Déjà importés : ' + skipped : 'Aucun fichier importé');
      refresh();
    } catch (e) {
      console.error('Auto-DJ import', e);
      toast('Import impossible', 'warn');
    } finally {
      importing = false;
      btn.disabled = false;
      st.hidden = true;
    }
  }

  $('btn-import').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', (e) => {
    const sel = [...e.target.files];
    e.target.value = ''; // permet de ré-importer le même fichier ensuite
    if (sel.length) importFiles(sel);
  });

  // Drop-zone : toute la section banque. Drags internes -> drag non null (ignorés ici).
  const bankCol = $('col-bank');
  const hasFiles = (e) => !!(e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files'));
  bankCol.addEventListener('dragover', (e) => {
    if (drag || !hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    bankCol.classList.add('drop-files');
  });
  bankCol.addEventListener('dragleave', (e) => {
    if (!e.relatedTarget || !bankCol.contains(e.relatedTarget)) bankCol.classList.remove('drop-files');
  });
  bankCol.addEventListener('drop', (e) => {
    bankCol.classList.remove('drop-files');
    if (drag || !hasFiles(e)) return; // drag interne : géré par les listes
    e.preventDefault();
    if (e.dataTransfer.files.length) importFiles(e.dataTransfer.files);
  });
  bankCol.addEventListener('dragend', () => bankCol.classList.remove('drop-files'));
  // Filet : un drop de fichiers hors de la banque ne doit jamais faire naviguer la page
  document.addEventListener('dragover', (e) => { if (!drag && hasFiles(e)) e.preventDefault(); });
  document.addEventListener('drop', (e) => { if (!drag && hasFiles(e)) e.preventDefault(); });

  /* ---------- SSE ---------- */
  let es = null;                   // flux courant (fermé à l'arrêt du serveur)
  function debouncedRefresh() {
    clearTimeout(refreshT);
    refreshT = setTimeout(refresh, 250);
  }

  function connectSSE() {
    if (dead) return;              // serveur arrêté : plus aucune reconnexion
    const src = new EventSource('/api/events');
    es = src;
    src.onerror = () => {
      src.close();
      if (es === src) es = null;
      setTimeout(() => { if (!dead) connectSSE(); }, 3000); // retry silencieux
    };
    const handle = (ev) => {
      let d = null;
      try { d = JSON.parse(ev.data); } catch (_) { /* data non-JSON : on ignore */ }
      switch (ev.type) {
        case 'download_ready':
          toast('Prêt : ' + (d && d.title ? d.title : 'téléchargement'));
          debouncedRefresh();
          break;
        case 'corrupt': {
          const title = d && (d.title || (d.path && trackOf(d.path).title)) || '';
          toast('Fichier illisible → sauté' + (title ? ' : ' + title : ''), 'warn');
          debouncedRefresh();
          break;
        }
        case 'queue_empty':
          toast('File vide — ajoutez des morceaux depuis la banque');
          break;
        case 'pos':
        case 'tick':
        case 'position':
          if (d && typeof d.pos === 'number' && S.playing) {
            S.playing.pos = d.pos;
            if (typeof d.dur === 'number') S.playing.dur = d.dur;
            // Pendant un fade, le tick décrit le SORTANT : la vue (entrant) ne bouge pas
            // et posAt n'est pas réarmé, sinon l'extrapolation de l'entrant repart de zéro.
            if (!(S.playing.fading && S.playing.incoming)) { S.view = S.playing; posAt = performance.now(); }
            updateNowPlaying();
          }
          break;
        default: // track_started, ended_fade, …
          debouncedRefresh();
      }
    };
    ['download_ready', 'corrupt', 'track_started', 'ended_fade', 'queue_empty', 'pos', 'tick', 'position']
      .forEach((n) => src.addEventListener(n, handle));
    src.onmessage = (ev) => { // message sans nom : tick pos générique ou état
      let d = null;
      try { d = JSON.parse(ev.data); } catch (_) { return; }
      if (d && typeof d.pos === 'number' && S.playing) {
        S.playing.pos = d.pos;
        if (!(S.playing.fading && S.playing.incoming)) { S.view = S.playing; posAt = performance.now(); }
        updateNowPlaying();
        return;
      }
      debouncedRefresh();
    };
  }

  /* ---------- Modale restauration ---------- */
  function maybeRestore() {
    if (!S.restore_offer || restoreDecided || !modal.hidden) return;
    const o = S.restore_offer;
    const np = o.playing || null;           // forme tolérée : {path,pos[,title]} ou chemin
    const p = typeof np === 'object' && np ? np.path : (typeof np === 'string' ? np : null);
    const n = Array.isArray(o.queue) ? o.queue.length : null;
    const bits = [];
    if (n != null) bits.push(n + ' morceau' + (n > 1 ? 'x' : '') + ' en file');
    if (p) {
      const pos = (typeof np === 'object' && np) ? Number(np.pos) : null;
      bits.push((trackOf(p).title || baseName(p)) + (pos != null ? ' à ' + fmt(pos) : ''));
    }

    $('modal-sub').textContent = bits.length ? bits.join(' · ') : 'File et position enregistrées.';
    restoreFocus = document.activeElement;  // rendu au clavier après la décision
    modal.hidden = false;
    $('btn-restore-yes').focus();
  }

  function decideRestore(confirm) {
    restoreDecided = true;
    modal.hidden = true;
    if (restoreFocus && restoreFocus !== document.body && restoreFocus.isConnected) restoreFocus.focus();
    restoreFocus = null;
    api('/api/restore', { confirm }).then(refresh).catch(() => { /* endpoint absent : silence */ });
  }
  $('btn-restore-yes').addEventListener('click', () => decideRestore(true));
  $('btn-restore-no').addEventListener('click', () => decideRestore(false));

  // focus piégé minimal (Tab) ; Échap est traité par le gestionnaire global
  modal.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const f = [...modal.querySelectorAll('button')];
    const i = f.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) { f[f.length - 1].focus(); e.preventDefault(); }
    else if (!e.shiftKey && (i === f.length - 1 || i < 0)) { f[0].focus(); e.preventDefault(); }
  });

  /* ---------- Clavier global ----------
     ESPACE = ducking (historique) ; N/P/F/A/± = raccourcis de confort.
     Ignorés dès qu'un champ, un bouton ou un éditable a le focus. */
  const typing = (t) => !!(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'BUTTON' ||
    t.tagName === 'SELECT' || t.isContentEditable));

  function nudgeVol(d) {
    const v = $('vol');
    const nv = Math.max(0, Math.min(100, Math.round(Number(v.value) + d)));
    if (nv === Number(v.value)) return;
    v.value = nv;
    v.dispatchEvent(new Event('input', { bubbles: true })); // réutilise le debounce /api/volume
  }

  document.addEventListener('keydown', (e) => {
    if (dead) return;
    if (!modal.hidden) {                   // modale de restauration : elle garde le clavier
      if (e.key === 'Escape') { e.preventDefault(); decideRestore(false); }
      else if (e.key === 'Tab' && !modal.contains(document.activeElement)) {
        e.preventDefault(); $('btn-restore-yes').focus();   // focus sorti (clic sur le fond)
      }
      return;
    }
    if (typing(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'Escape' && S.preview && S.preview.path) {
      // Échap en audition = retour à la file (priorité sur la fermeture du menu :
      // les deux se produisent, la file est toujours restaurée)
      e.preventDefault();
      backToQueue();
      return;
    }
    switch (e.key) {
      case 'n': case 'N': e.preventDefault(); actSkip(); break;
      case 'p': case 'P': e.preventDefault(); actPlay(); break;
      case 'f': case 'F': e.preventDefault(); $('search').focus(); break;
      case 'a': case 'A': e.preventDefault(); actAuto(); break;
      case '+': case '=': e.preventDefault(); nudgeVol(5); break;
      case '-': case '_': e.preventDefault(); nudgeVol(-5); break;
      default: break;
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.code !== 'Space' || e.repeat) return;
    if (dead || typing(e.target) || !modal.hidden) return;
    e.preventDefault();
    actDuck();
  });

  /* ---------- Tout couper : POST /api/quit → le serveur s'arrête ---------- */
  let quitArmed = 0, quitT = 0;
  function disarmQuit() {
    quitArmed = 0;
    clearTimeout(quitT);
    const b = $('btn-quit');
    if (!b) return;
    b.classList.remove('armed');
    b.textContent = 'Tout couper';
  }

  /* Arrêt définitif côté page : plus aucun réseau, plus aucune animation. */
  function shutdownUI() {
    dead = true;
    clearInterval(pollT); pollT = 0;
    clearTimeout(refreshT);
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    if (es) { try { es.close(); } catch (_) { /* déjà fermé */ } es = null; }
    disarmQuit();
    const b = $('btn-quit');
    b.disabled = true;
    b.textContent = 'Arrêté';
    linkBadge.textContent = 'arrêté';
    linkBadge.classList.add('stopped');
    linkBadge.hidden = false;
    $('shutdown').hidden = false;
    $('btn-reload').focus();       // utile au clavier, sans piéger le focus
  }

  function quitAll() {
    // Serveur déjà mort : fetch échoue, l'écran d'arrêt s'affiche quand même.
    fetch('/api/quit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .catch(() => null)
      .then(shutdownUI);
  }

  $('btn-quit').addEventListener('click', () => {
    const b = $('btn-quit');
    if (b.disabled) return;
    if (quitArmed) { disarmQuit(); quitAll(); return; }   // une seule requête
    quitArmed = 1;
    b.classList.add('armed');
    b.textContent = 'Confirmer ?';
    quitT = setTimeout(disarmQuit, 3000);
  });
  $('btn-reload').addEventListener('click', () => location.reload());

  /* ---------- Démarrage ---------- */
  const bb = $('boot-banner');
  if (bb) bb.hidden = true; // UI initialisée → sentinelle masquée (idempotent)
  poll();
  pollT = setInterval(poll, 1000);
  try { connectSSE(); } catch (e) { console.error('SSE indisponible', e); }

  window.AutoDJReady = true; // sentinelle boot-banner : prouve que l'IIFE a fini sans mourir
  return { refresh, toast }; // pour debug console
})();
