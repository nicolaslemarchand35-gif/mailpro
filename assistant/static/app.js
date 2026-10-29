// Interface de l'assistant mail. Tout contenu provenant des mails est insere via
// textContent (jamais innerHTML) : aucun HTML de mail n'est interprete.

const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (k === 'style') el.style.cssText = v; // CSSOM : compatible avec la CSP stricte
    else if (k === 'checked') el.checked = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const LABELS = {
  priorite: { haute: 'Priorité haute', normale: 'Priorité normale', basse: 'Priorité basse' },
  due: { explicite: 'échéance écrite dans le mail', suggeree: 'échéance suggérée', aucune: 'sans échéance' },
  taskStatus: { proposee: 'Proposée', a_faire: 'À faire', en_cours: 'En cours', en_attente: 'En attente', terminee: 'Terminée', rejetee: 'Rejetée' },
  draftStatus: {
    proposee: 'Proposition (pas encore dans Exchange)', creation_en_cours: 'Création en cours de vérification',
    enregistre: 'Enregistré dans Brouillons Exchange', modifie_dans_outlook: 'Modifié dans Outlook (protégé)',
    envoye_ou_supprime: 'Envoyé ou supprimé dans Outlook', erreur: 'Erreur', rejete: 'Rejeté',
  },
  audit: { deplacement: 'Déplacement', annulation: 'Annulation', brouillon: 'Brouillon créé', brouillon_remplace: 'Brouillon remplacé', dossier_cree: 'Dossier créé' },
};
const DAYS = ['Lun', 'Mar', 'Mer', 'Jeu', 'Ven', 'Sam', 'Dim'];

const state = { view: 'a_traiter', projects: [], folders: [], settings: null, config: null };

// ---------------------------------------------------------------- session
const auth = {
  get idToken() { return sessionStorage.getItem('idToken'); },
  store(tokens, remember) {
    sessionStorage.setItem('idToken', tokens.idToken);
    sessionStorage.setItem('expiresAt', String(Date.now() + (Number(tokens.expiresIn || 3600) - 120) * 1000));
    if (tokens.refreshToken) {
      (remember ? localStorage : sessionStorage).setItem('refreshToken', tokens.refreshToken);
    }
  },
  refreshToken() { return sessionStorage.getItem('refreshToken') || safeLocal('refreshToken'); },
  clear() {
    for (const k of ['idToken', 'expiresAt', 'refreshToken']) sessionStorage.removeItem(k);
    try { localStorage.removeItem('refreshToken'); } catch (_) { /* stockage indisponible */ }
  },
  async token() {
    const exp = Number(sessionStorage.getItem('expiresAt') || 0);
    if (this.idToken && (exp === 0 || Date.now() < exp)) return this.idToken;
    const refresh = this.refreshToken();
    if (!refresh || !state.config?.firebase) return this.idToken;
    const res = await fetch(`https://securetoken.googleapis.com/v1/token?key=${encodeURIComponent(state.config.firebase.apiKey)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: refresh }),
    });
    if (!res.ok) { this.clear(); return null; }
    const data = await res.json();
    this.store({ idToken: data.id_token, expiresIn: data.expires_in, refreshToken: data.refresh_token },
      !!safeLocal('refreshToken'));
    return data.id_token;
  },
};

function safeLocal(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}

async function api(path, { method = 'GET', body } = {}) {
  const token = await auth.token();
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (_) { data = null; }
  if (res.status === 401) { showLogin('Session expirée : reconnectez-vous.'); throw new Error('Non connecté'); }
  if (res.status === 403 && data?.error?.startsWith('forbidden_uid')) {
    const uid = data.error.split(':')[1] || '';
    showLogin(`Compte non autorisé. Ajoutez cet UID à ALLOWED_UIDS sur Render : ${uid}`);
    throw new Error('Non autorisé');
  }
  if (!res.ok) throw new Error(data?.message || errorText(data?.error) || `Erreur ${res.status}`);
  return data;
}

function errorText(code) {
  return ({ internal_error: 'Erreur interne du serveur.', exchange_error: 'Exchange a refusé l’opération.',
    not_found: 'Élément introuvable.' })[code] || code;
}

function toast(message, isError = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = isError ? 'toast error' : 'toast';
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3500);
}

async function run(button, fn, success) {
  if (button) button.disabled = true;
  try {
    const result = await fn();
    if (success) toast(success);
    return result;
  } catch (err) {
    toast(err.message || 'Erreur', true);
    return undefined;
  } finally {
    if (button) button.disabled = false;
  }
}

function showLogin(message = '') {
  $('#app').hidden = true;
  $('#login').hidden = false;
  $('#login-error').textContent = message;
  $('#login-firebase').hidden = !state.config?.firebase;
  $('#login-token').hidden = !state.config?.token_login;
  for (const el of $('#login-firebase').querySelectorAll('input')) el.required = !!state.config?.firebase;
  if (!state.config?.firebase && !state.config?.token_login) {
    $('#login-error').textContent = 'Connexion non configurée : renseignez FIREBASE_WEB_CONFIG sur le serveur.';
  }
}

async function login(event) {
  event.preventDefault();
  const form = event.target;
  const remember = form.remember.checked;
  const btn = form.querySelector('button');
  btn.disabled = true;
  $('#login-error').textContent = '';
  try {
    if (state.config.firebase && form.email.value) {
      const res = await fetch(`https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=${encodeURIComponent(state.config.firebase.apiKey)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: form.email.value, password: form.password.value, returnSecureToken: true }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error('Identifiants refusés.');
      auth.store(data, remember);
    } else if (state.config.token_login && form.token.value) {
      auth.store({ idToken: form.token.value, expiresIn: 0 }, false);
      sessionStorage.setItem('expiresAt', '0');
    } else {
      throw new Error('Renseignez vos identifiants.');
    }
    form.reset();
    await start();
  } catch (err) {
    $('#login-error').textContent = err.message;
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- helpers
function fmtDate(iso, withTime = true) {
  if (!iso) return '';
  const d = new Date(iso);
  const opts = withTime ? { dateStyle: 'short', timeStyle: 'short' } : { dateStyle: 'medium' };
  return new Intl.DateTimeFormat('fr-FR', { ...opts, timeZone: 'Europe/Paris' }).format(d);
}
function fmtDay(day) {
  if (!day) return '';
  const [y, m, d] = day.split('-').map(Number);
  return new Intl.DateTimeFormat('fr-FR', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })
    .format(new Date(y, m - 1, d));
}
function ago(iso) {
  if (!iso) return 'jamais';
  const min = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return "à l'instant";
  if (min < 60) return `il y a ${min} min`;
  if (min < 1440) return `il y a ${Math.round(min / 60)} h`;
  return fmtDate(iso);
}
const AVATAR_COLORS = ['#ff9f0a', '#30b0c7', '#5e5ce6', '#34c759', '#ff375f', '#0a84ff', '#bf5af2', '#ac8e68'];
function avatar(name) {
  const words = String(name || '?').replace(/[<>@._-]+/g, ' ').trim().split(/\s+/);
  const initials = ((words[0] || '?')[0] + (words[1] ? words[1][0] : '')).toUpperCase();
  let hash = 0;
  for (const c of String(name)) hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
  const el = h('div', { class: 'avatar', 'aria-hidden': 'true' }, initials);
  el.style.background = AVATAR_COLORS[hash % AVATAR_COLORS.length];
  return el;
}
const projectName = (id) => state.projects.find((p) => p.id === id)?.name;
const folderPath = (id) => state.folders.find((f) => f.id === id)?.path;
const sender = (m) => m.from_name || m.from_email || 'Expéditeur inconnu';

function dueBadge(due, source) {
  if (!due) return null;
  const late = due < new Date().toISOString().slice(0, 10);
  return h('span', { class: `badge ${late ? 'haute' : 'warn'}`, title: LABELS.due[source] || '' },
    `${late ? 'En retard · ' : ''}${fmtDay(due)} (${source === 'explicite' ? 'écrite' : 'suggérée'})`);
}

function folderSelect(selected, { includeInbox = true, placeholder = 'Choisir un dossier…' } = {}) {
  const sel = h('select', {}, h('option', { value: '' }, placeholder));
  for (const f of state.folders) {
    if (f.system || (!includeInbox && f.is_inbox)) continue;
    sel.append(h('option', { value: f.id, selected: f.id === selected }, f.path));
  }
  return sel;
}

function projectSelect(selected, placeholder = 'Aucun projet') {
  const sel = h('select', {}, h('option', { value: '' }, placeholder));
  for (const p of state.projects) sel.append(h('option', { value: p.id, selected: p.id === selected }, p.name));
  return sel;
}

// ---------------------------------------------------------------- statut
async function refreshStatus() {
  try {
    const s = await api('/api/status');
    const el = $('#sync-status');
    el.replaceChildren();
    const parts = [];
    if (s.last_success_at) parts.push(`Dernière synchro réussie ${ago(s.last_success_at)}`);
    else parts.push('Aucune synchronisation réussie pour l’instant');
    if (s.pending_analysis) parts.push(`${s.pending_analysis} mail(s) en attente d’analyse`);
    if (s.ai?.configured) parts.push(`IA : ${s.ai.used_today}/${s.ai.daily_limit} aujourd’hui`);
    else parts.push('IA non configurée');
    el.append(parts.join(' · '));
    if (s.stale) el.append(' · ', h('span', { class: 'alert' }, 'Synchronisation en retard : vérifiez le déclencheur.'));
    if (s.last_error) el.append(' · ', h('span', { class: 'alert' }, `Dernière erreur : ${s.last_error} (${ago(s.last_error_at)})`));
    if (s.folder_errors?.length) el.append(' · ', h('span', { class: 'alert' }, 'Un dossier suivi est introuvable.'));
    state.status = s;
  } catch (_) { /* affichage best-effort */ }
}

async function refreshCounts() {
  try {
    const counts = await api('/api/counts');
    for (const [key, n] of Object.entries(counts)) {
      const el = document.querySelector(`[data-count="${key}"]`);
      if (el) el.textContent = n ? String(n) : '';
    }
  } catch (_) { /* affichage best-effort */ }
}

// ---------------------------------------------------------------- mails
function messageCard(m, { mode }) {
  const a = m.analysis || {};
  const prio = a.priorite || 'normale';
  const badges = h('div', { class: 'badges' },
    mode === 'a_traiter' && prio !== 'normale' ? h('span', { class: `badge ${prio}` }, LABELS.priorite[prio]) : null,
    dueBadge(a.echeance, a.echeance_source),
    m.project_id && projectName(m.project_id) ? h('span', { class: 'badge' }, projectName(m.project_id)) : null,
    m.state === 'en_attente' ? h('span', { class: 'badge warn' }, 'Analyse IA en attente') : null,
    a.source === 'regles' && m.state !== 'en_attente' ? h('span', { class: 'badge warn' }, 'Analyse par règles') : null,
    m.ai_error && m.state !== 'en_attente' ? h('span', { class: 'badge warn' }, `IA : ${m.ai_error}`) : null,
    m.replied_at ? h('span', { class: 'badge ok' }, `Répondu le ${fmtDate(m.replied_at)}`) : null,
    m.has_attachments ? h('span', { class: 'badge' }, 'Pièce(s) jointe(s)') : null,
    m.in_watched === false ? h('span', { class: 'badge' }, m.folder_path ? `Rangé : ${m.folder_path}` : 'Hors dossiers suivis') : null,
  );
  const actions = h('div', { class: 'actions' });
  const patch = (body, msg) => async (e) => {
    await run(e.currentTarget, () => api(`/api/messages/${m.id}`, { method: 'PATCH', body }), msg);
    render();
  };
  if (mode === 'a_traiter') {
    actions.append(
      h('button', { class: 'primary small', onclick: patch({ treatment: 'traite' }, 'Marqué comme traité') }, 'Traité'),
      h('button', { class: 'small', onclick: patch({ category: 'a_savoir' }, 'Déplacé vers À savoir') }, '→ À savoir'),
      h('button', { class: 'small', onclick: patch({ category: 'ignorer' }, 'Ignoré') }, 'Ignorer'),
    );
  } else if (mode === 'a_savoir') {
    actions.append(
      h('button', { class: 'primary small', onclick: patch({ treatment: 'vu' }, 'Marqué comme vu') }, 'Vu'),
      h('button', { class: 'small', onclick: patch({ category: 'a_traiter' }, 'Déplacé vers À traiter') }, '→ À traiter'),
    );
  } else if (mode === 'other') {
    actions.append(
      h('button', { class: 'small', onclick: patch({ treatment: 'ouvert', category: 'a_traiter' }, 'Remis à traiter') }, 'Remettre à traiter'),
    );
  }
  actions.append(h('button', { class: 'small ghost', onclick: () => openMessage(m.id) }, 'Détails'));
  return h('article', { class: `card mail ${mode === 'a_traiter' ? `prio-${prio}` : ''}` },
    avatar(sender(m)),
    h('div', { class: 'mail-main' },
      h('div', { class: 'mail-top' }, h('span', { class: 'sender' }, sender(m)),
        h('span', { class: 'date' }, fmtDate(m.received_at))),
      h('div', { class: 'card-title', onclick: () => openMessage(m.id), role: 'button', tabindex: '0',
        onkeydown: (e) => { if (e.key === 'Enter') openMessage(m.id); } }, m.subject || '(sans objet)'),
      a.resume ? h('p', { class: 'summary' }, a.resume) : null,
      mode === 'a_traiter' && a.action_requise ? h('div', { class: 'action' }, a.action_requise) : null,
      badges, actions));
}

async function renderMessages(view) {
  const main = $('#main');
  const filterSel = h('select', { 'aria-label': 'Filtrer par projet' },
    h('option', { value: '' }, 'Tous les projets'),
    ...state.projects.map((p) => h('option', { value: p.id }, p.name)));
  const other = view === 'a_traiter'
    ? h('select', { 'aria-label': 'Autres listes' }, h('option', { value: '' }, 'Voir aussi…'),
      h('option', { value: 'traite' }, 'Traités'), h('option', { value: 'ignore' }, 'Ignorés / newsletters'))
    : h('select', { 'aria-label': 'Autres listes' }, h('option', { value: '' }, 'Voir aussi…'),
      h('option', { value: 'archive' }, 'Informations vues'));
  const list = h('div', { class: 'list' }, h('p', { class: 'muted' }, 'Chargement…'));
  main.replaceChildren(
    h('div', { class: 'toolbar' }, h('h2', {}, view === 'a_traiter' ? 'À traiter' : 'À savoir'),
      h('span', { class: 'spacer' }), filterSel, other),
    list);
  let current = view;
  const load = async () => {
    list.replaceChildren(h('p', { class: 'muted' }, 'Chargement…'));
    try {
      const data = await api(`/api/messages?view=${current}`);
      const items = data.items.filter((m) => !filterSel.value || m.project_id === filterSel.value);
      const mode = current === view ? view : 'other';
      list.replaceChildren(...(items.length ? items.map((m) => messageCard(m, { mode }))
        : [h('div', { class: 'empty' }, current === 'a_traiter' ? 'Rien à traiter pour le moment.' : 'Aucun élément.')]));
    } catch (err) {
      list.replaceChildren(h('div', { class: 'empty' }, err.message));
    }
  };
  filterSel.addEventListener('change', load);
  other.addEventListener('change', () => { current = other.value || view; load(); });
  await load();
}

async function openMessage(id) {
  const drawer = $('#drawer');
  const body = $('#drawer-body');
  $('#drawer-title').textContent = 'Chargement…';
  body.replaceChildren();
  drawer.hidden = false;
  let m;
  try { m = await api(`/api/messages/${id}`); } catch (err) { $('#drawer-title').textContent = err.message; return; }
  const a = m.analysis || {};
  $('#drawer-title').textContent = m.subject || '(sans objet)';
  const projSel = projectSelect(m.project_id);
  projSel.addEventListener('change', () => run(null, () => api(`/api/messages/${id}`, {
    method: 'PATCH', body: { project_id: projSel.value || null } }), 'Projet enregistré'));
  const bodyBox = h('div', {}, h('button', { class: 'small', onclick: async (e) => {
    const data = await run(e.currentTarget, () => api(`/api/messages/${id}/body`));
    if (!data) return;
    bodyBox.replaceChildren(
      data.attachments?.length ? h('p', { class: 'meta' }, `Pièces jointes : ${data.attachments.join(', ')}`) : null,
      h('pre', { class: 'body' }, data.body || '(corps vide)'));
  } }, 'Afficher le mail (lu dans Exchange)'));
  body.append(
    h('dl', { class: 'kv' },
      h('dt', {}, 'De'), h('dd', {}, `${m.from_name || ''} <${m.from_email || '?'}>`),
      h('dt', {}, 'À'), h('dd', {}, (m.to || []).join(', ')),
      m.cc?.length ? [h('dt', {}, 'Cc'), h('dd', {}, m.cc.join(', '))] : null,
      h('dt', {}, 'Reçu'), h('dd', {}, fmtDate(m.received_at)),
      h('dt', {}, 'Emplacement'), h('dd', {}, m.folder_path || (m.in_watched === false ? 'Hors dossiers suivis' : '—')),
      h('dt', {}, 'Analyse'), h('dd', {}, a.source === 'ia' ? `IA (${a.modele || 'modèle ?'}, confiance ${Math.round((a.confiance || 0) * 100)} %)` : 'Règles locales'),
      m.replied_at ? [h('dt', {}, 'Répondu'), h('dd', {}, fmtDate(m.replied_at))] : null),
    a.resume ? h('p', { class: 'summary' }, a.resume) : null,
    a.action_requise ? h('div', { class: 'action' }, a.action_requise) : null,
    a.echeance ? h('p', {}, `Échéance : ${fmtDay(a.echeance)} — ${LABELS.due[a.echeance_source]}`,
      a.echeance_citation ? h('span', { class: 'meta' }, ` « ${a.echeance_citation} »`) : null) : null,
    a.points_a_verifier?.length ? h('div', { class: 'notice' }, 'À vérifier : ', a.points_a_verifier.join(' · ')) : null,
    h('label', {}, 'Projet', projSel),
    h('div', { class: 'actions' },
      h('button', { class: 'small', onclick: async (e) => {
        const r = await run(e.currentTarget, () => api(`/api/messages/${id}/reanalyze`, { method: 'POST' }), 'Analyse mise à jour');
        if (r) { openMessage(id); render(); }
      } }, 'Réanalyser avec l’IA'),
      h('button', { class: 'small', onclick: () => { closeDrawer(); openDraftEditor(id, m); } },
        m.draft ? 'Voir le brouillon' : 'Préparer une réponse')),
    m.tasks?.length ? h('div', { class: 'section' }, h('h3', {}, 'Tâches liées'),
      h('ul', {}, ...m.tasks.map((t) => h('li', {}, `${t.title} — ${LABELS.taskStatus[t.status] || t.status}`)))) : null,
    h('div', { class: 'section' }, h('h3', {}, 'Contenu'), bodyBox),
  );
}

function closeDrawer() { $('#drawer').hidden = true; }

// ---------------------------------------------------------------- brouillons
function draftCard(d) {
  const editable = ['proposee', 'erreur', 'enregistre'].includes(d.status);
  const textarea = h('textarea', { 'aria-label': 'Texte du brouillon', disabled: !editable }, d.body || '');
  const replyAll = h('input', { type: 'checkbox', checked: d.reply_all, disabled: !editable });
  const actions = h('div', { class: 'actions' });
  const save = async (e) => {
    const r = await run(e.currentTarget, () => api(`/api/messages/${d.id}/draft`, {
      method: 'POST', body: { body: textarea.value, reply_all: replyAll.checked } }),
    'Brouillon enregistré dans Exchange (rien n’est envoyé).');
    if (r) render();
  };
  if (d.status === 'proposee' || d.status === 'erreur') {
    actions.append(
      h('button', { class: 'primary small', onclick: save }, 'Créer le brouillon dans Exchange'),
      h('button', { class: 'small', onclick: async (e) => {
        await run(e.currentTarget, () => api(`/api/drafts/${d.id}`, { method: 'PATCH', body: { body: textarea.value, reply_all: replyAll.checked } }), 'Texte enregistré');
      } }, 'Enregistrer le texte'),
      h('button', { class: 'small danger', onclick: async (e) => {
        await run(e.currentTarget, () => api(`/api/drafts/${d.id}`, { method: 'PATCH', body: { status: 'rejete' } }), 'Proposition rejetée');
        render();
      } }, 'Rejeter'));
  } else if (d.status === 'enregistre') {
    actions.append(
      h('button', { class: 'small', onclick: save }, 'Remplacer le brouillon Exchange'),
      h('button', { class: 'small', onclick: async (e) => {
        await run(e.currentTarget, () => api(`/api/drafts/${d.id}/check`, { method: 'POST' }), 'Statut vérifié');
        render();
      } }, 'Vérifier dans Exchange'));
  } else {
    actions.append(h('button', { class: 'small', onclick: async (e) => {
      await run(e.currentTarget, () => api(`/api/drafts/${d.id}/check`, { method: 'POST' }), 'Statut vérifié');
      render();
    } }, 'Vérifier'),
    h('button', { class: 'small', onclick: async (e) => {
      await run(e.currentTarget, () => api(`/api/drafts/${d.id}`, { method: 'PATCH', body: { status: 'archive' } }), 'Retiré de la liste');
      render();
    } }, 'Retirer de la liste'));
  }
  actions.append(h('button', { class: 'small ghost', onclick: () => openMessage(d.id) }, 'Mail d’origine'));
  return h('article', { class: 'card' },
    h('div', { class: 'card-head' }, h('div', { class: 'card-title' }, `RE: ${d.subject || ''}`),
      h('span', { class: `badge ${d.status === 'enregistre' ? 'ok' : (d.status === 'proposee' ? '' : 'warn')}` },
        LABELS.draftStatus[d.status] || d.status)),
    h('div', { class: 'meta' }, d.to?.length ? `À : ${d.to.join(', ')}${d.cc?.length ? ` · Cc : ${d.cc.join(', ')}` : ''}`
      : `Destinataire prévu : ${d.to_hint || '?'}${d.reply_all ? ' (+ répondre à tous)' : ''}`),
    d.error ? h('div', { class: 'notice' }, d.error) : null,
    d.checks?.length ? h('div', { class: 'notice' }, 'Avant envoi, vérifier : ', d.checks.join(' · ')) : null,
    h('label', {}, 'Texte (la signature des réglages est ajoutée ; le message d’origine est cité par Exchange)', textarea),
    h('label', { class: 'check' }, replyAll, 'Répondre à tous'),
    actions);
}

async function renderDrafts() {
  const main = $('#main');
  main.replaceChildren(h('div', { class: 'toolbar' }, h('h2', {}, 'Brouillons')),
    h('p', { class: 'muted' }, 'Les brouillons sont créés dans votre dossier Brouillons Exchange. Vous relisez et envoyez vous-même depuis Outlook : l’assistant n’envoie jamais de mail.'),
    h('div', { class: 'notice' }, 'Relisez chaque proposition : l’IA peut se tromper ou formuler un engagement que vous n’avez pas pris. Complétez les passages [à compléter : …].'));
  const data = await api('/api/drafts');
  main.append(h('div', { class: 'list' }, ...(data.items.length ? data.items.map(draftCard)
    : [h('div', { class: 'empty' }, 'Aucun brouillon proposé.')])));
}

async function openDraftEditor(id, m) {
  setView('brouillons');
  const data = await api('/api/drafts');
  if (data.items.some((d) => d.id === id)) return;
  // Pas de proposition IA : brouillon manuel.
  const card = draftCard({ id, subject: m.subject, to_hint: m.from_email, body: '', status: 'proposee' });
  $('#main .list').prepend(card);
}

// ---------------------------------------------------------------- taches
async function renderTasks() {
  const main = $('#main');
  const scopeSel = h('select', { 'aria-label': 'Filtre' }, h('option', { value: 'open' }, 'En cours'), h('option', { value: 'closed' }, 'Terminées / rejetées'));
  const projFilter = projectSelect('', 'Tous les projets');
  const list = h('div', { class: 'list' });
  const form = h('form', { class: 'card' },
    h('h3', {}, 'Nouvelle tâche'),
    h('div', { class: 'row' },
      h('input', { name: 'title', placeholder: 'Intitulé', required: true, maxlength: '300' }),
      projectSelect(''),
      h('select', { name: 'priority' }, h('option', { value: 'normale' }, 'Normale'), h('option', { value: 'haute' }, 'Haute'), h('option', { value: 'basse' }, 'Basse')),
      h('input', { type: 'date', name: 'due_at', 'aria-label': 'Échéance' }),
      h('button', { class: 'primary', type: 'submit' }, 'Ajouter')));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const sels = form.querySelectorAll('select');
    const r = await run(form.querySelector('button'), () => api('/api/tasks', { method: 'POST', body: {
      title: form.title.value, project_id: sels[0].value || null, priority: sels[1].value,
      due_at: form.due_at.value || null, status: 'a_faire' } }), 'Tâche ajoutée');
    if (r) { form.reset(); load(); }
  });
  main.replaceChildren(h('div', { class: 'toolbar' }, h('h2', {}, 'Tâches'), h('span', { class: 'spacer' }), projFilter, scopeSel), form,
    h('div', { class: 'section' }, list));
  const load = async () => {
    const data = await api(`/api/tasks?scope=${scopeSel.value}`);
    const items = data.items.filter((t) => !projFilter.value || t.project_id === projFilter.value);
    list.replaceChildren(...(items.length ? items.map((t) => taskCard(t, load)) : [h('div', { class: 'empty' }, 'Aucune tâche.')]));
  };
  scopeSel.addEventListener('change', load);
  projFilter.addEventListener('change', load);
  await load();
}

function taskCard(t, reload) {
  const patch = (body, msg) => run(null, () => api(`/api/tasks/${t.id}`, { method: 'PATCH', body }), msg).then(reload);
  const status = h('select', { 'aria-label': 'Statut' }, ...Object.entries(LABELS.taskStatus).map(([k, v]) => h('option', { value: k, selected: k === t.status }, v)));
  status.addEventListener('change', () => patch({ status: status.value }, 'Statut mis à jour'));
  const prio = h('select', { 'aria-label': 'Priorité' }, ...['haute', 'normale', 'basse'].map((k) => h('option', { value: k, selected: k === t.priority }, LABELS.priorite[k])));
  prio.addEventListener('change', () => patch({ priority: prio.value }));
  const due = h('input', { type: 'date', value: t.due_at || '', 'aria-label': 'Échéance' });
  due.addEventListener('change', () => patch({ due_at: due.value || null, due_source: due.value ? 'explicite' : 'aucune' }, 'Échéance mise à jour'));
  const proj = projectSelect(t.project_id);
  proj.addEventListener('change', () => patch({ project_id: proj.value || null }));
  return h('article', { class: `card task prio-${t.priority}` },
    h('div', { class: 'card-head' }, h('div', { class: 'card-title' }, t.title),
      t.created_by === 'ia' ? h('span', { class: 'badge' }, 'Proposée par l’IA') : null,
      t.due_at ? h('span', { class: 'badge' }, `${fmtDay(t.due_at)} · ${LABELS.due[t.due_source] || ''}`) : null),
    t.source_subject || t.source_message_ids?.length ? h('div', { class: 'meta' }, 'Source : ',
      ...(t.source_message_ids || []).map((mid, i) => h('a', { href: '#', onclick: (e) => { e.preventDefault(); openMessage(mid); } },
        i === 0 && t.source_subject ? t.source_subject : `mail ${i + 1}`)).flatMap((a, i) => (i ? [', ', a] : [a]))) : null,
    t.status === 'proposee' ? h('div', { class: 'actions' },
      h('button', { class: 'primary small', onclick: () => patch({ status: 'a_faire' }, 'Tâche acceptée') }, 'Accepter'),
      h('button', { class: 'small danger', onclick: () => patch({ status: 'rejetee' }, 'Tâche rejetée') }, 'Rejeter'))
      : null,
    h('div', { class: 'row', style: 'margin-top:.5rem' }, status, prio, due, proj,
      h('button', { class: 'small ghost danger', onclick: async () => {
        if (!confirm('Supprimer définitivement cette tâche ?')) return;
        await run(null, () => api(`/api/tasks/${t.id}`, { method: 'DELETE' }), 'Tâche supprimée');
        reload();
      } }, 'Supprimer')));
}

// ---------------------------------------------------------------- classement
async function renderClassement() {
  const main = $('#main');
  main.replaceChildren(h('div', { class: 'toolbar' }, h('h2', {}, 'Classement'), h('span', { class: 'spacer' }),
    h('button', { onclick: async (e) => { await run(e.currentTarget, loadFolders, 'Arborescence actualisée'); render(); } }, 'Actualiser les dossiers')),
  h('p', { class: 'muted' }, 'Propositions de rangement dans vos dossiers Outlook. Rien n’est déplacé sans votre validation (sauf projets en déplacement automatique, annulables dans le journal).'));
  const [props, audit] = await Promise.all([api('/api/messages?view=classement'), api('/api/audit')]);
  const list = h('div', { class: 'list' });
  for (const m of props.items) {
    const sel = folderSelect(m.classement_target, { includeInbox: false });
    list.append(h('article', { class: 'card' },
      h('div', { class: 'card-head' }, h('div', { class: 'card-title', onclick: () => openMessage(m.id) }, m.subject || '(sans objet)'),
        h('span', { class: 'meta' }, fmtDate(m.received_at))),
      h('div', { class: 'meta' }, sender(m)),
      h('div', { class: 'row', style: 'margin-top:.5rem' }, sel,
        h('button', { class: 'primary small', onclick: async (e) => {
          if (!sel.value) return toast('Choisissez un dossier.', true);
          const r = await run(e.currentTarget, () => api(`/api/messages/${m.id}/move`, { method: 'POST', body: { folder_id: sel.value } }), 'Mail déplacé (annulable dans le journal)');
          if (r) render();
        } }, 'Déplacer'),
        h('button', { class: 'small', onclick: async (e) => {
          await run(e.currentTarget, () => api(`/api/messages/${m.id}`, { method: 'PATCH', body: { classement: 'refuse' } }), 'Proposition refusée');
          render();
        } }, 'Laisser en place'))));
  }
  if (!props.items.length) list.append(h('div', { class: 'empty' }, 'Aucune proposition de classement.'));

  const parentSel = folderSelect('', { placeholder: 'À la racine de la boîte' });
  const nameInput = h('input', { placeholder: 'Nom du nouveau dossier', maxlength: '120' });
  const tree = h('div', { class: 'tree' }, ...state.folders.filter((f) => !f.system).map((f) =>
    h('div', { style: `padding-left:${(f.path.split(' / ').length - 1) * 1}rem` }, f.name, h('span', { class: 'meta' }, f.total ? ` (${f.total})` : ''))));
  const rows = audit.items.map((a) => h('tr', {},
    h('td', {}, fmtDate(a.at)), h('td', {}, LABELS.audit[a.op] || a.op, a.by === 'auto' ? ' (auto)' : ''),
    h('td', {}, a.subject || ''), h('td', {}, a.from_path ? `${a.from_path} → ${a.to_path || ''}` : (a.to_path || '')),
    h('td', {}, a.result === 'ok' ? 'OK' : a.result, a.error ? ` (${a.error})` : ''),
    h('td', {}, a.op === 'deplacement' && a.result === 'ok' ? h('button', { class: 'small', onclick: async (e) => {
      await run(e.currentTarget, () => api(`/api/audit/${a.id}/undo`, { method: 'POST' }), 'Déplacement annulé');
      render();
    } }, 'Annuler') : null)));
  main.append(list,
    h('div', { class: 'section' }, h('h3', {}, 'Arborescence de la boîte'), tree,
      h('div', { class: 'row', style: 'margin-top:.6rem' }, parentSel, nameInput,
        h('button', { onclick: async (e) => {
          const r = await run(e.currentTarget, () => api('/api/folders', { method: 'POST', body: { parent_id: parentSel.value || null, name: nameInput.value } }), 'Dossier créé dans Exchange');
          if (r) { await loadFolders(); render(); }
        } }, 'Créer le dossier'))),
    h('div', { class: 'section' }, h('h3', {}, 'Journal des opérations'),
      audit.items.length ? h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ...['Date', 'Opération', 'Mail', 'Dossiers', 'Résultat', ''].map((t) => h('th', {}, t)))),
        h('tbody', {}, ...rows))) : h('div', { class: 'empty' }, 'Aucune opération.')));
}

// ---------------------------------------------------------------- reglages
async function renderSettings() {
  const main = $('#main');
  const s = await api('/api/settings');
  state.settings = s;
  const f = {};
  const input = (key, attrs = {}) => (f[key] = h('input', { value: s[key] ?? '', ...attrs }));
  const row = (label, control, hint) => h('label', { class: 'field' },
    h('span', { class: 'field-label' }, label, hint ? h('small', {}, hint) : null), control);
  const toggle = (key, label, hint) => h('label', { class: 'field toggle' },
    h('span', { class: 'field-label' }, label, hint ? h('small', {}, hint) : null),
    (f[key] = h('input', { type: 'checkbox', class: 'switch', checked: s[key] })));
  const group = (title, footer, ...children) => h('section', { class: 'group' },
    title ? h('h3', { class: 'group-title' }, title) : null,
    h('div', { class: 'group-body' }, ...children),
    footer ? h('p', { class: 'group-footer' }, footer) : null);
  const days = h('div', { class: 'chips' }, ...DAYS.map((d, i) => h('label', { class: 'chip' },
    h('input', { type: 'checkbox', value: String(i), checked: s.work_days.includes(i) }), d)));
  const watched = h('select', { multiple: true, size: '6', 'aria-label': 'Dossiers suivis' },
    h('option', { value: 'inbox', selected: s.watched_folders.includes('inbox') }, 'Boîte de réception'),
    ...state.folders.filter((x) => !x.system && !x.is_inbox).map((x) => h('option', { value: x.id, selected: s.watched_folders.includes(x.id) }, x.path)));

  const form = h('form', { class: 'settings' },
    group('Synchronisation', 'Hors de cette plage, les appels automatiques ne touchent pas à Exchange. Le bouton Synchroniser fonctionne à tout moment.',
      row('Début de plage', input('work_start', { type: 'time' }), 'Heure de Paris'),
      row('Fin de plage', input('work_end', { type: 'time' }), 'Exclue'),
      h('div', { class: 'field' }, h('span', { class: 'field-label' }, 'Jours'), days),
      row('Historique au premier passage', input('history_days', { type: 'number', min: '0', max: '60' }), 'jours'),
      row('Conservation des résumés', input('retention_days', { type: 'number', min: '7', max: '730' }), 'jours'),
      row('Archiver « À savoir » après', input('a_savoir_expire_days', { type: 'number', min: '1', max: '90' }), 'jours'),
      h('label', { class: 'field stack' }, h('span', { class: 'field-label' }, 'Dossiers suivis',
        h('small', {}, 'Ctrl/Cmd + clic pour plusieurs — ajoutez ceux remplis par vos règles Outlook')), watched)),
    group('Intelligence artificielle', 'Les modèles gratuits peuvent conserver les textes envoyés. Chaque mail analysé compte pour un appel.',
      toggle('ai_enabled', 'Analyser les mails avec l’IA'),
      toggle('ai_on_bulk', 'Analyser aussi les newsletters'),
      row('Appels IA par jour au maximum', input('ai_daily_limit', { type: 'number', min: '0', max: '1000' }))),
    group('Rédaction', null,
      row('Votre nom', input('user_name')),
      h('label', { class: 'field stack' }, h('span', { class: 'field-label' }, 'Style rédactionnel'),
        (f.writing_style = h('textarea', { rows: '3' }, s.writing_style || ''))),
      h('label', { class: 'field stack' }, h('span', { class: 'field-label' }, 'Signature', h('small', {}, 'Ajoutée à la fin des brouillons')),
        (f.signature = h('textarea', { rows: '4' }, s.signature || ''))),
      h('label', { class: 'field stack' }, h('span', { class: 'field-label' }, 'Interlocuteurs prioritaires', h('small', {}, 'Un par ligne : adresse ou @domaine.fr')),
        (f.priority_contacts = h('textarea', { rows: '3' }, (s.priority_contacts || []).join('\n'))))),
    group('Automatismes', 'Désactivés par défaut. Chaque déplacement reste annulable dans le journal du Classement.',
      toggle('auto_draft', 'Brouillons automatiques', 'Enregistrés dans Exchange, jamais envoyés'),
      toggle('auto_move', 'Déplacements automatiques', 'Seulement les projets marqués « automatique »'),
      row('Confiance minimale', input('auto_move_min_confidence', { type: 'number', min: '0.5', max: '1', step: '0.05' }), '0 à 1')),
    h('div', { class: 'form-actions' }, h('button', { class: 'primary large', type: 'submit' }, 'Enregistrer les réglages')));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      work_start: f.work_start.value, work_end: f.work_end.value,
      history_days: Number(f.history_days.value), retention_days: Number(f.retention_days.value),
      a_savoir_expire_days: Number(f.a_savoir_expire_days.value), ai_daily_limit: Number(f.ai_daily_limit.value),
      work_days: [...days.querySelectorAll('input')].filter((i) => i.checked).map((i) => Number(i.value)),
      watched_folders: [...watched.selectedOptions].map((o) => o.value),
      ai_enabled: f.ai_enabled.checked, ai_on_bulk: f.ai_on_bulk.checked, auto_draft: f.auto_draft.checked,
      auto_move: f.auto_move.checked, auto_move_min_confidence: Number(f.auto_move_min_confidence.value),
      user_name: f.user_name.value, writing_style: f.writing_style.value, signature: f.signature.value,
      priority_contacts: f.priority_contacts.value.split('\n'),
    };
    const r = await run(form.querySelector('button[type=submit]'), () => api('/api/settings', { method: 'PUT', body }), 'Réglages enregistrés');
    if (r) state.settings = r;
  });

  const st = state.status || {};
  const kv = (k, v) => h('div', { class: 'field' }, h('span', { class: 'field-label' }, k), h('span', { class: 'field-value' }, v));
  const statusGroup = group('État', null,
    kv('Dernière synchro réussie', st.last_success_at ? fmtDate(st.last_success_at) : 'jamais'),
    kv('Dernier passage', st.last_tick_at ? fmtDate(st.last_tick_at) : '—'),
    kv('Dernière erreur', st.last_error ? `${st.last_error} (${fmtDate(st.last_error_at)})` : 'aucune'),
    kv('Travail restant', st.backlog ? 'oui, repris au prochain passage' : 'non'),
    kv('Appels IA aujourd’hui', st.ai ? `${st.ai.used_today} / ${st.ai.daily_limit}` : '—'));

  main.replaceChildren(h('div', { class: 'toolbar' }, h('h2', {}, 'Réglages')),
    statusGroup, await modelPicker(s), form, await projectsEditor());
}

async function modelPicker(s) {
  const select = h('select', { 'aria-label': 'Modèle IA' }, h('option', { value: '' }, 'Chargement des modèles…'));
  const info = h('p', { class: 'group-footer' });
  const testBtn = h('button', { type: 'button', class: 'small' }, 'Tester');
  const result = h('span', { class: 'field-value' });
  const box = h('section', { class: 'group' }, h('h3', { class: 'group-title' }, 'Modèle IA'),
    h('div', { class: 'group-body' },
      h('label', { class: 'field stack' }, h('span', { class: 'field-label' }, 'Modèle utilisé',
        h('small', {}, 'Liste des modèles gratuits disponibles sur OpenRouter, mise à jour automatiquement')), select),
      h('div', { class: 'field' }, h('span', { class: 'field-label' }, 'Vérifier la disponibilité', h('small', {}, 'Test sans aucune donnée de mail (1 appel)')),
        h('span', { class: 'inline' }, result, testBtn))),
    info);
  const fill = async (refresh) => {
    try {
      const data = await api('/api/ai/models');
      const opts = [h('option', { value: '' }, `Par défaut — ${(data.default || []).join(' → ')}`)];
      if (data.selected && !data.selected_available) {
        opts.push(h('option', { value: data.selected }, `${data.selected} (plus disponible)`));
      }
      for (const m of data.models) {
        const ctx = m.context_length ? ` · ${Math.round(m.context_length / 1000)}k` : '';
        opts.push(h('option', { value: m.id }, `${m.name}${ctx}`));
      }
      select.replaceChildren(...opts);
      select.value = data.selected || '';
      info.textContent = data.selected && !data.selected_available
        ? 'Le modèle choisi n’est plus proposé gratuitement : le repli « openrouter/free » est utilisé. Choisissez-en un autre.'
        : `${data.models.length} modèles gratuits disponibles. En cas d’indisponibilité, le routeur gratuit d’OpenRouter prend le relais.`;
      if (refresh) toast('Liste des modèles à jour');
    } catch (err) {
      select.replaceChildren(h('option', { value: s.ai_model || '' }, s.ai_model || 'Par défaut'));
      info.textContent = err.message;
    }
  };
  select.addEventListener('change', async () => {
    const r = await run(select, () => api('/api/settings', { method: 'PUT', body: { ai_model: select.value } }),
      select.value ? `Modèle enregistré : ${select.value}` : 'Modèle par défaut rétabli');
    if (r) { state.settings = r; result.textContent = ''; refreshStatus(); }
  });
  testBtn.addEventListener('click', async () => {
    const model = select.value || (state.status?.ai?.models || [])[0];
    if (!model) return toast('Aucun modèle sélectionné.', true);
    result.textContent = 'Test en cours…';
    const r = await run(testBtn, () => api('/api/ai/test', { method: 'POST', body: { model } }));
    if (!r) { result.textContent = ''; return; }
    result.textContent = r.ok ? `✓ Répond en ${r.seconds} s` : `✕ Indisponible (${r.error})`;
    result.className = `field-value ${r.ok ? 'ok-text' : 'error'}`;
  });
  fill(false);
  return box;
}

async function projectsEditor() {
  const wrap = h('section', { class: 'group' }, h('h3', { class: 'group-title' }, 'Projets'),
    h('p', { class: 'group-header-note' }, 'Mots-clés et interlocuteurs validés servent au rapprochement des mails ; le dossier cible alimente les propositions de classement.'));
  const list = h('div', { class: 'list' });
  const editor = (p = { name: '', keywords: [], contacts: [], folder_id: null, auto_move: false, active: true }) => {
    const name = h('input', { value: p.name, placeholder: 'Nom du projet (ex. Résidence Les Tilleuls)' });
    const kw = h('input', { value: (p.keywords || []).join(', '), placeholder: 'Mots-clés séparés par des virgules' });
    const ct = h('input', { value: (p.contacts || []).join(', '), placeholder: 'Interlocuteurs : adresses ou @domaine.fr' });
    const folder = folderSelect(p.folder_id, { includeInbox: false, placeholder: 'Aucun dossier cible' });
    const auto = h('input', { type: 'checkbox', class: 'switch', checked: p.auto_move });
    const split = (v) => v.split(',').map((x) => x.trim()).filter(Boolean);
    const body = () => ({ name: name.value, keywords: split(kw.value), contacts: split(ct.value), folder_id: folder.value || null, auto_move: auto.checked, active: true });
    return h('div', { class: 'card' },
      h('div', { class: 'grid2' }, h('label', {}, 'Nom', name), h('label', {}, 'Dossier cible', folder)),
      h('label', {}, 'Mots-clés', kw), h('label', {}, 'Interlocuteurs', ct),
      h('label', { class: 'field toggle' }, h('span', { class: 'field-label' }, 'Déplacement automatique',
        h('small', {}, 'Si l’automatisme est activé dans les réglages')), auto),
      h('div', { class: 'actions' },
        h('button', { class: 'primary small', onclick: async (e) => {
          const r = await run(e.currentTarget, () => (p.id ? api(`/api/projects/${p.id}`, { method: 'PUT', body: body() }) : api('/api/projects', { method: 'POST', body: body() })), 'Projet enregistré');
          if (r) { await loadProjects(); render(); }
        } }, 'Enregistrer'),
        p.id ? h('button', { class: 'small danger', onclick: async (e) => {
          if (!confirm(`Supprimer le projet « ${p.name} » ? Les tâches sont conservées.`)) return;
          await run(e.currentTarget, () => api(`/api/projects/${p.id}`, { method: 'DELETE' }), 'Projet supprimé');
          await loadProjects(); render();
        } }, 'Supprimer') : null));
  };
  for (const p of state.projects) list.append(editor(p));
  if (!state.projects.length) list.append(h('div', { class: 'empty' }, 'Aucun projet pour l’instant.'));
  wrap.append(list, h('div', { class: 'actions' }, h('button', { class: 'tinted', onclick: () => { list.querySelector('.empty')?.remove(); list.append(editor()); } }, '+ Ajouter un projet')));
  return wrap;
}

// ---------------------------------------------------------------- navigation
async function loadProjects() { state.projects = (await api('/api/projects')).items; }
async function loadFolders() { state.folders = (await api('/api/folders')).folders; }

function setView(view) {
  state.view = view;
  for (const b of document.querySelectorAll('#tabs button')) b.classList.toggle('active', b.dataset.view === view);
  try { sessionStorage.setItem('view', view); } catch (_) { /* ignore */ }
  return render();
}

async function render() {
  const main = $('#main');
  try {
    if (state.view === 'a_traiter' || state.view === 'a_savoir') await renderMessages(state.view);
    else if (state.view === 'brouillons') await renderDrafts();
    else if (state.view === 'taches') await renderTasks();
    else if (state.view === 'classement') await renderClassement();
    else if (state.view === 'reglages') await renderSettings();
  } catch (err) {
    main.replaceChildren(h('div', { class: 'empty' }, err.message || 'Erreur de chargement.'));
  }
  refreshCounts();
}

async function start() {
  await api('/api/me');
  $('#login').hidden = true;
  $('#app').hidden = false;
  await Promise.all([loadProjects(), loadFolders(), refreshStatus()]);
  const saved = sessionStorage.getItem('view');
  await setView(saved || 'a_traiter');
}

async function boot() {
  state.config = await (await fetch('/api/public-config')).json();
  $('#login-form').addEventListener('submit', login);
  $('#tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-view]');
    if (b) setView(b.dataset.view);
  });
  $('#btn-logout').addEventListener('click', () => { auth.clear(); showLogin(); });
  $('#btn-sync').addEventListener('click', async (e) => {
    const r = await run(e.currentTarget, () => api('/api/sync', { method: 'POST' }));
    if (r) {
      const c = r.counters || {};
      if (r.status === 'ok') toast(`Synchronisé : ${c.created || 0} nouveau(x), ${c.analyzed || 0} analysé(s)${r.backlog ? ' — suite au prochain passage' : ''}.`);
      else if (r.status === 'busy') toast('Une synchronisation est déjà en cours.');
      else toast(`Synchronisation en erreur : ${r.error}`, true);
    }
    await Promise.all([refreshStatus(), loadFolders()]);
    render();
  });
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer').addEventListener('click', (e) => { if (e.target.id === 'drawer') closeDrawer(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('#app').hidden) refreshStatus(); });
  setInterval(() => { if (!document.hidden && !$('#app').hidden) refreshStatus(); }, 5 * 60 * 1000);
  if (!auth.idToken && !auth.refreshToken()) return showLogin();
  try { await start(); } catch (_) { if (!$('#login').hidden) return; showLogin(); }
}

boot();
