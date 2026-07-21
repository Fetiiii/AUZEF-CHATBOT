import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  OnDestroy,
  inject,
  signal
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { BaseChartDirective } from 'ng2-charts';
import { ChartConfiguration, ChartData } from 'chart.js';
import { Subscription, interval, startWith } from 'rxjs';
import { StatsApiService, TrafficMode, TrafficPoint } from '../../services/services/chatbot/stats-api.service';

@Component({
  selector: 'app-view-data',
  standalone: true,
  imports: [CommonModule, FormsModule, BaseChartDirective],
  templateUrl: './view-data.component.html',
  styles: `
    :host { display: block; }
    canvas { max-height: 340px; }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class ViewDataComponent implements OnInit, OnDestroy {
  private readonly statsService = inject(StatsApiService);

  /* ── Oto-yenileme ── */
  private readonly REFRESH_MS = 30_000;   // 30 sn'de bir yenile (5 dk pencereyi canlı tutar)
  private pollSub?: Subscription;

  /* ── Metric cards ── */
  activeUsers = signal<number>(0);
  totalQueriesToday = signal<number>(0);

  /* ── Sorgu trafiği (saatlik / haftalık / aylık) ── */
  selectedMode = signal<TrafficMode>('hourly');
  selectedDate = '';                       // '' = canlı/güncel dönem. hourly/weekly: YYYY-MM-DD, monthly: YYYY-MM
  periodLabel = signal<string>('');
  private trafficPoints: TrafficPoint[] = [];   // tooltip için ham noktalar

  private readonly TR_MONTHS = [
    'Ocak', 'Şubat', 'Mart', 'Nisan', 'Mayıs', 'Haziran',
    'Temmuz', 'Ağustos', 'Eylül', 'Ekim', 'Kasım', 'Aralık'
  ];

  hourlyLabels: string[] = [];
  hourlyChartData = signal<ChartData<'line'>>({ labels: [], datasets: [] });

  hourlyChartOptions: ChartConfiguration<'line'>['options'] = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: 'rgba(15,23,42,.92)',
        titleFont: { size: 12, weight: 'bold' },
        bodyFont: { size: 11 },
        padding: 10,
        cornerRadius: 8,
        displayColors: false,
        callbacks: {
          // Başlık: haftalık/aylıkta "15 Temmuz 2026 Çarşamba", saatlikte tarih varsa gün + saat
          title: (items) => {
            const p = this.trafficPoints[items[0].dataIndex];
            if (!p) return items[0].label;
            if (this.selectedMode() === 'hourly') {
              return p.date ? `${this.formatLongDate(p.date)} · ${p.label}` : p.label;
            }
            const day = p.date ? this.formatLongDate(p.date) : p.label;
            return p.weekday ? `${day} ${p.weekday}` : day;
          },
          label: (item) => `Sorgu: ${item.parsed.y}`
        }
      }
    },
    scales: {
      x: {
        grid: { display: false },
        ticks: { font: { size: 10 }, maxRotation: 0 }
      },
      y: {
        beginAtZero: true,
        grid: { color: 'rgba(148,163,184,.12)' },
        ticks: { font: { size: 10 } }
      }
    }
  };

  /* ── Doughnut chart (LLM vs Meili vs None) ── */
  doughnutChartData = signal<ChartData<'doughnut'>>({ labels: [], datasets: [] });

  doughnutChartOptions: ChartConfiguration<'doughnut'>['options'] = {
    responsive: true,
    maintainAspectRatio: false,
    cutout: '62%',
    plugins: {
      legend: {
        position: 'bottom',
        labels: {
          padding: 16,
          usePointStyle: true,
          pointStyleWidth: 10,
          font: { size: 12 }
        }
      },
      tooltip: {
        backgroundColor: 'rgba(15,23,42,.92)',
        titleFont: { size: 12, weight: 'bold' },
        bodyFont: { size: 11 },
        padding: 10,
        cornerRadius: 8
      }
    }
  };

  /* ── Doughnut stats for the side panel ── */
  llmCount = signal<number>(0);
  meiliCount = signal<number>(0);
  noAnswerCount = signal<number>(0);

  /* ── Konuşma istatistikleri (tarih filtreli) ── */
  convStart = '';
  convEnd = '';
  convLoading = signal<boolean>(false);
  totalConversations = signal<number>(0);
  outcomeCounts = signal<{ olumlu: number; olumsuz: number; puansiz: number }>({ olumlu: 0, olumsuz: 0, puansiz: 0 });
  talepCounts = signal<{ redirected: number; declined: number; not_offered: number }>({ redirected: 0, declined: 0, not_offered: 0 });

  // Yıldız dağılımı — bar
  ratingChartData = signal<ChartData<'bar'>>({ labels: [], datasets: [] });
  ratingChartOptions: ChartConfiguration<'bar'>['options'] = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: { backgroundColor: 'rgba(15,23,42,.92)', padding: 10, cornerRadius: 8, displayColors: false }
    },
    scales: {
      x: { grid: { display: false }, ticks: { font: { size: 12 } } },
      y: { beginAtZero: true, grid: { color: 'rgba(148,163,184,.12)' }, ticks: { font: { size: 10 }, precision: 0 } }
    }
  };

  // Olumlu / olumsuz / puansız — doughnut (doughnutChartOptions yeniden kullanılıyor)
  outcomeChartData = signal<ChartData<'doughnut'>>({ labels: [], datasets: [] });

  ngOnInit(): void {
    // Sabit pencereli kartlar: hemen + 30 sn'de bir yenile
    this.pollSub = interval(this.REFRESH_MS)
      .pipe(startWith(0))
      .subscribe(() => this.loadStats());

    // Konuşma istatistikleri: varsayılan son 30 gün
    const today = new Date();
    const past = new Date();
    past.setDate(today.getDate() - 29);
    this.convEnd = this.toISODate(today);
    this.convStart = this.toISODate(past);
    this.loadConversationStats();
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
  }

  onConvDateChange(): void {
    this.loadConversationStats();
  }

  private toISODate(d: Date): string {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  private loadConversationStats(): void {
    this.convLoading.set(true);
    this.statsService.getConversationStats(this.convStart, this.convEnd).subscribe({
      next: (s) => {
        this.totalConversations.set(s.total_conversations);
        this.outcomeCounts.set(s.outcome);
        this.talepCounts.set(s.talep);

        const rd = s.rating_distribution;
        this.ratingChartData.set({
          labels: ['1★', '2★', '3★', '4★', '5★'],
          datasets: [{
            data: [rd['1'] || 0, rd['2'] || 0, rd['3'] || 0, rd['4'] || 0, rd['5'] || 0],
            label: 'Adet',
            backgroundColor: ['#ef4444', '#f97316', '#f59e0b', '#84cc16', '#10b981'],
            borderRadius: 6
          }]
        });

        this.outcomeChartData.set({
          labels: ['Olumlu (≥4)', 'Olumsuz (≤3)', 'Puansız'],
          datasets: [{
            data: [s.outcome.olumlu, s.outcome.olumsuz, s.outcome.puansiz],
            backgroundColor: ['#10b981', '#ef4444', '#94a3b8'],
            borderColor: ['#fff', '#fff', '#fff'],
            borderWidth: 2,
            hoverOffset: 6
          }]
        });

        this.convLoading.set(false);
      },
      error: () => this.convLoading.set(false)
    });
  }

  /* ── Trafik modu / dönem gezinme ── */

  selectMode(mode: TrafficMode): void {
    if (mode === this.selectedMode()) return;
    // Tarih formatını mod ile uyumla: monthly YYYY-MM, diğerleri YYYY-MM-DD
    if (this.selectedDate) {
      if (mode === 'monthly') this.selectedDate = this.selectedDate.slice(0, 7);
      else if (this.selectedDate.length === 7) this.selectedDate = `${this.selectedDate}-01`;
    }
    this.selectedMode.set(mode);
    this.loadStats();
  }

  onDateChange(): void {
    this.loadStats();
  }

  goToday(): void {
    this.selectedDate = '';   // boş = backend güncel dönemi (canlı) hesaplar
    this.loadStats();
  }

  prevPeriod(): void { this.shiftPeriod(-1); }
  nextPeriod(): void { this.shiftPeriod(1); }

  /** true iken "Sonraki" ve "Bugün" pasif olur (zaten güncel dönemdeyiz). */
  isCurrentPeriod(): boolean {
    if (!this.selectedDate) return true;
    return this.selectedMode() === 'monthly'
      ? this.selectedDate >= this.todayMonth()
      : this.selectedDate >= this.todayDate();
  }

  private shiftPeriod(dir: -1 | 1): void {
    if (this.selectedMode() === 'monthly') {
      const base = this.selectedDate || this.todayMonth();
      let [y, m] = base.split('-').map(Number);
      m += dir;
      if (m < 1) { m = 12; y--; } else if (m > 12) { m = 1; y++; }
      const next = `${y}-${String(m).padStart(2, '0')}`;
      if (dir === 1 && next > this.todayMonth()) return;   // geleceğe gitme
      this.selectedDate = next;
    } else {
      const step = this.selectedMode() === 'weekly' ? 7 : 1;
      const [y, m, d] = (this.selectedDate || this.todayDate()).split('-').map(Number);
      const dt = new Date(y, m - 1, d);
      dt.setDate(dt.getDate() + dir * step);
      const next = this.toISODate(dt);
      if (dir === 1 && next > this.todayDate()) return;    // geleceğe gitme
      this.selectedDate = next;
    }
    this.loadStats();
  }

  // Tarih seçicilerin [max] sınırı — geleceğe gidilmesini engeller
  get todayDateMax(): string { return this.todayDate(); }
  get todayMonthMax(): string { return this.todayMonth(); }

  private todayDate(): string { return this.toISODate(new Date()); }

  private todayMonth(): string {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  }

  private formatLongDate(iso: string): string {
    const [y, m, d] = iso.split('-').map(Number);
    return `${d} ${this.TR_MONTHS[m - 1]} ${y}`;
  }

  private loadStats(): void {
    this.statsService.getStats(this.selectedMode(), this.selectedDate).subscribe({
      next: (stats) => {
        this.activeUsers.set(stats.active_users);
        this.totalQueriesToday.set(stats.total_queries_today);
        this.periodLabel.set(stats.period_label);

        this.trafficPoints = stats.traffic;
        const labels = stats.traffic.map(p => p.label);
        const data = stats.traffic.map(p => p.count);
        this.hourlyLabels = labels;
        this.hourlyChartData.set({
          labels,
          datasets: [{
            data,
            label: 'Sorgu Sayısı',
            borderColor: '#7c3aed',
            backgroundColor: 'rgba(124,58,237,.08)',
            pointBackgroundColor: '#7c3aed',
            pointBorderColor: '#fff',
            pointRadius: 3,
            pointHoverRadius: 5,
            borderWidth: 2,
            fill: true,
            tension: 0.35
          }]
        });

        const { llm, meilisearch, none } = stats.sources;
        this.llmCount.set(llm);
        this.meiliCount.set(meilisearch);
        this.noAnswerCount.set(none);
        this.doughnutChartData.set({
          labels: ['LLM (Yapay Zeka)', 'MeiliSearch', 'Cevapsız'],
          datasets: [{
            data: [llm, meilisearch, none],
            backgroundColor: ['#7c3aed', '#10b981', '#f59e0b'],
            borderColor: ['#fff', '#fff', '#fff'],
            borderWidth: 2,
            hoverOffset: 6
          }]
        });
      },
      error: (err) => console.error('İstatistikler yüklenemedi:', err)
    });
  }
}