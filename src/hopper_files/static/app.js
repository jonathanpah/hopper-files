"use strict";

(() => {
  const waitText = seconds => {
    const minutes = Math.ceil(seconds / 60);
    return seconds <= 60 ? `${seconds} ${seconds === 1 ? "segundo" : "segundos"}` : `${minutes} ${minutes === 1 ? "minuto" : "minutos"}`;
  };
  const loginForm = document.getElementById("login-form");
  if (loginForm) {
    let waitTimer = 0;
    loginForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = loginForm.querySelector("button[type=submit]");
      const message = document.getElementById("login-status");
      const values = new FormData(loginForm);
      if (button) button.disabled = true;
      document.querySelector(".login-cartao .login-aviso, .login-cartao .login-erro")?.remove();
      if (message) { message.textContent = "Verificando senha…"; message.classList.remove("erro"); }
      try {
        const response = await fetch(loginForm.action, {
          method: "POST", credentials: "same-origin",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ nonce: values.get("nonce"), password: values.get("password") }),
        });
        if (response.ok) {
          window.location.assign(loginForm.dataset.basePath + "app");
          return;
        }
        if (message) {
          const wait = Number(response.headers.get("Retry-After"));
          clearInterval(waitTimer);
          message.textContent = response.status === 429
            ? `Muitas tentativas. Aguarde ${wait > 0 ? waitText(wait) : "um pouco"} antes de tentar de novo.`
            : response.status === 401 ? "Senha incorreta." : "Não foi possível entrar.";
          if (response.status === 429 && wait > 0) {
            const until = Date.now() + wait * 1000;
            waitTimer = setInterval(() => {
              const left = Math.ceil((until - Date.now()) / 1000);
              if (left > 0) { message.textContent = `Muitas tentativas. Aguarde ${waitText(left)} antes de tentar de novo.`; return; }
              clearInterval(waitTimer); message.textContent = "Pode tentar de novo."; message.classList.remove("erro");
            }, 1000);
          }
          message.classList.add("erro");
        }
        // Each attempt consumes the form's nonce; a new one is requested for the next attempt.
        try {
          const fresh = await fetch(loginForm.action, {credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}});
          const data = fresh.ok ? await fresh.json() : null;
          const field = loginForm.querySelector("input[name=nonce]");
          if (data && data.nonce && field) field.value = data.nonce;
        } catch (_error) { /* the message above already covers this case */ }
        const passwordField = loginForm.querySelector("input[name=password]");
        if (passwordField) { passwordField.focus(); passwordField.select(); }
      } catch (_error) {
        if (message) { message.textContent = "Não foi possível conectar a esta instância."; message.classList.add("erro"); }
      } finally {
        if (button) button.disabled = false;
      }
    });
    return;
  }
  // Password change page (HF-AUTH-004), linked from the sign-in page: current password plus the
  // new one twice.
  // The confirmation is checked here before spending an attempt; the server checks it again.
  const passwordForm = document.getElementById("password-form");
  if (passwordForm) {
    let waitTimer = 0;
    const message = document.getElementById("login-status");
    const field = name => passwordForm.querySelector(`input[name=${name}]`);
    const say = (text, error = true) => {
      clearInterval(waitTimer);
      if (!message) return;
      message.textContent = text; message.classList.toggle("erro", error);
    };
    passwordForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      document.querySelector(".login-cartao .login-erro")?.remove();
      const values = new FormData(passwordForm);
      if (values.get("new") !== values.get("confirmation")) {
        say("A nova senha e a confirmação não coincidem.");
        field("confirmation")?.focus(); field("confirmation")?.select();
        return;
      }
      const button = passwordForm.querySelector("button[type=submit]");
      if (button) button.disabled = true;
      say("Trocando a senha…", false);
      try {
        const response = await fetch(passwordForm.action, {
          method: "POST", credentials: "same-origin",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ nonce: values.get("nonce"), current: values.get("current"), new: values.get("new"), confirmation: values.get("confirmation") }),
        });
        if (response.ok) {
          window.location.assign(passwordForm.dataset.basePath + "login?senha=trocada");
          return;
        }
        const wait = Number(response.headers.get("Retry-After"));
        const code = await response.json().then(body => body?.error, () => "");
        say(response.status === 429
          ? `Muitas tentativas. Aguarde ${wait > 0 ? waitText(wait) : "um pouco"} antes de tentar de novo.`
          : response.status === 401 ? "Senha atual incorreta."
          : code === "password_mismatch" ? "A nova senha e a confirmação não coincidem."
          : response.status === 400 ? "A senha excede o limite."
          : "Não foi possível trocar a senha. Tente de novo.");
        if (response.status === 429 && wait > 0) {
          const until = Date.now() + wait * 1000;
          waitTimer = setInterval(() => {
            const left = Math.ceil((until - Date.now()) / 1000);
            if (left > 0) { if (message) message.textContent = `Muitas tentativas. Aguarde ${waitText(left)} antes de tentar de novo.`; return; }
            say("Pode tentar de novo.", false);
          }, 1000);
        }
        // Each attempt may consume the form's nonce; a new one is requested for the next attempt.
        try {
          const fresh = await fetch(passwordForm.action, {credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}});
          const data = fresh.ok ? await fresh.json() : null;
          const nonce = field("nonce");
          if (data && data.nonce && nonce) nonce.value = data.nonce;
        } catch (_error) { /* the message above already covers this case */ }
        const focus = response.status === 401 ? field("current") : field("new");
        if (focus) { focus.focus(); focus.select(); }
      } catch (_error) {
        say("Não foi possível conectar a esta instância.");
      } finally {
        if (button) button.disabled = false;
      }
    });
    return;
  }
  const app = document.getElementById("hf-app");
  if (!app) return;
  const base = app.dataset.basePath;
  const csrf = app.dataset.csrf;
  const $ = (selector) => app.querySelector(selector);
  // Single root (HF-NAV-001): the address carries the fixed identifier "fs" plus the path
  // relative to "/" (HF-NAV-005); the identity shown and persisted is the absolute path.
  const BASE_ID = "fs";
  const BASE_ROOT = {rootId: BASE_ID, label: "/", availability: "available", capabilities: {read: true, write: true, trash: true}};
  const state = {
    roots: [BASE_ROOT], generation: "", ui: null, listingWritable: true, baseUi: null, listingRootId: "", listingPath: "",
    view: "files", rootId: BASE_ID, path: "", entries: [], listingVersion: "",
    treeExpanded: new Map(), treeInitialized: new Set(), treeEntries: new Map(), treeLoading: new Map(), treeErrors: new Set(),
    listInFlight: new Map(), favoriteTreesSaved: null, treeOrigin: null, favoritesDrawPending: false,
    localFilter: "", searchMode: "name", searchUiMode: "name", searchText: "", searchTimer: null, searchRequestId: 0,
    sortDirection: "asc", groupBy: "none", treeOrdering: "name", treeGrouping: "none", treeDirection: "asc", treeSectionOpen: true,
    searchResult: null, tagIndex: null, tagIndexRequest: 0, tagFolders: null, selectedTag: "", selectedLabel: "",
    busy: false, listRequestId: 0, saveBusy: false, saveAgain: false, savePromise: null, conflict: null,
    activeTabPos: null, activeTabKey: null, tablessKey: null,
    documents: new Map(), activeDocument: null, visualRequestId: 0, visualCleanup: null,
    bufferAcks: [], bufferSyncBusy: false, bufferSyncTimer: null, bufferFinished: new Set(),
    trashEntries: [], trashLoad: null, selectedForZip: new Set(), splitEnabled: false, splitDocument: null,
    documentOperations: new Set(), selectedItem: null, dirSizes: new Map(), sizeCells: new Map(), sizeController: null, sessionEnded: false,
    listScroll: new Map(), restoreListScroll: false, focusListOnRender: false, historySeq: 0, saveRetryTimer: null, saveFailures: 0,
    bufferRegistered: true, conflictDeferred: false,
    logoutPhase: "idle", logoutNavigationApproved: false, logoutPostSnapshot: null, logoutCancelHandler: null,
  };

  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function endpoint(name, values = {}) {
    const url = new URL(base + name, window.location.origin);
    for (const [key, value] of Object.entries(values)) {
      if (Array.isArray(value)) value.forEach((part) => url.searchParams.append(key, part));
      else if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
    }
    return url;
  }
  async function request(name, options = {}, values = {}) {
    const response = await fetch(endpoint(name, values), {
      credentials: "same-origin", cache: "no-store",
      ...options,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
    let body = null;
    try { body = await response.json(); } catch (_error) { /* status remains useful */ }
    if (!response.ok) {
      if (response.status === 401) sessionEnded({remind: (options.method || "GET") !== "GET"});
      const error = new Error(body && body.error ? body.error : "Falha ao consultar a instância.");
      error.status = response.status;
      error.body = body;
      throw error;
    }
    return body;
  }
  // remind: a user action (open, save, create) failed after the session-ended notice was closed; show the notice again.
  function sessionEnded({remind = false} = {}) {
    const dialog = $("#operation-dialog");
    if (state.sessionEnded && (!remind || dialog.open)) return;
    if (!state.sessionEnded) {
      state.sessionEnded = true;
      window.clearInterval(state.bufferSyncTimer);
      clearTimeout(state.saveRetryTimer);
      status("Sua sessão terminou. Entre de novo para continuar.", true, {session: true});
    }
    if (dialog.open) dialog.close();
    const form = el("form"); form.method = "dialog";
    const heading = el("h2", "Sua sessão terminou"); heading.id = "operation-dialog-title";
    const dirty = [...state.documents.values()].filter(doc => doc.dirty).length;
    const text = el("p", dirty
      ? `Entre de novo para continuar. ${dirty === 1 ? "Um documento tem" : `${dirty} documentos têm`} alterações não salvas nesta aba: copie o texto antes de sair, se precisar.`
      : "Entre de novo para continuar de onde parou.");
    const menu = el("menu");
    const later = button("Agora não", () => dialog.close(), "secondary-button");
    const login = button("Entrar de novo", () => { window.location.assign(`${base}login`); }, "primary-button");
    menu.append(later, login); form.append(heading, text, menu);
    dialog.classList.remove("dlg-largo"); dialog.replaceChildren(form); dialog.showModal(); login.focus();
    requestAnimationFrame(() => { if (dialog.open && login.isConnected && document.activeElement !== login) login.focus(); });
  }
  function onDialogClosed(dialog, handler) {
    const listener = () => {
      if (dialog.open) return;
      dialog.removeEventListener("close", listener);
      handler();
    };
    dialog.addEventListener("close", listener);
  }
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function button(text, action, className = "") {
    const node = el("button", text, className);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  }
  const svgNamespace = "http://www.w3.org/2000/svg";
  // Type color uses currentColor, driven by a tipo-* class (--tipo-* tokens).
  const DS_ICONS = {
    "adicionar": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<path d=\"M8 10.5V3M5 6l3-3 3 3M3 12.5h10\"/>"],
    "agrupar": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<rect x=\"2.5\" y=\"3\" width=\"11\" height=\"4\" rx=\"1.2\"/><rect x=\"2.5\" y=\"9\" width=\"11\" height=\"4\" rx=\"1.2\"/>"],
    "autosalvar": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<path d=\"M13.4 8a5.4 5.4 0 1 1-1.6-3.8\"/><path d=\"M13.6 1.8v2.6H11\"/><path d=\"M5.8 8.2l1.6 1.6 2.8-3\"/>"],
    "baixar": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<path d=\"M8 2.5V10M5 7l3 3 3-3M3 12.5h10\"/>"],
    "buscar": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<circle cx=\"7\" cy=\"7\" r=\"4.5\"/><path d=\"M10.5 10.5L14 14\"/>"],
    "configuracoes": [{"viewBox": "0 0 24 24", "fill": "none", "stroke": "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round"}, "<circle cx=\"12\" cy=\"12\" r=\"3\"/><path d=\"M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z\"/>"],
    "copiar-caminho": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<rect x=\"5.5\" y=\"5.5\" width=\"8\" height=\"8\" rx=\"2\"/><path d=\"M10.5 5.5v-1a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h1\"/>"],
    "copiar-texto": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<path d=\"M4 1.8h5.2L12.8 5.4V13a1.4 1.4 0 0 1-1.4 1.4H4A1.4 1.4 0 0 1 2.6 13V3.2A1.4 1.4 0 0 1 4 1.8z\"/><path d=\"M5.4 8h5.2M5.4 10.4h5.2\"/>"],
    "densidade": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.3"}, "<path d=\"M2.5 4h11M2.5 8h11M2.5 12h11\"/>"],
    "desfazer": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4", "stroke-linecap": "round", "stroke-linejoin": "round"}, "<path d=\"M3 6.4h6.2a3.4 3.4 0 0 1 0 6.8H6\"/><path d=\"M5.4 3.4L2.6 6.4l2.8 3\"/>"],
    "dividir": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<rect x=\"2\" y=\"3\" width=\"12\" height=\"10\" rx=\"1.6\"/><path d=\"M8 3v10\"/>"],
    "favorito": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4", "stroke-linejoin": "round"}, "<path d=\"M8 2.2l1.75 3.55 3.92.57-2.84 2.76.67 3.9L8 11.15l-3.5 1.83.67-3.9L2.33 6.32l3.92-.57z\"/>"],
    "informacoes": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<circle cx=\"8\" cy=\"8\" r=\"6.5\"/><path d=\"M8 7.2v4\"/><circle cx=\"8\" cy=\"4.8\" r=\".8\" fill=\"currentColor\" stroke=\"none\"/>"],
    "inicio": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<path d=\"M2.5 7.5L8 2.8l5.5 4.7\"/><path d=\"M3.8 6.6V13a.6.6 0 0 0 .6.6h7.2a.6.6 0 0 0 .6-.6V6.6\"/>"],
    "lixeira": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<path d=\"M3 4.5h10M6.2 4.5V3.2A1 1 0 0 1 7.2 2.2h1.6a1 1 0 0 1 1 1v1.3M4.3 4.5l.6 8a1.2 1.2 0 0 0 1.2 1.1h3.8a1.2 1.2 0 0 0 1.2-1.1l.6-8\"/>"],
    "mais": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<circle cx=\"3.2\" cy=\"8\" r=\"1.1\" fill=\"currentColor\" stroke=\"none\"/><circle cx=\"8\" cy=\"8\" r=\"1.1\" fill=\"currentColor\" stroke=\"none\"/><circle cx=\"12.8\" cy=\"8\" r=\"1.1\" fill=\"currentColor\" stroke=\"none\"/>"],
    "menu": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4", "stroke-linecap": "round"}, "<path d=\"M2.5 4.5h11M2.5 8h11M2.5 11.5h11\"/>"],
    "nova-pasta": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<path d=\"M1.8 5.2A1.6 1.6 0 0 1 3.4 3.6h2.2l1 1.2h5.6a1.6 1.6 0 0 1 1.6 1.6v5.2a1.6 1.6 0 0 1-1.6 1.6H3.4a1.6 1.6 0 0 1-1.6-1.6z\"/><path d=\"M8 8v3M6.5 9.5h3\"/>"],
    "novo-arquivo": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<path d=\"M4 1.8h5.2L12.8 5.4V13a1.4 1.4 0 0 1-1.4 1.4H4A1.4 1.4 0 0 1 2.6 13V3.2A1.4 1.4 0 0 1 4 1.8z\"/><path d=\"M8 7v4M6 9h4\"/>"],
    "ocultos": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.3"}, "<path d=\"M1.5 8S3.8 3.8 8 3.8 14.5 8 14.5 8 12.2 12.2 8 12.2 1.5 8 1.5 8z\"/><circle cx=\"8\" cy=\"8\" r=\"1.9\"/>"],
    "ordenar": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<path d=\"M3 4.5h10M3 8h7M3 11.5h4\"/>"],
    "quebra-de-linha": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4"}, "<path d=\"M2 4h12M2 8h8.5a2.2 2.2 0 0 1 0 4.4H7\"/><path d=\"M8.6 10.6L7 12l1.6 1.4\"/><path d=\"M2 12h3\"/>"],
    "refazer": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4", "stroke-linecap": "round", "stroke-linejoin": "round"}, "<path d=\"M13 6.4H6.8a3.4 3.4 0 0 0 0 6.8H10\"/><path d=\"M10.6 3.4l2.8 3-2.8 3\"/>"],
    "teclado": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.3"}, "<rect x=\"1.5\" y=\"4\" width=\"13\" height=\"8\" rx=\"1.5\"/><path d=\"M3.5 6.5h1M6 6.5h1M8.5 6.5h1M11 6.5h1M3.5 9.5h1M6 9.5h4M11.5 9.5h1\"/>"],
    "tema": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.3"}, "<path d=\"M13.2 9.4A5.2 5.2 0 0 1 6.6 2.8 5.4 5.4 0 1 0 13.2 9.4z\"/>"],
    "tipo-codigo": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.3"}, "<path d=\"M6 5L3 8l3 3M10 5l3 3-3 3\"/>"],
    "tipo-compactado": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.2"}, "<path d=\"M4 1.8h5.2L12.8 5.4V13a1.4 1.4 0 0 1-1.4 1.4H4A1.4 1.4 0 0 1 2.6 13V3.2A1.4 1.4 0 0 1 4 1.8z\"/><path d=\"M9 1.8v3.8h3.8\"/><text x=\"8\" y=\"12\" fill=\"currentColor\" stroke=\"none\" font-size=\"4.2\" font-weight=\"700\" text-anchor=\"middle\" font-family=\"sans-serif\">zip</text>"],
    "tipo-csv": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.2"}, "<path d=\"M4 1.8h5.2L12.8 5.4V13a1.4 1.4 0 0 1-1.4 1.4H4A1.4 1.4 0 0 1 2.6 13V3.2A1.4 1.4 0 0 1 4 1.8z\"/><path d=\"M9 1.8v3.8h3.8\"/><text x=\"8\" y=\"12\" fill=\"currentColor\" stroke=\"none\" font-size=\"4.2\" font-weight=\"700\" text-anchor=\"middle\" font-family=\"sans-serif\">csv</text>"],
    "tipo-documento": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.2"}, "<path d=\"M4 1.8h5.2L12.8 5.4V13a1.4 1.4 0 0 1-1.4 1.4H4A1.4 1.4 0 0 1 2.6 13V3.2A1.4 1.4 0 0 1 4 1.8z\"/><path d=\"M9 1.8v3.8h3.8\"/>"],
    "tipo-imagem": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.2"}, "<rect x=\"2\" y=\"2.8\" width=\"12\" height=\"10.4\" rx=\"1.6\"/><circle cx=\"5.6\" cy=\"6.4\" r=\"1.1\" fill=\"currentColor\" stroke=\"none\"/><path d=\"M3 12l3.4-3.4 2.2 2.2 2.6-2.9L14 11\"/>"],
    "tipo-markdown": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.2"}, "<path d=\"M4 1.8h5.2L12.8 5.4V13a1.4 1.4 0 0 1-1.4 1.4H4A1.4 1.4 0 0 1 2.6 13V3.2A1.4 1.4 0 0 1 4 1.8z\"/><path d=\"M9 1.8v3.8h3.8\"/>"],
    "tipo-midia": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.3"}, "<circle cx=\"8\" cy=\"8\" r=\"6.2\"/><path d=\"M6.4 5.5l4 2.5-4 2.5z\" fill=\"currentColor\" stroke=\"none\"/>"],
    "tipo-pasta": [{"viewBox": "0 0 16 16", "fill": "currentColor"}, "<path d=\"M1.5 4.5A2 2 0 0 1 3.5 2.5h2.6a1.5 1.5 0 0 1 1.2.6l.7.9h4.5a2 2 0 0 1 2 2v5.5a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2z\"/>"],
    "tipo-pdf": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.2"}, "<path d=\"M4 1.8h5.2L12.8 5.4V13a1.4 1.4 0 0 1-1.4 1.4H4A1.4 1.4 0 0 1 2.6 13V3.2A1.4 1.4 0 0 1 4 1.8z\"/><path d=\"M9 1.8v3.8h3.8\"/><text x=\"8\" y=\"12\" fill=\"currentColor\" stroke=\"none\" font-size=\"4.2\" font-weight=\"700\" text-anchor=\"middle\" font-family=\"sans-serif\">pdf</text>"],
    "tipo-planilha": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.2"}, "<path d=\"M4 1.8h5.2L12.8 5.4V13a1.4 1.4 0 0 1-1.4 1.4H4A1.4 1.4 0 0 1 2.6 13V3.2A1.4 1.4 0 0 1 4 1.8z\"/><path d=\"M9 1.8v3.8h3.8\"/><text x=\"8\" y=\"12\" fill=\"currentColor\" stroke=\"none\" font-size=\"4.2\" font-weight=\"700\" text-anchor=\"middle\" font-family=\"sans-serif\">X</text>"],
    "tipo-word": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.2"}, "<path d=\"M4 1.8h5.2L12.8 5.4V13a1.4 1.4 0 0 1-1.4 1.4H4A1.4 1.4 0 0 1 2.6 13V3.2A1.4 1.4 0 0 1 4 1.8z\"/><path d=\"M9 1.8v3.8h3.8\"/><text x=\"8\" y=\"12\" fill=\"currentColor\" stroke=\"none\" font-size=\"4.2\" font-weight=\"700\" text-anchor=\"middle\" font-family=\"sans-serif\">W</text>"],
  };
  // Icons outside the shared set: update, save, and empty folder; rename, a pencil on the 12x12
  // grid of close; and close, a 10px × with a 1.6 stroke.
  const EXTRA_ICONS = {
    "atualizar": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4", "stroke-linecap": "round", "stroke-linejoin": "round"}, "<path d=\"M13.2 7.8a5.2 5.2 0 1 1-1.4-3.5\"/><path d=\"M13 2v3h-3\"/>"],
    "salvar": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4", "stroke-linejoin": "round"}, "<path d=\"M2.5 2.5h9l2 2v9h-11z\"/><path d=\"M5 2.5v4h6v-4\"/><path d=\"M5 13.5V9h6v4.5\"/>"],
    "renomear": [{"viewBox": "0 0 12 12", "fill": "none", "stroke": "currentColor", "stroke-width": "1.4", "stroke-linecap": "round", "stroke-linejoin": "round"}, "<path d=\"M2 10l.5-2.3L8 2.2 9.8 4 4.3 9.5z\"/>"],
    "pasta-vazia": [{"viewBox": "0 0 16 16", "fill": "none", "stroke": "currentColor", "stroke-width": "1"}, "<path d=\"M1.8 5.2A1.6 1.6 0 0 1 3.4 3.6h2.2l1 1.2h5.6a1.6 1.6 0 0 1 1.6 1.6v5.2a1.6 1.6 0 0 1-1.6 1.6H3.4a1.6 1.6 0 0 1-1.6-1.6z\"/>"],
    "fechar": [{"viewBox": "0 0 12 12", "fill": "none", "stroke": "currentColor", "stroke-width": "1.6", "stroke-linecap": "round"}, "<path d=\"M2 2l8 8M10 2l-8 8\"/>"],
  };
  // One icon size per context: 14px for controls, 15px for inline type, 32px for empty states,
  // 10px for the close ×.
  function svgIcon(name, size = 14) {
    const [attributes, inner] = DS_ICONS[name] || EXTRA_ICONS[name];
    const svg = document.createElementNS(svgNamespace, "svg");
    for (const [key, value] of Object.entries(attributes)) svg.setAttribute(key, value);
    svg.setAttribute("width", String(size)); svg.setAttribute("height", String(size));
    svg.setAttribute("aria-hidden", "true"); svg.setAttribute("focusable", "false");
    svg.innerHTML = inner;
    return svg;
  }
  function fileTypeName(fileName) {
    const ext = fileName.split("/").at(-1).split(".").at(-1).toLowerCase();
    if (/^(png|jpe?g|gif|webp|svg|heic|avif|bmp|tiff?)$/.test(ext)) return "tipo-imagem";
    if (/^(mp3|wav|m4a|flac|mp4|mov|mkv|webm|avi)$/.test(ext)) return "tipo-midia";
    if (/^(json|ya?ml|toml|xml|[jt]sx?|py|sh|bash|css|html|rs|go|c|cpp|java|sql)$/.test(ext)) return "tipo-codigo";
    if (/^(zip|gz|tar|tgz|7z|rar)$/.test(ext)) return "tipo-compactado";
    if (ext === "pdf") return "tipo-pdf";
    if (["doc", "docx"].includes(ext)) return "tipo-word";
    if (["xls", "xlsx"].includes(ext)) return "tipo-planilha";
    if (ext === "csv") return "tipo-csv";
    if (["md", "markdown"].includes(ext)) return "tipo-markdown";
    return "tipo-documento";
  }
  // Type icon in list, tree, and search-result rows: 15px; color comes from the tipo-* class.
  function iconNode(kind, fileName = "") {
    const name = kind === "folder" ? "tipo-pasta" : fileTypeName(fileName || "");
    const svg = svgIcon(name, 15);
    svg.classList.add(name === "tipo-csv" ? "tipo-planilha" : name);
    return svg;
  }
  function controlIcon(name) { return svgIcon(name, 14); }
  // Empty state: a 32px icon in text2 with a 1px stroke and a short centered 13px caption; inside
  // lists (tree), only the 12.5px caption.
  const EMPTY_ICONS = {pasta: "pasta-vazia", documento: "tipo-documento", busca: "buscar", lixeira: "lixeira", compactado: "tipo-compactado"};
  function emptyState(message, icon = "pasta") {
    const node = el("div", undefined, "aviso-central");
    const svg = svgIcon(EMPTY_ICONS[icon] || icon, 32);
    svg.setAttribute("stroke-width", "1");
    node.append(svg, el("div", message));
    return node;
  }
  // Label color uses the system token (--tag-<id>); ids are the seven fixed values from HF-META-003.
  function labelColor(id) { return `var(--tag-${id})`; }
  function closeButton(label, action, className) {
    const node = button("", action, className);
    node.setAttribute("aria-label", label);
    node.append(svgIcon("fechar", 10));
    return node;
  }
  function actionButton(label, iconName, action) {
    const node = button("", action, "acao so-icone");
    node.setAttribute("aria-label", label);
    node.dataset.tip = label;
    node.append(controlIcon(iconName));
    return node;
  }
  // Toast: a short success confirmation, past participle, no trailing period. Errors and
  // conflicts skip the toast; they stay in the status bar or in the document itself.
  function toast(message) {
    const node = $("#toast");
    if (!node) return;
    node.textContent = String(message).replace(/\.$/, "");
    node.classList.add("mostra");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => node.classList.remove("mostra"), 2600);
  }
  function status(message, isError = false, {session = false} = {}) {
    // After the session ends, the status bar keeps this notice instead of generic errors.
    if (state.sessionEnded && !session) return;
    const node = $("#app-status");
    node.textContent = message || "";
    node.classList.toggle("error", isError);
  }
  // HF-FILE-002: across filesystems, only the moved name leaves; other hard-linked names of the
  // same file stay at the source. The notice replaces the usual confirmation in the central alert
  // and stays in the status bar without an error tone.
  function separatedLinksNotice(count) {
    if (!count) return;
    const message = count === 1
      ? "Vínculo físico separado: os outros nomes continuam no arquivo original."
      : `${count} vínculos físicos separados: os outros nomes continuam nos arquivos originais.`;
    toast(message); status(message);
  }
  // The wider width (520px) applies only to the dialog that requested it.
  $("#operation-dialog")?.addEventListener("close", event => { if (!event.currentTarget.open) event.currentTarget.classList.remove("dlg-largo"); });
  const logoutForm = $("#logout-form");
  if (logoutForm) {
    const logoutButton = logoutForm.querySelector('button[type="submit"]');
    logoutForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void performSafeLogout({logoutButton, logoutForm});
    });
  }
  function captureLogoutState() {
    return new Map([...state.documents].map(([key, doc]) => [key, {doc, value: doc.getValue()}]));
  }
  function logoutStateChanged(snapshot) {
    if (state.documents.size !== snapshot.size) return true;
    for (const [key, previous] of snapshot) {
      const current = state.documents.get(key);
      if (current !== previous.doc || current.getValue() !== previous.value) return true;
    }
    return false;
  }
  function pendingLogoutDocuments() {
    return [...state.documents.values()].filter(doc => dirtyDocument(doc) || doc.saving || doc.saveAgain);
  }
  function trackDocumentOperation(kind, operation, documents = []) {
    if (state.logoutPhase !== "idle") return null;
    if (documents.some(doc => documentOperationsFor(doc).some(current => current.kind === "trash-delete")) ||
        (kind === "trash-delete" && documents.some(hasDocumentOperation))) return null;
    const record = {kind, documents, promise: null};
    state.documentOperations.add(record);
    record.promise = Promise.resolve().then(operation).finally(() => state.documentOperations.delete(record));
    return record.promise;
  }
  async function waitForDocumentOperations() {
    while (state.documentOperations.size) {
      showLogoutProgress("Aguardando operações que podem abrir ou alterar documentos…");
      const batch = [...state.documentOperations];
      const results = await Promise.allSettled(batch.map(operation => operation.promise));
      const failed = results.find(result => result.status === "rejected");
      if (failed) throw failed.reason;
    }
  }
  function setDocumentReadOnly(doc, frozen = state.logoutPhase !== "idle") {
    doc.source?.setReadOnly?.(Boolean(frozen || doc.held || doc.editable === false));
  }
  function logoutPostConsentStillValid() {
    return Boolean(state.logoutPostSnapshot) && state.documentOperations.size === 0 &&
      ![...state.documents.values()].some(doc => doc.saving || doc.saveAgain) &&
      !logoutStateChanged(state.logoutPostSnapshot);
  }
  function freezeLogoutBackground(frozen, logoutButton) {
    for (const selector of [".topo", "#sidebar-toggle", "#app-layout"]) {
      const element = $(selector);
      if (element) {
        try { element.inert = frozen; }
        catch (error) { if (frozen) throw error; }
      }
    }
    let firstError = null;
    for (const doc of state.documents.values()) {
      try { setDocumentReadOnly(doc, frozen); }
      catch (error) { firstError ||= error; }
    }
    if (logoutButton) logoutButton.disabled = frozen;
    if (frozen && firstError) throw firstError;
  }
  function showLogoutDecision(documentCount) {
    const dialog = $("#operation-dialog");
    if (dialog.open) dialog.close();
    if (state.logoutCancelHandler) {
      dialog.removeEventListener("cancel", state.logoutCancelHandler);
      state.logoutCancelHandler = null;
    }
    const heading = el("h2", "Há alterações não salvas"); heading.id = "operation-dialog-title";
    const message = el("p", documentCount === 1 ? "Há alterações ou salvamentos em andamento em 1 documento." : `Há alterações ou salvamentos em andamento em ${documentCount} documentos.`);
    const hint = el("p", "Escolha salvar todos os documentos, descartar as alterações atuais ou continuar editando.", "dialog-hint");
    const menu = el("menu");
    let finish;
    const result = new Promise(resolve => { finish = resolve; });
    let settled = false;
    const resolveOnce = choice => {
      if (settled) return;
      settled = true;
      dialog.removeEventListener("cancel", cancel);
      if (dialog.open) dialog.close();
      finish(choice);
    };
    const cancel = event => { event.preventDefault(); resolveOnce(null); };
    dialog.addEventListener("cancel", cancel);
    menu.append(
      button("Continuar editando", () => resolveOnce(null), "secondary-button"),
      button("Descartar e sair", () => resolveOnce("discard"), "secondary-button"),
      button("Salvar e sair", () => resolveOnce("save"), "primary-button"),
    );
    dialog.replaceChildren(heading, message, hint, menu);
    dialog.showModal();
    menu.querySelector("button")?.focus();
    return result;
  }
  function showLogoutProgress(message) {
    const dialog = $("#operation-dialog");
    if (state.logoutCancelHandler) dialog.removeEventListener("cancel", state.logoutCancelHandler);
    const heading = el("h2", "Preparando saída segura"); heading.id = "operation-dialog-title";
    const progress = el("p", message);
    progress.setAttribute("role", "status");
    dialog.replaceChildren(heading, progress);
    state.logoutCancelHandler = event => event.preventDefault();
    dialog.addEventListener("cancel", state.logoutCancelHandler);
    if (!dialog.open) dialog.showModal();
  }
  function finishSafeLogout({logoutButton, message, isError = false}) {
    const dialog = $("#operation-dialog");
    state.logoutPhase = "idle";
    state.logoutNavigationApproved = false;
    state.logoutPostSnapshot = null;
    if (state.logoutCancelHandler) {
      dialog.removeEventListener("cancel", state.logoutCancelHandler);
      state.logoutCancelHandler = null;
    }
    if (dialog.open) dialog.close();
    freezeLogoutBackground(false, logoutButton);
    if (message) status(message, isError);
  }
  async function performSafeLogout({logoutButton, logoutForm}) {
    if (state.logoutPhase !== "idle") return;
    state.logoutPhase = "preparing";
    try {
      freezeLogoutBackground(true, logoutButton);
      while (true) {
        await waitForDocumentOperations();
        state.logoutPhase = "preparing";
        const decisionState = captureLogoutState();
        const pending = pendingLogoutDocuments();
        let choice = "clean";
        if (pending.length) {
          state.logoutPhase = "decision";
          choice = await showLogoutDecision(pending.length);
          if (!choice) {
            finishSafeLogout({logoutButton, message: "Saída cancelada. Seus documentos e sua sessão continuam disponíveis."});
            return;
          }
        }

        if (choice === "save") {
          state.logoutPhase = "saving";
          showLogoutProgress("Salvando os documentos alterados…");
          for (const {doc} of decisionState.values()) {
            if (!dirtyDocument(doc) && !doc.saving && !doc.saveAgain) continue;
            const result = await saveDocument(doc, "safe-exit");
            if (!result.ok) {
              finishSafeLogout({logoutButton, message: "Não foi possível salvar todos os documentos. A sessão continua ativa e os textos abertos foram mantidos.", isError: true});
              return;
            }
          }
        } else if (choice === "discard") {
          state.logoutPhase = "waiting";
          showLogoutProgress("Aguardando os salvamentos em andamento…");
          for (const {doc} of decisionState.values()) {
            if (!doc.saving || !doc.savePromise) continue;
            const result = await doc.savePromise;
            if (!result.ok) {
              finishSafeLogout({logoutButton, message: "Um salvamento falhou. A sessão continua ativa e os textos abertos foram mantidos.", isError: true});
              return;
            }
          }
        }

        await waitForDocumentOperations();
        state.logoutPhase = "preparing";
        const changedDuringDecision = logoutStateChanged(decisionState);
        const stillPending = pendingLogoutDocuments();
        if ((changedDuringDecision && stillPending.length) || (choice === "save" && stillPending.length)) {
          showLogoutProgress("O conteúdo mudou durante a saída. Confirme de novo a ação para os textos abertos.");
          continue;
        }
        if (stillPending.some(doc => doc.saving)) {
          showLogoutProgress("Aguardando os salvamentos em andamento…");
          continue;
        }
        // A choice to discard applies only to the exact buffers shown in that prompt.
        // New or changed dirty documents return to the decision prompt before logout.
        if (changedDuringDecision && stillPending.length) continue;
        break;
      }

      await waitForDocumentOperations();
      state.logoutPhase = "preparing";
      if (state.documentOperations.size || pendingLogoutDocuments().some(doc => doc.saving || doc.saveAgain)) {
        finishSafeLogout({logoutButton, message: "Uma operação de documento ainda está em andamento. A sessão continua ativa; tente sair novamente quando ela terminar.", isError: true});
        return;
      }
      const beforePost = captureLogoutState();
      state.logoutPostSnapshot = beforePost;
      state.logoutPhase = "posting";
      showLogoutProgress("Encerrando a sessão…");
      const response = await fetch(logoutForm.action, {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: {
          Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrf,
        },
        body: JSON.stringify({csrfToken: csrf}),
      });
      if (!response.ok) {
        finishSafeLogout({logoutButton, message: response.status === 401 ? "Sua sessão expirou. Entre novamente." : "Não foi possível encerrar a sessão.", isError: true});
        return;
      }
      if (!logoutPostConsentStillValid()) {
        finishSafeLogout({logoutButton, message: "O conteúdo mudou durante o encerramento. A navegação foi interrompida para manter os textos abertos; confira o estado da sessão antes de continuar.", isError: true});
        return;
      }
      state.logoutPhase = "leaving";
      state.logoutNavigationApproved = true;
      window.location.assign(base + "login");
    } catch (_error) {
      finishSafeLogout({logoutButton, message: "Não foi possível confirmar a saída. Os textos abertos continuam nesta tela.", isError: true});
    } finally {
      if (state.logoutPhase !== "leaving" && state.logoutPhase !== "idle") {
        finishSafeLogout({logoutButton});
      }
    }
  }
  function askText(title, label, initialValue, submitLabel = "Continuar", {preserveWhitespace = false, stemOnly = false, maxLength = 1024, validate = null} = {}) {
    const dialog = $("#operation-dialog");
    if (dialog.open) return Promise.resolve(null);
    const form = el("form");
    form.method = "dialog";
    const heading = el("h2", title); heading.id = "operation-dialog-title";
    const fieldLabel = el("label", label);
    const input = el("input"); input.type = "text"; input.required = true; input.maxLength = maxLength; input.value = initialValue;
    fieldLabel.append(input);
    const menu = el("menu");
    const cancel = button("Cancelar", () => dialog.close(), "secondary-button");
    const submit = el("button", submitLabel, "primary-button"); submit.type = "submit";
    menu.append(cancel, submit);
    // The reason is shown inside the dialog itself, which stays open with the typed text.
    const reason = el("p", "", "dialog-hint dialog-motivo"); reason.hidden = true; reason.setAttribute("aria-live", "polite");
    form.append(heading, fieldLabel, reason, menu);
    dialog.replaceChildren(form);
    return new Promise((resolve) => {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const value = preserveWhitespace ? input.value : input.value.trim();
        if (!value) { input.focus(); return; }
        const problem = validate ? validate(value) : "";
        if (problem) { reason.textContent = problem; reason.hidden = false; input.focus(); return; }
        resolve(value);
        dialog.close();
      });
      input.addEventListener("input", () => { reason.hidden = true; });
      onDialogClosed(dialog, () => resolve(null));
      dialog.showModal();
      if (stemOnly) selectStem(input); else { input.focus(); input.select(); }
    });
  }
  // Confirmation uses the same <dialog> as other operations. If another dialog is already open, it
  // is not replaced: the answer defaults to "no" and the action has no effect. Closing a changed
  // document offers Save, Don't Save, or Cancel, as on macOS.
  function askCloseDirty(doc) {
    const dialog = $("#operation-dialog");
    const form = el("form"); form.method = "dialog";
    const heading = el("h2", `Salvar as alterações em ${doc.name}?`); heading.id = "operation-dialog-title";
    const text = el("p", "Se não salvar, as alterações feitas desde o último salvamento serão perdidas.");
    const menu = el("menu");
    let answer = null;
    const pick = value => () => { answer = value; dialog.close(); };
    const discard = button("Não salvar", pick("discard"), "secondary-button perigo-texto");
    const cancel = button("Cancelar", pick(null), "secondary-button");
    const save = button("Salvar", pick("save"), "primary-button");
    save.disabled = !doc.editable;
    menu.append(discard, cancel, save); form.append(heading, text, menu);
    dialog.classList.remove("dlg-largo"); dialog.replaceChildren(form);
    return new Promise(resolve => {
      onDialogClosed(dialog, () => resolve(answer));
      dialog.showModal(); (doc.editable ? save : cancel).focus();
    });
  }
  function askConfirm(title, message, confirmLabel, {danger = false} = {}) {
    const dialog = $("#operation-dialog");
    if (dialog.open) return Promise.resolve(false);
    const origin = document.activeElement;
    const form = el("form"); form.method = "dialog";
    const heading = el("h2", title); heading.id = "operation-dialog-title";
    const text = el("p", message, "dlg-texto");
    const menu = el("menu");
    const cancel = button("Cancelar", () => dialog.close(), "secondary-button");
    const submit = el("button", confirmLabel, danger ? "danger-button" : "primary-button"); submit.type = "submit";
    menu.append(cancel, submit); form.append(heading, text, menu);
    dialog.replaceChildren(form);
    return new Promise(resolve => {
      let settled = false;
      const settle = value => { if (!settled) { settled = true; resolve(value); } };
      form.addEventListener("submit", event => { event.preventDefault(); settle(true); dialog.close(); });
      onDialogClosed(dialog, () => { settle(false); if (origin?.isConnected) origin.focus(); });
      dialog.showModal();
      (danger ? cancel : submit).focus();
    });
  }
  function installSplitter(node, minimum, maximum, initial, apply, direction = "horizontal", commit = null) {
    let value = initial;
    node.setAttribute("aria-valuemin", String(minimum)); node.setAttribute("aria-valuemax", String(maximum));
    const update = next => {
      value = Math.max(minimum, Math.min(maximum, Math.round(next)));
      node.setAttribute("aria-valuenow", String(value));
      apply(value);
    };
    node.addEventListener("keydown", event => {
      const forward = direction === "horizontal" ? ["ArrowRight", "ArrowDown"] : ["ArrowDown", "ArrowRight"];
      const backward = direction === "horizontal" ? ["ArrowLeft", "ArrowUp"] : ["ArrowUp", "ArrowLeft"];
      const before = value;
      const step = (event.shiftKey ? 40 : 10) * (node.id === "workspace-splitter" ? -1 : 1);
      if (forward.includes(event.key)) { event.preventDefault(); update(value + step); }
      else if (backward.includes(event.key)) { event.preventDefault(); update(value - step); }
      else if (event.key === "Home") { event.preventDefault(); update(minimum); }
      else if (event.key === "End") { event.preventDefault(); update(maximum); }
      if (commit && value !== before) commit(value);
    });
    node.addEventListener("pointerdown", event => {
      event.preventDefault(); node.setPointerCapture(event.pointerId);
      // The sidebar's width transition is disabled while the pointer is captured.
      if (node.id === "sidebar-splitter") document.body.classList.add("resizing-side");
      const start = direction === "horizontal" ? event.clientX : event.clientY;
      const baseValue = value;
      const extentNode = node.id === "workspace-splitter" ? $("#app-layout") : node.parentElement;
      const extent = direction === "horizontal" ? extentNode.getBoundingClientRect().width : extentNode.getBoundingClientRect().height;
      const sign = node.id === "workspace-splitter" ? -1 : 1;
      const move = moveEvent => {
        const offset = sign * ((direction === "horizontal" ? moveEvent.clientX : moveEvent.clientY) - start);
        if (node.id === "sidebar-splitter") update(baseValue + offset);
        else if (extent > 0) update(baseValue + offset / extent * 100);
      };
      const finish = () => {
        node.removeEventListener("pointermove", move);
        node.removeEventListener("pointerup", finish);
        node.removeEventListener("pointercancel", finish);
        node.removeEventListener("lostpointercapture", finish);
        document.body.classList.remove("resizing-side");
        if (commit && value !== baseValue) commit(value);
      };
      node.addEventListener("pointermove", move);
      node.addEventListener("pointerup", finish);
      node.addEventListener("pointercancel", finish);
      node.addEventListener("lostpointercapture", finish);
    });
    update(initial);
  }
  // Top-bar tooltips use a fixed layer inside the window: a [data-tip] ::after would be clipped
  // by the overflow of main and #toolbar.
  function installTopTips() {
    const bar = document.querySelector(".topo");
    if (!bar) return;
    const tip = el("div", undefined, "tip-flutuante"); tip.setAttribute("aria-hidden", "true");
    document.body.append(tip);
    let timer = 0, owner = null;
    const hide = () => { clearTimeout(timer); owner = null; tip.classList.remove("mostra"); };
    const show = target => {
      clearTimeout(timer); owner = target; tip.classList.remove("mostra");
      timer = setTimeout(() => {
        if (owner !== target || !target.isConnected || !target.dataset.tip) return;
        tip.textContent = target.dataset.tip;
        const box = target.getBoundingClientRect();
        const left = Math.max(8, Math.min(box.left + box.width / 2 - tip.offsetWidth / 2, window.innerWidth - tip.offsetWidth - 8));
        tip.style.left = `${Math.round(left)}px`; tip.style.top = `${Math.round(box.bottom + 6)}px`;
        tip.classList.add("mostra");
      }, 350);
    };
    const tipOwner = event => { const target = event.target.closest?.("[data-tip]"); return target && bar.contains(target) ? target : null; };
    // Tooltips are for mouse and trackpad only; on touch they would stay stuck after a tap.
    bar.addEventListener("pointerover", event => { if (event.pointerType !== "mouse") return; const target = tipOwner(event); if (target && target !== owner) show(target); });
    bar.addEventListener("pointerout", event => { const target = tipOwner(event); if (target && !target.contains(event.relatedTarget)) hide(); });
    // Focus shows the tooltip only when the keyboard moved it (Tab and arrow keys); after a tap or
    // typing in a dialog, focus returned to the toolbar does not bring back the tooltip.
    let keyboard = false;
    const navigation = new Set(["Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);
    document.addEventListener("keydown", event => { if (navigation.has(event.key)) keyboard = true; }, true);
    document.addEventListener("pointerdown", () => { keyboard = false; }, true);
    bar.addEventListener("focusin", event => { const target = tipOwner(event); if (keyboard && target?.matches(":focus-visible")) show(target); });
    bar.addEventListener("focusout", hide);
    bar.addEventListener("pointerdown", hide);
    window.addEventListener("resize", hide);
    window.addEventListener("scroll", hide, true);
  }
  function setupLayoutControls() {
    installTopTips();
    const savedWidth = state.ui.preferences.sidebarWidth;
    installSplitter($("#sidebar-splitter"), 200, 420, Number.isInteger(savedWidth) ? savedWidth : 250, value => {
      $("#app-layout").style.setProperty("--sidebar-w", `${value}px`);
      $("#sidebar-splitter").setAttribute("aria-valuenow", String(value));
    }, "horizontal", value => {
      // Saves only when the drag ends (or on each key press); the server's response does not reapply the width.
      state.ui.preferences.sidebarWidth = value; scheduleSave();
    });
    const splitHandle = $("#workspace-splitter");
    installSplitter(splitHandle, 30, 70, 46, value => {
      $("#app-layout").style.setProperty("--split-width", `${value}%`);
      requestAnimationFrame(() => {
        const layout = $("#app-layout").getBoundingClientRect().width, pane = $("#split-pane").getBoundingClientRect().width;
        if (layout > 0 && pane > 0) splitHandle.setAttribute("aria-valuetext", `${Math.round(pane / layout * 100)}% da largura para o documento`);
      });
    });
    window.addEventListener("resize", () => {
      const mobile = window.matchMedia("(max-width: 720px)").matches;
      if (mobile && state.splitEnabled) setSplitEnabled(false);
      if (mobile) $("#app-layout").classList.remove("sidebar-collapsed");
      else { $("#sidebar").classList.remove("sidebar-open"); $("#sidebar").classList.remove("aberto"); }
      $("#sidebar-toggle").setAttribute("aria-expanded", String(mobile
        ? $("#sidebar").classList.contains("sidebar-open")
        : !$("#app-layout").classList.contains("sidebar-collapsed")));
    });
  }
  function setSplitEnabled(enabled) {
    const pane = $("#split-pane");
    const content = $("#split-content");
    const documentToShow = state.activeDocument || state.splitDocument;
    if (enabled && (!documentToShow || window.matchMedia("(max-width: 720px)").matches)) {
      status(documentToShow ? "A divisão fica disponível acima de 720 px." : "Abra um arquivo antes de ativar o painel dividido.", true);
      return;
    }
    state.splitEnabled = Boolean(enabled);
    $("#workspace-splitter").hidden = !state.splitEnabled;
    pane.hidden = !state.splitEnabled;
    pane.classList.toggle("aberto", state.splitEnabled);
    if (state.splitEnabled && documentToShow) {
      state.splitDocument = documentToShow;
      content.replaceChildren(documentToShow.host);
      redrawListing();
    } else if (!state.splitEnabled && state.activeDocument) {
      clearActiveVisual();
      $("#results").replaceChildren(state.activeDocument.host);
      state.splitDocument = null;
    } else if (!state.splitEnabled) {
      clearActiveVisual();
      content.replaceChildren(); state.splitDocument = null;
    }
    if (state.ui) renderToolbar();
  }
  function redrawListing() {
    if (state.searchResult) renderSearchResults(state.searchResult);
    else renderResults(state.entries);
  }
  function parentOf(path) { return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ""; }
  const pickerCollator = new Intl.Collator("pt-BR", {numeric: true, sensitivity: "base"});
  // Destination picker: .dlg-migalhas breadcrumb, .dlg-lista folder list with .pk-item rows, and
  // the item name when it fits.
  function askDestination({title, submitLabel = "Usar destino", folder = "", name = null, message = "", allowRoot = false, sourcePath = null, fallbackUp = false,
    sources = null, action = null, avoidFolder = null, avoidReason = ""}) {
    const dialog = $("#operation-dialog");
    dialog.classList.add("dlg-largo");
    const form = el("form"); form.method = "dialog"; form.noValidate = true;
    const heading = el("h2", title); heading.id = "operation-dialog-title";
    form.append(heading);
    if (message) form.append(el("p", message));
    const crumbs = el("nav", undefined, "dlg-migalhas"); crumbs.setAttribute("aria-label", "Pasta de destino");
    const list = el("div", undefined, "dlg-lista"); list.setAttribute("role", "group"); list.setAttribute("aria-label", "Pastas");
    form.append(el("div", "Pasta de destino", "dlg-rotulo"), crumbs, list);
    let nameInput = null;
    if (name !== null) {
      const label = el("label", "Nome");
      nameInput = el("input"); nameInput.type = "text"; nameInput.maxLength = 255; nameInput.value = name;
      nameInput.spellcheck = false; nameInput.autocomplete = "off"; label.append(nameInput); form.append(label);
    }
    const preview = el("p", undefined, "name-preview");
    const reason = el("p", "", "dialog-hint dialog-motivo"); reason.setAttribute("aria-live", "polite");
    const menu = el("menu");
    const cancel = button("Cancelar", () => dialog.close(), "secondary-button");
    const submit = el("button", submitLabel, "primary-button"); submit.type = "submit";
    menu.append(cancel, submit); form.append(preview, reason, menu);
    dialog.replaceChildren(form);
    let current = folder; let listing = null; let ticket = 0;
    const target = () => nameInput ? (current ? `${current}/${nameInput.value}` : nameInput.value) : current;
    const update = () => {
      // Line breaks fall between path segments, never in the middle of a name.
      const shown = el("strong");
      absolutePath(target()).split("/").forEach((part, index) => {
        if (index) shown.append("/", document.createElement("wbr"));
        shown.append(part);
      });
      preview.replaceChildren(document.createTextNode("Destino: "), shown);
      let why = ""; let neutral = false;
      if (!listing) { why = "Abrindo a pasta…"; neutral = true; }
      else if (listing.writable === false) why = "Sem permissão para gravar nesta pasta.";
      else if (!nameInput && current === "" && !allowRoot) { why = "Escolha uma pasta."; neutral = true; }
      else if (!nameInput && avoidFolder !== null && current === avoidFolder) why = avoidReason;
      else if (!nameInput && sources) {
        // For multiple items, the reason is shown inside the picker itself before it closes.
        const here = sources.filter(path => parentOf(path) === current).length;
        const clash = sources.map(path => path.split("/").pop()).find(value => listing.entries.some(entry => entry.name === value));
        if (action === "move" && here === sources.length) { why = "Escolha a pasta de destino."; neutral = true; }
        else if (action === "move" && here) why = "Alguns itens já estão nesta pasta.";
        else if (sources.some(path => current === path || current.startsWith(`${path}/`))) why = "Não dá para colocar uma pasta dentro dela mesma.";
        else if (clash) why = `Já existe “${clash}” nesta pasta.`;
      }
      else if (nameInput) {
        const value = nameInput.value;
        if (!value.length) why = "Digite um nome.";
        else if (!validSimpleName(value)) why = "O nome não pode conter “/”, nem ser “.” ou “..”.";
        else if (sourcePath !== null && target() === sourcePath) { why = "Escolha a pasta de destino ou um nome novo."; neutral = true; }
        else if (sourcePath !== null && (current === sourcePath || current.startsWith(`${sourcePath}/`))) why = "Não dá para colocar uma pasta dentro dela mesma.";
        else if (listing.entries.some(entry => entry.name === value)) why = "Já existe um item com esse nome nesta pasta.";
      }
      reason.textContent = why; reason.hidden = !why; reason.classList.toggle("neutro", neutral); submit.disabled = Boolean(why);
    };
    const open = async path => {
      // Keyboard navigation keeps focus on the list: on the folder navigated from, or on the first item.
      const keepFocus = list.contains(document.activeElement) || crumbs.contains(document.activeElement);
      const from = current;
      current = path; listing = null; const mine = ++ticket;
      crumbs.replaceChildren(button("/", () => void open(""), "seg"));
      let walked = "";
      for (const segment of path.split("/").filter(Boolean)) {
        if (walked) crumbs.append(el("span", "/", "sep"));
        walked = walked ? `${walked}/${segment}` : segment; const to = walked;
        crumbs.append(button(segment, () => void open(to), "seg"));
      }
      list.replaceChildren(el("div", "Abrindo…", "pk-vazio")); update();
      try {
        const result = await request("api/list", {}, {rootId: BASE_ID, path});
        if (mine !== ticket) return;
        listing = result;
        const folders = result.entries.filter(entry => entry.type === "directory" && entry.openable !== false && entry.addressable !== false)
          .sort((left, right) => pickerCollator.compare(left.name, right.name));
        list.replaceChildren();
        if (path) {
          const up = button("", () => void open(parentOf(path)), "pk-item pk-acima");
          up.append(el("span", "↰", "pk-seta"), el("span", "Pasta acima")); list.append(up);
        }
        for (const entry of folders) {
          const item = button("", () => void open(path ? `${path}/${entry.name}` : entry.name), "pk-item");
          const icon = iconNode("folder"); icon.classList.add("icone"); item.append(icon, el("span", entry.name)); list.append(item);
        }
        if (!folders.length) list.append(el("div", "Nenhuma subpasta aqui.", "pk-vazio"));
        if (keepFocus) {
          const cameFrom = from.startsWith(path ? `${path}/` : "") && from !== path ? from.slice(path ? path.length + 1 : 0).split("/")[0] : null;
          const items = [...list.querySelectorAll(".pk-item")];
          (items.find(item => cameFrom !== null && item.textContent === cameFrom) || items[0])?.focus();
        }
      } catch (error) {
        if (mine !== ticket) return;
        if (fallbackUp && path && [403, 404].includes(error.status)) { void open(parentOf(path)); return; }
        listing = {entries: [], writable: false};
        list.replaceChildren(el("div", error.status === 403 ? "Sem permissão para abrir esta pasta." : "Não foi possível abrir esta pasta.", "pk-vazio"));
      }
      update();
    };
    if (nameInput) nameInput.addEventListener("input", update);
    return new Promise(resolve => {
      form.addEventListener("submit", event => {
        event.preventDefault(); update();
        if (submit.disabled) return;
        resolve({rootId: BASE_ID, path: target()}); dialog.close();
      });
      onDialogClosed(dialog, () => { dialog.classList.remove("dlg-largo"); resolve(null); });
      // The dialog always opens scrolled to top and resets to top when the first list loads, since the
      // dialog element is reused and would otherwise keep the previous scroll position.
      dialog.showModal(); dialog.scrollTop = 0;
      void open(folder).then(() => { dialog.scrollTop = 0; });
      if (nameInput) { const dot = nameInput.value.lastIndexOf("."); nameInput.focus({preventScroll: true}); nameInput.setSelectionRange(0, dot > 0 ? dot : nameInput.value.length); }
    });
  }
  function destinationDescription(actions) {
    const action = actions.find(value => value.destination);
    return action?.destination || null;
  }
  function replaceDestinations(actions, destination) {
    return actions.map(action => ({...action, destination}));
  }
  function operationFailure(result) {
    const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    if (result.indeterminate?.length) return `Não foi possível confirmar o resultado de ${count(result.indeterminate.length, "item", "itens")}. Confira o destino antes de tentar de novo.`;
    if (result.committed?.length) return `Operação parcial: ${count(result.committed.length, "item concluído", "itens concluídos")} e ${count(result.uncommitted?.length || 0, "item não concluído", "itens não concluídos")}. Atualize a pasta e confira.`;
    const reasons = {conflict: "já existe um item com o mesmo nome no destino", destination_occupied: "já existe um item com o mesmo nome no destino",
      forbidden: "sem permissão", not_found: "o item não está mais lá", limit_exceeded: "passa do limite permitido", invalid: "nome ou destino inválido"};
    // "not_started" marks an item that never began because an earlier one failed; it is not a separate reason.
    const failed = (result.uncommitted || []).filter(item => item.error && item.error !== "not_started");
    const codes = [...new Set(failed.map(item => reasons[item.error] || "falha ao gravar"))];
    const taken = failed.filter(item => ["conflict", "destination_occupied"].includes(item.error))
      .map(item => (item.destination?.path || item.source?.path || "").split("/").pop()).filter(Boolean);
    const names = taken.length ? ` (${[...new Set(taken)].slice(0, 5).map(name => `“${name}”`).join(", ")})` : "";
    return codes.length ? `A operação não foi concluída: ${codes.join("; ")}${names}. Nada foi feito.` : "A operação não foi concluída. Atualize a pasta e confira o resultado.";
  }
  // The server also returns "conflict" for race conditions (the folder changed, an open document
  // is in use); the destination folder is re-checked before reporting that the name already exists.
  async function destinationTaken(actions) {
    if (actions.length !== 1 || actions[0].action === "extract") return true;
    const target = destinationDescription(actions);
    if (!target || typeof target.path !== "string") return true;
    try {
      const listing = await request("api/list", {}, {rootId: target.rootId, path: parentOf(target.path)});
      const name = target.path.split("/").pop();
      return listing.entries.some(entry => entry.name === name);
    } catch (_error) { return true; }
  }
  const BUSY_TEXT = "Não foi possível concluir agora: a pasta mudou ou um documento aberto está em uso. Nada foi feito; tente de novo.";
  function destinationConflict(error) {
    const body = error.body || {};
    return error.status === 409 && (
      ["conflict", "destination_occupied"].includes(body.error) ||
      (body.status === "failed" && !body.committed?.length && !body.indeterminate?.length &&
        (body.uncommitted || []).some(item => ["conflict", "destination_occupied"].includes(item.error)))
    );
  }
  async function performFileActions(actions, description, {allowAlternative = true, allowRoot = false, onConflict = null, onCompleted = null, onNetworkError = null} = {}) {
    let current = actions;
    const resolveAlternative = async () => {
      if (onConflict) return await onConflict(current);
      if (!allowAlternative) return null;
      const previous = destinationDescription(current)?.path || "";
      const folderTarget = current.some(action => action.action === "extract");
      const taken = previous.split("/").pop();
      const alternative = folderTarget
        ? await askDestination({title: "Escolher outra pasta", submitLabel: "Usar esta pasta", folder: previous, allowRoot,
          message: "A pasta de destino já tem itens com os mesmos nomes. Escolha outra pasta.",
          avoidFolder: previous, avoidReason: "Os nomes do ZIP já existem nesta pasta. Escolha outra."})
        : await askDestination({title: "Esse nome já existe", submitLabel: "Usar este destino", folder: parentOf(previous), name: taken,
          message: `Já existe “${taken}” em ${absolutePath(parentOf(previous))}. Escolha outro nome ou outra pasta.`});
      return alternative ? replaceDestinations(current, alternative) : null;
    };
    while (true) {
      try {
        const operation = await request("api/files/token", {
          method: "POST", headers: {"X-CSRF-Token": csrf},
        });
        const result = await request("api/files", {
          method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
          body: JSON.stringify({operationToken: operation.operationToken, actions: current}),
        });
        if (result.status === "completed") {
          toast(description);
          state.selectedForZip.clear();
          await loadDirectory({preserveActive: true});
          void refreshUiFromServer(); void refreshExpandedTreeBranches();
          separatedLinksNotice((result.committed || []).reduce((sum, item) => sum + (item.separatedLinks || 0), 0));
          if (onCompleted) await onCompleted(result);
          return true;
        }
        const conflictOnly = !result.committed?.length && !result.indeterminate?.length &&
          (result.uncommitted || []).some(item => ["conflict", "destination_occupied"].includes(item.error));
        if (conflictOnly) {
          if (!(await destinationTaken(current))) { await loadDirectory({preserveActive: true}); status(BUSY_TEXT, true); return false; }
          const alternative = await resolveAlternative();
          if (alternative) { current = alternative; continue; }
        }
        await loadDirectory({preserveActive: true});
        status(operationFailure(result), true);
        return false;
      } catch (error) {
        if (destinationConflict(error)) {
          if (!(await destinationTaken(current))) { status(BUSY_TEXT, true); return false; }
          const alternative = await resolveAlternative();
          if (alternative) { current = alternative; continue; }
          status("Nada foi feito: já existe um item com esse nome no destino.");
          return false;
        }
        const message = error.status === 403 ? "Não foi possível gravar no destino: sem permissão, ou a pasta não existe mais." :
          error.status === 404 ? "O item de origem ou destino não está mais disponível." :
          error.status === 413 ? "A operação passa do limite permitido (tamanho, quantidade de itens ou compressão)." :
          error.status === 422 && current.some(action => action.action === "extract") ? "Este ZIP não foi extraído: ele tem caminhos inseguros, nomes repetidos ou links. Nada foi gravado." :
          error.status === 422 ? "Nome ou destino não aceito. Nada foi alterado." :
          error.status === 409 ? "A pasta mudou durante a operação. Atualize e tente novamente." :
          !error.status ? "Sem conexão com o servidor. Nada foi feito; confira a rede e tente de novo." :
          "Não foi possível concluir a operação. Confira a pasta antes de repetir.";
        status(message, true);
        if (!error.status && onNetworkError) {
          const again = await onNetworkError(current);
          if (again) { current = again; continue; }
        }
        return false;
      }
    }
  }
  function currentAddress(path = state.path, rootId = state.rootId) { return {rootId, path}; }
  // HF-NAV-003: the listing reports whether the open folder accepts writes; the filesystem remains
  // the final authority once the operation actually runs.
  function canWrite(_rootId = state.rootId) { return state.listingWritable !== false; }
  function canRead(_rootId = state.rootId) { return true; }
  function writableRoots() { return state.roots; }
  const NO_WRITE = "Sem permissão para gravar aqui.";
  function validSimpleName(value) { return typeof value === "string" && value.length > 0 && new TextEncoder().encode(value).length <= 255 && ![".", ".."].includes(value) && !/[\\/\0]/.test(value); }
  function validExtension(value) { return typeof value === "string" && value.length > 0 && value.length <= 32 && !/[\\/.%]/.test(value); }
  function slugifySubject(value) {
    const blocks = value.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().match(/[a-z0-9]+/g) || [];
    const slug = blocks.join("-") || "nota";
    if (slug.length <= 60) return slug;
    const cut = slug.slice(0, 60);
    const hyphen = cut.lastIndexOf("-");
    return hyphen > 0 ? cut.slice(0, hyphen) : cut;
  }
  function previewTimestamp() {
    const date = new Date();
    const pad = value => String(value).padStart(2, "0");
    return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
  }
  function generatedName(subject, extension) { return `${slugifySubject(subject)}-${previewTimestamp()}.${extension}`; }
  function nameChoiceSignature(choice) {
    return choice.mode === "exact" ? JSON.stringify({mode: choice.mode, exactName: choice.exactName}) :
      JSON.stringify({mode: choice.mode, subject: choice.subject, extension: choice.extension});
  }
  function uploadChoiceSignature(choice) {
    return choice.mode === "original" ? JSON.stringify({mode: choice.mode, names: choice.items.map(item => item.originalName)}) :
      JSON.stringify({mode: choice.mode, files: choice.items.map(item => ({subject: item.subject, extension: item.extension}))});
  }
  function canTrash(rootId) { return canWrite(rootId); }
  function affectedDocuments(rootId, path) {
    return [...state.documents.values()].filter(doc => doc.rootId === rootId && (doc.path === path || doc.path.startsWith(`${path}/`)));
  }
  function documentOperationsFor(doc) {
    return [...state.documentOperations].filter(operation => operation.documents.includes(doc));
  }
  function hasDocumentOperation(doc) { return documentOperationsFor(doc).length > 0; }
  function hasDirtyDocuments(rootId, path) { return affectedDocuments(rootId, path).some(dirtyDocument); }
  function discardClosedDocuments(rootId, path) {
    for (const doc of affectedDocuments(rootId, path)) {
      if (dirtyDocument(doc) || doc.saving || doc.saveAgain || documentOperationsFor(doc).some(operation => operation.kind !== "trash-delete")) {
        docStatus(doc, "O item foi removido, mas o documento alterado continua aberto aqui, sem perder o texto.", true);
        continue;
      }
      if (state.activeDocument === doc) state.activeDocument = null;
      doc.destroy(); state.documents.delete(`${doc.rootId}\u0000${doc.path}`);
    }
  }
  // Naming mode is remembered per type (file, note, upload batch): the last choice is always visible
  // and changeable in the dialog. With no prior choice, exact-name mode applies; the generated name
  // is only used if the user selects it (HF-NAME-004).
  function rememberedMode(kind, fallback) {
    try { return localStorage.getItem(`hf-modo-nome-${kind}`) || fallback; } catch (_error) { return fallback; }
  }
  function rememberMode(kind, mode) {
    try { localStorage.setItem(`hf-modo-nome-${kind}`, mode); } catch (_error) { /* local convenience only */ }
  }
  // Segmented control backed by radio inputs (keyboard and screen-reader support).
  function modeChoice(name, label, options, current, onChange, onPick) {
    const group = el("div", undefined, "seg-escolha");
    group.setAttribute("role", "radiogroup"); group.setAttribute("aria-label", label);
    const inputs = [];
    for (const [value, caption] of options) {
      const option = el("label"); const radio = el("input");
      radio.type = "radio"; radio.name = name; radio.value = value; radio.checked = current === value;
      radio.addEventListener("change", () => { if (radio.checked) onChange(value); });
      // With mouse or touch, choosing the mode moves the cursor to the field it uses.
      if (onPick) option.addEventListener("pointerup", () => setTimeout(() => onPick(value), 0));
      option.append(radio, el("span", caption)); group.append(option); inputs.push(radio);
    }
    return {group, inputs};
  }
  // Suggested free-form name in the open folder, like Finder: nova-pasta, nova-pasta-2...
  function uniqueName(stem, extension = "") {
    const taken = new Set(state.entries.map(entry => entry.name));
    const suffix = extension ? `.${extension}` : "";
    for (let index = 1; index < 1000; index++) {
      const candidate = index === 1 ? `${stem}${suffix}` : `${stem}-${index}${suffix}`;
      if (!taken.has(candidate)) return candidate;
    }
    return `${stem}${suffix}`;
  }
  function selectStem(input) {
    const value = input.value; const dot = value.lastIndexOf(".");
    input.focus(); input.setSelectionRange(0, dot > 0 ? dot : value.length);
  }
  function askCreateName(kind, previous = null, requireChange = false) {
    const dialog = $("#operation-dialog");
    dialog.classList.remove("dlg-largo");
    const form = el("form"); form.method = "dialog"; form.noValidate = true;
    const labels = {file: "Novo arquivo", directory: "Nova pasta", note: "Nova nota"};
    const heading = el("h2", requireChange ? "Esse nome já existe" : labels[kind]); heading.id = "operation-dialog-title";
    const defaults = {file: ["novo-arquivo", "txt"], note: ["nova-nota", "md"], directory: ["nova-pasta", ""]}[kind];
    const choice = previous || {
      mode: kind === "directory" ? "exact" : rememberedMode(kind, "exact"),
      exactName: uniqueName(defaults[0], defaults[1]),
      subject: kind === "note" ? "Nova nota" : "Novo arquivo",
      extension: kind === "note" ? "md" : "txt",
    };
    let mode = kind === "directory" || choice.mode !== "generated" ? "exact" : "generated";
    const exactLabel = el("label", kind === "directory" ? "Nome da pasta" : kind === "note" ? "Nome da nota" : "Nome do arquivo, com a extensão");
    const exactInput = el("input"); exactInput.type = "text"; exactInput.maxLength = 255; exactInput.value = choice.exactName || "";
    exactInput.autocomplete = "off"; exactInput.spellcheck = false; exactLabel.append(exactInput);
    const generatedGroup = el("div", undefined, "name-generated-fields");
    const subjectLabel = el("label", "Assunto");
    const subjectInput = el("input"); subjectInput.type = "text"; subjectInput.maxLength = 1024; subjectInput.value = choice.subject || "";
    subjectInput.autocomplete = "off"; subjectLabel.append(subjectInput);
    const extensionLabel = el("label", kind === "note" ? "Extensão (Markdown)" : "Extensão");
    const extensionInput = el("input"); extensionInput.type = "text"; extensionInput.maxLength = 32; extensionInput.value = choice.extension || (kind === "note" ? "md" : "txt");
    extensionInput.autocomplete = "off"; extensionInput.spellcheck = false;
    if (kind === "note") { extensionInput.readOnly = true; extensionInput.setAttribute("aria-label", "Extensão Markdown md"); }
    extensionLabel.append(extensionInput); generatedGroup.append(subjectLabel, extensionLabel);
    form.append(heading);
    if (kind !== "directory") {
      const focusField = value => { if (value === "exact") selectStem(exactInput); else { subjectInput.focus(); subjectInput.select(); } };
      const choiceControl = modeChoice("create-name-mode", "Como nomear", [["exact", "Nome exato"], ["generated", "Gerar por assunto"]], mode,
        value => { mode = value; update(); }, focusField);
      form.append(choiceControl.group);
    }
    const preview = el("p", undefined, "name-preview"); preview.setAttribute("aria-live", "polite");
    const hint = el("p", "O nome leva a data e a hora da criação. A prévia usa o relógio deste aparelho; o servidor usa o da instância.", "dialog-hint");
    const reason = el("p", "", "dialog-hint dialog-motivo"); reason.setAttribute("aria-live", "polite");
    const errorText = el("p", "", "dialog-error"); errorText.setAttribute("role", "alert");
    const menu = el("menu");
    const cancel = button("Cancelar", () => dialog.close(), "secondary-button");
    const submit = el("button", "Criar", "primary-button"); submit.type = "submit";
    menu.append(cancel, submit);
    form.append(exactLabel, generatedGroup, preview, hint, reason, errorText, menu);
    dialog.replaceChildren(form);
    // A note using exact-name mode without a Markdown extension gets ".md" appended (otherwise it would become a plain file).
    const exactFinal = () => kind === "note" && exactInput.value && !/\.(md|markdown)$/i.test(exactInput.value) ? `${exactInput.value}.md` : exactInput.value;
    const currentChoice = () => ({mode, exactName: exactFinal(), subject: subjectInput.value, extension: extensionInput.value.replace(/^\.+/, "")});
    const problem = () => {
      const selected = currentChoice();
      if (selected.mode === "exact") {
        if (!selected.exactName.length) return "Digite um nome.";
        if (new TextEncoder().encode(selected.exactName).length > 255) return "Nome longo demais: o limite é 255 bytes (letras com acento contam 2).";
        if (!validSimpleName(selected.exactName)) return "O nome não pode conter “/”, nem ser “.” ou “..”.";
        if (state.entries.some(entry => entry.name === selected.exactName)) return "Já existe um item com esse nome nesta pasta.";
      } else {
        if (!selected.subject.trim().length) return "Digite um assunto.";
        if (!selected.extension.length) return "Digite uma extensão.";
        if (!validExtension(selected.extension)) return "Extensão inválida: até 32 caracteres, sem ponto, barra ou %.";
      }
      if (requireChange && nameChoiceSignature(selected) === nameChoiceSignature(choice)) return kind === "directory" ? "Escolha outro nome." : "Escolha outro nome ou outro modo.";
      return "";
    };
    const update = () => {
      exactLabel.hidden = mode !== "exact";
      generatedGroup.hidden = mode !== "generated";
      hint.hidden = mode !== "generated";
      const name = mode === "exact" ? exactFinal() : generatedName(subjectInput.value, currentChoice().extension || "ext");
      preview.hidden = kind === "directory";
      preview.replaceChildren(document.createTextNode("Será criado: "), el("strong", name || "—"));
      const why = problem();
      reason.textContent = why; reason.hidden = !why;
      submit.disabled = Boolean(why);
      errorText.textContent = "";
    };
    for (const input of [exactInput, subjectInput, extensionInput]) input.addEventListener("input", update);
    let timer;
    return new Promise(resolve => {
      form.addEventListener("submit", event => {
        event.preventDefault();
        const why = problem();
        if (why) { errorText.textContent = why; (mode === "exact" ? exactInput : subjectInput).focus(); return; }
        const selected = currentChoice();
        if (kind !== "directory") rememberMode(kind, selected.mode);
        resolve({...selected, mode: kind === "directory" ? "exact" : selected.mode}); dialog.close();
      });
      onDialogClosed(dialog, () => { clearInterval(timer); resolve(null); });
      dialog.showModal(); update(); timer = setInterval(() => { if (mode === "generated") update(); }, 1000);
      if (mode === "exact") selectStem(exactInput);
      else { subjectInput.focus(); subjectInput.select(); }
    });
  }
  function actionForCreate(kind, selected) {
    const action = {action: "create", kind, nameMode: selected.mode};
    if (selected.mode === "exact") {
      const name = selected.exactName;
      action.destination = currentAddress(state.path ? `${state.path}/${name}` : name);
      if (kind === "note") action.title = name.replace(/\.md$/i, "");
    } else {
      action.destination = currentAddress();
      action.subject = selected.subject;
      action.extension = selected.extension;
      if (kind === "note") action.title = selected.subject;
    }
    return action;
  }
  async function createItem(kind) {
    if (!canWrite()) { status(NO_WRITE, true); return; }
    const selected = await askCreateName(kind);
    if (!selected) return;
    const action = actionForCreate(kind, selected);
    const description = kind === "directory" ? "Pasta criada." : kind === "note" ? "Nota criada." : "Arquivo criado.";
    await performFileActions([action], description, {
      allowAlternative: false,
      onConflict: async current => {
        const alternative = await askCreateName(kind, selected, true);
        return alternative ? [actionForCreate(kind, alternative)] : null;
      },
      // Without network access, the dialog reopens with what was typed so the user can retry.
      onNetworkError: async () => {
        const again = await askCreateName(kind, selected);
        return again ? [actionForCreate(kind, again)] : null;
      },
      // A newly created note opens directly in Markdown view, ready to type: this is the exception to
      // the rule that .md files open in Formatted view (HF-NAV-008). New folders and files get focus in the list.
      onCompleted: kind === "note" ? result => openNewNote(result.committed?.[0]?.destination)
        : result => { const created = result.committed?.[0]?.destination?.path; if (created) focusListRow(created, {takeFrom: $("#toolbar")}); },
    });
  }
  async function openNewNote(destination) {
    if (!destination || typeof destination.path !== "string") return;
    const rootId = destination.rootId || BASE_ID;
    await openDocument(rootId, destination.path);
    const doc = state.documents.get(`${rootId}\u0000${destination.path}`);
    if (!doc || state.activeDocument !== doc || !doc.isMarkdown || !doc.editable) return;
    doc.mode = "markdown"; updateDocumentView(doc);
    const view = doc.source?.view;
    if (view) requestAnimationFrame(() => { view.dispatch({selection: {anchor: view.state.doc.length}}); view.focus(); });
  }
  async function renameItem(rootId, path) {
    if (!canWrite(rootId)) { status(NO_WRITE, true); return; }
    if (hasDirtyDocuments(rootId, path)) { status("Salve ou feche o documento alterado antes de renomear.", true); return; }
    const nameProblem = value => new TextEncoder().encode(value).length > 255 ? "Nome longo demais: o limite é 255 bytes (letras com acento contam 2)."
      : [".", ".."].includes(value) || /[\\/\0]/.test(value) ? "O nome não pode conter “/”, nem ser “.” ou “..”." : "";
    const name = await askText(`Renomear “${path.split("/").pop()}”`, "Novo nome", path.split("/").pop(), "Renomear", {preserveWhitespace: true, stemOnly: true, validate: nameProblem});
    if (!name || name === path.split("/").pop()) return;
    const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    const renameTo = next => [{action: "rename", source: currentAddress(path, rootId), destination: currentAddress(parent ? `${parent}/${next}` : next, rootId)}];
    let tried = name;
    state.returnFocusPath = null;
    const renamed = await performFileActions(renameTo(name), "Item renomeado.", {
      onConflict: async () => {
        const next = await askText("Esse nome já existe", `Já existe “${tried}” nesta pasta. Escolha outro nome`, tried, "Renomear", {preserveWhitespace: true, stemOnly: true, validate: nameProblem});
        if (!next || next === path.split("/").pop()) return null;
        tried = next; return renameTo(next);
      },
    });
    focusListRow(renamed ? (parent ? `${parent}/${tried}` : tried) : path);
  }
  // Focus before a dialog: returns to the same element or, if the list was redrawn, to the row with the same path.
  function focusReturn() {
    const origin = document.activeElement;
    const path = origin?.closest?.("#results tr.item")?.dataset.path || state.returnFocusPath || null;
    return () => {
      if (document.activeElement && document.activeElement !== document.body) return;
      if (origin?.isConnected && origin !== document.body) origin.focus();
      else if (path) focusListRow(path);
    };
  }
  // After a dialog, focus returns to the row it was on, not to the page body. Focus may move from a
  // different row in the list (a renamed item can move), but never from outside the list. takeFrom:
  // a region focus may also be taken from (the toolbar, after creating an item).
  function focusListRow(path, {takeFrom = null} = {}) {
    const active = document.activeElement;
    if (active && active !== document.body && !$("#results").contains(active) && !takeFrom?.contains(active)) return;
    const row = [...$("#results").querySelectorAll("tr.item")].find(item => item.dataset.path === path);
    if (row) { rovingRow(row); row.focus(); }
  }
  async function moveOrCopyItem(action, rootId, path) {
    if (action === "move" && !canWrite(rootId)) { status(NO_WRITE, true); return; }
    if (action === "move" && hasDirtyDocuments(rootId, path)) { status("Salve ou feche o documento alterado antes de mover.", true); return; }
    const name = path.split("/").pop();
    const dot = name.lastIndexOf(".");
    const siblings = new Set((state.treeEntries.get(treeBranchKey(rootId, parentOf(path))) || []).map(entry => entry.name));
    const copyAs = count => { const tag = count > 1 ? ` (cópia ${count})` : " (cópia)"; return dot > 0 ? `${name.slice(0, dot)}${tag}${name.slice(dot)}` : `${name}${tag}`; };
    let copies = 1; while (siblings.has(copyAs(copies)) && copies < 1000) copies += 1;
    const copyName = copyAs(copies);
    const target = await askDestination({
      title: action === "move" ? `Mover “${name}”` : `Copiar “${name}”`, submitLabel: action === "move" ? "Mover aqui" : "Copiar aqui",
      folder: parentOf(path), name: action === "move" ? name : copyName, sourcePath: path,
    });
    if (!target) return;
    await performFileActions([{action, source: currentAddress(path, rootId), destination: target}], action === "move" ? "Item movido." : "Cópia concluída.");
  }
  // Items checked in the list (checkboxes), for trash and ZIP export.
  function selectionTargets() {
    return [...state.selectedForZip].map(value => { const [rootId, path] = value.split("\u0000"); return {rootId, path}; });
  }
  function listCheckboxes() { return [...$("#results").querySelectorAll("tbody tr.item input.check:not(:disabled)")]; }
  // Bulk selection updates the toolbar and footer only once, at the end.
  function batchSelect(change) {
    state.batchSelect = true;
    try { change(); } finally { state.batchSelect = false; }
    syncSelectionButtons(); selectionStatus();
  }
  function setAllSelected(selected) {
    batchSelect(() => { for (const box of listCheckboxes()) if (box.checked !== selected) box.click(); });
  }
  function selectRange(from, to) {
    const rows = [...$("#results").querySelectorAll("tbody tr.item")];
    const a = rows.findIndex(row => row.dataset.path === from); const b = rows.findIndex(row => row.dataset.path === to);
    if (a < 0 || b < 0) return;
    batchSelect(() => {
      for (const row of rows.slice(Math.min(a, b), Math.max(a, b) + 1)) {
        const box = row.querySelector("input.check:not(:disabled)");
        if (box && !box.checked) box.click();
      }
    });
  }
  function selectionStatus() {
    const marked = state.selectedForZip.size;
    const where = window.matchMedia("(pointer: coarse), (max-width: 720px)").matches ? "os botões da barra (⋯ traz Renomear, Mover, Copiar e outras)" : "a barra ou o botão direito";
    status(marked ? `${formatCount(marked)} ${marked === 1 ? "item marcado" : "itens marcados"}. Use ${where} para agir sobre ${marked === 1 ? "ele" : "eles"}.` : "Nenhum item marcado.");
  }
  function syncSelectionButtons() {
    const count = state.selectedForZip.size;
    const all = $("#results thead input.check");
    if (all) {
      const boxes = listCheckboxes(); const marked = boxes.filter(box => box.checked).length;
      all.checked = boxes.length > 0 && marked === boxes.length; all.indeterminate = marked > 0 && marked < boxes.length;
    }
    const zip = $("#zip-create"); if (zip) zip.disabled = count === 0;
    $("#toolbar-actions")?.classList.toggle("com-selecao", count > 0);
    refreshInfoPanel();
    const trash = $("#trash-selection");
    if (trash) {
      trash.disabled = count === 0;
      const label = count > 1 ? `Mover ${count} itens para a lixeira` : "Mover para a lixeira";
      trash.setAttribute("aria-label", label); trash.dataset.tip = label;
    }
  }
  async function moveOrCopySelection(action) {
    const targets = selectionTargets();
    if (!targets.length) return;
    if (action === "move" && targets.some(item => hasDirtyDocuments(item.rootId, item.path))) { status("Salve ou feche o documento alterado antes de mover.", true); return; }
    const folder = await askDestination({
      title: `${action === "move" ? "Mover" : "Copiar"} ${targets.length} itens`,
      submitLabel: action === "move" ? "Mover para cá" : "Copiar para cá", folder: parentOf(targets[0].path), allowRoot: true,
      sources: targets.map(item => item.path), action,
    });
    if (!folder) return;
    if (action === "move" && targets.some(item => parentOf(item.path) === folder.path)) { status("Os itens já estão nesta pasta. Escolha outra.", true); return; }
    const actions = targets.map(item => {
      const name = item.path.split("/").pop();
      return {action, source: currentAddress(item.path, item.rootId), destination: {rootId: folder.rootId, path: folder.path ? `${folder.path}/${name}` : name}};
    });
    // Multiple items have no single alternative name: a conflict is reported without changing anything.
    await performFileActions(actions, action === "move" ? `${targets.length} itens movidos.` : `${targets.length} itens copiados.`, {allowAlternative: false});
  }
  // Dragging: list items onto folders (list, tree), Favorites, and Labels; files from the computer
  // onto the list or a folder. Option/Alt while dropping copies instead of moving.
  function draggedPaths(path) {
    const key = `${state.rootId}\u0000${path}`;
    return state.selectedForZip.has(key) && state.selectedForZip.size > 1 ? selectionTargets().map(item => item.path) : [path];
  }
  function clearDropTargets() { for (const node of app.querySelectorAll(".drop-alvo-item")) node.classList.remove("drop-alvo-item"); }
  function makeDraggable(row, path) {
    row.draggable = true; row.dataset.drag = "1";
    row.addEventListener("dragstart", event => {
      const paths = draggedPaths(path);
      state.dragPaths = paths;
      event.dataTransfer.setData("text/plain", paths.map(absolutePath).join("\n"));
      event.dataTransfer.effectAllowed = "all";
      document.body.classList.add("arrastando-itens"); row.classList.add("arrastando");
    });
    row.addEventListener("dragend", () => {
      state.dragPaths = null; document.body.classList.remove("arrastando-itens"); row.classList.remove("arrastando"); clearDropTargets();
    });
  }
  function makeFolderDropTarget(node, folder) {
    const accepts = event => {
      if (state.dragPaths) return state.dragPaths.every(path => path !== folder && !folder.startsWith(`${path}/`) && parentOf(path) !== folder);
      return [...(event.dataTransfer?.types || [])].includes("Files");
    };
    const veilText = text => { const box = $("#drop-alvo .drop-caixa"); if (box) box.textContent = text; };
    node.addEventListener("dragover", event => {
      if (!accepts(event)) return;
      event.preventDefault(); event.stopPropagation();
      // Dragging from a read-only folder copies instead of moving, since moving would require writing to the source.
      event.dataTransfer.dropEffect = state.dragPaths && !event.altKey && state.listingWritable !== false ? "move" : "copy";
      node.classList.add("drop-alvo-item");
      if (!state.dragPaths) veilText(`Soltar em “${folder.split("/").pop() || "/"}”`);
    });
    node.addEventListener("dragleave", event => {
      if (node.contains(event.relatedTarget)) return;
      node.classList.remove("drop-alvo-item"); veilText("Soltar para adicionar");
    });
    node.addEventListener("drop", event => {
      node.classList.remove("drop-alvo-item"); veilText("Soltar para adicionar");
      if (!accepts(event)) return;
      event.preventDefault(); event.stopPropagation();
      $("#drop-alvo")?.classList.remove("ativo");
      if (state.dragPaths) { const paths = state.dragPaths; state.dragPaths = null; void dropItems(paths, folder, event.altKey || state.listingWritable === false ? "copy" : "move"); return; }
      const files = [...(event.dataTransfer?.files || [])];
      if (files.length) void uploadFiles(files, {rootId: BASE_ID, path: folder});
    });
  }
  async function dropItems(paths, folder, action) {
    if (action === "move" && paths.some(path => hasDirtyDocuments(state.rootId, path))) { status("Salve ou feche o documento alterado antes de mover.", true); return; }
    const actions = paths.map(path => {
      const name = path.split("/").pop();
      return {action, source: currentAddress(path), destination: currentAddress(folder ? `${folder}/${name}` : name)};
    });
    const verb = action === "move" ? "movido" : "copiado";
    const where = folder.split("/").pop() || "/";
    const done = paths.length === 1 ? `Item ${verb} para “${where}”` : `${formatCount(paths.length)} itens ${verb}s para “${where}”`;
    await performFileActions(actions, done, {allowAlternative: paths.length === 1});
    await refreshExpandedTreeBranches();
  }
  function markDropTarget(node, apply) {
    node.addEventListener("dragover", event => { if (!state.dragPaths) return; event.preventDefault(); event.dataTransfer.dropEffect = "link"; node.classList.add("drop-alvo-item"); });
    node.addEventListener("dragleave", event => { if (!node.contains(event.relatedTarget)) node.classList.remove("drop-alvo-item"); });
    node.addEventListener("drop", event => {
      node.classList.remove("drop-alvo-item");
      if (!state.dragPaths) return;
      event.preventDefault(); const paths = state.dragPaths; state.dragPaths = null;
      for (const path of paths) apply(itemState(state.rootId, path, true));
      changed(); refreshItems();
    });
  }
  async function trashSelection() {
    const targets = selectionTargets();
    if (!targets.length) { status("Marque na lista os itens que vão para a lixeira.", true); return; }
    await deleteItems(targets);
  }
  async function deleteItem(rootId, path) { await deleteItems([{rootId, path}]); }
  async function deleteItems(items) {
    // Conditions are checked before and again after confirmation: the dialog wait is asynchronous,
    // and a document may have changed or started saving in the meantime.
    const blocked = () => {
      if (!items.every(item => canTrash(item.rootId))) { status(NO_WRITE, true); return true; }
      if (items.some(item => hasDirtyDocuments(item.rootId, item.path))) { status("Salve ou feche o documento alterado antes de mover para a lixeira.", true); return true; }
      if (items.some(item => affectedDocuments(item.rootId, item.path).some(doc => doc.saving || hasDocumentOperation(doc)))) {
        status("Aguarde as operações dos documentos afetados antes de mover para a lixeira.", true); return true;
      }
      return false;
    };
    if (blocked()) return;
    const names = items.map(item => item.path.split("/").at(-1) || item.path);
    const title = items.length === 1 ? "Mover para a lixeira?" : `Mover ${items.length} itens para a lixeira?`;
    const listed = names.slice(0, 8).join(", ") + (names.length > 8 ? ` e mais ${names.length - 8}` : "");
    const message = items.length === 1 ? `${names[0]} vai para a lixeira.` : `${listed} vão para a lixeira.`;
    if (!await askConfirm(title, message, "Mover para a lixeira", {danger: true})) return;
    if (blocked()) return;
    const documents = items.flatMap(item => affectedDocuments(item.rootId, item.path));
    const operation = trackDocumentOperation("trash-delete", async () => {
      let moved = 0; let separated = 0; const failed = [];
      // Batch operations: progress shown in the toolbar, with selection buttons locked until it finishes.
      const buttons = ["#trash-selection", "#zip-create"].map(selector => $(selector)).filter(Boolean);
      if (items.length > 1) buttons.forEach(node => { node.disabled = true; });
      for (const [index, item] of items.entries()) {
        if (items.length > 1) status(`Movendo para a lixeira: ${formatCount(index + 1)} de ${formatCount(items.length)}…`);
        try {
          const deleted = await request("api/trash", {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify({action: "delete", source: currentAddress(item.path, item.rootId)})});
          discardClosedDocuments(item.rootId, item.path);
          state.selectedForZip.delete(`${item.rootId}\u0000${item.path}`);
          const row = [...$("#results").querySelectorAll("tr.item")].find(node => node.dataset.path === item.path);
          row?.classList.add("saindo");
          moved += 1; separated += deleted?.separatedLinks || 0;
        } catch (error) { failed.push({item, error}); }
      }
      if (moved) toast(moved === 1 ? "Movido para a lixeira" : `${moved} itens movidos para a lixeira`);
      await loadDirectory();
      if (moved) { void refreshUiFromServer(); void refreshExpandedTreeBranches(); }
      if (failed.length === 1 && items.length === 1) {
        status(failed[0].error.status === 409 ? "O item mudou durante a exclusão. Atualize e tente novamente." : "Não foi possível mover o item para a lixeira.", true);
      } else if (failed.length) {
        status(`${failed.length} de ${items.length} itens não foram para a lixeira: ${failed.map(entry => entry.item.path.split("/").at(-1)).join(", ")}.`, true);
      }
      separatedLinksNotice(separated);
    }, documents);
    if (operation) await operation;
    else status("Aguarde as operações dos documentos afetados antes de excluir.", true);
  }
  async function showDirectorySize(rootId, path) {
    try {
      status("Calculando o tamanho da pasta…");
      const result = await request("api/dir-size", {}, {rootId, path});
      const total = `${formatSize(result.totalBytes)} em ${folderCounts(result)}`;
      status(result.complete ? `${total}.` : `Pelo menos ${total}: parte da pasta não pôde ser medida (tempo, permissão ou sistema virtual).`);
      const key = `${rootId}\u0000${path}`;
      if (state.dirSizes.has(key)) { state.dirSizes.set(key, result); const cell = state.sizeCells.get(key); if (cell?.isConnected) showFolderSize(cell, result); }
    } catch (error) {
      status(error.status === 403 ? "Sem permissão para ler esta pasta." : "Não foi possível calcular o tamanho da pasta.", true);
    }
  }
  async function createZip() {
    if (!canWrite()) { status(NO_WRITE, true); return; }
    const sources = [...state.selectedForZip].map(value => {
      const [rootId, path] = value.split("\u0000"); return {rootId, path};
    });
    if (!sources.length) { status("Marque pelo menos um item para criar um ZIP.", true); return; }
    let name = await askText("Criar arquivo ZIP", "Nome do ZIP (fica nesta pasta)", "arquivos.zip", "Criar ZIP", {stemOnly: true});
    if (!name) return;
    if ([".", ".."].includes(name) || /[\\/\0]/.test(name)) { status("Use um nome simples, sem separadores de caminho.", true); return; }
    if (!/\.zip$/i.test(name)) name += ".zip";
    const path = state.path ? `${state.path}/${name}` : name;
    await performFileActions([{action: "zip", sources, destination: currentAddress(path)}], "Arquivo ZIP criado.", {
      onConflict: async current => {
        const previous = destinationDescription(current)?.path || path;
        const taken = previous.split("/").pop();
        const chosen = await askDestination({title: "Esse nome já existe", submitLabel: "Usar este destino", folder: parentOf(previous), name: taken,
          message: `Já existe “${taken}” em ${absolutePath(parentOf(previous))}. Escolha outro nome ou outra pasta.`});
        if (!chosen) return null;
        return replaceDestinations(current, {rootId: chosen.rootId, path: /\.zip$/i.test(chosen.path) ? chosen.path : `${chosen.path}.zip`});
      },
    });
  }
  async function extractZip(rootId, path) {
    if (!canWrite(rootId)) { status(NO_WRITE, true); return; }
    const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    await performFileActions([{action: "extract", source: currentAddress(path, rootId), destination: currentAddress(parent, rootId)}], "Conteúdo do ZIP extraído.", {allowRoot: true});
  }
  function askUploadNames(files, previous = null, requireChange = false, existing = null) {
    const dialog = $("#operation-dialog");
    const form = el("form"); form.method = "dialog";
    const heading = el("h2", requireChange ? "Resolver conflito de nomes" : "Nomes dos arquivos adicionados"); heading.id = "operation-dialog-title";
    dialog.classList.add("dlg-largo");
    const defaultItems = files.map(file => {
      const dot = file.name.lastIndexOf(".");
      const hasExtension = dot > 0 && dot < file.name.length - 1;
      return {originalName: file.name, subject: hasExtension ? file.name.slice(0, dot) : file.name, extension: hasExtension ? file.name.slice(dot + 1) : "txt"};
    });
    const choice = previous || {mode: rememberedMode("upload", "original"), items: defaultItems};
    let mode = choice.mode === "generated" ? "generated" : "original";
    const groups = [];
    const modes = modeChoice("upload-name-mode", "Nomes de todo o lote", [["original", "Manter nomes originais"], ["generated", "Gerar por assunto"]], mode,
      value => { mode = value; update(); });
    const modeInputs = modes.inputs;
    form.append(heading, modes.group);
    const hint = el("p", "O nome gerado leva a data e a hora do envio.", "dialog-hint");
    if (requireChange) {
      const taken = files.map(file => file.name).filter(name => existing ? existing.names.has(name) : state.entries.some(entry => entry.name === name));
      const place = existing ? `em “${existing.label}”` : "nesta pasta";
      form.append(el("p", taken.length ? `Já existem ${place}: ${taken.join(", ")}. Mude os nomes, gere nomes por assunto ou escolha outra pasta.` : "Alguns nomes já existem no destino. Mude os nomes, gere nomes por assunto ou escolha outra pasta.", "dialog-hint dialog-motivo"));
    }
    const errorText = el("p", "", "dialog-error"); errorText.setAttribute("role", "alert");
    files.forEach((file, index) => {
      const itemChoice = choice.items[index] || defaultItems[index];
      const group = el("fieldset", undefined, "upload-name-item");
      group.append(el("legend", `Arquivo ${index + 1}: ${file.name}`));
      const originalLabel = el("label", "Nome original ou alternativo");
      const originalInput = el("input"); originalInput.type = "text"; originalInput.maxLength = 255; originalInput.value = itemChoice.originalName; originalLabel.append(originalInput);
      const originalPreview = el("output", undefined, "name-preview"); originalPreview.setAttribute("aria-live", "polite");
      const generated = el("div", undefined, "name-generated-fields");
      const subjectLabel = el("label", "Assunto");
      const subjectInput = el("input"); subjectInput.type = "text"; subjectInput.maxLength = 1024; subjectInput.value = itemChoice.subject; subjectLabel.append(subjectInput);
      const extensionLabel = el("label", "Extensão");
      const extensionInput = el("input"); extensionInput.type = "text"; extensionInput.maxLength = 32; extensionInput.value = itemChoice.extension; extensionLabel.append(extensionInput);
      const preview = el("output", undefined, "name-preview"); preview.setAttribute("aria-live", "polite");
      generated.append(subjectLabel, extensionLabel, preview);
      group.append(originalLabel, originalPreview, generated); form.append(group);
      groups.push({file, originalLabel, originalInput, originalPreview, generated, subjectInput, extensionInput, preview});
    });
    const menu = el("menu");
    const cancel = button("Cancelar", () => dialog.close(), "secondary-button");
    let selectOtherFolder = () => {};
    const folder = requireChange ? button("Escolher outra pasta", () => selectOtherFolder(), "secondary-button") : null;
    const submit = el("button", requireChange ? "Usar nomes alternativos" : "Continuar", "primary-button"); submit.type = "submit";
    menu.append(cancel); if (folder) menu.append(folder); menu.append(submit);
    form.append(hint, errorText, menu);
    dialog.replaceChildren(form);
    const currentChoice = () => ({mode, items: groups.map(({originalInput, subjectInput, extensionInput}) => ({originalName: originalInput.value, subject: subjectInput.value, extension: extensionInput.value}))});
    const update = () => {
      for (const group of groups) {
        group.originalLabel.hidden = mode === "generated";
        group.originalPreview.hidden = mode === "generated";
        group.originalPreview.textContent = `Prévia: ${group.originalInput.value}`;
        group.generated.hidden = mode === "original";
        group.originalInput.required = mode === "original";
        group.subjectInput.required = mode === "generated";
        group.extensionInput.required = mode === "generated";
        group.preview.textContent = `Prévia: ${generatedName(group.subjectInput.value, group.extensionInput.value || "ext")}`;
      }
      hint.hidden = mode !== "generated";
      const selected = currentChoice();
      submit.disabled = !["original", "generated"].includes(mode) ||
        (requireChange && uploadChoiceSignature(selected) === uploadChoiceSignature(choice));
    };
    // The error clears only when the fields are edited, not automatically.
    for (const group of groups) for (const input of [group.originalInput, group.subjectInput, group.extensionInput]) input.addEventListener("input", () => { errorText.textContent = ""; update(); });
    let timer;
    return new Promise(resolve => {
      form.addEventListener("submit", event => {
        event.preventDefault();
        const selected = currentChoice();
        if (!["original", "generated"].includes(selected.mode)) {
          errorText.textContent = "Escolha um modo para todo o lote antes de continuar.";
          modeInputs[0]?.focus(); return;
        }
        const valid = selected.items.every(item => mode === "original" ? validSimpleName(item.originalName) :
          item.subject.length > 0 && item.subject.length <= 1024 && validExtension(item.extension));
        if (!valid) { errorText.textContent = "Confira os nomes: cada nome original deve ser simples e cada nome gerado exige assunto e extensão válidos."; return; }
        if (requireChange && uploadChoiceSignature(selected) === uploadChoiceSignature(choice)) return;
        rememberMode("upload", selected.mode);
        resolve(selected); dialog.close();
      });
      // Only one response is allowed when the dialog closes: "Escolher outra pasta" records the choice before it closes.
      let answer = null;
      selectOtherFolder = () => { answer = {chooseFolder: true}; dialog.close(); };
      onDialogClosed(dialog, () => { clearInterval(timer); resolve(answer); });
      dialog.showModal(); update(); timer = setInterval(update, 1000);
      if (mode === "original") groups[0]?.originalInput.focus();
      else if (mode === "generated") groups[0]?.subjectInput.focus();
      else modeInputs[0]?.focus();
    });
  }
  async function uploadFiles(files, destination = null) {
    if (!destination && !canWrite()) { status(NO_WRITE, true); return; }
    if (!files.length) return;
    const total = files.reduce((sum, file) => sum + file.size, 0);
    const tooBig = files.find(file => file.size > 200 * 1024 * 1024);
    if (files.length > 20) { status(`Dá para adicionar até 20 arquivos por vez; foram escolhidos ${files.length}.`, true); return; }
    if (tooBig) { status(`“${tooBig.name}” passa do limite de 200 MB por arquivo.`, true); return; }
    if (total > 400 * 1024 * 1024) { status(`O lote tem ${formatSize(total)}; o limite é 400 MB por vez.`, true); return; }
    let selection = await askUploadNames(files);
    if (!selection) return;
    // Names already taken at the real destination (which may be a subfolder where the files were dropped).
    const destinationNames = async target => {
      try {
        const listing = await request("api/list", {}, {rootId: target.rootId, path: target.path});
        return {names: new Set(listing.entries.map(entry => entry.name)), label: target.path.split("/").pop() || "/"};
      } catch (_error) { return null; }
    };
    try {
      const digestHex = async file => [...new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer()))].map(value => value.toString(16).padStart(2, "0")).join("");
      const digests = await Promise.all(files.map(digestHex));
      const manifest = {action: "upload", destination: destination || currentAddress(), nameMode: selection.mode, files: []};
      const updateManifest = () => {
        manifest.nameMode = selection.mode;
        manifest.files = files.map((file, index) => selection.mode === "original" ?
          {originalName: selection.items[index].originalName, contentDigest: digests[index], contentSize: file.size} :
          {subject: selection.items[index].subject, extension: selection.items[index].extension, contentDigest: digests[index], contentSize: file.size});
      };
      updateManifest();
      const encodeManifest = () => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(manifest)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
      const body = new Blob(files, {type: "application/octet-stream"});
      while (true) {
        try {
          const operation = await request("api/files/token", {method: "POST", headers: {"X-CSRF-Token": csrf}});
          const result = await request("api/files/upload", {
            method: "POST", body,
            headers: {"X-CSRF-Token": csrf, "X-Hopper-Operation-Token": operation.operationToken, "X-Hopper-Upload-Manifest": encodeManifest(), "Content-Type": "application/octet-stream"},
          });
          if (result.status === "completed") { toast(files.length === 1 ? "Arquivo adicionado" : `${files.length} arquivos adicionados`); await loadDirectory({preserveActive: true}); break; }
          const conflictOnly = !result.committed?.length && !result.indeterminate?.length && (result.uncommitted || []).some(item => ["conflict", "destination_occupied"].includes(item.error));
          if (conflictOnly) {
            const alternative = await askUploadNames(files, selection, true, await destinationNames(manifest.destination));
            if (!alternative) break;
            if (alternative.chooseFolder) {
              const destination = await askDestination({title: "Escolher pasta de destino", submitLabel: "Usar esta pasta", folder: manifest.destination.path, allowRoot: true});
              if (!destination) break;
              manifest.destination = destination;
            } else selection = alternative;
            updateManifest(); continue;
          }
          status(operationFailure(result), true); break;
        } catch (error) {
          if (destinationConflict(error)) {
            const alternative = await askUploadNames(files, selection, true, await destinationNames(manifest.destination));
            if (!alternative) break;
            if (alternative.chooseFolder) {
              const destination = await askDestination({title: "Escolher pasta de destino", submitLabel: "Usar esta pasta", folder: manifest.destination.path, allowRoot: true});
              if (!destination) break;
              manifest.destination = destination;
            } else selection = alternative;
            updateManifest(); continue;
          }
          throw error;
        }
      }
    } catch (error) {
      status(error.status === 409 ? "O destino mudou ou está ocupado; nenhum arquivo existente foi substituído." : error.status === 413 ? "O envio excede o limite permitido." : "Não foi possível enviar os arquivos.", true);
    }
  }
  function rootFor(id) { return state.roots.find((root) => root.rootId === id); }
  function rootLabel(_id) { return "/"; }
  function absolutePath(path) { return `/${path}`; }
  function relativePath(absolute) { return absolute === "/" ? "" : absolute.slice(1); }
  // ui-state version 2 (HF-META-004) stores absolute paths; internally the UI uses the {rootId, path}
  // transport pair. Orphaned entries from the migration come from the server and are passed through unchanged.
  function fromWireUi(document) {
    const value = clone(document);
    value.items = value.items.map(({path, ...item}) => ({rootId: BASE_ID, path: relativePath(path), ...item}));
    value.tabs = value.tabs.map(({path, mode}) => ({rootId: BASE_ID, path: relativePath(path), mode}));
    return value;
  }
  function toWireUi(document) {
    const value = clone(document);
    value.items = value.items.map(({rootId: _root, path, labelIds, favorite, emoji, inode, device}) =>
      ({path: absolutePath(path), labelIds, favorite, emoji, inode, device}));
    value.tabs = value.tabs.map(({path, mode}) => ({path: absolutePath(path), mode}));
    return value;
  }
  async function fetchUi() { return fromWireUi(await request("api/state")); }
  function itemState(rootId, path, create = false) {
    let item = state.ui.items.find((value) => value.rootId === rootId && value.path === path);
    if (!item && create) {
      item = { rootId, path, labelIds: [], favorite: false, emoji: null, inode: null, device: null };
      state.ui.items.push(item);
    }
    return item;
  }
  function removeEmptyItem(item) {
    if (item && !item.favorite && item.labelIds.length === 0 && item.emoji === null) {
      state.ui.items = state.ui.items.filter((value) => value !== item);
    }
  }
  function changed() {
    renderCollections();
    scheduleSave();
  }
  function refreshItems() {
    if (state.view === "files") {
      if (state.activeDocument && !state.splitEnabled) return;
      if (state.searchResult) renderSearchResults(state.searchResult);
      else renderResults(state.entries);
    } else if (state.view === "favorites" || state.view === "labels") {
      renderCollectionItems();
    } else if (state.view === "tags" && state.selectedTag) {
      selectTag(state.selectedTag);
    }
  }

  async function initialize() {
    try {
      const ui = await fetchUi();
      state.ui = ui;
      state.baseUi = clone(ui);
      state.rootId = BASE_ID;
      const resumeIndex = savedActiveTab(ui.tabs);
      const resume = ui.tabs[resumeIndex];
      if (resume) { state.rootId = resume.rootId; state.path = resume.path; setActiveTab(resumeIndex); }
      // With no saved tab (all were closed), opening the app does not recreate one; the next navigation creates it.
      else state.tablessKey = tabKey(state.rootId, state.path);
      loadListPrefs();
      applyPreferences();
      setupLayoutControls();
      installLongPress();
      const trail = $(".trilha-barra");
      if (trail && window.ResizeObserver) new ResizeObserver(() => { trail.scrollLeft = trail.scrollWidth; trail.classList.toggle("cortada", trail.scrollLeft > 0); }).observe(trail);
      trail?.addEventListener("scroll", () => trail.classList.toggle("cortada", trail.scrollLeft > 0), {passive: true});
      // After a reload, the current history entry stays the same: position comes from it.
      state.historySeq = history.state?.hf ? (history.state.seq ?? 0) : 0;
      window.addEventListener("online", () => { if (state.saveFailures) scheduleSave(); });
      $("#operation-dialog").addEventListener("close", () => {
        const path = state.returnFocusPath; state.returnFocusPath = null;
        if (path && !$("#operation-dialog").open) focusListRow(path);
      });
      window.addEventListener("popstate", event => {
        const entry = event.state;
        if (!entry?.hf) return;
        const forward = (entry.seq ?? 0) > state.historySeq;
        state.historySeq = entry.seq ?? 0;
        // Navigation requested by history arrives later (async load) and must not push a new entry,
        // or the Forward action would be lost.
        const view = entry.view || "files";
        const doc = entry.doc ? state.documents.get(entry.doc) : null;
        state.historyTarget = historyEntry({...entry, doc: doc ? entry.doc : null});
        if (view !== "files") { enterView(view, {tag: entry.tag, label: entry.label}); return; }
        if (doc) { showDocument(doc); return; }
        // The entry happened in another tab that is still on that folder: switch back to it instead of
        // changing the current tab's folder.
        const tab = state.ui.tabs[entry.tab];
        if (Number.isInteger(entry.tab) && entry.tab !== activeTabIndex() && tab && tab.rootId === BASE_ID && tab.path === entry.path) {
          state.restoreListScroll = true; openFolderTab(tab, {index: entry.tab}); return;
        }
        if (entry.path !== state.path || state.activeDocument || state.view !== "files") { state.restoreListScroll = true; navigateFolder(entry.path); return; }
        // Nothing would change on screen (this entry's document was closed): continue in the same direction.
        state.historyTarget = null;
        if (forward) history.forward(); else if (state.historySeq > 0) history.back();
      });
      // Each folder remembers its own list scroll position: Back, switching tabs, and closing a document all return to it.
      $("#results").addEventListener("scroll", () => {
        if (!state.listingRootId || state.view !== "files" || state.searchResult || !$("#results").querySelector(":scope > table.lista")) return;
        const key = tabKey(state.listingRootId, state.listingPath);
        state.listScroll.set(key, {...state.listScroll.get(key), top: $("#results").scrollTop});
      }, {passive: true});
      document.addEventListener("keydown", event => {
        if (!(event.metaKey || event.ctrlKey) || event.altKey || event.key.toLowerCase() !== "a" || event.defaultPrevented) return;
        if (event.target.closest?.("input, textarea, select, [contenteditable=true], .cm-editor, dialog, #split-content, .menu-ctx")) return;
        if (state.view !== "files" || state.searchResult || (state.activeDocument && !state.splitEnabled) || !$("#results table.lista")) return;
        event.preventDefault(); setAllSelected(true);
      });
      document.addEventListener("keydown", event => {
        if (!(event.metaKey || event.ctrlKey) || event.altKey || event.key.toLowerCase() !== "s") return;
        // Editors already save on the shortcut and mark the event handled; this covers only the other
        // views, otherwise the same Cmd/Ctrl+S would save twice.
        const handled = event.defaultPrevented;
        event.preventDefault();
        if (handled || event.target.closest?.(".cm-editor")) return;
        const doc = state.activeDocument || state.splitDocument;
        if (doc?.editable && dirtyDocument(doc)) void saveDocument(doc);
      });
      let fileDepth = 0;
      const hasFiles = event => [...(event.dataTransfer?.types || [])].includes("Files") && !state.dragPaths;
      const veil = () => $("#drop-alvo");
      const docShown = () => Boolean(state.activeDocument && !state.splitEnabled);
      $("#results").addEventListener("dragenter", event => { if (!hasFiles(event) || state.view !== "files" || !canWrite() || docShown()) return; fileDepth += 1; veil()?.classList.add("ativo"); });
      $("#results").addEventListener("dragleave", event => { if (!hasFiles(event)) return; fileDepth = Math.max(0, fileDepth - 1); if (!fileDepth) veil()?.classList.remove("ativo"); });
      $("#results").addEventListener("dragover", event => { if (hasFiles(event) && state.view === "files" && canWrite() && !docShown()) { event.preventDefault(); event.dataTransfer.dropEffect = "copy"; } });
      $("#results").addEventListener("drop", event => {
        fileDepth = 0; veil()?.classList.remove("ativo");
        if (!hasFiles(event) || state.view !== "files" || docShown()) return;
        event.preventDefault();
        const files = [...(event.dataTransfer?.files || [])];
        if (files.length) void uploadFiles(files);
      });
      // Dropping a file outside the drop zones must not open it in place of the app.
      window.addEventListener("dragover", event => { if (hasFiles(event)) event.preventDefault(); });
      window.addEventListener("drop", event => { if (hasFiles(event)) { event.preventDefault(); veil()?.classList.remove("ativo"); fileDepth = 0; } });
      $("#results").addEventListener("keydown", event => {
        if (state.view !== "files" || docShown() || event.target.closest("input:not([type=checkbox]), textarea, [contenteditable=true], .cm-editor")) return;
        const row = event.target.closest("tr.item");
        const rows = [...$("#results").querySelectorAll("tr.item")];
        const moves = {ArrowDown: 1, ArrowUp: -1};
        if (row && (event.key in moves || event.key === "Home" || event.key === "End")) {
          event.preventDefault();
          const index = rows.indexOf(row);
          const next = event.key === "Home" ? rows[0] : event.key === "End" ? rows.at(-1) : rows[Math.min(rows.length - 1, Math.max(0, index + moves[event.key]))];
          next?.focus(); next?.scrollIntoView({block: "nearest"});
          return;
        }
        if (event.key === "F2" && row?.dataset.operable && canWrite()) { event.preventDefault(); void renameItem(state.rootId, row.dataset.path); return; }
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a") { event.preventDefault(); setAllSelected(true); return; }
        if (event.key === "Escape" && state.selectedForZip.size) { event.preventDefault(); setAllSelected(false); return; }
        const trashKey = event.key === "Delete" || (event.key === "Backspace" && (event.metaKey || event.ctrlKey));
        if (!trashKey) return;
        if (state.selectedForZip.size) { event.preventDefault(); void trashSelection(); }
        else if (row?.dataset.operable) { event.preventDefault(); void deleteItem(state.rootId, row.dataset.path); }
      });
      void loadTagFolders();
      renderNavigation();
      renderToolbar();
      renderCollections();
      renderTabs();
      await loadDirectory();
      await restoreOpenDocuments();
      state.revealReady = true;
      void refreshTagIndex();
      status("Navegação pronta.");
      syncOpenBuffers();
      state.bufferSyncTimer = window.setInterval(syncOpenBuffers, 1500);
    } catch (error) {
      status(error.status === 401 ? "Sua sessão expirou. Entre novamente." : "Não foi possível carregar a instância.", true);
    }
  }

  function treeBranchKey(rootId, path) { return `${rootId}\u0000${path}`; }
  // HF-NAV-006: each favorite folder is its own tree, following the same rules as the "/" tree.
  // Each tree keeps its open branches in its own set (`treeId`); listings come from the single root,
  // so trees share the cache and requests (`rootId`).
  const FAVORITE_TREE = "fav\u0000";
  function favoriteTreeId(path) { return `${FAVORITE_TREE}${path}`; }
  function isFavoriteTree(treeId) { return treeId.startsWith(FAVORITE_TREE); }
  function treeDataRoot(treeId) { return isFavoriteTree(treeId) ? BASE_ID : treeId; }
  function withinPath(path, folder) { return folder === "" || path === folder || path.startsWith(`${folder}/`); }
  function pathDepth(path) { return path.split("/").filter(Boolean).length; }
  function treeExpandedFor(treeId) {
    let expanded = state.treeExpanded.get(treeId);
    if (!expanded) {
      expanded = new Set(isFavoriteTree(treeId) ? savedFavoriteTree(treeId.slice(FAVORITE_TREE.length)) : []);
      state.treeExpanded.set(treeId, expanded);
    }
    return expanded;
  }
  // Favorite trees are restored in this browser as the user left them. The "/" tree never
  // auto-expands anything on load.
  // Stored as a list of [favorite, open branches] pairs: no folder name is ever used as a key.
  function savedFavoriteTrees() {
    if (!state.favoriteTreesSaved) {
      let saved = null;
      try { saved = JSON.parse(localStorage.getItem("hf-favoritos-abertos") || "null"); } catch (_error) { saved = null; }
      const pairs = Array.isArray(saved) ? saved.filter(pair => Array.isArray(pair) && typeof pair[0] === "string" && Array.isArray(pair[1])) : [];
      state.favoriteTreesSaved = new Map(pairs);
    }
    return state.favoriteTreesSaved;
  }
  function savedFavoriteTree(path) {
    return (savedFavoriteTrees().get(path) || []).filter(value => typeof value === "string" && withinPath(value, path));
  }
  // The favorite itself stays open or closed as it was; only the × removes it.
  function forgetFavoriteBranch(path) {
    let forgot = false;
    for (const [treeId, expanded] of state.treeExpanded) {
      if (!isFavoriteTree(treeId)) continue;
      const root = treeId.slice(FAVORITE_TREE.length);
      for (const value of [...expanded]) {
        if (value !== root && withinPath(value, path)) { expanded.delete(value); forgot = true; }
      }
    }
    if (forgot) saveFavoriteTrees();
  }
  function saveFavoriteTrees() {
    const pairs = [];
    for (const item of favoriteItems()) {
      const expanded = [...treeExpandedFor(favoriteTreeId(item.path))].filter(value => withinPath(value, item.path));
      if (expanded.length) pairs.push([item.path, expanded]);
    }
    state.favoriteTreesSaved = new Map(pairs);
    try { localStorage.setItem("hf-favoritos-abertos", JSON.stringify(pairs)); } catch (_error) { /* this browser's convenience state only */ }
  }
  // HF-NAV-006: clicking the name navigates to the folder and expands it without collapsing; only the arrow toggles it.
  function activateTreeFolder(rootId, path, treeId = rootId) {
    const expanded = treeExpandedFor(treeId);
    expanded.add(path);
    if (isFavoriteTree(treeId)) saveFavoriteTrees();
    state.treeInitialized.add(treeId);
    // The clicked tree already shows the folder: other trees do not open to follow it.
    state.treeOrigin = {treeId, path};
    state.view = "files"; state.rootId = rootId; state.path = path; state.localFilter = "";
    state.selectedItem = {rootId, path, type: "directory"};
    renderNavigation(); renderToolbar(); loadDirectory();
  }
  function treeKeyboard(tree) {
    if (tree.dataset.teclado) return;
    tree.dataset.teclado = "1";
    tree.addEventListener("keydown", event => {
      const names = [...tree.querySelectorAll(".node-row .tree-entry-name")].filter(node => node.offsetParent !== null);
      const index = names.indexOf(event.target);
      if (index < 0) return;
      const row = event.target.closest(".node-row");
      const chevron = row.querySelector("button.chev");
      const go = node => { if (!node) return; event.preventDefault(); for (const other of names) other.tabIndex = -1; node.tabIndex = 0; node.focus(); };
      if (event.key === "ArrowDown") go(names[index + 1]);
      else if (event.key === "ArrowUp") go(names[index - 1]);
      else if (event.key === "Home") go(names[0]);
      else if (event.key === "End") go(names.at(-1));
      else if (event.key === "ArrowRight" && chevron) {
        event.preventDefault();
        if (chevron.getAttribute("aria-expanded") !== "true") chevron.click(); else go(names[index + 1]);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        if (chevron && chevron.getAttribute("aria-expanded") === "true") chevron.click();
        else go(row.parentElement?.closest(".node")?.parentElement?.closest(".node")?.querySelector(":scope > .node-row .tree-entry-name"));
      }
    });
  }
  function rovingTree(tree) {
    const names = [...tree.querySelectorAll(".node-row .tree-entry-name")];
    const current = tree.querySelector(".node-row.ativo .tree-entry-name") || names[0];
    for (const node of names) node.tabIndex = node === current ? 0 : -1;
    for (const chevron of tree.querySelectorAll("button.chev")) chevron.tabIndex = -1;
  }
  function treeChevron(open, label, action) {
    const control = button("", action, "chev");
    control.setAttribute("aria-label", `${open ? "Recolher" : "Expandir"} ${label}`);
    control.setAttribute("aria-expanded", String(open));
    const svg = document.createElementNS(svgNamespace, "svg");
    svg.setAttribute("viewBox", "0 0 10 10"); svg.setAttribute("width", "9"); svg.setAttribute("height", "9");
    svg.setAttribute("fill", "currentColor"); svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(svgNamespace, "path");
    path.setAttribute("d", "M3 1l4 4-4 4z"); svg.append(path); control.append(svg);
    return control;
  }
  function toggleTreeBranch(rootId, path, treeId = rootId) {
    const expanded = treeExpandedFor(treeId);
    const key = treeBranchKey(rootId, path);
    const opening = !expanded.has(path);
    if (opening) expanded.add(path); else expanded.delete(path);
    if (isFavoriteTree(treeId)) saveFavoriteTrees();
    state.treeInitialized.add(treeId);
    renderNavigation();
    if (opening && !state.treeEntries.has(key)) void loadTreeBranch(rootId, path);
  }
  function scrollActiveTreeRow(treeId) {
    if (isFavoriteTree(treeId)) {
      if (state.favoritesOpen === false) return;
      // Favorites re-renders right after (renderFavorites); scrolling happens after that render.
      queueMicrotask(() => [...$("#favoritos").querySelectorAll(".node-row.ativo")].find(row => row.dataset.tree === treeId)?.scrollIntoView({block: "nearest"}));
    } else if (state.treeSectionOpen !== false) $("#tree .node-row.ativo")?.scrollIntoView({block: "nearest"});
  }
  // HF-NAV-006: navigating outside the tree (list, breadcrumb, link, tab, search, or document)
  // expands the ancestors of the current location, highlights it, and scrolls only as needed to
  // reveal it. Nothing auto-expands on app startup: `revealReady` only turns on after the first listing.
  async function revealInTree(path) {
    if (!state.revealReady) return;
    const origin = state.treeOrigin; state.treeOrigin = null;
    if (origin && origin.path === path) { scrollActiveTreeRow(origin.treeId); return; }
    const parts = path.split("/").filter(Boolean);
    const ancestors = parts.map((_part, index) => parts.slice(0, index).join("/"));
    // With the Favorites section open, an item inside a favorite does not expand the "/" tree.
    // The deepest open favorite that contains it is the one that follows; with none open, nothing
    // expands. A favorite pointing at "/" contains everything and is exempt from this rule.
    const holders = state.favoritesOpen === false ? [] : favoriteItems().filter(item => item.path && withinPath(path, item.path));
    if (holders.length) {
      const holder = holders.filter(item => treeExpandedFor(favoriteTreeId(item.path)).has(item.path))
        .sort((left, right) => pathDepth(right.path) - pathDepth(left.path))[0];
      if (!holder) return;
      const treeId = favoriteTreeId(holder.path);
      const opened = treeExpandedFor(treeId);
      const inside = ancestors.filter(ancestor => withinPath(ancestor, holder.path));
      let changedTree = false;
      for (const ancestor of inside) if (!opened.has(ancestor)) { opened.add(ancestor); changedTree = true; }
      if (changedTree) { saveFavoriteTrees(); renderNavigation(); }
      await Promise.all(inside.map(ancestor => loadTreeBranch(BASE_ID, ancestor)));
      scrollActiveTreeRow(treeId);
      return;
    }
    const expanded = treeExpandedFor(BASE_ID);
    let opened = false;
    for (const ancestor of ancestors) if (!expanded.has(ancestor)) { expanded.add(ancestor); opened = true; }
    state.treeInitialized.add(BASE_ID);
    if (opened) renderNavigation();
    await Promise.all(ancestors.map(ancestor => loadTreeBranch(BASE_ID, ancestor)));
    if (state.treeSectionOpen === false) return;
    $("#tree .node-row.ativo")?.scrollIntoView({block: "nearest"});
  }
  // The tree and the listing request the same folder on the same click. The listing always makes
  // its own request (it needs the current folder after an operation); the tree reuses a request
  // already in flight. Nothing is cached after the response.
  function listDirectory(rootId, path, {join = true} = {}) {
    const key = treeBranchKey(rootId, path);
    const current = state.listInFlight.get(key);
    if (join && current) return current;
    const pending = request("api/list", {}, {rootId, path}).finally(() => {
      if (state.listInFlight.get(key) === pending) state.listInFlight.delete(key);
    });
    state.listInFlight.set(key, pending);
    return pending;
  }
  async function loadTreeBranch(rootId, path, {force = false} = {}) {
    const key = treeBranchKey(rootId, path);
    const pending = state.treeLoading.get(key);
    if (pending) return pending;
    if (!force && state.treeEntries.has(key)) return true;
    const operation = (async () => {
      try {
        const result = await listDirectory(rootId, path);
        state.treeEntries.set(key, result.entries);
        state.treeErrors.delete(key);
        return true;
      } catch (error) {
        if (force) state.treeEntries.delete(key);
        state.treeErrors.add(key);
        // A folder that disappeared, changed type, or lost permission is removed from what favorites stores.
        if ([403, 404, 422].includes(error?.status)) forgetFavoriteBranch(path);
        return false;
      } finally {
        state.treeLoading.delete(key);
        renderNavigation();
      }
    })();
    state.treeLoading.set(key, operation);
    renderNavigation();
    return operation;
  }
  async function refreshExpandedTreeBranches() {
    const refreshes = [];
    for (const [treeId, expanded] of state.treeExpanded) {
      for (const path of expanded) refreshes.push(loadTreeBranch(treeDataRoot(treeId), path, {force: true}));
    }
    await Promise.all(refreshes);
  }
  // HF-NAV-002: each entry comes exactly as the listing reports it; links show their literal target
  // and navigate to the real path; unreadable folders, broken links, special entries, and
  // addressless names appear dimmed (.inacessivel), with the reason in the tooltip.
  const ENTRY_REASONS = {
    permission: "Sem permissão para abrir",
    broken: "Link quebrado: o destino não existe",
    special: "Arquivo especial: não abre como documento",
    invalid_name: "Nome que não é UTF-8: sem endereço, não abre nem aceita operações",
    unavailable: "Indisponível no momento",
  };
  function entryLabel(entry) { return entry.type === "link" && typeof entry.target === "string" ? `${entry.name} → ${entry.target}` : entry.name; }
  function entryReason(entry) {
    if (entry.addressable === false) return ENTRY_REASONS.invalid_name;
    if (entry.type === "other") return ENTRY_REASONS.special;
    return entry.reason ? (ENTRY_REASONS[entry.reason] || ENTRY_REASONS.unavailable) : "";
  }
  function entryUsable(entry) { return entry.addressable !== false && entry.openable !== false && entry.type !== "other"; }
  function entryIsFolder(entry) { return entry.type === "directory" || (entry.type === "link" && entry.resolvedType === "directory"); }
  function entryIcon(entry, path = null) {
    const emoji = path !== null && entry.type === "directory" ? itemState(BASE_ID, path)?.emoji : null;
    if (emoji) { const node = el("span", emoji, "emoji-icone"); node.setAttribute("aria-hidden", "true"); return node; }
    return iconNode(entryIsFolder(entry) ? "folder" : "file", entry.name);
  }
  function navigateFolder(path) {
    state.view = "files"; state.rootId = BASE_ID; state.path = path; state.localFilter = "";
    state.selectedItem = {rootId: BASE_ID, path, type: "directory"};
    state.searchResult = null; state.selectedTag = ""; state.selectedLabel = "";
    renderNavigation(); renderToolbar(); renderCollections(); loadDirectory();
  }
  // Folder shown by the listing: the last one received from the server, not the one still loading.
  function listedFolder() {
    return state.view === "files" && state.listingRootId ? [state.listingRootId, state.listingPath] : [state.rootId, state.path];
  }
  // A click on the old list, while the next folder is still loading, applies to the clicked item:
  // the pending navigation is abandoned and the item opens from the folder it is actually in.
  function activateListed(rootId, parent, entry) {
    const key = tabKey(rootId, parent);
    state.listScroll.set(key, {top: $("#results").scrollTop, focus: parent ? `${parent}/${entry.name}` : entry.name});
    if (state.view === "files" && (state.rootId !== rootId || state.path !== parent)) {
      state.listRequestId += 1; state.busy = false;
      state.rootId = rootId; state.path = parent;
    }
    activateEntry(parent, entry);
  }
  function activateEntry(parentPath, entry) {
    if (!entryUsable(entry)) { status(entryReason(entry) || "Este item não abre.", true); return; }
    if (entry.type === "link") {
      const target = relativePath(entry.resolved);
      if (entry.resolvedType === "directory") navigateFolder(target);
      else void openDocument(BASE_ID, target);
      return;
    }
    const path = parentPath ? `${parentPath}/${entry.name}` : entry.name;
    if (entry.type === "directory") navigateFolder(path);
    else void openDocument(BASE_ID, path);
  }
  function renderTreeEntries(container, rootId, parentPath, entries, treeId = rootId) {
    const levelBase = isFavoriteTree(treeId) ? pathDepth(treeId.slice(FAVORITE_TREE.length)) : 0;
    if (!entries.length) { container.append(el("p", "Pasta vazia", "tree-empty")); return; }
    const collator = new Intl.Collator("pt-BR", {numeric: true, sensitivity: "base"});
    const ordered = [...entries].sort((left, right) => {
      const typeOrder = entryIsFolder(left) === entryIsFolder(right) ? 0 : entryIsFolder(left) ? -1 : 1;
      // Grouping requires folders to stay together; otherwise every type change would repeat the header.
      if (typeOrder) return typeOrder;
      const compare = state.treeOrdering === "type"
        ? collator.compare(entryKindKey(left), entryKindKey(right)) || collator.compare(left.name, right.name)
        : collator.compare(left.name, right.name);
      return state.treeDirection === "desc" ? -compare : compare;
    });
    let priorType = "";
    for (const entry of ordered) {
      if (state.treeGrouping === "type") {
        const group = entryIsFolder(entry) ? "Pastas" : "Arquivos";
        if (group !== priorType) { container.append(el("div", group, "arv-grupo")); priorType = group; }
      }
      const path = parentPath ? `${parentPath}/${entry.name}` : entry.name;
      const usable = entryUsable(entry);
      const directory = entry.type === "directory" && usable;
      const node = el("div", undefined, "node");
      const row = el("div", undefined, directory ? "node-row" : "node-row arq");
      row.setAttribute("role", "treeitem");
      row.setAttribute("aria-label", entryLabel(entry));
      row.dataset.path = path; row.dataset.tree = treeId;
      row.setAttribute("aria-level", String(pathDepth(path) - levelBase + 1));
      if (!usable) { row.classList.add("inacessivel"); row.setAttribute("aria-disabled", "true"); }
      const activePath = state.activeDocument && state.activeDocument.rootId === rootId
        ? state.activeDocument.path : state.path;
      row.classList.toggle("ativo", usable && state.view === "files" && state.rootId === rootId && activePath === path);
      // In Favorites, dropping an item only favorites it (HF-FILE-001): folders there do not receive dropped items.
      if (directory && !isFavoriteTree(treeId)) makeFolderDropTarget(row, path);
      const icon = entryIcon(entry, path); icon.classList.add("icone");
      // Folder: the name navigates and expands without collapsing (HF-NAV-006); other entry types follow activateEntry.
      // A file opened from the tree is already visible there; a link leads elsewhere, which then gets revealed.
      const activate = () => {
        if (directory) { activateTreeFolder(rootId, path, treeId); return; }
        if (usable && entry.type === "file") state.treeOrigin = {treeId, path};
        activateEntry(parentPath, entry);
      };
      const name = button("", activate, "tree-entry-name");
      name.append(icon, el("span", entryLabel(entry), "nome"));
      if (directory && isTagFolder(path)) name.append(tagFolderBadge());
      name.title = usable ? entryLabel(entry) : `${entryLabel(entry)} — ${entryReason(entry)}`;
      if (!usable) name.setAttribute("aria-disabled", "true");
      row.onclick = event => { if (event.target === row) activate(); };
      if (entry.addressable !== false) {
        const full = usable && ["file", "directory"].includes(entry.type);
        row.addEventListener("contextmenu", event => showItemMenu(event, rootId, path, entry.type === "file", entry.type, {pathOnly: !full}));
      }
      if (directory) {
        const expanded = treeExpandedFor(treeId).has(path);
        row.classList.toggle("aberto", expanded); row.setAttribute("aria-expanded", String(expanded));
        row.append(treeChevron(expanded, entry.name, () => toggleTreeBranch(rootId, path, treeId)), name);
        const children = el("div", undefined, "node-filhos"); children.setAttribute("role", "group");
        children.hidden = !expanded;
        node.append(row, children);
        if (expanded) renderTreeChildren(children, rootId, path, treeId);
      } else {
        const spacer = el("span", undefined, "chev vazio");
        spacer.setAttribute("aria-hidden", "true");
        row.append(spacer, name);
        node.append(row);
      }
      container.append(node);
    }
  }
  function renderTreeChildren(children, rootId, path, treeId = rootId) {
    const key = treeBranchKey(rootId, path);
    if (state.treeEntries.has(key)) renderTreeEntries(children, rootId, path, state.treeEntries.get(key), treeId);
    else if (state.treeErrors.has(key)) {
      children.append(el("span", "Não foi possível carregar esta pasta.", "tree-load-error"));
      children.append(button("Tentar novamente", () => void loadTreeBranch(rootId, path, {force: true}), "tree-retry"));
    } else {
      children.append(el("span", "Carregando…", "tree-loading"));
      if (!state.treeLoading.has(key)) queueMicrotask(() => {
        if (treeExpandedFor(treeId).has(path)) void loadTreeBranch(rootId, path);
      });
    }
  }
  function renderNavigation() {
    const tree = $("#tree");
    // Tree key presses (Right, Left, Enter) redraw it: focus returns to the same node.
    const treeFocus = tree.contains(document.activeElement) ? document.activeElement.closest(".node-row")?.dataset.path : undefined;
    tree.replaceChildren();
    tree.setAttribute("role", "tree"); tree.setAttribute("aria-label", "Arquivos e pastas");
    const heading = $("#tree-heading");
    const headingLabel = heading.querySelector(".sec-rotulo") || heading;
    heading.removeAttribute("role"); heading.removeAttribute("tabindex");
    headingLabel.setAttribute("role", "button");
    headingLabel.setAttribute("tabindex", "0");
    headingLabel.setAttribute("aria-expanded", String(state.treeSectionOpen !== false));
    heading.classList.toggle("fechada", state.treeSectionOpen === false);
    tree.hidden = state.treeSectionOpen === false;
    const setTreeOpen = open => {
      state.treeSectionOpen = open;
      tree.hidden = !open;
      heading.classList.toggle("fechada", !open);
      headingLabel.setAttribute("aria-expanded", String(open));
      // A collapsed section stays collapsed after reload, like Favorites, Tags, and Labels.
      try { localStorage.setItem("hf-secao-treeSectionOpen", open ? "aberta" : "fechada"); } catch (_error) { /* optional */ }
    };
    heading.onclick = event => {
      if (event.target.closest(".sec-ferramentas")) return;
      setTreeOpen(state.treeSectionOpen === false);
    };
    headingLabel.onkeydown = event => {
      if (!["Enter", " "].includes(event.key)) return;
      event.preventDefault(); setTreeOpen(state.treeSectionOpen === false);
    };

    // A single tree whose first node is "/", collapsed on first load (HF-NAV-006).
    const rootNode = el("div", undefined, "node");
    const rootExpanded = treeExpandedFor(BASE_ID).has("");
    const rootRow = el("div", undefined, "node-row raiz-vault");
    rootRow.setAttribute("role", "treeitem"); rootRow.setAttribute("aria-label", "/");
    rootRow.classList.toggle("aberto", rootExpanded);
    const activeRootPath = state.activeDocument ? state.activeDocument.path : state.path;
    rootRow.classList.toggle("ativo", state.view === "files" && activeRootPath === "");
    const rootName = button("", () => activateTreeFolder(BASE_ID, ""), "tree-entry-name tree-root-name");
    const rootIcon = iconNode("folder"); rootIcon.classList.add("icone");
    rootName.append(rootIcon, el("span", "/", "nome")); rootName.title = "/";
    rootRow.append(treeChevron(rootExpanded, "/", () => toggleTreeBranch(BASE_ID, "")), rootName);
    rootRow.onclick = event => { if (event.target === rootRow) activateTreeFolder(BASE_ID, ""); };
    rootRow.addEventListener("contextmenu", event => showItemMenu(event, BASE_ID, "", false, "directory", {pathOnly: true}));
    const rootChildren = el("div", undefined, "node-filhos"); rootChildren.hidden = !rootExpanded; rootChildren.setAttribute("role", "group");
    if (rootExpanded) renderTreeChildren(rootChildren, BASE_ID, "");
    rootNode.append(rootRow, rootChildren); tree.append(rootNode);
    rootRow.dataset.path = ""; rootRow.setAttribute("aria-level", "1"); rootRow.setAttribute("aria-expanded", String(rootExpanded));
    treeKeyboard(tree); rovingTree(tree);
    if (treeFocus !== undefined) {
      const target = [...tree.querySelectorAll(".node-row")].find(node => node.dataset.path === treeFocus)?.querySelector(".tree-entry-name");
      if (target) {
        for (const other of tree.querySelectorAll(".tree-entry-name")) other.tabIndex = -1;
        target.tabIndex = 0; target.focus({preventScroll: true});
      }
    }
    // Favorite trees share the same loaded branches: they redraw together with the "/" tree.
    renderFavorites();

    const trash = $("#btn-lixeira");
    trash.classList.toggle("ativo", state.view === "trash");
    trash.setAttribute("aria-current", state.view === "trash" ? "page" : "false");
    trash.onclick = () => enterView("trash");
    $("#sidebar-toggle").onclick = () => {
      const mobile = window.matchMedia("(max-width: 720px)").matches;
      let opened;
      if (mobile) {
        opened = $("#sidebar").classList.toggle("sidebar-open");
        $("#sidebar").classList.toggle("aberto", opened);
      } else opened = !$("#app-layout").classList.toggle("sidebar-collapsed");
      $("#sidebar-toggle").setAttribute("aria-expanded", String(opened));
    };
    $("#btn-inicio").onclick = () => selectRoot(state.rootId);
    $("#btn-config").onclick = () => showPreferences();
    $("#theme-choice").onclick = () => {
      if (!state.ui) return;
      const order = ["system", "light", "dark"];
      const current = order.indexOf(state.ui.preferences.theme);
      state.ui.preferences.theme = order[(current + 1 + order.length) % order.length];
      applyPreferences(); scheduleSave();
    };
    const mobile = window.matchMedia("(max-width: 720px)").matches;
    $("#sidebar-toggle").setAttribute("aria-expanded", String(mobile
      ? $("#sidebar").classList.contains("sidebar-open")
      : !$("#app-layout").classList.contains("sidebar-collapsed")));

    function selectRoot(_rootId) { navigateFolder(""); }
  }
  // Sidebar views (Trash, tag, label): are pushed to history and update the breadcrumb, tabs, and highlight.
  function enterView(view, {tag = "", label = ""} = {}) {
    if (view === "labels" && !state.ui.labels[label]) { navigateFolder(state.path); return; }
    state.view = view; state.localFilter = ""; state.searchResult = null; state.activeDocument = null;
    state.selectedTag = view === "tags" ? tag : ""; state.selectedLabel = view === "labels" ? label : "";
    state.selectedForZip.clear(); state.listRequestId++;
    if (view === "trash") $("#painel-info").classList.remove("aberto");
    showDocumentModeControls(null);
    renderNavigation(); renderToolbar(); renderCollections(); renderBreadcrumbs(); renderTabs();
    recordHistory({view, tag: state.selectedTag, label: state.selectedLabel});
    if (view === "trash") loadTrash();
    else if (view === "labels") renderCollectionItems();
    else if (view === "tags") { if (tag) void selectTag(tag); else void loadTags(); }
  }

  // Settings dialog: uppercase section headers (.opt-secao), 13px option rows (.opt-linha), and
  // choices via a segmented control.
  function showPreferences({section = ""} = {}) {
    const dialog = $("#operation-dialog");
    if (dialog.open) return;
    const form = el("form"); form.method = "dialog";
    const title = el("h2", "Configurações"); title.id = "operation-dialog-title";
    const choice = (label, options, current) => {
      const row = el("div", undefined, "opt-linha");
      const name = el("span", label); name.id = `opt-${options[0][0]}-rotulo`;
      const group = el("div", undefined, "segmentado"); group.setAttribute("role", "group"); group.setAttribute("aria-labelledby", name.id);
      let value = current;
      const paint = () => { for (const node of group.children) { const on = node.dataset.value === value; node.classList.toggle("ativo", on); node.setAttribute("aria-pressed", String(on)); } };
      for (const [optionValue, optionLabel] of options) {
        const node = button(optionLabel, () => { value = optionValue; paint(); });
        node.dataset.value = optionValue; group.append(node);
      }
      paint(); row.append(name, group);
      return {row, get value() { return value; }};
    };
    const theme = choice("Tema", [["system", "Automático"], ["light", "Claro"], ["dark", "Escuro"]], state.ui.preferences.theme);
    const density = choice("Densidade da listagem", [["comfortable", "Padrão"], ["compact", "Compacta"]], state.ui.preferences.density);
    // Only tags from the current index can be ignored: they already arrive normalized from the server (HF-META-001).
    let ignored = [...ignoredTags()];
    const chips = el("div", undefined, "opt-tags-ign");
    const known = el("datalist"); known.id = "opt-tags-lista";
    const tagInput = el("input", undefined, "dlg-input"); tagInput.type = "text"; tagInput.setAttribute("list", known.id);
    tagInput.placeholder = "#tag"; tagInput.setAttribute("aria-label", "Tag a ignorar"); tagInput.autocomplete = "off";
    const tagHint = el("p", "", "opt-tag-aviso"); tagHint.setAttribute("aria-live", "polite");
    const paintTags = () => {
      chips.replaceChildren();
      if (!ignored.length) chips.append(el("span", "Nenhuma tag ignorada.", "vazio"));
      for (const tag of ignored) {
        const chip = el("span", `#${tag}`, "opt-tag-ign");
        chip.append(closeButton(`Voltar a mostrar #${tag}`, () => { ignored = ignored.filter(value => value !== tag); paintTags(); tagInput.focus(); }, "opt-tag-x"));
        chips.append(chip);
      }
      known.replaceChildren(...(state.tagIndex?.tags || []).map(item => item.tag).filter(tag => !ignored.includes(tag)).map(tag => { const option = el("option"); option.value = `#${tag}`; return option; }));
    };
    const addTag = () => {
      const value = tagInput.value.trim().replace(/^#/, "").normalize("NFC").toLowerCase().normalize("NFC");
      if (!value) return;
      if (!(state.tagIndex?.tags || []).some(item => item.tag === value)) { tagHint.textContent = "Escolha uma tag que aparece na lista de tags."; return; }
      if (!ignored.includes(value)) ignored = [...ignored, value];
      tagInput.value = ""; tagHint.textContent = ""; paintTags();
    };
    tagInput.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); addTag(); } });
    tagInput.addEventListener("input", () => { tagHint.textContent = ""; });
    const tagAdd = el("div", undefined, "opt-tag-add");
    tagAdd.append(tagInput, button("Ignorar", addTag, "acao"), known);
    paintTags();
    if (!state.tagIndex) void refreshTagIndex().then(paintTags);
    // Watched folders (HF-META-002): removing one from the list takes effect on Apply; adding one happens per folder.
    const removedFolders = new Set();
    const folderList = el("div", undefined, "opt-pastas");
    const paintFolders = () => {
      folderList.replaceChildren();
      if (!state.tagFolders) { folderList.append(el("span", "Não foi possível carregar a lista de pastas monitoradas.", "vazio")); return; }
      const current = state.tagFolders.filter(folder => !removedFolders.has(folder.path));
      if (!current.length) folderList.append(el("span", "Nenhuma pasta monitorada.", "vazio"));
      for (const folder of current) {
        const row = el("div", undefined, "opt-pasta");
        const covering = tagFolderCovering(relativePath(folder.path));
        const note = !folder.available ? "não existe mais ou não abre" : covering ? `já coberta por ${covering}` : "";
        const text = el("span", undefined, "opt-pasta-texto");
        text.append(el("span", folder.path, "opt-pasta-cam"));
        if (note) text.append(el("span", note, "opt-pasta-nota"));
        row.append(iconNode("folder"), text, closeButton(`Parar de monitorar ${folder.path}`, () => {
          removedFolders.add(folder.path); paintFolders(); (folderList.querySelector(".opt-tag-x") || tagInput).focus();
        }, "opt-tag-x"));
        folderList.append(row);
      }
    };
    paintFolders();
    // Each folder's availability is re-checked when opened.
    void loadTagFolders().then(() => { if (folderList.isConnected) paintFolders(); });
    const folderSection = el("div", "Pastas monitoradas (tags)", "opt-secao");
    const folderHint = el("p", "Entram as notas .md dessas pastas e de todas as subpastas. Subpastas ocultas (nome começando com ponto) e temporárias só entram se forem marcadas elas mesmas. Para marcar, use o botão direito numa pasta (no toque, segure o dedo sobre ela) ou o painel Informações.", "opt-dica");
    const exit = button("Sair da sessão", () => {
      dialog.close();
      if (logoutForm?.requestSubmit) logoutForm.requestSubmit();
    }, "acao opt-larga");
    exit.setAttribute("aria-label", "Sair da sessão com segurança");
    const menu = el("menu");
    menu.append(button("Cancelar", () => dialog.close(), "secondary-button"));
    const save = el("button", "Aplicar", "primary-button"); save.type = "submit"; menu.append(save);
    form.append(title, el("div", "Aparência", "opt-secao"), theme.row, el("div", "Listagem", "opt-secao"), density.row,
      folderSection, folderList, folderHint,
      el("div", "Tags ignoradas", "opt-secao"), chips, tagAdd, tagHint, el("div", "Sessão", "opt-secao"), exit, menu);
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (removedFolders.size) void setTagFolders([...removedFolders].map(relativePath), false).then(done => {
        if (done) status(removedFolders.size === 1 ? "Uma pasta deixou de ser monitorada." : `${formatCount(removedFolders.size)} pastas deixaram de ser monitoradas.`);
      });
      state.ui.preferences.theme = theme.value;
      state.ui.preferences.density = density.value;
      const tagsChanged = !same([...ignoredTags()].sort(), [...ignored].sort());
      if (ignored.length) state.ui.preferences.ignoredTags = [...ignored];
      else delete state.ui.preferences.ignoredTags;
      applyPreferences(); scheduleSave(); dialog.close();
      if (tagsChanged) {
        if (state.view === "tags" && ignored.includes(state.selectedTag)) state.selectedTag = "";
        renderCollections();
        // The server filters by the saved state; the index is only reloaded after saving.
        void state.savePromise?.then(() => state.view === "tags" ? loadTags() : refreshTagIndex());
      }
    });
    dialog.replaceChildren(form);
    dialog.showModal();
    save.focus();
    if (section === "tagFolders") { folderSection.scrollIntoView({block: "start"}); (folderList.querySelector(".opt-tag-x") || save).focus({preventScroll: true}); }
  }

  async function loadDirectory({preserveActive = false, recordTab = true} = {}) {
    if (!preserveActive) state.selectedForZip.clear();
    if (!preserveActive) state.selectedItem = {rootId: state.rootId, path: state.path, type: "directory"};
    clearActiveVisual();
    const requestId = ++state.listRequestId;
    const rootId = state.rootId;
    const path = state.path;
    // Reloading the same folder does not reopen a branch the user collapsed.
    const preservedDocument = preserveActive ? state.activeDocument : null;
    if (!preserveActive) {
      state.activeDocument = null;
      renderToolbar();
    }
    state.searchResult = null;
    if (!rootId) { state.busy = false; renderResults([]); renderBreadcrumbs(); return; }
    state.busy = true;
    status("Carregando pasta…");
    try {
      const result = await listDirectory(rootId, path, {join: false});
      if (requestId !== state.listRequestId || state.view !== "files" || state.rootId !== rootId || state.path !== path) return;
      state.entries = result.entries;
      state.listingVersion = result.listingVersion;
      resetFolderSizes();
      const writableChanged = state.listingWritable !== (result.writable !== false);
      state.listingWritable = result.writable !== false;
      const moved = state.listingRootId !== rootId || state.listingPath !== path;
      state.listingRootId = rootId;
      state.listingPath = path;
      if (writableChanged && !state.activeDocument) renderToolbar();
      const treeKey = treeBranchKey(rootId, path);
      state.treeEntries.set(treeKey, result.entries);
      state.treeErrors.delete(treeKey);
      if (recordTab) rememberTab();
      if (!preservedDocument) recordHistory({path});
      renderNavigation(); renderBreadcrumbs(); renderResults(state.entries);
      restoreListPosition(rootId, path, moved);
      // The open info panel follows the folder that was just opened.
      if (!preserveActive) refreshInfoPanel();
      if (moved) void revealInTree(path);
      status(filterStatus());
    } catch (error) {
      if (requestId !== state.listRequestId || state.view !== "files" || state.rootId !== rootId || state.path !== path) return;
      state.restoreListScroll = false; state.focusListOnRender = false;
      if (error.status === 401) {
        const panel = $("#results"); panel.replaceChildren();
        const notice = emptyState("Sua sessão terminou. Entre de novo para ver esta pasta.", "pasta");
        notice.append(button("Entrar de novo", () => window.location.assign(`${base}login`), "primary-button"));
        panel.append(notice); renderBreadcrumbs();
        return;
      }
      const reason = error.status === 409 ? "A pasta mudou durante a leitura. Atualize para tentar de novo."
        : error.status === 403 ? "Sem permissão para abrir esta pasta."
          : error.status === 404 ? "Esta pasta não existe mais."
            : error.status ? "Não foi possível listar esta pasta." : "Sem conexão com o servidor. Confira a rede e tente de novo.";
      // The same folder stays visible when only the re-read failed (e.g. network down); a different
      // folder shows the reason and "Tentar de novo" instead of "Pasta vazia".
      if (state.listingRootId !== rootId || state.listingPath !== path) {
        const panel = $("#results"); panel.replaceChildren();
        const notice = emptyState(reason, "pasta");
        notice.append(button("Tentar de novo", () => void loadDirectory(), "secondary-button"));
        panel.append(notice); renderBreadcrumbs();
      }
      status(reason, true);
    } finally {
      if (requestId === state.listRequestId) state.busy = false;
      if (requestId === state.listRequestId && preservedDocument &&
          state.rootId === rootId && state.path === path) showDocument(preservedDocument, {expandTree: false});
    }
  }
  // List footer: with the "here" filter, how many items are shown out of how many exist.
  function filterStatus() {
    const total = state.entries.length;
    const noun = total === 1 ? "item" : "itens";
    const base = state.localFilter.trim()
      ? `${formatCount(displayEntries().length)} de ${formatCount(total)} ${noun} nesta pasta aparecem com o filtro.`
      : `${formatCount(total)} ${noun} nesta pasta.`;
    const measuring = state.ui.preferences.ordering === "size" && [...state.dirSizes.values()].includes("pending");
    return measuring ? `${base} Medindo as pastas; a ordem por tamanho se ajusta ao terminar.` : base;
  }
  // Back, switching tabs, and closing a document reopen the folder the list was on, with the open
  // row focused; entering a new folder starts at the top.
  function restoreListPosition(rootId, path, moved) {
    const panel = $("#results");
    const memory = state.restoreListScroll ? state.listScroll.get(tabKey(rootId, path)) : null;
    state.restoreListScroll = false;
    if (memory) {
      panel.scrollTop = memory.top || 0;
      const row = memory.focus ? [...panel.querySelectorAll("tr.item")].find(item => item.dataset.path === memory.focus) : null;
      if (row) { rovingRow(row); row.focus({preventScroll: true}); }
    } else if (moved) panel.scrollTop = 0;
  }
  // Folder tabs: the active tab is matched by the (rootId, path) key from HF-META-004, never by
  // object reference (saving replaces state.ui with a copy). Duplicate tabs at the same location
  // are allowed: "+" always creates a new one.
  function tabKey(rootId, path) { return `${rootId}\u0000${path}`; }
  function tabIndex(key) { return state.ui.tabs.findIndex(tab => tabKey(tab.rootId, tab.path) === key); }
  function tabAt(index, key) {
    const tab = state.ui.tabs[index];
    return tab && tabKey(tab.rootId, tab.path) === key ? index : tabIndex(key);
  }
  function activeTabIndex() { return state.activeTabKey === null ? -1 : tabAt(state.activeTabPos, state.activeTabKey); }
  function setActiveTab(index) {
    const tab = state.ui.tabs[index];
    state.activeTabPos = tab ? index : null;
    state.activeTabKey = tab ? tabKey(tab.rootId, tab.path) : null;
    // The active tab is restored on reload (this browser's convenience state; the saved state itself is unchanged).
    try {
      if (tab) {
        const value = JSON.stringify({index, path: tab.path});
        sessionStorage.setItem("hf-aba-ativa", value); localStorage.setItem("hf-aba-ativa", value);
      }
    } catch (_error) { /* optional */ }
  }
  function savedActiveTab(tabs) {
    try {
      // Each browser tab restores its own active tab; a new browser tab starts from the last one used.
      const saved = JSON.parse(sessionStorage.getItem("hf-aba-ativa") || localStorage.getItem("hf-aba-ativa") || "null");
      if (saved && tabs[saved.index] && tabs[saved.index].path === saved.path) return saved.index;
      const byPath = saved ? tabs.findIndex(tab => tab.path === saved.path) : -1;
      if (byPath >= 0) return byPath;
    } catch (_error) { /* no logging needed here */ }
    return tabs.length - 1;
  }
  function insertTab(tab, index) {
    state.ui.tabs.splice(index, 0, {rootId: tab.rootId, path: tab.path, mode: "navegar"});
    while (state.ui.tabs.length > 12) { state.ui.tabs.shift(); index--; }
    return index;
  }
  function rememberTab() {
    const key = tabKey(state.rootId, state.path);
    const active = activeTabIndex();
    // After closing the last tab, reloading the same folder does not recreate one.
    if ((active >= 0 && key === state.activeTabKey) || key === state.tablessKey) { renderTabs(); return; }
    state.tablessKey = null;
    // Navigating reuses the current tab; only "+" or "Abrir em nova aba" add another one.
    if (active >= 0) {
      state.ui.tabs[active] = { rootId: state.rootId, path: state.path, mode: "navegar" };
      setActiveTab(active);
    } else setActiveTab(insertTab({rootId: state.rootId, path: state.path}, state.ui.tabs.length));
    renderTabs();
    scheduleSave();
  }
  // index: the clicked tab; newTab: "+" (always creates one); reuse: "Abrir em nova aba" (activates the existing one).
  function openFolderTab(tab, {index = -1, newTab = false, reuse = false, atEnd = false} = {}) {
    const key = tabKey(tab.rootId, tab.path);
    const existing = reuse ? tabIndex(key) : -1;
    if ((newTab || (reuse && existing < 0)) && state.ui.tabs.length >= 12) {
      status("Limite de 12 abas de pasta. Feche uma aba para abrir outra.", true); return;
    }
    if (newTab || (reuse && existing < 0)) {
      const active = activeTabIndex();
      setActiveTab(insertTab(tab, atEnd || active < 0 ? state.ui.tabs.length : active + 1));
      scheduleSave();
    } else if (reuse) setActiveTab(existing);
    else setActiveTab(tabAt(index, key));
    state.tablessKey = null;
    state.restoreListScroll = state.restoreListScroll || !newTab;
    state.view = "files"; state.rootId = tab.rootId; state.path = tab.path; state.localFilter = "";
    state.searchResult = null; state.selectedTag = ""; state.selectedLabel = "";
    state.activeDocument = null;
    state.selectedItem = {rootId: tab.rootId, path: tab.path, type: "directory"};
    renderNavigation(); renderToolbar(); renderCollections(); renderTabs();
    void loadDirectory();
  }
  function closeFolderTab(position, key) {
    const index = tabAt(position, key);
    if (index < 0) return;
    const active = activeTabIndex();
    state.ui.tabs.splice(index, 1);
    if (index === active) {
      const neighbor = index < state.ui.tabs.length ? index : index - 1;
      if (neighbor < 0) { setActiveTab(-1); state.tablessKey = tabKey(state.rootId, state.path); }
      else if (state.view === "files" && !state.activeDocument) { scheduleSave(); openFolderTab(state.ui.tabs[neighbor], {index: neighbor}); return; }
      else setActiveTab(neighbor);
    } else if (active > index) setActiveTab(active - 1);
    renderTabs(); scheduleSave();
  }
  function renderTabs() {
    const strip = $("#tabs");
    strip.replaceChildren();
    const showingFolder = state.view === "files" && !state.activeDocument;
    const active = activeTabIndex();
    for (const [index, tab] of state.ui.tabs.entries()) {
      const key = tabKey(tab.rootId, tab.path);
      const current = showingFolder && index === active;
      const wrapper = el("div", undefined, current ? "aba ativa" : "aba");
      const label = tab.path.split("/").filter(Boolean).at(-1) || "/";
      const open = button(label, () => openFolderTab(tab, {index}), "aba-nome");
      open.title = absolutePath(tab.path);
      open.setAttribute("aria-current", current ? "page" : "false");
      wrapper.append(open, closeButton(`Fechar aba ${label}`, () => closeFolderTab(index, key), "aba-x"));
      strip.append(wrapper);
    }
    for (const [key, doc] of state.documents) {
      const current = state.activeDocument === doc || (state.splitEnabled && state.splitDocument === doc);
      const wrapper = el("div", undefined, current ? "aba ativa" : "aba");
      const open = button((dirtyDocument(doc) ? "• " : "") + doc.name, () => showDocument(doc), "aba-nome");
      open.title = absolutePath(doc.path);
      open.setAttribute("aria-label", dirtyDocument(doc) ? `${doc.name}, alterado` : doc.name);
      open.setAttribute("aria-current", current ? "page" : "false");
      wrapper.append(open, closeButton(`Fechar aba ${doc.name}`, async () => {
        if (dirtyDocument(doc)) {
          const choice = await askCloseDirty(doc);
          // The wait is asynchronous: only act if the document is still open.
          if (!choice || state.documents.get(key) !== doc) return;
          if (choice === "save") { const saved = await saveDocument(doc); if (!(saved?.ok ?? saved) || dirtyDocument(doc)) return; }
        }
        doc.destroy(); state.documents.delete(key);
        void syncOpenBuffers();
        if (state.activeDocument === doc || state.splitDocument === doc) {
          state.activeDocument = null; state.splitDocument = null; state.splitEnabled = false;
          $("#split-pane").classList.remove("aberto"); $("#split-pane").hidden = true;
          $("#workspace-splitter").hidden = true; $("#split-content").replaceChildren();
          const folderKey = tabKey(doc.rootId, parentOf(doc.path));
          state.listScroll.set(folderKey, {...state.listScroll.get(folderKey), focus: doc.path});
          state.restoreListScroll = true;
          loadDirectory();
        }
        renderTabs(); renderToolbar();
      }, "aba-x"));
      strip.append(wrapper);
    }
    const plus = button("+", () => {
      if (state.ui.tabs.length >= 12) { status("Limite de 12 abas de pasta. Feche uma aba para abrir outra.", true); return; }
      openFolderTab({rootId: BASE_ID, path: state.view === "files" ? state.path : ""}, {newTab: true, atEnd: true});
    }, "aba-mais");
    plus.dataset.tip = "Nova aba"; plus.setAttribute("aria-label", "Nova aba");
    strip.append(plus);
    rememberOpenDocuments();
    // The active tab always stays visible, even with many tabs open.
    strip.querySelector(".aba.ativa")?.scrollIntoView({block: "nearest", inline: "nearest"});
  }
  // Document tabs are restored on reload (the list is stored in this browser; unsaved text itself is
  // not stored, since the browser already warns before leaving with unsaved changes).
  function rememberOpenDocuments() {
    if (!state.documentsRestored) return;
    try {
      const docs = [...state.documents.values()].map(doc => ({rootId: doc.rootId, path: doc.path}));
      const active = state.activeDocument ? {rootId: state.activeDocument.rootId, path: state.activeDocument.path} : null;
      const value = JSON.stringify({docs, active});
      sessionStorage.setItem("hf-documentos", value); localStorage.setItem("hf-documentos", value);
    } catch (_error) { /* optional */ }
  }
  async function restoreOpenDocuments() {
    const startedAt = `${state.view}\u0000${state.rootId}\u0000${state.path}`;
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem("hf-documentos") || localStorage.getItem("hf-documentos") || "null"); } catch (_error) { saved = null; }
    for (const item of (saved?.docs || []).slice(0, 12)) {
      if (typeof item?.path !== "string" || typeof item?.rootId !== "string") continue;
      const key = `${item.rootId}\u0000${item.path}`;
      if (state.documents.has(key)) continue;
      try {
        const loaded = await documentRequest("api/file", item.rootId, item.path);
        const doc = createDocumentShell(item.rootId, item.path, loaded);
        setDocumentReadOnly(doc);
        state.documents.set(key, doc);
      } catch (_error) { /* the file is gone or can no longer be opened: the tab is not restored */ }
    }
    state.documentsRestored = true;
    const active = saved?.active ? state.documents.get(`${saved.active.rootId}\u0000${saved.active.path}`) : null;
    const moved = startedAt !== `${state.view}\u0000${state.rootId}\u0000${state.path}` || state.activeDocument;
    if (active && !moved) showDocument(active); else renderTabs();
  }
  function renderBreadcrumbs() {
    const nav = $("#breadcrumbs");
    nav.replaceChildren();
    const viewName = state.view === "trash" ? "Lixeira"
      : state.view === "tags" && state.selectedTag ? `#${state.selectedTag}`
        : state.view === "labels" && state.selectedLabel ? `Label: ${state.ui.labels[state.selectedLabel]?.name || ""}` : "";
    if (viewName) { nav.append(el("span", viewName, "seg atual")); nav.classList.remove("com-documento"); return; }
    // Breadcrumb shows the absolute path starting from "/" (HF-NAV-006).
    nav.append(button("/", () => navigateFolder(""), "seg"));
    let current = "";
    for (const [index, segment] of state.path.split("/").filter(Boolean).entries()) {
      if (index > 0) nav.append(el("span", "/", "sep"));
      current = current ? `${current}/${segment}` : segment;
      const target = current;
      nav.append(button(segment, () => navigateFolder(target), "seg"));
    }
    // In split view, the breadcrumb reflects the list; the document on the right has its own path.
    const shownDocument = state.activeDocument && !state.splitEnabled ? state.activeDocument : null;
    if (shownDocument) {
      nav.append(el("span", "/", "sep"));
      nav.append(el("span", shownDocument.name, "seg atual"));
    }
    // Only the last segment is highlighted: with a document open, that is its name.
    nav.classList.toggle("com-documento", Boolean(shownDocument));
    // The breadcrumb scrolls horizontally; the end is the current item and stays visible.
    const bar = nav.closest(".trilha-barra");
    if (bar) { bar.scrollLeft = bar.scrollWidth; bar.classList.toggle("cortada", bar.scrollLeft > 0); }
  }
  // The scope shows the end of the path, which identifies the folder; the beginning yields to "..." when truncated.
  function tailPath(path, max = 34) {
    if (path.length <= max) return path;
    const parts = path.split("/");
    let tail = parts.pop();
    while (parts.length && `…/${parts.at(-1)}/${tail}`.length <= max) tail = `${parts.pop()}/${tail}`;
    return `…/${tail}`;
  }
  function renderSidebarSearch() {
    const form = $("#sidebar-search");
    form.replaceChildren();
    const modes = state.view === "files" && state.rootId ? ["name", "text", "here"] : ["name", "text"];
    if (!modes.includes(state.searchUiMode)) state.searchUiMode = "name";
    if (state.searchUiMode === "here") state.searchText = state.localFilter;

    const box = el("div", undefined, "busca-box");
    const searchIcon = controlIcon("buscar"); searchIcon.classList.add("busca-lupa"); box.append(searchIcon);
    const query = el("input");
    query.id = "sidebar-search-query"; query.type = "search"; query.maxLength = 256;
    query.placeholder = "Buscar"; query.autocomplete = "off"; query.spellcheck = false;
    query.value = state.searchUiMode === "here" ? state.localFilter : state.searchText;
    query.setAttribute("aria-label", "Texto da busca");
    query.title = "nome = nomes a partir da pasta aberta · texto = conteúdo · aqui = filtra a pasta atual";
    query.addEventListener("input", () => {
      state.searchText = query.value;
      if (state.searchUiMode === "here") {
        state.localFilter = query.value; state.searchResult = null; state.selectedForZip.clear(); renderResults(state.entries); status(filterStatus());
      } else {
        clearTimeout(state.searchTimer);
        if (!query.value.trim()) {
          state.searchRequestId++; state.searchResult = null;
          if (state.view === "files" && !state.activeDocument) renderResults(state.entries);
        } else {
          state.searchTimer = window.setTimeout(() => runSearch(), 260);
        }
      }
    });
    const mode = button(({name: "nome", text: "texto", here: "aqui"})[state.searchUiMode], () => {
      const next = modes[(modes.indexOf(state.searchUiMode) + 1) % modes.length];
      state.searchUiMode = next;
      if (next === "here") state.localFilter = state.searchText;
      else { state.searchMode = next; state.localFilter = ""; }
      renderSidebarSearch();
      const nextQuery = $("#sidebar-search-query"); nextQuery.focus(); nextQuery.setSelectionRange(nextQuery.value.length, nextQuery.value.length);
      if (state.view === "files") renderResults(state.entries);
      if (state.searchText.trim() && next !== "here") {
        clearTimeout(state.searchTimer);
        state.searchTimer = window.setTimeout(() => runSearch(), 0);
      }
    }, "busca-modo");
    mode.id = "sidebar-search-mode"; mode.dataset.mode = state.searchUiMode;
    mode.title = "Alternar busca: nome, texto ou pasta atual";
    mode.dataset.tip = mode.title;
    mode.setAttribute("aria-label", `Modo da busca: ${{name: "nome", text: "texto", here: "pasta atual"}[state.searchUiMode]}`);
    box.append(query, mode); form.append(box);

    // HF-API-004: search starts from the open folder, and the scope stays visible.
    if (state.searchUiMode !== "here") {
      const scopePath = absolutePath(searchScope());
      const scope = el("div", `Em ${tailPath(scopePath)}`, "busca-escopo");
      scope.id = "sidebar-search-scope"; scope.title = `A busca desce a partir desta pasta: ${scopePath}`;
      form.append(scope);
    } else {
      const scope = el("div", "Só nesta pasta, pelo nome", "busca-escopo");
      scope.id = "sidebar-search-scope"; scope.title = "Filtra os itens da pasta aberta pelo nome.";
      form.append(scope);
    }
    form.onsubmit = event => {
      event.preventDefault(); state.searchText = query.value;
      if (state.searchUiMode === "here") {
        state.localFilter = query.value; state.searchResult = null; state.selectedForZip.clear(); renderResults(state.entries); status(filterStatus());
      } else { clearTimeout(state.searchTimer); void runSearch(); }
    };
  }
  function renderToolbar() {
    const fixed = $("#toolbar-fixed");
    const actions = $("#toolbar-actions");
    // Redrawing the toolbar returns focus to the same button (or one with the same id), not to the page.
    const focused = $("#toolbar")?.contains(document.activeElement) ? document.activeElement : null;
    queueMicrotask(() => {
      if (!focused || document.activeElement === focused) return;
      const again = focused.isConnected ? focused : (focused.id ? document.getElementById(focused.id) : null);
      if (again && (document.activeElement === document.body || !document.activeElement)) again.focus({preventScroll: true});
    });
    fixed.replaceChildren(); actions.replaceChildren();
    renderSidebarSearch();
    if (state.view !== "files") $("#painel-info").classList.remove("aberto");

    const splitButton = actionButton(state.splitEnabled ? "Fechar painel dividido" : "Dividir com o arquivo aberto", "dividir", () => setSplitEnabled(!state.splitEnabled));
    splitButton.id = "split-choice";
    splitButton.disabled = !state.activeDocument && !state.splitDocument && !state.splitEnabled;
    fixed.append(splitButton);
    const infoButton = actionButton("Informações", "informacoes", showInfoPanel);
    infoButton.id = "info-choice";
    infoButton.hidden = state.view !== "files";
    infoButton.setAttribute("aria-pressed", String($("#painel-info").classList.contains("aberto")));

    const doc = state.activeDocument || state.splitDocument;
    showDocumentModeControls(state.view === "files" ? doc : null);
    const listVisible = state.view === "files" && (!doc || state.splitEnabled);
    $("#lista-barra").hidden = !listVisible;

    if (doc && state.view === "files") {
      actions.append(doc.actionBar, infoButton);
      return;
    }
    if (state.view === "files") {
      actions.append(actionButton("Atualizar pasta", "atualizar", async () => {
        await loadDirectory({preserveActive: true}); await refreshExpandedTreeBranches();
      }));
      if (canWrite(state.rootId)) {
        const folder = button("", () => createItem("directory"), "acao tip-dir criar");
        folder.setAttribute("aria-label", "Nova pasta"); folder.dataset.tip = "Nova pasta"; folder.dataset.curto = "Pasta";
        folder.append(controlIcon("nova-pasta"), el("span", "Nova pasta", "rotulo-btn")); actions.append(folder);
        for (const [label, short, icon, kind] of [["Novo arquivo", "Arquivo", "novo-arquivo", "file"], ["Nova nota", "Nota", "copiar-texto", "note"]]) {
          const node = button("", () => createItem(kind), "acao tip-dir criar");
          node.setAttribute("aria-label", label); node.dataset.tip = label; node.dataset.curto = short;
          node.append(controlIcon(icon), el("span", label, "rotulo-btn")); actions.append(node);
        }
        const upload = el("input"); upload.type = "file"; upload.multiple = true; upload.hidden = true;
        upload.addEventListener("change", () => { const files = [...upload.files]; upload.value = ""; uploadFiles(files); });
        const add = actionButton("Adicionar arquivos do aparelho", "adicionar", () => upload.click()); add.classList.add("criar");
        actions.append(add, upload);
        const zip = actionButton("Criar ZIP dos selecionados", "tipo-compactado", createZip);
        zip.id = "zip-create"; zip.disabled = state.selectedForZip.size === 0; actions.append(zip);
        const trashSelected = actionButton("Mover para a lixeira", "lixeira", () => void trashSelection());
        trashSelected.id = "trash-selection"; actions.append(trashSelected);
      }
      const more = actionButton("Mais ações para os itens marcados", "mais", () => showSelectionMenu(more));
      more.id = "selection-more"; more.setAttribute("aria-haspopup", "menu"); more.setAttribute("aria-expanded", "false");
      actions.append(more);
      queueMicrotask(syncSelectionButtons);
      if (!canWrite(state.rootId)) {
        const note = el("span", "Somente leitura", "barra-somente-leitura");
        note.title = "Esta conta não pode criar nem alterar itens nesta pasta (permissão do Linux).";
        actions.append(note);
      }
      if (doc) {
        actions.append(doc.actionBar);
      }
    } else if (state.view === "trash") {
      actions.append(actionButton("Atualizar lixeira", "atualizar", loadTrash));
    }
    actions.append(infoButton);
  }

  // The panel describes the single checked item; otherwise, the open folder.
  function infoTarget() {
    if (state.view !== "files") return state.selectedItem;
    const [listRoot, parent] = listedFolder();
    if (state.selectedForZip.size === 1) {
      const [rootId, path] = [...state.selectedForZip][0].split("\u0000");
      const entry = knownEntry(rootId, path);
      if (entry) return {rootId, path, type: entry.type === "directory" ? "directory" : "file"};
    }
    return {rootId: listRoot, path: parent, type: "directory"};
  }
  function showInfoPanel() {
    const panel = $("#painel-info");
    if (panel.classList.contains("aberto")) {
      panel.classList.remove("aberto"); $("#info-choice")?.setAttribute("aria-pressed", "false"); return;
    }
    renderInfoPanel();
  }
  // While open, the panel follows the selection without needing to be closed and reopened.
  function refreshInfoPanel() {
    if (!$("#painel-info").classList.contains("aberto")) return;
    clearTimeout(state.infoTimer);
    state.infoTimer = setTimeout(renderInfoPanel, 120);
  }
  function renderInfoPanel() {
    const panel = $("#painel-info");
    const inner = $("#info-inner"); inner.replaceChildren();
    const doc = state.activeDocument || state.splitDocument;
    const selected = doc ? {rootId: doc.rootId, path: doc.path, type: "file"} : infoTarget();
    const heading = el("h3", selected?.path?.split("/").at(-1) || "/");
    const close = closeButton("Fechar informações", () => {
      panel.classList.remove("aberto"); $("#info-choice")?.setAttribute("aria-pressed", "false");
    }, "fechar");
    inner.append(heading, close);
    const field = (label, value) => {
      const row = el("div", undefined, "campo"); row.append(el("div", label, "rotulo"), el("div", value, "valor")); inner.append(row);
    };
    if (selected) {
      const listed = knownEntry(selected.rootId, selected.path);
      field("Caminho", absolutePath(selected.path));
      field("Tipo", selected.type === "directory" ? "Pasta" : "Arquivo");
      const writable = listed ? listed.writable === true : (selected.path === state.path ? state.listingWritable : null);
      field("Acesso", writable === null ? "Conforme as permissões da conta" : writable ? "Leitura e gravação" : "Somente leitura");
      if (selected.type === "file" && listed?.links > 1) field("Links", `${listed.links} nomes para o mesmo arquivo`);
      const dates = entry => {
        field("Criado", entry.createdAt ? formatFullDate(entry.createdAt) : "Não registrado pelo sistema de arquivos");
        field("Modificado", entry.modifiedAt ? formatFullDate(entry.modifiedAt) : "Indisponível");
      };
      if (listed && ("createdAt" in listed || "modifiedAt" in listed)) dates(listed);
      else if (selected.path) {
        // The open folder is not part of its own listing: its dates come from the parent folder's listing.
        const pending = el("div", undefined, "campo-datas"); inner.append(pending);
        const name = selected.path.split("/").pop();
        void request("api/list", {}, {rootId: selected.rootId, path: parentOf(selected.path)}).then(result => {
          const entry = result.entries.find(item => item.name === name);
          if (!entry || !pending.isConnected) return;
          const before = inner.lastChild; dates(entry);
          const added = [];
          while (inner.lastChild !== before && inner.lastChild) added.unshift(inner.removeChild(inner.lastChild));
          pending.replaceWith(...added);
        }).catch(() => {});
      }
      if (selected.type === "file") {
        field("Tamanho", Number.isSafeInteger(listed?.size) ? formatSize(listed.size) : "Não informado pela listagem atual.");
      } else {
        const cached = state.treeEntries.get(treeBranchKey(selected.rootId, selected.path));
        if (cached) field("Itens", formatCount(cached.length));
        const size = el("div", undefined, "campo");
        size.append(el("div", "Tamanho", "rotulo"));
        const value = el("div", undefined, "valor");
        const sizeText = result => `${formatSize(result.totalBytes)}${result.complete === false ? "+" : ""} · ${folderCounts(result)}${result.complete === false ? " · parcial" : ""}`;
        const measured = state.dirSizes.get(`${selected.rootId}\u0000${selected.path}`);
        const calculate = button("Calcular", async () => {
          calculate.disabled = true; value.textContent = "Calculando…";
          try {
            const result = await request("api/dir-size", {}, {rootId: selected.rootId, path: selected.path});
            value.textContent = sizeText(result);
          } catch (error) {
            value.textContent = error.status === 413 ? "O cálculo excedeu o limite; nenhum total parcial foi apresentado." : "Não foi possível calcular o tamanho.";
          } finally { calculate.disabled = false; }
        }, "mini");
        if (measured && typeof measured === "object") value.textContent = sizeText(measured); else value.append(calculate);
        size.append(value); inner.append(size);
        if (state.tagFolders) inner.append(tagFolderField(selected.path));
      }
      if (doc) {
        field("Estado", dirtyDocument(doc) ? "Não salvo" : "Salvo");
        field("Caracteres", String(Array.from(doc.getValue()).length));
        field("Linhas", String(doc.getValue().split(/\r\n|\r|\n/).length));
      }
    } else {
      field("Caminho", absolutePath(state.path));
    }
    panel.classList.add("aberto");
    $("#info-choice")?.setAttribute("aria-pressed", "true");
  }

  // Touch screens have no right-click: the folder's Info panel offers the same watched-folder option as the context menu.
  function tagFolderField(path) {
    const row = el("div", undefined, "campo");
    const value = el("div", undefined, "valor");
    const covering = isTagFolder(path) ? null : tagFolderCovering(path);
    const gap = tagFolderGap(path);
    const text = isTagFolder(path) ? "Monitoradas nesta pasta."
      : covering ? `Monitoradas por ${covering}.`
      : gap?.kind === "sem permissão de leitura" ? "Não monitoradas: a varredura não passa por uma pasta sem permissão de leitura acima desta. Marque esta pasta para lê-la."
      : gap?.inside ? `Não monitoradas: fica dentro de uma pasta ${gap.kind}, que só entra se for marcada.`
      : gap ? `Não monitoradas: pasta ${gap.kind} só entra se for marcada.` : "Não monitoradas.";
    value.append(el("span", text));
    if (isTagFolder(path)) value.append(el("br"), button("Parar de monitorar tags", () => void toggleTagFolder(path, false), "mini"));
    else if (!covering) value.append(el("br"), button("Monitorar tags nesta pasta", () => void toggleTagFolder(path, true), "mini"));
    row.append(el("div", "Tags", "rotulo"), value);
    return row;
  }

  function loadTrash({quiet = false} = {}) {
    const requestId = ++state.listRequestId;
    if (!quiet) status("Carregando lixeira…");
    const pending = (async () => {
      try {
        const result = await request("api/trash");
        if (requestId !== state.listRequestId || state.view !== "trash") return {status: "stale", requestId};
        state.trashEntries = result.entries || [];
        renderTrash(state.trashEntries);
        if (!quiet) status(`${state.trashEntries.length} ${state.trashEntries.length === 1 ? "item" : "itens"} na lixeira.`);
        return {status: "loaded", requestId};
      } catch (error) {
        if (requestId !== state.listRequestId || state.view !== "trash") return {status: "stale", requestId};
        renderResults([], "A lixeira não está disponível no momento.");
        if (!quiet) status(error.status === 401 ? "Sua sessão expirou. Entre novamente." : "Não foi possível carregar a lixeira.", true);
        return {status: "failed", requestId};
      }
    })();
    state.trashLoad = {requestId, pending};
    return pending;
  }

  async function currentTrashLoad(outcome) {
    while (outcome.status === "stale") {
      const latest = state.trashLoad;
      if (!latest || latest.requestId <= outcome.requestId) return outcome;
      outcome = await latest.pending;
    }
    return outcome;
  }

  function renderTrash(entries) {
    const panel = $("#results"); panel.replaceChildren();
    if (!entries.length) panel.append(emptyState("A lixeira está vazia", "lixeira"));
    for (const entry of entries) {
      const card = el("div", undefined, "res-item trash-item");
      if (entry.quarantined) {
        card.append(iconNode("file"), el("div", undefined, "res-corpo"));
        card.lastElementChild.append(el("strong", "Item em quarentena", "res-nome"), el("div", entry.legacy
          ? "Metadados da versão anterior que não puderam ser convertidos; o item fica guardado e não pode ser restaurado automaticamente."
          : "Não foi possível conferir os dados guardados deste item; ele não pode ser restaurado.", "res-cam"));
        panel.append(card); continue;
      }
      const title = entry.sourcePath.split("/").pop() || "/";
      const icon = iconNode(entry.kind === "directory" ? "folder" : "file", entry.sourcePath);
      const body = el("div", undefined, "res-corpo");
      // User-facing line (in Portuguese): origin, type, size, reason, local date, and days left in the 30-day retention.
      const reason = {user: "movido por você", image_collection: "imagem retirada da nota", move: "substituído ao mover"}[entry.reason] || entry.reason;
      const deleted = parseStamp(entry.deletedAt);
      const daysLeft = deleted ? Math.max(0, Math.ceil((deleted.getTime() + 30 * 86400000 - Date.now()) / 86400000)) : null;
      const line = el("div", `Em ${absolutePath(parentOf(entry.sourcePath))} · ${entry.kind === "directory" ? "Pasta" : "Arquivo"} · ${formatSize(entry.size)} · ${reason} · ${formatDate(entry.deletedAt) || entry.deletedAt}`, "res-cam");
      if (deleted) line.title = `Na lixeira desde ${formatFullDate(entry.deletedAt)}`;
      if (daysLeft !== null) {
        const due = el("span", daysLeft === 0 ? "sai hoje" : `sai em ${daysLeft} ${daysLeft === 1 ? "dia" : "dias"}`, "lx-prazo");
        due.title = "Depois de 30 dias na lixeira, o item pode ser apagado de vez.";
        line.append(" ", due);
      }
      body.append(el("strong", title, "res-nome"), line);
      card.append(icon, body);
      const actions = el("div", undefined, "trash-actions");
      const relatedArea = el("div", undefined, "restore-related");
      const note = entry.kind === "file" && /\.(md|markdown)$/i.test(entry.sourcePath);
      let restoreOnly = button("Restaurar", () => restoreTrashEntry(entry.id, [], card, entry), "primary-button");
      restoreOnly.disabled = !entry.restoreAvailable || entry.recovery !== "ready";
      actions.append(restoreOnly);
      if (note && entry.restoreAvailable && entry.recovery === "ready") {
        const relatedButton = button("Imagens removidas…", async () => {
          relatedButton.disabled = true;
          relatedArea.replaceChildren(el("p", "Verificando referências salvas…", "muted"));
          try {
            const relation = await request("api/trash/related-images", {}, {id: entry.id});
            relatedArea.replaceChildren();
            if (!relation.complete) {
              relatedArea.append(el("p", "Não foi possível verificar todas as referências. A restauração conjunta não está disponível.", "document-status error"));
              return;
            }
            if (!relation.images.length) {
              relatedArea.append(el("p", "Nenhuma imagem coletada correspondente foi encontrada.", "muted"));
              return;
            }
            const selected = [];
            relation.images.forEach((group, index) => {
              const field = el("fieldset", undefined, "restore-image-group");
              field.append(el("legend", absolutePath(group.path)));
              if (group.candidates.length === 1) {
                const candidate = group.candidates[0];
                const label = el("label", undefined, "restore-image-choice");
                const checkbox = el("input"); checkbox.type = "checkbox"; checkbox.value = candidate.id;
                label.append(checkbox, document.createTextNode(` Incluir imagem (${formatSize(candidate.size)}, removida ${candidate.deletedAt})`));
                field.append(label);
              } else {
                field.append(el("p", "Há mais de uma versão nesta localização; escolha no máximo uma."));
                for (const candidate of group.candidates) {
                  const label = el("label", undefined, "restore-image-choice");
                  const radio = el("input"); radio.type = "radio"; radio.name = `restore-${entry.id}-${index}`; radio.value = candidate.id;
                  label.append(radio, document.createTextNode(` Versão ${candidate.deletedAt} (${formatSize(candidate.size)})`));
                  field.append(label);
                }
              }
              relatedArea.append(field);
            });
            const selectedIds = () => [...relatedArea.querySelectorAll("input:checked")].map((input) => input.value);
            const restoreTogether = button("Restaurar nota e imagens selecionadas", () => restoreTrashEntry(entry.id, selectedIds(), card), "primary-button");
            restoreTogether.disabled = true;
            relatedArea.addEventListener("change", () => { restoreTogether.disabled = selectedIds().length === 0; });
            relatedArea.append(restoreTogether);
          } catch (_error) {
            relatedArea.replaceChildren(el("p", "Não foi possível verificar as imagens associadas.", "document-status error"));
          } finally {
            relatedButton.disabled = false;
          }
        }, "secondary-button");
        actions.append(relatedButton);
      }
      card.append(actions);
      panel.append(card, relatedArea);
    }
  }

  async function restoreTrashEntry(identifier, imageIds, card, entry = null, alternativePath = null) {
    const buttons = card.querySelectorAll("button");
    buttons.forEach((node) => { node.disabled = true; });
    status(imageIds.length ? "Restaurando a nota e as imagens selecionadas…" : "Restaurando…");
    try {
      const payload = {action: "restore", id: identifier};
      if (imageIds.length) payload.relatedImageIds = imageIds;
      if (alternativePath !== null) payload.alternativePath = alternativePath;
      await request("api/trash", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: JSON.stringify(payload),
      });
      const message = imageIds.length ? "Nota e imagens selecionadas restauradas." : "Item restaurado.";
      toast(message);
      // The server returns favorites and labels for the restored item and advances the state's version
      // counter: without re-reading it, the next save would wrongly detect a change from another tab.
      void refreshUiFromServer(); void refreshExpandedTreeBranches();
      const refreshed = await currentTrashLoad(await loadTrash({quiet: true}));
      const refreshFailed = refreshed.status === "failed";
      status(refreshFailed ? `${message} A lixeira não pôde ser atualizada.` : message, refreshFailed);
    } catch (error) {
      // HF-TRASH-004: a taken name or a missing source folder requires an explicit destination.
      const code = error.body?.error;
      if (error.status === 404) {
        const refreshed = await currentTrashLoad(await loadTrash({quiet: true}));
        if (refreshed.status === "loaded" && !state.trashEntries.some(item => item.id === identifier)) {
          status("Este item já saiu da lixeira (restaurado ou removido em outra aba).", true);
          return;
        }
      }
      const needsDestination = entry && !imageIds.length && (
        (error.status === 409 && ["destination_occupied", "source_unavailable"].includes(code)) || [403, 404].includes(error.status));
      if (needsDestination) {
        buttons.forEach((node) => { node.disabled = false; });
        const original = alternativePath ?? entry.sourcePath;
        const name = original.split("/").pop();
        const occupied = error.status === 409 && code === "destination_occupied";
        const target = await askDestination({
          title: `Restaurar “${name}”`, submitLabel: "Restaurar aqui", folder: parentOf(original), name, fallbackUp: true,
          message: occupied ? `Já existe “${name}” em ${absolutePath(parentOf(original))}. Escolha outro nome ou outra pasta.`
            : "A pasta de origem não existe mais ou não aceita gravação. Escolha onde restaurar.",
        });
        if (target) return restoreTrashEntry(identifier, imageIds, card, entry, target.path);
        status("Restauração cancelada; o item continua na lixeira.");
        return;
      }
      const partial = error.body?.restored;
      let message;
      if (Array.isArray(partial)) {
        const paths = partial.map((item) => absolutePath(item.path)).join("; ");
        message = `Restauração parcial; itens já restaurados: ${paths}. Os demais continuam na lixeira.`;
      } else if (error.status === 409 && ["destination_occupied", "metadata_conflict", "partial_restore"].includes(error.body?.error)) {
        message = "O destino ou as marcações do item (favorito, labels) entraram em conflito. Nada foi substituído; atualize a lixeira e tente de novo.";
      } else {
        message = "A restauração não foi concluída. Os itens restantes continuam na lixeira.";
      }
      const refreshed = await currentTrashLoad(await loadTrash({quiet: true}));
      status(refreshed.status === "failed" ? `${message} A lixeira não pôde ser atualizada.` : message, true);
    } finally {
      buttons.forEach((node) => { node.disabled = false; });
    }
  }

  function documentUrl(name, rootId, path) {
    const url = new URL(base + name, window.location.origin);
    url.searchParams.set("rootId", rootId); url.searchParams.set("path", path);
    return url;
  }
  async function documentRequest(name, rootId, path, options = {}) {
    const response = await fetch(documentUrl(name, rootId, path), {
      credentials: "same-origin", cache: "no-store", ...options,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
    let body = null;
    try { body = await response.json(); } catch (_error) { /* a status is enough */ }
    if (!response.ok) {
      if (response.status === 401) sessionEnded({remind: true});
      const error = new Error(body?.error || "Não foi possível abrir o arquivo."); error.status = response.status; error.body = body; throw error;
    }
    return body;
  }
  function extension(path) { return path.split("/").pop().split(".").pop().toLocaleLowerCase(); }
  function downloadText(name, text) { window.HFEditorRuntime.saveBlob(name, text); }
  function markdownTitle(content, filename) {
    let offset = 0;
    const nextLine = () => {
      const end = content.indexOf("\n", offset);
      if (end < 0) { const line = content.slice(offset).replace(/\r$/, ""); offset = content.length; return line; }
      const line = content.slice(offset, end).replace(/\r$/, ""); offset = end + 1; return line;
    };
    if (nextLine().trim() === "---") {
      let line;
      do { if (offset >= content.length) return filename; line = nextLine(); } while (line.trim() !== "---");
    } else offset = 0;
    let fence = null;
    while (offset < content.length) {
      const line = nextLine();
      const marker = /^\s{0,3}(`{3,}|~{3,})/.exec(line);
      if (marker) { if (!fence) fence = marker[1]; else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = null; continue; }
      if (fence) continue;
      const heading = /^\s{0,3}#\s+(.+?)\s*#*\s*$/.exec(line);
      if (heading) return heading[1].replace(/\*\*|__|~~|==|[*_`]/g, "").trim() || filename;
    }
    return filename;
  }
  function dirtyDocument(doc) { return Boolean(doc.dirty); }
  function docStatus(doc, text, isError = false) {
    status(text, isError);
    const saveError = isError && /^(Não foi possível salvar|Conflito|Não foi possível confirmar o salvamento)/.test(text);
    if (doc.errorNode && (saveError || !isError)) {
      doc.errorNode.hidden = !saveError; doc.errorNode.textContent = saveError ? (text.startsWith("Conflito") ? "Conflito ao salvar" : "Não foi possível salvar") : ""; doc.errorNode.title = saveError ? text : "";
    }
    const dirty = paintEditState(doc);
    doc.saveButton.disabled = doc.saving || !dirty;
    renderTabs();
  }
  // Editor bar state: "Salvo" / "• Não salvo" only for editable files (HF-NAV-007); in Formatted
  // and Clean views, the tooltip points to Markdown, where editing happens (HF-NAV-008).
  function paintEditState(doc) {
    const dirty = dirtyDocument(doc);
    doc.dirtyNode.textContent = dirty ? "• Não salvo" : "Salvo";
    doc.dirtyNode.classList.toggle("sujo", dirty);
    doc.dirtyNode.hidden = !doc.editable;
    if (doc.editHintNode) doc.editHintNode.hidden = !(doc.isMarkdown && doc.editable && doc.mode !== "markdown");
    return dirty;
  }
  function updateNoteHeader(doc, {now = false} = {}) {
    if (!doc.noteMeta) return;
    clearTimeout(doc.headerTimer);
    if (!now) { doc.headerTimer = setTimeout(() => updateNoteHeader(doc, {now: true}), 400); return; }
    const value = doc.getValue();
    const chars = Array.from(value).length;
    const words = (value.match(/\S+/gu) || []).length;
    const lines = value.length ? value.split(/\r\n|\r|\n/).length : 0;
    doc.noteMeta.replaceChildren(
      el("span", doc.name, "nota-nome"),
      el("span", `${formatCount(chars)} ${chars === 1 ? "caractere" : "caracteres"}`),
      el("span", `${formatCount(words)} ${words === 1 ? "palavra" : "palavras"}`),
      el("span", `${formatCount(lines)} ${lines === 1 ? "linha" : "linhas"}`),
    );
  }
  function renderCodeViewer(target, value, wrap = false) {
    target.replaceChildren();
    target.classList.toggle("wrap", Boolean(wrap));
    target.classList.add("viewer-codigo");
    const fragment = document.createDocumentFragment();
    const lines = value.split(/\r\n|\r|\n/);
    lines.forEach((line, index) => {
      const row = el("div", undefined, "ln");
      row.append(el("span", String(index + 1), "n"), el("span", line, "t"));
      fragment.append(row);
    });
    target.append(fragment);
  }
  function createDocumentShell(rootId, path, loaded) {
    const isMarkdown = ["md", "markdown"].includes(extension(path));
    const name = path.split("/").pop();
    const doc = {
      rootId, path, name, isMarkdown, baseline: loaded.content, version: loaded.version,
      counter: 0, baselineCounter: 0, saving: false, saveAgain: false, savePromise: null, serverVersion: null,
      // HF-NAV-008: a .md file opens in Formatted view; non-Markdown text has only a single view.
      mode: isMarkdown ? "formatted" : "text", source: null, dirty: false,
      host: el("article", undefined, "document-shell"), wrap: false,
      editable: loaded.editable !== false, readOnlyReason: loaded.readOnlyReason || null,
    };
    const noteHeader = el("header", undefined, "nota-cab");
    const pathLine = el("div", undefined, "nota-caminho");
    // LRM marks: with direction rtl (truncation at the start), the path keeps its normal left-to-right order.
    doc.pathNode = el("span", `\u200e${absolutePath(path)}\u200e`, "nota-caminho-txt");
    doc.pathNode.title = absolutePath(path);
    // tip-dir: the tooltip grows to the left; centered, it would overflow the right edge and add
    // unwanted horizontal scroll to the document area.
    const copyPath = button("", () => void copyPaths([doc.path]), "nota-copiar-caminho tip-dir");
    copyPath.dataset.tip = "Copiar caminho"; copyPath.setAttribute("aria-label", "Copiar caminho"); copyPath.append(controlIcon("copiar-caminho"));
    pathLine.append(doc.pathNode, copyPath); noteHeader.append(pathLine);
    const meta = el("div", undefined, "nota-meta");
    doc.noteMeta = el("span", undefined, "nota-meta-resto"); meta.append(doc.noteMeta); noteHeader.append(meta);
    doc.updateHeader = () => {
      const currentPath = absolutePath(doc.path);
      doc.pathNode.textContent = `\u200e${currentPath}\u200e`; doc.pathNode.title = currentPath;
      if (doc.nameNode) doc.nameNode.textContent = doc.path.split("/").pop() || doc.name;
      updateNoteHeader(doc);
    };

    const actionBar = el("div", undefined, "document-actions");
    doc.saveButton = actionButton("Salvar alterações", "salvar", () => saveDocument(doc));
    doc.saveButton.classList.add("salvar-documento");
    // Editor bar: name in body-strong style and "Salvo" / "• Não salvo" state.
    const editorBar = el("div", undefined, "editor-barra");
    doc.nameNode = el("span", name, "ed-nome");
    doc.dirtyNode = el("span", "Salvo", "ed-estado"); doc.dirtyNode.setAttribute("role", "status");
    // HF-NAV-008: Formatted and Clean are read-only; the tooltip points to Markdown, where editing happens.
    doc.editHintNode = el("span", "· para editar, use ", "ed-dica");
    doc.editHintNode.append(button("Markdown", () => {
      // The cursor is placed at the read position, not at the very start, to avoid overtyping the title.
      switchDocumentMode(doc, "markdown", {cursor: true}); doc.source?.view?.focus();
    }, "ed-dica-link"));
    doc.editHintNode.hidden = true;
    doc.errorNode = el("span", "", "ed-erro"); doc.errorNode.hidden = true; doc.errorNode.setAttribute("role", "alert");
    editorBar.append(doc.nameNode, doc.dirtyNode, doc.errorNode, doc.editHintNode);
    // HF-NAV-007: a non-editable file shows "Somente leitura" with the reason in the tooltip.
    doc.readOnlyNode = el("span", "Somente leitura", "ed-somente-leitura");
    doc.readOnlyNode.hidden = doc.editable; doc.readOnlyNode.title = readOnlyReasonText(doc.readOnlyReason);
    doc.readOnlyNode.setAttribute("aria-label", `Somente leitura: ${readOnlyReasonText(doc.readOnlyReason)}`);
    editorBar.append(doc.readOnlyNode);
    const download = actionButton("Baixar arquivo", "baixar", () => downloadText(name, doc.getValue()));
    const copy = actionButton("Copiar texto", "copiar-texto", async () => {
      if (doc.mode === "clean") { await doc.copyClean(); return; }
      try { await navigator.clipboard.writeText(doc.getValue()); toast("Texto copiado"); }
      catch (_error) { docStatus(doc, "Não foi possível copiar; selecione o texto e copie.", true); }
    });
    actionBar.append(doc.saveButton, download, copy);

    // A single segmented control switches between three views of the same buffer (HF-NAV-008).
    const modeBar = el("div", undefined, "document-modes");
    doc.readForms = el("div", undefined, "segmentado seg-formas"); doc.readForms.id = "seg-md";
    doc.readForms.setAttribute("role", "group"); doc.readForms.setAttribute("aria-label", "Vista do documento");
    modeBar.append(doc.readForms);

    const surface = el("div", undefined, "document-surface");
    doc.statusNode = $("#app-status");
    doc.sourcePane = el("div", undefined, "document-source");
    doc.previewPane = el("div", undefined, "markdown-preview"); doc.previewPane.setAttribute("role", "region");
    doc.previewPane.addEventListener("keydown", event => {
      const tag = event.target.closest?.("mark[data-tag]");
      if (tag && ["Enter", " "].includes(event.key)) { event.preventDefault(); enterView("tags", {tag: tag.dataset.tag}); }
    });
    doc.previewPane.addEventListener("click", event => {
      const tag = event.target.closest("mark[data-tag]");
      if (tag) {
        event.preventDefault();
        enterView("tags", {tag: tag.dataset.tag});
        return;
      }
      const link = event.target.closest("a[href]");
      if (!link) return;
      event.preventDefault();
      window.open(link.href, "_blank", "noopener,noreferrer");
    });
    doc.rawPane = el("div", undefined, "document-raw viewer-codigo"); doc.rawPane.setAttribute("role", "region");
    doc.cleanPane = el("pre", undefined, "document-clean viewer-limpo");
    doc.attachmentsPane = el("section", undefined, "attachments-panel"); doc.attachmentsPane.hidden = true;
    doc.modes = {};
    const modes = isMarkdown ? [["formatted", "Formatado"], ["markdown", "Markdown"], ["clean", "Limpo"]] : [];
    for (const [key, label] of modes) {
      const node = button(label, () => switchDocumentMode(doc, key), "modo-forma");
      node.id = ({formatted: "seg-visual", markdown: "seg-cru", clean: "seg-limpo"})[key];
      doc.modes[key] = node; doc.readForms.append(node);
    }

    // Formatting toolbar: 24px .lp-btn tools (40px on touch), separated by group; undo and redo
    // use the shared icons.
    const commandBar = el("div", undefined, "lp-paleta");
    commandBar.setAttribute("role", "toolbar"); commandBar.setAttribute("aria-label", "Formatação");
    if (isMarkdown) {
      // Headings support three levels. The three list types use native Markdown markers, each with its
      // own bullet in Formatted view: - filled circle, * hollow circle, + square. The button shows the bullet.
      const groups = [
        [["heading1", "H1", "Título 1"], ["heading2", "H2", "Título 2"], ["heading3", "H3", "Título 3"]],
        [["bold", "B", "Negrito", "b"], ["italic", "I", "Itálico", "i"],
          ["strike", "S", "Riscado", "s"], ["highlight", "M", "Marca", "mark"], ["code", "</>", "Código"],
          ["link", "↗", "Link"], ["image", "▤", "Imagem"]],
        [["bulletDash", "cheio", "Lista com ponto cheio (-)", "ponto"], ["bulletStar", "vazado", "Lista com ponto vazado (*)", "ponto"],
          ["bulletPlus", "quadrado", "Lista com ponto quadrado (+)", "ponto"], ["tab", "⇥", "Indentar"], ["outdent", "⇤", "Recuar"]],
        [["undo", "desfazer", "Desfazer", "icon"], ["redo", "refazer", "Refazer", "icon"]],
      ];
      groups.forEach((group, index) => {
        if (index) commandBar.append(el("span", undefined, "lp-sep"));
        for (const [command, glyph, label, style] of group) {
          const control = button("", () => { if (state.logoutPhase === "idle") doc.source.command(command); }, "lp-btn");
          control.setAttribute("aria-label", label); control.dataset.tip = label; control.dataset.command = command;
          if (style === "icon") control.append(controlIcon(glyph));
          else if (style === "ponto") control.append(el("span", undefined, `lp-ico-ponto ${glyph}`));
          else {
            const text = el("span", undefined, style === "mark" ? "lp-i lp-i-mark" : "lp-i");
            text.append(["b", "i", "s"].includes(style) ? el(style, glyph) : document.createTextNode(glyph));
            control.append(text);
          }
          commandBar.append(control);
        }
      });
      doc.imageInput = el("input"); doc.imageInput.type = "file";
      doc.imageInput.accept = "image/png,image/jpeg,image/gif,image/webp"; doc.imageInput.multiple = true; doc.imageInput.hidden = true;
      doc.imageInput.addEventListener("change", async () => { const files = [...doc.imageInput.files]; doc.imageInput.value = ""; await uploadManagedImages(doc, files); });
      const tool = (label, icon, action) => {
        const control = button("", action, "lp-btn");
        control.setAttribute("aria-label", label); control.dataset.tip = label; control.append(controlIcon(icon));
        return control;
      };
      doc.imageButton = tool("Anexar imagem", "adicionar", () => doc.imageInput.click());
      doc.galleryButton = tool("Galeria de imagens", "tipo-imagem", async () => {
        doc.attachmentsPane.hidden = !doc.attachmentsPane.hidden;
        if (!doc.attachmentsPane.hidden) await refreshAttachmentGallery(doc);
      });
      commandBar.append(el("span", undefined, "lp-sep"), doc.imageButton, doc.galleryButton, doc.imageInput);
    }
    doc.digitar = button("Digitar", () => {
      if (doc.source?.setTypingEnabled) {
        const enabled = !doc.source.isTypingEnabled(); doc.source.setTypingEnabled(enabled);
      }
    }, "lp-btn digitar-button");
    doc.digitar.setAttribute("aria-pressed", "false"); doc.digitar.setAttribute("aria-label", "Permitir digitação");
    doc.digitar.addEventListener("pointerdown", event => event.preventDefault());
    doc.digitar.addEventListener("mousedown", event => event.preventDefault());
    doc.commandBar = commandBar;

    if (isMarkdown) {
      // The .lp-doc styles for headings, code, tables, and list markers apply only to the editor
      // host.
      doc.sourcePane.classList.add("lp-doc");
      doc.source = window.HFEditorRuntime.createMarkdownEditor({
        parent: doc.sourcePane, initial: loaded.content, nonce: app.dataset.styleNonce,
        onActiveFormats: active => {
          for (const node of commandBar.querySelectorAll(".lp-btn[data-command]")) {
            const on = active.has(node.dataset.command);
            node.classList.toggle("ativo", on); node.setAttribute("aria-pressed", String(on));
          }
        },
        onTypingChange: enabled => {
          doc.digitar.setAttribute("aria-pressed", String(enabled));
          doc.digitar.textContent = enabled ? "Bloquear teclado" : "Digitar";
          doc.digitar.setAttribute("aria-label", enabled ? "Bloquear teclado" : "Permitir digitação");
        },
        onChange: () => { doc.dirty = doc.source.isDirty(); docStatus(doc, "Há alterações não salvas."); doc.updateHeader(); },
        onSave: () => saveDocument(doc),
      });
      doc.source.view.contentDOM.addEventListener("paste", event => {
        const files = [...(event.clipboardData?.items || [])].filter(item => item.kind === "file").map(item => item.getAsFile()).filter(file => file && file.type.startsWith("image/"));
        if (!files.length) return; event.preventDefault(); event.stopImmediatePropagation(); void uploadManagedImages(doc, files);
      }, true);
      doc.source.view.contentDOM.addEventListener("dragover", event => { if ([...(event.dataTransfer?.types || [])].includes("Files")) event.preventDefault(); });
      doc.source.view.contentDOM.addEventListener("drop", event => {
        const files = [...(event.dataTransfer?.files || [])].filter(file => file.type.startsWith("image/"));
        if (!files.length) return; event.preventDefault(); event.stopImmediatePropagation(); void uploadManagedImages(doc, files);
      }, true);
    } else {
      doc.source = window.HFEditorRuntime.createPlainEditor({
        parent: doc.sourcePane, initial: loaded.content, onSave: () => saveDocument(doc),
        onTypingChange: enabled => { doc.digitar.setAttribute("aria-pressed", String(enabled)); doc.digitar.textContent = enabled ? "Bloquear teclado" : "Digitar"; },
        onChange: () => { doc.dirty = doc.source.isDirty(); docStatus(doc, "Há alterações não salvas."); doc.updateHeader(); },
      });
    }
    if (doc.source.coarse) commandBar.append(doc.digitar);
    doc.previewPane.setAttribute("aria-label", "Prévia formatada");
    doc.modeBar = isMarkdown ? modeBar : null; doc.actionBar = actionBar;
    if (!doc.editable) { doc.saveButton.hidden = true; commandBar.replaceChildren(); }
    doc.editorBar = editorBar;
    doc.host.append(editorBar, commandBar, noteHeader, surface);
    surface.append(doc.sourcePane, doc.previewPane, doc.rawPane, doc.cleanPane, doc.attachmentsPane);
    doc.getValue = () => doc.source.getValue();
    doc.destroy = () => doc.source.destroy?.();
    doc.copyClean = async () => {
      const clean = window.HFEditorRuntime.cleanCopy(doc.getValue(), isMarkdown);
      try { await navigator.clipboard.writeText(clean); toast("Texto limpo copiado"); }
      catch (_error) { docStatus(doc, "Não foi possível copiar; selecione o texto Limpo.", true); }
    };
    doc.downloadButton = download; doc.copyCleanButton = copy;
    updateNoteHeader(doc, {now: true}); updateDocumentView(doc); docStatus(doc, "Arquivo carregado.");
    return doc;
  }
  const READ_ONLY_REASONS = {
    file_not_writable: "A conta não tem permissão de gravação neste arquivo.",
    directory_not_writable: "A conta não tem permissão de gravação na pasta do arquivo.",
    not_owner: "O arquivo pertence a outra conta.",
    group_not_member: "A conta não pertence ao grupo do arquivo.",
    hard_link: "O arquivo tem mais de um nome (link rígido).",
    set_id: "O arquivo tem bit set-user-ID ou set-group-ID.",
    extended_attributes: "O arquivo tem atributos estendidos não suportados.",
    not_regular: "Não é um arquivo regular.",
  };
  function readOnlyReasonText(reason) { return READ_ONLY_REASONS[reason] || "O arquivo não pode ser editado por esta conta."; }
  function showDocumentModeControls(doc) {
    const slot = $("#document-mode-slot");
    if (doc?.modeBar && slot.childNodes.length === 1 && slot.firstChild === doc.modeBar) { slot.hidden = false; return; }
    slot.replaceChildren();
    if (!doc?.modeBar) { slot.hidden = true; return; }
    slot.append(doc.modeBar);
    slot.hidden = false;
  }
  function updateDocumentView(doc) {
    const modeTips = {
      formatted: "Formatado: só leitura",
      markdown: doc.editable ? "Markdown: edição" : "Markdown: só leitura",
      clean: "Limpo: texto sem marcação, só leitura",
    };
    for (const [key, node] of Object.entries(doc.modes)) {
      node.setAttribute("aria-pressed", String(doc.mode === key)); node.classList.toggle("ativo", doc.mode === key);
      node.title = modeTips[key];
    }
    // Only the Markdown view (or the single text view) shows the editor and accepts typing.
    const sourceView = doc.mode === "markdown" || doc.mode === "text";
    doc.sourcePane.hidden = !sourceView;
    doc.previewPane.hidden = doc.mode !== "formatted";
    doc.rawPane.hidden = true;
    doc.cleanPane.hidden = doc.mode !== "clean";
    doc.commandBar.hidden = !sourceView || !doc.editable || !doc.commandBar.querySelector(".lp-btn");
    doc.host.classList.toggle("lp-wrap", sourceView);
    const value = doc.getValue();
    if (doc.mode === "formatted") {
      doc.previewPane.replaceChildren();
      const viewer = el("div", undefined, "viewer-md");
      window.HFEditorRuntime.renderPreviewHtml(value, viewer, {
        resolveImage: reference => request("api/images/resolve", {}, {
          rootId: doc.rootId, path: doc.path, reference,
        }),
        previewUrl: identity => endpoint("api/preview", {
          rootId: identity.rootId, path: identity.path,
        }).href,
      });
      for (const tag of viewer.querySelectorAll("mark[data-tag]")) { tag.tabIndex = 0; tag.setAttribute("role", "link"); }
      doc.previewPane.append(viewer);
    } else if (doc.mode === "clean") doc.cleanPane.textContent = window.HFEditorRuntime.cleanCopy(value, true);
    const dirty = paintEditState(doc);
    doc.saveButton.disabled = doc.saving || !dirty || !doc.editable;
    doc.updateHeader();
    if (state.activeDocument === doc || state.splitDocument === doc) renderToolbar();
  }
  function documentScroller(doc, mode = doc.mode) {
    if (mode === "formatted") return doc.previewPane;
    if (mode === "clean") return doc.cleanPane.closest(".document-surface") || doc.cleanPane;
    return doc.source?.view?.scrollDOM || doc.sourcePane;
  }
  // Switching views keeps the line that was at the top: Formatted view tags each block with its
  // source line (data-linha), the editor reports and reveals the top line; Clean view uses proportional scroll.
  function sourceLineInView(doc) {
    if (doc.mode === "markdown" && doc.source?.topLine) return doc.source.topLine();
    const pane = documentScroller(doc);
    if (!pane) return 1;
    if (doc.mode === "formatted") {
      const top = pane.getBoundingClientRect().top;
      let line = 1;
      for (const node of pane.querySelectorAll("[data-linha]")) {
        const rect = node.getBoundingClientRect();
        line = Number(node.dataset.linha) || line;
        if (rect.bottom > top + 4) break;
      }
      return line;
    }
    const total = Math.max(1, doc.getValue().split("\n").length);
    const range = pane.scrollHeight - pane.clientHeight;
    return range > 0 ? 1 + Math.round(pane.scrollTop / range * (total - 1)) : 1;
  }
  function revealSourceLine(doc, line, {cursor = false} = {}) {
    if (doc.mode === "markdown" && doc.source?.revealLine) { doc.source.revealLine(line, {cursor}); return; }
    const pane = documentScroller(doc);
    if (!pane) return;
    if (doc.mode === "formatted") {
      let target = null;
      for (const node of pane.querySelectorAll("[data-linha]")) { if (Number(node.dataset.linha) <= line) target = node; else break; }
      pane.scrollTop = target ? pane.scrollTop + target.getBoundingClientRect().top - pane.getBoundingClientRect().top - 8 : 0;
      return;
    }
    const total = Math.max(1, doc.getValue().split("\n").length);
    pane.scrollTop = (line - 1) / Math.max(1, total - 1) * Math.max(0, pane.scrollHeight - pane.clientHeight);
  }
  function switchDocumentMode(doc, mode, {cursor = false} = {}) {
    const line = sourceLineInView(doc);
    doc.mode = mode; updateDocumentView(doc);
    requestAnimationFrame(() => requestAnimationFrame(() => revealSourceLine(doc, line, {cursor})));
  }
  // The browser's Back and Forward move through folders and documents visited within the app.
  function historyEntry(entry = {}) {
    return {hf: 1, view: entry.view || "files", path: entry.path ?? state.path, doc: entry.doc || null,
      tag: entry.tag || "", label: entry.label || "", tab: activeTabIndex()};
  }
  function sameHistory(left, right) {
    return (left.view || "files") === right.view && left.path === right.path && (left.doc || null) === right.doc &&
      (left.tag || "") === right.tag && (left.label || "") === right.label;
  }
  function recordHistory(entry) {
    const next = historyEntry(entry);
    const current = history.state;
    const target = state.historyTarget;
    if (target && sameHistory(target, next)) { state.historyTarget = null; history.replaceState({...next, seq: state.historySeq}, ""); return; }
    state.historyTarget = null;
    if (!current?.hf) { state.historySeq = 0; history.replaceState({...next, seq: 0}, ""); return; }
    if (sameHistory(current, next)) { if (current.tab !== next.tab) history.replaceState({...next, seq: state.historySeq}, ""); return; }
    state.historySeq = (current.seq ?? state.historySeq) + 1;
    history.pushState({...next, seq: state.historySeq}, "");
  }
  function showDocument(doc, {expandTree = true} = {}) {
    status(`Documento aberto: ${absolutePath(doc.path)}`);
    recordHistory({path: parentOf(doc.path), doc: `${doc.rootId}\u0000${doc.path}`});
    clearActiveVisual();
    state.activeDocument = doc;
    state.selectedItem = {rootId: doc.rootId, path: doc.path, type: "file"};
    state.view = "files";
    state.rootId = doc.rootId;
    state.path = doc.path.includes("/") ? doc.path.slice(0, doc.path.lastIndexOf("/")) : "";
    // HF-NAV-006: opening a document from outside the tree makes the tree reveal its location.
    if (expandTree) void revealInTree(doc.path);
    state.listRequestId++;
    renderNavigation(); renderToolbar();
    showDocumentModeControls(doc);
    const results = $("#results");
    if (state.splitEnabled && !window.matchMedia("(max-width: 720px)").matches) {
      state.splitDocument = doc;
      $("#split-pane").hidden = false;
      $("#split-pane").classList.add("aberto");
      $("#split-content").replaceChildren(doc.host);
      redrawListing();
    } else {
      state.splitDocument = null;
      results.replaceChildren(doc.host);
    }
    state.documents.set(`${doc.rootId}\u0000${doc.path}`, doc);
    doc.host.hidden = false;
    if (doc.source?.view) requestAnimationFrame(() => doc.source.view.requestMeasure());
    renderBreadcrumbs(); renderTabs();
    if (state.focusDocumentOnShow) {
      // Opened via keyboard: focus moves into the text, instead of staying on the page body.
      state.focusDocumentOnShow = false;
      requestAnimationFrame(() => {
        const pane = doc.mode === "formatted" ? doc.previewPane : doc.mode === "clean" ? doc.cleanPane : null;
        if (pane) { pane.tabIndex = -1; pane.focus({preventScroll: true}); } else doc.source?.view?.focus();
      });
    }
  }
  function relativeAttachmentReference(notePath, imagePath) {
    const from = notePath.split("/").slice(0, -1);
    const to = imagePath.split("/");
    let common = 0;
    while (common < from.length && common < to.length && from[common] === to[common]) common++;
    const escaped = value => encodeURIComponent(value).replace(/[!'()*]/g, character => `%${character.charCodeAt(0).toString(16).toUpperCase()}`);
    return [...from.slice(common).map(() => ".."), ...to.slice(common).map(escaped)].join("/");
  }
  async function uploadManagedImages(doc, files) {
    if (!doc.isMarkdown || !files.length) return;
    const operation = trackDocumentOperation("managed-image-upload", async () => {
      let refreshGallery = false;
      for (const file of files) refreshGallery = await uploadManagedImage(doc, file) || refreshGallery;
      return refreshGallery;
    }, [doc]);
    if (!operation) {
      const reason = state.logoutPhase === "idle"
        ? "Outra operação do documento está em andamento. Nenhum novo envio foi iniciado."
        : "A saída segura está em andamento. Nenhum novo envio foi iniciado.";
      docStatus(doc, reason, true);
      return;
    }
    if (await operation) await refreshAttachmentGallery(doc);
  }
  async function uploadManagedImage(doc, file) {
    if (file.size > 20 * 1024 * 1024) { docStatus(doc, "A imagem excede o limite de 20 MiB.", true); return; }
    if (["image/svg+xml", "image/svg"].includes(file.type)) { docStatus(doc, "SVG não pode ser inserido como imagem gerenciada.", true); return; }
    if (file.type && !["image/png", "image/jpeg", "image/gif", "image/webp"].includes(file.type)) {
      docStatus(doc, "Formato não aceito. Use PNG, JPEG, GIF ou WebP.", true); return;
    }
    try {
      docStatus(doc, `Enviando ${file.name || "imagem"}…`);
      const operation = await request("api/files/token", {
        method: "POST", headers: {"X-CSRF-Token": csrf},
      });
      const response = await fetch(endpoint("api/images/upload", {rootId: doc.rootId, path: doc.path}), {
        method: "POST", credentials: "same-origin", cache: "no-store", body: file,
        headers: {
          Accept: "application/json", "Content-Type": file.type || "application/octet-stream",
          "X-CSRF-Token": csrf, "X-Hopper-Operation-Token": operation.operationToken,
        },
      });
      let result = null;
      try { result = await response.json(); } catch (_error) { /* report the status below */ }
      if (!response.ok) {
        if (response.status === 401) sessionEnded({remind: true});
        const error = new Error(result?.error || "upload_failed"); error.status = response.status; error.body = result; throw error;
      }
      const image = result.image;
      doc.source.command("image", image.reference, file.name || "imagem");
      doc.dirty = doc.source.isDirty();
      docStatus(doc, "Imagem enviada e inserida na nota. Salve a nota para confirmar a referência.");
      return !doc.attachmentsPane.hidden;
    } catch (error) {
      const message = error.body?.error === "unsupported_image" || error.status === 415
        ? "O conteúdo não corresponde a PNG, JPEG, GIF ou WebP."
        : error.status === 409
          ? "A operação de imagem entrou em conflito. O texto da nota foi mantido."
          : error.status === 413
            ? "A imagem excede o limite permitido."
            : "Não foi possível enviar a imagem. A nota e o arquivo permanecem disponíveis.";
      docStatus(doc, message, true);
      return false;
    }
  }
  async function refreshAttachmentGallery(doc) {
    const pane = doc.attachmentsPane;
    // HF-IMG-001: the gallery lists the attachments/ folder next to the note.
    pane.replaceChildren(el("h3", "Imagens da nota", "attachments-title"));
    try {
      const result = await request("api/images", {}, {rootId: doc.rootId, path: doc.path});
      pane.append(el("p", absolutePath(result.path), "muted"));
      if (!result.entries.length) { pane.append(el("p", "Ainda não há imagens em attachments/ ao lado desta nota.", "muted")); return; }
      for (const image of result.entries) {
        const row = el("div", undefined, "attachment-row");
        const preview = el("img", undefined, "attachment-preview");
        preview.alt = image.name;
        preview.loading = "lazy";
        preview.src = endpoint("api/preview", {rootId: doc.rootId, path: image.path}).toString();
        const details = el("div", undefined, "attachment-details");
        details.append(el("strong", image.name), el("span", image.pending ? "Pendente — não removida automaticamente" : "Salva na galeria"));
        const insert = button("Inserir", () => {
          if (state.logoutPhase !== "idle") return;
          const reference = relativeAttachmentReference(doc.path, image.path);
          doc.source.command("image", reference, image.name);
          doc.dirty = doc.source.isDirty();
          docStatus(doc, "Referência inserida na nota; salve a nota para confirmá-la.");
        }, "secondary-button");
        row.append(preview, details, insert);
        if (image.pending) {
          row.append(button("Liberar pendência", async () => {
            try {
              await request("api/images/pending", {
                method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
                body: JSON.stringify({rootId: doc.rootId, path: image.path}),
              });
              await refreshAttachmentGallery(doc);
            } catch (_error) { docStatus(doc, "Não foi possível alterar a pendência da imagem.", true); }
          }, "secondary-button"));
        }
        pane.append(row);
      }
    } catch (_error) {
      pane.append(el("p", "A galeria não está disponível no momento.", "document-status error"));
    }
  }
  async function syncOpenBuffers() {
    if (!state.ui || state.bufferSyncBusy || state.logoutPhase !== "idle" || state.sessionEnded) return;
    // With no document open, the tab stays quiet, but only after telling the server the list is empty
    // (on page load and when the last one closes); otherwise the server would still think the note is
    // open and refuse to rename or move it.
    if (!state.documents.size && !state.bufferAcks.length && !state.bufferRegistered) return;
    const documents = [...state.documents.values()];
    const operation = trackDocumentOperation("buffer-move-sync", async () => {
      state.bufferSyncBusy = true;
      try {
        await syncOpenBuffersNow();
      } finally {
        state.bufferSyncBusy = false;
      }
    }, documents);
    if (operation) await operation;
  }
  async function syncOpenBuffersNow() {
    const documents = [...state.documents.values()].map(doc => ({
      rootId: doc.rootId, path: doc.path, version: doc.version, dirty: dirtyDocument(doc),
    }));
    const acknowledgements = state.bufferAcks.splice(0);
    try {
      const result = await request("api/buffers", {
        method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: JSON.stringify({documents, acknowledgements}),
      });
      state.bufferRegistered = documents.length > 0;
      for (const command of result.commands || []) {
        if (state.bufferFinished.has(command.id)) continue;
        if (command.type === "hold") {
          const matched = [];
          let accepted = true;
          for (const target of command.documents || []) {
            const doc = state.documents.get(`${target.rootId}\u0000${target.path}`);
            if (!doc || doc.version !== target.version || dirtyDocument(doc) || doc.saving || doc.held) {
              accepted = false;
              continue;
            }
            matched.push({doc, target});
          }
          if (matched.length !== (command.documents || []).length) accepted = false;
          if (accepted) for (const {doc} of matched) {
            doc.held = true; doc.source.setReadOnly?.(true); doc.saveButton.disabled = true;
            docStatus(doc, "Aguardando movimento seguro deste arquivo.");
          }
          state.bufferAcks.push({
            moveId: command.id, accepted,
            documents: matched.map(({doc, target}) => ({
              rootId: target.rootId, path: target.path, version: doc.version, dirty: dirtyDocument(doc),
            })),
          });
        } else if (command.type === "complete") {
          for (const target of command.documents || []) await reloadMovedBuffer(target);
          state.bufferFinished.add(command.id);
        } else if (command.type === "released") {
          for (const target of command.documents || []) {
            const doc = state.documents.get(`${target.rootId}\u0000${target.path}`);
            if (doc) {
              doc.held = false; setDocumentReadOnly(doc); updateDocumentView(doc);
              docStatus(doc, "Movimento cancelado; o texto continua aberto aqui.");
            }
          }
          state.bufferFinished.add(command.id);
        }
      }
    } catch (_error) {
      // The server treats a missing heartbeat or acknowledgement as a conflict.
    }
  }
  async function reloadMovedBuffer(target) {
    const oldKey = `${target.rootId}\u0000${target.path}`;
    const doc = state.documents.get(oldKey);
    if (!doc) return;
    try {
      const loaded = await documentRequest("api/file", target.newRootId, target.newPath);
      doc.source.replace?.(loaded.content);
      doc.source.setBaseline(loaded.content);
      doc.rootId = target.newRootId; doc.path = target.newPath;
      doc.name = target.newPath.split("/").pop();
      doc.version = loaded.version; doc.baseline = loaded.content;
      doc.held = false; setDocumentReadOnly(doc);
      state.documents.delete(oldKey);
      state.documents.set(`${doc.rootId}\u0000${doc.path}`, doc);
      doc.updateHeader();
      docStatus(doc, "Movimento concluído; a versão atual foi recarregada.");
      if (state.activeDocument === doc) {
        state.rootId = doc.rootId;
        state.path = doc.path.includes("/") ? doc.path.slice(0, doc.path.lastIndexOf("/")) : "";
        renderNavigation(); renderToolbar(); renderBreadcrumbs();
      }
      renderTabs();
      if (same(withoutRevision(state.ui), withoutRevision(state.baseUi))) {
        const current = await fetchUi();
        state.ui = clone(current); state.baseUi = clone(current); renderCollections(); renderTabs();
      }
    } catch (_error) {
      doc.held = false; setDocumentReadOnly(doc);
      docStatus(doc, "O arquivo foi movido, mas não foi possível recarregá-lo. O texto anterior continua aberto aqui.", true);
    }
  }
  async function openDocument(rootId, path) {
    if (state.logoutPhase !== "idle") return;
    const key = `${rootId}\u0000${path}`;
    const existing = state.documents.get(key);
    if (existing) { showDocument(existing); status(`Documento aberto: ${absolutePath(path)}`); return; }
    status(`Abrindo ${absolutePath(path)}…`);
    const ext = extension(path);
    if (["png", "jpg", "jpeg", "gif", "webp", "pdf"].includes(ext)) {
      state.focusDocumentOnShow = false;
      openVisual(rootId, path, ext); return;
    }
    const operation = trackDocumentOperation("open-document", async () => {
      try {
        const loaded = await documentRequest("api/file", rootId, path);
        const doc = createDocumentShell(rootId, path, loaded);
        setDocumentReadOnly(doc);
        state.documents.set(key, doc); showDocument(doc); status(`Documento aberto: ${absolutePath(path)}`);
      } catch (error) {
        state.focusDocumentOnShow = false;
        if (error.status === 422 && error.body?.error === "invalid_encoding") {
          showDocumentError(path, "Sem pré-visualização para este tipo de arquivo. Use Baixar para abri-lo em outro programa.", rootId);
        } else if (error.status === 413) {
          showDocumentError(path, "Arquivo grande demais para abrir aqui (limite de 5 MiB). Use Baixar para abri-lo em outro programa.", rootId);
        } else {
          status(error.status === 404 ? "O arquivo não está mais disponível." : "Não foi possível abrir o arquivo.", true);
        }
      }
    });
    if (operation) await operation;
  }
  // Preview header follows the EditorBar pattern: name in body-strong style and the Download action
  // as a 30px action button with a 14px icon.
  function visualHeader(rootId, path, downloadLabel) {
    const header = el("div", undefined, "editor-barra");
    header.append(el("span", path.split("/").pop(), "ed-nome"));
    const download = actionButton(downloadLabel, "baixar", () => {
      const link = el("a"); link.href = rawLink(rootId, path); link.setAttribute("download", ""); link.click();
    });
    // In the right-hand corner, a centered tooltip would overflow the edge and cause horizontal scroll.
    download.classList.add("tip-dir");
    header.append(download);
    return header;
  }
  function showDocumentError(path, message, rootId) {
    clearActiveVisual();
    status(`${path.split("/").pop()}: ${message}`);
    const panel = state.splitEnabled ? $("#split-content") : $("#results"); panel.replaceChildren();
    const card = el("section", undefined, "document-shell");
    card.append(visualHeader(rootId, path, "Baixar arquivo original"), emptyState(message, "documento")); panel.append(card);
    if (state.splitEnabled) { $("#split-pane").hidden = false; $("#split-pane").classList.add("aberto"); redrawListing(); }
    state.activeDocument = null;
    renderToolbar();
  }
  async function openVisual(rootId, path, ext) {
    clearActiveVisual();
    const visualRequestId = state.visualRequestId;
    const panel = state.splitEnabled ? $("#split-content") : $("#results"); panel.replaceChildren();
    const card = el("section", undefined, "document-shell visual-document");
    card.append(visualHeader(rootId, path, "Baixar arquivo"));
    // While loading, .doc-status shows (13px, text2); it then disappears, leaving just the preview.
    const statusNode = el("p", ext === "pdf" ? "Gerando visualização…" : "Carregando prévia…", "doc-status"); statusNode.setAttribute("role", "status"); card.append(statusNode);
    const surface = el("div", undefined, ext === "pdf" ? "visual-preview" : "visual-preview viewer-img"); card.append(surface); panel.append(card);
    state.activeDocument = null;
    state.splitDocument = null;
    if (state.splitEnabled) { $("#split-pane").hidden = false; $("#split-pane").classList.add("aberto"); redrawListing(); }
    const src = documentUrl("api/preview", rootId, path).toString();
    if (ext === "pdf") {
      try {
        const {renderPdfPreview} = await import("../../../frontend/src/pdf-viewer.js");
        const cleanup = await renderPdfPreview({container: surface, source: src, basePath: base, onStatus: (message, error) => { statusNode.textContent = message; statusNode.hidden = !error; statusNode.classList.toggle("error", Boolean(error)); }});
        if (visualRequestId === state.visualRequestId) status(`Aberto: ${absolutePath(path)}`);
        if (visualRequestId === state.visualRequestId && surface.isConnected) state.visualCleanup = cleanup;
        else cleanup();
      } catch (_error) {
        statusNode.textContent = "Não foi possível carregar o visualizador de PDF.";
        statusNode.classList.add("error");
      }
    } else {
      const image = el("img", undefined, "raster-preview"); image.alt = `Prévia de ${path.split("/").pop()}`;
      image.title = "Clique para ver no tamanho real";
      image.addEventListener("click", () => {
        const real = surface.classList.toggle("tamanho-real");
        image.title = real ? "Clique para ajustar à largura" : "Clique para ver no tamanho real";
      });
      image.addEventListener("load", () => { if (visualRequestId === state.visualRequestId) { statusNode.hidden = true; status(`Aberto: ${absolutePath(path)}`); } });
      image.addEventListener("error", () => { if (visualRequestId === state.visualRequestId) { statusNode.textContent = "Não foi possível exibir esta imagem."; statusNode.classList.add("error"); } });
      image.src = src; surface.append(image);
    }
  }
  function clearActiveVisual() {
    showDocumentModeControls(null);
    state.visualRequestId++;
    const cleanup = state.visualCleanup;
    state.visualCleanup = null;
    cleanup?.();
  }
  async function saveDocumentAttempt(doc) {
    const snapshot = doc.getValue();
    if (new TextEncoder().encode(snapshot).length > 5 * 1024 * 1024) { docStatus(doc, "Não foi possível salvar: o texto passa do limite de 5 MiB.", true); return false; }
    const sequence = ++doc.counter;
    const submittedVersion = doc.version;
    doc.saveButton.disabled = true; docStatus(doc, "Salvando…");
    try {
      const result = await documentRequest("api/file", doc.rootId, doc.path, {
        method: "PUT", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: JSON.stringify({baseVersion: submittedVersion, content: snapshot}),
      });
      if (sequence >= doc.baselineCounter) {
        doc.baselineCounter = sequence; doc.baseline = result.content; doc.version = result.savedVersion;
        doc.source.setBaseline(result.content);
      }
      doc.dirty = doc.source.isDirty();
      docStatus(doc, dirtyDocument(doc) ? "Salvo; há alterações feitas depois disso." : "Salvamento confirmado.");
      if (doc.attachmentsPane && !doc.attachmentsPane.hidden) void refreshAttachmentGallery(doc);
      // With the list visible (split view), the saved file's size and date refresh.
      if (state.splitEnabled && state.view === "files" && doc.rootId === state.rootId && parentOf(doc.path) === state.path) {
        void listDirectory(state.rootId, state.path, {join: false}).then(result => {
          if (state.path === parentOf(doc.path) && state.listingPath === state.path) { state.entries = result.entries; redrawListing(); }
        }).catch(() => {});
      }
      if (doc.isMarkdown) void refreshTagIndex();
      return true;
    } catch (error) {
      if (error.status === 409 && error.body?.error === "conflict") {
        doc.serverVersion = error.body.current || null;
        docStatus(doc, "Conflito: o arquivo mudou no servidor desde que foi aberto. Seu texto e sua seleção foram mantidos.", true);
        showConflictActions(doc);
      } else if (error.body?.error === "indeterminate") {
        doc.serverVersion = error.body.current || null;
        docStatus(doc, "Não foi possível confirmar o salvamento. Seu texto foi mantido; confira a versão do servidor antes de resolver.", true);
        showConflictActions(doc);
      } else {
        const message = error.status === 401
          ? "Não foi possível salvar: a sessão terminou. Entre de novo; o texto continua aberto aqui para copiar."
          : error.body?.error === "limit_exceeded"
          ? "O arquivo excede o limite de 5 MiB."
          : error.body?.error === "storage_unavailable"
            ? "Não foi possível salvar: não há espaço em disco. O texto continua aberto aqui."
            : error.status === 403
              ? `Não foi possível salvar: ${error.body?.error === "not_editable" ? readOnlyReasonText(error.body.reason) : "sem permissão para gravar aqui."} O texto continua aberto aqui.`
              : error.status === 404
                ? "Não foi possível salvar: o arquivo não está mais lá. O texto continua aberto aqui."
                : error.body?.error === "metadata_unsupported"
                  ? "Não foi possível salvar: o dono, o grupo ou as permissões do arquivo não podem ser mantidos por esta conta. O texto continua aberto aqui."
                  : "Não foi possível salvar por uma falha de leitura ou gravação. O texto continua aberto para copiar ou baixar.";
        docStatus(doc, message, true);
      }
      return false;
    }
  }
  async function saveDocument(doc, origin = "user") {
    if (state.logoutPhase !== "idle" && origin !== "safe-exit") return {ok: false, clean: false};
    if (doc.saving) {
      doc.saveAgain = true;
      return doc.savePromise || {ok: false, clean: false};
    }
    doc.saving = true;
    doc.saveButton.disabled = true;
    const saveRun = (async () => {
      let ok = true;
      do {
        doc.saveAgain = false;
        if (!dirtyDocument(doc)) break;
        ok = await saveDocumentAttempt(doc);
        if (!ok) break;
      } while (doc.saveAgain && dirtyDocument(doc));
      return {ok, clean: !dirtyDocument(doc)};
    })();
    doc.savePromise = saveRun;
    try {
      return await saveRun;
    } finally {
      doc.saving = false;
      doc.savePromise = null;
      updateDocumentView(doc);
    }
  }
  function showConflictActions(doc) {
    doc.host.querySelector(".conflict-actions")?.remove();
    const actions = el("div", undefined, "conflict-actions");
    if (doc.serverVersion?.content !== undefined) {
      actions.append(button("Copiar versão do servidor", async () => {
        try { await navigator.clipboard.writeText(doc.serverVersion.content); toast("Versão do servidor copiada"); }
        catch (_error) { docStatus(doc, "Não foi possível copiar a versão do servidor.", true); }
      }, "secondary-button"));
      actions.append(button("Recarregar versão do servidor", async () => {
        if (state.logoutPhase !== "idle") return;
        const server = doc.serverVersion;
        if (!await askConfirm("Recarregar a versão do servidor?", "O texto local deste documento será substituído pela versão atual do servidor.", "Substituir", {danger: true})) return;
        // Only replaces it if nothing changed during confirmation.
        if (state.logoutPhase !== "idle" || doc.serverVersion !== server || ![...state.documents.values()].includes(doc)) return;
        if (doc.source.replace) doc.source.replace(doc.serverVersion.content);
        else if (doc.source.view) doc.source.view.dispatch({changes: {from: 0, to: doc.source.view.state.doc.length, insert: doc.serverVersion.content}, selection: {anchor: 0}});
        else { doc.source.element.value = doc.serverVersion.content; doc.source.element.dispatchEvent(new Event("input", {bubbles: true})); }
        doc.baseline = doc.serverVersion.content; doc.version = doc.serverVersion.version; doc.serverVersion = null;
        updateDocumentView(doc); docStatus(doc, "Versão do servidor carregada."); toast("Versão do servidor carregada"); actions.remove();
      }, "secondary-button"));
    }
    doc.host.append(actions);
  }
  window.addEventListener("beforeunload", event => {
    if (state.logoutPhase === "leaving" && state.logoutNavigationApproved) return;
    if (state.logoutPhase === "posting") {
      // Keep protecting the document until the server confirms logout and the
      // approved navigation begins. Consent to logout does not approve an
      // unrelated unload while the POST is still in flight.
      event.preventDefault(); event.returnValue = "";
      return;
    }
    if (state.documentOperations.size === 0 &&
        ![...state.documents.values()].some(doc => dirtyDocument(doc) || doc.saving || doc.saveAgain)) return;
    event.preventDefault(); event.returnValue = "";
  });
  function displayEntries() {
    const query = state.localFilter.trim().normalize("NFC").toLocaleLowerCase();
    // "here" (HF-API-004): only immediate files and folders of the open folder.
    const entries = query ? state.entries.filter(entry => ["file", "directory"].includes(entry.type) && entry.name.normalize("NFC").toLocaleLowerCase().includes(query)) : [...state.entries];
    const collator = new Intl.Collator("pt-BR", {numeric: true, sensitivity: "base"});
    const order = state.ui.preferences.ordering;
    entries.sort((left, right) => {
      if (entryIsFolder(left) !== entryIsFolder(right)) return entryIsFolder(left) ? -1 : 1;
      let compare = 0;
      if (order === "type") compare = collator.compare(entryKindKey(left), entryKindKey(right)) || collator.compare(left.name, right.name);
      else if (order === "size") compare = entrySize(left) - entrySize(right) || collator.compare(left.name, right.name);
      else if (order === "created" || order === "modified") {
        const key = order === "created" ? "createdAt" : "modifiedAt";
        compare = (parseStamp(left[key])?.getTime() ?? -1) - (parseStamp(right[key])?.getTime() ?? -1) || collator.compare(left.name, right.name);
      } else compare = collator.compare(left.name, right.name);
      return state.sortDirection === "desc" ? -compare : compare;
    });
    return entries;
  }
  // Roving tabindex: only the current row is part of the Tab order; arrow keys move between rows.
  function rovingRow(row) {
    if (!row) return;
    for (const other of row.closest("tbody")?.querySelectorAll("tr.item[tabindex='0']") || []) if (other !== row) other.tabIndex = -1;
    row.tabIndex = 0;
  }
  // Labels use the fixed spectrum order (as in Finder and the app's specification), not alphabetical.
  const LABEL_ORDER = ["vermelho", "laranja", "amarelo", "verde", "azul", "roxo", "cinza"];
  function orderedLabels() {
    return Object.entries(state.ui.labels).sort(([left], [right]) => LABEL_ORDER.indexOf(left) - LABEL_ORDER.indexOf(right));
  }
  function entryKindKey(entry) {
    if (entryIsFolder(entry)) return "";
    const dot = entry.name.lastIndexOf(".");
    return dot > 0 ? entry.name.slice(dot + 1).toLocaleLowerCase() : "\uffff";
  }
  function entrySize(entry) {
    if (entry.type === "directory") {
      const [listRoot, parent] = listedFolder();
      const known = state.dirSizes.get(`${listRoot}\u0000${parent ? `${parent}/${entry.name}` : entry.name}`);
      return known && typeof known === "object" ? known.totalBytes : -1;
    }
    return Number.isFinite(entry.size) ? entry.size : -1;
  }
  // Folder sizes: one request per folder, at most two at a time, canceled when the open folder
  // changes. The server measures with a short deadline and reports when the total is partial.
  function showFolderSize(cell, known) {
    cell.classList.remove("c-calculando");
    if (known === "pending") { cell.textContent = "…"; cell.classList.add("c-calculando"); cell.title = "Calculando o tamanho da pasta…"; return; }
    if (!known || typeof known !== "object") { cell.textContent = "—"; cell.title = known === "denied" ? "Sem permissão para ler esta pasta." : "Tamanho indisponível."; return; }
    const virtual = known.omissions?.some(item => item.category === "pseudo_filesystem") && known.directoryCount === 0;
    if (virtual) { cell.textContent = "—"; cell.title = "Sistema de arquivos virtual: não ocupa espaço em disco."; return; }
    cell.textContent = `${formatSize(known.totalBytes)}${known.complete ? "" : "+"}`;
    const counts = folderCounts(known);
    const reasons = (known.omissions || []).map(item => ({execution_budget: "o cálculo parou no limite de tempo", unreadable_directory: "há subpastas sem permissão de leitura", pseudo_filesystem: "sistemas de arquivos virtuais não entram", depth_limit: "há pastas profundas demais", entry_race: "itens mudaram durante o cálculo", invalid_name: "há nomes que não são UTF-8"})[item.category] || item.category);
    cell.title = known.complete ? counts : `Pelo menos isto: ${counts}; ${reasons.join("; ")}.`;
  }
  // The measurement counts the folder itself in directoryCount; the text reports only the subfolders.
  function folderCounts(known) {
    const subfolders = Math.max(0, known.directoryCount - 1);
    return `${formatCount(known.fileCount)} ${known.fileCount === 1 ? "arquivo" : "arquivos"} e ${formatCount(subfolders)} ${subfolders === 1 ? "subpasta" : "subpastas"}`;
  }
  function queueFolderSizes(entries) {
    const [rootId, parent] = listedFolder();
    const wanted = entries.filter(entry => entry.type === "directory" && entryUsable(entry))
      .map(entry => parent ? `${parent}/${entry.name}` : entry.name)
      .filter(path => !state.dirSizes.has(`${rootId}\u0000${path}`));
    if (!wanted.length) return;
    const controller = state.sizeController || (state.sizeController = new AbortController());
    for (const path of wanted) state.dirSizes.set(`${rootId}\u0000${path}`, "pending");
    let next = 0; let running = 0;
    // Sort by size: the list flags that it is still measuring folders and re-sorts once, at the end.
    const bySize = () => state.ui.preferences.ordering === "size";
    const listShown = () => state.view === "files" && !state.searchResult && !(state.activeDocument && !state.splitEnabled)
      && state.listingRootId === rootId && state.listingPath === parent && Boolean($("#results > table.lista"));
    const settled = () => {
      if (controller.signal.aborted) return;
      if (next >= wanted.length && running === 0 && bySize() && listShown()) { renderResults(displayEntries()); status(filterStatus()); }
    };
    const launch = () => {
      while (running < 2 && next < wanted.length && !controller.signal.aborted) {
        const path = wanted[next++]; running += 1;
        const key = `${rootId}\u0000${path}`;
        request("api/dir-size", {signal: controller.signal}, {rootId, path}).then(result => {
          if (!controller.signal.aborted) state.dirSizes.set(key, result);
        }, error => {
          if (!controller.signal.aborted) state.dirSizes.set(key, error.status === 403 || error.status === 404 ? "denied" : "error");
        }).finally(() => {
          running -= 1;
          if (!controller.signal.aborted) {
            const cell = state.sizeCells.get(key); if (cell?.isConnected) showFolderSize(cell, state.dirSizes.get(key));
            launch(); settled();
          }
        });
      }
    };
    launch();
  }
  function resetFolderSizes() {
    state.sizeController?.abort(); state.sizeController = null;
    state.dirSizes = new Map(); state.sizeCells = new Map();
  }
  function renderResults(entries, message = "") {
    if (state.view === "files" && state.rootId && state.listingRootId === state.rootId && state.listingPath === state.path) entries = displayEntries();
    // Rows belong to the listed folder. While another folder is loading, state.path is already the
    // requested folder; using it would pair the clicked name with the wrong folder.
    const [listRoot, parent] = listedFolder();
    const panel = $("#results");
    const focused = panel.contains(document.activeElement) ? document.activeElement : null;
    const focusRow = focused?.closest?.("tr.item")?.dataset.path ?? null;
    const focusIndex = focused ? [...panel.querySelectorAll("tr.item")].indexOf(focused.closest("tr.item")) : -1;
    const focusHead = focused?.closest?.("th")?.dataset.order ?? null;
    const focusList = state.focusListOnRender && state.view === "files" && state.listingRootId === state.rootId && state.listingPath === state.path;
    if (focusList) state.focusListOnRender = false;
    panel.replaceChildren();
    if (message) { panel.append(emptyState(message, "documento")); return; }
    const table = el("table", undefined, "lista");
    if (state.ui.preferences.density === "compact") table.classList.add("compacta");
    const columns = el("colgroup");
    for (const [className, width] of [["c-check", "34px"], ["c-nome", ""], ["c-tam", "104px"], ["c-criado", "140px"], ["c-mod", "150px"]]) {
      const column = el("col"); if (className) column.className = className; if (width) column.style.width = width; columns.append(column);
    }
    table.append(columns);
    const head = el("thead"); const header = el("tr");
    const allCell = el("th", undefined, "c-check");
    if (state.view === "files") {
      const all = el("input"); all.type = "checkbox"; all.className = "check"; all.tabIndex = -1;
      all.setAttribute("aria-label", "Marcar todos os itens");
      all.addEventListener("change", () => setAllSelected(all.checked));
      allCell.append(all);
      allCell.addEventListener("click", event => { if (event.target === allCell) all.click(); });
    }
    header.append(allCell);
    const nameHead = el("th", "Nome", "c-nome ordenavel"); nameHead.tabIndex = 0; nameHead.title = "Ordenar por nome";
    nameHead.addEventListener("click", () => setOrdering("name"));
    const sizeHead = el("th", "Tamanho", "c-tam ordenavel"); sizeHead.tabIndex = 0; sizeHead.title = "Ordenar por tamanho";
    sizeHead.addEventListener("click", () => setOrdering("size"));
    const createdHead = el("th", "Criado", "c-criado ordenavel"); createdHead.tabIndex = 0; createdHead.title = "Ordenar por data de criação";
    createdHead.addEventListener("click", () => setOrdering("created"));
    const modifiedHead = el("th", "Modificado", "c-mod ordenavel"); modifiedHead.tabIndex = 0; modifiedHead.title = "Ordenar por data de modificação";
    modifiedHead.addEventListener("click", () => setOrdering("modified"));
    for (const [cell, order] of [[nameHead, "name"], [sizeHead, "size"], [createdHead, "created"], [modifiedHead, "modified"]]) {
      cell.dataset.order = order;
      cell.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); setOrdering(order); } });
      if ((state.ui.preferences.ordering || "name") === order) {
        cell.setAttribute("aria-sort", state.sortDirection === "desc" ? "descending" : "ascending");
        cell.append(el("span", state.sortDirection === "desc" ? " ↓" : " ↑", "ordem-seta"));
      }
    }
    header.append(nameHead, sizeHead, createdHead, modifiedHead); head.append(header); table.append(head);
    if (state.view === "files") state.sizeCells = new Map();
    const body = el("tbody");
    let previousGroup = "";
    for (const entry of entries) {
      const group = entryIsFolder(entry) ? "Pastas" : "Arquivos";
      if (state.groupBy === "type" && group !== previousGroup) {
        const groupRow = el("tr", undefined, "grupo-cab");
        const groupCell = el("td", `${group} · ${entries.filter(item => (entryIsFolder(item) ? "Pastas" : "Arquivos") === group).length}`, "c-grupo");
        groupCell.colSpan = 5; groupRow.append(groupCell); body.append(groupRow); previousGroup = group;
      }
      const path = parent ? `${parent}/${entry.name}` : entry.name;
      const usable = entryUsable(entry);
      const operable = usable && ["file", "directory"].includes(entry.type);
      const row = el("tr", undefined, usable ? "item" : "item inacessivel"); row.tabIndex = -1;
      row.dataset.path = path; if (operable) row.dataset.operable = "1";
      row.addEventListener("focus", () => { rovingRow(row); state.listFocusPath = path; });
      if (!usable) { row.setAttribute("aria-disabled", "true"); row.title = entryReason(entry); row.setAttribute("aria-label", `${entryLabel(entry)} — ${entryReason(entry)}`); }
      const marked = operable ? itemState(listRoot, path) : null;
      const selectCell = el("td", undefined, "c-check");
      const check = el("input"); check.type = "checkbox"; check.className = "check";
      check.checked = state.selectedForZip.has(`${listRoot}\u0000${path}`); check.disabled = !operable;
      check.setAttribute("aria-label", `Selecionar ${entry.name}`);
      check.tabIndex = -1;
      check.addEventListener("click", event => event.stopPropagation());
      check.addEventListener("change", () => {
        const key = `${listRoot}\u0000${path}`;
        if (check.checked) state.selectedForZip.add(key); else state.selectedForZip.delete(key);
        row.classList.toggle("marcado", check.checked);
        if (state.batchSelect) return;
        syncSelectionButtons(); selectionStatus();
      });
      selectCell.append(check); row.classList.toggle("marcado", check.checked);
      const nameCell = el("td", undefined, "c-nome");
      const nameWrap = el("span", undefined, "nome-wrap");
      const icon = entryIcon(entry, path); icon.classList.add("icone-item"); nameWrap.append(icon);
      // Cmd/Ctrl+click toggles selection; Shift+click selects the range; a plain click opens the item.
      const choose = event => {
        if (!operable || check.disabled || !(event.metaKey || event.ctrlKey || event.shiftKey)) return false;
        event.preventDefault();
        if (event.shiftKey && state.selectAnchor && state.entries.some(item => (parent ? `${parent}/${item.name}` : item.name) === state.selectAnchor)) selectRange(state.selectAnchor, path);
        else { check.click(); state.selectAnchor = path; }
        return true;
      };
      const name = button(entryLabel(entry), event => { if (event.detail > 1 || choose(event)) return; activateListed(listRoot, parent, entry); }, "nome-link nome");
      name.tabIndex = -1;
      name.title = usable ? (entry.type === "link" ? `${absolutePath(path)} → ${entry.resolved}` : absolutePath(path)) : `${entryLabel(entry)} — ${entryReason(entry)}`;
      if (!usable) name.setAttribute("aria-disabled", "true");
      nameWrap.append(name);
      if (entry.links > 1 && entry.type === "file") { const linked = el("span", "vínculos", "badge-link"); linked.title = `${entry.links} nomes para o mesmo arquivo; não editável`; nameWrap.append(linked); }
      if (marked?.favorite) { const star = el("span", undefined, "badge-fav"); star.title = "Favorito"; star.setAttribute("aria-label", "Favorito"); star.append(svgIcon("favorito", 12)); nameWrap.append(star); }
      if (entry.type === "directory" && usable && isTagFolder(path)) nameWrap.append(tagFolderBadge());
      for (const id of marked?.labelIds || []) {
        const label = state.ui.labels[id]; if (!label) continue;
        const dot = el("span", undefined, "tag-dot"); dot.style.backgroundColor = labelColor(id); dot.title = label.name; dot.setAttribute("aria-label", label.name); nameWrap.append(dot);
      }
      nameCell.append(nameWrap);
      const sizeCell = el("td", undefined, "c-tam");
      if (entry.type === "directory" && usable && state.view === "files") {
        const key = `${listRoot}\u0000${path}`;
        state.sizeCells.set(key, sizeCell); showFolderSize(sizeCell, state.dirSizes.get(key) || "pending");
      } else sizeCell.textContent = Number.isFinite(entry.size) ? formatSize(entry.size) : "—";
      const createdCell = el("td", formatDate(entry.createdAt) || "—", "c-criado c-data");
      const modifiedCell = el("td", formatDate(entry.modifiedAt) || "—", "c-mod c-data");
      createdCell.title = entry.createdAt ? `Criado em ${formatFullDate(entry.createdAt)}` : "O sistema de arquivos não registra a data de criação deste item.";
      modifiedCell.title = entry.modifiedAt ? `Modificado em ${formatFullDate(entry.modifiedAt)}` : "Data de modificação indisponível.";
      row.append(selectCell, nameCell, sizeCell, createdCell, modifiedCell);
      row.addEventListener("click", event => {
        if (event.target.closest("button,input,a,select")) return;
        // The whole selection cell toggles the item.
        if (event.target.closest("td.c-check")) { if (!choose(event) && !check.disabled) check.click(); return; }
        if (event.detail > 1 || choose(event)) return;
        activateListed(listRoot, parent, entry);
      });
      check.addEventListener("click", () => { state.selectAnchor = path; });
      row.addEventListener("keydown", event => {
        if (event.target !== row) return;
        if (event.key === "Enter") {
          event.preventDefault();
          if (entryIsFolder(entry)) state.focusListOnRender = true; else state.focusDocumentOnShow = true;
          activateListed(listRoot, parent, entry);
        }
        else if (event.key === " ") { event.preventDefault(); if (!check.disabled) check.click(); }
      });
      if (entry.addressable !== false) row.addEventListener("contextmenu", event => showItemMenu(event, listRoot, path, entry.type === "file", entry.type, {pathOnly: !operable}));
      if (operable && state.view === "files") makeDraggable(row, path);
      if (entry.type === "directory" && usable && state.view === "files") makeFolderDropTarget(row, path);
      body.append(row);
    }
    table.append(body); panel.append(table);
    const rows = [...body.querySelectorAll("tr.item")];
    const current = rows.find(row => row.dataset.path === (focusRow ?? state.listFocusPath))
      || (focusIndex >= 0 ? rows[Math.min(focusIndex, rows.length - 1)] : null) || rows[0];
    rovingRow(current);
    // Redrawing does not drop keyboard focus: it returns to the same row or the same header.
    const idle = !document.activeElement || document.activeElement === document.body;
    if (focusHead) head.querySelector(`th[data-order="${focusHead}"]`)?.focus();
    else if (current && (focusRow !== null || (focusList && idle))) current.focus({preventScroll: focusRow !== null});
    if (state.view === "files") queueFolderSizes(entries);
    if (!entries.length) panel.append(emptyState(state.localFilter ? "Nada corresponde ao filtro" : "Pasta vazia", state.localFilter ? "busca" : "pasta"));
    syncSelectionButtons();
  }
  // Sort direction and grouping are restored on reload (this browser's convenience state).
  function saveListPrefs() {
    try { localStorage.setItem("hf-lista", JSON.stringify({direction: state.sortDirection, groupBy: state.groupBy})); } catch (_error) { /* optional */ }
  }
  function loadListPrefs() {
    for (const key of ["favoritesOpen", "tagsOpen", "labelsOpen", "treeSectionOpen"]) {
      try { if (localStorage.getItem(`hf-secao-${key}`) === "fechada") state[key] = false; } catch (_error) { /* optional */ }
    }
    try {
      const saved = JSON.parse(localStorage.getItem("hf-lista") || "null");
      if (saved && ["asc", "desc"].includes(saved.direction)) state.sortDirection = saved.direction;
      if (saved && ["none", "type"].includes(saved.groupBy)) state.groupBy = saved.groupBy;
    } catch (_error) { /* default */ }
  }
  function setOrdering(order) {
    if (state.ui.preferences.ordering === order) state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
    else { state.ui.preferences.ordering = order; state.sortDirection = "asc"; }
    saveListPrefs();
    applyPreferences(); renderResults(displayEntries()); scheduleSave();
  }
  function openEntry(rootId, path, type) {
    if (type === "link") {
      // A link found by search is the link itself: it opens the folder that contains it.
      openEntry(rootId, path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "", "directory");
      return;
    }
    state.view = "files"; state.rootId = rootId;
    state.selectedItem = {rootId, path, type};
    if (type === "directory") {
      state.path = path; state.localFilter = ""; renderNavigation(); renderToolbar(); renderCollections(); loadDirectory();
    } else void openDocument(rootId, path);
  }
  function closeItemMenu(options) { state.closeItemMenu?.(options); }
  // Touch: a steady long-press on a row opens the same menu as right-click. Safari on iOS never
  // fires "contextmenu" on long-press, so the app dispatches it manually after 500ms; the
  // triggering touch does not also open the item, and touch rows are not draggable, to avoid
  // competing with the system's own drag gesture (mouse and trackpad remain draggable).
  function installLongPress() {
    const rows = "#results tr.item, #tree .node-row, #favoritos .node-row, .fav-row, .res-item";
    let timer = null; let start = null; let fired = false; let swallowUntil = 0;
    const cancel = () => { clearTimeout(timer); timer = null; };
    document.addEventListener("pointerdown", event => {
      cancel(); fired = false; swallowUntil = 0;
      const row = event.target.closest?.(rows);
      if (row?.dataset.drag) row.draggable = event.pointerType === "mouse";
      if (!row || event.pointerType === "mouse" || !event.isPrimary || event.target.closest("input, textarea")) return;
      start = {x: event.clientX, y: event.clientY, target: event.target};
      // A context menu counts as "open" only once a handler calls preventDefault: Trash cards and
      // search-pagination rows share the row class but have no menu. The synthetic event carries
      // detail: 1, so a touch-opened menu does not auto-focus the first item the way a keyboard-opened one does.
      timer = setTimeout(() => {
        timer = null;
        fired = !start.target.dispatchEvent(new MouseEvent("contextmenu", {bubbles: true, cancelable: true, detail: 1, clientX: start.x, clientY: start.y}));
      }, 500);
    }, true);
    document.addEventListener("pointermove", event => {
      if (timer && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 10) cancel();
    }, true);
    document.addEventListener("pointerup", () => { cancel(); if (fired) swallowUntil = Date.now() + 700; }, true);
    document.addEventListener("pointercancel", cancel, true);
    // If the system also fires its own event on long-press, only one menu should end up open.
    document.addEventListener("contextmenu", event => {
      if (!event.isTrusted) return;
      if (fired) { event.preventDefault(); event.stopImmediatePropagation(); } else cancel();
    }, true);
    document.addEventListener("click", event => {
      if (Date.now() >= swallowUntil) return;
      swallowUntil = 0; event.preventDefault(); event.stopImmediatePropagation();
    }, true);
  }
  // "Mais ações para os itens marcados" (touch and narrow screens): the checked item's menu, or
  // the multi-item menu, opened right below the button.
  function showSelectionMenu(anchor) {
    const [key] = state.selectedForZip;
    if (!key) { status("Marque um item para ver as ações.", true); return; }
    const [rootId, path] = key.split("\u0000");
    const kind = knownEntry(rootId, path)?.type === "directory" ? "directory" : "file";
    const rect = anchor.getBoundingClientRect();
    showItemMenu({preventDefault() {}, stopPropagation() {}, type: "click", detail: 0, clientX: rect.left, clientY: rect.bottom + 4, anchor}, rootId, path, kind === "file", kind);
  }
  // pathOnly: an entry with an address that accepts no other action (link, no permission, special
  // file) gets a menu with only "Copiar caminho", and does not become the selected item.
  function showItemMenu(event, rootId, path, file, kind, {pathOnly = false} = {}) {
    event.preventDefault(); event.stopPropagation();
    closeItemMenu();
    if (!pathOnly) state.selectedItem = {rootId, path, type: kind};
    const origin = document.activeElement;
    const anchor = event.anchor || null;
    const menu = $("#menu-ctx");
    menu.replaceChildren(itemActions(rootId, path, file, kind, {pathOnly}));
    for (const row of app.querySelectorAll("tr.menu-alvo")) row.classList.remove("menu-alvo");
    const targetRow = [...app.querySelectorAll("#results tr.item")].find(row => row.dataset.path === path);
    targetRow?.classList.add("menu-alvo");
    state.returnFocusPath = targetRow ? path : null;
    menu.hidden = false; menu.style.display = "block";
    const rect = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(8, Math.min(event.clientX, window.innerWidth - Math.max(rect.width, 220) - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(event.clientY, window.innerHeight - Math.max(rect.height, 100) - 8))}px`;
    const items = () => [...menu.querySelectorAll(".ctx-item")];
    const close = (outside, {restoreFocus = false} = {}) => {
      if (outside && menu.contains(outside.target)) return;
      menu.hidden = true; menu.style.display = "";
      anchor?.setAttribute("aria-expanded", "false");
      for (const row of app.querySelectorAll("tr.menu-alvo")) row.classList.remove("menu-alvo");
      document.removeEventListener("pointerdown", close, true); document.removeEventListener("keydown", keys, true);
      if (state.closeItemMenu === closeNow) state.closeItemMenu = null;
      if (restoreFocus && origin?.isConnected) origin.focus();
    };
    const closeNow = options => close(null, options);
    const keys = keyEvent => {
      if (keyEvent.key === "Escape") { keyEvent.preventDefault(); close(null, {restoreFocus: true}); return; }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(keyEvent.key)) return;
      const list = items(); if (!list.length) return;
      keyEvent.preventDefault();
      const index = list.indexOf(document.activeElement);
      const next = keyEvent.key === "Home" ? 0 : keyEvent.key === "End" ? list.length - 1
        : keyEvent.key === "ArrowDown" ? (index + 1) % list.length : (index - 1 + list.length) % list.length;
      list[next].focus();
    };
    state.closeItemMenu = closeNow;
    anchor?.setAttribute("aria-expanded", "true");
    setTimeout(() => {
      document.addEventListener("pointerdown", close, true); document.addEventListener("keydown", keys, true);
    }, 0);
    if (event.type !== "contextmenu" || event.detail === 0) items()[0]?.focus();
  }

  const sizeNumber = new Intl.NumberFormat("pt-BR", {minimumFractionDigits: 1, maximumFractionDigits: 1});
  function formatCount(value) { return Number(value).toLocaleString("pt-BR"); }
  function formatSize(size) {
    if (!Number.isFinite(size)) return "";
    if (size < 1024) return size === 1 ? "1 byte" : `${size} bytes`;
    const units = ["KB", "MB", "GB", "TB"];
    let value = size / 1024; let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return `${sizeNumber.format(value)} ${units[unit]}`;
  }
  // Listing dates use the instance's configured time zone (the same one used for generated names).
  const dateFormats = (() => {
    const make = (options) => {
      try { return new Intl.DateTimeFormat("pt-BR", {timeZone: app.dataset.timeZone || undefined, ...options}); }
      catch (_error) { return new Intl.DateTimeFormat("pt-BR", options); }
    };
    return {
      day: make({day: "2-digit", month: "2-digit", year: "numeric"}),
      time: make({hour: "2-digit", minute: "2-digit"}),
      full: make({dateStyle: "full", timeStyle: "medium"}),
    };
  })();
  function parseStamp(value) {
    const time = value ? Date.parse(value) : NaN;
    return Number.isFinite(time) ? new Date(time) : null;
  }
  function formatDate(value) {
    const date = parseStamp(value);
    if (!date) return "";
    const day = dateFormats.day.format(date);
    if (day === dateFormats.day.format(new Date())) return `Hoje, ${dateFormats.time.format(date)}`;
    if (day === dateFormats.day.format(new Date(Date.now() - 86400000))) return `Ontem, ${dateFormats.time.format(date)}`;
    return `${day} ${dateFormats.time.format(date)}`;
  }
  function formatFullDate(value) {
    const date = parseStamp(value);
    return date ? dateFormats.full.format(date) : "";
  }
  function rawLink(rootId, path) {
    return endpoint("api/raw", { rootId, path }).toString();
  }
  // Absolute paths to the clipboard, one per line. writeText runs inside the click that asked
  // for it, which Safari requires.
  async function copyPaths(paths) {
    const many = paths.length > 1;
    try {
      await navigator.clipboard.writeText(paths.map(absolutePath).join("\n"));
      toast(many ? `${paths.length} caminhos copiados` : "Caminho copiado");
    } catch (_error) { status(many ? "Não foi possível copiar os caminhos." : "Não foi possível copiar o caminho.", true); }
  }
  // Checked items in the order the listing shows them; any other checked item follows by path.
  function selectedPaths() {
    const shown = new Map([...app.querySelectorAll("#results tr.item")].map((row, index) => [row.dataset.path, index]));
    const position = path => shown.get(path) ?? shown.size;
    return [...state.selectedForZip].map(key => key.split("\u0000")[1])
      .sort((left, right) => position(left) - position(right) || left.localeCompare(right, "pt-BR"));
  }
  // Context menu: 13px .ctx-item entries, .ctx-sep separators, and the destructive action last,
  // styled .perigo.
  function itemActions(rootId, path, file = false, kind = file ? "file" : "directory", {pathOnly = false} = {}) {
    const actions = el("div", undefined, "item-actions");
    actions.setAttribute("role", "menu");
    const item = itemState(rootId, path);
    const groups = [];
    // Choosing an item closes the menu and returns focus to its origin (the row) before the action
    // runs, so a dialog opened right after keeps the correct origin and restores it on close.
    const entry = (label, action) => { const node = button(label, () => { closeItemMenu({restoreFocus: true}); action(); }, "ctx-item"); node.setAttribute("role", "menuitem"); return node; };
    if (pathOnly) { actions.append(entry("Copiar caminho", () => void copyPaths([path]))); return actions; }
    const count = state.selectedForZip.size;
    if (count > 1 && state.selectedForZip.has(`${rootId}\u0000${path}`)) {
      const head = el("div", `${count} itens marcados`, "ctx-titulo"); head.setAttribute("role", "presentation");
      const rule = el("div", undefined, "ctx-sep"); rule.setAttribute("role", "separator");
      actions.append(head, entry(`Copiar os ${count} caminhos`, () => void copyPaths(selectedPaths())), rule);
      const batch = [];
      if (canWrite(rootId)) batch.push(entry(`Mover ${count} itens…`, () => void moveOrCopySelection("move")));
      batch.push(entry(`Copiar ${count} itens…`, () => void moveOrCopySelection("copy")));
      if (canWrite(rootId)) batch.push(entry(`Criar ZIP dos ${count} itens…`, () => void createZip()));
      const trash = canTrash(rootId) ? entry(`Mover ${count} itens para a lixeira`, () => void trashSelection()) : null;
      if (trash) trash.classList.add("perigo");
      batch.forEach(node => actions.append(node));
      if (trash) { const sep = el("div", undefined, "ctx-sep"); sep.setAttribute("role", "separator"); actions.append(sep, trash); }
      return actions;
    }
    const first = [];
    if (file) {
      first.push(entry("Abrir", () => openDocument(rootId, path)));
      const download = el("a", "Baixar", "ctx-item"); download.href = rawLink(rootId, path); download.setAttribute("download", "");
      download.setAttribute("role", "menuitem"); download.addEventListener("click", () => closeItemMenu({restoreFocus: true}));
      first.push(download);
    }
    if (kind === "directory") first.push(entry("Abrir em nova aba", () => openFolderTab({rootId, path}, {reuse: true})));
    if (kind === "directory") first.push(entry("Calcular tamanho", () => showDirectorySize(rootId, path)));
    if (file && /\.zip$/i.test(path)) first.push(entry("Extrair aqui", () => extractZip(rootId, path)));
    first.push(entry("Copiar caminho", () => void copyPaths([path])));
    groups.push(first);
    if (kind === "directory") groups.push(tagFolderMenu(path, entry));
    const operations = [];
    if (canWrite(rootId)) {
      operations.push(entry("Renomear…", () => renameItem(rootId, path)));
      operations.push(entry("Mover…", () => moveOrCopyItem("move", rootId, path)));
    }
    if (canRead(rootId) && writableRoots().length) operations.push(entry("Copiar…", () => moveOrCopyItem("copy", rootId, path)));
    groups.push(operations);
    const marks = [entry(item?.favorite ? "Remover dos favoritos" : "Adicionar aos favoritos", () => {
      const target = itemState(rootId, path, true); target.favorite = !target.favorite; removeEmptyItem(target); changed();
      refreshItems();
    })];
    marks.push(entry("Labels…", () => showLabelsDialog(rootId, path)));
    if (!file) marks.push(entry("Símbolo da pasta…", () => showEmojiDialog(rootId, path)));
    groups.push(marks);
    if (canTrash(rootId)) {
      const trash = entry("Mover para a lixeira", () => deleteItem(rootId, path)); trash.classList.add("perigo");
      groups.push([trash]);
    }
    groups.filter(group => group.length).forEach((group, index) => {
      if (index) { const sep = el("div", undefined, "ctx-sep"); sep.setAttribute("role", "separator"); actions.append(sep); }
      actions.append(...group);
    });
    return actions;
  }
  function showLabelsDialog(rootId, path) {
    const dialog = $("#operation-dialog");
    if (dialog.open) return;
    const restore = focusReturn();
    onDialogClosed(dialog, () => requestAnimationFrame(restore));
    const heading = el("h2", "Labels"); heading.id = "operation-dialog-title";
    const text = el("p", path.split("/").at(-1) || rootLabel(rootId), "dlg-texto");
    const chips = el("div", undefined, "tag-chips");
    const paint = () => {
      chips.replaceChildren();
      const current = itemState(rootId, path);
      for (const [id, label] of orderedLabels()) {
        const applied = current?.labelIds.includes(id) || false;
        const chip = el("span", undefined, applied ? "tag-chip on" : "tag-chip");
        const toggle = button("", () => {
          const target = itemState(rootId, path, true);
          target.labelIds = target.labelIds.includes(id) ? target.labelIds.filter(value => value !== id) : [...target.labelIds, id];
          removeEmptyItem(target); changed(); refreshItems(); paint();
          chips.querySelector(`[data-label="${id}"]`)?.focus();
        }, "tc-tog");
        toggle.dataset.label = id; toggle.setAttribute("aria-pressed", String(applied));
        const dot = el("span", undefined, "tag-dot"); dot.style.backgroundColor = labelColor(id);
        toggle.append(dot, el("span", label.name, "tc-nome"));
        chip.append(toggle); chips.append(chip);
      }
    };
    paint();
    const menu = el("menu");
    const close = button("Concluir", () => dialog.close(), "primary-button");
    menu.append(close);
    dialog.replaceChildren(heading, text, chips, menu);
    dialog.showModal();
    chips.querySelector("button")?.focus();
  }
  function showEmojiDialog(rootId, path) {
    const dialog = $("#operation-dialog");
    if (dialog.open) return;
    const restore = focusReturn();
    onDialogClosed(dialog, () => requestAnimationFrame(restore));
    const heading = el("h2", "Símbolo da pasta"); heading.id = "operation-dialog-title";
    const text = el("p", path.split("/").at(-1) || rootLabel(rootId), "dlg-texto");
    const list = el("div", undefined, "dlg-lista dlg-lista-curta"); list.setAttribute("role", "listbox");
    const current = itemState(rootId, path)?.emoji || "";
    for (const [value, label] of [["", "Sem símbolo"], ["📁", "📁 Pasta"], ["⭐", "⭐ Destaque"], ["📌", "📌 Fixado"], ["🧭", "🧭 Referência"]]) {
      const option = button(label, () => {
        const target = itemState(rootId, path, Boolean(value));
        if (target) { target.emoji = value || null; removeEmptyItem(target); changed(); refreshItems(); renderNavigation(); }
        dialog.close();
      }, value === current ? "pk-item escolhido" : "pk-item");
      option.setAttribute("role", "option"); option.setAttribute("aria-selected", String(value === current));
      list.append(option);
    }
    list.addEventListener("keydown", event => {
      const options = [...list.querySelectorAll("button")];
      const index = options.indexOf(document.activeElement);
      const next = {ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: options.length - 1}[event.key];
      if (next === undefined) return;
      event.preventDefault(); options[Math.max(0, Math.min(options.length - 1, next))]?.focus();
    });
    const menu = el("menu");
    menu.append(button("Cancelar", () => dialog.close(), "secondary-button"));
    dialog.replaceChildren(heading, text, list, menu);
    dialog.showModal();
    (list.querySelector(".escolhido") || list.querySelector("button"))?.focus();
  }

  // Collapsible section titles in the sidebar (Favorites, Tags, Labels); the choice is kept in this browser.
  function sectionHeading(container, title, cls, key, rerender) {
    const heading = el("div", undefined, `${cls} sec-recolhe`);
    heading.setAttribute("role", "button"); heading.tabIndex = 0;
    heading.setAttribute("aria-expanded", String(state[key] !== false));
    heading.classList.toggle("fechada", state[key] === false);
    heading.append(el("span", undefined, "sec-chev"), document.createTextNode(title));
    const activate = () => {
      state[key] = state[key] === false;
      try { localStorage.setItem(`hf-secao-${key}`, state[key] === false ? "fechada" : "aberta"); } catch (_error) { /* optional */ }
      rerender();
    };
    heading.onclick = activate;
    heading.onkeydown = event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); activate(); } };
    container.classList.toggle("sec-fechada", state[key] === false);
    container.append(heading);
  }
  function favoriteItems() {
    if (!state.ui) return [];
    return state.ui.items.filter(item => item.favorite && item.rootId === state.rootId)
      .sort((left, right) => left.path.localeCompare(right.path, "pt-BR"));
  }
  // HF-NAV-006: Favorites is listed above the file tree. Each favorite folder is the root of its
  // own tree, following the "/" tree's rules: the arrow only expands or collapses, the name navigates
  // to the folder and expands it. Same-tick renderNavigation/renderCollections calls are coalesced into one redraw.
  function renderFavorites() {
    if (state.favoritesDrawPending) return;
    state.favoritesDrawPending = true;
    queueMicrotask(() => { state.favoritesDrawPending = false; drawFavorites(); });
  }
  function drawFavorites() {
    const favorites = $("#favoritos");
    if (!state.ui) return;
    if (!favorites.dataset.alvo) {
      favorites.dataset.alvo = "1";
      // Dropping onto Favorites only favorites the item (HF-FILE-001). With no favorites, the drop zone
      // sits below Trash, so the tree does not shift position mid-drag.
      for (const zone of [favorites, $("#favoritos-soltar")]) if (zone) markDropTarget(zone, item => { item.favorite = true; });
    }
    // Focus returns to the same control after redraw: title, name, arrow, or ×.
    const active = favorites.contains(document.activeElement) ? document.activeElement : null;
    const activeRow = active?.closest(".node-row");
    const focus = !active ? null : active.classList.contains("fav-titulo") ? {part: "titulo"}
      : activeRow ? {key: `${activeRow.dataset.tree}\u0000${activeRow.dataset.path}`, part: active.classList.contains("fav-x") ? "x" : active.classList.contains("chev") ? "seta" : "nome"} : null;
    favorites.replaceChildren();
    const items = favoriteItems();
    // What this browser stores follows the favorites list: a removed favorite is dropped from memory and from storage.
    const paths = new Set(items.map(item => item.path));
    for (const treeId of [...state.treeExpanded.keys()]) if (isFavoriteTree(treeId) && !paths.has(treeId.slice(FAVORITE_TREE.length))) state.treeExpanded.delete(treeId);
    if ([...savedFavoriteTrees().keys()].some(path => !paths.has(path))) saveFavoriteTrees();
    if (!items.length) return;
    sectionHeading(favorites, "Favoritos", "fav-titulo", "favoritesOpen", renderFavorites);
    if (focus?.part === "titulo") favorites.querySelector(".fav-titulo")?.focus({preventScroll: true});
    // Collapsed, a section shows only its title: nothing below it gets built.
    if (state.favoritesOpen === false) return;
    const tree = el("div", undefined, "fav-arvore");
    tree.setAttribute("role", "tree"); tree.setAttribute("aria-label", "Favoritos");
    for (const item of items) tree.append(favoriteNode(item));
    favorites.append(tree);
    treeKeyboard(favorites); rovingTree(favorites);
    if (focus?.key) {
      const row = [...favorites.querySelectorAll(".node-row")].find(node => `${node.dataset.tree}\u0000${node.dataset.path}` === focus.key);
      const target = focus.part === "x" ? row?.querySelector(".fav-x") : focus.part === "seta" ? row?.querySelector("button.chev") : row?.querySelector(".tree-entry-name");
      if (target) {
        if (focus.part === "nome") { for (const other of favorites.querySelectorAll(".tree-entry-name")) other.tabIndex = -1; target.tabIndex = 0; }
        target.focus({preventScroll: true});
      }
    }
    // Folder or file: known from the parent folder's listing, requested once per favorite; the request
    // is made after the render, like the tree, so the render does not recurse into itself.
    const unknown = items.filter(item => item.path && !knownEntry(item.rootId, item.path));
    if (unknown.length) queueMicrotask(() => {
      for (const item of unknown) {
        const key = treeBranchKey(item.rootId, parentOf(item.path));
        if (!state.treeEntries.has(key) && !state.treeLoading.has(key) && !state.treeErrors.has(key)) void loadTreeBranch(item.rootId, parentOf(item.path));
      }
    });
  }
  function favoriteNode(item) {
    const treeId = favoriteTreeId(item.path);
    const name = item.path ? item.path.split("/").at(-1) : rootLabel(item.rootId);
    // Without the parent folder's listing, the type is unknown: the row stays neutral, with no icon or
    // arrow, unless the favorite was open in this browser before (so it was known to be a folder).
    const entry = knownEntry(item.rootId, item.path);
    const usable = !entry || entryUsable(entry);
    const folder = entry ? entry.type === "directory" && usable : treeExpandedFor(treeId).has(item.path);
    const label = entry ? entryLabel(entry) : name;
    const node = el("div", undefined, "node");
    const row = el("div", undefined, `node-row fav-row fav-raiz${folder ? "" : " arq"}`);
    row.setAttribute("role", "treeitem"); row.setAttribute("aria-label", label); row.setAttribute("aria-level", "1");
    row.dataset.path = item.path; row.dataset.tree = treeId;
    if (!usable) { row.classList.add("inacessivel"); row.setAttribute("aria-disabled", "true"); }
    const activePath = state.activeDocument && state.activeDocument.rootId === item.rootId ? state.activeDocument.path : state.path;
    row.classList.toggle("ativo", usable && state.view === "files" && state.rootId === item.rootId && activePath === item.path);
    const icon = item.emoji ? el("span", item.emoji, "emoji-icone")
      : entry ? entryIcon(entry) : folder ? iconNode("folder", name) : el("span", undefined, "icone-vazio");
    icon.classList.add("icone"); icon.setAttribute("aria-hidden", "true");
    // As in the tree: a folder opens and expands; a file opens; a link goes to its target; no permission shows a warning.
    const open = async () => {
      let known = knownEntry(item.rootId, item.path);
      if (!known && item.path) { await loadTreeBranch(item.rootId, parentOf(item.path)); known = knownEntry(item.rootId, item.path); }
      if (!known) { state.treeOrigin = {treeId, path: item.path}; void openMarkedItem(item); return; }
      if (known.type === "directory" && entryUsable(known)) { activateTreeFolder(item.rootId, item.path, treeId); return; }
      if (known.type === "file" && entryUsable(known)) state.treeOrigin = {treeId, path: item.path};
      activateEntry(parentOf(item.path), known);
    };
    const nameButton = button("", () => void open(), "tree-entry-name");
    nameButton.append(icon, el("span", label, "nome"));
    nameButton.title = usable ? absolutePath(item.path) : `${absolutePath(item.path)} — ${entryReason(entry)}`;
    if (!usable) nameButton.setAttribute("aria-disabled", "true");
    const remove = closeButton(`Remover ${name} dos favoritos`, () => {
      const target = itemState(item.rootId, item.path); if (target) { target.favorite = false; removeEmptyItem(target); changed(); refreshItems(); }
    }, "fav-x");
    row.onclick = event => { if (event.target === row) void open(); };
    row.addEventListener("contextmenu", event => {
      const known = knownEntry(item.rootId, item.path);
      const full = known && entryUsable(known) && ["file", "directory"].includes(known.type);
      showItemMenu(event, item.rootId, item.path, known?.type === "file", known?.type || "file", {pathOnly: !full});
    });
    if (!folder) {
      const spacer = el("span", undefined, "chev vazio"); spacer.setAttribute("aria-hidden", "true");
      row.append(spacer, nameButton, remove); node.append(row);
      return node;
    }
    const expanded = treeExpandedFor(treeId).has(item.path);
    row.classList.toggle("aberto", expanded); row.setAttribute("aria-expanded", String(expanded));
    row.append(treeChevron(expanded, name, () => toggleTreeBranch(item.rootId, item.path, treeId)), nameButton, remove);
    const children = el("div", undefined, "node-filhos"); children.setAttribute("role", "group");
    children.hidden = !expanded;
    node.append(row, children);
    if (expanded) renderTreeChildren(children, item.rootId, item.path, treeId);
    return node;
  }

  function renderCollections() {
    renderFavorites();
    const tags = $("#tags-nav");
    const labels = $("#etiquetas");
    tags.replaceChildren(); labels.replaceChildren();
    const makeHeading = (container, title, cls, key) => sectionHeading(container, title, cls, key, renderCollections);

    const visibleTags = (state.tagIndex?.tags || []).filter(tag => !ignoredTags().has(tag.tag));
    if (visibleTags.length || state.tagFolders) makeHeading(tags, "Tags", "tg-titulo", "tagsOpen", "tags", visibleTags.length);
    if (state.tagFolders) {
      // HF-META-002: tags only come from watched folders; the row shows the count and opens the list.
      const count = state.tagFolders.length;
      const summary = button(count ? `${formatCount(count)} ${count === 1 ? "pasta monitorada" : "pastas monitoradas"}` : "Nenhuma pasta monitorada", () => showPreferences({section: "tagFolders"}), "tg-pastas");
      summary.title = count ? state.tagFolders.map(folder => folder.path).join("\n") : "Ver como escolher pastas para as tags";
      tags.append(summary);
      if (!count) tags.append(el("p", "Numa pasta, use o botão direito (no toque, segure o dedo sobre ela) ou o painel Informações e escolha “Monitorar tags nesta pasta”.", "tg-dica"));
    }
    if (visibleTags.length) {
      for (const tag of visibleTags) {
        const row = el("div", undefined, "tg-row");
        row.setAttribute("role", "button"); row.tabIndex = 0;
        const activate = () => enterView("tags", {tag: tag.tag});
        row.onclick = activate;
        row.onkeydown = event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); activate(); } };
        row.classList.toggle("ativo", state.view === "tags" && state.selectedTag === tag.tag);
        row.append(el("span", "#", "tg-hash"), el("span", tag.tag, "nome"), el("span", String(tag.count), "tg-n"));
        row.title = `${formatCount(tag.count)} ${tag.count === 1 ? "arquivo" : "arquivos"} com #${tag.tag}`; tags.append(row);
      }
    }

    const labelEntries = orderedLabels();
    const usedLabelEntries = labelEntries.filter(([id]) => state.ui.items.some(item => item.rootId === state.rootId && item.labelIds.includes(id)));
    if (usedLabelEntries.length) makeHeading(labels, "Labels", "et-titulo", "labelsOpen", "labels", usedLabelEntries.length);
    for (const [id, label] of usedLabelEntries) {
      const row = el("div", undefined, "et-row"); row.classList.toggle("ativo", state.selectedLabel === id && state.view === "labels");
      const pick = el("div", undefined, "et-cab"); pick.setAttribute("role", "button"); pick.tabIndex = 0;
      const activate = () => enterView("labels", {label: id});
      pick.onclick = activate;
      pick.onkeydown = event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); activate(); } };
      const dot = el("span", undefined, "et-bola"); dot.style.backgroundColor = labelColor(id);
      const count = state.ui.items.filter(item => item.rootId === state.rootId && item.labelIds.includes(id)).length;
      pick.append(dot, el("span", label.name, "nome"), el("span", String(count), "et-n")); row.append(pick);
      markDropTarget(row, item => { if (!item.labelIds.includes(id)) item.labelIds.push(id); });
      const rename = button("", async () => {
        const name = await askText("Renomear label", "Nome da label (até 80 caracteres)", label.name, "Renomear", {maxLength: 80});
        if (name === null) return;
        const safe = name.trim(); if (!safe) { status("O nome da label não pode ficar vazio.", true); return; }
        // The server accepts up to 80 characters (HF-META-004); a longer name would block the whole save.
        if ([...safe].length > 80) { status(`Nome da label longo demais: ${[...safe].length} caracteres; use até 80.`, true); return; }
        if (!state.ui.labels[id]) return;
        state.ui.labels[id].name = safe; changed(); renderToolbar(); renderCollections(); renderBreadcrumbs();
        if (state.view === "labels" && state.selectedLabel === id) renderCollectionItems();
      }, "et-ren"); rename.setAttribute("aria-label", `Renomear label ${label.name}`); rename.append(svgIcon("renomear", 10)); row.append(rename); labels.append(row);
    }
  }

  function knownEntry(rootId, path) {
    if (!path) return {name: rootLabel(rootId), type: "directory"};
    const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    const cached = state.treeEntries.get(treeBranchKey(rootId, parent));
    const name = path.split("/").at(-1);
    return cached?.find(entry => entry.name === name) || null;
  }
  function knownEntryType(rootId, path) {
    return knownEntry(rootId, path)?.type || null;
  }
  function renderCollectionItems() {
    const panel = $("#results"); panel.replaceChildren();
    let selected = state.ui.items.filter(item => {
      if (item.rootId !== state.rootId) return false;
      if (state.view === "favorites") return item.favorite;
      return state.view === "labels" && state.selectedLabel && item.labelIds.includes(state.selectedLabel);
    });
    selected = selected.sort((a, b) => a.rootId.localeCompare(b.rootId, "en") || a.path.localeCompare(b.path, "en"));
    const title = state.view === "favorites" ? "Favoritos" : `Label: ${state.ui.labels[state.selectedLabel]?.name || ""}`;
    const header = el("div", undefined, "busca-cab"); header.append(el("strong", title), el("span", `${formatCount(selected.length)} ${selected.length === 1 ? "item" : "itens"}`, "conta")); panel.append(header);
    status(`${formatCount(selected.length)} ${selected.length === 1 ? "item" : "itens"} ${state.view === "favorites" ? "nos favoritos" : `com a label ${state.ui.labels[state.selectedLabel]?.name || ""}`}.`);
    if (!selected.length) { panel.append(emptyState("Ainda não há arquivos nesta coleção", "pasta")); return; }
    for (const item of selected) {
      const row = el("div", undefined, "res-item");
      row.tabIndex = 0; row.setAttribute("role", "button");
      const knownType = knownEntryType(item.rootId, item.path);
      const icon = iconNode(knownType === "directory" || !item.path ? "folder" : "file", item.path.split("/").at(-1)); row.append(icon);
      const body = el("div", undefined, "res-corpo");
      const open = button(item.path.split("/").filter(Boolean).at(-1) || rootLabel(item.rootId), () => void openMarkedItem(item), "res-nome");
      const path = absolutePath(item.path);
      open.title = path; body.append(open, el("div", path, "res-cam")); row.append(body);
      // Removing from the collection without leaving the view (.et-x on the label; same in Favorites).
      const label = state.view === "labels" ? state.ui.labels[state.selectedLabel]?.name : null;
      const removeLabel = state.view === "labels" ? `Tirar ${open.textContent} da label ${label || ""}`.trim() : `Remover ${open.textContent} dos favoritos`;
      row.append(closeButton(removeLabel, () => {
        const target = itemState(item.rootId, item.path);
        if (!target) return;
        if (state.view === "labels") target.labelIds = target.labelIds.filter(id => id !== state.selectedLabel);
        else target.favorite = false;
        removeEmptyItem(target); changed(); renderCollectionItems();
      }, "et-x"));
      row.addEventListener("contextmenu", async event => {
        event.preventDefault();
        try {
          const parent = item.path.includes("/") ? item.path.slice(0, item.path.lastIndexOf("/")) : "";
          const listing = await request("api/list", {}, {rootId: item.rootId, path: parent});
          const found = listing.entries.find(entry => entry.name === item.path.split("/").at(-1));
          const type = found?.type || "file";
          const full = !found || (entryUsable(found) && ["file", "directory"].includes(type));
          showItemMenu(event, item.rootId, item.path, type === "file", type, {pathOnly: !full});
        } catch (_error) {
          status("Este item não está mais disponível neste caminho.", true);
          showItemMenu(event, item.rootId, item.path, false, "file", {pathOnly: true});
        }
      });
      row.addEventListener("click", event => {
        if (event.target.closest("button, a, select, input")) return;
        void openMarkedItem(item);
      });
      row.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key) && event.target === row) { event.preventDefault(); void openMarkedItem(item); } });
      panel.append(row);
    }
  }

  async function openMarkedItem(item) {
    if (!item.path) {
      state.view = "files"; state.rootId = item.rootId; state.path = "";
      state.selectedItem = {rootId: item.rootId, path: "", type: "directory"};
      renderNavigation(); renderToolbar(); renderCollections(); loadDirectory(); return;
    }
    const parent = item.path.includes("/") ? item.path.slice(0, item.path.lastIndexOf("/")) : "";
    const name = item.path.split("/").at(-1);
    try {
      const listing = await request("api/list", {}, {rootId: item.rootId, path: parent});
      const entry = listing.entries.find(value => value.name === name);
      if (!entry) throw new Error("not_found");
      state.view = "files"; state.rootId = item.rootId; state.path = parent; state.localFilter = "";
      state.selectedItem = {rootId: item.rootId, path: item.path, type: entry.type};
      if (entry.type === "directory") state.path = item.path;
      renderNavigation(); renderToolbar(); renderCollections();
      if (entry.type === "directory") await loadDirectory(); else await openDocument(item.rootId, item.path);
    } catch (_error) { status("Este item não está mais disponível neste caminho.", true); }
  }
  async function loadTags() {
    status("Indexando tags…");
    try {
      const index = await refreshTagIndex();
      if (!index) throw new Error("tag_index_unavailable");
      if (state.selectedTag) await selectTag(state.selectedTag);
      else {
        const panel = $("#results"); panel.replaceChildren();
        const none = state.tagFolders && !state.tagFolders.length;
        panel.append(emptyState(none ? "Nenhuma pasta monitorada. Numa pasta, use o botão direito (no toque, segure o dedo sobre ela) ou o painel Informações e escolha “Monitorar tags nesta pasta”."
          : index.complete ? "Escolha uma tag para ver os arquivos" : "A indexação terminou parcialmente; consulte as omissões no painel", "busca"));
        renderMetadata(index, {tags: true});
        status(`Encontradas ${index.tags.length} tags.`);
      }
    } catch (_error) { status("Não foi possível indexar as tags.", true); }
  }
  // Tag watched folders (HF-META-002). The server stores the list outside the UI state; only the
  // marked folder gets the # badge, folders below it qualify through tagFolderReaches.
  const TAG_TEMPORARY = new Set(["/tmp", "/var/tmp"]);
  async function loadTagFolders() {
    try { state.tagFolders = (await request("api/tag-folders")).folders || []; } catch (_error) { return; }
    repaintTagFolders();
  }
  // In a tag view, reloading the rows would repeat the query: whatever changes the list also reloads the tags.
  function repaintTagFolders() {
    if (state.view === "files") refreshItems();
    renderNavigation(); renderCollections(); refreshInfoPanel();
  }
  function tagFolderPaths() { return (state.tagFolders || []).map(folder => folder.path); }
  function isTagFolder(path) { return tagFolderPaths().includes(absolutePath(path)); }
  // Why a scan starting from a watched folder does not reach this folder on the path:
  // hidden, temporary, or, per the last listing, unreadable.
  function tagFolderStop(name, address) {
    if (name.startsWith(".")) return "oculta";
    if (TAG_TEMPORARY.has(address)) return "temporária";
    return knownEntry(BASE_ID, relativePath(address))?.openable === false ? "sem permissão de leitura" : "";
  }
  // Does the scan starting at `start` reach `target`?
  function tagFolderReaches(start, target) {
    if (start === target) return true;
    const prefix = start === "/" ? "/" : `${start}/`;
    if (!target.startsWith(prefix)) return false;
    let current = start === "/" ? "" : start;
    for (const name of target.slice(prefix.length).split("/")) {
      current = `${current}/${name}`;
      if (tagFolderStop(name, current)) return false;
    }
    return true;
  }
  // The nearest watched folder above this one whose scan already reaches it.
  function tagFolderCovering(path) {
    const target = absolutePath(path);
    return tagFolderPaths().filter(start => start !== target && tagFolderReaches(start, target))
      .sort((left, right) => right.length - left.length)[0] || null;
  }
  // Why a folder below a watched one was left out, starting from the nearest watched ancestor:
  // it, or a folder in between, is hidden, temporary, or unreadable.
  function tagFolderGap(path) {
    const target = absolutePath(path);
    const start = tagFolderPaths().filter(value => value !== target && target.startsWith(value === "/" ? "/" : `${value}/`))
      .sort((left, right) => right.length - left.length)[0];
    if (!start) return null;
    let current = start === "/" ? "" : start;
    for (const name of target.slice(start === "/" ? 1 : start.length + 1).split("/")) {
      current = `${current}/${name}`;
      const kind = tagFolderStop(name, current);
      if (kind) return {kind, inside: current !== target};
    }
    return null;
  }
  function tagFolderBadge() {
    const badge = el("span", "#", "badge-tags");
    badge.title = "Tags monitoradas a partir desta pasta"; badge.setAttribute("aria-label", "Tags monitoradas");
    return badge;
  }
  function tagFolderMenu(path, entry) {
    if (!state.tagFolders) return [];
    if (isTagFolder(path)) return [entry("Parar de monitorar tags", () => void toggleTagFolder(path, false))];
    const covering = tagFolderCovering(path);
    if (!covering) return [entry("Monitorar tags nesta pasta", () => void toggleTagFolder(path, true))];
    const note = el("div", `Tags já monitoradas por ${covering}`, "ctx-nota"); note.setAttribute("role", "presentation");
    return [note];
  }
  // Saves one folder at a time; the response carries the full list, which replaces the local one.
  async function setTagFolders(paths, monitored) {
    let folders = state.tagFolders;
    try {
      for (const path of paths) {
        const result = await request("api/tag-folders", {
          method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
          body: JSON.stringify({path: absolutePath(path), monitored}),
        });
        folders = result.folders || [];
      }
    } catch (error) {
      // Part of a batch removal may have succeeded: the list and tags follow whatever the server actually saved.
      if (folders !== state.tagFolders) {
        state.tagFolders = folders; repaintTagFolders();
        void (state.view === "tags" ? loadTags() : refreshTagIndex());
      }
      if (error.status !== 401) status(error.body?.error === "not_directory" ? "Esta pasta não existe mais ou não pode ser aberta."
        : error.status === 413 ? "Limite de 64 pastas monitoradas atingido. Tire uma em Configurações antes de marcar outra."
        : "Não foi possível alterar as pastas monitoradas.", true);
      return false;
    }
    state.tagFolders = folders; repaintTagFolders();
    void (state.view === "tags" ? loadTags() : refreshTagIndex());
    return true;
  }
  async function toggleTagFolder(path, monitored) {
    const address = absolutePath(path);
    if (await setTagFolders([path], monitored)) status(monitored ? `As tags de ${address} passam a ser monitoradas.` : `As tags de ${address} deixaram de ser monitoradas.`);
  }
  async function refreshTagIndex() {
    const requestId = ++state.tagIndexRequest;
    try {
      const result = await request("api/tags");
      if (requestId !== state.tagIndexRequest) return null;
      state.tagIndex = result;
      renderCollections();
      return result;
    } catch (_error) { return null; }
  }
  async function selectTag(tag) {
    state.selectedTag = tag;
    try {
      const result = await request("api/tags", {}, { tag });
      const panel = $("#results"); panel.replaceChildren();
      const heading = el("div", undefined, "busca-cab");
      const tagged = (result.items || []).length;
      heading.append(el("strong", `#${tag}`), el("span", `${formatCount(tagged)} ${tagged === 1 ? "arquivo" : "arquivos"}`, "conta")); panel.append(heading);
      for (const item of result.items || []) {
        const row = el("div", undefined, "res-item");
        row.append(iconNode("file", item.path.split("/").at(-1)));
        const body = el("div", undefined, "res-corpo");
        const open = button(item.path.split("/").at(-1), () => void openMarkedItem(item), "res-nome");
        open.title = absolutePath(item.path);
        body.append(open, el("div", absolutePath(item.path), "res-cam")); row.append(body);
        row.addEventListener("contextmenu", async event => {
          event.preventDefault();
          try {
            const parent = item.path.includes("/") ? item.path.slice(0, item.path.lastIndexOf("/")) : "";
            const listing = await request("api/list", {}, {rootId: item.rootId, path: parent});
            const found = listing.entries.find(entry => entry.name === item.path.split("/").at(-1));
            const type = found?.type || "file";
            const full = !found || (entryUsable(found) && ["file", "directory"].includes(type));
            showItemMenu(event, item.rootId, item.path, type === "file", type, {pathOnly: !full});
          } catch (_error) {
            status("Este item não está mais disponível neste caminho.", true);
            showItemMenu(event, item.rootId, item.path, false, "file", {pathOnly: true});
          }
        });
        panel.append(row);
      }
      if (!(result.items || []).length) panel.append(emptyState("Nenhum arquivo contém esta tag", "busca"));
      renderMetadata(result, {tags: true});
      status(result.complete ? `${formatCount(tagged)} ${tagged === 1 ? "arquivo" : "arquivos"} com #${tag}.` : "A lista está incompleta; consulte as omissões.");
    } catch (_error) { status("Não foi possível abrir esta tag.", true); }
  }

  function searchScope() {
    if (state.searchResult && state.searchScopePath !== undefined) return state.searchScopePath;
    return state.path;
  }
  async function runSearch(cursor = null, {page = 0, again = false, focusFirst = false} = {}) {
    const query = state.searchText.trim();
    if (!query) { status("Digite um termo para buscar.", true); return; }
    const scope = cursor || again ? state.searchScopePath : state.path;
    const searchKey = JSON.stringify([state.searchUiMode, query, scope, cursor]);
    if (!cursor && !again && state.lastSearchKey === searchKey && (state.searchInFlight || state.searchResult)) return;
    state.lastSearchKey = searchKey; state.searchInFlight = true;
    state.selectedForZip.clear(); syncSelectionButtons();
    if (!cursor && state.view !== "files") {
      state.view = "files"; state.selectedTag = ""; state.selectedLabel = ""; state.activeDocument = null;
      renderCollections(); renderBreadcrumbs(); renderTabs();
    }
    const requestId = ++state.searchRequestId;
    status(`Buscando em ${absolutePath(scope)}…`);
    state.searchResult = null;
    try {
      state.searchMode = state.searchUiMode === "text" ? "text" : "name";
      state.searchScopePath = scope;
      const values = { mode: state.searchMode, q: query, scope: absolutePath(scope), limit: 100 };
      if (cursor) values.cursor = cursor;
      const result = await request("api/search", {}, values);
      if (requestId !== state.searchRequestId) return;
      state.searchInFlight = false;
      if (!page) state.searchCursors = [null];
      state.searchPage = page; state.searchCursors[page] = cursor;
      state.searchResult = result;
      renderSearchResults(result);
      if (!result.items.length) $("#results").append(emptyState("Nenhum resultado", "busca"));
      $("#results").scrollTop = 0;
      if (focusFirst) ($("#results .res-item") || $("#results .busca-proxima button"))?.focus({preventScroll: true});
      const skipped = metadataEntries(result.omissions).length > 0;
      const extent = skipped ? "a busca não percorreu tudo; veja os detalhes" : result.nextCursor ? "há mais resultados na próxima página"
        : result.complete ? "busca completa" : "a busca não percorreu tudo; veja os detalhes";
      status(`${formatCount(result.items.length)} ${result.items.length === 1 ? "resultado" : "resultados"} ${page ? `na página ${page + 1}` : "nesta página"} · ${extent}.`);
    } catch (error) {
      if (requestId !== state.searchRequestId) return;
      state.searchInFlight = false; state.lastSearchKey = null;
      status(error.status === 409 ? "O conteúdo mudou. Execute a busca novamente para obter um novo retrato." : error.status === 410 ? "Esta página expirou. Execute a busca novamente." : error.status === 404 ? "A pasta de origem da busca não pode ser aberta." : "A busca não pôde ser concluída.", true);
    }
  }
  function renderSearchResults(result) {
    const panel = $("#results"); panel.replaceChildren();
    const heading = el("div", undefined, "busca-cab");
    const modeLabel = state.searchMode === "text" ? "no conteúdo" : "por nome";
    const resultCount = result.complete ? result.items.length : `${result.items.length}+`;
    heading.append(el("span", `${resultCount} ${resultCount === 1 ? "resultado" : "resultados"} · ${modeLabel} · em ${absolutePath(state.searchScopePath || "")}`, "conta"));
    const clear = button("Limpar", () => {
      state.searchResult = null; state.searchText = ""; state.searchUiMode = "name"; state.localFilter = "";
      state.restoreListScroll = true;
      renderSidebarSearch(); renderNavigation(); renderToolbar(); loadDirectory();
    }, "limpar"); heading.append(clear); panel.append(heading);
      for (const item of result.items) {
        const row = el("div", undefined, "res-item");
        row.tabIndex = 0; row.setAttribute("role", "button");
      row.append(iconNode(item.type === "directory" ? "folder" : item.type === "link" ? "link" : "file", item.path.split("/").at(-1)));
      const body = el("div", undefined, "res-corpo");
      const name = item.path.split("/").at(-1) || "/";
      const open = button(name, () => openEntry(item.rootId, item.path, item.type), "res-nome");
      open.title = absolutePath(item.path);
      body.append(open, el("div", absolutePath(item.path), "res-cam"));
      if (item.line) body.append(el("div", `Linha ${item.line}`, "res-cam"));
      if (item.snippet) body.append(searchSnippet(item.snippet, state.searchText));
      row.append(body);
        row.addEventListener("contextmenu", event => showItemMenu(event, item.rootId, item.path, item.type === "file", item.type, {pathOnly: !["file", "directory"].includes(item.type)}));
        row.addEventListener("click", event => {
          if (event.target.closest("button, a, select, input")) return;
          openEntry(item.rootId, item.path, item.type);
        });
        row.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key) && event.target === row) { event.preventDefault(); openEntry(item.rootId, item.path, item.type); } });
        panel.append(row);
    }
    renderMetadata(result);
    const page = state.searchPage || 0;
    const pager = el("div", undefined, "res-item busca-proxima");
    if (page > 0) pager.append(button("‹ Página anterior", () => void runSearch(state.searchCursors[page - 1], {page: page - 1, again: true, focusFirst: true}), "res-nome"));
    if (result.nextCursor) pager.append(button("Próxima página ›", () => void runSearch(result.nextCursor, {page: page + 1, focusFirst: true}), "res-nome"));
    if (pager.childElementCount) panel.append(pager);
  }
  function searchSnippet(snippet, query) {
    const node = el("div", undefined, "res-trecho");
    const normalized = String(snippet);
    const term = String(query || "").trim();
    const index = term ? normalized.toLocaleLowerCase().indexOf(term.toLocaleLowerCase()) : -1;
    if (index < 0) { node.textContent = normalized; return node; }
    node.append(document.createTextNode(normalized.slice(0, index)));
    const match = el("mark", normalized.slice(index, index + term.length)); node.append(match);
    node.append(document.createTextNode(normalized.slice(index + term.length)));
    return node;
  }
  // For tags, hidden folders, temporary folders, and links are excluded by rule (HF-META-002): the
  // warning only counts what should have been read and was not.
  const TAG_NOTICES = new Set(["unavailable_folder", "unreadable_directory", "unreadable_file", "invalid_name"]);
  function renderMetadata(result, {tags = false} = {}) {
    const old = $("#results .search-metadata"); if (old) old.remove();
    const parts = [];
    const exclusions = metadataEntries(result.exclusions).filter(item => !tags || TAG_NOTICES.has(item.category));
    for (const item of [...metadataEntries(result.omissions), ...exclusions]) {
      parts.push(`${item.count} ${omissionName(item.category, item.count)}`);
    }
    if (!result.complete || parts.length) {
      const lead = tags ? "Nas pastas monitoradas, a indexação não leu" : "A busca não entrou em";
      const notice = el("p", parts.length ? `${lead}: ${parts.join("; ")}.` : "Há mais resultados na próxima página.", "search-metadata");
      notice.setAttribute("role", "status"); $("#results").append(notice);
    }
  }
  function metadataEntries(value) {
    if (!Array.isArray(value)) return [];
    return value.filter((item) => item && typeof item.category === "string" && Number.isSafeInteger(item.count) && item.count > 0);
  }
  function omissionName(key, count) {
    const names = {
      unreadable_directory: ["pasta sem permissão de leitura", "pastas sem permissão de leitura"],
      symbolic_link: ["link simbólico não seguido", "links simbólicos não seguidos"],
      special_file: ["arquivo especial", "arquivos especiais"],
      invalid_name: ["nome que não é UTF-8", "nomes que não são UTF-8"],
      pseudo_filesystem: ["sistema de arquivos virtual não percorrido", "sistemas de arquivos virtuais não percorridos"],
      internal_trash: ["lixeira interna não percorrida", "lixeiras internas não percorridas"],
      entry_race: ["item alterado durante a leitura", "itens alterados durante a leitura"],
      depth_limit: ["limite de profundidade atingido", "limites de profundidade atingidos"],
      invalid_utf8: ["arquivo com UTF-8 inválido", "arquivos com UTF-8 inválido"],
      nul: ["arquivo binário", "arquivos binários"],
      too_large: ["arquivo acima do limite", "arquivos acima do limite"],
      hard_link: ["arquivo com hard link", "arquivos com hard link"],
      protected: ["item protegido", "itens protegidos"],
      read_error: ["erro de leitura", "erros de leitura"],
      execution_budget: ["limite de tempo atingido", "limites de tempo atingidos"],
      result_limit: ["limite de resultados atingido", "limites de resultados atingidos"],
      snapshot_limit: ["limite do retrato atingido", "limites do retrato atingidos"],
      tag_file_limit: ["arquivo Markdown acima do limite", "arquivos Markdown acima do limite"],
      invalid_markdown: ["arquivo Markdown inválido", "arquivos Markdown inválidos"],
      tag_count_limit: ["arquivo acima do limite de tags", "arquivos acima do limite de tags"],
      unavailable_folder: ["pasta monitorada que não existe mais ou não abre", "pastas monitoradas que não existem mais ou não abrem"],
      unreadable_file: ["arquivo sem permissão de leitura", "arquivos sem permissão de leitura"],
      hidden_directory: ["pasta oculta", "pastas ocultas"],
      excluded_path: ["pasta temporária", "pastas temporárias"],
    }[key];
    return names ? names[count === 1 ? 0 : 1] : `${key}:`;
  }

  function applyPreferences() {
    const theme = ["dark", "light"].includes(state.ui.preferences.theme) ? state.ui.preferences.theme : "system";
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.tema = theme === "light" ? "claro" : theme === "dark" ? "escuro" : "sistema";
    const density = state.ui.preferences.density === "compact" ? "compact" : "comfortable";
    document.documentElement.dataset.density = density;
    for (const table of app.querySelectorAll("table.lista")) table.classList.toggle("compacta", density === "compact");
    const themeIcons = {
      system: '<circle cx="8" cy="8" r="5.6"/><path d="M8 2.4a5.6 5.6 0 0 1 0 11.2z" fill="currentColor"/>',
      light: '<circle cx="8" cy="8" r="2.8"/><path d="M8 1.6v1.5M8 12.9v1.5M1.6 8h1.5M12.9 8h1.5M3.5 3.5l1 1M11.5 11.5l1 1M3.5 12.5l1-1M11.5 4.5l1-1"/>',
      dark: '<path d="M13.2 9.4A5.2 5.2 0 0 1 6.6 2.8 5.4 5.4 0 1 0 13.2 9.4z"/>',
    };
    const themeSvg = $("#theme-choice svg");
    if (themeSvg) { themeSvg.setAttribute("viewBox", "0 0 16 16"); themeSvg.innerHTML = themeIcons[theme]; }
    const nextTheme = {system: "claro", light: "escuro", dark: "automático"}[theme];
    $("#theme-choice").setAttribute("aria-label", `Tema ${theme === "system" ? "automático" : theme === "light" ? "claro" : "escuro"}; mudar para ${nextTheme}`);
    $("#theme-choice").dataset.tip = `Tema ${theme === "system" ? "automático" : theme === "light" ? "claro" : "escuro"} · clique para ${nextTheme}`;
    const densityButton = $("#density-choice");
    densityButton.setAttribute("aria-pressed", String(density === "compact"));
    densityButton.dataset.tip = density === "compact" ? "Densidade padrão" : "Densidade compacta";
    densityButton.onclick = () => {
      state.ui.preferences.density = density === "compact" ? "comfortable" : "compact";
      applyPreferences(); renderResults(displayEntries()); scheduleSave();
    };
    const ordering = $("#ordering-choice");
    ordering.value = ["type", "size", "created", "modified"].includes(state.ui.preferences.ordering) ? state.ui.preferences.ordering : "name";
    ordering.onchange = () => {
      state.ui.preferences.ordering = ordering.value;
      state.sortDirection = "asc"; saveListPrefs(); applyPreferences(); renderResults(displayEntries()); scheduleSave();
    };
    const grouping = $("#grouping-choice");
    if (grouping) {
      grouping.value = state.groupBy;
      grouping.onchange = () => { state.groupBy = grouping.value; saveListPrefs(); renderResults(displayEntries()); };
    }
    const direction = $("#sort-direction");
    direction.textContent = state.sortDirection === "desc" ? "↓" : "↑";
    direction.onclick = () => { state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc"; saveListPrefs(); applyPreferences(); renderResults(displayEntries()); };
    const treeOrdering = $("#arv-ordenar");
    if (treeOrdering) {
      treeOrdering.value = state.treeOrdering || "name";
      treeOrdering.onchange = () => { state.treeOrdering = treeOrdering.value; renderNavigation(); };
    }
    const treeGrouping = $("#arv-agrupar");
    if (treeGrouping) {
      treeGrouping.value = state.treeGrouping || "none";
      treeGrouping.onchange = () => { state.treeGrouping = treeGrouping.value; renderNavigation(); };
    }
    const treeDirection = $("#arv-dir");
    if (treeDirection) {
      treeDirection.textContent = state.treeDirection === "desc" ? "↓" : "↑";
      treeDirection.onclick = () => { state.treeDirection = state.treeDirection === "desc" ? "asc" : "desc"; renderNavigation(); applyPreferences(); };
    }
  }

  function scheduleSave() {
    if (!state.ui || state.conflict || state.sessionEnded) return;
    clearTimeout(state.saveRetryTimer); state.saveRetryTimer = null;
    if (state.saveBusy) { state.saveAgain = true; return; }
    state.saveBusy = true;
    state.savePromise = saveLoop().then(() => {
      // After a failure, the red warning clears once a save succeeds.
      if (state.saveFailures && !state.conflict) { state.saveFailures = 0; status("Favoritos, labels e preferências gravados."); }
    }, error => {
      // A server rejection (422) must not stay stuck in state: whatever was not accepted reverts to the
      // last saved state, and subsequent changes are saved normally again.
      if (error?.status === 422 && state.baseUi) {
        state.ui = clone(state.baseUi); applyPreferences(); renderCollections(); renderTabs(); refreshItems();
        status("Não foi possível guardar a última mudança de favoritos, labels ou preferências: um valor não é aceito. Ela foi desfeita.", true);
      } else if (error?.status !== 401) {
        // Transient failure (network, server): the app retries on its own, with increasing backoff,
        // and also when connectivity returns.
        state.saveFailures += 1;
        const delay = Math.min(60, 5 * 2 ** (state.saveFailures - 1)) * 1000;
        state.saveRetryTimer = setTimeout(() => { state.saveRetryTimer = null; scheduleSave(); }, delay);
        status("Não foi possível guardar favoritos, labels e preferências agora. O app tenta de novo sozinho.", true);
      }
    }).finally(() => { state.saveBusy = false; });
  }
  function ignoredTags() { return new Set(state.ui?.preferences.ignoredTags || []); }
  async function saveLoop() {
    do {
      state.saveAgain = false;
      const draft = clone(state.ui);
      const { stateRevision, ...document } = draft;
      try {
        const saved = fromWireUi(await request("api/state", {
          method: "PUT", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
          body: JSON.stringify({ baseRevision: stateRevision, ...toWireUi(document) }),
        }));
        state.baseUi = clone(saved);
        const currentContent = withoutRevision(state.ui);
        const submittedContent = withoutRevision(draft);
        if (same(currentContent, submittedContent)) state.ui = clone(saved);
        else { state.ui.stateRevision = saved.stateRevision; state.saveAgain = true; }
      } catch (error) {
        if (error.status === 409) { await showConflict(); return; }
        throw error;
      }
    } while (state.saveAgain || !same(withoutRevision(state.ui), withoutRevision(state.baseUi)));
  }
  async function showConflict() {
    const open = $("#operation-dialog");
    if (open.open) {
      // If another dialog is open (rename, new folder, upload), the conflict prompt waits for it to
      // close, instead of closing it and canceling what the user was doing.
      if (!state.conflictDeferred) {
        state.conflictDeferred = true;
        onDialogClosed(open, () => { state.conflictDeferred = false; void showConflict(); });
      }
      return;
    }
    let remote;
    try { remote = await fetchUi(); }
    catch (_error) { status("Conflito ao salvar. Sua edição local continua na tela; recarregue para comparar.", true); return; }
    state.conflict = { base: clone(state.baseUi), remote: clone(remote) };
    // A plain dialog instead of cramped buttons in the status bar (HF-META-004 requires the choice).
    const dialog = $("#operation-dialog");
    if (dialog.open) dialog.close();
    const form = el("form"); form.method = "dialog";
    const heading = el("h2", "Favoritos e preferências mudaram em outra aba"); heading.id = "operation-dialog-title";
    const text = el("p", "Outra aba ou aparelho gravou mudanças depois desta. Juntar mantém as mudanças das duas; usar as da outra aba desfaz as desta.");
    const menu = el("menu");
    const useRemote = button("Usar as da outra aba", () => {
      state.ui = clone(state.conflict.remote); state.baseUi = clone(state.conflict.remote); state.conflict = null;
      applyPreferences(); renderCollections(); renderTabs(); refreshItems(); dialog.close(); status("Mudanças da outra aba aplicadas.");
    }, "secondary-button");
    const mergeChanges = () => {
      const { base: before, remote: latest } = state.conflict;
      state.ui = mergeUi(before, state.ui, latest); state.baseUi = clone(latest); state.ui.stateRevision = latest.stateRevision; state.conflict = null;
      applyPreferences(); renderCollections(); renderTabs(); refreshItems();
      status("Mudanças juntadas com as da outra aba."); scheduleSave();
    };
    const merge = button("Juntar as mudanças", () => { mergeChanges(); dialog.close(); }, "primary-button");
    menu.append(useRemote, merge); form.append(heading, text, menu);
    dialog.classList.remove("dlg-largo"); dialog.replaceChildren(form);
    // Closing without choosing (Esc) merges the changes: nothing is lost and saving continues.
    const restore = focusReturn();
    onDialogClosed(dialog, () => { if (state.conflict) mergeChanges(); requestAnimationFrame(restore); });
    dialog.showModal(); merge.focus();
  }
  // The server moves favorites and labels along with the item (HF-META-005); the UI re-reads the
  // state so it does not show a favorite or label on the old path, nor overwrite the old state.
  async function refreshUiFromServer() {
    if (!state.ui || state.conflict) return;
    try {
      await state.savePromise;
      const fresh = await fetchUi();
      const pending = !same(withoutRevision(state.ui), withoutRevision(state.baseUi));
      state.ui = pending ? mergeUi(state.baseUi, state.ui, fresh) : clone(fresh);
      if (pending) state.ui.stateRevision = fresh.stateRevision;
      state.baseUi = clone(fresh);
      renderCollections(); renderTabs(); refreshItems();
      if (pending) scheduleSave();
    } catch (_error) { /* the next save treats this as a conflict */ }
  }
  function same(left, right) { return JSON.stringify(left) === JSON.stringify(right); }
  function withoutRevision(document) {
    const { stateRevision: _revision, ...content } = document;
    return content;
  }
  function mergeUi(before, local, remote) {
    const merged = clone(remote);
    if (!same(before.labels, local.labels)) {
      for (const [id, value] of Object.entries(local.labels)) if (!same(before.labels[id], value)) merged.labels[id] = clone(value);
    }
    if (!same(before.preferences, local.preferences)) {
      // Includes keys removed locally, such as an emptied ignoredTags.
      for (const key of new Set([...Object.keys(before.preferences), ...Object.keys(local.preferences)])) {
        if (same(before.preferences[key], local.preferences[key])) continue;
        if (key in local.preferences) merged.preferences[key] = clone(local.preferences[key]);
        else delete merged.preferences[key];
      }
    }
    if (!same(before.items, local.items)) merged.items = mergeRows(before.items, local.items, remote.items, (item) => `${item.rootId}\u0000${item.path}`);
    if (!same(before.tabs, local.tabs)) merged.tabs = mergeRows(before.tabs, local.tabs, remote.tabs, (tab) => `${tab.rootId}\u0000${tab.path}`);
    return merged;
  }
  function mergeRows(before, local, remote, keyOf) {
    const old = new Map(before.map((value) => [keyOf(value), value]));
    const localMap = new Map(local.map((value) => [keyOf(value), value]));
    const result = new Map(remote.map((value) => [keyOf(value), clone(value)]));
    for (const [key, value] of old) if (!localMap.has(key)) result.delete(key);
    for (const [key, value] of localMap) if (!old.has(key) || !same(old.get(key), value)) result.set(key, clone(value));
    return [...result.values()];
  }

  initialize();
})();
