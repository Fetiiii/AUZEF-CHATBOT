import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  HostListener,
  ViewChild,
  computed,
  effect,
  inject,
  signal
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { ChatbotService, BotAction } from '../chatbot.service';



@Component({
  standalone: true,
  selector: 'app-general-chatbot',
  imports: [CommonModule],
  templateUrl: './chatbot.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: [
    `
      /* Panel açılış animasyonu (mini pop) */
      .csm-chat-panel {
        transform-origin: bottom right;
        animation: csmChatPop 160ms ease-out;
      }
      @keyframes csmChatPop {
        from { opacity: 0; transform: translateY(10px) scale(0.985); }
        to { opacity: 1; transform: translateY(0) scale(1); }
      }

      /* İnce + renkli scrollbar */
      .csm-chat-scroll {
        scrollbar-width: thin;
        scrollbar-color: rgba(124,58,237,.55) transparent;
        scrollbar-gutter: stable;
      }
      .csm-chat-scroll::-webkit-scrollbar { width: 8px; }
      .csm-chat-scroll::-webkit-scrollbar-track { background: transparent; }
      .csm-chat-scroll::-webkit-scrollbar-thumb {
        background: linear-gradient(180deg, rgba(124,58,237,.65), rgba(217,70,239,.65));
        border-radius: 999px;
        border: 2px solid rgba(255,255,255,0);
        background-clip: padding-box;
      }
      .csm-chat-scroll::-webkit-scrollbar-thumb:hover {
        background: linear-gradient(180deg, rgba(124,58,237,.85), rgba(217,70,239,.85));
      }

      :host-context(.dark) .csm-chat-scroll {
        scrollbar-color: rgba(168,85,247,.65) transparent;
      }
      :host-context(.dark) .csm-chat-scroll::-webkit-scrollbar-thumb {
        background: linear-gradient(180deg, rgba(168,85,247,.7), rgba(236,72,153,.7));
      }
      :host-context(.dark) .csm-chat-scroll::-webkit-scrollbar-thumb:hover {
        background: linear-gradient(180deg, rgba(168,85,247,.9), rgba(236,72,153,.9));
      }
    `
  ]
})
export class ChatbotComponent implements AfterViewInit {
  private router = inject(Router);
  private destroyRef = inject(DestroyRef);

  constructor(public bot: ChatbotService) { }

  @ViewChild('scrollEl') scrollEl?: ElementRef<HTMLDivElement>;
  @ViewChild('inputEl') inputEl?: ElementRef<HTMLInputElement>;
  @ViewChild('panelEl') panelEl?: ElementRef<HTMLDivElement>;

  // ✅ EN ÖNEMLİ: En alta kaydırma anchor’ı
  @ViewChild('bottomAnchor') bottomAnchor?: ElementRef<HTMLDivElement>;

  open = signal<boolean>(false);
  input = signal<string>('');

  private lgMQ = window.matchMedia('(min-width: 1024px)');
  isLg = signal<boolean>(this.lgMQ.matches);

  bottomOffset = computed(() => {
    if (!this.isLg()) return 'calc(var(--bottom-nav-h, 60px) + 4.5rem)';
    return '1rem';
  });

  // template refs
  messages = this.bot.messages;
  suggestions = this.bot.suggestions;
  isTyping = this.bot.isTyping;
  statusText = this.bot.statusText;
  statusDotClass = this.bot.statusDotClass;

  ngAfterViewInit(): void {
    const onChange = (e: MediaQueryListEvent) => this.isLg.set(e.matches);
    this.lgMQ.addEventListener?.('change', onChange);
    // @ts-ignore legacy
    this.lgMQ.addListener?.(onChange);

    this.destroyRef.onDestroy(() => {
      this.lgMQ.removeEventListener?.('change', onChange);
      // @ts-ignore legacy
      this.lgMQ.removeListener?.(onChange);
    });

    // Panel açılınca focus + en alta kaydır
    effect(() => {
      if (!this.open()) return;

      setTimeout(() => this.inputEl?.nativeElement?.focus(), 60);

      // panel ilk açıldığında da en alta
      this.scrollToLatest(false);
    });

    // ✅ MESAJ / TYPING değiştikçe her zaman en alta kaydır
    effect(() => {
      if (!this.open()) return;

      // Signals: değişimleri dinlemek için okuyalım
      this.messages();
      this.isTyping();

      this.scrollToLatest(false);
    });
  }

  // ✅ En sağlam scroll (DOM render gecikmelerine dayanıklı)
  private scrollToLatest(smooth: boolean) {
    // 1) microtask
    queueMicrotask(() => this.scrollNow(smooth));
    // 2) next tick
    setTimeout(() => this.scrollNow(smooth), 0);
    // 3) biraz daha geç (özellikle typing bubble / actions renderında)
    setTimeout(() => this.scrollNow(smooth), 60);
    // 4) raf
    requestAnimationFrame(() => this.scrollNow(smooth));
  }

  private scrollNow(smooth: boolean) {
    if (!this.open()) return;

    // anchor varsa en garanti yöntem
    const anchor = this.bottomAnchor?.nativeElement;
    if (anchor) {
      try {
        anchor.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'end' });
        return;
      } catch {
        // fallback aşağıda
      }
    }

    // fallback
    const el = this.scrollEl?.nativeElement;
    if (!el) return;

    if (smooth && 'scrollTo' in el) {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    } else {
      el.scrollTop = el.scrollHeight;
    }
  }

  @HostListener('document:keydown.escape')
  onEsc() {
    if (this.open()) this.open.set(false);
  }

  @HostListener('document:mousedown', ['$event'])
  onDocMouseDown(ev: MouseEvent) {
    if (!this.open()) return;

    const panel = this.panelEl?.nativeElement;
    if (!panel) return;

    const target = ev.target as Node | null;
    if (!target) return;

    if (panel.contains(target)) return;

    const el = target as HTMLElement;
    if (el?.closest?.('[data-chatbot-toggle="1"]')) return;

    this.open.set(false);
  }

  toggle() {
    const next = !this.open();
    this.open.set(next);

    // açarken anında en alta
    if (next) this.scrollToLatest(true);
  }

  close() {
    this.open.set(false);
  }

  reset() {
    this.input.set('');
    this.bot.reset();
    this.scrollToLatest(true);
  }

  onInput(v: string) {
    this.input.set(v);
  }

  sendFromEnter(ev: KeyboardEvent) {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      this.send();
    }
  }

  async send(text?: string) {
    if (this.isTyping()) return;

    const raw = (text ?? this.input()).trim();
    if (!raw) return;

    this.input.set('');
    await this.bot.sendUserMessage(raw);

    // gönderince de en alta
    this.scrollToLatest(true);
  }

  async onAction(a: BotAction) {
    if (a.type === 'navigate') {
      this.router.navigate([a.value]).catch(() => {
        this.bot.notify('Bu sayfaya şu an yönlendiremedim. Route adını kontrol edelim.');
      });
      return;
    }

    if (a.type === 'copy') {
      const val = a.value ?? '';
      if (!val) return;

      try {
        await navigator.clipboard.writeText(val);
        this.bot.notify('Kopyalandı ✅');
      } catch {
        this.bot.notify('Kopyalayamadım. Tarayıcı izinlerini kontrol edelim.');
      }

      this.scrollToLatest(true);
    }
  }
}
