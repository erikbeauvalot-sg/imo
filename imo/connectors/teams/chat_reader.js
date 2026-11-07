// Lecteur du chat de réunion Teams, injecté dans la page.
// Observe le panneau de chat et appelle window.__imvOnMessage({sender, text,
// messageId, sentAt, backlog}) pour chaque nouveau message. Le panneau doit être
// ouvert pour que les messages soient dans le DOM. `backlog` signale les messages
// déjà présents lors du premier passage.
//
// Adapté de Vexa (https://github.com/Vexa-ai/vexa, Apache-2.0),
// core/meetings/modules/teams-capture/src/teams-chat.ts — voir NOTICE.
(() => {
  if (window.__imvChatReader) return;

  const CONTAINER_SELECTORS = [
    '[data-tid="chat-pane-list"]',
    '[data-tid="message-pane-list-runway"]',
    '[data-tid="chatPaneMessageList"]',
    '[role="log"]',
    '[aria-label*="Chat messages"]',
  ];
  const MESSAGE_SELECTORS = [
    '[data-tid="chat-pane-message"]',
    'div[data-tid^="chat-pane-message"]',
    'div[data-mid]',
    '[data-tid="message"]',
    '[role="listitem"]',
  ];
  const SENDER_SELECTORS = [
    '[data-tid="message-author-name"]',
    '[data-tid*="author"]',
    '[class*="author-name"]',
    '[class*="authorName"]',
  ];
  const TEXT_SELECTORS = [
    '[data-tid="messageBodyContent"]',
    '[id^="content-"]',
    '[class*="messageBody"]',
    '[class*="message-body"]',
    'div[dir="auto"]',
  ];

  const seenKeys = new Set();
  let container = null;
  let matchedContainer = null;
  let lastSender = '';
  let attachedOnce = false;

  const firstText = (root, selectors) => {
    for (const s of selectors) {
      const t = root.querySelector(s)?.textContent?.trim();
      if (t) return t;
    }
    return '';
  };

  // Teams étiquette les lignes « Barbara W, 10:42 AM, Bonjour ».
  const senderFromAria = (node) => {
    let cur = node;
    for (let i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
      const m = (cur.getAttribute?.('aria-label') || '').match(/^(.+?)\s*,\s*\d{1,2}:\d{2}/);
      if (m && m[1].trim()) return m[1].trim();
    }
    return '';
  };

  const extract = (node) => {
    const text = firstText(node, TEXT_SELECTORS);
    if (!text) return null;
    // Teams n'affiche l'auteur qu'une fois pour une suite de messages : on
    // remonte les ancêtres, puis l'aria-label, puis le dernier auteur vu.
    // La remontée s'arrête au conteneur, qui contient les auteurs de tous les messages.
    let sender = firstText(node, SENDER_SELECTORS);
    for (let cur = node.parentElement, i = 0; !sender && cur && cur !== container && i < 4; i++, cur = cur.parentElement) {
      sender = firstText(cur, SENDER_SELECTORS);
    }
    if (!sender) sender = senderFromAria(node);
    sender = sender.replace(/\s*\d{1,2}:\d{2}\s*(AM|PM)?\s*$/i, '').trim() || lastSender || 'Inconnu';
    lastSender = sender;
    const mid = node.getAttribute('data-mid') || node.closest('[data-mid]')?.getAttribute('data-mid') || null;
    const sentAt = node.querySelector('time[datetime]')?.getAttribute('datetime') || null;
    return { sender, text, messageId: mid, sentAt };
  };

  const scan = (backlog = false) => {
    if (!container) return;
    for (const sel of MESSAGE_SELECTORS) {
      const nodes = container.querySelectorAll(sel);
      if (!nodes.length) continue;
      nodes.forEach((node) => {
        const msg = extract(node);
        if (!msg) return;
        // La liste est virtualisée : un même message est re-rendu au défilement.
        const key = msg.messageId || `${msg.sender}\u0000${msg.text}`;
        if (seenKeys.has(key)) return;
        seenKeys.add(key);
        msg.backlog = backlog;
        try { window.__imvOnMessage(msg); } catch (e) { /* ne jamais casser la capture */ }
      });
      return;
    }
  };

  const observer = new MutationObserver(() => scan(false));
  // Le panneau se monte / démonte quand on l'ouvre / ferme : on le recherche
  // régulièrement et on rattache l'observateur.
  const attach = () => {
    for (const sel of CONTAINER_SELECTORS) {
      const el = document.querySelector(sel);
      if (!el) continue;
      if (el !== container) {
        container = el;
        matchedContainer = sel;
        observer.disconnect();
        observer.observe(container, { childList: true, subtree: true, characterData: true });
      }
      scan(!attachedOnce);
      attachedOnce = true;
      return;
    }
  };
  attach();
  const poll = window.setInterval(attach, 2000);

  window.__imvChatReader = {
    destroy() { window.clearInterval(poll); observer.disconnect(); },
    state() { return { matchedContainer, seen: seenKeys.size }; },
  };
})();
