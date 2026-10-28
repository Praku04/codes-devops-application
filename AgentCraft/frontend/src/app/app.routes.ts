import { Routes } from '@angular/router';
import { adminGuard, authGuard, guestGuard } from './auth.guard';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'sessions' },
  {
    path: 'login',
    canActivate: [guestGuard],
    loadComponent: () => import('./login/login.component').then((m) => m.LoginComponent),
  },
  {
    path: 'signup',
    canActivate: [guestGuard],
    loadComponent: () => import('./signup/signup.component').then((m) => m.SignupComponent),
  },
  {
    path: 'admin',
    canActivate: [adminGuard],
    loadComponent: () => import('./admin/admin.component').then((m) => m.AdminComponent),
  },
  {
    path: 'sessions',
    canActivate: [authGuard],
    loadComponent: () => import('./sessions/sessions.component').then((m) => m.SessionsComponent),
  },
  {
    path: 'wizard',
    canActivate: [authGuard],
    loadComponent: () => import('./wizard/wizard.component').then((m) => m.WizardComponent),
  },
  {
    path: 'wizard/:id',
    canActivate: [authGuard],
    loadComponent: () => import('./wizard/wizard.component').then((m) => m.WizardComponent),
  },
  { path: '**', redirectTo: 'sessions' },
];
