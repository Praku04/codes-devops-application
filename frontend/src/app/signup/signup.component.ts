import { ChangeDetectorRef, Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { AuthService } from '../services/auth.service';

@Component({
  selector: 'ac-signup',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './signup.component.html',
  styleUrl: './signup.component.css',
})
export class SignupComponent {
  private cdr = inject(ChangeDetectorRef);

  name = '';
  email = '';
  password = '';
  confirmPassword = '';
  /** Reveal each password in plain text (the eye toggles). Never persisted. */
  showPassword = false;
  showConfirmPassword = false;
  error = '';
  success = '';
  busy = false;

  constructor(
    private auth: AuthService,
    private router: Router,
  ) {}

  submit(): void {
    this.error = '';
    this.success = '';
    if (this.password.length < 6) {
      this.error = 'Password must be at least 6 characters';
      return;
    }
    if (this.password !== this.confirmPassword) {
      this.error = 'Password and confirm password do not match';
      return;
    }
    this.busy = true;
    this.cdr.detectChanges();
    this.auth.signup(this.email.trim(), this.password, this.name.trim() || 'AgentCraft User').subscribe({
      next: (res) => {
        this.busy = false;
        this.success =
          res.message ||
          'Account submitted for admin approval. You can log in after the super admin approves you.';
        this.password = '';
        this.confirmPassword = '';
        // Don't leave the form in reveal mode if it comes back into view.
        this.showPassword = false;
        this.showConfirmPassword = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.busy = false;
        this.error = err?.error?.message || 'Signup failed';
        this.cdr.detectChanges();
      },
    });
  }
}
