import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { AuthService } from './services/auth.service';

/** Logged-in users (approved members + super admin) can use the studio. */
export const authGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  if (!auth.isLoggedIn) return router.createUrlTree(['/login']);
  return true;
};

/** Super admin dashboard only. */
export const adminGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  if (!auth.isLoggedIn) return router.createUrlTree(['/login']);
  if (!auth.isAdmin) return router.createUrlTree(['/sessions']);
  return true;
};

export const guestGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  if (!auth.isLoggedIn) return true;
  return router.createUrlTree([auth.homeUrl()]);
};
