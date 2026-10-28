import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Router } from '@angular/router';
import { BehaviorSubject, Observable, tap } from 'rxjs';
import { AuthResponse, SignupPendingResponse, User } from '../models';
import { API_BASE } from '../api-base';

/** Resolved from the browser's location + API_PORT — see api-base.ts. */
const API = API_BASE;
const TOKEN_KEY = 'agentcraft_token';
const USER_KEY = 'agentcraft_user';
/** One-shot reason for a forced logout, read by the login screen. */
const NOTICE_KEY = 'agentcraft_logout_notice';

@Injectable({ providedIn: 'root' })
export class AuthService {
  private http = inject(HttpClient);
  private router = inject(Router);
  private userSubject = new BehaviorSubject<User | null>(this.readUser());
  readonly user$ = this.userSubject.asObservable();

  get token(): string | null {
    return localStorage.getItem(TOKEN_KEY);
  }

  get user(): User | null {
    return this.userSubject.value;
  }

  get isLoggedIn(): boolean {
    return !!this.token && !!this.user;
  }

  get isAdmin(): boolean {
    return this.user?.role === 'admin';
  }

  /** Home route after login. */
  homeUrl(): string {
    return this.isAdmin ? '/admin' : '/sessions';
  }

  signup(email: string, password: string, name: string): Observable<SignupPendingResponse> {
    return this.http.post<SignupPendingResponse>(`${API}/api/v1/auth/signup`, {
      email,
      password,
      name,
    });
  }

  login(email: string, password: string): Observable<AuthResponse> {
    return this.http
      .post<AuthResponse>(`${API}/api/v1/auth/login`, { email, password })
      .pipe(tap((res) => this.persist(res)));
  }

  /**
   * Clear the session and go to /login.
   *
   * `reason` survives the navigation via sessionStorage so the login screen can
   * explain a forced logout — a deleted account otherwise just bounces back to the
   * form with no idea why.
   */
  logout(reason?: string): void {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    if (reason) {
      sessionStorage.setItem(NOTICE_KEY, reason);
    } else {
      sessionStorage.removeItem(NOTICE_KEY);
    }
    this.userSubject.next(null);
    this.router.navigateByUrl('/login');
  }

  /** Read-and-clear the forced-logout reason (shown once on the login screen). */
  takeLogoutNotice(): string {
    const notice = sessionStorage.getItem(NOTICE_KEY) || '';
    if (notice) sessionStorage.removeItem(NOTICE_KEY);
    return notice;
  }

  /**
   * Log out with the server's explanation when the account no longer exists.
   *
   * Returns true when it handled the error, so callers can skip their own banner.
   * A 403 whose message mentions deletion is the backend's tombstone response.
   */
  handleAuthError(err: unknown): boolean {
    const e = err as { status?: number; error?: { message?: string } } | null;
    const status = e?.status;
    const message = e?.error?.message || '';
    if (status === 403 && /delete/i.test(message)) {
      this.logout(message);
      return true;
    }
    if (status === 401) {
      this.logout();
      return true;
    }
    return false;
  }

  private persist(res: AuthResponse): void {
    localStorage.setItem(TOKEN_KEY, res.access_token);
    localStorage.setItem(USER_KEY, JSON.stringify(res.user));
    this.userSubject.next(res.user);
  }

  private readUser(): User | null {
    try {
      const raw = localStorage.getItem(USER_KEY);
      return raw ? (JSON.parse(raw) as User) : null;
    } catch {
      return null;
    }
  }
}
