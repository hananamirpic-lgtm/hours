/**
 * Create or edit a login (Requirement 1, 2.1).
 *
 * On create: a username, a password, a role, and a language. On edit: the same, except the password
 * field is a *reset* — left blank it changes nothing, and filled it re-hashes and ends the user's
 * sessions immediately (Requirement 20.8). A site manager's site scope is not set here; it is assigned
 * from the card once the login exists, because it needs the created id and a live site list. `is_active`
 * is not a form field either — deactivation is a deliberate act on the card, not one field among many.
 *
 * Every message is a translation key, and a failure is rendered from the machine `code` the API
 * returns (e.g. a duplicate username), so the words live here, not on the server.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import type { AppLanguage, UserCreate, UserResponse, UserRole, UserUpdate } from '@/api/types';
import { createUser, updateUser, userKeys } from '@/api/users';
import { employeeKeys, listEmployees } from '@/api/employees';
import { toError } from '@/lib/apiError';

const ROLES: UserRole[] = ['admin', 'site_manager', 'accounting', 'employee'];
const LANGUAGES: AppLanguage[] = ['he', 'en'];

interface Fields {
  username: string;
  password: string;
  role: UserRole;
  language: AppLanguage;
  employeeId: string;
}

const fromUser = (user: UserResponse): Fields => ({
  username: user.username,
  password: '',
  role: user.role,
  language: user.language,
  employeeId: user.employee_id ?? '',
});

const emptyFields: Fields = {
  username: '',
  password: '',
  role: 'site_manager',
  language: 'he',
  employeeId: '',
};

export function UserForm({
  user,
  onDone,
  onCancel,
}: {
  user?: UserResponse;
  onDone: (id: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const editing = user !== undefined;
  const [fields, setFields] = useState<Fields>(user ? fromUser(user) : emptyFields);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});

  const set =
    <K extends keyof Fields>(key: K) =>
    (event: { target: { value: string } }) =>
      setFields((prev) => ({ ...prev, [key]: event.target.value as Fields[K] }));

  // The list of employees to link an employee-role login to. Loaded only when the role calls for it,
  // and only on create — the link is set once, when the login is made. A directory list, no sensitive
  // fields, so a large workforce still loads cheaply here.
  const needsEmployee = fields.role === 'employee' && !editing;
  // limit is the endpoint's maximum: the list caps at 200 per page (a higher value is a 422), which
  // is plenty for a link picker over the active directory.
  const employeeList = useQuery({
    queryKey: employeeKeys.list({ status: 'active', limit: 200, offset: 0 }),
    queryFn: () => listEmployees({ status: 'active', limit: 200, offset: 0 }),
    enabled: needsEmployee,
  });

  const mutation = useMutation({
    mutationFn: async (): Promise<UserResponse> => {
      if (editing && user) {
        const payload: UserUpdate = {
          username: fields.username,
          role: fields.role,
          language: fields.language,
        };
        if (fields.password) {
          payload.password = fields.password;
        }
        return updateUser(user.id, payload);
      }
      const payload: UserCreate = {
        username: fields.username,
        password: fields.password,
        role: fields.role,
        language: fields.language,
      };
      // An employee login is meaningful only when tied to a person: the mobile app shows that
      // employee's own scans and hours, which it finds through this link.
      if (fields.role === 'employee') {
        payload.employee_id = fields.employeeId || null;
      }
      return createUser(payload);
    },
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: userKeys.all });
      onDone(saved.id);
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setErrorKey(null);
    setErrorParams({});
    mutation.mutate();
  };

  return (
    <form className="form" onSubmit={onSubmit}>
      <label className="field">
        <span className="field__label">{t('user.username')}</span>
        <input
          className="input"
          value={fields.username}
          onChange={set('username')}
          autoComplete="off"
          required
        />
      </label>

      <label className="field">
        <span className="field__label">{editing ? t('user.newPassword') : t('user.password')}</span>
        <input
          className="input"
          type="password"
          value={fields.password}
          onChange={set('password')}
          autoComplete="new-password"
          minLength={8}
          required={!editing}
          placeholder={editing ? t('user.passwordUnchanged') : undefined}
        />
        {editing ? <span className="field__hint">{t('user.resetHint')}</span> : null}
      </label>

      <div className="form-row">
        <label className="field">
          <span className="field__label">{t('user.role')}</span>
          <select className="input" value={fields.role} onChange={set('role')}>
            {ROLES.map((role) => (
              <option key={role} value={role}>
                {t(`role.${role}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field__label">{t('user.language')}</span>
          <select className="input" value={fields.language} onChange={set('language')}>
            {LANGUAGES.map((language) => (
              <option key={language} value={language}>
                {t(`user.language_${language}`)}
              </option>
            ))}
          </select>
        </label>
      </div>

      {needsEmployee ? (
        <label className="field">
          <span className="field__label">{t('user.linkedEmployee')}</span>
          <select className="input" value={fields.employeeId} onChange={set('employeeId')} required>
            <option value="">{t('user.selectEmployee')}</option>
            {(employeeList.data?.items ?? []).map((employee) => (
              <option key={employee.id} value={employee.id}>
                {t('assignment.employeeOption', {
                  name: employee.full_name,
                  nameEn: employee.full_name_en,
                })}
              </option>
            ))}
          </select>
          <span className="field__hint">{t('user.linkedEmployeeHint')}</span>
        </label>
      ) : null}

      {fields.role === 'site_manager' && !editing ? (
        <p className="subtitle">{t('user.assignSitesAfter')}</p>
      ) : null}

      {errorKey ? (
        <p className="feedback feedback--error" role="alert">
          {t(errorKey, errorParams)}
        </p>
      ) : null}

      <div className="form__actions">
        <button type="button" className="button" onClick={onCancel}>
          {t('common.cancel')}
        </button>
        <button type="submit" className="button button--primary" disabled={mutation.isPending}>
          {mutation.isPending ? t('common.saving') : t('common.save')}
        </button>
      </div>
    </form>
  );
}
