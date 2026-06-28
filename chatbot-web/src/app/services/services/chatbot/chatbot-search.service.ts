import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface SearchResponse {
  source: 'meilisearch' | 'qdrant_vector' | 'llm' | 'academic_calendar' | 'none';
  status: 'success' | 'suggest' | 'error';
  answer?: string;
  question?: string;
  score?: number;
  context_used?: string[];
  message?: string;
  suggestions?: string[];
}

@Injectable({ providedIn: 'root' })
export class ChatbotSearchService {
  private readonly base = '/api/search';

  constructor(private http: HttpClient) {}

  search(query: string): Observable<SearchResponse> {
    const params = encodeURIComponent(query);
    return this.http.get<SearchResponse>(`${this.base}?q=${params}`);
  }
}
