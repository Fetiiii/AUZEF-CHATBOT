import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnInit,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AgGridAngular } from 'ag-grid-angular';
import {
  AllCommunityModule,
  ModuleRegistry,
  ColDef,
  GridApi,
  GridReadyEvent,
  CellValueChangedEvent,
  themeQuartz,
} from 'ag-grid-community';

import {
  AcademicCalendarApiService,
  AcademicCalendarItem,
} from '../../services/services/chatbot/academic-calendar-api.service';

ModuleRegistry.registerModules([AllCommunityModule]);

@Component({
  selector: 'app-academic-calendar',
  standalone: true,
  imports: [CommonModule, FormsModule, AgGridAngular],
  templateUrl: './academic-calendar.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AcademicCalendarComponent implements OnInit {
  // ── AG Grid ──────────────────────────────────────
  theme = themeQuartz;
  private gridApi!: GridApi<AcademicCalendarItem>;
  rowData: AcademicCalendarItem[] = [];
  pendingChanges = new Map<number, Partial<AcademicCalendarItem>>();

  colDefs: ColDef<AcademicCalendarItem>[] = [
    {
      field: 'id',
      headerName: 'ID',
      width: 80,
      sortable: true,
      filter: true,
      editable: false,
      pinned: 'left',
    },
    {
      field: 'period',
      headerName: 'Dönem',
      flex: 1,
      editable: true,
      sortable: true,
      filter: true,
      wrapText: true,
      autoHeight: true,
      cellStyle: { 'white-space': 'normal', 'line-height': '1.5' },
    },
    {
      field: 'event',
      headerName: 'Etkinlik',
      flex: 2,
      editable: true,
      sortable: true,
      filter: true,
      wrapText: true,
      autoHeight: true,
      cellStyle: { 'white-space': 'normal', 'line-height': '1.5' },
    },
    {
      field: 'start_date',
      headerName: 'Başlangıç Tarihi',
      width: 155,
      editable: true,
      sortable: true,
      filter: true,
    },
    {
      field: 'end_date',
      headerName: 'Bitiş Tarihi',
      width: 155,
      editable: true,
      sortable: true,
      filter: true,
    },
    {
      field: 'updated_at',
      headerName: 'Güncelleme',
      width: 155,
      editable: false,
      sortable: true,
      valueFormatter: (p) => {
        if (!p.value) return '—';
        return new Date(p.value).toLocaleString('tr-TR');
      },
    },
    {
      headerName: '',
      width: 56,
      editable: false,
      pinned: 'right',
      cellRenderer: (params: any) => {
        return `<button
          onclick="window.dispatchEvent(new CustomEvent('ag-delete-calendar-row', {detail: ${params.data.id}}))"
          class="h-8 w-8 grid place-items-center rounded-lg text-red-400 hover:text-red-600 hover:bg-red-50 transition"
          title="Sil">
          <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>
        </button>`;
      },
    },
  ];

  gridOptions = {
    editType: 'fullRow' as const,
    stopEditingWhenGridLosesFocus: true,
  };

  // ── State ─────────────────────────────────────────
  loading = signal(true);
  saving = signal(false);
  addingRow = signal(false);
  hasPendingChanges = signal(false);
  toastMessage = signal<string | null>(null);
  toastType = signal<'success' | 'error'>('success');
  importing = signal(false);
  selectedFile = signal<File | null>(null);

  // Yeni satır formu
  newPeriod = '';
  newEvent = '';
  newStartDate = '';
  newEndDate = '';

  constructor(
    private calendarApi: AcademicCalendarApiService,
    private cdr: ChangeDetectorRef
  ) {}

  ngOnInit(): void {
    this.loadAll();

    window.addEventListener('ag-delete-calendar-row', (e: any) => {
      this.deleteRow(e.detail);
    });
  }

  private loadAll(): void {
    this.loading.set(true);
    this.calendarApi.getAll().subscribe({
      next: (data) => {
        this.rowData = data;
        this.loading.set(false);
        this.cdr.markForCheck();
      },
      error: () => {
        this.showToast('Veriler yüklenemedi.', 'error');
        this.loading.set(false);
        this.cdr.markForCheck();
      },
    });
  }

  onGridReady(event: GridReadyEvent<AcademicCalendarItem>): void {
    this.gridApi = event.api;
  }

  onCellValueChanged(event: CellValueChangedEvent<AcademicCalendarItem>): void {
    const id = event.data.id;
    const field = event.colDef.field as keyof AcademicCalendarItem;
    const existing = this.pendingChanges.get(id) ?? {};
    this.pendingChanges.set(id, { ...existing, [field]: event.newValue });
    this.hasPendingChanges.set(this.pendingChanges.size > 0);
    this.cdr.markForCheck();
  }

  saveAllChanges(): void {
    if (this.pendingChanges.size === 0) return;
    this.saving.set(true);

    const items = Array.from(this.pendingChanges.entries()).map(([id, changes]) => ({
      id,
      ...changes,
    }));

    this.calendarApi.bulkUpdate(items).subscribe({
      next: (res) => {
        this.pendingChanges.clear();
        this.hasPendingChanges.set(false);
        this.saving.set(false);
        this.showToast(`${res.count} kayıt güncellendi.`, 'success');
        this.refreshData();
      },
      error: () => {
        this.saving.set(false);
        this.showToast('Kaydedilirken hata oluştu.', 'error');
      },
    });
  }

  discardChanges(): void {
    this.pendingChanges.clear();
    this.hasPendingChanges.set(false);
    this.refreshData();
  }

  deleteRow(id: number): void {
    if (!confirm(`ID ${id} olan kaydı silmek istediğinize emin misiniz?`)) return;
    this.calendarApi.delete(id).subscribe({
      next: () => {
        this.rowData = this.rowData.filter((r) => r.id !== id);
        this.pendingChanges.delete(id);
        this.hasPendingChanges.set(this.pendingChanges.size > 0);
        this.showToast('Kayıt silindi.', 'success');
        this.cdr.markForCheck();
      },
      error: () => this.showToast('Silme işlemi başarısız.', 'error'),
    });
  }

  submitNewRow(): void {
    if (!this.newEvent.trim() || !this.newStartDate.trim()) return;
    this.addingRow.set(true);

    this.calendarApi.create({
      period: this.newPeriod.trim(),
      event: this.newEvent.trim(),
      start_date: this.newStartDate.trim(),
      end_date: this.newEndDate.trim() || this.newStartDate.trim(),
    }).subscribe({
      next: (item) => {
        this.rowData = [...this.rowData, item];
        this.newPeriod = '';
        this.newEvent = '';
        this.newStartDate = '';
        this.newEndDate = '';
        this.addingRow.set(false);
        this.showToast('Yeni kayıt eklendi.', 'success');
        this.cdr.markForCheck();
      },
      error: () => {
        this.addingRow.set(false);
        this.showToast('Kayıt eklenemedi.', 'error');
      },
    });
  }

  private refreshData(): void {
    this.calendarApi.getAll().subscribe({
      next: (data) => {
        this.rowData = data;
        this.cdr.markForCheck();
      },
    });
  }

  private showToast(message: string, type: 'success' | 'error'): void {
    this.toastMessage.set(message);
    this.toastType.set(type);
    this.cdr.markForCheck();
    setTimeout(() => {
      this.toastMessage.set(null);
      this.cdr.markForCheck();
    }, 3500);
  }

  quickFilter(value: string): void {
    this.gridApi?.setGridOption('quickFilterText', value);
  }

  onFileSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.selectedFile.set(input.files?.[0] ?? null);
    this.cdr.markForCheck();
  }

  uploadCsv(): void {
    const file = this.selectedFile();
    if (!file) return;
    this.importing.set(true);

    this.calendarApi.importCsv(file).subscribe({
      next: (res) => {
        this.importing.set(false);
        this.selectedFile.set(null);
        this.showToast(`${res.imported} kayıt içe aktarıldı.`, 'success');
        this.refreshData();
        this.cdr.markForCheck();
      },
      error: () => {
        this.importing.set(false);
        this.showToast('İçe aktarma başarısız oldu.', 'error');
        this.cdr.markForCheck();
      },
    });
  }
}
