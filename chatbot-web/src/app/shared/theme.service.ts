
import { Injectable, effect, signal } from '@angular/core';
export type ThemeMode = 'light' | 'dark' | 'system';
@Injectable({ providedIn: 'root' })
export class ThemeService {
  mode = signal<ThemeMode>((localStorage.getItem('theme') as ThemeMode) || 'system');
  private media = window.matchMedia('(prefers-color-scheme: dark)');
  systemPrefersDark = signal<boolean>(this.media.matches);
  constructor(){
    this.media.addEventListener('change', e => this.systemPrefersDark.set(e.matches));
    effect(() => localStorage.setItem('theme', this.mode()));
  }
  setMode(m: ThemeMode){ this.mode.set(m); }
  cycle(){ const order: ThemeMode[] = ['system','light','dark']; const i = order.indexOf(this.mode()); this.setMode(order[(i+1)%order.length]); }
  isDark(){ return this.mode()==='dark' || (this.mode()==='system' && this.systemPrefersDark()); }
  applyTheme(){ const root = document.documentElement; if (this.isDark()) root.classList.add('dark'); else root.classList.remove('dark'); }
}
