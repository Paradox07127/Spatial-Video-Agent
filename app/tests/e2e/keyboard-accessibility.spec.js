const { test, expect } = require('@playwright/test');
const { mockApi, mockAsk } = require('./mockapi');
const kernel = (page, name, expr) => page.evaluate(async ([name, expr]) => {
  const m = await import(new URL(`kernel/${name}.js`, document.baseURI).href);
  return new Function('m', `return (${expr})`)(m);
}, [name, expr]);

test.beforeEach(async ({ page }) => { await mockApi(page); });

test('command dialog keeps Tab focus, exposes its active option, and Escape returns to its invoker', async ({ page }) => {
  await page.goto('#/test');
  await page.locator('#ask-input').click();
  await page.keyboard.press('Control+k');
  const dialog = page.getByRole('dialog', { name: 'Go to a scene or ask the agent' });
  const input = dialog.getByRole('combobox');
  const list = dialog.getByRole('listbox');
  await expect(dialog).toBeVisible(); await expect(dialog).toHaveAttribute('aria-modal', 'true');
  await expect(input).toBeFocused(); await expect(input).toHaveAttribute('aria-controls', 'cmdk-options');
  await expect(input).toHaveAttribute('aria-expanded', 'true');
  const first = await input.getAttribute('aria-activedescendant');
  await expect(list.locator(`#${first}`)).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('ArrowDown');
  const second = await input.getAttribute('aria-activedescendant');
  expect(second).not.toBe(first);
  await expect(list.locator(`#${second}`)).toHaveAttribute('aria-selected', 'true');
  await expect(list.locator('[aria-selected="true"]')).toHaveCount(1);
  await page.keyboard.press('Tab'); await expect(input).toBeFocused();
  await page.keyboard.press('Shift+Tab'); await expect(input).toBeFocused();
  await expect.poll(() => input.evaluate(el => getComputedStyle(el).outlineStyle)).toBe('solid');
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden(); await expect(page.locator('#ask-input')).toBeFocused();
  await expect(page.locator('.cmdk-input')).toHaveAttribute('aria-expanded', 'false');
  await expect(page).toHaveURL(/#\/test$/);
});

test('command options activate with Enter while keeping DOM focus in the combobox', async ({ page }) => {
  await page.goto('#/test'); await page.keyboard.press('Control+k');
  const input = page.getByRole('combobox', { name: 'Scene, trip, or question' });
  await input.fill('Recent runs');
  await expect(page.getByRole('option', { name: /^Recent runs/ })).toHaveAttribute('aria-selected', 'true');
  await expect(input).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#\/runs$/);
  await expect(page.locator('#cmdk')).toBeHidden();
});

test('timeline slider has an initial value and supports bounded arrow, page, Home and End keys', async ({ page }) => {
  await page.goto('#/test');
  const slider = page.getByRole('slider', { name: 'Scene time' });
  await expect(slider).toHaveAttribute('aria-valuemin', '0');
  await expect(slider).toHaveAttribute('aria-valuemax', '9999');
  await expect(slider).toHaveAttribute('aria-valuenow', '0');
  await expect(slider).toHaveAttribute('aria-orientation', 'horizontal');
  await slider.focus();
  await page.keyboard.press('ArrowRight'); await expect(slider).toHaveAttribute('aria-valuenow', '100');
  await page.keyboard.press('ArrowUp'); await expect(slider).toHaveAttribute('aria-valuenow', '200');
  await page.keyboard.press('PageUp'); await expect(slider).toHaveAttribute('aria-valuenow', '1200');
  await page.keyboard.press('PageDown'); await expect(slider).toHaveAttribute('aria-valuenow', '200');
  await page.keyboard.press('Home'); await expect(slider).toHaveAttribute('aria-valuenow', '0');
  await page.keyboard.press('ArrowDown'); await expect(slider).toHaveAttribute('aria-valuenow', '0');
  await page.keyboard.press('End'); await expect(slider).toHaveAttribute('aria-valuenow', '9999');
  await page.keyboard.press('ArrowRight'); await expect(slider).toHaveAttribute('aria-valuenow', '9999');
  await expect.poll(() => slider.evaluate(el => getComputedStyle(el).outlineStyle)).toBe('solid');
  await kernel(page, 'clock', 'm.clock.setRange(2000, 5000)');
  await expect(slider).toHaveAttribute('aria-valuemin', '2000');
  await expect(slider).toHaveAttribute('aria-valuemax', '4999');
  await page.keyboard.press('Home'); await expect(slider).toHaveAttribute('aria-valuenow', '2000');
  await expect(slider).toHaveAttribute('aria-valuetext', '2.0 seconds');
});

test('timeline event and evidence buttons activate with Space and Enter without losing focus on redraw', async ({ page }) => {
  await page.goto('#/warehouse/wh-sdg-fixture-01');
  await page.locator('.tl-cams-btn').click();
  const event = page.locator('.tl-events [data-ev="E1"]');
  await expect(event).toHaveAttribute('role', 'button');
  await event.focus(); await page.keyboard.press('Space');
  await expect.poll(() => kernel(page, 'clock', 'm.clock.t')).toBe(4300);
  await expect(event).toBeFocused();
  await page.keyboard.press('Enter'); await expect(event).toBeFocused();
  await expect.poll(() => event.evaluate(el => getComputedStyle(el).outlineStyle)).toBe('solid');
  await mockAsk(page, 'wh-main');
  await page.goto('#/test'); await page.locator('.preset').first().click();
  await expect(page.locator('#ask-status')).toContainText('Done');
  const evidence = page.locator('.tl-evm[data-eid="e_1449f20a31b383ff"]');
  await expect(evidence).toHaveAttribute('role', 'button');
  await evidence.focus(); await page.keyboard.press('Space');
  await expect.poll(() => kernel(page, 'clock', 'm.clock.t')).toBe(4000);
  await expect(evidence).toBeFocused();
  await page.keyboard.press('Enter'); await expect(evidence).toBeFocused();
  await page.keyboard.press('Control+k');
  await expect(page.locator('#cmdk')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#cmdk')).toBeHidden();
  await expect(evidence).toBeFocused();
  await expect(page.locator('body')).toHaveAttribute('data-stage', 'evidence');
  // Escape still exits Answer mode before clearing the run.
  await page.keyboard.press('Escape'); await expect(page.locator('body')).toHaveAttribute('data-stage', 'explore');
  await expect(page.locator('.tl-evidence')).toBeVisible();
});
