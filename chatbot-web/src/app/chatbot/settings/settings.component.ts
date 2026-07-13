import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnInit, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';

import {
  LLMSettings,
  SettingsApiService,
  SettingsUser
} from '../../services/services/chatbot/settings-api.service';
import { ChatbotAuthService } from '../../services/services/chatbot/chatbot-auth.service';
import { AdminRole } from '../../services/models/chatbot/chatbot-auth.model';

/**
 * Ayarlar sayfası — yalnızca super_admin (route guard + backend middleware).
 * İki bölüm: LLM ayarları (aç/kapa + OpenRouter anahtarı) ve kullanıcı yönetimi.
 */
@Component({
  standalone: true,
  selector: 'app-chatbot-settings',
  imports: [CommonModule, FormsModule],
  templateUrl: './settings.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class SettingsComponent implements OnInit {
  // ── Toast ──
  toastMessage = signal<string | null>(null);
  toastType = signal<'success' | 'error'>('success');

  // ── LLM ──
  llm = signal<LLMSettings | null>(null);
  llmSaving = signal(false);
  newApiKey = '';

  // ── Kullanıcılar ──
  users = signal<SettingsUser[]>([]);
  usersLoading = signal(true);
  savingUserId = signal<number | null>(null);

  roles: { value: AdminRole; label: string }[] = [
    { value: 'editor', label: 'Editör' },
    { value: 'admin', label: 'Admin' },
    { value: 'super_admin', label: 'Super Admin' }
  ];

  // Yeni kullanıcı formu
  creating = signal(false);
  newUser = { email: '', full_name: '', role: 'editor' as AdminRole, password: '' };

  constructor(
    private api: SettingsApiService,
    private auth: ChatbotAuthService,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    this.loadLLM();
    this.loadUsers();
  }

  /** Kendi satırında rol/aktiflik alanlarını kilitlemek için. */
  isSelf(u: SettingsUser): boolean {
    return this.auth.user()?.id === u.id;
  }

  roleLabel(role: string): string {
    return this.roles.find((r) => r.value === role)?.label ?? role;
  }

  private showToast(msg: string, type: 'success' | 'error' = 'success'): void {
    this.toastMessage.set(msg);
    this.toastType.set(type);
    this.cdr.markForCheck();
    setTimeout(() => {
      this.toastMessage.set(null);
      this.cdr.markForCheck();
    }, 3500);
  }

  private errDetail(err: HttpErrorResponse, fallback: string): string {
    return err?.error?.detail || fallback;
  }

  // ── LLM ─────────────────────────────────────────────

  private loadLLM(): void {
    this.api.getLLM().subscribe({
      next: (s) => { this.llm.set(s); this.cdr.markForCheck(); },
      error: () => this.showToast('LLM ayarları yüklenemedi.', 'error')
    });
  }

  toggleLLM(): void {
    const current = this.llm();
    if (!current || this.llmSaving()) return;
    this.llmSaving.set(true);
    this.api.updateLLM({ enabled: !current.enabled }).subscribe({
      next: (s) => {
        this.llm.set(s);
        this.llmSaving.set(false);
        this.showToast(s.enabled ? 'LLM etkinleştirildi.' : 'LLM devre dışı bırakıldı.');
      },
      error: (err) => {
        this.llmSaving.set(false);
        this.showToast(this.errDetail(err, 'LLM ayarı değiştirilemedi.'), 'error');
      }
    });
  }

  saveApiKey(): void {
    const key = this.newApiKey.trim();
    if (!key || this.llmSaving()) return;
    this.llmSaving.set(true);
    this.api.updateLLM({ openrouter_api_key: key }).subscribe({
      next: (s) => {
        this.llm.set(s);
        this.llmSaving.set(false);
        this.newApiKey = '';
        this.showToast('OpenRouter anahtarı kaydedildi.');
      },
      error: (err) => {
        this.llmSaving.set(false);
        this.showToast(this.errDetail(err, 'Anahtar kaydedilemedi.'), 'error');
      }
    });
  }

  clearApiKey(): void {
    if (this.llmSaving()) return;
    if (!confirm('Panelden girilen anahtar silinecek ve sunucu .env anahtarına (varsa) dönülecek. Emin misiniz?')) return;
    this.llmSaving.set(true);
    this.api.updateLLM({ openrouter_api_key: '' }).subscribe({
      next: (s) => {
        this.llm.set(s);
        this.llmSaving.set(false);
        this.showToast('Panel anahtarı silindi.');
      },
      error: (err) => {
        this.llmSaving.set(false);
        this.showToast(this.errDetail(err, 'Anahtar silinemedi.'), 'error');
      }
    });
  }

  // ── Kullanıcılar ────────────────────────────────────

  private loadUsers(): void {
    this.usersLoading.set(true);
    this.api.listUsers().subscribe({
      next: (res) => {
        this.users.set(res.users);
        this.usersLoading.set(false);
        this.cdr.markForCheck();
      },
      error: () => {
        this.usersLoading.set(false);
        this.showToast('Kullanıcılar yüklenemedi.', 'error');
      }
    });
  }

  createUser(): void {
    const u = this.newUser;
    if (!u.email.trim() || !u.password || this.creating()) return;
    if (u.password.length < 10) {
      this.showToast('Parola en az 10 karakter olmalı.', 'error');
      return;
    }
    this.creating.set(true);
    this.api.createUser({
      email: u.email.trim(),
      password: u.password,
      full_name: u.full_name.trim() || null,
      role: u.role
    }).subscribe({
      next: (created) => {
        this.users.update((list) => [...list, created]);
        this.creating.set(false);
        this.newUser = { email: '', full_name: '', role: 'editor', password: '' };
        this.showToast(`Kullanıcı oluşturuldu: ${created.email}`);
      },
      error: (err) => {
        this.creating.set(false);
        this.showToast(this.errDetail(err, 'Kullanıcı oluşturulamadı.'), 'error');
      }
    });
  }

  private patchUser(u: SettingsUser, body: object, okMsg: string): void {
    this.savingUserId.set(u.id);
    this.api.updateUser(u.id, body).subscribe({
      next: (updated) => {
        this.users.update((list) => list.map((x) => (x.id === updated.id ? updated : x)));
        this.savingUserId.set(null);
        this.showToast(okMsg);
      },
      error: (err) => {
        this.savingUserId.set(null);
        // Başarısız değişiklikte listeyi tazele (select eski değere dönsün)
        this.loadUsers();
        this.showToast(this.errDetail(err, 'Güncelleme başarısız.'), 'error');
      }
    });
  }

  onRoleChange(u: SettingsUser, role: string): void {
    this.patchUser(u, { role }, `${u.email} → ${this.roleLabel(role)} (açık oturumları kapatıldı)`);
  }

  toggleActive(u: SettingsUser): void {
    const next = !u.is_active;
    const msg = next ? `${u.email} aktifleştirildi.` : `${u.email} pasifleştirildi (oturumları kapatıldı).`;
    this.patchUser(u, { is_active: next }, msg);
  }

  resetPassword(u: SettingsUser): void {
    const pw = prompt(`${u.email} için yeni parola (en az 10 karakter):`);
    if (pw === null) return;
    if (pw.length < 10) {
      this.showToast('Parola en az 10 karakter olmalı.', 'error');
      return;
    }
    this.patchUser(u, { password: pw }, `${u.email} parolası sıfırlandı (oturumları kapatıldı).`);
  }
}
