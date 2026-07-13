import {
  ChangeDetectionStrategy,
  Component,
  Input,
  OnChanges,
  SimpleChanges,
  signal
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterLink, RouterLinkActive } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { I18nService } from 'src/app/shared/i18n.service';
import { ChatbotAuthService } from 'src/app/services/services/chatbot/chatbot-auth.service';
import { roleLevel } from 'src/app/services/models/chatbot/chatbot-auth.model';

type ActiveUser = { name: string; avatar: string; online: boolean };

@Component({
  selector: 'app-chatbot-sidebar',
  standalone: true,
  imports: [CommonModule, RouterLink, RouterLinkActive],
  templateUrl: './sidebar.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class ChatbotSidebarComponent implements OnChanges {
  @Input() collapsed = false;
  @Input() myPendingCount = 0;
  @Input() allPending = 0;
  @Input() activeUsers: ActiveUser[] = [];

  // Accordion state
  openUsers = signal<boolean>(false);
  openAdmin = signal<boolean>(false);

  // ✅ I18n key + label
  private loginLabelSig = signal<string>('');

  // Oturumdaki kullanıcının rol seviyesi (menü öğelerini yetkiye göre gizlemek
  // için — gerçek yaptırım backend'dedir, burası yalnızca UX).
  private roleLevelSig = signal<number>(0);

  constructor(
    public i18n: I18nService,
    private auth: ChatbotAuthService,
  ) {
    this.auth.user$.pipe(takeUntilDestroyed()).subscribe((u) => {
      this.roleLevelSig.set(roleLevel(u?.role));
    });
  }

  /** Konuşmalar + İzleme Paneli: en az admin */
  canSeeAdmin(): boolean {
    return this.roleLevelSig() >= 2;
  }

  /** Ayarlar: yalnızca super_admin */
  canSeeSuper(): boolean {
    return this.roleLevelSig() >= 3;
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['collapsed'] && this.collapsed) {
      this.openUsers.set(false);
      this.openAdmin.set(false);
    }


  }



  /** Template’de kullan */
  loginLabel(): string {
    return this.loginLabelSig();
  }

  toggleUsers(): void {
    if (this.collapsed) return;
    this.openUsers.update((v) => !v);
  }

  toggleAdmin(): void {
    if (this.collapsed) return;
    this.openAdmin.update((v) => !v);
  }

  onUsersToggle(event: MouseEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.toggleUsers();
  }

  onAdminToggle(event: MouseEvent): void {
    event.preventDefault();
    event.stopPropagation();
    this.toggleAdmin();
  }
}
