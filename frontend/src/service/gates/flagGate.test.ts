import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { featuresKeys, type FlagGateState } from '@/service/api/features';
import { FlagGate, FlagGateContent } from '@/service/gates/FlagGate';
import { flagGateView } from '@/service/gates/flagGateView';
import { NOT_AVAILABLE_TEXT } from '@/service/gates/flagGateStrings';

const SIGN_IN_MARKER = 'SIGN-IN-SCREEN';

const render = (state: FlagGateState): string =>
  renderToStaticMarkup(
    createElement(FlagGateContent, {
      state,
      onRetry: () => undefined,
      children: createElement('form', null, SIGN_IN_MARKER),
    }),
  );

describe('flagGateView', () => {
  it.each([
    ['on', 'content'],
    ['off', 'not-available'],
    ['unknown', 'retry'],
    ['loading', 'loading'],
  ] as const)('%s renders the %s view', (state, view) => {
    expect(flagGateView(state)).toBe(view);
  });
});

describe('FlagGateContent', () => {
  it('on renders the wrapped children', () => {
    expect(render('on')).toContain(SIGN_IN_MARKER);
  });

  it('off renders the bilingual not-available page with no form, no control and no link', () => {
    const html = render('off');
    expect(html).toContain(NOT_AVAILABLE_TEXT.he);
    expect(html).toContain(NOT_AVAILABLE_TEXT.en);
    expect(html).not.toContain(SIGN_IN_MARKER);
    expect(html).not.toMatch(/<(form|input|button|a|select|textarea)[\s>]/);
    expect(html).not.toContain('href');
  });

  it('off page text reads as the one bilingual sentence', () => {
    const text = render('off').replace(/<[^>]*>/g, '');
    expect(text).toBe(`${NOT_AVAILABLE_TEXT.he} / ${NOT_AVAILABLE_TEXT.en}`);
  });

  it('unknown renders a retry panel with one button and no children', () => {
    const html = render('unknown');
    expect(html).not.toContain(SIGN_IN_MARKER);
    expect(html).not.toContain(NOT_AVAILABLE_TEXT.en);
    expect(html.match(/<button[\s>]/g)).toHaveLength(1);
    expect(html).toContain('role="alert"');
    expect(html).not.toContain('<form');
    expect(html).not.toContain('href');
  });

  it('loading renders a neutral status with no children and no controls', () => {
    const html = render('loading');
    expect(html).toContain('role="status"');
    expect(html).not.toContain(SIGN_IN_MARKER);
    expect(html).not.toMatch(/<(form|input|button|a)[\s>]/);
  });
});

describe('FlagGate', () => {
  const renderGate = (seed: 'on' | 'off' | undefined): string => {
    const client = new QueryClient();
    if (seed !== undefined) {
      client.setQueryData(featuresKeys.all, seed);
    }
    return renderToStaticMarkup(
      createElement(
        QueryClientProvider,
        { client },
        createElement(FlagGate, { children: createElement('form', null, SIGN_IN_MARKER) }),
      ),
    );
  };

  it('shows the children when the shared query holds on', () => {
    expect(renderGate('on')).toContain(SIGN_IN_MARKER);
  });

  it('shows the not-available page and never the children when the query holds off', () => {
    const html = renderGate('off');
    expect(html).toContain(NOT_AVAILABLE_TEXT.he);
    expect(html).not.toContain(SIGN_IN_MARKER);
  });

  it('shows the loading state while the first answer is outstanding', () => {
    const html = renderGate(undefined);
    expect(html).toContain('role="status"');
    expect(html).not.toContain(SIGN_IN_MARKER);
  });
});
