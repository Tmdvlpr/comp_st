import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // RC-правила eslint-plugin-react-hooks@7 (purity / set-state-in-effect / refs)
      // помечают распространённые легаси-паттерны во ВСЁМ существующем UI
      // (Date.now для окон отображения, сброс state в effect, присваивание ref).
      // Понижено до warning: рефакторить рабочий, оттестированный UI ради нового
      // RC-линтера — лишний риск. Новый код этих паттернов избегает.
      'react-hooks/purity': 'warn',
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/refs': 'warn',
    },
  },
])
