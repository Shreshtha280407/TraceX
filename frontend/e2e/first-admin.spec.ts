import { expect, test } from '@playwright/test'

test('first-admin route reflects deployment setup state', async ({ page, request }) => {
  const status = await request.get('/api/v1/setup/first-admin/status')
  expect(status.ok()).toBeTruthy()
  const { setup_required: setupRequired } = (await status.json()) as { setup_required: boolean }

  await page.goto('/setup/first-admin')
  if (setupRequired) {
    await expect(page.getByRole('heading', { name: 'Private deployment setup' })).toBeVisible()
    await expect(page.getByLabel('One-time deployment setup token')).toBeVisible()
  } else {
    await expect(page).toHaveURL(/\/login$/)
  }
})

test('landing page exposes only the state-appropriate entry action', async ({ page, request }) => {
  const status = await request.get('/api/v1/setup/first-admin/status')
  expect(status.ok()).toBeTruthy()
  const { setup_required: setupRequired } = (await status.json()) as { setup_required: boolean }

  await page.goto('/')
  if (setupRequired) {
    await expect(page.getByRole('link', { name: 'Set up first administrator' }).first()).toHaveAttribute(
      'href',
      '/setup/first-admin',
    )
  } else {
    await expect(page.getByRole('link', { name: 'Sign in' }).first()).toHaveAttribute('href', '/login')
  }
})
