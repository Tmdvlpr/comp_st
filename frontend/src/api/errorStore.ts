import { useSyncExternalStore } from 'react'

// Простой глобальный стор последней ошибки API на базе useSyncExternalStore.
// QueryCache.onError вызывает reportApiError(), onSuccess — clearApiError().

let lastErrorAt = 0
const listeners = new Set<() => void>()

function emit() {
  for (const l of listeners) l()
}

export function reportApiError() {
  lastErrorAt = Date.now()
  emit()
}

export function clearApiError() {
  if (lastErrorAt === 0) return
  lastErrorAt = 0
  emit()
}

function subscribe(cb: () => void) {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

function getSnapshot() {
  return lastErrorAt
}

/** Возвращает timestamp последней ошибки API (0 — ошибок нет). */
export function useApiErrorAt() {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}
