// Pure page-structure helpers shared by every portal screen (R8.4, R23.10; design "Accessibility measures",
// row "Page structure": one <h1> per screen, landmarks, `document.title` per screen, focus moved to the
// <h1> on route change).
//
// Nothing here touches the DOM or React, so every rule is testable without a browser. The hook that
// applies them is `usePageChrome.ts`.
import { PORTAL_PATH, isPortalPath } from '../portalPath';

/** One key per portal screen. Each has a `portal.pageTitle.<key>` entry in the `service` locale. */
export const PAGE_KEYS = [
  'signIn',
  'changePassword',
  'list',
  'newTicket',
  'confirmation',
  'detail',
  'notFound',
  'accessibility',
  'unavailable',
] as const;

export type PageKey = (typeof PAGE_KEYS)[number];

/** Values interpolated into a page title. Only the ticket detail has one: `{ number }`. */
export type PageTitleParams = Readonly<Record<string, string | number>>;

/** The part of i18next's `t` this module needs, so tests and callers can pass any translator. */
export type Translate = (key: string, options?: Record<string, unknown>) => string;

/** True when `value` is one of the known page keys. */
export function isPageKey(value: string): value is PageKey {
  return (PAGE_KEYS as readonly string[]).includes(value);
}

/**
 * The document title of a screen: `<page> — Service tickets` (or `<page> — קריאות שירות`), built from the
 * `portal.pageTitle.template` of the current language. An unknown page key falls back to the app name
 * alone, so a title is never empty and never shows a raw translation key.
 */
export function documentTitle(pageKey: string, params: PageTitleParams | undefined, t: Translate): string {
  if (!isPageKey(pageKey)) {
    return t('portal.appName');
  }
  const page = t(`portal.pageTitle.${pageKey}`, { ...params });
  return t('portal.pageTitle.template', { page });
}

/** What a route path means for the page chrome. */
export interface PageForPath {
  pageKey: PageKey;
  params?: { number: string };
}

/**
 * The path below the router basename, with no query or hash, no trailing slash (except the root) and
 * with the `/service` prefix removed when the caller passes a full browser path.
 */
export function routePath(pathname: string): string {
  let path = pathname;
  const cut = path.search(/[?#]/);
  if (cut !== -1) path = path.slice(0, cut);
  if (isPortalPath(path)) path = path.slice(PORTAL_PATH.length);
  path = path.replace(/\/+$/, '');
  return path === '' ? '/' : path.startsWith('/') ? path : `/${path}`;
}

function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/**
 * The page key (and, for the detail page, the ticket number) of a route path. The sign-in and
 * change-password screens render in place of a gated path (R6.2) and are not path-driven, so they pass
 * their own key. Any path the route table does not know is `notFound`.
 */
export function pageForPath(pathname: string): PageForPath {
  const path = routePath(pathname);
  if (path === '/') return { pageKey: 'list' };
  if (path === '/new') return { pageKey: 'newTicket' };
  if (path === '/new/done') return { pageKey: 'confirmation' };
  if (path === '/accessibility') return { pageKey: 'accessibility' };
  const detail = /^\/tickets\/([^/]+)$/.exec(path);
  if (detail !== null) return { pageKey: 'detail', params: { number: decodeSegment(detail[1] ?? '') } };
  return { pageKey: 'notFound' };
}

/** True when `<html>`-level content has an open modal dialog that must keep keyboard focus. */
export function isDialogOpen(root: Pick<ParentNode, 'querySelector'>): boolean {
  return root.querySelector('dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]') !== null;
}

/** The identity of a screen view: changes when the path or the screen changes, never on a data reload. */
export function pageViewKey(pathname: string, pageKey: string): string {
  return `${routePath(pathname)}\n${pageKey}`;
}

/**
 * Whether to move focus to the `<h1>` now. Only when the view changed after the first one (the first
 * load keeps the browser's own start-of-page behaviour, so the skip link stays the first tab stop), and
 * never while a dialog is open.
 */
export function shouldMoveFocus(previousViewKey: string | null, viewKey: string, dialogOpen: boolean): boolean {
  return previousViewKey !== null && previousViewKey !== viewKey && !dialogOpen;
}
