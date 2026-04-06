import { Routes } from '@angular/router';
import { chatbotAuthGuard } from './services/services/chatbot/chatbot-auth.guard';

export const routes: Routes = [
  // ROOT: varsayılan olarak chatbot login
  {
    path: '',
    pathMatch: 'full',
    redirectTo: 'chatbot/sign-in'
  },

  // 🔓 Chatbot login
  {
    path: 'chatbot/sign-in',
    loadComponent: () =>
      import('./chatbot/sign-in/sign-in.component').then((m) => m.SignInComponent),
    data: { title: 'chatbot.signIn' }
  },

  // 🔐 Chatbot shell
  {
    path: 'chatbot',
    loadComponent: () =>
      import('./chatbot/layout/shell/shell.component').then((m) => m.ShellComponent),
    canActivate: [chatbotAuthGuard],
    canActivateChild: [chatbotAuthGuard],
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'dashboard' },

      {
        path: 'dashboard',
        loadComponent: () =>
          import('./chatbot/dashboard/dashboard.component').then((m) => m.DashboardComponent),
        data: { title: 'nav.chatbot.dashboard' }
      },

      {
        path: 'document-upload',
        loadComponent: () =>
          import('./chatbot/document-upload/document-upload.component').then(
            (m) => m.DocumentUploadComponent
          ),
        data: { title: 'nav.chatbot.document-upload' }
      }
    ]
  },

  // Bilinmeyen tüm route'lar chatbot login'e yönlensin
  {
    path: '**',
    redirectTo: 'chatbot/sign-in'
  }
];