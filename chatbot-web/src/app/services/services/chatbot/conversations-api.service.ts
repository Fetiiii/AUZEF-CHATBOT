import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface ConversationSummary {
  id: number;
  started_at: string | null;
  ip_address: string | null;
  talep_status: string;
  message_count: number;
  first_question: string | null;
  avg_rating: number | null;
  rating_count: number;
}

export interface ConversationListResponse {
  total: number;
  items: ConversationSummary[];
}

export interface ConversationMessageItem {
  id: number;
  role: string;              // 'user' | 'bot'
  content: string;
  source: string | null;     // bot: meilisearch | qdrant_vector | llm | academic_calendar | none
  rating: number | null;
  rating_at: string | null;
  created_at: string | null;
}

export interface ConversationDetail {
  id: number;
  ip_address: string | null;
  talep_status: string;
  started_at: string | null;
  updated_at: string | null;
  messages: ConversationMessageItem[];
}

@Injectable({ providedIn: 'root' })
export class ConversationsApiService {
  private readonly base = '/api/conversations';

  constructor(private http: HttpClient) {}

  list(skip = 0, limit = 25, search = ''): Observable<ConversationListResponse> {
    const s = encodeURIComponent(search || '');
    return this.http.get<ConversationListResponse>(
      `${this.base}?skip=${skip}&limit=${limit}&search=${s}`
    );
  }

  detail(id: number): Observable<ConversationDetail> {
    return this.http.get<ConversationDetail>(`${this.base}/${id}`);
  }

  exportCsv(params: { ids?: number[]; start?: string; end?: string }): Observable<Blob> {
    const q = new URLSearchParams();
    if (params.ids && params.ids.length) q.set('ids', params.ids.join(','));
    if (params.start) q.set('start', params.start);
    if (params.end) q.set('end', params.end);
    return this.http.get(`${this.base}/export?${q.toString()}`, { responseType: 'blob' });
  }
}
