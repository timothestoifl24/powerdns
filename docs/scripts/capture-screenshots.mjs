// Drives a running stack with Playwright, seeds a small demo estate and writes
// the screenshots used on /screenshots into docs/public/screenshots.
//
// It talks to the panel like a person does -- through the forms -- so the audit
// log in the captured screenshots is real rather than staged.
//
//   podman compose down -v && podman compose up -d      # a clean demo estate
//   podman run --rm --network powerdns_backend \
//     -v "$PWD:/work" -w /work/docs \
//     -e ADMIN_PASSWORD="$(cat ../secrets/webui_admin_password)" \
//     mcr.microsoft.com/playwright:v1.50.0-noble \
//     sh -c 'npm i --no-save playwright@1.50.0 && node scripts/capture-screenshots.mjs'
//
// PANEL_URL defaults to the compose-internal address; set it to
// http://127.0.0.1:9191 to run against the published port from the host.

import { chromium } from 'playwright'
import { mkdir } from 'node:fs/promises'

const BASE = (process.env.PANEL_URL || 'http://webui:8080').replace(/\/$/, '')
const USERNAME = process.env.ADMIN_USERNAME || 'admin'
const PASSWORD = process.env.ADMIN_PASSWORD
const OUT = process.env.OUT_DIR || 'public/screenshots'

if (!PASSWORD) {
  console.error('ADMIN_PASSWORD is not set (cat secrets/webui_admin_password)')
  process.exit(1)
}

const shots = []

// The panel reports every rejected form as a danger alert; fail loudly rather
// than capturing screenshots of an estate that was never seeded.
async function assertNoError(page, what) {
  const alerts = page.locator('.alert-danger')
  if (await alerts.count()) {
    throw new Error(`${what} failed: ${(await alerts.first().innerText()).trim()}`)
  }
}

async function shot(page, name, { fullPage = false } = {}) {
  await page.waitForLoadState('networkidle')
  // Bootstrap fades modals and alerts in; give them a beat to settle.
  await page.waitForTimeout(400)
  const path = `${OUT}/${name}.png`
  await page.screenshot({ path, fullPage })
  shots.push(path)
  console.log('captured', path)
}

async function login(page) {
  await page.goto(`${BASE}/auth/login`)
  await page.fill('#username', USERNAME)
  await page.fill('#password', PASSWORD)
  await Promise.all([page.waitForURL(`${BASE}/`), page.click('button[type=submit]')])
}

async function createZone(page, { name, kind = 'Native', nameservers = [], dnssec = false }) {
  await page.goto(`${BASE}/zones/new`)
  await page.fill('#name', name)
  await page.selectOption('#kind', kind)
  if (nameservers.length) await page.fill('#nameservers', nameservers.join('\n'))
  if (dnssec) await page.check('input[name=dnssec]')
  await page.getByRole('button', { name: 'Create zone' }).click()
  await page.waitForLoadState('networkidle')
  await assertNoError(page, `creating zone ${name}`)
}

async function addRecord(page, zone, { name, type, content, ttl = 3600, comment = '' }) {
  await page.goto(`${BASE}/zones/${zone}`)
  await page.click('button[data-bs-target="#record-modal"]')
  const modal = page.locator('#record-modal')
  await modal.waitFor({ state: 'visible' })
  await modal.locator('#record-name').fill(name)
  await modal.locator('#record-type').selectOption(type)
  await modal.locator('#record-ttl').fill(String(ttl))
  await modal.locator('#record-content').fill(content)
  if (comment) await modal.locator('#record-comment').fill(comment)
  await modal.locator('button[type=submit]').click()
  await page.waitForLoadState('networkidle')
  await assertNoError(page, `adding ${name} ${type} to ${zone}`)
}

async function createUser(page, { username, displayName, email, role, password }) {
  await page.goto(`${BASE}/admin/users/new`)
  await page.fill('#username', username)
  await page.fill('#display_name', displayName)
  await page.fill('#email', email)
  await page.selectOption('#role', role)
  await page.fill('#password', password)
  await page.fill('#confirm_password', password)
  await page.getByRole('button', { name: 'Create user' }).click()
  await page.waitForLoadState('networkidle')
  await assertNoError(page, `creating user ${username}`)
}

const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
  colorScheme: 'light',
})
const page = await context.newPage()

await mkdir(OUT, { recursive: true })

// The sign-in screen, before there is a session.
await page.goto(`${BASE}/auth/login`)
await shot(page, 'login')

await login(page)

// ---------------------------------------------------------------- seed data
await createZone(page, {
  name: 'example.com',
  nameservers: ['ns1.example.com', 'ns2.example.com'],
})
await addRecord(page, 'example.com.', {
  name: 'www',
  type: 'A',
  content: '192.0.2.10\n192.0.2.11',
  ttl: 300,
  comment: 'Both front-end nodes',
})
await addRecord(page, 'example.com.', {
  name: 'mail',
  type: 'A',
  content: '192.0.2.25',
})
await addRecord(page, 'example.com.', {
  name: '@',
  type: 'MX',
  content: '10 mail.example.com.',
})
await addRecord(page, 'example.com.', {
  name: '@',
  type: 'TXT',
  content: '"v=spf1 mx -all"',
  comment: 'SPF: only the MX may send',
})
await addRecord(page, 'example.com.', {
  name: 'docs',
  type: 'CNAME',
  content: 'www.example.com.',
})

await createZone(page, {
  name: 'lab.internal',
  nameservers: ['ns1.example.com'],
})
await addRecord(page, 'lab.internal.', {
  name: 'gitea',
  type: 'A',
  content: '10.20.0.14',
  ttl: 600,
})

await createUser(page, {
  username: 'j.reed',
  displayName: 'Jamie Reed',
  email: 'j.reed@example.com',
  role: 'operator',
  password: 'Sc4ffold-Demo-Only-2026-a',
})
await createUser(page, {
  username: 's.okafor',
  displayName: 'Sam Okafor',
  email: 's.okafor@example.com',
  role: 'user',
  password: 'Sc4ffold-Demo-Only-2026-b',
})

// Grant the plain user access to one zone, on their own page.
await page.goto(`${BASE}/admin/users`)
await page.locator('tr', { hasText: 's.okafor' }).locator('a[href*="/admin/users/"]').first().click()
await page.waitForLoadState('networkidle')
const grant = page.locator('form[action$="/zones"]')
if (await grant.count()) {
  await grant.locator('input[name=zones]').first().check()
  await grant.locator('button[type=submit]').click()
  await page.waitForLoadState('networkidle')
}

// Sign the flagship zone, so the DNSSEC page has keys and DS records on it.
await page.goto(`${BASE}/zones/example.com./dnssec`)
const enable = page.locator('form input[value=enable]')
if (await enable.count()) {
  await enable.locator('..').locator('button[type=submit]').click()
  await page.waitForLoadState('networkidle')
}

// ------------------------------------------------------------- the captures
await page.goto(`${BASE}/`)
await shot(page, 'dashboard')

await page.goto(`${BASE}/zones/`)
await shot(page, 'zones')

await page.goto(`${BASE}/zones/new`)
await shot(page, 'zone-new')

await page.goto(`${BASE}/zones/example.com.`)
await shot(page, 'zone-detail', { fullPage: true })

await page.goto(`${BASE}/zones/example.com.`)
await page.click('button[data-bs-target="#record-modal"]')
await page.locator('#record-modal').waitFor({ state: 'visible' })
await page.locator('#record-modal #record-name').fill('vpn')
await page.locator('#record-modal #record-type').selectOption('A')
await page.locator('#record-modal #record-content').fill('192.0.2.44')
await shot(page, 'record-editor')

await page.goto(`${BASE}/zones/example.com./dnssec`)
await shot(page, 'dnssec', { fullPage: true })

await page.goto(`${BASE}/admin/users`)
await shot(page, 'users')

await page.goto(`${BASE}/admin/audit`)
await shot(page, 'audit')

await page.goto(`${BASE}/admin/settings`)
await shot(page, 'settings', { fullPage: true })

await page.goto(`${BASE}/profile/`)
await shot(page, 'profile')

await browser.close()
console.log(`\n${shots.length} screenshots written to ${OUT}`)
