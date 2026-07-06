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
import { StatsApiService } from '../../services/services/chatbot/stats-api.service';

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

  /* ── Hourly traffic line chart (24h) ── */
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
        displayColors: false
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

  private loadStats(): void {
    this.statsService.getStats().subscribe({
      next: (stats) => {
        this.activeUsers.set(stats.active_users);
        this.totalQueriesToday.set(stats.total_queries_today);

        const labels = stats.hourly.map(h => h.label);
        const data = stats.hourly.map(h => h.count);
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