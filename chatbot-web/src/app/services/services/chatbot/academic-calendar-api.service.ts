import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface AcademicCalendarItem {
  id: number;
  period: string;
  event: string;
  start_date: string;
  end_date: string;
  updated_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AcademicCalendarCreateRequest {
  period: string;
  event: string;
  start_date: string;
  end_date: string;
}

export interface AcademicCalendarUpdateRequest {
  period?: string;
  event?: string;
  start_date?: string;
  end_date?: string;
}

export interface AcademicCalendarBulkUpdateItem {
  id: number;
  period?: string;
  event?: string;
  start_date?: string;
  end_date?: string;
}

@Injectable({ providedIn: 'root' })
export class AcademicCalendarApiService {
  private readonly base = '/api/academic-calendar';

  constructor(private http: HttpClient) {}

  getAll(skip = 0, limit = 500): Observable<AcademicCalendarItem[]> {
    return this.http.get<AcademicCalendarItem[]>(`${this.base}?skip=${skip}&limit=${limit}`);
  }

  create(body: AcademicCalendarCreateRequest): Observable<AcademicCalendarItem> {
    return this.http.post<AcademicCalendarItem>(this.base, body);
  }

  update(id: number, body: AcademicCalendarUpdateRequest): Observable<AcademicCalendarItem> {
    return this.http.put<AcademicCalendarItem>(`${this.base}/${id}`, body);
  }

  delete(id: number): Observable<void> {
    return this.http.delete<void>(`${this.base}/${id}`);
  }

  bulkUpdate(items: AcademicCalendarBulkUpdateItem[]): Observable<{ updated_ids: number[]; count: number }> {
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
