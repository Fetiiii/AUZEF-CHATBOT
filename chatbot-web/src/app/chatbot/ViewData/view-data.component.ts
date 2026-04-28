import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  inject,
  signal
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { BaseChartDirective } from 'ng2-charts';
import { ChartConfiguration, ChartData } from 'chart.js';
import { StatsApiService } from '../../services/services/chatbot/stats-api.service';

@Component({
  selector: 'app-view-data',
  standalone: true,
  imports: [CommonModule, BaseChartDirective],
  templateUrl: './view-data.component.html',
  styles: `
    :host { display: block; }
    canvas { max-height: 340px; }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class ViewDataComponent implements OnInit {
  private readonly statsService = inject(StatsApiService);

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

  ngOnInit(): void {
    this.loadStats();
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