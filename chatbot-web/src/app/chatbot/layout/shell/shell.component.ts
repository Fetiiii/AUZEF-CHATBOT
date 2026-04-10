import {
  ChangeDetectionStrategy,
  Component,
  HostListener,
  signal
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterOutlet } from '@angular/router';

import { HeaderComponent as ChatbotHeaderComponent } from '../header/header.component';
import { ChatbotSidebarComponent } from '../sidebar/sidebar.component';
import { FooterComponent as ChatbotFooterComponent } from '../footer/footer.component';


@Component({
  standalone: true,
  selector: 'app-chatbot-shell',
  imports: [
    CommonModule,
    RouterOutlet,
    ChatbotHeaderComponent,
    ChatbotSidebarComponent,
    ChatbotFooterComponent,
  ],
  templateUrl: './shell.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class ShellComponent {
  // Sol menü dar / geniş
  sidebarCollapsed = signal<boolean>(false);

  // Ekran ≥ lg?
  private lgMQ = window.matchMedia('(min-width: 1024px)');
  isLg = signal<boolean>(this.lgMQ.matches);

  // İçerik / header için sol offset
  offsetLeft = signal<string>(this.computeOffset());

  // Scroll-to-top
  showUp = signal<boolean>(false);

  @HostListener('window:scroll')
  onScroll() {
    this.showUp.set(window.scrollY > 300);
  }

  goTop() {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  constructor() {
    const onChange = (e: MediaQueryListEvent) => {
      this.isLg.set(e.matches);
      this.recalcOffset();
    };

    // modern tarayıcılar
    this.lgMQ.addEventListener?.('change', onChange);
    // eski tarayıcılar fallback
    // @ts-ignore
    this.lgMQ.addListener?.(onChange);

    this.showUp.set(window.scrollY > 300);
  }

  // === Actions ===
  toggleSidebar() {
    this.sidebarCollapsed.set(!this.sidebarCollapsed());
    this.recalcOffset();
  }

  // === Helpers ===
  private computeOffset(): string {
    // Mobilde sidebar overlay / gizli → offset 0
    if (!this.isLg()) return '0';

    // Chatbot sidebar genişliği:
    // collapsed: w-20 = 5rem
    // expanded:  w-72 = 18rem  (GLOBAL İLE AYNI)
    return this.sidebarCollapsed() ? '5rem' : '18rem';
  }

  private recalcOffset() {
    this.offsetLeft.set(this.computeOffset());
  }
}
