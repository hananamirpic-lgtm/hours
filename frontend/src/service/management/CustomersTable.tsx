// The customers table (task 24.5; R3.10, R3.1, R23.12): per row the full name, the normalised phone, the city,
// the apartment number and the state, then the row actions. The state is text ("Active", "Inactive"), never
// colour alone. Every row button names the customer in its accessible name, so a screen reader user can tell
// fifty "Edit" buttons apart. The phone is isolated left to right so its digits keep their order in Hebrew.
import { useTranslation } from 'react-i18next';
import type { StaffCustomer } from '../api/staffTypes';
import { actionTexts, availableActions, cellText, stateKey, type CustomerAction } from './customersModel';
import { SERVICE_NAMESPACE } from './namespace';
import './customers.css';

export interface CustomersTableProps {
  readonly rows: readonly StaffCustomer[];
  readonly onEdit: (customer: StaffCustomer) => void;
  readonly onAction: (customer: StaffCustomer, action: CustomerAction) => void;
}

export function CustomersTable({ rows, onEdit, onAction }: CustomersTableProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const rowLabel = (actionLabel: string, name: string): string =>
    t('management.customers.rowAction', { action: actionLabel, name });
  return (
    <div className="table-wrap">
      <table className="table customers-table">
        <caption className="customers-sr-only">{t('management.customers.title')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('management.customers.columns.fullName')}</th>
            <th scope="col">{t('management.customers.columns.phone')}</th>
            <th scope="col">{t('management.customers.columns.city')}</th>
            <th scope="col">{t('management.customers.columns.apartment')}</th>
            <th scope="col">{t('management.customers.columns.state')}</th>
            <th scope="col">{t('management.common.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((customer) => (
            <tr key={customer.id}>
              <th scope="row" className="customers-table__name">
                {customer.full_name}
              </th>
              <td>
                <bdi dir="ltr">{customer.phone}</bdi>
              </td>
              <td>{cellText(customer.city)}</td>
              <td>{cellText(customer.apartment)}</td>
              <td>
                <span className={customer.is_active ? 'pill pill--active' : 'pill pill--muted'}>
                  {t(stateKey(customer))}
                </span>
              </td>
              <td>
                <div className="customers-table__actions">
                  <button
                    type="button"
                    className="button button--small"
                    aria-label={rowLabel(t('management.common.edit'), customer.full_name)}
                    onClick={() => onEdit(customer)}
                  >
                    {t('management.common.edit')}
                  </button>
                  {availableActions(customer).map((action) => {
                    const label = t(actionTexts(action).labelKey);
                    return (
                      <button
                        key={action}
                        type="button"
                        className="button button--small"
                        aria-label={rowLabel(label, customer.full_name)}
                        onClick={() => onAction(customer, action)}
                      >
                        {label}
                      </button>
                    );
                  })}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
