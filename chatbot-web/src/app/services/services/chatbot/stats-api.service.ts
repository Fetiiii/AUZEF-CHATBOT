import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface StatsResponse {
  active_users: number;
  total_queries_today: number;
  hourly: { label: string; count: number }[];
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

  getStats(): Observable<StatsResponse> {
    return this.http.get<StatsResponse>(this.base);
  }

  getConversationStats(start: string, end: string): Observable<ConversationStats> {
    const q = new URLSearchParams();
    if (start) q.set('start', start);
    if (end) q.set('end', end);
    return this.http.get<ConversationStats>(`${this.base}/conversations?${q.toString()}`);
  }
}
