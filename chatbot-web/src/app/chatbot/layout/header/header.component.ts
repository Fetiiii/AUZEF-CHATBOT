import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  EventEmitter,
  HostListener,
  Input,
  OnDestroy,
  OnInit,
  Output,
  signal
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { NavigationEnd, Router, RouterModule } from '@angular/router';
import { Subject } from 'rxjs';
import { filter, takeUntil } from 'rxjs/operators';

import { I18nService } from 'src/app/shared/i18n.service';
import { ThemeMode, ThemeService } from 'src/app/shared/theme.service';
import { ChatbotAuthService } from 'src/app/services/services/chatbot/chatbot-auth.service';


type UserStatus = 'available' | 'away';

@Component({
  standalone: true,
  selector: 'app-chatbot-header',
  imports: [CommonModule, RouterModule],
  templateUrl: './header.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class HeaderComponent implements OnInit, OnDestroy {
  @Input() offsetLeft: string = '0';
  @Output() toggleSidebar = new EventEmitter<void>();

  // dropdown / panel durumları
  langOpen = false;
  userMenuOpen = false;
  notificationPanelOpen = false;

  userStatus: UserStatus = 'available';

  // Oturumdaki kullanıcıdan doldurulur (guard geçtiği için önbellekte hazırdır)
  userName = 'Personel';

  // Başlık artık signal → OnPush ile sorunsuz
  pageTitle = signal<string>('');

  private destroy$ = new Subject<void>();

  constructor(
    public theme: ThemeService,
    public i18n: I18nService,
    private eRef: ElementRef,
    private router: Router,
    private auth: ChatbotAuthService,
  ) { }

  ngOnInit(): void {
    // Oturum kullanıcısını göster (guard sayesinde login'den geçilmiş durumda)
    const u = this.auth.user();
    if (u) this.userName = u.full_name || u.email;

    // İlk yüklemede geçerli route başlığını çek
    this.updateTitleFromRouteTree();

    // Her navigation sonrasında tekrar güncelle
    this.router.events
      .pipe(
        filter((event) => event instanceof NavigationEnd),
        takeUntil(this.destroy$)
      )
      .subscribe(() => this.updateTitleFromRouteTree());

    // Kullanıcı adını localStorage'dan çek (chatbot tarafı)
    this.setUserNameFromStorage();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  // === User name ===
  private setUserNameFromStorage(): void {
    const storedName =
      localStorage.getItem('chatbot_operator_name') ??
      localStorage.getItem('chatbot_remember_id');

    if (storedName && storedName.trim() !== '') {
      this.userName = storedName;
    }
  }

  // === Global click / esc ===
  @HostListener('document:click', ['$event'])
  onDocumentClick(event: MouseEvent) {
    const clickedInside = this.eRef.nativeElement.contains(
      event.target as Node
    );
    if (!clickedInside) {
      this.langOpen = false;
      this.userMenuOpen = false;
      this.notificationPanelOpen = false;
    }
  }

  @HostListener('document:keydown.escape')
  onEsc() {
    this.langOpen = false;
    this.userMenuOpen = false;
    this.notificationPanelOpen = false;
  }

  // === Route başlığını güncelleyen asıl fonksiyon ===
  private updateTitleFromRouteTree(): void {
    // Router durumunun en kökünden başla
    let current = this.router.routerState.root;

    // En derindeki aktif child route’a kadar in
    while (current.firstChild) {
      current = current.firstChild;
    }

    const data = current.snapshot.data || {};
    const titleKey = data['title'] as string | undefined;

    // chatbot tarafı için anlamlı bir fallback verelim
    const safeKey = titleKey || 'chatbot.request-create';

    this.pageTitle.set(this.i18n.t(safeKey));
  }

  

  



  
  // === UI Actions ===
  onToggleSidebar() {
    this.toggleSidebar.emit();
  }

  setTheme(v: string) {
    this.theme.setMode(v as ThemeMode);
  }

  toggleLangMenu() {
    this.langOpen = !this.langOpen;
    if (this.langOpen) {
      this.userMenuOpen = false;
      this.notificationPanelOpen = false;
    }
  }

  toggleUserMenu() {
    this.userMenuOpen = !this.userMenuOpen;
    if (this.userMenuOpen) {
      this.langOpen = false;
      this.notificationPanelOpen = false;
    }
  }

  closeUserMenu() {
    this.userMenuOpen = false;
  }

  

  setLang(v: 'tr' | 'en') {
    this.i18n.setLang(v);
    this.langOpen = false;

    // Dil değişince başlığı yeniden çevir
    this.updateTitleFromRouteTree();
  }

  currentFlag() {
    return this.i18n.lang() === 'tr'
      ? 'assets/flags/tr.svg'
      : 'assets/flags/en.svg';
  }

  setUserStatus(s: UserStatus) {
    this.userStatus = s;
    this.userMenuOpen = false;
  }

  statusLabel() {
    return this.userStatus === 'available'
      ? this.i18n.t('user.available')
      : this.i18n.t('user.away');
  }

  changePassword() {
    this.userMenuOpen = false;
    this.router.navigate(['/chatbot/profile']);
  }

  openProfile() {
    this.userMenuOpen = false;
    this.router.navigate(['/chatbot/profile']);
  }

  openPreparedMessages() {
    this.userMenuOpen = false;
    this.router.navigate(['/chatbot/prepared-messages']);
  }

  goHome() {
    this.router.navigate(['/chatbot/home']);
  }

  logout() {
    this.userMenuOpen = false;
    // Backend'deki oturumu kapat (DB'den silinir → cookie anında geçersizleşir),
    // sonra login'e dön. "Beni hatırla" e-postasına dokunulmaz.
    this.auth.logout().subscribe(() => {
      this.router.navigate(['/chatbot/sign-in']);
    });
  }
}
