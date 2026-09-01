import { useTranslation } from 'react-i18next';

import { setLanguagePreference } from '@/api/auth';
import { useAuth } from '@/auth/AuthProvider';
import { changeLanguage, type Language } from '@/i18n';

/**
 * Switches language and, with it, the document direction.
 *
 * The switch is applied locally at once, so the interface flips without waiting on the network. When
 * a user is signed in it is also written to their account, so the choice follows them to the next
 * device (Requirement 21.2); signed out — on the login screen — only the local copy changes, which is
 * the browser's guess until an account overrides it. A failed write leaves the local flip in place: a
 * user should not be trapped in a language they did not choose because the server was briefly away.
 */
export function LanguageToggle() {
  const { t, i18n } = useTranslation();
  const { user } = useAuth();
  const next: Language = i18n.language === 'he' ? 'en' : 'he';

  const onToggle = async () => {
    await changeLanguage(next);
    if (user) {
      await setLanguagePreference(next).catch(() => {
        // The local flip stands; the account keeps its previous value until the next successful write.
      });
    }
  };

  return (
    <button
      type="button"
      className="button"
      aria-label={t('language.label')}
      onClick={() => {
        void onToggle();
      }}
    >
      {t('language.switchTo')}
    </button>
  );
}
