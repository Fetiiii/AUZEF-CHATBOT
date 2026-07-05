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
    // Design tokens (kurumsal mavi paleti)
    ':host{--aw-blue:#1e3a8a;--aw-blue-600:#2563eb;--aw-blue-700:#1d4ed8;',
    '--aw-ink:#0f172a;--aw-muted:#64748b;--aw-faint:#94a3b8;',
    '--aw-surface:#ffffff;--aw-line:#e7ecf7;--aw-chip:#eef3ff;',
    "--aw-font:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;}",
    '*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}',

    // ── FAB button ───────────────────────────────────────────────────────────
    '.fab{position:fixed;bottom:24px;' + _pos + ':24px;width:60px;height:60px;border-radius:50%;',
    'background:linear-gradient(140deg,#2563eb 0%,#1e3a8a 100%);color:#fff;border:none;cursor:pointer;',
    'box-shadow:0 10px 26px -6px rgba(30,58,138,.55),0 4px 10px -4px rgba(15,23,42,.35),',
    'inset 0 1px 0 rgba(255,255,255,.25);display:flex;align-items:center;',
    'justify-content:center;transition:transform .25s cubic-bezier(.34,1.56,.64,1),box-shadow .25s;',
    'z-index:2147483646;}',
    '.fab::after{content:"";position:absolute;inset:0;border-radius:50%;',
    'background:radial-gradient(circle,rgba(37,99,235,.55),transparent 70%);z-index:-1;',
    'animation:fab-pulse 2.8s ease-out infinite;}',
    '@keyframes fab-pulse{0%{transform:scale(1);opacity:.6}70%,100%{transform:scale(1.7);opacity:0}}',
    '.fab:hover{transform:scale(1.08) translateY(-1px);',
    'box-shadow:0 14px 34px -6px rgba(30,58,138,.65),0 6px 14px -4px rgba(15,23,42,.4);}',
    '.fab:active{transform:scale(.96);}',
    '.fab svg{width:27px;height:27px;filter:drop-shadow(0 1px 1px rgba(0,0,0,.2));}',

    // ── Popup ────────────────────────────────────────────────────────────────
    '.popup{position:fixed;bottom:98px;' + _pos + ':24px;width:372px;height:544px;',
    'background:var(--aw-surface);border-radius:22px;',
    'box-shadow:0 24px 64px -16px rgba(30,58,138,.32),0 10px 28px -12px rgba(15,23,42,.28),',
    'inset 0 0 0 1px rgba(255,255,255,.6);',
    'display:flex;flex-direction:column;overflow:hidden;z-index:2147483645;',
    'font-family:var(--aw-font);',
    'transform-origin:bottom ' + _pos + ';',
    'transition:transform .32s cubic-bezier(.34,1.4,.5,1),opacity .22s;}',
    '.popup.hidden{transform:scale(.82) translateY(20px);opacity:0;pointer-events:none;}',

    // ── Header (cam efekti) ──────────────────────────────────────────────────
    '.header{position:relative;background:linear-gradient(135deg,#1e3a8a 0%,#2b4fb8 55%,#1d4ed8 100%);',
    'padding:15px 16px;display:flex;align-items:center;gap:11px;flex-shrink:0;overflow:hidden;}',
    '.header::before{content:"";position:absolute;top:-60%;left:-10%;width:70%;height:200%;',
    'background:radial-gradient(closest-side,rgba(255,255,255,.28),transparent);pointer-events:none;}',
    '.header::after{content:"";position:absolute;left:0;right:0;bottom:0;height:1px;',
    'background:linear-gradient(90deg,transparent,rgba(255,255,255,.35),transparent);}',
    '.avatar{position:relative;width:38px;height:38px;border-radius:50%;',
    'background:linear-gradient(160deg,rgba(255,255,255,.32),rgba(255,255,255,.12));',
    'box-shadow:inset 0 0 0 1px rgba(255,255,255,.4),0 2px 6px rgba(0,0,0,.15);',
    'display:flex;align-items:center;justify-content:center;flex-shrink:0;}',
    '.avatar svg{width:21px;height:21px;}',
    '.header-info{flex:1;min-width:0;position:relative;}',
    '.header-title{font-size:15px;font-weight:700;color:#fff;letter-spacing:.2px;}',
    '.header-sub{font-size:11.5px;color:rgba(255,255,255,.82);display:flex;align-items:center;gap:6px;margin-top:1px;}',
    '.dot-online{position:relative;width:7px;height:7px;border-radius:50%;background:#4ade80;',
    'box-shadow:0 0 0 0 rgba(74,222,128,.7);animation:dot-ping 2s infinite;}',
    '@keyframes dot-ping{0%{box-shadow:0 0 0 0 rgba(74,222,128,.65)}70%,100%{box-shadow:0 0 0 6px rgba(74,222,128,0)}}',
    '.close-btn{position:relative;width:30px;height:30px;border:none;background:rgba(255,255,255,.16);',
    'border-radius:9px;cursor:pointer;display:flex;align-items:center;justify-content:center;',
    'color:#fff;flex-shrink:0;transition:background .18s,transform .15s;}',
    '.close-btn:hover{background:rgba(255,255,255,.3);transform:rotate(90deg);}',
    '.close-btn svg{width:15px;height:15px;}',

    // ── Messages ─────────────────────────────────────────────────────────────
    '.messages{flex:1;overflow-y:auto;padding:16px 14px;display:flex;flex-direction:column;',
    'gap:11px;background:linear-gradient(180deg,#f7f9ff 0%,#eef2fc 100%);}',
    '.messages::-webkit-scrollbar{width:5px;}',
    '.messages::-webkit-scrollbar-track{background:transparent;}',
    '.messages::-webkit-scrollbar-thumb{background:#c7d5f5;border-radius:6px;}',
    '.messages::-webkit-scrollbar-thumb:hover{background:#a9bdec;}',

    // ── Welcome line ─────────────────────────────────────────────────────────
    '.welcome{text-align:center;color:var(--aw-muted);font-size:12.5px;margin:8px 0;line-height:1.5;}',

    // ── Message bubbles ──────────────────────────────────────────────────────
    '.msg{display:flex;flex-direction:column;max-width:87%;animation:msg-in .32s cubic-bezier(.34,1.3,.5,1) both;}',
    '@keyframes msg-in{from{opacity:0;transform:translateY(10px) scale(.98)}to{opacity:1;transform:none}}',
    '.msg.user{align-self:flex-end;align-items:flex-end;}',
    '.msg.bot{align-self:flex-start;align-items:flex-start;}',
    '.bubble{padding:10px 14px;border-radius:16px;font-size:13.5px;line-height:1.62;',
    'word-break:break-word;white-space:pre-wrap;}',
    '.msg.user .bubble{background:linear-gradient(135deg,#2563eb,#1e40af);color:#fff;',
    'border-bottom-right-radius:5px;box-shadow:0 4px 12px -4px rgba(37,99,235,.45);}',
    '.msg.bot .bubble{background:var(--aw-surface);color:var(--aw-ink);border-bottom-left-radius:5px;',
    'box-shadow:0 2px 8px -2px rgba(15,23,42,.1),inset 0 0 0 1px rgba(15,23,42,.03);}',
    '.ts{font-size:10px;color:var(--aw-faint);margin-top:4px;padding:0 4px;}',

    // ── Typing dots ──────────────────────────────────────────────────────────
    '.typing{display:flex;gap:5px;align-items:center;padding:3px 2px;}',
    '.tdot{width:8px;height:8px;border-radius:50%;',
    'background:linear-gradient(135deg,#60a5fa,#2563eb);animation:tdot-bounce 1.3s infinite;}',
    '.tdot:nth-child(2){animation-delay:.18s}.tdot:nth-child(3){animation-delay:.36s}',
    '@keyframes tdot-bounce{0%,80%,100%{transform:translateY(0);opacity:.6}40%{transform:translateY(-6px);opacity:1}}',

    // ── Input area ───────────────────────────────────────────────────────────
    '.input-area{padding:11px 12px;display:flex;align-items:flex-end;gap:9px;',
    'border-top:1px solid var(--aw-line);background:var(--aw-surface);flex-shrink:0;}',
    '.input-wrap{flex:1;display:flex;align-items:center;background:#f5f7fd;',
    'border:1.5px solid var(--aw-line);border-radius:13px;transition:border-color .18s,box-shadow .18s;}',
    '.input-wrap:focus-within{border-color:var(--aw-blue-600);',
    'box-shadow:0 0 0 3px rgba(37,99,235,.14);background:#fff;}',
    '.input-area textarea{flex:1;resize:none;border:none;background:transparent;',
    'padding:10px 12px;font-size:13.5px;font-family:inherit;color:var(--aw-ink);',
    'outline:none;max-height:100px;overflow-y:auto;line-height:1.5;}',
    '.input-area textarea::placeholder{color:var(--aw-faint);}',
    '.send-btn{width:38px;height:38px;flex-shrink:0;border:none;',
    'background:linear-gradient(140deg,#2563eb,#1e40af);color:#fff;',
    'border-radius:12px;cursor:pointer;display:flex;align-items:center;justify-content:center;',
    'box-shadow:0 4px 12px -3px rgba(37,99,235,.5);transition:transform .12s,box-shadow .18s,opacity .18s;}',
    '.send-btn:hover{transform:translateY(-1px);box-shadow:0 7px 16px -4px rgba(37,99,235,.6);}',
    '.send-btn:active{transform:scale(.92);}',
    '.send-btn:disabled{background:#93b4f0;cursor:not-allowed;transform:none;box-shadow:none;}',
    '.send-btn svg{width:17px;height:17px;}',
    '.spinner{width:15px;height:15px;border:2px solid rgba(255,255,255,.4);',
    'border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;}',
    '@keyframes spin{to{transform:rotate(360deg)}}',

    // ── Links inside bot answers ─────────────────────────────────────────────
    '.chat-link{color:var(--aw-blue-600);font-weight:600;text-decoration:underline;',
    'text-underline-offset:2px;word-break:break-all;}',
    '.chat-link:hover{color:var(--aw-blue-700);}',

    // ── Feedback ─────────────────────────────────────────────────────────────
    '.feedback{display:flex;flex-direction:column;gap:7px;padding:5px 0 2px;}',
    '.fb-q{font-size:11.5px;color:var(--aw-muted);margin:0;}',
    '.fb-btns{display:flex;gap:7px;}',
    '.fb-btn{padding:5px 15px;border-radius:20px;border:1.5px solid;font-size:12px;font-weight:600;',
    'cursor:pointer;font-family:inherit;transition:background .15s,transform .12s;}',
    '.fb-btn:active{transform:scale(.94);}',
    '.fb-btn.yes{border-color:#86efac;background:#f0fdf4;color:#15803d;}',
    '.fb-btn.yes:hover{background:#dcfce7;}',
    '.fb-btn.no{border-color:#fca5a5;background:#fff1f2;color:#b91c1c;}',
    '.fb-btn.no:hover{background:#fee2e2;}',

    // ── Suggestion chips ─────────────────────────────────────────────────────
    '.suggestions{display:flex;flex-direction:column;gap:7px;padding:6px 0 2px;}',
    '.chip{background:var(--aw-chip);color:var(--aw-blue-700);border:1px solid #d6e2fb;border-radius:13px;',
    'padding:9px 14px;font-size:12.5px;font-weight:550;cursor:pointer;font-family:inherit;',
    'text-align:left;width:100%;transition:background .15s,transform .12s,border-color .15s;}',
    '.chip:hover{background:#e2ebff;border-color:#bcd0f7;transform:translateX(2px);}',
    '.suggestions-nav{display:flex;gap:7px;padding:6px 0 2px;}',
    '.chip-next{background:#f1f5f9;color:#475569;border:1.5px solid #cbd5e1;border-radius:20px;',
    'padding:5px 13px;font-size:12px;font-weight:600;font-style:normal;cursor:pointer;font-family:inherit;',
    'text-decoration:none;transition:background .15s;}',
    '.chip-next:hover{background:#e2e8f0;}',

    // ── Stars ────────────────────────────────────────────────────────────────
    '.stars{display:flex;gap:3px;padding:2px 0;}',
    '.star{font-size:24px;color:#d7dce6;background:none;border:none;cursor:pointer;',
    'padding:0 1px;transition:color .12s,transform .12s;line-height:1;font-family:inherit;}',
    '.star:hover{transform:scale(1.15);}',
    '.star.lit{color:#f59e0b;filter:drop-shadow(0 1px 3px rgba(245,158,11,.4));}',

    // ── Responsive ───────────────────────────────────────────────────────────
    '@media(max-width:420px){',
    '.popup{width:calc(100vw - 16px);height:calc(100vh - 96px);' + _pos + ':8px;bottom:84px;}',
    '.fab{' + _pos + ':14px;bottom:14px;}}'
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
        '<div class="input-wrap">' +
          '<textarea id="w-input" rows="1" placeholder="Sorunuzu yazın..." aria-label="Mesaj"></textarea>' +
        '</div>' +
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
    this.style.height = Math.min(this.scrollHeight, 100) + 'px';
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
