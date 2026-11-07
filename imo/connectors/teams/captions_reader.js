// Lecteur des sous-titres en direct de Teams, injecté dans la page.
// Appelle window.__imvOnCaption({sender, text}) pour chaque phrase terminée.
// Teams affiche une ligne par prise de parole, dont le texte s'allonge et se corrige
// pendant que la personne parle : une ligne est publiée quand son texte n'a pas
// changé depuis STABLE_MS, ou quand elle disparaît de l'écran. Si la ligne
// s'allonge encore après publication, seule la suite est publiée.
//
// Sélecteurs d'après l'extension « MS Teams Live Captions Saver » et Vexa :
// à confirmer sur le vrai Teams (captures « captions-* » du dossier debug).
(() => {
  if (window.__imvCaptionsReader) return;

  const STABLE_MS = window.__imvCaptionsStableMs || 2500;  // réglable pour les tests
  const CONTAINER_SELECTORS = [
    '[data-tid="closed-captions-renderer"]',
    '[data-tid="closed-caption-v2-window-wrapper"]',
    '[data-tid*="closed-caption"]',
  ];
  const ITEM_SELECTORS = [
    '.fui-ChatMessageCompact',
    '[data-tid="closed-caption-message"]',
    '[class*="ChatMessageCompact"]',
  ];
  const AUTHOR_SELECTORS = ['[data-tid="author"]', '[class*="author"]'];
  const TEXT_SELECTORS = ['[data-tid="closed-caption-text"]', '[class*="caption-text"]'];

  const firstText = (root, selectors) => {
    for (const s of selectors) {
      const t = root.querySelector(s)?.textContent?.trim();
      if (t) return t;
    }
    return '';
  };

  // Ligne affichée → { sender, text, changedAt, published }
  const lines = new Map();
  let container = null;
  let matchedContainer = null;
  let published = 0;

  const emit = (line) => {
    let text = line.text;
    // Suite d'une ligne déjà publiée : seulement la partie nouvelle
    if (line.published && text.startsWith(line.published)) text = text.slice(line.published.length).trim();
    line.published = line.text;
    if (!text) return;
    published++;
    try { window.__imvOnCaption({ sender: line.sender, text }); } catch (e) { /* ne jamais casser la capture */ }
  };

  const findContainer = () => {
    for (const sel of CONTAINER_SELECTORS) {
      const el = document.querySelector(sel);
      if (el) { matchedContainer = sel; return el; }
    }
    return null;
  };

  const items = (container) => {
    for (const sel of ITEM_SELECTORS) {
      const nodes = container.querySelectorAll(sel);
      if (nodes.length) return [...nodes];
    }
    return [];
  };

  // Chaque changement de la zone est vu (une ligne retirée entre deux passages serait perdue) ;
  // le passage périodique publie les lignes devenues stables.
  const observer = new MutationObserver(() => tick());

  const tick = () => {
    const now = Date.now();
    const found = findContainer();
    if (found !== container) {
      container = found;
      observer.disconnect();
      if (container) observer.observe(container, { childList: true, subtree: true, characterData: true });
    }
    const current = new Set(container ? items(container) : []);
    for (const node of current) {
      const text = firstText(node, TEXT_SELECTORS);
      if (!text) continue;
      const line = lines.get(node);
      if (!line) {
        lines.set(node, { sender: firstText(node, AUTHOR_SELECTORS) || 'Inconnu', text, changedAt: now, published: '' });
      } else if (line.text !== text) {
        line.text = text;
        line.changedAt = now;
      }
    }
    for (const [node, line] of lines) {
      const gone = !current.has(node) || !node.isConnected;
      if (line.text !== line.published && (gone || now - line.changedAt >= STABLE_MS)) emit(line);
      if (gone) lines.delete(node);
    }
  };

  const poll = window.setInterval(tick, 500);

  window.__imvCaptionsReader = {
    destroy() { window.clearInterval(poll); observer.disconnect(); },
    state() { return { matchedContainer, pending: lines.size, published }; },
  };
})();
