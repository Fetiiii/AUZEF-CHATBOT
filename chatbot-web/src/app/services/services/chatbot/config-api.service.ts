import { Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface LLMConfigResponse {
  enabled: boolean;
}

export interface LLMConfigUpdateResponse {
  enabled: boolean;
  message: string;
}

@Injectable({ providedIn: 'root' })
export class ConfigApiService {
  private readonly base = '/api/config';

  constructor(private http: HttpClient) {}

  getLLMStatus(): Observable<LLMConfigResponse> {
    return this.http.get<LLMConfigResponse>(`${this.base}/llm`);
  }

  setLLMStatus(enabled: boolean): Observable<LLMConfigUpdateResponse> {
    return this.http.put<LLMConfigUpdateResponse>(`${this.base}/llm`, { enabled });
  }
}
