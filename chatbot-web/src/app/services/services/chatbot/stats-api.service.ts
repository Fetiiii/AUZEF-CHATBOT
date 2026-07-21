import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export type TrafficMode = 'hourly' | 'weekly' | 'monthly';

export interface TrafficPoint {
  label: string;              // x-ekseni etiketi (saat / kısa gün adı / gün numarası)
  count: number;
  date: string | null;        // YYYY-MM-DD (haftalık/aylık ve belirli gün için)
  weekday: string | null;     // "Çarşamba" vb. (tooltip için)
}

export interface StatsResponse {
  active_users: number;
  total_queries_today: number;
  mode: TrafficMode;
  period_label: string;
  traffic: TrafficPoint[];
  /** @deprecated `traffic` ile aynı — geriye dönük uyum için tutuluyor */
  hourly: TrafficPoint[];
  sources: {
    meilisearch: number;
    qdrant_vector: number;
    llm: number;
    none: number;
  };
}

export interface ConversationStats {
  total_conversations: number;
  rating_distribution: Record<string, number>;   // "1".."5" -> adet
  outcome: { olumlu: number; olumsuz: number; puansiz: number };
  talep: { redirected: number; declined: number; not_offered: number };
}

@Injectable({ providedIn: 'root' })
export class StatsApiService {
  private readonly base = '/api/stats';

  constructor(private http: HttpClient) {}

  getStats(mode: TrafficMode = 'hourly', date = ''): Observable<StatsResponse> {
    const q = new URLSearchParams();
    if (mode) q.set('mode', mode);
    if (date) q.set('date', date);
    const qs = q.toString();
    return this.http.get<StatsResponse>(qs ? `${this.base}?${qs}` : this.base);
  }

  getConversationStats(start: string, end: string): Observable<ConversationStats> {
    const q = new URLSearchParams();
    if (start) q.set('start', start);
    if (end) q.set('end', end);
    return this.http.get<ConversationStats>(`${this.base}/conversations?${q.toString()}`);
  }
}
