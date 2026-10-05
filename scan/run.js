// scan/run.js — bahn_scanner.js'i headless Chromium'da bahn.de uzerinde calistirir,
// sonucu repo kokune data.js (+ bahn_results.csv) olarak yazar.
// Kullanim (repo kokunden):  node scan/run.js
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "..");
const SCANNER = fs.readFileSync(path.join(__dirname, "bahn_scanner.js"), "utf8").trim().replace(/;\s*$/, "");
const MIN_LEGS = Number(process.env.MIN_LEGS || 50); // bundan azsa tarama bozuk say, data.js'e dokunma

(async () => {
  const browser = await chromium.launch({
    headless: true,
    args: ["--disable-blink-features=AutomationControlled"],
  });
  // Script saatleri yerel saatle hesapliyor -> runner UTC olsa da Berlin saati kullan
  const context = await browser.newContext({
    locale: "de-DE",
    timezoneId: "Europe/Berlin",
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    viewport: { width: 1366, height: 900 },
  });
  await context.addInitScript(() => {
    Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  });
  const page = await context.newPage();
  page.on("console", (m) => console.log(`[page:${m.type()}] ${m.text()}`));
  page.on("pageerror", (e) => console.error("[page:error]", e.message));

  try {
    await page.goto("https://www.bahn.de/", { waitUntil: "domcontentloaded", timeout: 90_000 });
    await page.waitForTimeout(5000);

    // On kontrol: API'ye erisebiliyor muyuz? (bot korumasi engelliyorsa burada anlasilir)
    const probe = await page.evaluate(async () => {
      const r = await fetch("/web/api/reiseloesung/orte?suchbegriff=Hamburg%20Hbf&typ=ALL&limit=1", {
        headers: { accept: "application/json" },
      });
      return { status: r.status, body: (await r.text()).slice(0, 200) };
    });
    console.log("API on kontrol:", probe.status, probe.body);
    if (probe.status !== 200) throw new Error(`bahn.de API erisilemedi (HTTP ${probe.status})`);

    if (process.env.PROBE_ONLY === "true") {
      // Asil taramanin kullandigi fahrplan endpoint'ini de bir kez dene (Hamburg -> Berlin, gelecek Cuma 18:00)
      const fp = await page.evaluate(async () => {
        const id = async (q) => (await (await fetch(`/web/api/reiseloesung/orte?suchbegriff=${encodeURIComponent(q)}&typ=ALL&limit=1`,
          { headers: { accept: "application/json" } })).json())[0].id;
        const d = new Date(); d.setDate(d.getDate() + ((5 - d.getDay() + 7) % 7 || 7));
        const day = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
        const r = await fetch("/web/api/angebote/fahrplan", {
          method: "POST",
          headers: { accept: "application/json", "content-type": "application/json; charset=UTF-8" },
          body: JSON.stringify({
            abfahrtsHalt: await id("Hamburg Hbf"), ankunftsHalt: await id("Berlin Hbf"),
            anfrageZeitpunkt: `${day}T18:00:00`, ankunftSuche: "ABFAHRT", klasse: "KLASSE_2",
            produktgattungen: ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"],
            reisende: [{ typ: "ERWACHSENER", ermaessigungen: [{ art: "KEINE_ERMAESSIGUNG", klasse: "KLASSENLOS" }], alter: [], anzahl: 1 }],
            schnelleVerbindungen: true, autonomeReservierungOnly: false, bikeCarriage: false,
            reservierungsKontingenteVorhanden: false, nurDeutschlandTicketVerbindungen: false, deutschlandTicketVorhanden: false,
          }),
        });
        if (!r.ok) return { status: r.status, body: (await r.text()).slice(0, 200) };
        const v = (await r.json()).verbindungen || [];
        const prices = v.map((c) => (c.angebotsPreis || c.einzelPreis || {}).betrag).filter((p) => p != null);
        return { status: r.status, day, count: v.length, prices };
      });
      console.log("Fahrplan testi:", JSON.stringify(fp));
      if (fp.status !== 200 || !fp.count) throw new Error(`fahrplan endpoint'i calismadi (HTTP ${fp.status})`);
      console.log("TEST BASARILI: bahn.de GitHub sunucusundan erisilebiliyor.");
      return;
    }

    await page.evaluate(() => { window.__BAHN_HEADLESS = true; });
    const t0 = Date.now();
    const res = await page.evaluate(SCANNER);
    console.log(`Tarama bitti: ${Math.round((Date.now() - t0) / 60000)} dk, ${res.deals} kombo, ${res.legs} sefer`);

    if (!res || res.legs < MIN_LEGS) {
      throw new Error(`Sefer sayisi cok dusuk (${res && res.legs} < ${MIN_LEGS}), data.js guncellenmedi`);
    }
    fs.writeFileSync(path.join(ROOT, "data.js"), res.dataJs, "utf8");
    fs.writeFileSync(path.join(ROOT, "bahn_results.csv"), res.csv, "utf8");
    console.log("data.js ve bahn_results.csv yazildi.");
  } catch (e) {
    console.error("HATA:", e.message);
    await page.screenshot({ path: path.join(ROOT, "scan-error.png"), fullPage: true }).catch(() => {});
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})();
