import { ChangeDetectionStrategy, Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import { I18nService } from 'src/app/shared/i18n.service';

@Component({
  standalone: true,
  selector: 'app-chatbot-footer',
  imports: [CommonModule, RouterModule],
  templateUrl: './footer.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class FooterComponent {
  constructor(public i18n: I18nService) { }
}
