import { Injectable, signal } from '@angular/core';

type Lang = 'tr' | 'en';
type Dict = Record<string, string>;

const TR: Dict = {
  'app.title': 'AUZEF Çözüm Merkezi',


  // Sidebar
  'sidebar.brand': 'ÇÖZÜM MERKEZİ',
  'sidebar.activeUsers': 'AKTİF KULLANICILAR',

  // Sidebar Login Label
  'sidebar.login.student': 'ÖĞRENCİ',
  'sidebar.login.personnel': 'PERSONEL',
  'sidebar.login.admin': 'ADMİN',
  'sidebar.login.call-us': 'ÇAĞRI MERKEZİ',
  'sidebar.login.chatbot': 'CHATBOT',



  // NAV
  'nav.dashboard': 'Anasayfa',
  'nav.all-announcements': 'Duyurular',
  'nav.announcements-detail': 'Duyurular',
  'nav.requests.mine': 'Benim Taleplerim',
  'nav.requests-detail': 'Benim Talep Detay',

  'nav.requests.all': 'Tüm Talepler',
  'nav.requests.pending': 'bekleyen talep',
  'nav.users': 'Kullanıcılar',

  'nav.management': 'Yönetim',
  'nav.request-categories': 'Talep Kategorileri',
  'nav.request-management': 'Talep Yönetimi',
  'nav.user-groups': 'Kullanıcı Grupları',
  'nav.authority-and-role-management': 'Yetki & Rol Yönetimi',
  'nav.management.announcements-management': 'Duyuru Yönetimi',
  'nav.personnel-status-management': 'Personel Durum Yönetimi',
  'nav.frequently-asked-questions-management': 'Sıkça Sorulan Sorular Yönetimi',


  'nav.requests': 'Talepler',
  'nav.reports': 'Raporlar',
  'nav.profile': 'Profilim',
  'nav.prepared-messages': 'Hazır Cevaplar',
  'nav.notifications': 'Bildirimler',
  'nav.settings': 'Ayarlar',
  'nav.requests.team': 'Takım Talepleri',

  'nav.requests.closed': 'Kapanan Talepler',
  'nav.requests.watching': 'İzlenen Talepler',
  'nav.reports.kpi': 'KPI Skor Kartları',
  'nav.reports.distribution': 'Talep Dağılım Raporları',
  'nav.reports.satisfaction': 'Kullanıcı Memnuniyet Raporları',
  'nav.reports.performance': 'Performans ve Gecikme Raporları',
  'nav.reports.unit': 'Birim Raporları',
  'nav.perssonel-list': 'Personel Listesi',
  'nav.add-new-user': 'Yeni Kullanıcı Ekle',
  'nav.chatbot-document-upload': 'Chatbot Dokümanı Yükle',
  'nav.login-history': 'Oturum Açma Geçmişi',
  'nav.test-page': 'Test Sayfası',
  'nav.chatbot.dashboard': 'Panel',
  'nav.chatbot.document-upload': 'Doküman Yükle',
  'nav.chatbot.view-data': 'İzleme Paneli',


  // GENERAL SIGN- IN CARD
  'university': 'İSTANBUL ÜNİVERSİTESİ',
  'faculty': 'AÇIK VE UZAKTAN EĞİTİM FAKÜLTESİ',
  'system': 'ÇÖZÜM MERKEZİ',
  'system-student': 'ÖĞRENCİ',
  'system-call-us': 'ÇAĞRI MERKEZİ',
  'system-personnel': 'PERSONEL',
  'system-admin': 'ADMIN',
  'system-chatbot': 'CHATBOT',
  'system-sign-in-student-text': 'Öğrenci numaranız ve parolanız ile giriş yapın.',
  'system-sign-in-personnel-text': 'Kulllanıcı adınız ve parolanız ile giriş yapın.',
  'system-sign-in-username-text': 'Kullanıcı Adı',
  'system-sign-in-username-box-text': 'Kullanıcı adınız',
  'system-sign-in-Password-text': 'Parola',
  'this-field-is-mandatory': 'Bu alan zorunludur.',
  'technicalsupport': 'Teknik Destek',
  'remember-me': 'Beni hatırla',
  'I-forgot-my-password': 'Parolamı unuttum',

  // STUDENT SIGN IN
  'student.signIn': 'ÖĞRENCİ GİRİŞ',
  'student.signIn-dev': 'ÖĞRENCİ GİRİŞ',

  // CALL US SIGN IN
  'call-us.signIn': 'ÇAĞRI MERKEZİ GİRİŞ',
  'call-us.signIn-dev': 'ÇAĞRI MERKEZİ GİRİŞ',

  // PERSONNEL SIGN IN
  'personnel.signIn': 'PERSONEL GİRİŞ',
  'personnel.signIn-dev': 'PERSONEL GİRİŞ',

  // ADMIN SIGN IN
  'auth.signIn': 'ADMİN GİRİŞ',
  'auth.signUp': 'KAYIT',
  'action.new': 'YENİ',

  // CHATBOT SIGN IN
  'chatbot.signIn': 'CHATBOT GİRİŞ',
  'chatbot.signUp': 'KAYIT',
  'chatbot.new': 'YENİ',

  // HEADER
  'header.logout': 'Çıkış',
  'header.logoutAll': 'Tüm cihazlardan çıkış yap',     // ✅ eklendi
  'header.loggingOut': 'Çıkış yapılıyor…',            // ✅ eklendi
  'header.sessionActions': 'Oturum İşlemleri',        // ✅ eklendi (opsiyonel ama kullanışlı)

  // THEME
  'theme.light': 'Aydınlık',
  'theme.dark': 'Karanlık',
  'theme.system': 'Sistem',

  // LANG
  'lang.tr': 'Türkçe',
  'lang.en': 'English',

  // user menu
  'user.available': 'Uygun',
  'user.away': 'Dışarıda',
  'user.changePassword': 'Şifre Değiştir',

  // Users sub menus
  'nav.users.roles': 'Yetki & Rol Yönetimi',
  'nav.users.user-groups': 'Kullanıcı Grupları',

  // Management sub menus
  'nav.management.request-categories': 'Talep Kategorileri',
  'nav.management.request-management': 'Talep Yönetimi',
  'nav.management.user-groups': 'Kullanıcı Grupları',
  'nav.management.authority-and-role-management': 'Yetki & Rol Yönetimi',
  'nav.announcements-management': 'Duyuru Yönetimi',

  // FAQ
  'nav.frequently-asked-questions': 'Sıkça Sorulan Sorular',

  // ==== CALL-US (ÖĞRENCİ) ==== //
  'call-us.request-create': 'Talep Oluştur',
  'call-us.request-view': 'Talep Görüntüle',
  'call-us.request-detail': 'Talep Detayı',
  'call-us.user-management': 'Kullanıcı Yönetimi',
  'call-us.user-management.edit': 'Kullanıcı Ekle/Güncelle',
  'call-us.frequently-asked-questions': 'Sıkça Sorulan Sorular',

  // ==== STUDENT (AYRI SHELL) ==== //
  'student.request-create': 'Talep Oluştur',
  'student.request-view': 'Talep Görüntüle',
  'student.request-detail': 'Talep Detayı',
  'student.frequently-asked-questions': 'Sıkça Sorulan Sorular',

  // ==== PERMISSIONS ==== //
  'permissions.every': 'Hepsi',

  // ---- Tickets (Self) ----
  'permissions.tickets.self.view': 'Kendi taleplerini görüntüleme',
  'permissions.tickets.self.answer': 'Kendi taleplerini yanıtlama',
  'permissions.tickets.self.merge': 'Kendi taleplerini birleştirme',
  'permissions.tickets.self.close': 'Kendi taleplerini kapatma',
  'permissions.tickets.self.edit': 'Kendi taleplerini düzenleme',
  'permissions.tickets.self.remove': 'Kendi taleplerini silme',
  'permissions.tickets.self.reassign.category': 'Kendi taleplerinde kategori değiştirme',
  'permissions.tickets.self.reassign.personnel': 'Kendi taleplerinde personel değiştirme',

  // ---- Tickets (Group) ----
  'permissions.tickets.group.view': 'Grup taleplerini görüntüleme',
  'permissions.tickets.group.answer': 'Grup taleplerini yanıtlama',
  'permissions.tickets.group.merge': 'Grup taleplerini birleştirme',
  'permissions.tickets.group.close': 'Grup taleplerini kapatma',
  'permissions.tickets.group.reassign.category': 'Grup taleplerinde kategori değiştirme',
  'permissions.tickets.group.reassign.personnel': 'Grup taleplerinde personel değiştirme',

  // ---- Tickets (All) ----
  'permissions.tickets.all.view': 'Tüm talepleri görüntüleme',
  'permissions.tickets.all.answer': 'Tüm talepleri yanıtlama',
  'permissions.tickets.all.merge': 'Tüm talepleri birleştirme',
  'permissions.tickets.all.close': 'Tüm talepleri kapatma',
  'permissions.tickets.all.edit': 'Tüm talepleri düzenleme',
  'permissions.tickets.all.remove': 'Tüm talepleri silme',
  'permissions.tickets.all.reassign.category': 'Tüm taleplerde kategori değiştirme',
  'permissions.tickets.all.reassign.personnel': 'Tüm taleplerde personel değiştirme',

  // ---- Saved Answers (Self) ----
  'permissions.savedanswers.self.view': 'Kendi hazır cevaplarını görüntüleme',
  'permissions.savedanswers.self.create': 'Kendi hazır cevabını oluşturma',

  // ---- Saved Answers (All) ----
  'permissions.savedanswers.all.view': 'Tüm hazır cevapları görüntüleme',
  'permissions.savedanswers.all.create': 'Hazır cevap oluşturma',

  // ---- Personnel Management ----
  'permissions.management.personnel.view': 'Personel görüntüleme',
  'permissions.management.personnel.create': 'Personel oluşturma',
  'permissions.management.personnel.edit': 'Personel düzenleme',
  'permissions.management.personnel.remove': 'Personel silme',

  // ---- Settings ----
  'permissions.management.setting.view': 'Ayarları görüntüleme',
  'permissions.management.setting.edit': 'Ayarları düzenleme',

  // ---- Banned Students ----
  'permissions.management.bannedstudent.view': 'Engelli öğrencileri görüntüleme',
  'permissions.management.bannedstudent.create': 'Öğrenci engelleme',
  'permissions.management.bannedstudent.remove': 'Öğrenci engelini kaldırma',

  // ---- Banned Words ----
  'permissions.management.bannedword.view': 'Yasaklı kelimeleri görüntüleme',
  'permissions.management.bannedword.create': 'Yasaklı kelime ekleme',
  'permissions.management.bannedword.remove': 'Yasaklı kelime silme',

  // ---- Categories ----
  'permissions.management.category.view': 'Kategori görüntüleme',
  'permissions.management.category.create': 'Kategori oluşturma',
  'permissions.management.category.edit': 'Kategori düzenleme',
  'permissions.management.category.remove': 'Kategori silme',
  'permissions.management.category.assignpersonnel': 'Kategoriye personel atama',

  // ---- Departments ----
  'permissions.management.department.view': 'Departman görüntüleme',
  'permissions.management.department.create': 'Departman oluşturma',
  'permissions.management.department.edit': 'Departman düzenleme',
  'permissions.management.department.remove': 'Departman silme',
  'permissions.management.department.assignpersonnel': 'Departmana personel atama',

  // ---- Roles ----
  'permissions.management.role.view': 'Rol görüntüleme',
  'permissions.management.role.create': 'Rol oluşturma',
  'permissions.management.role.edit': 'Rol düzenleme',
  'permissions.management.role.remove': 'Rol silme',

  // ---- Announcements ----
  'permissions.management.announcement.view': 'Duyuruları görüntüleme',
  'permissions.management.announcement.create': 'Duyuru oluşturma',
  'permissions.management.announcement.edit': 'Duyuru düzenleme',
  'permissions.management.announcement.remove': 'Duyuru silme',
};

const EN: Dict = {
  'app.title': 'AUZEF Support Center',

  // Sidebar
  'sidebar.brand': 'SUPPORT CENTER',
  'sidebar.activeUsers': 'ACTIVE USERS',

  // Sidebar Login Label
  'sidebar.login.student': 'STUDENT',
  'sidebar.login.personnel': 'PERSONNEL',
  'sidebar.login.admin': 'ADMIN',
  'sidebar.login.call-us': 'CALL CENTER',
  'sidebar.login.chatbot': 'CHATBOT',

  // NAV
  'nav.dashboard': 'Home',
  'nav.all-announcements': 'Announcements',
  'nav.announcements-detail': 'Announcements',
  'nav.requests.mine': 'My Requests',
  'nav.requests-detail': 'My Requests Detail',
  'nav.requests.all': 'All Requests',
  'nav.requests.pending': 'pending requests',
  'nav.users': 'Users',

  'nav.management': 'Management',
  'nav.request-categories': 'Request Categories',
  'nav.request-management': 'Request Management',
  'nav.user-groups': 'User Groups',
  'nav.authority-and-role-management': 'Authority & Role Management',

  'nav.management.user-groups': 'User Groups',
  'nav.management.authority-and-role-management': 'Authority & Role Management',
  'nav.announcements-management': 'Announcements Management',
  'nav.personnel-status-management': 'Personnel Status Management',
  'nav.frequently-asked-questions-management': 'Frequently Asked Questions Management',


  'nav.requests': 'Requests',
  'nav.reports': 'Reports',
  'nav.profile': 'My Profile',
  'nav.prepared-messages': 'Prepared Messages',
  'nav.notifications': 'Notifications',
  'nav.settings': 'Settings',
  'nav.requests.team': 'Team Requests',

  'nav.requests.closed': 'Closed Requests',
  'nav.requests.watching': 'Watching',
  'nav.reports.kpi': 'KPI Scorecards',
  'nav.reports.distribution': 'Request Distribution Reports',
  'nav.reports.satisfaction': 'Customer Satisfaction Reports',
  'nav.reports.performance': 'Performance & Delay Reports',
  'nav.reports.unit': 'Department Reports',
  'nav.perssonel-list': 'Perssonel List',
  'nav.add-new-user': 'Add New User',
  'nav.chatbot-document-upload': 'Chatbot Document Upload',
  'nav.login-history': 'Login History',
  'nav.test-page': 'Test Page',
  'nav.chatbot.dashboard': 'Dashboard',
  'nav.chatbot.document-upload': 'Upload Document',
  'nav.chatbot.view-data': 'Monitoring',




  // GENERAL SIGN- IN CARD
  'university': 'ISTANBUL UNIVERSITY',
  'faculty': 'FACULTY OF OPEN AND DISTANCE EDUCATION',
  'system': 'SOLUTION CENTER',
  'system-student': 'STUDENT',
  'system-call-us': 'CALL CENTER',
  'system-personnel': 'PERSONNEL',
  'system-admin': 'ADMIN',
  'system-chatbot': 'CHATBOT',
  'system-sign-in-student-text': 'Log in with your student number and password.',
  'system-sign-in-personnel-text': 'Log in with your username and password.',
  'system-sign-in-username-text': 'Username',
  'system-sign-in-username-box-text': 'Your username',
  'system-sign-in-Password-text': 'Password',
  'this-field-is-mandatory': 'This field is mandatory.',
  'technicalsupport': 'Technical Support',
  'remember-me': 'Remember me',
  'I-forgot-my-password': 'I forgot my password',




  // STUDENT SIGN IN
  'student.signIn': 'STUDENT SIGN IN',
  'student.signIn-dev': 'STUDENT SIGN IN',

  // CALL US SIGN IN
  'call-us.signIn': 'CALL US SIGN IN',
  'call-us.signIn-dev': 'CALL US SIGN IN',

  // PERSONNEL SIGN IN
  'personnel.signIn': 'PERSONNEL SIGN IN',
  'personnel.signIn-dev': 'PERSONNEL SIGN IN',

  // ADMIN SIGN IN
  'auth.signIn': 'ADMIN SIGN IN',
  'auth.signUp': 'SIGN UP',
  'action.new': 'NEW',

  // CHATBOT SIGN IN
  'chatbot.signIn': 'CHATBOT SIGN IN',
  'chatbot.signUp': 'SIGN UP',
  'chatbot.new': 'NEW',







  // HEADER
  'header.logout': 'Logout',
  'header.logoutAll': 'Sign out of all devices',  // ✅ eklendi
  'header.loggingOut': 'Signing out…',            // ✅ eklendi
  'header.sessionActions': 'Session Actions',     // ✅ eklendi (opsiyonel)

  // THEME
  'theme.light': 'Light',
  'theme.dark': 'Dark',
  'theme.system': 'System',

  // LANG
  'lang.tr': 'Turkish',
  'lang.en': 'English',

  // user menu
  'user.available': 'Available',
  'user.away': 'Away',
  'user.changePassword': 'Change Password',

  // Users sub menus
  'nav.users.roles': 'Authority & Role Management',
  'nav.users.user-groups': 'User Groups',

  // Management sub menus
  'nav.management.request-categories': 'Request Categories',
  'nav.management.request-management': 'Request Management',
  'nav.management.announcements-management': 'Announcements Management',

  // FAQ
  'nav.frequently-asked-questions': 'Frequently Asked Questions',

  // ==== CALL-US (STUDENT) ==== //
  'call-us.request-create': 'Create Request',
  'call-us.request-view': 'View Request',
  'call-us.request-detail': 'Request Detail',
  'call-us.user-management': 'User Management',
  'call-us.user-management.edit': 'Add/Update User',
  'call-us.frequently-asked-questions': 'Frequently Asked Questions',

  // ==== STUDENT (SEPARATE SHELL) ==== //
  'student.request-create': 'Create Request',
  'student.request-view': 'View Requests',
  'student.request-detail': 'Request Detail',
  'student.frequently-asked-questions': 'Frequently Asked Questions',

  // ==== PERMISSIONS ==== //
  'permissions.every': 'All',

  // ---- Tickets (Self) ----
  'permissions.tickets.self.view': 'View own tickets',
  'permissions.tickets.self.answer': 'Reply to own tickets',
  'permissions.tickets.self.merge': 'Merge own tickets',
  'permissions.tickets.self.close': 'Close own tickets',
  'permissions.tickets.self.edit': 'Edit own tickets',
  'permissions.tickets.self.remove': 'Delete own tickets',
  'permissions.tickets.self.reassign.category': 'Change category of own tickets',
  'permissions.tickets.self.reassign.personnel': 'Reassign personnel for own tickets',

  // ---- Tickets (Group) ----
  'permissions.tickets.group.view': 'View group tickets',
  'permissions.tickets.group.answer': 'Reply to group tickets',
  'permissions.tickets.group.merge': 'Merge group tickets',
  'permissions.tickets.group.close': 'Close group tickets',
  'permissions.tickets.group.reassign.category': 'Change category of group tickets',
  'permissions.tickets.group.reassign.personnel': 'Reassign personnel for group tickets',

  // ---- Tickets (All) ----
  'permissions.tickets.all.view': 'View all tickets',
  'permissions.tickets.all.answer': 'Reply to all tickets',
  'permissions.tickets.all.merge': 'Merge all tickets',
  'permissions.tickets.all.close': 'Close all tickets',
  'permissions.tickets.all.edit': 'Edit all tickets',
  'permissions.tickets.all.remove': 'Delete all tickets',
  'permissions.tickets.all.reassign.category': 'Change category of all tickets',
  'permissions.tickets.all.reassign.personnel': 'Reassign personnel for all tickets',

  // ---- Saved Answers (Self) ----
  'permissions.savedanswers.self.view': 'View own saved answers',
  'permissions.savedanswers.self.create': 'Create own saved answers',

  // ---- Saved Answers (All) ----
  'permissions.savedanswers.all.view': 'View all saved answers',
  'permissions.savedanswers.all.create': 'Create saved answers',

  // ---- Personnel Management ----
  'permissions.management.personnel.view': 'View personnel',
  'permissions.management.personnel.create': 'Create personnel',
  'permissions.management.personnel.edit': 'Edit personnel',
  'permissions.management.personnel.remove': 'Delete personnel',

  // ---- Settings ----
  'permissions.management.setting.view': 'View settings',
  'permissions.management.setting.edit': 'Edit settings',

  // ---- Banned Students ----
  'permissions.management.bannedstudent.view': 'View banned students',
  'permissions.management.bannedstudent.create': 'Ban student',
  'permissions.management.bannedstudent.remove': 'Unban student',

  // ---- Banned Words ----
  'permissions.management.bannedword.view': 'View banned words',
  'permissions.management.bannedword.create': 'Add banned word',
  'permissions.management.bannedword.remove': 'Remove banned word',

  // ---- Categories ----
  'permissions.management.category.view': 'View categories',
  'permissions.management.category.create': 'Create category',
  'permissions.management.category.edit': 'Edit category',
  'permissions.management.category.remove': 'Delete category',
  'permissions.management.category.assignpersonnel': 'Assign personnel to category',

  // ---- Departments ----
  'permissions.management.department.view': 'View departments',
  'permissions.management.department.create': 'Create department',
  'permissions.management.department.edit': 'Edit department',
  'permissions.management.department.remove': 'Delete department',
  'permissions.management.department.assignpersonnel': 'Assign personnel to department',

  // ---- Roles ----
  'permissions.management.role.view': 'View roles',
  'permissions.management.role.create': 'Create role',
  'permissions.management.role.edit': 'Edit role',
  'permissions.management.role.remove': 'Delete role',

  // ---- Announcements ----
  'permissions.management.announcement.view': 'View announcements',
  'permissions.management.announcement.create': 'Create announcement',
  'permissions.management.announcement.edit': 'Edit announcement',
  'permissions.management.announcement.remove': 'Delete announcement',
};

@Injectable({ providedIn: 'root' })
export class I18nService {
  lang = signal<Lang>((localStorage.getItem('lang') as Lang) || 'tr');

  t(key: string) {
    const d = this.lang() === 'tr' ? TR : EN;
    return d[key] ?? key;
  }

  setLang(l: Lang) {
    this.lang.set(l);
    localStorage.setItem('lang', l);
    document.documentElement.lang = l;
  }

  getCurrentLangAsLocaleId() {
    return this.lang() === 'tr' ? 'tr-TR' : 'en-US';
  }

  toggle() {
    this.setLang(this.lang() === 'tr' ? 'en' : 'tr');
  }
}
