#!/usr/bin/env node
/**
 * NotebookLM browser automation via Chrome DevTools Protocol (CDP).
 *
 * Replicates the nlm-mcp-cli auth pattern:
 *   1. Launch Chrome with --remote-debugging-port and a fresh profile dir
 *   2. Connect via CDP WebSocket
 *   3. Wait for user to log in (one-time)
 *   4. Use CDP commands to automate NotebookLM UI
 *
 * Usage:
 *   node nlm-puppeteer.js                    # Test workflow (default)
 *   node nlm-puppeteer.js --list             # List all notebooks
 *   node nlm-puppeteer.js --delete-worker    # Delete worker notebooks
 *
 * Requires:
 *   npm install ws
 */

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const WebSocket = require('ws');

// Chrome paths
const CHROME_PATH = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PROFILE_DIR = path.join(os.homedir(), '.notebooklm-mcp-cli', 'chrome-profile');
const CDP_PORT = 9222;
const CDP_BASE = `http://127.0.0.1:${CDP_PORT}`;
const NOTEBOOKLM_URL = 'https://notebooklm.google.com';

// ---------------------------------------------------------------------------
// CDP WebSocket client — promise-based, properly async
// ---------------------------------------------------------------------------

let ws = null;
let wsUrl = null;
let msgId = 0;

// Pending requests: msgId -> { resolve, reject, timer }
const pending = {};

function wsSetup() {
  ws.on('message', (raw) => {
    // ws library may pass Buffer or string depending on Node version
    let resp;
    if (typeof raw === 'string') {
      resp = JSON.parse(raw);
    } else if (Buffer.isBuffer(raw) || raw instanceof ArrayBuffer || ArrayBuffer.isView(raw)) {
      const str = raw.toString();
      try { resp = JSON.parse(str); } catch { return; }
    } else {
      return;
    }
    // Route responses to the correct pending request
    if (resp.id !== undefined && pending[resp.id]) {
      const p = pending[resp.id];
      clearTimeout(p.timer);
      delete pending[resp.id];
      p.resolve(resp.result || {});
    }
    // CDP events (no id) are silently ignored
  });
}

function cdpSend(method, params = {}, timeout = 30000) {
  return new Promise((resolve, reject) => {
    const id = ++msgId;
    pending[id] = {
      resolve,
      reject,
      timer: setTimeout(() => {
        delete pending[id];
        reject(new Error(`CDP ${method} timed out`));
      }, timeout),
    };
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function cdpNavigate(url) {
  const host = url.split('/')[2];
  try {
    const current = await cdpGetUrl();
    if (current.includes(host)) {
      console.log('[debug] Already on', host);
      await sleep(2000);
      return;
    }
    console.log('[debug] Navigating from', current, 'to', url);
  } catch (e) {
    console.log('[debug] cdpGetUrl failed:', e.message);
  }
  try {
    await cdpSend('Page.navigate', { url });
    await sleep(5000);
  } catch (e) {
    console.log(`[debug] cdpNavigate nav: ${e.message}`);
  }
}

async function cdpGetUrl() {
  for (let i = 0; i < 5; i++) {
    try {
      await cdpSend('Runtime.enable');
      const r = await cdpSend('Runtime.evaluate', { expression: 'window.location.href' });
      return r && r.result ? (r.result.value || '') : '';
    } catch (e) {
      if (i < 4) {
        console.log(`[debug] cdpGetUrl attempt ${i+1} failed: ${e.message}, retrying...`);
        await sleep(2000);
      } else {
        throw e;
      }
    }
  }
  return '';
}

async function cdpEval(expression, timeout) {
  for (let i = 0; i < 3; i++) {
    try {
      // NOTE: Do NOT call Runtime.enable here. After OAuth redirect, Runtime.enable
      // appears to reset/restore the CDP context to a pre-login state (returning only
      // sidebar text instead of full page content). We already enable Runtime during
      // connectCDP() and after login redirect in cmdList. Call evaluate directly.
      const r = await cdpSend('Runtime.evaluate', { expression }, timeout || 30000);
      return r && r.result ? r.result.value : null;
    } catch (e) {
      if (i < 2) {
        console.log('[debug] cdpEval attempt ' + (i+1) + ' failed: ' + e.message + ', retrying...');
        await sleep(1000);
      }
    }
  }
  return null;
}

async function cdpWaitForLogin(timeout = 120) {
  console.log('[auth] Waiting for login...');
  const start = Date.now();
  let lastReconnectAttempt = 0;

  while (Date.now() - start < timeout * 1000) {
    // Try to get URL; if it fails or shows signin, try reconnecting
    const url = await cdpGetUrl().catch(() => '');
    if (url && !url.includes('accounts.google.com') && !url.toLowerCase().includes('signin')) {
      console.log(`[auth] Logged in! URL: ${url}`);
      await reconnectToNotebookLM();
      return true;
    }

    // Periodically check CDP list for actual NotebookLM page
    if (Date.now() - lastReconnectAttempt > 5000) {
      lastReconnectAttempt = Date.now();
      await tryReconnectToNotebookLM();
    }

    await sleep(2000);
  }
  return false;
}

async function tryReconnectToNotebookLM() {
  try {
    const res = await fetch(`${CDP_BASE}/json/list`);
    const pages = await res.json();
    const nlmPages = pages.filter(p =>
      p.type === 'page' &&
      p.url &&
      p.url.startsWith('https://notebooklm.google.com')
    );
    console.log(`[cdp] Found ${nlmPages.length} NotebookLM page(s), ${pages.length} total page targets`);

    for (const nlmPage of nlmPages) {
      if (nlmPage.webSocketDebuggerUrl === wsUrl) continue;

      console.log('[cdp] Found NotebookLM page, reconnecting...');
      ws.close();
      for (const id of Object.keys(pending)) {
        pending[id].reject(new Error('Connection replaced'));
        delete pending[id];
      }
      ws = new WebSocket(nlmPage.webSocketDebuggerUrl);
      await new Promise((r, re) => { ws.on('open', r); ws.on('error', re); });
      wsSetup();
      wsUrl = nlmPage.webSocketDebuggerUrl;

      // Re-enable CDP and wait for context to initialize
      await cdpSend('Page.enable', {}, 5000);
      await cdpSend('Runtime.enable', {}, 5000);
      await sleep(2000);

      // Verify it's actually the right page with document body
      try {
        const r = await cdpSend('Runtime.evaluate', {
          expression: 'window.location.href + "|" + document.body.children.length'
        }, 5000);
        const val = r && r.result ? r.result.value : '';
        console.log('[cdp] Reconnected to:', nlmPage.url, '|', val);
        return;
      } catch (e) {
        console.log('[debug]  Reconnect smoke test failed:', e.message);
        ws.close();
        ws = null;
      }
    }
  } catch (e) {
    console.log('[debug] tryReconnectToNotebookLM error:', e.message);
  }
}

async function reconnectToNotebookLM() {
  await tryReconnectToNotebookLM();
}

// ---------------------------------------------------------------------------
// Chrome process management
// ---------------------------------------------------------------------------

let chromeProcess = null;

function getChromeProfileDir() {
  const dir = PROFILE_DIR;
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function launchChrome() {
  // Kill any existing Chrome processes using this profile to avoid stale CDP sessions
  try {
    require('child_process').execSync('taskkill /F /IM chrome.exe', { stdio: 'ignore' });
  } catch {}

  const profileDir = getChromeProfileDir();
  const args = [
    `--remote-debugging-port=${CDP_PORT}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    `--user-data-dir=${profileDir}`,
    `--remote-allow-origins=http://127.0.0.1:${CDP_PORT}`,
    NOTEBOOKLM_URL,
  ];

  console.log(`[chrome] Launching: ${CHROME_PATH}`);
  console.log(`[chrome] Profile: ${profileDir}`);
  console.log('[chrome] Wait for Chrome to open, then log in manually...\n');

  chromeProcess = spawn(CHROME_PATH, args, {
    detached: true,
    stdio: 'ignore',
  });
  chromeProcess.unref();
}

async function tryConnectPage(wsUrl) {
  return new Promise((resolve) => {
    const testWs = new WebSocket(wsUrl);
    const timer = setTimeout(() => {
      testWs.close();
      resolve(false);
    }, 3000);
    testWs.on('open', () => {
      clearTimeout(timer);
      testWs.close();
      resolve(true);
    });
    testWs.on('error', () => {
      clearTimeout(timer);
      resolve(false);
    });
  });
}

async function connectCDP() {
  const res = await fetch(`${CDP_BASE}/json/list`);
  const pages = await res.json();

  // Find all NotebookLM pages
  const nlmPages = pages.filter(p =>
    p.type === 'page' &&
    p.url &&
    p.url.startsWith('https://notebooklm.google.com')
  );

  if (nlmPages.length === 0) {
    throw new Error('No NotebookLM pages found');
  }

  // Try each NotebookLM page until one responds to CDP
  for (const page of nlmPages) {
    console.log('[debug] Trying page:', page.url, page.webSocketDebuggerUrl.split('/').pop());
    const canConnect = await tryConnectPage(page.webSocketDebuggerUrl);
    if (!canConnect) {
      console.log('[debug]  WebSocket handshake failed, trying next...');
      continue;
    }

    // Try a quick CDP command to confirm it's alive
    wsUrl = page.webSocketDebuggerUrl;
    ws = new WebSocket(wsUrl);
    await new Promise((resolve, reject) => {
      ws.on('open', resolve);
      ws.on('error', reject);
    });
    wsSetup();

    try {
      // Enable page events first, then runtime, then wait for context
      await cdpSend('Page.enable', {}, 5000);
      await cdpSend('Runtime.enable', {}, 5000);
      await sleep(2000);
      // Quick smoke test
      const r = await cdpSend('Runtime.evaluate', { expression: 'document.title' }, 5000);
      const title = r && r.result ? r.result.value : '(no title)';
      console.log('[cdp] Connected to live NotebookLM page:', page.url, '| title:', title);
      return;
    } catch (e) {
      console.log('[debug]  CDP test failed:', e.message);
      ws.close();
      ws = null;
    }
  }

  // Fallback: use first page, let caller retry
  const target = nlmPages[0];
  wsUrl = target.webSocketDebuggerUrl;
  ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.on('open', resolve);
    ws.on('error', reject);
  });
  wsSetup();
  console.log('[cdp] Fallback: connected to', target.url);
}

// ---------------------------------------------------------------------------
// NotebookLM automation
// ---------------------------------------------------------------------------

async function nlGetNotebooks() {
  // Use innerText extraction via cdpSend (avoids Runtime.enable context reset issue)
  const snippet = await cdpSend('Runtime.evaluate', {
    expression: '(document.body.innerText || "").substring(0, 3000)'
  }, 15000).then(r => r && r.result ? r.result.value : null).catch(() => null);
  if (snippet && snippet.indexOf('Sources') >= 0) {
    const lines = snippet.split('\n').filter(l => l.trim());
    const seen = {};
    const names = [];
    for (let i = 0; i < lines.length - 1; i++) {
      const curr = lines[i].trim();
      const next = lines[i + 1] ? lines[i + 1].trim() : '';
      if (/^\d+\s+Sources/.test(next) && curr.length > 0 && curr.length < 100) {
        if (!seen[curr]) { seen[curr] = true; names.push(curr); }
      }
    }
    return names.map(t => ({ title: t, href: '' }));
  }
  return [];
}

async function nlDeleteNotebook(title) {
  await cdpNavigate(NOTEBOOKLM_URL);
  await sleep(3000);

  // Use tr[mat-row] + td[role=cell] selectors (verified against live NotebookLM HTML)
  // Runtime.evaluate returns the stringified JSON value; parse it to get the actual result
  const rawResult = await cdpEval(
    'JSON.stringify((function(){var rows=document.querySelectorAll("tr[mat-row]");for(var i=0;i<rows.length;i++){var tds=rows[i].querySelectorAll("td[role=cell]");if(tds.length>0&&(tds[0].innerText||"").includes(' + JSON.stringify(title) + ')){var menuBtn=rows[i].querySelector("button[aria-haspopup=menu]");if(menuBtn){menuBtn.click();return JSON.stringify({status:"clicked",label:menuBtn.getAttribute("aria-label")});}return JSON.stringify({status:"no-menu-btn",text:tds[0].innerText.slice(0,40)});}}return JSON.stringify({status:"not-found"});})())'
  );
  await sleep(1500);
  let result;
  try { result = JSON.parse(JSON.parse(rawResult)); } catch(e) { result = { status: 'parse-error', raw: rawResult }; }
  if (result.status !== 'clicked') { return false; }

  // Step 2: click delete menu item
  const rawStep2 = await cdpEval(
    'JSON.stringify((function(){var items=Array.from(document.querySelectorAll("[role=menuitem]"));var del=items.find(function(el){var t=el.textContent.trim();return t==="Delete"||t.endsWith(" Delete");});if(del){del.click();return JSON.stringify({status:"ok",count:items.length});}return JSON.stringify({status:"not-found",items:items.map(function(e){return e.textContent.trim();})});})())'
  );
  await sleep(1500);
  let step2;
  try { step2 = JSON.parse(JSON.parse(rawStep2)); } catch(e) { step2 = { status: 'parse-error', raw: rawStep2 }; }
  if (step2.status !== 'ok') return false;

  // Step 3: confirm in dialog
  const rawStep3 = await cdpEval(
    'JSON.stringify((function(){var dialog=document.querySelector("[role=dialog],dialog");if(!dialog)return JSON.stringify({status:"no-dialog"});var btns=Array.from(dialog.querySelectorAll("button"));var confirmBtn=btns.find(function(b){var t=b.textContent.trim();return(t==="Delete"||t.endsWith(" Delete"))&&!b.disabled;});if(confirmBtn){confirmBtn.click();return JSON.stringify({status:"confirmed"});}return JSON.stringify({status:"no-confirm",btns:btns.map(function(b){return b.textContent.trim();})});})())'
  );
  await sleep(1500);
  let step3;
  try { step3 = JSON.parse(JSON.parse(rawStep3)); } catch(e) { step3 = { status: 'parse-error', raw: rawStep3 }; }
  return step3.status === 'confirmed';
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------

async function cmdList() {
  await connectCDP();

  console.log('[auth] Navigating to NotebookLM...');
  await cdpNavigate(NOTEBOOKLM_URL);
  const loggedIn = await cdpWaitForLogin();
  if (!loggedIn) {
    console.log('[auth] Timeout waiting for login.');
    cleanup();
    return;
  }

  console.log('\n=== Notebooks ===');
  // After login redirect, the page may need to re-mount. Re-enable and wait.
  try {
    await cdpSend('Page.enable', {}, 5000);
    await cdpSend('Runtime.enable', {}, 5000);
    await sleep(3000);
  } catch (e) {
    console.log('[debug] Re-enable after login failed:', e.message);
  }
  const notebooks = await nlGetNotebooks();
  if (notebooks.length === 0) {
    console.log('  (none found)');
  } else {
    for (const nb of notebooks) {
      const title = nb.title || '(untitled)';
      console.log(`  ${title.padEnd(50)}  ${nb.href}`);
    }
  }
  cleanup();
}

async function cmdDeleteWorker() {
  await connectCDP();
  await cdpNavigate(NOTEBOOKLM_URL);
  const loggedIn = await cdpWaitForLogin();
  if (!loggedIn) {
    cleanup();
    return;
  }

  console.log('\n[info] Looking for worker notebooks...');
  const notebooks = await nlGetNotebooks();
  const workerNbs = notebooks.filter(n => n.title.toLowerCase().includes('worker'));
  console.log(`  Found ${workerNbs.length} worker notebook(s)`);

  let deleted = 0, skipped = 0;
  for (const nb of workerNbs) {
    console.log(`  Deleting: ${nb.title}`);
    const ok = await nlDeleteNotebook(nb.title);
    if (ok) { deleted++; console.log('    deleted'); }
    else { skipped++; console.log('    skipped'); }
  }

  console.log(`\nDone: ${deleted} deleted, ${skipped} skipped`);
  cleanup();
}

async function cmdDeleteTitle(title) {
  await connectCDP();
  await cdpNavigate(NOTEBOOKLM_URL);
  const loggedIn = await cdpWaitForLogin();
  if (!loggedIn) {
    cleanup();
    return;
  }

  console.log(`\n[info] Looking for notebook title: ${title}`);
  const notebooks = await nlGetNotebooks();
  const exactMatches = notebooks.filter(n => n.title === title);
  console.log(`  Found ${exactMatches.length} exact notebook(s)`);

  let deleted = 0, skipped = 0;
  for (const nb of exactMatches) {
    console.log(`  Deleting: ${nb.title}`);
    const ok = await nlDeleteNotebook(title);
    if (ok) { deleted++; console.log('    deleted'); }
    else { skipped++; console.log('    skipped'); }
  }

  console.log(`\nDone: ${deleted} deleted, ${skipped} skipped`);
  cleanup();
}

async function cmdTest() {
  await connectCDP();
  await cdpNavigate(NOTEBOOKLM_URL);
  const loggedIn = await cdpWaitForLogin();
  if (!loggedIn) {
    cleanup();
    return;
  }

  console.log('\n=== Test Workflow ===');

  // 1. Create a new notebook via the "Create new notebook" button
  console.log('\n[1/4] Creating new notebook...');
  const created = await cdpEval(
    'JSON.stringify((function(){var btn=document.querySelector("[aria-label=\\"Create new notebook\\"]");if(btn){btn.click();return "ok";}return "not-found";})())'
  );
  let createStatus;
  try { createStatus = JSON.parse(JSON.parse(created)).status; }
  catch(e) { createStatus = created; }
  console.log('  Create result:', createStatus);
  await sleep(2000);

  // Re-enable CDP context after page interaction
  await cdpSend('Page.enable', {}, 5000);
  await cdpSend('Runtime.enable', {}, 5000);
  await sleep(1000);

  // 2. Navigate back to the notebook list to find the newly created notebook
  console.log('\n[2/4] Navigating back to notebook list...');
  await cdpSend('Page.navigate', { url: NOTEBOOKLM_URL }, 15000);
  await sleep(3000);
  await cdpSend('Page.enable', {}, 5000);
  await cdpSend('Runtime.enable', {}, 5000);
  await sleep(2000);
  await cdpSend('Page.enable', {}, 5000);
  await cdpSend('Runtime.enable', {}, 5000);
  await sleep(2000);

  // Get notebook list and find the most recent (first in list)
  const snippet = await cdpSend('Runtime.evaluate', {
    expression: '(document.body.innerText || "").substring(0, 3000)'
  }, 15000).then(r => r && r.result ? r.result.value : null).catch(() => null);

  let testTitle = null;
  if (snippet && snippet.indexOf('Sources') >= 0) {
    const lines = snippet.split('\n').filter(l => l.trim());
    for (let i = 0; i < lines.length - 1; i++) {
      const curr = lines[i].trim();
      const next = lines[i + 1] ? lines[i + 1].trim() : '';
      if (/^\d+\s+Sources/.test(next) && curr.length > 0 && curr.length < 100) {
        testTitle = curr;
        break; // first match is the most recent (top of list)
      }
    }
  }
  if (!testTitle) {
    console.log('  Could not determine test notebook title, skipping delete');
    cleanup();
    return;
  }
  console.log('  Test notebook title:', testTitle);

  // 3. Delete it using the proven nlDeleteNotebook approach
  console.log('\n[3/4] Deleting test notebook...');
  const deleted = await nlDeleteNotebook(testTitle);
  console.log('  Deleted:', deleted);

  console.log('\n=== Test complete ===');
  cleanup();
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function cleanup() {
  if (ws) { ws.close(); ws = null; }
  if (chromeProcess) {
    try { process.kill(-chromeProcess.pid); } catch {}
    chromeProcess = null;
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const args = process.argv.slice(2);
  let cmd = 'test';
  let deleteTitle = '';
  if (args.includes('--list')) cmd = 'list';
  else if (args.includes('--delete-worker')) cmd = 'delete-worker';
  else if (args.includes('--delete-title')) {
    cmd = 'delete-title';
    deleteTitle = args[args.indexOf('--delete-title') + 1] || '';
  }

  console.log('[info] CDP-based NotebookLM automation');
  console.log(`       Profile: ${PROFILE_DIR}`);
  console.log('       Close Chrome windows first if you see errors.\n');

  try {
    launchChrome();
    await sleep(3000);

      if (cmd === 'list') await cmdList();
      else if (cmd === 'delete-worker') await cmdDeleteWorker();
      else if (cmd === 'delete-title') await cmdDeleteTitle(deleteTitle);
      else await cmdTest();

    console.log('\n[done]');
  } catch (e) {
    console.error('[error]', e.message);
    cleanup();
    process.exit(1);
  }
}

main();
