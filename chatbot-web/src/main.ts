

import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app/app.component';
import { appConfig } from './app/app.config';
import { registerLocaleData } from '@angular/common';
import localeTr from '@angular/common/locales/tr';

// Türkçe yerel verilerini Angular'a kaydet
registerLocaleData(localeTr, 'tr-TR');

bootstrapApplication(AppComponent, appConfig).catch(console.error);
