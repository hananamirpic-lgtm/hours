/**
 * The `service` namespace for the management interface (design "Front end", "Translations").
 *
 * The management tree runs on the staff app's global i18next instance. That instance is built in
 * `src/i18n/index.ts` with the `common` namespace only, and that file is not edited for this feature. So
 * this module adds the two `service` catalogues to the running instance with `addResourceBundle`, once, at
 * module load. Every management component imports it, so the namespace exists before any of them renders:
 *
 *   import '../i18n';
 *   const { t } = useTranslation(SERVICE_NAMESPACE);
 *
 * Adding a bundle to an instance does not change the active language or the `common` texts, so the Hours
 * screens are not affected. Calling it again is harmless (the same resources overwrite themselves).
 */
import i18next from '@/i18n';
import en from '@/i18n/locales/en/service.json';
import he from '@/i18n/locales/he/service.json';
import { SERVICE_NAMESPACE } from './namespace';

export { SERVICE_NAMESPACE };

/** Adds the Hebrew and English `service` catalogues to `instance`. Deep and overwriting, so repeat calls are idempotent. */
export function registerServiceBundles(instance: typeof i18next = i18next): void {
  instance.addResourceBundle('he', SERVICE_NAMESPACE, he, true, true);
  instance.addResourceBundle('en', SERVICE_NAMESPACE, en, true, true);
}

registerServiceBundles();
