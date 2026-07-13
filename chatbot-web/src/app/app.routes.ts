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

  // Chatbot artık widget olarak sunuluyor
  { path: 'chat', redirectTo: 'chatbot/sign-in', pathMatch: 'full' },

  // 🔐 Chatbot admin shell (login gerekir)
  {
    path: 'chatbot',
    loadComponent: () =>
      import('./chatbot/layout/shell/shell.component').then((m) => m.ShellComponent),
    canActivate: [chatbotAuthGuard],
    canActivateChild: [chatbotAuthGuard],
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'document-upload' },

      {
        path: 'document-upload',
        loadComponent: () =>
          import('./chatbot/document-upload/document-upload.component').then(
            (m) => m.DocumentUploadComponent
          ),
        data: { title: 'nav.chatbot.document-upload' }
      },

      {
        path: 'academic-calendar',
        loadComponent: () =>
          import('./chatbot/academic-calendar/academic-calendar.component').then(
            (m) => m.AcademicCalendarComponent
          ),
        data: { title: 'nav.chatbot.academic-calendar' }
      },

      {
        path: 'view-data',
        loadComponent: () =>
          import('./chatbot/ViewData/view-data.component').then(
            (m) => m.ViewDataComponent
          ),
        data: { title: 'nav.chatbot.view-data', minRole: 'admin' }
      },

      {
        path: 'conversations',
        loadComponent: () =>
          import('./chatbot/conversations/conversations.component').then(
            (m) => m.ConversationsComponent
          ),
        data: { title: 'nav.chatbot.conversations', minRole: 'admin' }
      },

      {
        path: 'settings',
        loadComponent: () =>
          import('./chatbot/settings/settings.component').then(
            (m) => m.SettingsComponent
          ),
        data: { title: 'nav.chatbot.settings', minRole: 'super_admin' }
      }
    ]
  },
  
  // Bilinmeyen tüm route'lar sign-in'e yönlensin
  {
    path: '**',
    redirectTo: 'chatbot/sign-in'
  }
];