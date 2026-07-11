import { ChangeDetectionStrategy, ChangeDetectorRef, Component, HostListener } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';

import { ThemeService, ThemeMode } from '../../shared/theme.service';
import { I18nService } from '../../shared/i18n.service';
import { ChatbotAuthService } from '../../services/services/chatbot/chatbot-auth.service';

@Component({
  standalone: true,
  selector: 'app-sign-in',
  imports: [CommonModule, ReactiveFormsModule],
  templateUrl: './sign-in.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class SignInComponent {
  langOpen = false;

  showPass = false;
  loading = false;
  loginErrorMessage: string | null = null;

  private readonly AFTER_LOGIN_URL = '/chatbot/document-upload';

  form = this.fb.group({
    identifier: ['', [Validators.required]],
    password: ['', [Validators.required]],
    remember: [false]
  });

  constructor(
    private fb: FormBuilder,
    private router: Router,
    private route: ActivatedRoute,
    private auth: ChatbotAuthService,
    private cdr: ChangeDetectorRef,
    public theme: ThemeService,
    public i18n: I18nService
  ) {
    const remembered = this.auth.getRememberedEmail();
    if (remembered) {
      this.form.patchValue({ identifier: remembered, remember: true });
    }
  }

  // ✅ i18n yoksa bile patlamasın diye küçük helper
  t(key: string): string {
    try {
      const val = this.i18n?.t?.(key);
      if (typeof val === 'string' && val.trim()) return val;
    } catch { }
    // fallback (istersen genişlet)
    const fallback: Record<string, string> = {
      'auth.signIn': 'Giriş Yap',
      'chatbot-sign-in-text': 'Chatbot paneline erişmek için giriş yapın.',
      'this-field-is-mandatory': 'Bu alan zorunludur.',
      'remember-me': 'Beni hatırla',
      'technicalsupport': 'Teknik Destek',
      'theme.system': 'Sistem',
      'theme.light': 'Açık',
      'theme.dark': 'Koyu',
      'university': 'İstanbul Üniversitesi',
      'faculty': 'Açık ve Uzaktan Eğitim Fakültesi',
      'system': 'CSM',
      'chatbot': 'Chatbot',
      'error': 'Hata',
      'signing-in': 'Giriş yapılıyor...'
    };
    return fallback[key] ?? key;
  }

  lang(): 'tr' | 'en' {
    // i18n servisinde lang() varsa onu kullanır, yoksa tr döner
    try {
      const l = this.i18n?.lang?.();
      if (l === 'tr' || l === 'en') return l;
    } catch { }
    return 'tr';
  }

  @HostListener('document:click')
  closeMenus() {
    this.langOpen = false;
  }

  setTheme(v: string) {
    this.theme.setMode(v as ThemeMode);
  }

  setLang(v: 'tr' | 'en') {
    this.i18n.setLang(v);
    this.langOpen = false;
  }

  currentFlag() {
    return this.lang() === 'tr' ? 'assets/flags/tr.svg' : 'assets/flags/en.svg';
  }

  togglePass() {
    this.showPass = !this.showPass;
  }

  identifierInvalid() {
    const c = this.form.controls.identifier;
    return c.touched && c.invalid;
  }

  passwordInvalid() {
    const c = this.form.controls.password;
    return c.touched && c.invalid;
  }


  submit() {
    if (this.form.invalid || this.loading) {
      this.form.markAllAsTouched();
      return;
    }

    this.loading = true;
    this.loginErrorMessage = null;

    const email = (this.form.value.identifier || '').trim();
    const password = String(this.form.value.password || '');
    const remember = !!this.form.value.remember;

    // ✅ ReturnUrl (örn: /chatbot/document-upload)
    const returnUrl = (this.route.snapshot.queryParamMap.get('returnUrl') || '').trim();

    // Gerçek oturum: backend parolayı doğrular, HttpOnly session cookie kurar.
    // (Eski istemci-taraflı fb/1 kontrolü kaldırıldı — bundle'ı okuyan herkes
    // görebiliyordu ve localStorage bayrağı DevTools'tan set edilebiliyordu.)
    this.auth.login({ email, password, remember }).subscribe((res) => {
      this.loading = false;

      if (!res.ok) {
        this.loginErrorMessage = res.message ?? 'Giriş başarısız.';
        this.cdr.markForCheck();
        return;
      }

      const target = this.safeReturnUrl(returnUrl) || this.AFTER_LOGIN_URL;

      this.router
        .navigateByUrl(target, { replaceUrl: true })
        .then((navOk) => {
          if (!navOk) window.location.assign(target);
        })
        .catch(() => window.location.assign(target));
    });
  }

  private safeReturnUrl(url: string): string | null {
    if (!url) return null;
    if (!url.startsWith('/')) return null;
    if (url.startsWith('//')) return null;
    if (url.startsWith('/chatbot/sign-in')) return null;
    return url;
  }
}
