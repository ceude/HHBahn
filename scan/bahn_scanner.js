/*
 * bahn_scanner.js (v12)
 * Iki sekilde calisir:
 *  1) Manuel: www.bahn.de acikken F12 -> Console -> tamamini yapistir -> Enter
 *     Bitince data.js + bahn_results.csv iner.
 *  2) Otomatik: scan/run.js (Playwright) bu dosyayi bahn.de sayfasinda calistirir.
 *     window.__BAHN_HEADLESS = true ise dosya indirmez, sonucu return eder.
 *
 * v7: Adaptif hiz (900ms baslar, 429'da +300ms, temiz gidince hizlanir) + jitter.
 * v8: Ara duraklar (Zwischenhalte) kaydediliyor. Amsterdam eklendi.
 * v10: max sure 9->10 saat (Paris denendi, direkt sefer olmadigi icin cikarildi).
 * v9: Pencereye uyan TUM seferler data.js'e yaziliyor (legs dizisi) ->
 *     tek yon modunda hicbir sefer elenmez. Gidis-donus kombolari yine en ucuz eslesme.
 * v12: Headless modu (GitHub Actions ile otomatik tarama).
 */
(async () => {
  // ---------------- AYARLAR ----------------
  const MAX_DURATION_H = 10.0;
  const MIN_STAY_H = 2.0;
  let REQUEST_DELAY = 900;      // adaptif baslangic
  const DELAY_MIN = 700, DELAY_MAX = 4000;
  let okStreak = 0;
  const slower = () => { REQUEST_DELAY = Math.min(DELAY_MAX, REQUEST_DELAY + 300); okStreak = 0; console.warn(`   bekleme artirildi: ${REQUEST_DELAY}ms`); };
  const faster = () => { if (++okStreak >= 40 && REQUEST_DELAY > DELAY_MIN) { REQUEST_DELAY -= 100; okStreak = 0; } };
  const jitter = () => REQUEST_DELAY + Math.floor(Math.random() * 250);
  const NUM_WEEKENDS = 6;

  // [gorunumAdi(site anahtari), istasyonAdi, EVA]
  const ORIGINS = [
    {
      key: "Hamburg", name: "Hamburg Hbf", eva: "8002549",
      dests: [
        ["Berlin",     "Berlin Hbf",           "8011160"],
        ["München",    "München Hbf",          "8000261"],
        ["Köln",       "Köln Hbf",             "8000207"],
        ["Frankfurt",  "Frankfurt (Main) Hbf", "8000105"],
        ["Stuttgart",  "Stuttgart Hbf",        "8000096"],
        ["Düsseldorf", "Düsseldorf Hbf",       "8000085"],
        ["Leipzig",    "Leipzig Hbf",          "8010205"],
        ["Nürnberg",   "Nürnberg Hbf",         "8000284"],
        ["Münster",    "Münster (Westf) Hbf",  "8000263"],
        ["Hannover",   "Hannover Hbf",         "8000152"],
        ["Bremen",     "Bremen Hbf",           "8000050"],
        ["Kiel",       "Kiel Hbf",             "8000199"],
        ["Kopenhagen", "København H",          "8601309"],
        ["Amsterdam",  "Amsterdam Centraal",   "8400058"],
      ],
    },
    {
      key: "München", name: "München Hbf", eva: "8000261",
      dests: [
        ["Hamburg",    "Hamburg Hbf",          "8002549"],
        ["Berlin",     "Berlin Hbf",           "8011160"],
        ["Wien",       "Wien Hbf",             "8103000"],
        ["Prag",       "Praha hl.n.",          "5400014"],
      ],
    },
  ];

  const WINDOWS = {
    FRI_OUT: [["18:00", "20:30"], "18:00", "23:00"],
    SAT_OUT: [["06:00"],          "06:00", "10:00"],
    SAT_RET: [["17:00", "20:00"], "17:00", "23:00"],
    SUN_RET: [["15:00", "17:30"], "15:00", "20:00"],
  };

  // ---------------- YARDIMCILAR ----------------
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const pad = (n) => String(n).padStart(2, "0");
  const dstr = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const iso = (dt) => `${dstr(dt)}T${pad(dt.getHours())}:${pad(dt.getMinutes())}:00`;
  const gunler = ["Paz", "Pzt", "Sal", "Car", "Per", "Cum", "Cts"];
  const fmt = (dt) => `${gunler[dt.getDay()]} ${pad(dt.getDate())}.${pad(dt.getMonth() + 1)} ${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
  const hhmm = (dt) => `${pad(dt.getHours())}:${pad(dt.getMinutes())}`;

  function nextWeekends(n) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const toFri = (5 - today.getDay() + 7) % 7;
    const out = [];
    for (let i = 0; i < n; i++) {
      const fri = new Date(today);
      fri.setDate(today.getDate() + toFri + i * 7);
      const sat = new Date(fri); sat.setDate(fri.getDate() + 1);
      const sun = new Date(fri); sun.setDate(fri.getDate() + 2);
      out.push([fri, sat, sun]);
    }
    return out;
  }

  async function resolveStation(name, eva) {
    const r = await fetch(
      `/web/api/reiseloesung/orte?suchbegriff=${encodeURIComponent(name)}&typ=ALL&limit=10`,
      { headers: { accept: "application/json" } }
    );
    if (!r.ok) throw new Error(`orte ${r.status} (${name})`);
    const j = await r.json();
    const hit = j.find((x) => x.extId === eva || (x.id || "").includes(`L=${eva}@`)) || j[0];
    if (!hit) throw new Error(`Istasyon bulunamadi: ${name}`);
    if (hit.extId !== eva) console.warn(`  UYARI: ${name} icin EVA ${eva} eslesmedi, kullanilan: ${hit.name} (${hit.extId})`);
    return hit.id;
  }

  async function queryConnections(fromId, toId, whenIso) {
    const body = {
      abfahrtsHalt: fromId,
      anfrageZeitpunkt: whenIso,
      ankunftsHalt: toId,
      ankunftSuche: "ABFAHRT",
      klasse: "KLASSE_2",
      produktgattungen: ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"],
      reisende: [{ typ: "ERWACHSENER", ermaessigungen: [{ art: "KEINE_ERMAESSIGUNG", klasse: "KLASSENLOS" }], alter: [], anzahl: 1 }],
      schnelleVerbindungen: true,
      autonomeReservierungOnly: false,
      bikeCarriage: false,
      reservierungsKontingenteVorhanden: false,
      nurDeutschlandTicketVerbindungen: false,
      deutschlandTicketVorhanden: false,
    };
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const r = await fetch("/web/api/angebote/fahrplan", {
          method: "POST",
          headers: { accept: "application/json", "content-type": "application/json; charset=UTF-8" },
          body: JSON.stringify(body),
        });
        if (r.status === 429) { slower(); console.warn("429 rate limit, 20 sn bekleniyor..."); await sleep(20000); continue; }
        if (!r.ok) throw new Error(`fahrplan ${r.status}`);
        faster();
        return (await r.json()).verbindungen || [];
      } catch (e) {
        if (attempt === 2) { console.warn("istek atlandi:", e.message); return []; }
        await sleep(4000);
      }
    }
    return [];
  }

  const isWalk = (s) => {
    const t = ((s.verkehrsmittel || {}).typ || "").toUpperCase();
    return !s.verkehrsmittel || ["FUSSWEG", "TRANSFER", "WALK", "UEBERGANG"].includes(t);
  };
  const depTime = (s) => s.abfahrtsZeitpunkt || (s.abfahrt || {}).ezZeit || (s.abfahrt || {}).sollzeit ||
                         ((s.startHalt || {}).abfahrt || {}).ezZeit || ((s.startHalt || {}).abfahrt || {}).sollzeit || null;
  const arrTime = (s) => s.ankunftsZeitpunkt || (s.ankunft || {}).ezZeit || (s.ankunft || {}).sollzeit ||
                         ((s.zielHalt || {}).ankunft || {}).ezZeit || ((s.zielHalt || {}).ankunft || {}).sollzeit || null;

  let stopsFieldLogged = false;
  // Bir tren bolumunden ara duraklari cikar (alan adi surumden surume degisebiliyor)
  function sectionStops(sec) {
    const arr = sec.halte || sec.zwischenHalte || sec.haltestellen || sec.stopps || null;
    if (!Array.isArray(arr) || arr.length < 3) return [];
    if (!stopsFieldLogged) {
      stopsFieldLogged = true;
      console.log(`   (ara durak alani bulundu, ornek: ${arr.length} halt)`);
    }
    // ilk ve son = biniş/iniş, aradakiler ara durak
    return arr.slice(1, -1).map((h) => {
      const t = (h.ankunftsZeitpunkt || (h.ankunft || {}).ezZeit || (h.ankunft || {}).sollzeit ||
                 h.abfahrtsZeitpunkt || (h.abfahrt || {}).ezZeit || (h.abfahrt || {}).sollzeit || null);
      return { n: (h.name || h.bahnhofsInfoId || "").trim(), t: t ? hhmm(new Date(t)) : "" };
    }).filter((h) => h.n);
  }

  function parseLeg(conn) {
    const secs = conn.verbindungsAbschnitte || [];
    if (!secs.length) return null;
    const dep = depTime(secs[0]);
    const arr = arrTime(secs[secs.length - 1]);
    if (!dep || !arr) return null;
    const depDt = new Date(dep), arrDt = new Date(arr);
    const durH = (arrDt - depDt) / 3600000;
    const rides = secs.filter((s) => !isWalk(s));
    let umstiege = conn.umsteigeAnzahl ?? conn.umstiegsAnzahl;
    if (umstiege == null) umstiege = Math.max(0, rides.length - 1);
    const price = conn.angebotsPreis ? conn.angebotsPreis.betrag
                : conn.einzelPreis ? conn.einzelPreis.betrag
                : null;
    const trains = rides.map((s) => ((s.verkehrsmittel || {}).name || "").trim()).filter(Boolean).join(", ");
    const stops = rides.length ? sectionStops(rides[0]) : [];
    return { depDt, arrDt, durH, umstiege, price, trains, stops };
  }

  async function scanLeg(fromSt, toSt, date, windowKey) {
    const [starts, wFrom, wTo] = WINDOWS[windowKey];
    const seen = new Set(), legs = [];
    for (const start of starts) {
      const conns = await queryConnections(fromSt.id, toSt.id, `${dstr(date)}T${start}:00`);
      await sleep(jitter());
      for (const c of conns) {
        const leg = parseLeg(c);
        if (!leg) continue;
        const key = leg.depDt.toISOString();
        if (seen.has(key)) continue;
        seen.add(key);
        if (dstr(leg.depDt) !== dstr(date)) continue;
        const t = hhmm(leg.depDt);
        if (t < wFrom || t > wTo) continue;
        if (leg.umstiege > 0) continue;
        if (leg.durH > MAX_DURATION_H) continue;
        if (leg.price == null) continue;
        leg.from = fromSt.name; leg.to = toSt.name;
        legs.push(leg);
      }
    }
    return legs;
  }

  function download(filename, text, mime) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: mime }));
    a.download = filename;
    a.click();
  }

  // ---------------- ANA AKIS ----------------
  const weekends = nextWeekends(NUM_WEEKENDS);
  console.log("Haftasonlari:", weekends.map(([f, , s]) => `${pad(f.getDate())}.${pad(f.getMonth() + 1)}-${pad(s.getDate())}.${pad(s.getMonth() + 1)}`).join(", "));

  const allLegs = [], deals = [], legsOut = [];
  const legJson = (l) => ({
    dep: iso(l.depDt), arr: iso(l.arrDt), from: l.from, to: l.to,
    trains: l.trains, price: l.price, durH: Math.round(l.durH * 10) / 10,
    stops: (l.stops || []).map((h) => [h.n, h.t]),
  });

  const totalScan = ORIGINS.reduce((a, o) => a + o.dests.length, 0) * weekends.length;
  let done = 0;

  for (const og of ORIGINS) {
    console.log(`\n=== KALKIS: ${og.name} — istasyonlar cozuluyor... ===`);
    const origin = { name: og.name, id: await resolveStation(og.name, og.eva) };
    const dests = [];
    for (const [key, name, eva] of og.dests) {
      dests.push({ key, name, id: await resolveStation(name, eva) });
      await sleep(300);
    }

    for (const [fri, sat, sun] of weekends) {
      for (const d of dests) {
        const friLegs = await scanLeg(origin, d, fri, "FRI_OUT");
        const satLegs = await scanLeg(origin, d, sat, "SAT_OUT");
        const satRetLegs = await scanLeg(d, origin, sat, "SAT_RET");
        const sunLegs = await scanLeg(d, origin, sun, "SUN_RET");
        done++;
        console.log(`[${done}/${totalScan}] ${og.key} -> ${d.key} ${pad(fri.getDate())}.${pad(fri.getMonth() + 1)} — Cu:${friLegs.length} Cts:${satLegs.length} CtsD:${satRetLegs.length} Paz:${sunLegs.length}`);
        allLegs.push(...friLegs, ...satLegs, ...satRetLegs, ...sunLegs);

        // Tek yon icin: pencereye uyan TUM seferler
        const pushLegs = (arr, dir, win) => arr.forEach((l) => legsOut.push({
          o: og.key, c: d.key, dir, win,
          dep: iso(l.depDt), arr: iso(l.arrDt), from: l.from, to: l.to,
          trains: l.trains, price: l.price, durH: Math.round(l.durH * 10) / 10,
          stops: (l.stops || []).map((h) => [h.n, h.t]),
        }));
        pushLegs(friLegs, "out", "FRI");
        pushLegs(satLegs, "out", "SAT");
        pushLegs(satRetLegs, "ret", "SATRET");
        pushLegs(sunLegs, "ret", "SUN");

        if (sunLegs.length) {
          const cheapSun = sunLegs.reduce((a, b) => (a.price <= b.price ? a : b));
          for (const [label, outLegs] of [["Cuma", friLegs], ["Cumartesi", satLegs]]) {
            if (!outLegs.length) continue;
            const cheapOut = outLegs.reduce((a, b) => (a.price <= b.price ? a : b));
            deals.push({
              origin: og.key, city: d.key, variant: label,
              total: Math.round((cheapOut.price + cheapSun.price) * 100) / 100,
              out: legJson(cheapOut), ret: legJson(cheapSun),
            });
          }
        }
        if (satLegs.length && satRetLegs.length) {
          let best = null;
          for (const o of satLegs) for (const r of satRetLegs) {
            if ((r.depDt - o.arrDt) / 3600000 < MIN_STAY_H) continue;
            const p = o.price + r.price;
            if (!best || p < best.p) best = { o, r, p };
          }
          if (best) {
            deals.push({
              origin: og.key, city: d.key, variant: "Gunubirlik",
              total: Math.round(best.p * 100) / 100,
              out: legJson(best.o), ret: legJson(best.r),
            });
          }
        }
      }
    }
  }

  deals.sort((a, b) => a.total - b.total);
  console.log(`\nToplam kombo: ${deals.length}`);

  const now = new Date();
  legsOut.sort((a, b) => (a.o + a.c + a.dep).localeCompare(b.o + b.c + b.dep));
  const payload = {
    generatedAt: iso(now),
    priceCap: null,
    origins: ORIGINS.map((o) => o.key),
    deals,
    legs: legsOut,
  };
  console.log(`Tek yon sefer sayisi: ${legsOut.length}`);
  const dataJs = "window.BAHN_DATA = " + JSON.stringify(payload, null, 1) + ";\n";

  const csv = "﻿" + [
    ["Tarih/saat", "Kalkis", "Varis", "Varis saati", "Tren", "Sure (s)", "EUR"].join(";"),
    ...allLegs.sort((a, b) => (a.to + a.depDt.toISOString()).localeCompare(b.to + b.depDt.toISOString()))
      .map((lg) => [fmt(lg.depDt), lg.from, lg.to, hhmm(lg.arrDt), lg.trains, lg.durH.toFixed(1), lg.price.toFixed(2)].join(";")),
  ].join("\r\n");

  if (window.__BAHN_HEADLESS) {
    return { dataJs, csv, deals: deals.length, legs: legsOut.length };
  }
  download("data.js", dataJs, "text/javascript");
  download("bahn_results.csv", csv, "text/csv;charset=utf-8");
  console.log("Bitti. data.js'i GitHub repoya push'la.");
})();
