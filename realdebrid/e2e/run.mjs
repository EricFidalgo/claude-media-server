// The plugin loads, only admins get through, and the whole job works on a phone-sized screen.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, statSync, writeFileSync } from 'node:fs';
import { chromium, devices } from 'playwright-core';

const URL_ = 'http://127.0.0.1:18096';
const AUTH = 'MediaBrowser Client="e2e", Device="e2e", DeviceId="e2e", Version="1"';
const MB = 1024 * 1024;

export async function http(method, path, { token, body, raw } = {}) {
    const res = await fetch(URL_ + path, {
        method,
        headers: { Authorization: AUTH + (token ? `, Token="${token}"` : ''), 'Content-Type': 'application/json' },
        body: raw ?? (body === undefined ? undefined : JSON.stringify(body)),
    });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch { /* not JSON */ }
    return { status: res.status, json, text };
}

async function waitUp() {
    // While it migrates and starts, Jellyfin answers everything with a 503 status page.
    for (let i = 0; i < 180; i++) {
        try {
            const r = await fetch(URL_ + '/Startup/User', { headers: { Authorization: AUTH } });
            if (r.status !== 503) return;
        } catch { /* still starting */ }
        await new Promise((r) => setTimeout(r, 1000));
    }
    throw new Error('Jellyfin never came up');
}

async function setUp() {
    await waitUp();
    await http('POST', '/Startup/Configuration', { body: { UICulture: 'en-US', MetadataCountryCode: 'US', PreferredMetadataLanguage: 'en' } });
    await http('GET', '/Startup/User');
    const u = await http('POST', '/Startup/User', { body: { Name: 'admin', Password: 'e2e-password' } });
    assert.ok(u.status < 300, 'set the first user: ' + u.status + ' ' + u.text.slice(0, 200));
    const done = await http('POST', '/Startup/Complete');
    assert.ok(done.status < 300, 'finish the wizard: ' + done.status);
    const login = await http('POST', '/Users/AuthenticateByName', { body: { Username: 'admin', Pw: 'e2e-password' } });
    assert.equal(login.status, 200, 'admin login');
    return login.json.AccessToken;
}

// The same way a library gets made on a new server: on an empty folder. Jellyfin's own watching is off, so only the plugin's scan can bring films in.
async function makeLibraries(admin) {
    for (const [name, type, path] of [['Movies', 'movies', '/media/movies'], ['Shows', 'tvshows', '/media/shows']]) {
        const made = await http('POST', `/Library/VirtualFolders?name=${name}&collectionType=${type}&paths=${path}&refreshLibrary=false`, { token: admin, body: { LibraryOptions: { EnableRealtimeMonitor: false } } });
        assert.ok(made.status < 300, 'make a library: ' + made.status);
    }
}

export async function accessChecks(admin) {
    const made = await http('POST', '/Users/New', { token: admin, body: { Name: 'guest', Password: 'guest-password' } });
    assert.equal(made.status, 200, 'make a non-admin');
    const guest = (await http('POST', '/Users/AuthenticateByName', { body: { Username: 'guest', Pw: 'guest-password' } })).json.AccessToken;

    const plugins = await http('GET', '/Plugins', { token: admin });
    const mine = plugins.json.find((p) => p.Name === 'Real-Debrid');
    assert.ok(mine, 'plugin is listed: ' + plugins.text);
    assert.equal(mine.Status, 'Active');

    for (const path of ['/RealDebrid/api/overview', '/RealDebrid/api/torrents', '/RealDebrid/api/queue']) {
        assert.equal((await http('GET', path)).status, 401, 'no login: ' + path);
        assert.equal((await http('GET', path, { token: guest })).status, 403, 'non-admin: ' + path);
    }
    assert.equal((await http('POST', '/RealDebrid/api/add', { body: { link: 'magnet:?x' } })).status, 401, 'no login, writing');
    assert.equal((await http('POST', '/RealDebrid/api/add', { token: guest, body: { link: 'magnet:?x' } })).status, 403, 'non-admin, writing');
    assert.equal((await http('DELETE', '/RealDebrid/api/torrents/FILM1', { token: guest })).status, 403, 'non-admin, deleting');

    const overview = await http('GET', '/RealDebrid/api/overview', { token: admin });
    assert.equal(overview.status, 200, 'admin: ' + overview.text);
    assert.equal(overview.json.account.username, 'e2e');
    assert.equal((await http('GET', '/RealDebrid/api/..%2f..%2fuser', { token: admin })).status, 400, 'odd paths stay out');
    const home = await fetch(URL_ + '/web/');
    assert.ok((await home.text()).includes('RealDebrid/menu.js'), 'home page carries the menu script');
    assert.equal((await fetch(URL_ + '/RealDebrid/menu.js')).status, 200);

    // The captions page and service have the same locks
    assert.equal((await http('GET', '/Captions/api/videos')).status, 401, 'captions, no login');
    assert.equal((await http('GET', '/Captions/api/videos', { token: guest })).status, 403, 'captions, non-admin');
    assert.equal((await http('POST', '/Captions/api/videos/x/remove', { token: guest, body: { captions: ['y'] } })).status, 403, 'captions, non-admin removing');
    assert.equal((await http('GET', '/Captions/api/videos', { token: admin })).status, 200, 'captions, admin');
    assert.equal((await http('GET', '/Captions/api/..%2f..%2fuser', { token: admin })).status, 400, 'captions, odd paths stay out');

    // The page loads before login, so it mustn't hold anything private, and it only talks to this server
    const app = await fetch(URL_ + '/RealDebrid/app');
    assert.equal(app.status, 200, 'the page');
    assert.match(app.headers.get('content-type'), /text\/html/);
    assert.match(app.headers.get('content-security-policy') ?? '', /connect-src 'self'/);
    assert.ok(!(await app.text()).includes('FILM1'), 'no account data in the page itself');
    console.log('access checks passed');
    return guest;
}

const LIB = process.env.E2E_LIBRARY;
const SHOTS = new URL('./screenshots/', import.meta.url).pathname;
mkdirSync(SHOTS, { recursive: true });

async function untilFile(path, bytes) {
    for (let i = 0; i < 100; i++) {
        if (existsSync(path) && statSync(path).size === bytes) return;
        await new Promise((r) => setTimeout(r, 200));
    }
    assert.fail('never arrived: ' + path);
}

// Nobody touches a refresh button here: the plugin's scan has to bring them in.
async function untilInJellyfin(admin, type, path) {
    let paths = [];
    for (let i = 0; i < 60; i++) {
        paths = ((await http('GET', `/Items?Recursive=true&IncludeItemTypes=${type}&Fields=Path`, { token: admin })).json?.Items ?? []).map((m) => m.Path);
        if (paths.includes(path)) return paths;
        await new Promise((r) => setTimeout(r, 1000));
    }
    assert.fail(`${path} never appeared in Jellyfin, which has: ${paths}`);
}

async function logIn(context, name, password) {
    const page = await context.newPage();
    page.on('pageerror', (e) => { problems.push(name + ': ' + e.message); });
    await page.goto(URL_ + '/web/');
    await page.waitForSelector('#txtManualName, .btnManual, .manualLoginForm', { timeout: 30000 });
    if (!(await page.locator('#txtManualName').isVisible())) await page.locator('.btnManual').click();
    await page.fill('#txtManualName', name);
    await page.fill('#txtManualPassword', password);
    await page.keyboard.press('Enter');
    await page.getByRole('button', { name: 'User Menu' }).waitFor({ timeout: 30000 });
    await page.waitForTimeout(500);
    return page;
}

async function noSideScroll(page, where) {
    const fits = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
    assert.ok(fits, 'sideways scrolling on ' + where);
}

const problems = [];
const admin = await setUp();
await makeLibraries(admin);
await accessChecks(admin);

const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true });
try {
    const phone = devices['iPhone 14'];
    const { defaultBrowserType, ...phoneOptions } = phone;
    const context = await browser.newContext({ ...phoneOptions, colorScheme: 'dark' });
    const page = await logIn(context, 'admin', 'e2e-password');
    await page.screenshot({ path: SHOTS + '01-home.png' });

    const row = (id) => page.locator(`[data-torrent="${id}"]`);
    const rows = () => page.locator('[data-torrent]');
    const search = page.getByRole('searchbox');
    const untilRows = (n) => page.waitForFunction((want) => document.querySelectorAll('[data-torrent]').length === want, n);

    // The side menu has the entry, and it leaves Jellyfin's menus behind altogether
    await page.getByRole('button', { name: 'Open Menu' }).click();
    const inDrawer = page.locator('.MuiDrawer-root a.realDebridLink');
    await inDrawer.waitFor({ state: 'visible', timeout: 10000 });
    assert.equal(await inDrawer.innerText(), 'Real-Debrid');
    await page.waitForTimeout(400);
    await page.screenshot({ path: SHOTS + '02-menu.png' });
    await inDrawer.click();
    await page.waitForURL('**/RealDebrid/app');
    await row('FILM1').waitFor({ timeout: 20000 });
    assert.equal(await page.locator('.MuiDrawer-root').count(), 0, 'no Jellyfin side menu here');
    assert.equal(await page.getByRole('button', { name: 'Open Menu' }).count(), 0, 'no menu button here');
    await page.screenshot({ path: SHOTS + '03-page.png' });
    await noSideScroll(page, 'the page');

    // Going back lands on the plain home page, whose menu has no admin pages in it
    await page.getByRole('link', { name: /Jellyfin/ }).click();
    await page.waitForURL('**/web/#/home');
    await page.getByRole('button', { name: 'Open Menu' }).click();
    await page.locator('.MuiDrawer-root a.realDebridLink').waitFor({ state: 'visible', timeout: 10000 });
    assert.equal(await page.locator('.MuiDrawer-root', { hasText: 'Branding' }).count(), 0, 'the admin menu stayed behind');

    // The user menu reaches the same page
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: 'User Menu' }).click();
    const inUserMenu = page.locator('.MuiMenu-list a.realDebridLink');
    await inUserMenu.waitFor({ state: 'visible', timeout: 10000 });
    await page.screenshot({ path: SHOTS + '04-user-menu.png' });
    await inUserMenu.click();
    await page.waitForURL('**/RealDebrid/app');
    await row('FILM1').waitFor({ timeout: 20000 });

    // Only what can be downloaded right now, and nothing else on the page to get lost in
    for (const id of ['CACHE1', 'PICK1', 'FAIL1']) assert.equal(await row(id).count(), 0, id + ' is not ready, so not listed');
    const whole = await page.locator('body').innerText();
    for (const gone of ['Add', 'Points', 'Hosts', 'Links', 'days left']) assert.ok(!new RegExp(`\\b${gone}\\b`).test(whole), 'still shows ' + gone);

    // The two-line names
    assert.equal(await row('FILM1').locator('.name').innerText(), 'Buddy (2026) WEB-DL [🇺🇸]');
    assert.equal(await row('FILM1').locator('.desc').innerText(), '1080p [6MB]');
    assert.equal(await row('SPAN1').locator('.name').innerText(), 'Pelicula Buena (2021) BluRay [🇪🇸]');
    assert.equal(await row('SPAN1').locator('.desc').innerText(), '1080p [5MB]');

    // A hostile name stays plain text
    assert.match(await row('EVIL1').locator('.name').innerText(), /<img src=x onerror=xss=1>/i);
    assert.equal(await page.evaluate(() => window.xss), undefined, 'a name ran as code');
    assert.equal(await page.locator('img[src="x"]').count(), 0);

    // A long list comes in chunks, newest first, and search reaches past the first chunk
    assert.equal(await rows().count(), 50);
    assert.equal(await rows().first().getAttribute('data-torrent'), 'EVIL1');
    assert.equal(await row('OLDEST').count(), 0, 'the oldest is further down');
    assert.match(await search.getAttribute('placeholder'), /65 videos/);
    await search.fill('oldest');
    await row('OLDEST').waitFor();
    await search.fill('');
    await page.getByRole('button', { name: 'Show more' }).click();
    await untilRows(65);
    assert.equal(await page.getByRole('button', { name: 'Show more' }).isVisible(), false, 'nothing left to show');

    // Search: any words in any order, with or without accents, on the file's name too
    await search.fill('buddy');
    await untilRows(1);
    await search.fill('lasso ted');
    await untilRows(1);
    await row('PACK1').waitFor();
    await search.fill('Película');
    await untilRows(1);
    await row('SPAN1').waitFor();
    await search.fill('zzz nothing');
    await page.getByText('Nothing matches').waitFor();

    // Typing stays put while things refresh behind it (a replaced box would close the phone's keyboard)
    await search.focus();
    await page.keyboard.type('q');
    await page.waitForTimeout(4500);
    assert.equal(await page.evaluate(() => document.activeElement?.type), 'search', 'the box kept its focus');
    assert.equal(await search.inputValue(), 'zzz nothingq');
    await search.fill('');
    await untilRows(50);

    // A film: one tap, the right place, and Jellyfin shows it without anyone asking
    await row('FILM1').getByRole('button', { name: 'Download' }).click();
    await untilFile(LIB + '/movies/Buddy (2026)/Buddy (2026).mkv', 6 * MB);
    await row('FILM1').getByText('On server').waitFor({ timeout: 15000 });
    assert.equal(await row('FILM1').getByRole('button').count(), 0, 'nothing left to tap');
    await untilInJellyfin(admin, 'Movie', '/media/movies/Buddy (2026)/Buddy (2026).mkv');

    // A season pack: pick some episodes, and the rest stay one tap away
    await row('PACK1').getByRole('button', { name: 'Download' }).click();
    const sheet = page.getByRole('dialog');
    await sheet.waitFor();
    assert.ok(!(await sheet.innerText()).includes('notes.nfo'), 'only videos are offered');
    assert.equal(await sheet.locator('input[type=checkbox]:checked').count(), 2, 'both episodes start ticked');
    await page.screenshot({ path: SHOTS + '05-pack.png' });
    await noSideScroll(page, 'the file sheet');
    await sheet.getByRole('button', { name: 'None' }).click();
    assert.equal(await sheet.getByRole('button', { name: 'Download 0' }).isDisabled(), true);
    await sheet.getByRole('button', { name: 'All' }).click();
    await sheet.getByRole('button', { name: 'Download 2' }).waitFor();
    await sheet.locator('[data-file="3"] input[type=checkbox]').uncheck();
    await sheet.getByRole('button', { name: 'Download 1' }).click();
    await sheet.waitFor({ state: 'detached' });
    await untilFile(LIB + '/shows/Ted Lasso/Season 04/Ted Lasso S04E01.mkv', 4 * MB);
    await row('PACK1').getByRole('button', { name: '1 more' }).click();
    await untilFile(LIB + '/shows/Ted Lasso/Season 04/Ted Lasso S04E02.mkv', 4 * MB);
    await row('PACK1').getByText('On server').waitFor({ timeout: 15000 });
    await untilInJellyfin(admin, 'Episode', '/media/shows/Ted Lasso/Season 04/Ted Lasso S04E02.mkv');

    // Files on their way in stay hidden from Jellyfin, even through a scan
    mkdirSync(LIB + '/movies/.saving', { recursive: true });
    writeFileSync(LIB + '/movies/.saving/Stray Film (2020).mkv', 'not ready');
    writeFileSync(LIB + '/movies/.saving/abc123.part', 'not ready');
    await row('SPAN1').getByRole('button', { name: 'Download' }).click();
    await untilFile(LIB + '/movies/Pelicula Buena (2021)/Pelicula Buena (2021).mkv', 5 * MB);
    const films = await untilInJellyfin(admin, 'Movie', '/media/movies/Pelicula Buena (2021)/Pelicula Buena (2021).mkv');
    assert.ok(!films.some((p) => /stray|abc123/i.test(p)), 'files on their way in stay hidden: ' + films);

    // A download Real-Debrid refuses can be tried again
    const job = (name) => page.locator('[data-job]', { hasText: name });
    await row('RETRY1').getByRole('button', { name: 'Download' }).click();
    await job('Retry Film').getByText('Failed').waitFor({ timeout: 15000 });
    await row('RETRY1').getByText('Failed').waitFor();
    await page.screenshot({ path: SHOTS + '06-failed.png' });
    await noSideScroll(page, 'a failed download');
    await job('Retry Film').getByRole('button', { name: 'Try again' }).click();
    await job('Retry Film').waitFor({ state: 'detached', timeout: 15000 });
    await untilFile(LIB + '/movies/Retry Film (2022)/Retry Film (2022).mkv', 2 * MB);
    await row('RETRY1').getByText('On server').waitFor({ timeout: 15000 });

    // ...or cancelled, which takes two taps, and the name stays text there too
    await row('EVIL1').getByRole('button', { name: 'Download' }).click();
    const evil = page.locator('[data-job]', { hasText: 'onerror' });
    await evil.getByText('Failed').waitFor({ timeout: 15000 });
    await evil.getByRole('button', { name: 'Cancel' }).click();
    assert.equal(await evil.count(), 1, 'one tap cancels nothing');
    await evil.getByRole('button', { name: 'Tap again to confirm' }).click();
    await evil.waitFor({ state: 'detached' });
    await row('EVIL1').getByRole('button', { name: 'Download' }).waitFor();
    assert.equal(await page.evaluate(() => window.xss), undefined, 'a name ran as code');
    assert.equal(await page.locator('img[src="x"]').count(), 0);
    await page.screenshot({ path: SHOTS + '07-done.png' });

    assert.ok(existsSync(LIB + '/movies/Buddy (2026)/Buddy (2026).mkv'), 'the library is untouched by any of that');

    // Captions: a real video with three built-in captions, and Jellyfin's own list follows each change by itself
    const captionFilm = LIB + '/movies/Caption Film (2020)/Caption Film (2020).mkv';
    mkdirSync(LIB + '/movies/Caption Film (2020)', { recursive: true });
    const srt = LIB + '/hello.srt';
    writeFileSync(srt, '1\n00:00:00,000 --> 00:00:01,000\nHello\n');
    execFileSync('ffmpeg', ['-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=d=2:s=64x64:r=5', '-f', 'lavfi', '-i', 'sine=d=2',
        '-i', srt, '-i', srt, '-i', srt, '-map', '0', '-map', '1', '-map', '2', '-map', '3', '-map', '4',
        '-metadata:s:s:0', 'language=eng', '-metadata:s:s:1', 'language=spa', '-metadata:s:s:2', 'language=rus',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-c:s', 'srt', captionFilm]);
    const filmSubtitles = async () => {
        const item = (await http('GET', '/Items?Recursive=true&IncludeItemTypes=Movie&Fields=Path,MediaStreams&SearchTerm=Caption', { token: admin })).json?.Items?.[0];
        return (item?.MediaStreams ?? []).filter((m) => m.Type === 'Subtitle').map((m) => m.Language).sort().join(',');
    };
    const untilSubtitles = async (want) => {
        let have = null;
        for (let i = 0; i < 60; i++) {
            have = await filmSubtitles();
            if (have === want) return;
            await new Promise((r) => setTimeout(r, 1000));
        }
        assert.fail(`Jellyfin shows captions "${have}", wanted "${want}"`);
    };
    await http('POST', '/Library/Refresh', { token: admin });
    await untilSubtitles('eng,rus,spa');

    await page.goto(URL_ + '/web/#/home');
    await page.getByRole('button', { name: 'Open Menu' }).click();
    const captionsEntry = page.locator('.MuiDrawer-root a.captionsLink');
    await captionsEntry.waitFor({ state: 'visible', timeout: 10000 });
    assert.equal(await captionsEntry.innerText(), 'Captions');
    await page.waitForTimeout(400);
    await page.screenshot({ path: SHOTS + '12-captions-menu.png' });
    await captionsEntry.click();
    await page.waitForURL('**/Captions/app');
    await page.getByRole('searchbox').fill('caption film');
    await page.getByRole('button', { name: /Caption Film/ }).click();
    const caption = (name) => page.locator('[data-caption]', { hasText: name });
    await caption('Spanish').waitFor({ timeout: 20000 });
    await page.screenshot({ path: SHOTS + '13-captions-video.png' });
    await noSideScroll(page, 'the captions page');

    // Tick two, tap twice, and the video keeps its picture, sound and third caption
    const removeButton = page.getByRole('button', { name: /Remove \d caption|Tap again/ });
    await caption('Spanish').locator('input').check();
    await caption('Russian').locator('input').check();
    await page.getByRole('button', { name: 'Remove 2 captions' }).click();
    await page.waitForTimeout(300);
    assert.equal(await caption('Spanish').count(), 1, 'one tap removes nothing');
    await page.getByRole('button', { name: 'Tap again to remove for good' }).click();
    await page.getByText('Removed.').waitFor({ timeout: 30000 });
    await caption('Spanish').waitFor({ state: 'detached' });
    assert.equal(await caption('English').count(), 1);
    assert.equal(await removeButton.count(), 0, 'nothing ticked any more');
    await untilSubtitles('eng');

    // Add one from the phone's files
    await page.getByLabel('Language of the new caption').selectOption('pt');
    await page.locator('input[type=file]').setInputFiles({ name: 'whatever.srt', mimeType: 'application/x-subrip', buffer: Buffer.from('1\n00:00:00,000 --> 00:00:01,000\nOla\n') });
    await page.getByText('Added.').waitFor({ timeout: 15000 });
    await caption('Portuguese').waitFor();
    assert.ok(existsSync(LIB + '/movies/Caption Film (2020)/Caption Film (2020).pt.srt'), 'saved beside the video');
    await untilSubtitles('eng,por');

    // A wrong file is turned away in plain words
    await page.locator('input[type=file]').setInputFiles({ name: 'list.srt', mimeType: 'application/x-subrip', buffer: Buffer.from('milk, eggs') });
    await page.getByText("doesn't look like a caption file").waitFor({ timeout: 15000 });
    await page.locator('input[type=file]').setInputFiles({ name: 'photo.jpg', mimeType: 'image/jpeg', buffer: Buffer.from('jpeg') });
    await page.getByText('Pick a .srt').waitFor();
    await page.screenshot({ path: SHOTS + '14-captions-after.png' });

    // And the added one comes off again without touching the video
    const videoBefore = statSync(captionFilm).mtimeMs;
    await caption('Portuguese').locator('input').check();
    await page.getByRole('button', { name: 'Remove 1 caption' }).click();
    await page.getByRole('button', { name: 'Tap again to remove for good' }).click();
    await caption('Portuguese').waitFor({ state: 'detached', timeout: 30000 });
    assert.equal(statSync(captionFilm).mtimeMs, videoBefore, 'the video was left alone');
    await untilSubtitles('eng');

    // A file too big to be captions is turned away before it's sent
    await page.locator('input[type=file]').setInputFiles({ name: 'huge.srt', mimeType: 'application/x-subrip', buffer: Buffer.alloc(7 * 1024 * 1024, 'a') });
    await page.getByText('too big to be a caption file').waitFor();

    // A removal that failed while the page was closed is explained on coming back
    const filmId = (await http('GET', '/Captions/api/videos', { token: admin })).json.find((v) => v.name === 'Caption Film (2020)').id;
    const doomed = (await http('POST', `/Captions/api/videos/${filmId}/remove`, { token: admin, body: { captions: ['gone'] } })).json;
    for (let i = 0; i < 40; i++) {
        const job = (await http('GET', '/Captions/api/jobs', { token: admin })).json.find((j) => j.id === doomed.id);
        if (job.status === 'failed') break;
        await new Promise((r) => setTimeout(r, 250));
    }
    await page.reload();
    await page.getByText('captions have changed').waitFor({ timeout: 15000 });

    // If the service restarts mid-job and forgets it, the page doesn't spin forever
    const addCaption = async (language, name) => {
        await page.getByLabel('Language of the new caption').selectOption(language);
        await page.locator('input[type=file]').setInputFiles({ name: 'more.srt', mimeType: 'application/x-subrip', buffer: Buffer.from('1\n00:00:00,000 --> 00:00:01,000\nHej\n') });
        await caption(name).waitFor({ timeout: 15000 });
    };
    await addCaption('sv', 'Swedish');
    await page.route('**/Captions/api/jobs', (route) => route.fulfill({ json: [] }));
    await caption('Swedish').locator('input').check();
    await page.getByRole('button', { name: 'Remove 1 caption' }).click();
    await page.getByRole('button', { name: 'Tap again to remove for good' }).click();
    await page.getByText('was interrupted').waitFor({ timeout: 15000 });
    assert.equal(await page.locator('.working').count(), 0, 'no spinner left behind');
    assert.equal(await page.locator('input[type=file]').isDisabled(), false, 'the controls work again');
    await page.unroute('**/Captions/api/jobs');

    // Caption changes still reach Jellyfin while the Real-Debrid service is down
    execFileSync('docker', ['compose', 'stop', 'realdebrid'], { stdio: 'ignore' });
    await addCaption('it', 'Italian');
    await untilSubtitles('eng,ita');
    execFileSync('docker', ['compose', 'start', 'realdebrid'], { stdio: 'ignore' });

    await page.getByRole('link', { name: /All videos/ }).click();
    await page.getByRole('searchbox').waitFor();

    // A strange file name stays plain text on the page
    const odd = '<img src=x onerror=window.hacked=1> (2021)';
    mkdirSync(`${LIB}/movies/${odd}`, { recursive: true });
    copyFileSync(captionFilm, `${LIB}/movies/${odd}/${odd}.mkv`);
    await page.reload();
    await page.getByRole('searchbox').fill('onerror');
    await page.locator('[data-video]', { hasText: odd }).click();
    await page.locator('h1', { hasText: odd }).waitFor();
    assert.equal(await page.evaluate(() => window.hacked), undefined, 'the name stayed text');
    assert.equal(await page.locator('img').count(), 0, 'no image was made from the name');
    await context.close();

    // A non-admin sees none of it
    const guestContext = await browser.newContext({ ...phoneOptions, colorScheme: 'dark' });
    const guestPage = await logIn(guestContext, 'guest', 'guest-password');
    await guestPage.getByRole('button', { name: 'Open Menu' }).click();
    await guestPage.waitForTimeout(1500);
    await guestPage.screenshot({ path: SHOTS + '08-guest-menu.png' });
    assert.equal(await guestPage.locator('a.realDebridLink').count(), 0, 'no menu entry for a guest');
    assert.equal(await guestPage.locator('a.captionsLink').count(), 0, 'no captions entry for a guest');
    await guestPage.goto(URL_ + '/RealDebrid/app');
    await guestPage.getByText('admins only').waitFor({ timeout: 10000 });
    assert.equal(await guestPage.locator('[data-torrent]').count(), 0, 'no list for a guest');
    await guestPage.goto(URL_ + '/Captions/app');
    await guestPage.getByText('admins only').waitFor({ timeout: 10000 });
    assert.equal(await guestPage.locator('[data-video]').count(), 0, 'no videos for a guest');
    await guestContext.close();

    // Nor does someone who isn't logged in, who gets pointed at the login
    const strangerContext = await browser.newContext({ ...phoneOptions, colorScheme: 'dark' });
    const stranger = await strangerContext.newPage();
    stranger.on('pageerror', (e) => { problems.push('stranger: ' + e.message); });
    await stranger.goto(URL_ + '/RealDebrid/app');
    await stranger.getByRole('link', { name: /Log in/ }).click();
    await stranger.waitForURL('**/web/**');
    await strangerContext.close();

    assert.deepEqual(problems, [], 'errors in the browser');
    console.log('phone run passed');
} catch (err) {
    for (const ctx of browser.contexts()) for (const p of ctx.pages()) await p.screenshot({ path: SHOTS + 'failure.png' }).catch(() => {});
    throw err;
} finally {
    await browser.close();
}
