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
import { FlagGate } from '@/service/gates/FlagGate';
import { TechnicianGate } from '@/service/gates/TechnicianGate';
import { ManagementLayout } from '@/service/management/ManagementLayout';
import { CategoriesPage } from '@/service/management/pages/CategoriesPage';
import { CustomersPage as TicketCustomersPage } from '@/service/management/pages/CustomersPage';
import { DashboardPage as TicketDashboardPage } from '@/service/management/pages/DashboardPage';
import { LabelsPage } from '@/service/management/pages/LabelsPage';
import { OptionsPage } from '@/service/management/pages/OptionsPage';
import { ReportsPage as TicketReportsPage } from '@/service/management/pages/ReportsPage';
import { TicketDetailPage } from '@/service/management/pages/TicketDetailPage';
import { TicketListPage } from '@/service/management/pages/TicketListPage';
import { RequireConfigStaff } from '@/service/management/RequireConfigStaff';
import { RequireTicketStaff } from '@/service/management/RequireTicketStaff';

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

      {/*
        Service-ticket management interface (design "Routes"). The flag gate is outermost, so with the flag
        off or unknown no sign-in or ticket screen renders at any /tickets path. Inside it, RequireTicketStaff
        sends a visitor with no staff session to /login and shows a not-authorized page to any other role or
        to a customer token. The forced password change and the 2FA enrolment come before any screen, as in
        the console. The three configuration screens add RequireConfigStaff (a technician sees "not available").
      */}
      <Route
        path="/tickets"
        element={
          <FlagGate>
            <RequireTicketStaff>
              <ChangePasswordGate>
                <TwoFactorGate>
                  <ManagementLayout />
                </TwoFactorGate>
              </ChangePasswordGate>
            </RequireTicketStaff>
          </FlagGate>
        }
      >
        <Route index element={<TicketDashboardPage />} />
        <Route path="list" element={<TicketListPage />} />
        <Route path="list/:ticketNumber" element={<TicketDetailPage />} />
        <Route path="customers" element={<TicketCustomersPage />} />
        <Route path="reports" element={<TicketReportsPage />} />
        <Route
          path="settings/categories"
          element={
            <RequireConfigStaff>
              <CategoriesPage />
            </RequireConfigStaff>
          }
        />
        <Route
          path="settings/labels"
          element={
            <RequireConfigStaff>
              <LabelsPage />
            </RequireConfigStaff>
          }
        />
        <Route
          path="settings/options"
          element={
            <RequireConfigStaff>
              <OptionsPage />
            </RequireConfigStaff>
          }
        />
        <Route path="*" element={<NotFoundPage />} />
      </Route>

      {/* Management console tree */}
      <Route
        path="/"
        element={
          <RequireAuth tree="console">
            <ChangePasswordGate>
              <TechnicianGate>
                <TwoFactorGate>
                  <ConsoleShell />
                </TwoFactorGate>
              </TechnicianGate>
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
