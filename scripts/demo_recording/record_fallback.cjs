// Records the fallback video of the full-trust demo from the RUNNING local application.
//
// 1. Runs `python -m scripts.demo_trust_journey` and keeps its real transcript.
// 2. Starts the full-trust stack (scripts.demo_recording.serve_stack); the agent is its own process.
// 3. Plays the customer in Chrome and records the browser with Playwright's video capture.
// 4. Shows two cards built only from this run's own data: the audit trail and the transcript.
//
// Needs Node.js and the pinned Playwright of this folder (package.json, package-lock.json):
//   cd scripts/demo_recording; npm ci; cd ../..
//   node scripts/demo_recording/record_fallback.cjs
// The browser is the installed Google Chrome if it exists (or CHROME_PATH). Without it, or with BROWSER=chromium,
// Playwright's own Chromium is used: `npx playwright install chromium` in this folder, once.
// Recording (RECORD not 0) also needs Playwright's ffmpeg: `npx playwright install ffmpeg`, once.
// Output (OUT_DIR, default docs/presentation/demo): TRUSTEQ_Demo_Recording_Source.webm and
// TRUSTEQ_Demo_Transcript.txt. Convert to MP4 with ffmpeg (see docs/presentation/demo/README.md).
const { createRequire } = require('module');
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const project = path.resolve(__dirname, '..', '..');
const outDir = path.resolve(process.env.OUT_DIR || path.join(project, 'docs', 'presentation', 'demo'));
const python = process.env.PYTHON || path.join(project, '.venv', 'Scripts', 'python.exe');
const defaultChrome = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
// undefined: Playwright's own Chromium (installed with `npx playwright install chromium`).
const chromePath = process.env.BROWSER === 'chromium' ? undefined
  : process.env.CHROME_PATH || (fs.existsSync(defaultChrome) ? defaultChrome : undefined);
// The pinned dependency of this folder; PLAYWRIGHT_MODULE_DIR is only an optional override.
const moduleDir = process.env.PLAYWRIGHT_MODULE_DIR || __dirname;
// LIVE use: RECORD=0 HEADLESS=0 TERMINAL_DEMO=0 shows the journey in a visible Chrome, records nothing.
const record = process.env.RECORD !== "0";
const headless = process.env.HEADLESS !== "0";
const terminalDemo = process.env.TERMINAL_DEMO !== "0";
const pace = Number(process.env.PACE || "1");

function loadChromium() {
  const req = createRequire(path.join(moduleDir, 'package.json'));
  for (const name of ['playwright', 'playwright-core', 'patchright']) {
    try { return req(name).chromium; } catch { /* try the next one */ }
  }
  throw new Error(`no playwright under ${moduleDir}: run "npm ci" in scripts/demo_recording`);
}

const escapeHtml = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
let failed = 0;
const check = (label, ok) => { console.log((ok ? 'PASS ' : 'FAIL ') + label); if (!ok) failed += 1; };
const stamp = () => new Date().toISOString().replace('T', ' ').slice(0, 16) + ' UTC';

function card(title, lines, { dark = false } = {}) {
  const bg = dark ? '#003366' : '#FFFFFF';
  const fg = dark ? '#FFFFFF' : '#003366';
  const body = lines.map((l) => `<p>${l}</p>`).join('');
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    body{margin:0;background:${bg};color:${fg};font-family:Calibri,Arial,sans-serif}
    main{padding:56px 72px} h1{font-size:40px;margin:0 0 24px} p{font-size:23px;margin:10px 0;color:${dark ? '#FFFFFF' : '#000000'}}
    .tag{display:inline-block;background:#AE9966;color:#fff;padding:2px 10px;font-size:18px}
    </style></head><body><main><h1>${title}</h1>${body}</main></body></html>`;
}

(async () => {
  fs.mkdirSync(outDir, { recursive: true });
  const env = { ...process.env, PYTHONPATH: project };
  for (const key of Object.keys(env)) if (key.startsWith('TRUSTEQ_') || key.startsWith('ANTHROPIC_')) delete env[key];

  console.log('== 0. The terminal demo, run for real; its transcript is shown at the end');
  const run = terminalDemo
    ? spawnSync(python, ["-m", "scripts.demo_trust_journey"], { cwd: project, env, encoding: "utf8", timeout: 240000 })
    : { status: null, stdout: "", stderr: "" };
  const transcript = (run.stdout || '') + (run.stderr || '');
  const transcriptAt = stamp();
  if (terminalDemo) fs.writeFileSync(path.join(outDir, "TRUSTEQ_Demo_Transcript.txt"), transcript);
  if (terminalDemo) check("Terminal demo exit code 0", run.status === 0);
  if (terminalDemo) check("Terminal demo says RESULT: PASS", transcript.includes("RESULT: PASS"));

  const stack = spawn(python, ['-m', 'scripts.demo_recording.serve_stack'], { cwd: project, env, stdio: ['pipe', 'pipe', 'ignore'] });
  const lines = readline.createInterface({ input: stack.stdout });
  const waiting = []; const queue = [];
  lines.on('line', (line) => { let item; try { item = JSON.parse(line); } catch { return; } if (waiting.length) waiting.shift()(item); else queue.push(item); });
  const next = () => new Promise((ok) => { if (queue.length) ok(queue.shift()); else waiting.push(ok); });
  const send = (value) => stack.stdin.write(JSON.stringify(value) + '\n');

  const up = await next();
  const chromium = loadChromium();
  const browser = await chromium.launch({ headless, executablePath: chromePath });
  const size = { width: 1280, height: 720 };
  const context = await browser.newContext(record ? { viewport: size, recordVideo: { dir: outDir, size } } : { viewport: size });
  const page = await context.newPage();
  const problems = []; const hosts = new Set();
  page.on('request', (r) => { if (r.url().startsWith('http')) hosts.add(new URL(r.url()).host); });
  page.on('pageerror', (e) => problems.push(String(e)));
  page.on('console', (m) => { if (m.type() === 'error' && !/status of 4\d\d/.test(m.text())) problems.push(m.text().slice(0, 140)); });
  const pause = (ms) => page.waitForTimeout(ms * pace);
  const click = async (text) => { await page.click(`button:has-text("${text}")`); await page.waitForLoadState(); };
  const type = async (selector, value) => { await page.fill(selector, ''); await page.type(selector, value, { delay: 45 }); };
  const show = async (selector) => { await page.locator(selector).scrollIntoViewIfNeeded(); await pause(1800); };

  await page.setContent(card('TRUSTEQ: agent-led depot opening, full-trust demo', [
    `Recorded ${stamp()} from the running local application.`,
    'An independent agent process asks; the customer decides in the bank\'s own pages; the bank\'s policy and the provider\'s signed callback decide.',
    '<span class="tag">SYNTHETIC</span> OIDC users, trust authority, identity proofing, test signer, mock depot provider. No real customer, eID, QES or bank.',
  ], { dark: true }));
  await pause(5500);

  console.log('\n== 1. The agent asks; the customer signs in at the separate identity provider');
  send({ do: 'agent' });
  const asked = await next();
  await page.goto(asked.customer_link);
  check('The bank sends the customer to the separate identity provider', page.url().startsWith(up.idp));
  await pause(1500);
  await type('input[name=username]', 'alice-synthetic');
  await type('input[name=password]', up.users['alice-synthetic']);
  await click('Sign in');
  check('Signed in, and the page says this is not identity proofing', (await page.innerText('body')).includes('It is not identity verification'));
  await pause(2200);
  await page.click('#continue');
  await page.waitForLoadState();

  console.log('\n== 2. The customer decides about the agent');
  let text = await page.innerText('body');
  check('The delegation page names the agent and what it cannot do', text.includes('Personal agent (demonstration)') && text.includes('cannot confirm or sign for you'));
  await pause(2500);
  await page.mouse.wheel(0, 500); await pause(2000);
  await click('Allow this agent');
  check('Decision recorded', (await page.title()) === 'Decision recorded');
  await pause(1500);

  send({ do: 'after_delegation' });
  const handoff = await next();
  console.log('\n== 3. The five steps, in the bank\'s customer channel');
  await page.goto(handoff.customer_link);
  check('No Confirm before identity, tax and answers exist', (await page.locator('button:has-text("Confirm and authorize")').count()) === 0);
  await pause(2500);
  await show('#identity-verification');
  const reviewUrl = page.url();
  await click('Start identity verification');
  await pause(1500);
  await page.click('#provider-link');
  await page.waitForLoadState();
  check('The test identity provider states it is not eID or VideoIdent', (await page.innerText('body')).includes('not eID, VideoIdent'));
  await pause(2200);
  await click('Simulate: verified');
  check('Signed proofing result accepted by the bank', (await page.innerText('body')).includes('Bank answered: 200'));
  await pause(1500);
  await page.goto(reviewUrl);
  await show('#identity-verification');
  await show('form[action$="/tax"]');
  await type('form[action$="/tax"] input[name=tax_residency_country]', 'DE');
  await type('form[action$="/tax"] input[name=synthetic_tax_id]', 'SYNTH-TAX-700421');
  await page.locator('form[action$="/tax"] input[name=us_person_declared][value=no]').check();
  await pause(800);
  await click('Check and save tax information');
  await show('form[action$="/assessment"]');
  await page.locator('form[action$="/assessment"] input[name=knows_capital_loss][value=true]').first().check();
  await page.locator('form[action$="/assessment"] input[name=knows_no_guarantee][value=false]').first().check();
  await page.locator('form[action$="/assessment"] input[name=experience][value=MORE_THAN_FIVE]').first().check();
  await pause(800);
  await click('Save my answers');
  check('Confirm is offered once the information exists', (await page.locator('button:has-text("Confirm and authorize")').count()) === 1);
  await show('button:has-text("Confirm and authorize")');
  await click('Confirm and authorize');
  check('Confirmation of the exact action recorded', (await page.title()) === 'Confirmation recorded');
  await pause(1800);

  console.log('\n== 4. The contract package and the test signature');
  await page.click('#contract-link');
  await page.waitForLoadState();
  text = await page.innerText('body');
  check('Contract page shows the package hash and says no legal effect, no QES', text.includes('Package hash (SHA-256)') && text.includes('None. This is a synthetic package') && text.includes('no qualified electronic signature'));
  await pause(2500);
  await page.mouse.wheel(0, 450); await pause(2200);
  await page.mouse.wheel(0, 450); await pause(2000);
  const contractUrl = page.url();
  const applicationId = contractUrl.match(/applications\/([^/]+)\/contract/)[1];
  await click('Authorize signing');
  await pause(1500);
  await page.click('#provider-link');
  await page.waitForLoadState();
  check('The test signer states it creates no qualified signature', (await page.innerText('body')).includes('No qualified or legally valid'));
  await pause(2000);
  await click('Sign (test signature)');
  check('Signed result accepted by the bank', (await page.innerText('body')).includes('Bank answered: 200'));
  await pause(1200);
  await page.goto(contractUrl);
  check('The bank shows the package as signed by the test signer', (await page.innerText('body')).includes('signed by the test signer'));
  await pause(2200);

  console.log('\n== 5. The agent submits; only the provider\'s signed callback opens the depot');
  send({ do: 'after_signature', application_id: applicationId });
  const final = await next();
  const audit = await next();
  check('Provider callback accepted', final.provider === 200);
  check('The agent reads OPENED', final.state === 'OPENED');
  check('The audit trail ends with the provider confirmation', audit.events.length > 0 && audit.events[audit.events.length - 1].event === 'provider.confirmation_accepted');
  await page.goto(new URL('/customer/delegations', up.bank).href);
  await pause(2500);

  const rows = audit.events.map((e, i) => `${String(i + 1).padStart(2, ' ')}  ${e.event.padEnd(42, ' ')} ${e.actor.padEnd(9, ' ')} ${e.outcome}`);
  await page.setContent(`<!doctype html><html><head><meta charset="utf-8"><style>
    body{margin:0;font-family:Calibri,Arial,sans-serif;color:#000} main{padding:28px 48px}
    h1{color:#003366;font-size:30px;margin:0 0 6px} p{font-size:19px;margin:4px 0 12px}
    pre{font-family:Consolas,monospace;font-size:14.5px;line-height:1.28;margin:0;columns:2;column-gap:36px}
    .ok{color:#003366;font-weight:bold}</style></head><body><main>
    <h1>Result of this run: <span class="ok">${escapeHtml(final.state)}</span></h1>
    <p>Application ${escapeHtml(final.application_id)}. Provider callback answered ${final.provider}.
    The audit trail below is read from this run's bank database (trusted operator view), in order.</p>
    <pre>${escapeHtml(rows.join('\n'))}</pre></main></body></html>`);
  await pause(9000);

  if (terminalDemo) {
    const marked = escapeHtml(transcript).split('\n').map((l) => (
      l.startsWith('==') || l.startsWith('RESULT') ? `<b class="h">${l}</b>`
        : /refused|REJECT|MANUAL_REVIEW|untrusted|bad_signature/.test(l) ? `<span class="n">${l}</span>` : l)).join('\n');
    await page.setContent(`<!doctype html><html><head><meta charset="utf-8"><style>
      body{margin:0;font-family:Calibri,Arial,sans-serif} header{position:sticky;top:0;background:#003366;color:#fff;padding:14px 40px;font-size:20px}
      pre{font-family:Consolas,monospace;font-size:13.5px;line-height:1.3;padding:16px 40px;margin:0;white-space:pre-wrap}
      .h{color:#003366} .n{color:#8A6D2F;font-weight:bold}</style></head><body>
      <header>Transcript of <code>python -m scripts.demo_trust_journey</code>, run by this recording at ${transcriptAt}, exit code ${run.status}: the journey and the negative cases</header>
      <pre>${marked}</pre></body></html>`);
    await pause(2500);
    const height = await page.evaluate(() => document.body.scrollHeight);
    for (let y = 0; y < height; y += 260) { await page.evaluate((v) => window.scrollTo(0, v), y); await pause(1100); }
    await pause(2500);
  }

  console.log('\n== Totals');
  console.log('Hosts requested'.padEnd(40), JSON.stringify([...hosts]));
  check('Only local hosts', [...hosts].every((h) => h.startsWith('127.0.0.1')));
  check('No unexpected console or page errors', problems.length === 0);
  const video = record ? page.video() : null;
  await context.close();
  await browser.close();
  if (video) {
    const target = path.join(outDir, 'TRUSTEQ_Demo_Recording_Source.webm');
    fs.renameSync(await video.path(), target);
    console.log('video'.padEnd(40), target);
  }
  send({ do: 'stop' });
  console.log(failed ? `\n${failed} CHECK(S) FAILED` : '\nALL CHECKS PASSED');
  setTimeout(() => process.exit(failed ? 1 : 0), 2500);
})().catch((e) => { console.error('RECORDER FAILED', e); setTimeout(() => process.exit(1), 2000); });
