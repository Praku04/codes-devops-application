import { ChangeDetectorRef, Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { AuthService } from '../services/auth.service';

@Component({
  selector: 'ac-login',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './login.component.html',
  styleUrl: './login.component.css',
})
export class LoginComponent implements OnInit {
  private cdr = inject(ChangeDetectorRef);

  email = '';
  password = '';
  /** Reveal the password in plain text (the eye toggle). Never persisted. */
  showPassword = false;
  error = '';
  /** Why the last session ended (e.g. the admin deleted the account). */
  notice = '';
  busy = false;

  constructor(
    private auth: AuthService,
    private router: Router,
  ) {}

  ngOnInit(): void {
    // Set when a request failed because the account is gone — shown once.
    this.notice = this.auth.takeLogoutNotice();
  }

  submit(): void {
    this.error = '';
    this.notice = '';
    this.busy = true;
    this.cdr.detectChanges();
    this.auth.login(this.email.trim(), this.password).subscribe({
      next: () => {
        this.busy = false;
        void this.router.navigateByUrl(this.auth.homeUrl());
      },
      error: (err) => {
        this.busy = false;
        this.error = err?.error?.message || 'Login failed';
        this.cdr.detectChanges();
      },
    });
  }
}
