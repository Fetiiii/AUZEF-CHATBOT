import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  ElementRef,
  OnInit,
  ViewChild,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ChatbotSearchService, SearchResponse } from '../../services/services/chatbot/chatbot-search.service';

export interface ChatMessage {
  id: string;
  role: 'user' | 'bot';
  text: string;
  source?: SearchResponse['source'];
  suggestions?: string[];
  loading?: boolean;
  timestamp: Date;
}

@Component({
  selector: 'app-chat',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './chat.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChatComponent implements OnInit {
  @ViewChild('messagesEnd') messagesEnd!: ElementRef;

  messages = signal<ChatMessage[]>([]);
  inputText = '';
  sending = signal(false);

  private readonly STORAGE_KEY = 'auzef_chat_history';

  constructor(
    private searchService: ChatbotSearchService,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    this.loadHistory();
    if (this.messages().length === 0) {
      this.pushWelcome();
    }
  }

  private pushWelcome(): void {
    this.pushMessage({
      role: 'bot',
      text: 'Merhaba! Ben AUZEF Akıllı Asistanı. AUZEF ile ilgili sorularınızı yanıtlamak için buradayım. Size nasıl yardımcı olabilirim?',
      source: undefined,
    });
  }

  send(): void {
    const q = this.inputText.trim();
    if (!q || this.sending()) return;

    this.inputText = '';
    this.sending.set(true);

    // Kullanıcı mesajını ekle
    this.pushMessage({ role: 'user', text: q });

    // Typing placeholder
    const loadingId = this.pushMessage({ role: 'bot', text: '', loading: true });

    this.searchService.search(q).subscribe({
      next: (res) => {
        this.replaceMessage(loadingId, this.buildBotMessage(res));
        this.sending.set(false);
        this.saveHistory();
        this.scrollToEnd();
        this.cdr.markForCheck();
      },
      error: () => {
        this.replaceMessage(loadingId, {
          role: 'bot',
          text: 'Üzgünüm, bir hata oluştu. Lütfen daha sonra tekrar deneyin.',
          source: undefined,
        });
        this.sending.set(false);
        this.cdr.markForCheck();
      },
    });

    this.scrollToEnd();
    this.cdr.markForCheck();
  }

  private buildBotMessage(res: SearchResponse): Partial<ChatMessage> {
    if (res.status === 'success' && res.answer) {
      return { role: 'bot', text: res.answer, source: res.source };
    }
    if (res.status === 'suggest') {
      return {
        role: 'bot',
        text: res.message ?? 'Doğrudan bir cevap bulamadım. Şunları sormak istediniz mi?',
        source: 'none',
        suggestions: res.suggestions ?? [],
      };
    }
    return { role: 'bot', text: 'Üzgünüm, bu soruya cevap bulamadım.', source: 'none' };
  }

  suggestClick(s: string): void {
    this.inputText = s;
    this.send();
  }

  clearChat(): void {
    this.messages.set([]);
    localStorage.removeItem(this.STORAGE_KEY);
    this.pushWelcome();
    this.cdr.markForCheck();
  }

  onKeyDown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.send();
    }
  }

  sourceLabel(source?: SearchResponse['source']): string {
    switch (source) {
      case 'meilisearch': return 'Anahtar Kelime';
      case 'qdrant_vector': return 'Semantik Arama';
      case 'llm': return 'LLM (RAG)';
      default: return '';
    }
  }

  sourceBadgeClass(source?: SearchResponse['source']): string {
    switch (source) {
      case 'meilisearch': return 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300';
      case 'qdrant_vector': return 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300';
      case 'llm': return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300';
      default: return '';
    }
  }

  private pushMessage(msg: Partial<ChatMessage>): string {
    const id = typeof crypto?.randomUUID === 'function'
      ? crypto.randomUUID()
      : `${Date.now()}_${Math.random().toString(16).slice(2)}`;
    const full: ChatMessage = {
      id,
      role: msg.role ?? 'bot',
      text: msg.text ?? '',
      source: msg.source,
      suggestions: msg.suggestions,
      loading: msg.loading ?? false,
      timestamp: new Date(),
    };
    this.messages.update((m) => [...m, full]);
    this.cdr.markForCheck();
    setTimeout(() => this.scrollToEnd(), 50);
    return id;
  }

  private replaceMessage(id: string, patch: Partial<ChatMessage>): void {
    this.messages.update((msgs) =>
      msgs.map((m) =>
        m.id === id ? { ...m, ...patch, loading: false, timestamp: new Date() } : m
      )
    );
  }

  private scrollToEnd(): void {
    setTimeout(() => {
      this.messagesEnd?.nativeElement?.scrollIntoView({ behavior: 'smooth' });
    }, 80);
  }

  private saveHistory(): void {
    const last50 = this.messages().slice(-50);
    localStorage.setItem(this.STORAGE_KEY, JSON.stringify(last50));
  }

  private loadHistory(): void {
    try {
      const raw = localStorage.getItem(this.STORAGE_KEY);
      if (raw) {
        const parsed: ChatMessage[] = JSON.parse(raw);
        this.messages.set(
          parsed.map((m) => ({ ...m, timestamp: new Date(m.timestamp) }))
        );
      }
    } catch {
      localStorage.removeItem(this.STORAGE_KEY);
    }
  }
}
