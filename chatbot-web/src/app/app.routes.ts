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

  // 🔐 Chatbot admin shell (login gerekir)
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
        path: 'chat',
        loadComponent: () =>
          import('./chatbot/chat/chat.component').then((m) => m.ChatComponent),
        data: { title: 'nav.chatbot.chat' }
      },

      {
        path: 'document-upload',
        loadComponent: () =>
          import('./chatbot/document-upload/document-upload.component').then(
            (m) => m.DocumentUploadComponent
          ),
        data: { title: 'nav.chatbot.document-upload' }
      },

      {
        path: 'view-data',
        loadComponent: () =>
          import('./chatbot/ViewData/view-data.component').then(
            (m) => m.ViewDataComponent
          ),
        data: { title: 'nav.chatbot.view-data' }
      }
    ]
  },

  // Bilinmeyen tüm route'lar sign-in'e yönlensin
  {
    path: '**',
    redirectTo: 'chatbot/sign-in'
  }
];