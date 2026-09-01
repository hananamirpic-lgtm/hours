// Flat config. The rule that earns its place here is `react/jsx-no-literals`: it fails the build on a
// display string written straight into JSX, which is Requirement 21.1 ("no hard-coded display strings
// in components") turned into something a machine checks rather than a reviewer remembers. Everything
// user-facing has to come through `t(...)`, so both languages stay complete by construction.

import js from '@eslint/js';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'coverage'] },
  {
    files: ['src/**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    settings: { react: { version: '18.3' } },
    plugins: {
      react,
      'react-hooks': reactHooks,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,

      // The localization gate. Any literal *text* rendered inside a component — the words a user
      // reads — must instead come through a translation key. `ignoreProps` is on so structural
      // attributes (className, type, autoComplete) are not mistaken for display strings; those are
      // code, not copy. What is left is exactly Requirement 21.1: no hard-coded display text.
      'react/jsx-no-literals': [
        'error',
        {
          noStrings: true,
          allowedStrings: [],
          ignoreProps: true,
          elementOverrides: {},
        },
      ],
    },
  },
  {
    // Tests, config and the formatting library carry strings that are data, not display text.
    files: ['src/**/*.test.{ts,tsx}', 'src/lib/**/*.ts', 'src/i18n/**/*.ts', 'src/api/**/*.ts'],
    rules: {
      'react/jsx-no-literals': 'off',
    },
  },
);
