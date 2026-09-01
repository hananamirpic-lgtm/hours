/**
 * The settings screen — read and tune the calculation engine's knobs (administrator only).
 *
 * The top-level "Settings" screen: every tuning knob the engine reads — the working-day length (the
 * daily overtime threshold), the Shabbat window, the anomaly thresholds — each shown with an input
 * typed to its value (a number box, a time picker, a checkbox, or a text box) and labelled from the
 * reader's language. A known knob is labelled and explained from the resource files; a knob the UI
 * does not yet name falls back to the server's own description, so a setting added later still reads
 * sensibly with no code change.
 *
 * Both endpoints are administrator-only on the server. A non-administrator caller gets a 403, which
 * this shows as a plain admin-only note rather than a load error, matching how the audit console
 * handles its administrator-only feed.
 *
 * The working-day length is presented in hours (an eight-hour day, not a 480-minute one) and converted
 * back to whole minutes on save; `settingsView` owns that conversion. Save sends only the knobs the
 * administrator actually changed, so a no-op field is never written and never audited. A validation
 * failure comes back as the error envelope and is rendered from its machine code in the reader's
 * language. Every string is a translation key and Hebrew renders right-to-left with no change here.
 *
 * Changing a setting alters future calculations only — the engine reads a knob at calculation time —
 * so this page triggers no recalculation and nothing already computed is rewritten.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';

import { ApiError } from '@/api/client';
import { listSettings, settingsKeys, updateSettings } from '@/api/settings';
import type { SettingItem } from '@/api/types';
import {
  helpKeyFor,
  inputKindFor,
  labelKeyFor,
  toDisplayValue,
  toStoredValue,
} from '@/app/settings/settingsView';
import { LoadingState } from '@/components/management/QueryState';
import { toError } from '@/lib/apiError';

/** The draft values keyed by setting key, as strings in the form's own (display) form. */
type Draft = Record<string, string>;

const draftFrom = (items: SettingItem[]): Draft =>
  Object.fromEntries(items.map((item) => [item.key, toDisplayValue(item)]));

/**
 * The knobs whose display value differs from what the server holds, converted back to stored form.
 * Only changed knobs are returned, so a save sends the minimum and audits only what moved.
 */
const changedUpdates = (items: SettingItem[], draft: Draft): Record<string, string> => {
  const updates: Record<string, string> = {};
  for (const item of items) {
    const stored = toStoredValue(item.key, draft[item.key] ?? '');
    if (stored !== item.value) {
      updates[item.key] = stored;
    }
  }
  return updates;
};

export function SettingsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: settingsKeys.list(),
    queryFn: listSettings,
  });

  const items = useMemo(() => query.data?.items ?? [], [query.data]);

  const [draft, setDraft] = useState<Draft | null>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [errorParams, setErrorParams] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);

  // The draft is seeded from the server on first successful load and whenever the query data changes
  // (after a save invalidates it), so the form always reflects the stored values without a stale copy.
  const currentDraft = draft ?? draftFrom(items);

  const set = (key: string) => (value: string) => {
    setDraft({ ...currentDraft, [key]: value });
    setSaved(false);
  };

  const mutation = useMutation({
    mutationFn: () => updateSettings(changedUpdates(items, currentDraft)),
    onSuccess: async (response) => {
      setDraft(draftFrom(response.items));
      setErrorKey(null);
      setErrorParams({});
      setSaved(true);
      await queryClient.invalidateQueries({ queryKey: settingsKeys.all });
    },
    onError: (error) => {
      const translated = toError(error);
      setErrorKey(translated.key);
      setErrorParams(translated.params);
      setSaved(false);
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setSaved(false);
    setErrorKey(null);
    mutation.mutate();
  };

  if (query.isPending) {
    return (
      <section className="page-body">
        <div className="page-header">
          <h1>{t('settings.title')}</h1>
        </div>
        <LoadingState />
      </section>
    );
  }

  if (query.isError) {
    const forbidden = query.error instanceof ApiError && query.error.status === 403;
    return (
      <section className="page-body">
        <div className="page-header">
          <h1>{t('settings.title')}</h1>
        </div>
        <p className="state" role={forbidden ? undefined : 'alert'}>
          {t(forbidden ? 'settings.adminOnly' : 'common.loadError')}
        </p>
      </section>
    );
  }

  return (
    <section className="page-body">
      <div className="page-header">
        <h1>{t('settings.title')}</h1>
      </div>
      <p className="subtitle">{t('settings.subtitle')}</p>

      <form className="form" onSubmit={onSubmit}>
        {items.map((item) => (
          <SettingField
            key={item.key}
            item={item}
            value={currentDraft[item.key] ?? ''}
            onChange={set(item.key)}
          />
        ))}

        {errorKey ? (
          <p className="feedback feedback--error" role="alert">
            {t(errorKey, errorParams)}
          </p>
        ) : null}
        {saved ? (
          <p className="feedback feedback--success" role="status">
            {t('settings.saved')}
          </p>
        ) : null}

        <div className="form__actions">
          <button type="submit" className="button button--primary" disabled={mutation.isPending}>
            {mutation.isPending ? t('common.saving') : t('settings.save')}
          </button>
        </div>
      </form>
    </section>
  );
}

/**
 * One setting's row: a label and helptext, and the input typed to the value. A known key is labelled
 * and explained from the resource files; an unknown key falls back to the server `description`. The
 * working-day length reads in hours with a minutes note appended, so the administrator sees both the
 * friendly unit and what it means.
 */
function SettingField({
  item,
  value,
  onChange,
}: {
  item: SettingItem;
  value: string;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  const kind = inputKindFor(item);

  const label = t(labelKeyFor(item.key), { defaultValue: item.key });
  // The helptext prefers a translated note and falls back to the server's description; an untranslated
  // known key with no description shows nothing rather than a raw key.
  const help = t(helpKeyFor(item.key), { defaultValue: item.description ?? '' });

  if (kind === 'checkbox') {
    return (
      <label className="field field--inline">
        <input
          className="checkbox"
          type="checkbox"
          checked={value === 'true'}
          onChange={(event) => onChange(event.target.checked ? 'true' : 'false')}
        />
        <span className="field__label">{label}</span>
        {help ? <span className="field__hint">{help}</span> : null}
      </label>
    );
  }

  return (
    <label className="field">
      <span className="field__label">{label}</span>
      <input
        className="input"
        type={kind === 'number' ? 'number' : kind === 'time' ? 'time' : 'text'}
        {...(kind === 'number' ? { step: 'any' } : {})}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      {help ? <span className="field__hint">{help}</span> : null}
    </label>
  );
}
