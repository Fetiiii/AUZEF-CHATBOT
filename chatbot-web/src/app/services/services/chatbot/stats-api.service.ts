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

@Injectable({ providedIn: 'root' })
export class StatsApiService {
  private readonly base = '/api/stats';

  constructor(private http: HttpClient) {}

  getStats(): Observable<StatsResponse> {
    return this.http.get<StatsResponse>(this.base);
  }
}
