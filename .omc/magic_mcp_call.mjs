// Direct JSON-RPC client for 21st.dev Magic MCP over stdio
import { spawn } from 'node:child_process'

const [,, method = 'tools/list', argsJson = '{}'] = process.argv

const proc = spawn('npx', ['-y', '@21st-dev/magic@latest'], {
  shell: true,
  env: { ...process.env, API_KEY: '8d73671763dd8ea7835c6e5efe501fd21042930d81b58514b48eecc09c05e8ca' },
})

let buf = ''
const pending = new Map()
let nextId = 1

proc.stdout.on('data', d => {
  buf += d.toString()
  let nl
  while ((nl = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, nl).trim()
    buf = buf.slice(nl + 1)
    if (!line) continue
    try {
      const msg = JSON.parse(line)
      if (msg.id && pending.has(msg.id)) {
        pending.get(msg.id)(msg)
        pending.delete(msg.id)
      }
    } catch { /* log lines */ }
  }
})
proc.stderr.on('data', () => {})

function rpc(method, params) {
  return new Promise((resolve, reject) => {
    const id = nextId++
    pending.set(id, resolve)
    proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n')
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error(`timeout: ${method}`)) } }, 300000)
  })
}

const init = await rpc('initialize', {
  protocolVersion: '2024-11-05',
  capabilities: {},
  clientInfo: { name: 'claude-direct', version: '1.0' },
})
proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n')

if (method === 'tools/list') {
  const res = await rpc('tools/list', {})
  console.log(JSON.stringify(res.result?.tools?.map(t => ({ name: t.name, schema: t.inputSchema })), null, 2))
} else {
  const { readFileSync } = await import('node:fs')
  const args = argsJson.endsWith('.json') ? JSON.parse(readFileSync(argsJson, 'utf8')) : JSON.parse(argsJson)
  const res = await rpc('tools/call', { name: method, arguments: args })
  const content = res.result?.content
  if (Array.isArray(content)) {
    for (const c of content) console.log(c.type === 'text' ? c.text : JSON.stringify(c))
  } else {
    console.log(JSON.stringify(res.result ?? res.error, null, 2))
  }
}

proc.kill()
process.exit(0)
