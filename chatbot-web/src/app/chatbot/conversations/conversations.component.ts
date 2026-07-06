import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import {
  ConversationsApiService,
  ConversationSummary,
  ConversationDetail,
} from '../../services/services/chatbot/conversations-api.service';

@Component({
  selector: 'app-conversations',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './conversations.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ConversationsComponent implements OnInit {
  // ── Liste durumu ──────────────────────────────────
  rows = signal<ConversationSummary[]>([]);
  total = signal(0);
  page = signal(0);            // 0 tabanlı
  readonly pageSize = 25;
  loading = signal(true);
  search = '';

  // ── Detay durumu ──────────────────────────────────
  selectedId = signal<number | null>(null);
  detail = signal<ConversationDetail | null>(null);
  detailLoading = signal(false);

  // ── Export / seçim ────────────────────────────────
  selected = signal<Set<number>>(new Set());   // sayfalar arası korunur
  exporting = signal(false);
  exportStart = '';
  exportEnd = '';

  constructor(
    private api: ConversationsApiService,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.api.list(this.page() * this.pageSize, this.pageSize, this.search).subscribe({
      next: (res) => {
        this.rows.set(res.items);
        this.total.set(res.total);
        this.loading.set(false);
        this.cdr.markForCheck();
      },
      error: () => {
        this.rows.set([]);
        this.total.set(0);
        this.loading.set(false);
        this.cdr.markForCheck();
      },
    });
  }

  doSearch(): void {
    this.page.set(0);
    this.selectedId.set(null);
    this.detail.set(null);
    this.load();
  }

  totalPages(): number {
    return Math.max(1, Math.ceil(this.total() / this.pageSize));
  }

  prevPage(): void {
    if (this.page() > 0) {
      this.page.update((p) => p - 1);
      this.load();
    }
  }

  nextPage(): void {
    if (this.page() + 1 < this.totalPages()) {
      this.page.update((p) => p + 1);
      this.load();
    }
  }

  selectRow(id: number): void {
    if (this.selectedId() === id) return;
    this.selectedId.set(id);
    this.detail.set(null);
    this.detailLoading.set(true);
    this.api.detail(id).subscribe({
      next: (d) => {
        this.detail.set(d);
        this.detailLoading.set(false);
        this.cdr.markForCheck();
      },
      error: () => {
        this.detailLoading.set(false);
        this.cdr.markForCheck();
      },
    });
  }

  // ── Export / seçim ────────────────────────────────
  toggleSelect(id: number): void {
    const s = new Set(this.selected());
    if (s.has(id)) { s.delete(id); } else { s.add(id); }
    this.selected.set(s);
  }

  exportSelected(): void {
    const ids = Array.from(this.selected());
    if (ids.length === 0) return;
    this.runExport({ ids });
  }

  exportRange(): void {
    if (!this.exportStart && !this.exportEnd) return;
    this.runExport({ start: this.exportStart, end: this.exportEnd });
  }

  private runExport(params: { ids?: number[]; start?: string; end?: string }): void {
    this.exporting.set(true);
    this.api.exportCsv(params).subscribe({
      next: (blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'konusmalar_export.csv';
        a.click();
        URL.revokeObjectURL(url);
        this.exporting.set(false);
        this.cdr.markForCheck();
      },
      error: () => {
        this.exporting.set(false);
        alert('Dışa aktarma başarısız oldu.');
        this.cdr.markForCheck();
      },
    });
  }

  // ── Görsel yardımcılar ────────────────────────────
  formatDate(value: string | null): string {
    if (!value) return '—';
    return new Date(value).toLocaleString('tr-TR');
  }

  talepLabel(status: string): string {
    switch (status) {
      case 'redirected': return 'Yönlendirildi';
      case 'declined': return 'Reddetti';
      default: return 'Önerilmedi';
    }
  }

  talepClass(status: string): string {
    switch (status) {
      case 'redirected':
        return 'bg-blue-100 text-blue-700 dark:bg-blue-950/60 dark:text-blue-300';
      case 'declined':
        return 'bg-amber-100 text-amber-700 dark:bg-amber-950/60 dark:text-amber-300';
      default:
        return 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400';
    }
  }

  sourceLabel(source: string | null): string {
    switch (source) {
      case 'meilisearch': return 'Anahtar kelime';
      case 'qdrant_vector': return 'Anlamsal';
      case 'llm': return 'LLM';
      case 'academic_calendar': return 'Akademik Takvim';
      case 'none': return 'Cevap yok';
      default: return source || '';
    }
  }

  stars(n: number | null): string {
    if (!n) return '';
    return '★'.repeat(n) + '☆'.repeat(Math.max(0, 5 - n));
  }
}
