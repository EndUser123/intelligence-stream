#!/usr/bin/env node
/**
 * NotebookLM browser automation via Puppeteer.
 *
 * Uses the user's actual Chrome profile directly — existing cookies and
 * session are reused, bypassing Google's OAuth "insecure browser" block.
 *
 * Usage:
 *   node nlm-puppeteer.js                    # Test workflow (default)
 *   node nlm-puppeteer.js --list             # List all notebooks
 *   node nlm-puppeteer.js --delete-worker    # Delete worker notebooks
 *
 * Requires:
 *   npm install puppeteer
 *
 * Note: Chrome must be fully closed before running (profile is locked while Chrome is open).
 */

const puppeteer = require('puppeteer');

const CHROME_PATH = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const CHROME_PROFILE = 'C:\\Users\\brsth\\AppData\\Local\\Google\\Chrome\\User Data\\Default';

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function waitForLogin(page, timeout = 120) {
  console.log('[auth] Waiting for login...');
  const start = Date.now();
  while (Date.now() - start < timeout * 1000) {
    const url = page.url();
    if (!url.includes('accounts.google.com') && !url.toLowerCase().includes('signin')) {
      console.log(`[auth] Logged in! URL: ${url}`);
      return true;
    }
    await sleep(2000);
  }
  return false;
}

async function ensureNotebooklm(page) {
  await page.goto('https://notebooklm.google.com', { waitUntil: 'networkidle2' });
  await sleep(2000);

  const url = page.url();
  if (url.includes('accounts.google.com') || url.toLowerCase().includes('signin')) {
    const ok = await waitForLogin(page);
    if (!ok) {
      console.log('[auth] Timeout waiting for login.');
      return false;
    }
    await sleep(2000);
  }
  console.log(`[ui] On: ${page.url()}`);
  return true;
}

async function getNotebooks(page) {
  // Wait for notebook list to load
  await sleep(3000);

  // Try JS evaluation first
  let notebooks = [];
  try {
    notebooks = await page.evaluate(() => {
      const cards = document.querySelectorAll('[data-testid*="notebook"], [role="listitem"]');
      return Array.from(cards).map(el => {
        const titleEl = el.querySelector('h2, h3, [role="heading"]');
        const linkEl = el.querySelector('a[href*="notebook"]');
        return {
          title: titleEl ? titleEl.innerText.trim() : '',
          href: linkEl ? linkEl.href : ''
        };
      }).filter(n => n.title || n.href);
    });
  } catch (e) {}

  if (notebooks.length > 0) {
    return notebooks;
  }

  // Fallback: look for sidebar notebooks
  const items = await page.$$('[aria-label*="notebook"], [data-testid*="notebook"]');
  const result = [];
  for (const item of items) {
    try {
      const title = await item.$eval('h2, h3, span', el => el.innerText.trim()).catch(() => '');
      const href = await item.$eval('a', el => el.href).catch(() => '');
      if (title || href) result.push({ title, href });
    } catch (e) {}
  }
  return result;
}

async function clickDeleteNotebook(page, card) {
  try {
    // Find menu button (three dots or "More" button)
    const menuBtn = await card.$('button[aria-label*="More"], button[aria-label*="menu"], [aria-label*="Delete"]');
    if (menuBtn) {
      await menuBtn.click();
      await sleep(500);
    }

    // Find delete option
    const deleteBtn = await page.$('[role="menuitem"]:has-text("Delete"), [aria-label*="Delete permanently"]');
    if (deleteBtn) {
      await deleteBtn.click();
      await sleep(1000);
      // Confirm in dialog
      const confirmBtn = await page.$('[role="alertdialog"] button:has-text("Delete")');
      if (confirmBtn) {
        await confirmBtn.click();
        await sleep(500);
      }
      return true;
    }
  } catch (e) {
    console.log(`  [warn] Delete error: ${e.message}`);
  }
  return false;
}

async function cmdList(page) {
  if (!await ensureNotebooklm(page)) return;

  console.log('\n=== Notebooks ===');
  const notebooks = await getNotebooks(page);
  if (notebooks.length === 0) {
    console.log('  (none found)');
    return;
  }
  for (const nb of notebooks) {
    const title = nb.title || '(untitled)';
    console.log(`  ${title.padEnd(50)}  ${nb.href}`);
  }
}

async function cmdDeleteWorker(page) {
  if (!await ensureNotebooklm(page)) return;

  console.log('\n[info] Looking for worker notebooks...');
  await sleep(3000);

  // Get all notebook cards
  const cards = await page.$$('[data-testid*="notebook"]');
  console.log(`  Found ${cards.length} notebook cards`);

  let deleted = 0, skipped = 0;

  for (const card of cards) {
    try {
      const titleEl = await card.$('h2, h3, [role="heading"]');
      if (!titleEl) continue;
      const title = await titleEl.innerText();

      if (!title.toLowerCase().includes('worker')) continue;

      console.log(`  Deleting: ${title}`);
      const ok = await clickDeleteNotebook(page, card);
      if (ok) {
        console.log('    ✓ deleted');
        deleted++;
      } else {
        console.log('    ✗ skipped');
        skipped++;
      }
    } catch (e) {
      console.log(`    ✗ error: ${e.message}`);
      skipped++;
    }
  }

  console.log(`\nDone: ${deleted} deleted, ${skipped} skipped`);
}

async function cmdTest(page) {
  if (!await ensureNotebooklm(page)) return;

  console.log('\n=== Test Workflow ===');

  // Create new notebook
  console.log('\n[1/5] Creating test notebook...');
  const newBtn = await page.$('[aria-label*="new notebook"], [aria-label*="New notebook"], button:has-text("New notebook")');
  if (!newBtn) {
    // Try finding in sidebar
    const btns = await page.$$('button');
    for (const btn of btns) {
      const txt = await btn.innerText().catch(() => '');
      if (txt.toLowerCase().includes('new') && txt.toLowerCase().includes('notebook')) {
        await btn.click();
        await sleep(1000);
        console.log('  ✓ Clicked new notebook');
        break;
      }
    }
  } else {
    await newBtn.click();
    await sleep(1000);
    console.log('  ✓ Clicked new notebook');
  }

  await sleep(2000);
  console.log(`  URL: ${page.url()}`);

  // Add a source
  console.log('\n[2/5] Adding YouTube source...');
  const addBtn = await page.$('[aria-label*="Add source"], button:has-text("Add"), [data-testid*="add-source"]');
  if (addBtn) {
    await addBtn.click();
    await sleep(1000);
    console.log('  ✓ Clicked add source');
  }

  const urlInput = await page.$('input[type="url"], input[placeholder*="url"], input[aria-label*="url"]');
  if (urlInput) {
    await urlInput.fill('https://www.youtube.com/watch?v=dQw4w9WgXcQ');
    await sleep(500);
    const confirmBtn = await page.$('button:has-text("Add"), button:has-text("Continue")');
    if (confirmBtn) {
      await confirmBtn.click();
      await sleep(3000);
      console.log('  ✓ Added YouTube source');
    }
  }

  // Verify source was added
  console.log('\n[3/5] Verifying source...');
  await sleep(2000);
  const sources = await page.$$('[data-testid*="source"], .source-item');
  console.log(`  Found ${sources.length} sources`);

  // Delete sources
  console.log('\n[4/5] Deleting sources...');
  for (const src of sources) {
    try {
      const menu = await src.$('button:last-child, [aria-label*="More"], [aria-label*="menu"]');
      if (menu) {
        await menu.click();
        await sleep(300);
      }
      const delBtn = await src.$('[aria-label*="Delete"], [role="menuitem"]:has-text("Delete")');
      if (delBtn) {
        await delBtn.click();
        await sleep(500);
        console.log('  ✓ Deleted a source');
      }
      const confirm = await page.$('[role="alertdialog"] button:has-text("Delete")');
      if (confirm) {
        await confirm.click();
        await sleep(300);
      }
    } catch (e) {
      console.log(`  ⚠ Error: ${e.message}`);
    }
  }

  // Delete notebook
  console.log('\n[5/5] Deleting test notebook...');
  await page.goto('https://notebooklm.google.com');
  await sleep(2000);

  const cards = await page.$$('[data-testid*="notebook"]');
  for (const card of cards) {
    try {
      const titleEl = await card.$('h2, h3, [role="heading"]');
      if (!titleEl) continue;
      const title = await titleEl.innerText();
      if (title.toLowerCase().includes('test') || title.toLowerCase().includes('untitled') || !title) {
        console.log(`  Deleting: ${title || '(untitled)'}`);
        await clickDeleteNotebook(page, card);
        console.log('  ✓ Notebook deleted');
        break;
      }
    } catch (e) {
      console.log(`  ⚠ ${e.message}`);
    }
  }

  console.log('\n=== Test complete ===');
}

// Main
async function main() {
  const args = process.argv.slice(2);
  let cmd = 'test';
  if (args.includes('--list')) cmd = 'list';
  else if (args.includes('--delete-worker')) cmd = 'delete-worker';

  console.log('[info] Launching Chrome with user profile...');
  console.log(`       Profile: ${CHROME_PROFILE}`);
  console.log('       Note: Close all Chrome windows first!\n');

  let browser;
  try {
    browser = await puppeteer.launch({
      executablePath: CHROME_PATH,
      userDataDir: CHROME_PROFILE,
      headless: false,
      args: ['--disable-blink-images', '--disable-web-security']
    });
  } catch (e) {
    if (e.message.includes('EADDRINUSE') || e.message.includes('lock')) {
      console.log('[error] Chrome profile is locked. Close all Chrome windows and try again.');
      process.exit(1);
    }
    throw e;
  }

  const page = await browser.newPage();

  if (cmd === 'list') {
    await cmdList(page);
  } else if (cmd === 'delete-worker') {
    await cmdDeleteWorker(page);
  } else {
    await cmdTest(page);
  }

  console.log('\n[done] Browser open for inspection. Close it manually.');
  // await browser.close();
}

main().catch(e => {
  console.error('[error]', e.message);
  process.exit(1);
});