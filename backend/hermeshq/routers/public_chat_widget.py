"""Embeddable public chat widget served as a self-contained JS file."""

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter(tags=["public-chat-widget"])

WIDGET_JS = r"""(function(){
'use strict';

var script = document.currentScript || (function() {
  var scripts = document.querySelectorAll('script[data-api-key]');
  return scripts[scripts.length - 1];
})();
var API_KEY = script ? script.getAttribute('data-api-key') || '' : '';
var BASE = script ? (script.getAttribute('data-base-url') || script.src.replace(/\/api\/public\/chat\/widget\.js.*/, '')) : '';
var THEME = script ? script.getAttribute('data-theme') || 'auto' : 'auto';
var ACCENT = script ? script.getAttribute('data-accent-color') || '#6366f1' : '#6366f1';
var POSITION = script ? script.getAttribute('data-position') || 'right' : 'right';
var TITLE = script ? script.getAttribute('data-title') || '' : '';

var sessionId = null;
var sessionToken = null;
var agentName = '';
var sending = false;
var inactivityTimer = null;
var hostEl, shadowRoot, fab, widget, container;

function hexToRgb(hex) {
  var r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
  return r+','+g+','+b;
}

var CSS = (function(){
  var pos = POSITION === 'left' ? 'left: 20px;' : 'right: 20px;';
  var posChat = POSITION === 'left' ? 'left: 20px;' : 'right: 20px;';
  var rgb = hexToRgb(ACCENT);
  return '\
:host { all: initial; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; font-size: 14px; line-height: 1.5; }\
*,:before,:after { box-sizing: border-box; margin: 0; padding: 0; }\
\
.hq-fab {\
  position: fixed; bottom: 20px; ' + pos + ' width: 56px; height: 56px;\
  border-radius: 28px; border: none; cursor: pointer; z-index: 2147483646;\
  display: flex; align-items: center; justify-content: center;\
  background: ' + ACCENT + '; color: #fff; font-size: 24px;\
  box-shadow: 0 4px 12px rgba(' + rgb + ',.4), 0 1px 3px rgba(0,0,0,.1);\
  transition: transform .2s cubic-bezier(.4,0,.2,1), box-shadow .2s cubic-bezier(.4,0,.2,1), opacity .2s;\
  opacity: 1;\
}\
.hq-fab:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(' + rgb + ',.5), 0 2px 6px rgba(0,0,0,.12); }\
.hq-fab:active { transform: translateY(0) scale(.95); }\
.hq-fab.hidden { opacity: 0; pointer-events: none; transform: scale(.8); }\
.hq-fab svg { width: 26px; height: 26px; fill: currentColor; }\
\
.hq-widget {\
  position: fixed; bottom: 20px; ' + posChat + ' width: 380px; max-width: calc(100vw - 32px);\
  height: 520px; max-height: calc(100vh - 40px); border-radius: 16px;\
  display: flex; flex-direction: column; overflow: hidden; z-index: 2147483647;\
  opacity: 0; pointer-events: none;\
  transform: translateY(12px) scale(.96);\
  transition: opacity .25s cubic-bezier(.4,0,.2,1), transform .25s cubic-bezier(.4,0,.2,1);\
}\
.hq-widget.open { opacity: 1; pointer-events: auto; transform: translateY(0) scale(1); }\
\
@media (max-width: 480px) {\
  .hq-widget { top: 0; left: 0; right: 0; bottom: 0; width: 100%; max-width: 100%;\
    height: 100%; max-height: 100%; border-radius: 0; transform: translateY(100%); }\
  .hq-widget.open { transform: translateY(0); }\
  .hq-fab { bottom: 16px; ' + pos.replace('20px', '16px') + ' width: 52px; height: 52px; }\
}\
\
.hq-header {\
  padding: 14px 16px; display: flex; align-items: center; justify-content: space-between;\
  background: ' + ACCENT + '; color: #fff; flex-shrink: 0;\
}\
.hq-header-info { display: flex; align-items: center; gap: 10px; min-width: 0; }\
.hq-header-avatar {\
  width: 34px; height: 34px; border-radius: 50%; background: rgba(255,255,255,.18);\
  display: flex; align-items: center; justify-content: center; font-size: 16px; flex-shrink: 0;\
}\
.hq-header-text { min-width: 0; }\
.hq-header-name { font-weight: 600; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }\
.hq-header-status { font-size: 11px; opacity: .75; display: flex; align-items: center; gap: 4px; }\
.hq-header-status::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: #4ade80; flex-shrink: 0; }\
.hq-header-actions { display: flex; gap: 2px; flex-shrink: 0; }\
.hq-header-actions button {\
  background: transparent; border: none; color: rgba(255,255,255,.8); width: 32px; height: 32px;\
  border-radius: 8px; cursor: pointer; font-size: 16px; display: flex; align-items: center;\
  justify-content: center; transition: background .15s, color .15s;\
}\
.hq-header-actions button:hover { background: rgba(255,255,255,.15); color: #fff; }\
.hq-header-actions button svg { width: 16px; height: 16px; fill: currentColor; }\
\
.hq-messages {\
  flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 6px;\
  scroll-behavior: smooth;\
}\
.hq-messages::-webkit-scrollbar { width: 4px; }\
.hq-messages::-webkit-scrollbar-track { background: transparent; }\
.hq-messages::-webkit-scrollbar-thumb { border-radius: 2px; }\
\
.hq-msg {\
  max-width: 80%; padding: 10px 14px; border-radius: 18px; font-size: 13.5px;\
  line-height: 1.5; word-wrap: break-word; white-space: pre-wrap; position: relative;\
  animation: hqFadeIn .2s ease;\
}\
@keyframes hqFadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }\
.hq-msg-user {\
  align-self: flex-end; border-bottom-right-radius: 6px;\
  background: ' + ACCENT + '; color: #fff;\
}\
.hq-msg-assistant {\
  align-self: flex-start; border-bottom-left-radius: 6px;\
}\
.hq-msg-system {\
  align-self: center; font-size: 12px; border-radius: 12px; padding: 6px 14px;\
  text-align: center;\
}\
.hq-msg-time {\
  font-size: 10px; opacity: .45; margin-top: 3px;\
}\
.hq-msg-user .hq-msg-time { text-align: right; }\
\
.hq-attachment {\
  display: flex; align-items: center; gap: 8px; margin-top: 8px; padding: 8px 12px;\
  border-radius: 10px; background: rgba(' + rgb + ',.08); text-decoration: none;\
  color: inherit; font-size: 12.5px;\
}\
.hq-attachment:hover { background: rgba(' + rgb + ',.15); }\
.hq-attachment-icon { font-size: 16px; }\
.hq-attachment-name { font-weight: 600; }\
.hq-attachment-size { opacity: .55; font-size: 11px; }\
\
.hq-typing { align-self: flex-start; padding: 12px 16px; }\
.hq-typing-dots { display: flex; gap: 5px; }\
.hq-typing-dots span {\
  width: 7px; height: 7px; border-radius: 50%; animation: hqBounce 1.4s ease-in-out infinite;\
}\
.hq-typing-dots span:nth-child(2) { animation-delay: .2s; }\
.hq-typing-dots span:nth-child(3) { animation-delay: .4s; }\
@keyframes hqBounce {\
  0%,60%,100% { transform: translateY(0); opacity: .35; }\
  30% { transform: translateY(-5px); opacity: 1; }\
}\
\
.hq-input-area {\
  padding: 10px 12px; display: flex; gap: 8px; align-items: flex-end; flex-shrink: 0;\
}\
.hq-input {\
  flex: 1; padding: 10px 14px; border-radius: 22px; font-size: 13.5px;\
  outline: none; font-family: inherit; resize: none; line-height: 1.4;\
  transition: border-color .15s, box-shadow .15s;\
}\
.hq-input::placeholder { opacity: .5; }\
.hq-input:focus { border-color: ' + ACCENT + '; box-shadow: 0 0 0 3px rgba(' + rgb + ',.12); }\
.hq-send {\
  width: 38px; height: 38px; border-radius: 50%; border: none; cursor: pointer;\
  display: flex; align-items: center; justify-content: center;\
  transition: background .15s, transform .1s;\
  flex-shrink: 0; background: ' + ACCENT + '; color: #fff;\
}\
.hq-send:hover:not(:disabled) { filter: brightness(1.1); }\
.hq-send:active:not(:disabled) { transform: scale(.92); }\
.hq-send:disabled { opacity: .35; cursor: default; }\
.hq-send svg { width: 16px; height: 16px; fill: currentColor; }\
\
.hq-powered {\
  text-align: center; padding: 5px; font-size: 10px; flex-shrink: 0;\
}\
\
.hq-reconnect {\
  text-align: center; padding: 24px 16px; display: flex; flex-direction: column; align-items: center;\
  gap: 14px; flex: 1; justify-content: center;\
}\
.hq-reconnect p { font-size: 13px; }\
.hq-reconnect button {\
  padding: 10px 24px; border-radius: 22px; border: none; cursor: pointer;\
  font-size: 13px; font-family: inherit; background: ' + ACCENT + '; color: #fff;\
  display: flex; align-items: center; gap: 6px;\
  transition: filter .15s;\
}\
.hq-reconnect button:hover { filter: brightness(1.08); }\
.hq-reconnect button svg { width: 14px; height: 14px; fill: currentColor; }\
\
/* Light theme */\
.hq-theme-light .hq-widget { background: #ffffff; box-shadow: 0 8px 40px rgba(0,0,0,.12), 0 0 0 1px rgba(0,0,0,.06); }\
.hq-theme-light .hq-messages { background: #f7f8fa; }\
.hq-theme-light .hq-messages::-webkit-scrollbar-thumb { background: rgba(0,0,0,.12); }\
.hq-theme-light .hq-msg-assistant { background: #fff; color: #1a1d23; box-shadow: 0 1px 2px rgba(0,0,0,.06); }\
.hq-theme-light .hq-msg-system { background: #eef0f4; color: #6b7280; }\
.hq-theme-light .hq-typing-dots span { background: #b0b5bf; }\
.hq-theme-light .hq-input-area { background: #fff; border-top: 1px solid #eef0f4; }\
.hq-theme-light .hq-input { background: #f3f4f6; border: 1.5px solid #e5e7eb; color: #1a1d23; }\
.hq-theme-light .hq-powered { color: #b0b5bf; background: #fff; }\
.hq-theme-light .hq-reconnect { color: #6b7280; }\
\
/* Dark theme */\
.hq-theme-dark .hq-widget { background: #1c1f2e; box-shadow: 0 8px 40px rgba(0,0,0,.35), 0 0 0 1px rgba(255,255,255,.06); }\
.hq-theme-dark .hq-messages { background: #171a27; }\
.hq-theme-dark .hq-messages::-webkit-scrollbar-thumb { background: rgba(255,255,255,.1); }\
.hq-theme-dark .hq-msg-assistant { background: #242839; color: #e1e3ea; box-shadow: 0 1px 2px rgba(0,0,0,.2); }\
.hq-theme-dark .hq-msg-system { background: #242839; color: #8b90a0; }\
.hq-theme-dark .hq-typing-dots span { background: #5a5f73; }\
.hq-theme-dark .hq-input-area { background: #1c1f2e; border-top: 1px solid #2a2e40; }\
.hq-theme-dark .hq-input { background: #242839; border: 1.5px solid #333752; color: #e1e3ea; }\
.hq-theme-dark .hq-powered { color: #4a4f63; background: #1c1f2e; }\
.hq-theme-dark .hq-reconnect { color: #a0a4b4; }\
';
})();

function resolveTheme() {
  if (THEME === 'dark') return 'dark';
  if (THEME === 'light') return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function timeAgo(d) {
  var s = Math.floor((Date.now() - d) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return new Date(d).toLocaleDateString();
}

function esc(t) {
  var d = document.createElement('div'); d.textContent = t; return d.innerHTML;
}

var SVG_CHAT = '<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H5.2L4 17.2V4h16v12z"/></svg>';
var SVG_CLOSE = '<svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>';
var SVG_SEND = '<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>';
var SVG_REFRESH = '<svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.96 7.96 0 0012 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>';
var SVG_TRASH = '<svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>';

function init() {
  if (!API_KEY) { console.warn('[HermesHQ] data-api-key is required'); return; }

  hostEl = document.createElement('div');
  hostEl.id = 'hermeshq-chat-widget';
  document.body.appendChild(hostEl);
  shadowRoot = hostEl.attachShadow({ mode: 'closed' });

  var style = document.createElement('style');
  style.textContent = CSS;
  shadowRoot.appendChild(style);

  var themeClass = 'hq-theme-' + resolveTheme();
  container = document.createElement('div');
  container.className = themeClass;
  shadowRoot.appendChild(container);

  if (THEME === 'auto') {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e) {
      container.className = 'hq-theme-' + (e.matches ? 'dark' : 'light');
    });
  }

  fab = document.createElement('button');
  fab.className = 'hq-fab';
  fab.innerHTML = SVG_CHAT;
  fab.setAttribute('aria-label', 'Open chat');
  fab.addEventListener('click', toggleChat);
  container.appendChild(fab);

  widget = document.createElement('div');
  widget.className = 'hq-widget';
  widget.setAttribute('role', 'dialog');
  widget.setAttribute('aria-label', 'Chat');
  widget.innerHTML = '\
<div class="hq-header">\
  <div class="hq-header-info">\
    <div class="hq-header-avatar">\u{1F916}</div>\
    <div class="hq-header-text"><div class="hq-header-name">' + esc(TITLE || 'Assistant') + '</div>\
    <div class="hq-header-status">Online</div></div>\
  </div>\
  <div class="hq-header-actions">\
    <button class="hq-btn-clear" title="New conversation">' + SVG_TRASH + '</button>\
    <button class="hq-btn-close" title="Close">' + SVG_CLOSE + '</button>\
  </div>\
</div>\
<div class="hq-messages"></div>\
<div class="hq-input-area">\
  <input class="hq-input" type="text" placeholder="Type a message…" aria-label="Message">\
  <button class="hq-send" disabled aria-label="Send">' + SVG_SEND + '</button>\
</div>\
<div class="hq-powered">Powered by HermesHQ</div>';
  container.appendChild(widget);

  widget.querySelector('.hq-btn-close').addEventListener('click', closeChat);
  widget.querySelector('.hq-btn-clear').addEventListener('click', clearChat);

  var input = widget.querySelector('.hq-input');
  var sendBtn = widget.querySelector('.hq-send');
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); }
  });
  input.addEventListener('input', function() {
    sendBtn.disabled = !input.value.trim() || sending;
  });
  sendBtn.addEventListener('click', doSend);

  window.addEventListener('beforeunload', destroySession);
}

function toggleChat() {
  if (widget.classList.contains('open')) {
    closeChat();
  } else {
    openChat();
  }
}

function openChat() {
  fab.classList.add('hidden');
  widget.classList.add('open');
  widget.querySelector('.hq-input').focus();
  if (!sessionId) createSession();
}

function closeChat() {
  widget.classList.remove('open');
  fab.classList.remove('hidden');
}

function clearChat() {
  destroySession();
  widget.querySelector('.hq-messages').innerHTML = '';
  createSession();
}

function createSession() {
  var msgs = widget.querySelector('.hq-messages');
  msgs.innerHTML = '';

  fetch(BASE + '/api/public/chat/sessions', {
    method: 'POST',
    headers: { 'X-Api-Key': API_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({})
  }).then(function(res) {
    if (!res.ok) return res.json().then(function(e) { throw new Error(e.detail || res.statusText); });
    return res.json();
  }).then(function(data) {
    sessionId = data.session_id;
    sessionToken = data.session_token;
    agentName = data.agent_name;
    widget.querySelector('.hq-header-name').textContent = TITLE || agentName;
    addMsg('system', 'Session started. How can I help you?');
    resetInactivity();
  }).catch(function(err) {
    addMsg('system', 'Connection error: ' + err.message);
  });
}

function doSend() {
  if (sending) return;
  var input = widget.querySelector('.hq-input');
  var content = input.value.trim();
  if (!content || !sessionId) return;

  input.value = '';
  widget.querySelector('.hq-send').disabled = true;
  addMsg('user', content);
  resetInactivity();
  sending = true;

  var typingEl = showTyping();

  fetch(BASE + '/api/public/chat/sessions/' + sessionId + '/messages', {
    method: 'POST',
    headers: { 'X-Session-Token': sessionToken, 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: content })
  }).then(function(res) {
    if (!res.ok) return res.json().then(function(e) { throw new Error(e.detail || res.statusText); });
    return readSSE(res, typingEl);
  }).catch(function(err) {
    removeTyping(typingEl);
    if (err.message && err.message.indexOf('Rate limit') >= 0) {
      addMsg('system', 'Please wait a moment before sending another message.');
    } else {
      addMsg('system', 'Error: ' + err.message);
    }
  }).finally(function() {
    sending = false;
    var input2 = widget.querySelector('.hq-input');
    widget.querySelector('.hq-send').disabled = !input2.value.trim();
    resetInactivity();
  });
}

function readSSE(res, typingEl) {
  var reader = res.body.getReader();
  var decoder = new TextDecoder();
  var buffer = '';
  var assistantContent = '';
  var msgEl = null;

  function process(result) {
    if (result.done) {
      removeTyping(typingEl);
      if (!msgEl && !assistantContent) addMsg('assistant', '(no response)');
      return;
    }
    buffer += decoder.decode(result.value, { stream: true });
    var lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.indexOf('data: ') !== 0) continue;
      try {
        var data = JSON.parse(line.substring(6));
        if (data.type === 'stream') {
          if (!msgEl) { removeTyping(typingEl); msgEl = addMsg('assistant', ''); }
          assistantContent += data.content;
          msgEl.querySelector('.hq-msg-text').textContent = assistantContent;
          scrollBottom();
        } else if (data.type === 'done') {
          removeTyping(typingEl);
          // Strip trailing "MEDIA: uploads/<uuid>.ext" marker lines — the
          // backend leaves these for plain-text-only consumers (e.g. the
          // Teams bridge) to regex out, but this widget already shows the
          // file as a proper attachment card below, so the raw marker is
          // just noise here.
          var displayContent = (data.content || '').replace(/\n?MEDIA:\s*uploads\/[0-9a-f-]+\.\w+\s*$/gim, '').replace(/\s+$/, '');
          if (!msgEl) {
            msgEl = addMsg('assistant', displayContent);
          } else {
            // The server-side cleanup (stripping MEDIA:/sandbox: references,
            // now this marker too) only applies to the final response text —
            // streamed chunks shown live are the pre-cleanup text. Sync the
            // bubble to the cleaned final version now that it's available.
            msgEl.querySelector('.hq-msg-text').textContent = displayContent;
          }
          if (data.attachments && data.attachments.length) {
            for (var ai = 0; ai < data.attachments.length; ai++) {
              addAttachment(msgEl, data.attachments[ai]);
            }
          }
          return;
        } else if (data.type === 'error') {
          removeTyping(typingEl);
          addMsg('system', data.content || 'An error occurred.');
          return;
        } else if (data.type === 'timeout') {
          removeTyping(typingEl);
          addMsg('system', 'The agent did not respond in time.');
          return;
        }
      } catch(e) {}
    }
    return reader.read().then(process);
  }
  return reader.read().then(process);
}

function addMsg(role, text) {
  var msgs = widget.querySelector('.hq-messages');
  var el = document.createElement('div');
  el.className = 'hq-msg hq-msg-' + role;

  var textEl = document.createElement('span');
  textEl.className = 'hq-msg-text';
  textEl.textContent = text;
  el.appendChild(textEl);

  if (role !== 'system') {
    var timeEl = document.createElement('div');
    timeEl.className = 'hq-msg-time';
    timeEl.textContent = timeAgo(Date.now());
    el.appendChild(timeEl);
  }

  msgs.appendChild(el);
  scrollBottom();
  return el;
}

function formatBytes(n) {
  if (!n || n < 1024) return (n || 0) + ' B';
  if (n < 1024 * 1024) return Math.round(n / 1024) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

function addAttachment(msgEl, att) {
  if (!att || !att.file_id) return;
  var url = BASE + '/api/public/chat/sessions/' + sessionId + '/attachments/' + att.file_id +
    '?token=' + encodeURIComponent(sessionToken);
  var link = document.createElement('a');
  link.className = 'hq-attachment';
  link.href = url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';

  var icon = document.createElement('span');
  icon.className = 'hq-attachment-icon';
  icon.textContent = '📄';
  link.appendChild(icon);

  var meta = document.createElement('span');
  var nameEl = document.createElement('span');
  nameEl.className = 'hq-attachment-name';
  nameEl.textContent = att.filename || 'archivo';
  meta.appendChild(nameEl);
  if (att.size) {
    var sizeEl = document.createElement('span');
    sizeEl.className = 'hq-attachment-size';
    sizeEl.textContent = ' · ' + formatBytes(att.size);
    meta.appendChild(sizeEl);
  }
  link.appendChild(meta);

  var timeEl = msgEl.querySelector('.hq-msg-time');
  if (timeEl) {
    msgEl.insertBefore(link, timeEl);
  } else {
    msgEl.appendChild(link);
  }
  scrollBottom();
}

function showTyping() {
  var msgs = widget.querySelector('.hq-messages');
  var el = document.createElement('div');
  el.className = 'hq-msg hq-msg-assistant hq-typing';
  el.innerHTML = '<div class="hq-typing-dots"><span></span><span></span><span></span></div>';
  msgs.appendChild(el);
  scrollBottom();
  return el;
}

function removeTyping(el) {
  if (el && el.parentNode) el.parentNode.removeChild(el);
}

function scrollBottom() {
  var msgs = widget.querySelector('.hq-messages');
  if (msgs) msgs.scrollTop = msgs.scrollHeight;
}

function destroySession() {
  clearTimeout(inactivityTimer);
  if (sessionId && sessionToken) {
    var body = JSON.stringify({ session_token: sessionToken });
    navigator.sendBeacon(
      BASE + '/api/public/chat/sessions/' + sessionId + '/close',
      new Blob([body], { type: 'application/json' })
    );
  }
  sessionId = null;
  sessionToken = null;
}

function resetInactivity() {
  clearTimeout(inactivityTimer);
  inactivityTimer = setTimeout(function() {
    destroySession();
    var msgs = widget.querySelector('.hq-messages');
    if (msgs) {
      msgs.innerHTML = '';
      var reconnect = document.createElement('div');
      reconnect.className = 'hq-reconnect';
      reconnect.innerHTML = '<p>Session expired due to inactivity.</p>\
        <button>' + SVG_REFRESH + ' New conversation</button>';
      reconnect.querySelector('button').addEventListener('click', function() {
        msgs.innerHTML = '';
        createSession();
      });
      msgs.appendChild(reconnect);
    }
  }, 5 * 60 * 1000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

})();"""


@router.get("/api/public/chat/widget.js")
async def serve_widget():
    return Response(
        content=WIDGET_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )
