(function () {
  'use strict';

  // Resolve API URL from script tag attribute, or auto-detect from script src origin
  var _script = document.currentScript || (function () {
    var scripts = document.getElementsByTagName('script');
    return scripts[scripts.length - 1];
  })();
  var _apiUrl = (_script && _script.getAttribute('data-api-url')) ||
    ((_script && _script.src ? new URL(_script.src).origin : '') + '/widget-chat');
  var _pos = (_script && _script.getAttribute('data-position') === 'left') ? 'left' : 'right';
  // ── Talep oluşturma sayfası (AUZEF Çözüm Merkezi) — URL hazır olunca buraya yazın ──
  var _solutionUrl = (_script && _script.getAttribute('data-solution-url')) || 'https://cozummerkeziauzef.istanbul.edu.tr/student/sign-in';

  // ── Mount shadow root ───────────────────────────────────────────────────────
  var host = document.createElement('div');
  host.id = 'auzef-widget-host';
  document.body.appendChild(host);
  var shadow = host.attachShadow({ mode: 'open' });

  // ── Styles ──────────────────────────────────────────────────────────────────
  var style = document.createElement('style');
  style.textContent = [
    '*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}',

    // FAB button
    '.fab{position:fixed;bottom:24px;' + _pos + ':24px;width:58px;height:58px;border-radius:50%;',
    'background:linear-gradient(135deg,#1e3a8a,#1e40af);color:#fff;border:none;cursor:pointer;',
    'box-shadow:0 4px 20px rgba(30,58,138,.5);display:flex;align-items:center;',
    'justify-content:center;transition:transform .2s,box-shadow .2s;z-index:2147483646;}',
    '.fab:hover{transform:scale(1.08);box-shadow:0 6px 28px rgba(30,58,138,.65);}',
    '.fab svg{width:26px;height:26px;}',

    // Popup
    '.popup{position:fixed;bottom:96px;' + _pos + ':24px;width:360px;height:520px;',
    'background:#fff;border-radius:16px;box-shadow:0 16px 56px rgba(0,0,0,.18);',
    'display:flex;flex-direction:column;overflow:hidden;z-index:2147483645;',
    'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;',
    'transform-origin:bottom ' + _pos + ';transition:transform .25s cubic-bezier(.34,1.56,.64,1),opacity .2s;}',
    '.popup.hidden{transform:scale(.85) translateY(16px);opacity:0;pointer-events:none;}',

    // Header
    '.header{background:linear-gradient(135deg,#1e3a8a,#1e40af);padding:14px 16px;',
    'display:flex;align-items:center;gap:10px;flex-shrink:0;}',
    '.avatar{width:36px;height:36px;border-radius:50%;background:rgba(255,255,255,.2);',
    'display:flex;align-items:center;justify-content:center;flex-shrink:0;}',
    '.avatar svg{width:20px;height:20px;}',
    '.header-info{flex:1;min-width:0;}',
    '.header-title{font-size:14px;font-weight:700;color:#fff;}',
    '.header-sub{font-size:11px;color:rgba(255,255,255,.75);display:flex;align-items:center;gap:5px;}',
    '.dot-online{width:6px;height:6px;border-radius:50%;background:#4ade80;}',
    '.close-btn{width:28px;height:28px;border:none;background:rgba(255,255,255,.15);',
    'border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;',
    'color:#fff;flex-shrink:0;transition:background .15s;}',
    '.close-btn:hover{background:rgba(255,255,255,.28);}',
    '.close-btn svg{width:14px;height:14px;}',

    // Messages
    '.messages{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;',
    'gap:10px;background:#f8f7ff;}',
    '.messages::-webkit-scrollbar{width:4px;}',
    '.messages::-webkit-scrollbar-track{background:transparent;}',
    '.messages::-webkit-scrollbar-thumb{background:#bfdbfe;border-radius:4px;}',
    '.welcome{text-align:center;color:#9b8ec4;font-size:12px;margin:6px 0;}',

    // Message bubbles
    '.msg{display:flex;flex-direction:column;max-width:86%;}',
    '.msg.user{align-self:flex-end;align-items:flex-end;}',
    '.msg.bot{align-self:flex-start;align-items:flex-start;}',
    '.bubble{padding:9px 13px;border-radius:14px;font-size:13px;line-height:1.6;',
    'word-break:break-word;white-space:pre-wrap;}',
    '.msg.user .bubble{background:#1e3a8a;color:#fff;border-bottom-right-radius:3px;}',
    '.msg.bot .bubble{background:#fff;color:#1e1b3a;border-bottom-left-radius:3px;',
    'box-shadow:0 1px 4px rgba(0,0,0,.09);}',
    '.ts{font-size:10px;color:#a09ab8;margin-top:3px;}',

    // Typing dots
    '.typing{display:flex;gap:4px;align-items:center;padding:2px;}',
    '.tdot{width:7px;height:7px;border-radius:50%;background:#93c5fd;animation:tdot-bounce 1.2s infinite;}',
    '.tdot:nth-child(2){animation-delay:.2s}.tdot:nth-child(3){animation-delay:.4s}',
    '@keyframes tdot-bounce{0%,80%,100%{transform:translateY(0)}40%{transform:translateY(-6px)}}',

    // Input area
    '.input-area{padding:10px 12px;display:flex;align-items:flex-end;gap:8px;',
    'border-top:1px solid #dbeafe;background:#fff;flex-shrink:0;}',
    '.input-area textarea{flex:1;resize:none;border:1.5px solid #e9e0fd;border-radius:10px;',
    'padding:8px 10px;font-size:13px;font-family:inherit;color:#1e1b3a;background:#faf9ff;',
    'outline:none;max-height:96px;overflow-y:auto;line-height:1.5;transition:border-color .15s;}',
    '.input-area textarea::placeholder{color:#b0a8cc;}',
    '.input-area textarea:focus{border-color:#1e3a8a;}',
    '.send-btn{width:36px;height:36px;flex-shrink:0;border:none;background:#1e3a8a;color:#fff;',
    'border-radius:10px;cursor:pointer;display:flex;align-items:center;justify-content:center;',
    'transition:background .15s,transform .1s;}',
    '.send-btn:hover{background:#1e40af}.send-btn:active{transform:scale(.92)}',
    '.send-btn:disabled{background:#93c5fd;cursor:not-allowed;transform:none}',
    '.send-btn svg{width:16px;height:16px}',
    '.spinner{width:14px;height:14px;border:2px solid rgba(255,255,255,.4);',
    'border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;}',
    '@keyframes spin{to{transform:rotate(360deg)}}',

    // Links inside bot answers
    '.chat-link{color:#1e3a8a;text-decoration:underline;word-break:break-all;}',
    '.chat-link:hover{color:#1e40af;}',

    // Feedback
    '.feedback{display:flex;flex-direction:column;gap:6px;padding:4px 0 2px;}',
    '.fb-q{font-size:11px;color:#64748b;margin:0;}',
    '.fb-btns{display:flex;gap:6px;}',
    '.fb-btn{padding:4px 14px;border-radius:20px;border:1.5px solid;font-size:12px;',
    'cursor:pointer;font-family:inherit;transition:background .15s;}',
    '.fb-btn.yes{border-color:#86efac;background:#f0fdf4;color:#15803d;}',
    '.fb-btn.yes:hover{background:#dcfce7;}',
    '.fb-btn.no{border-color:#fca5a5;background:#fff1f2;color:#b91c1c;}',
    '.fb-btn.no:hover{background:#fee2e2;}',

    // Suggestion chips
    '.suggestions{display:flex;flex-direction:column;gap:6px;padding:6px 0 2px;}',
    '.chip{background:#dbeafe;color:#1e40af;border:1px solid #bfdbfe;border-radius:20px;',
    'padding:7px 14px;font-size:12px;cursor:pointer;font-family:inherit;',
    'text-align:left;width:100%;transition:background .15s,transform .1s;}',
    '.chip:hover{background:#bfdbfe;transform:scale(1.02);}',
    '.suggestions-nav{display:flex;gap:6px;padding:6px 0 2px;}',
    '.chip-next{background:#f1f5f9;color:#475569;border:1.5px solid #cbd5e1;border-radius:20px;',
    'padding:4px 12px;font-size:12px;font-style:normal;cursor:pointer;font-family:inherit;',
    'text-decoration:none;transition:background .15s;}',
    '.chip-next:hover{background:#e2e8f0;}',

    // Stars
    '.stars{display:flex;gap:2px;padding:2px 0;}',
    '.star{font-size:22px;color:#d1d5db;background:none;border:none;cursor:pointer;',
    'padding:0 1px;transition:color .1s;line-height:1;font-family:inherit;}',
    '.star.lit{color:#f59e0b;}',

    // Responsive
    '@media(max-width:420px){',
    '.popup{width:calc(100vw - 16px);' + _pos + ':8px;bottom:84px;}',
    '.fab{' + _pos + ':12px;bottom:12px;}}'
  ].join('');

  // ── HTML ────────────────────────────────────────────────────────────────────
  var wrap = document.createElement('div');
  wrap.innerHTML =
    '<button class="fab" id="w-fab" aria-label="AUZEF Asistanı Aç">' +
      '<svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8">' +
        '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>' +
      '</svg>' +
    '</button>' +
    '<div class="popup hidden" id="w-popup">' +
      '<div class="header">' +
        '<div class="avatar">' +
          '<svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8" style="color:#fff">' +
            '<path stroke-linecap="round" stroke-linejoin="round" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-5l-3 3-3-3z"/>' +
          '</svg>' +
        '</div>' +
        '<div class="header-info">' +
          '<div class="header-title">AUZEF Asistan</div>' +
          '<div class="header-sub"><span class="dot-online"></span>Çevrimiçi</div>' +
        '</div>' +
        '<button class="close-btn" id="w-close" aria-label="Kapat">' +
          '<svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">' +
            '<path d="M18 6 6 18M6 6l12 12"/>' +
          '</svg>' +
        '</button>' +
      '</div>' +
      '<div class="messages" id="w-messages">' +
        '<p class="welcome">Merhaba! Size nasıl yardımcı olabilirim?</p>' +
      '</div>' +
      '<div class="input-area">' +
        '<textarea id="w-input" rows="1" placeholder="Sorunuzu yazın..." aria-label="Mesaj"></textarea>' +
        '<button class="send-btn" id="w-send" aria-label="Gönder">' +
          '<svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">' +
            '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9"/>' +
          '</svg>' +
        '</button>' +
      '</div>' +
    '</div>';

  shadow.appendChild(style);
  shadow.appendChild(wrap);

  // ── References ──────────────────────────────────────────────────────────────
  var fab       = shadow.getElementById('w-fab');
  var popup     = shadow.getElementById('w-popup');
  var closeBtn  = shadow.getElementById('w-close');
  var messages  = shadow.getElementById('w-messages');
  var input     = shadow.getElementById('w-input');
  var sendBtn   = shadow.getElementById('w-send');

  var isOpen   = false;
  var isSending = false;

  // ── Toggle ──────────────────────────────────────────────────────────────────
  function toggle() {
    isOpen = !isOpen;
    popup.classList.toggle('hidden', !isOpen);
    if (isOpen) { setTimeout(function () { input.focus(); }, 250); scrollEnd(); }
  }
  fab.addEventListener('click', toggle);
  closeBtn.addEventListener('click', toggle);

  // ── Auto-resize textarea ────────────────────────────────────────────────────
  input.addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 96) + 'px';
  });

  // ── Send on Enter ───────────────────────────────────────────────────────────
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  sendBtn.addEventListener('click', send);

  // ── Send message ────────────────────────────────────────────────────────────
  function send() {
    var text = input.value.trim();
    if (!text || isSending) return;

    appendMsg('user', text);
    input.value = '';
    input.style.height = 'auto';

    var typingEl = appendTyping();
    isSending = true;
    setSendState(true);

    fetch(_apiUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text })
    })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (data) {
        typingEl.remove();
        var answer = data.answer || 'Yanıt alınamadı.';
        appendBotMsg(answer, function () {
          if (data.suggestions && data.suggestions.length) {
            appendSuggestions(data.suggestions);
          } else {
            appendFeedback();
          }
        });
      })
      .catch(function () {
        typingEl.remove();
        appendMsg('bot', 'Bağlantı hatası. Lütfen tekrar deneyin.');
      })
      .then(function () {
        isSending = false;
        setSendState(false);
      });
  }

  // ── Append a chat bubble ────────────────────────────────────────────────────
  function appendMsg(role, text) {
    var ts = new Date().toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' });
    var el = document.createElement('div');
    el.className = 'msg ' + role;
    el.innerHTML =
      '<div class="bubble">' + esc(text) + '</div>' +
      '<span class="ts">' + ts + '</span>';
    messages.appendChild(el);
    scrollEnd();
    return el;
  }

  // ── Bot message with typewriter effect ──────────────────────────────────────
  function appendBotMsg(text, onDone) {
    var ts = new Date().toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit' });
    var el = document.createElement('div');
    el.className = 'msg bot';

    var bubble = document.createElement('div');
    bubble.className = 'bubble';

    var p = document.createElement('p');
    p.style.cssText = 'margin:0;white-space:pre-wrap;word-break:break-word;';
    bubble.appendChild(p);

    var tsSpan = document.createElement('span');
    tsSpan.className = 'ts';
    tsSpan.textContent = ts;

    el.appendChild(bubble);
    el.appendChild(tsSpan);
    messages.appendChild(el);
    scrollEnd();

    var i = 0;
    // speed: max 20ms/char, but cap total duration at 4s for long answers
    var speed = Math.min(20, Math.floor(4000 / Math.max(text.length, 1)));
    function tick() {
      if (i < text.length) {
        p.textContent += text[i];
        i++;
        scrollEnd();
        setTimeout(tick, speed);
      } else {
        // Animation done — replace plain text with linked HTML
        p.innerHTML = linkify(text);
        if (onDone) onDone();
      }
    }
    tick();
    return el;
  }

  // ── Typing indicator ────────────────────────────────────────────────────────
  function appendTyping() {
    var el = document.createElement('div');
    el.className = 'msg bot';
    el.innerHTML =
      '<div class="bubble">' +
        '<div class="typing">' +
          '<span class="tdot"></span><span class="tdot"></span><span class="tdot"></span>' +
        '</div>' +
      '</div>';
    messages.appendChild(el);
    scrollEnd();
    return el;
  }

  // ── Suggestion chips (paginated) ────────────────────────────────────────────
  function appendSuggestions(list) {
    var PAGE_SIZE = 3;
    var page = 0;

    var el = document.createElement('div');
    el.className = 'msg bot';
    messages.appendChild(el);

    function renderPage() {
      el.innerHTML = '';
      var start = page * PAGE_SIZE;
      var end = Math.min(start + PAGE_SIZE, list.length);

      // Suggestion items — vertical italic list
      var itemsWrap = document.createElement('div');
      itemsWrap.className = 'suggestions';
      list.slice(start, end).forEach(function (s) {
        var btn = document.createElement('button');
        btn.className = 'chip';
        btn.textContent = s;
        btn.addEventListener('click', function () {
          input.value = s;
          el.remove();
          send();
        });
        itemsWrap.appendChild(btn);
      });
      el.appendChild(itemsWrap);

      // Navigation row — separate from items
      if (page > 0 || end < list.length) {
        var navWrap = document.createElement('div');
        navWrap.className = 'suggestions-nav';
        if (page > 0) {
          var prevBtn = document.createElement('button');
          prevBtn.className = 'chip chip-next';
          prevBtn.textContent = '← Geri';
          prevBtn.addEventListener('click', function () { page--; renderPage(); });
          navWrap.appendChild(prevBtn);
        }
        if (end < list.length) {
          var nextBtn = document.createElement('button');
          nextBtn.className = 'chip chip-next';
          nextBtn.textContent = 'Devam →';
          nextBtn.addEventListener('click', function () { page++; renderPage(); });
          navWrap.appendChild(nextBtn);
        }
        el.appendChild(navWrap);
      }

      scrollEnd();
    }

    renderPage();
  }

  // ── Feedback ────────────────────────────────────────────────────────────────
  function appendFeedback() {
    var el = document.createElement('div');
    el.className = 'msg bot';

    var fb = document.createElement('div');
    fb.className = 'feedback';

    function showThankYou() {
      fb.innerHTML = '<p class="fb-q">Geri bildiriminiz için teşekkür ederiz.</p>';
      scrollEnd();
    }

    function showRequestStep() {
      fb.innerHTML = '';
      var q2 = document.createElement('p');
      q2.className = 'fb-q';
      q2.textContent = 'Talep oluşturmak ister misiniz?';

      var btns2 = document.createElement('div');
      btns2.className = 'fb-btns';

      var yes2 = document.createElement('button');
      yes2.className = 'fb-btn yes';
      yes2.textContent = 'Evet';

      var no2 = document.createElement('button');
      no2.className = 'fb-btn no';
      no2.textContent = 'Hayır';

      yes2.addEventListener('click', function () {
        window.open(_solutionUrl, '_blank');
        fb.innerHTML = '<p class="fb-q">Talep sayfasına yönlendiriliyorsunuz.</p>';
        scrollEnd();
      });
      no2.addEventListener('click', function () {
        fb.innerHTML = '<p class="fb-q">Anlıyorum. Başka sorularınız için buradayım.</p>';
        scrollEnd();
      });

      btns2.appendChild(yes2);
      btns2.appendChild(no2);
      fb.appendChild(q2);
      fb.appendChild(btns2);
      scrollEnd();
    }

    // Star rating
    var q1 = document.createElement('p');
    q1.className = 'fb-q';
    q1.textContent = 'Bu cevaptan memnun kaldınız mı?';

    var starsWrap = document.createElement('div');
    starsWrap.className = 'stars';

    for (var r = 1; r <= 5; r++) {
      (function (rating) {
        var star = document.createElement('button');
        star.className = 'star';
        star.textContent = '★';
        star.setAttribute('data-r', rating);

        star.addEventListener('mouseenter', function () {
          starsWrap.querySelectorAll('.star').forEach(function (s) {
            s.classList.toggle('lit', parseInt(s.getAttribute('data-r')) <= rating);
          });
        });
        star.addEventListener('mouseleave', function () {
          starsWrap.querySelectorAll('.star').forEach(function (s) { s.classList.remove('lit'); });
        });
        star.addEventListener('click', function () {
          starsWrap.querySelectorAll('.star').forEach(function (s) {
            s.classList.toggle('lit', parseInt(s.getAttribute('data-r')) <= rating);
            s.style.pointerEvents = 'none';
          });
          if (rating >= 4) {
            setTimeout(showThankYou, 300);
          } else {
            setTimeout(showRequestStep, 300);
          }
        });

        starsWrap.appendChild(star);
      })(r);
    }

    fb.appendChild(q1);
    fb.appendChild(starsWrap);
    el.appendChild(fb);
    messages.appendChild(el);
    scrollEnd();
  }

  // ── Helpers ─────────────────────────────────────────────────────────────────
  function scrollEnd() {
    requestAnimationFrame(function () { messages.scrollTop = messages.scrollHeight; });
  }

  function setSendState(busy) {
    sendBtn.disabled = busy;
    sendBtn.innerHTML = busy
      ? '<div class="spinner"></div>'
      : '<svg fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">' +
          '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9"/>' +
        '</svg>';
  }

  function esc(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function linkify(str) {
    var parts = str.split(/(https?:\/\/[^\s]+)/g);
    return parts.map(function (part, i) {
      if (i % 2 === 1) {
        var safe = esc(part);
        return '<a href="' + safe + '" target="_blank" rel="noopener noreferrer" class="chat-link">' + safe + '</a>';
      }
      return esc(part);
    }).join('');
  }

})();
