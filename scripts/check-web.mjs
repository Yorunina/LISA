import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { getSchema } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { chromium } from '@playwright/test';

// Toolchain probe only; application behavior is checked by scripts/test_app.py --browser.
const schema = getSchema([StarterKit]);
const doc = schema.nodeFromJSON({
  type: 'doc',
  content: [{ type: 'paragraph', content: [{ type: 'text', text: '中文创作' }] }],
});
assert.equal(doc.textContent, '中文创作');
const markup = renderToStaticMarkup(createElement('main', null, doc.textContent));
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.setContent(markup);
  assert.equal(await page.locator('main').textContent(), '中文创作');
  console.log('PASS: React render, Tiptap document schema, Playwright Chromium');
} finally {
  await browser.close();
}
