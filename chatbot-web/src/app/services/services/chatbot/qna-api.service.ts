import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface QnAItem {
  id: number;
  question_text: string;
  answer_text: string;
  status: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface QnACreateRequest {
  question_text: string;
  answer_text: string;
  status?: number;
}

export interface QnAUpdateRequest {
  question_text?: string;
  answer_text?: string;
  status?: number;
}

export interface QnABulkUpdateItem {
  id: number;
  question_text?: string;
  answer_text?: string;
  status?: number;
}

@Injectable({ providedIn: 'root' })
export class QnaApiService {
  private readonly base = '/api/qna';

  constructor(private http: HttpClient) {}

  getAll(skip = 0, limit = 500): Observable<QnAItem[]> {
    return this.http.get<QnAItem[]>(`${this.base}?skip=${skip}&limit=${limit}`);
  }

  create(body: QnACreateRequest): Observable<QnAItem> {
    return this.http.post<QnAItem>(this.base, body);
  }

  update(id: number, body: QnAUpdateRequest): Observable<QnAItem> {
    return this.http.put<QnAItem>(`${this.base}/${id}`, body);
  }

  delete(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/${id}`);
  }

  bulkUpdate(items: QnABulkUpdateItem[]): Observable<{ updated_ids: number[]; count: number }> {
    return this.http.put<{ updated_ids: number[]; count: number }>(`${this.base}/bulk-update`, items);
  }

  importCsv(file: File): Observable<{ imported: number }> {
    const form = new FormData();
    form.append('file', file);
    return this.http.post<{ imported: number }>(`${this.base}/import`, form);
  }

  exportCsv(): Observable<Blob> {
    return this.http.get(`${this.base}/export`, { responseType: 'blob' });
  }
}
