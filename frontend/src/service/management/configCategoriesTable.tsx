// The category table (task 24.7; R19.9, R19.10, R23.12): both names, the state and the row actions. The state
// is text ("Active", "Inactive"), never colour alone. Every row button names the category in its accessible
// name, so a screen reader user can tell the "Edit" buttons apart. The Hebrew name is shown right to left and
// the English name left to right, whatever the language of the screen.
import { useTranslation } from 'react-i18next';
import type { StaffCategory } from '../api/staffTypes';
import {
  availableCategoryActions,
  categoryActionTexts,
  categoryDisplayName,
  categoryStateKey,
  type CategoryAction,
  type ConfigLanguage,
} from './configModel';
import { SERVICE_NAMESPACE } from './namespace';
import './config.css';

export interface CategoriesTableProps {
  readonly rows: readonly StaffCategory[];
  readonly language: ConfigLanguage;
  /** True while a deactivate or reactivate request is running: the row buttons do nothing (and keep focus). */
  readonly busy: boolean;
  readonly onEdit: (category: StaffCategory) => void;
  readonly onAction: (category: StaffCategory, action: CategoryAction) => void;
}

export function CategoriesTable({ rows, language, busy, onEdit, onAction }: CategoriesTableProps) {
  const { t } = useTranslation(SERVICE_NAMESPACE);
  const rowLabel = (actionLabel: string, name: string): string =>
    t('management.settings.categories.rowAction', { action: actionLabel, name });
  return (
    <div className="table-wrap">
      <table className="table config-table">
        <caption className="config-sr-only">{t('management.settings.categories.title')}</caption>
        <thead>
          <tr>
            <th scope="col">{t('management.settings.categories.nameHe')}</th>
            <th scope="col">{t('management.settings.categories.nameEn')}</th>
            <th scope="col">{t('management.settings.categories.state')}</th>
            <th scope="col">{t('management.common.actions')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((category) => {
            const name = categoryDisplayName(category, language);
            return (
              <tr key={category.id}>
                <th scope="row" className="config-table__name" lang="he" dir="rtl">
                  {category.name_he}
                </th>
                <td lang="en" dir="ltr">
                  {category.name_en}
                </td>
                <td>
                  <span className={category.is_active ? 'pill pill--active' : 'pill pill--muted'}>
                    {t(categoryStateKey(category))}
                  </span>
                </td>
                <td>
                  <div className="config-table__actions">
                    <button
                      type="button"
                      className="button button--small"
                      aria-disabled={busy}
                      aria-label={rowLabel(t('management.common.edit'), name)}
                      onClick={() => {
                        if (!busy) onEdit(category);
                      }}
                    >
                      {t('management.common.edit')}
                    </button>
                    {/* Keyed by position, so the button that was deactivate keeps keyboard focus when it
                        becomes reactivate. */}
                    {availableCategoryActions(category).map((action, index) => {
                      const label = t(categoryActionTexts(action).labelKey);
                      return (
                        <button
                          key={index}
                          type="button"
                          className="button button--small"
                          aria-disabled={busy}
                          aria-label={rowLabel(label, name)}
                          onClick={() => {
                            if (!busy) onAction(category, action);
                          }}
                        >
                          {label}
                        </button>
                      );
                    })}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
