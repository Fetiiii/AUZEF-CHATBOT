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

  // Tarih aralığı export'u (GET)
  exportCsv(params: { start?: string; end?: string }): Observable<Blob> {
    const q = new URLSearchParams();
    if (params.start) q.set('start', params.start);
    if (params.end) q.set('end', params.end);
    return this.http.get(`${this.base}/export?${q.toString()}`, { responseType: 'blob' });
  }

  // Seçili id'lerin export'u — POST (binlerce id URL'e sığmasın diye).
  // Yalnızca ELLE seçilmiş (tamamı değil) küçük kümeler için kullanılır —
  // "tümünü seç" exportBySearch kullanır (bkz. o metodun notu).
  exportByIds(ids: number[]): Observable<Blob> {
    return this.http.post(`${this.base}/export`, { ids }, { responseType: 'blob' });
  }

  // "Tümünü seç" export'u — id listesi GÖNDERMEZ, aynı arama filtresini
  // backend'e verir. Eskiden allIds() + exportByIds() zinciriyle TÜM id'ler
  // POST body'sinde gidiyordu; 592K konuşmada bu ~4MB'a çıkıp nginx'in
  // varsayılan 1MB limitini aşıp 413 veriyordu (canlıda yakalandı).
  // select_all=true ZORUNLU: search boş olsa bile (arama kutusu boşken
  // "tümünü seç") backend bunu "filtre yok" ile karıştırıp 400 dönmemeli.
  exportBySearch(search = ''): Observable<Blob> {
    const q = new URLSearchParams();
    q.set('select_all', 'true');
    if (search) q.set('search', search);
    return this.http.get(`${this.base}/export?${q.toString()}`, { responseType: 'blob' });
  }

  // Filtreye uyan TÜM id'ler ("tümünü seç" için)
  allIds(search = ''): Observable<{ ids: number[] }> {
    const q = new URLSearchParams();
    if (search) q.set('search', search);
    return this.http.get<{ ids: number[] }>(`${this.base}/ids?${q.toString()}`);
  }
}
