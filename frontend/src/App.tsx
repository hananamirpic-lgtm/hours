/**
 * The two route trees over the shared shells (design, "Front end"):
 *
 *   /m/*  — the employee mobile app: home with the one large action, scanner, history, alerts.
 *   /*    — the management console: dashboard, employees, clients, sites, hours, approvals, payroll,
 *           billing, reports, users, audit, settings.
 *
 * Each tree sits behind `RequireAuth`, which also keeps a role on its own tree. The screens are mostly
 * placeholders that later milestones fill in; what is real here is the shell, the role-driven
 * navigation, the auth flow and the localisation, which is what this task delivers.
 */

import { Navigate, Route, Routes } from 'react-router-dom';

import { ConsoleShell } from '@/app/ConsoleShell';
import { DashboardPage } from '@/app/DashboardPage';
import { ApprovalsPage } from '@/app/approvals/ApprovalsPage';
import { BillingPage } from '@/app/billing/BillingPage';
import { ClientsPage } from '@/app/clients/ClientsPage';
import { EmployeesPage } from '@/app/employees/EmployeesPage';
import { HoursPage } from '@/app/hours/HoursPage';
import { AuditPage } from '@/app/audit/AuditPage';
import { SettingsPage } from '@/app/settings/SettingsPage';
import { PayrollPage } from '@/app/payroll/PayrollPage';
import { PeriodsPage } from '@/app/periods/PeriodsPage';
import { ReportsPage } from '@/app/reports/ReportsPage';
import { SitesPage } from '@/app/sites/SitesPage';
import { StaffingCompaniesPage } from '@/app/staffingCompanies/StaffingCompaniesPage';
import { UsersPage } from '@/app/users/UsersPage';
import { TwoFactorGate } from '@/app/users/TwoFactorGate';
import { ChangePasswordGate } from '@/app/auth/ChangePasswordGate';
import { MobileHomePage } from '@/app/MobileHomePage';
import { MobileMyHoursPage } from '@/app/MobileMyHoursPage';
import { MobileProfilePage } from '@/app/MobileProfilePage';
import { MobileScanLandingPage } from '@/app/MobileScanLandingPage';
import { MobileShell } from '@/app/MobileShell';
import { NotFoundPage } from '@/app/NotFoundPage';
import { PlaceholderPage } from '@/app/PlaceholderPage';
import { RedirectIfAuthenticated, RequireAuth } from '@/app/guards';
import { LoginScreen } from '@/auth/LoginScreen';

export default function App() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <RedirectIfAuthenticated>
            <LoginScreen />
          </RedirectIfAuthenticated>
        }
      />

      {/* Employee mobile tree */}
      <Route
        path="/m"
        element={
          <RequireAuth tree="mobile">
            <ChangePasswordGate>
              <MobileShell />
            </ChangePasswordGate>
          </RequireAuth>
        }
      >
        <Route index element={<MobileHomePage />} />
        <Route path="scan" element={<MobileScanLandingPage />} />
        <Route path="history" element={<MobileMyHoursPage />} />
        <Route path="profile" element={<MobileProfilePage />} />
        <Route path="alerts" element={<PlaceholderPage titleKey="mobile.alerts" />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>

      {/* Management console tree */}
      <Route
        path="/"
        element={
          <RequireAuth tree="console">
            <ChangePasswordGate>
              <TwoFactorGate>
                <ConsoleShell />
              </TwoFactorGate>
            </ChangePasswordGate>
          </RequireAuth>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="employees" element={<EmployeesPage />} />
        <Route path="staffing-companies" element={<StaffingCompaniesPage />} />
        <Route path="clients" element={<ClientsPage />} />
        <Route path="sites" element={<SitesPage />} />
        <Route path="hours" element={<HoursPage />} />
        <Route path="approvals" element={<ApprovalsPage />} />
        <Route path="periods" element={<PeriodsPage />} />
        <Route path="payroll" element={<PayrollPage />} />
        <Route path="billing" element={<BillingPage />} />
        <Route path="reports" element={<ReportsPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="audit" element={<AuditPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
