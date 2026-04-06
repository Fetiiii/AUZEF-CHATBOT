import { ApplicationConfig, importProvidersFrom } from '@angular/core';
import { provideRouter } from '@angular/router';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';
import { provideCharts, withDefaultRegisterables } from 'ng2-charts';

import { routes } from './app.routes';

import { apiCredentialsInterceptor } from './services/services/api-credentials.interceptor';
import { adminAuthInterceptor } from './services/services/admin/admin-auth.interceptor';
import { authInterceptor } from './services/services/core/auth/auth.interceptor';

import { FroalaEditorModule, FroalaViewModule } from 'angular-froala-wysiwyg';

export const appConfig: ApplicationConfig = {
    providers: [
        provideRouter(routes),
        provideHttpClient(
            withInterceptors([
                apiCredentialsInterceptor,
                adminAuthInterceptor,
                authInterceptor
            ])
        ),
        provideAnimations(),
        provideCharts(withDefaultRegisterables()),
        importProvidersFrom(FroalaEditorModule, FroalaViewModule)
    ]
};