import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface ErrorBoundaryProps {
  children: ReactNode
}

interface ErrorBoundaryState {
  hasError: boolean
  error: Error | null
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Логируем в консоль — в production sourcemaps восстановят стектрейс
    console.error('ErrorBoundary поймал ошибку:', error, info.componentStack)
  }

  render() {
    if (!this.state.hasError) return this.props.children

    return (
      <div
        style={{
          position: 'fixed',
          inset: 0,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 20,
          padding: 32,
          background: 'var(--bg, #111113)',
          color: 'var(--text, #E8E8EC)',
          fontFamily: "'Inter', 'Inter', system-ui, sans-serif",
          textAlign: 'center',
          zIndex: 99999,
        }}
      >
        <div
          style={{
            fontSize: 'var(--fs-xl, 30px)',
            fontWeight: 700,
            letterSpacing: '0.04em',
            color: 'var(--crit, #C04040)',
          }}
        >
          Ошибка отображения интерфейса
        </div>
        <div
          style={{
            fontSize: 'var(--fs-sm, 14px)',
            color: 'var(--text-3, #9292A4)',
            maxWidth: 640,
          }}
        >
          Произошёл сбой при отрисовке. Данные продолжают обновляться на сервере —
          перезагрузите страницу, чтобы восстановить интерфейс.
        </div>
        {/* Детали ошибки показываем только в dev-режиме, чтобы не раскрывать
            внутренности реализации конечным пользователям в production */}
        {this.state.error && import.meta.env.DEV && (
          <pre
            style={{
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              fontSize: 'var(--fs-xs, 12px)',
              color: 'var(--text-2, #BBBCCE)',
              background: 'var(--surface, #18181C)',
              border: '1px solid var(--line, #2C2C32)',
              borderRadius: 'var(--r-md, 0px)',
              padding: '12px 16px',
              maxWidth: 'min(90vw, 720px)',
              maxHeight: '30vh',
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              textAlign: 'left',
              margin: 0,
            }}
          >
            {this.state.error.message}
          </pre>
        )}
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', justifyContent: 'center' }}>
          {/* Сброс границы без перезагрузки страницы — сохраняет состояние остального UI */}
          <button
            onClick={() => this.setState({ hasError: false, error: null })}
            style={{
              padding: '10px 24px',
              fontFamily: 'inherit',
              fontSize: 'var(--fs-sm, 14px)',
              fontWeight: 700,
              letterSpacing: '0.04em',
              color: 'var(--text-1, #fff)',
              background: 'var(--surface-2, #1E1E24)',
              border: '1px solid var(--line, #2C2C32)',
              borderRadius: 'var(--r-md, 0px)',
              cursor: 'pointer',
            }}
          >
            Попробовать снова
          </button>
          <button
            onClick={() => location.reload()}
            style={{
              padding: '10px 24px',
              fontFamily: 'inherit',
              fontSize: 'var(--fs-sm, 14px)',
              fontWeight: 700,
              letterSpacing: '0.04em',
              color: '#fff',
              background: 'var(--accent, #4A90A4)',
              border: 'none',
              borderRadius: 'var(--r-md, 0px)',
              cursor: 'pointer',
            }}
          >
            Перезагрузить страницу
          </button>
        </div>
      </div>
    )
  }
}
