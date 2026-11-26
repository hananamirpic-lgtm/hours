// The confirmation of a customer action: deactivate, reactivate, reset password (task 24.5; R3.7, R3.8,
// R3.9, R3.12, R3.14, R23.12).
//
// The text says what the action does to the customer. The reset text names the initial password rule (the
// customer gets the initial password again and must choose a new one at the next sign-in); it shows no hash
// and the API returns none (R3.12). Focus starts on the safe choice, Cancel. A refusal stays in the dialog as
// an alert; `customer_not_found` (R3.14) also refreshes the list behind it and leaves only Close.
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  deactivateCustomer,
  isStaffApiError,
  reactivateCustomer,
  resetCustomerPassword,
  staffKeys,
  type StaffCustomer,
} from '../api/staff';
import { CustomerDialog } from './CustomerDialog';
import { actionTexts, errorTextKey, isCustomerGone, isolateName, type CustomerAction, type ErrorTextKey } from './customersModel';
import { SERVICE_NAMESPACE } from './namespace';
import './customers.css';

export const CONFIRM_IDS = {
  title: 'customer-confirm-title',
  prompt: 'customer-confirm-prompt',
  confirm: 'customer-confirm-yes',
  dismiss: 'customer-confirm-no',
} as const;

export interface CustomerConfirmViewProps {
  readonly action: CustomerAction;
  readonly customer: Pick<StaffCustomer, 'full_name'>;
  readonly pending: boolean;
  /** The text key of the refusal, when the last attempt failed. */
  readonly error: ErrorTextKey | null;
  /** True after `customer_not_found`: there is nothing left to confirm. */
  readonly gone: boolean;
  readonly onConfirm: () => void;
  readonly onDismiss: () => void;
}

/** The body of the dialog. Presentational: no request, no cache. */
export function CustomerConfirmView({ action, customer, pending, error, gone, onConfirm, onDismiss }: CustomerConfirmViewProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const texts = actionTexts(action);
  const errorText = error === null ? null : `${t('a11y.errorPrefix')} ${t(error)}`;
  const confirmClass = action === 'reactivate' ? 'button button--primary' : 'button button--danger';
  return (
    <>
      <p id={CONFIRM_IDS.prompt}>{t(texts.promptKey, { name: isolateName(customer.full_name) })}</p>
      {errorText === null ? null : (
        <p className="feedback feedback--error" role="alert">
          {errorText}
        </p>
      )}
      <div className="form__actions">
        {gone ? null : (
          <button type="button" id={CONFIRM_IDS.confirm} className={confirmClass} disabled={pending} onClick={onConfirm}>
            {pending ? t('management.customers.working') : t(texts.labelKey)}
          </button>
        )}
        <button type="button" id={CONFIRM_IDS.dismiss} className="button" disabled={pending} onClick={onDismiss}>
          {gone ? t('management.common.close') : t('management.common.cancel')}
        </button>
      </div>
    </>
  );
}

function runAction(action: CustomerAction, customerId: string): Promise<unknown> {
  switch (action) {
    case 'deactivate':
      return deactivateCustomer(customerId);
    case 'reactivate':
      return reactivateCustomer(customerId);
    case 'reset':
      return resetCustomerPassword(customerId);
  }
}

export interface CustomerConfirmDialogProps {
  readonly action: CustomerAction;
  readonly customer: StaffCustomer;
  /** Called after the Ticket_API did it. The page closes the dialog and says what happened. */
  readonly onDone: (action: CustomerAction) => void;
  readonly onDismiss: () => void;
}

export function CustomerConfirmDialog({ action, customer, onDone, onDismiss }: CustomerConfirmDialogProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const queryClient = useQueryClient();
  const [error, setError] = useState<ErrorTextKey | null>(null);
  const [gone, setGone] = useState(false);

  const mutation = useMutation({
    mutationFn: () => runAction(action, customer.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: staffKeys.customers });
      onDone(action);
    },
    onError: (caught) => {
      const code = isStaffApiError(caught) ? caught.code : 'generic';
      setError(errorTextKey(code));
      if (isCustomerGone(code)) {
        setGone(true);
        void queryClient.invalidateQueries({ queryKey: staffKeys.customers });
      }
    },
  });

  return (
    <CustomerDialog
      titleId={CONFIRM_IDS.title}
      title={t(actionTexts(action).titleKey)}
      initialFocusId={CONFIRM_IDS.dismiss}
      describedById={CONFIRM_IDS.prompt}
      busy={mutation.isPending}
      onClose={onDismiss}
    >
      <CustomerConfirmView
        action={action}
        customer={customer}
        pending={mutation.isPending}
        error={error}
        gone={gone}
        onConfirm={() => {
          if (mutation.isPending) return;
          setError(null);
          mutation.mutate();
        }}
        onDismiss={onDismiss}
      />
    </CustomerDialog>
  );
}
