import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  signal
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { BaseChartDirective } from 'ng2-charts';
import { ChartConfiguration, ChartData } from 'chart.js';

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
    this.loadMockData();
  }

  /* ────────────────────────────────────────────
     MOCK DATA — replace with real API calls later
     ──────────────────────────────────────────── */
  private loadMockData(): void {
    const now = new Date();
    const currentHour = now.getHours();

    // ── 1. Build 24-hour traffic with a realistic bell curve ──
    const labels: string[] = [];
    const data: number[] = [];
    let total = 0;

    for (let i = 0; i < 24; i++) {
      const hour = (currentHour - 23 + i + 24) % 24;
      labels.push(`${hour.toString().padStart(2, '0')}:00`);

      // Bell curve peak at 11:00-13:00
      const dist = Math.abs(hour - 12);
      const base = Math.max(2, Math.round(55 * Math.exp(-(dist * dist) / 18)));
      const jitter = Math.round((Math.random() - 0.5) * 12);
      const value = Math.max(0, base + jitter);
      data.push(value);
      total += value;
    }

    this.totalQueriesToday.set(total);
    this.hourlyLabels = labels;
    this.hourlyChartData.set({
      labels,
      datasets: [
        {
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
        }
      ]
    });

    // ── 2. Active users (random realistic count) ──
    this.activeUsers.set(Math.floor(Math.random() * 18) + 3);

    // ── 3. Resolution source breakdown ──
    const llm = Math.round(total * 0.42);
    const meili = Math.round(total * 0.45);
    const none = total - llm - meili;

    this.llmCount.set(llm);
    this.meiliCount.set(meili);
    this.noAnswerCount.set(none);

    this.doughnutChartData.set({
      labels: ['LLM (Yapay Zeka)', 'MeiliSearch', 'Cevapsız'],
      datasets: [
        {
          data: [llm, meili, none],
          backgroundColor: ['#7c3aed', '#10b981', '#f59e0b'],
          borderColor: ['#fff', '#fff', '#fff'],
          borderWidth: 2,
          hoverOffset: 6
        }
      ]
    });
  }
}
