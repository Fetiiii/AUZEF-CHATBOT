import { ApplicationConfig, importProvidersFrom } from '@angular/core';
import { provideRouter } from '@angular/router';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';
import { provideCharts, withDefaultRegisterables } from 'ng2-charts';

import { routes } from './app.routes';

import { apiCredentialsInterceptor } from './services/services/api-credentials.interceptor';
import { chatbotSessionInterceptor } from './services/services/chatbot/chatbot-session.interceptor';

import { FroalaEditorModule, FroalaViewModule } from 'angular-froala-wysiwyg';

export const appConfig: ApplicationConfig = {
    providers: [
        provideRouter(routes),
        provideHttpClient(
            withInterceptors([
                // Sıra önemli değil: biri cookie taşır, diğeri 401'de login'e yönlendirir.
                // (CSM'den miras kalan Bearer-token interceptor'ları kaldırıldı —
                // bu panel oturum cookie'si kullanır, localStorage token'ı yoktur.)
                apiCredentialsInterceptor,
                chatbotSessionInterceptor
            ])
        ),
        provideAnimations(),
        provideCharts(withDefaultRegisterables()),
        importProvidersFrom(FroalaEditorModule, FroalaViewModule)
    ]
};