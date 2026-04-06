import { Component, effect, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import {
    Router,
    RouterOutlet,
    NavigationStart,
    NavigationEnd,
    NavigationCancel,
    NavigationError,
    Event as RouterEvent
} from '@angular/router';
import { ThemeService } from './shared/theme.service';

@Component({
    selector: 'app-root',
    standalone: true,
    imports: [CommonModule, RouterOutlet],
    template: `
    <router-outlet></router-outlet>

    <!-- ✅ Global Technical Support Modal (1 kere) -->
    

    <!-- Global route loading overlay -->
    <div
      *ngIf="isRouteLoading()"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">

      <!-- DİKDÖRTGEN YÜKLENİYOR KUTUSU -->
      <div
        class="flex flex-col items-center gap-3 px-8 py-5
               bg-white/95 dark:bg-slate-900/98
               border border-gray-200/90 dark:border-slate-700/90
               shadow-2xl
               rounded-md w-80 max-w-[90%]">

        <div
          class="h-9 w-9 rounded-full border-2 border-slate-300 dark:border-slate-600
                 border-t-slate-700 dark:border-t-slate-100 animate-spin">
        </div>

        <div class="text-sm font-semibold text-slate-800 dark:text-slate-100 tracking-wide">
          {{ loadingMessage() }}
        </div>

        <div class="text-[11px] text-slate-500 dark:text-slate-400 text-center">
          Lütfen bekleyin, yönlendiriliyorsunuz...
        </div>
      </div>
    </div>
  `
})
export class AppComponent {
    // Overlay için sinyaller
    isRouteLoading = signal(false);
    loadingMessage = signal('Yükleniyor');

    // Yavaş / hissedilir geçiş için
    private navigationStartTime = 0;
    private hideTimeoutId: any;

    constructor(
        private theme: ThemeService,
        private router: Router
    ) {
        effect(() => this.theme.applyTheme());

        this.router.events.subscribe((event: RouterEvent) => {
            if (event instanceof NavigationStart) {
                this.navigationStartTime = Date.now();

                if (this.hideTimeoutId) {
                    clearTimeout(this.hideTimeoutId);
                    this.hideTimeoutId = null;
                }

                this.isRouteLoading.set(true);

                const url = event.url ?? '';

                if (url.includes('/auth/sign-in')) {
                    this.loadingMessage.set('Giriş yapılıyor');
                } else if (url.includes('/auth/sign-up')) {
                    this.loadingMessage.set('Kayıt yapılıyor');
                } else if (url.includes('logout')) {
                    this.loadingMessage.set('Çıkış yapılıyor');
                } else if (url.includes('/auth')) {
                    this.loadingMessage.set('İşleminiz hazırlanıyor');
                } else if (url === '/' || url.includes('/dashboard')) {
                    this.loadingMessage.set('Panel yükleniyor');
                } else {
                    this.loadingMessage.set('Sayfa yükleniyor');
                }

            } else if (
                event instanceof NavigationEnd ||
                event instanceof NavigationCancel ||
                event instanceof NavigationError
            ) {
                const MIN_DURATION = 0;
                const elapsed = Date.now() - this.navigationStartTime;
                const remaining = Math.max(0, MIN_DURATION - elapsed);

                if (remaining > 0) {
                    this.hideTimeoutId = setTimeout(() => {
                        this.isRouteLoading.set(false);
                        this.hideTimeoutId = null;
                    }, remaining);
                } else {
                    this.isRouteLoading.set(false);
                }
            }
        });
    }
}
