// Cloudflare Worker for the "Detalle operativo pivotes" box on https://h-foy.github.io/planta-solar-vivo/
//
// What it does:
//   GET  /pivotes?date=YYYY-MM-DD           -> that day's pivot times (read straight from the repo, always fresh)
//   POST /check    {password}               -> 200 if the edit password is right, 401 if not
//   POST /pivotes  {date, on, password}     -> saves pivotes/YYYY-MM-DD.json in the repo
//
// Settings (Worker -> Settings -> Variables and Secrets):
//   GH_TOKEN       (secret)  GitHub fine-grained token: only repository h-foy/planta-solar-vivo, permission "Contents: Read and write"
//   EDIT_PASSWORD  (secret)  the password people type to edit pivot times
//   ALLOW_ORIGIN   (text, optional)  defaults to https://h-foy.github.io
//   REPO           (text, optional)  defaults to h-foy/planta-solar-vivo
//   CAM_KEY        (secret)  shared key; GitHub's cam_fetch.py sends it with each camera photo
//   CAM            (KV namespace binding)  holds the single latest camera photo
//
// Camera photo:
//   GET  /foto.jpg                          -> the latest photo (only one is ever kept)
//   GET  /foto                              -> {hora} time of that photo (UTC, ISO)
//   PUT  /foto   (X-Cam-Key, X-Foto-Hora)   -> replaces the photo

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const SLOTS = /^[01]{96}$/;

export default {
  async fetch(req, env) {
    const origin = env.ALLOW_ORIGIN || 'https://h-foy.github.io';
    const cors = {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Max-Age': '86400',
      'Vary': 'Origin',
    };
    const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
      status, headers: { ...cors, 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' },
    });
    if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });

    const url = new URL(req.url);
    const repo = env.REPO || 'h-foy/planta-solar-vivo';
    const gh = (path, init = {}) => fetch(`https://api.github.com/repos/${repo}/contents/${path}`, {
      ...init,
      headers: {
        'Authorization': `Bearer ${env.GH_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'planta-solar-pivotes',
        ...(init.headers || {}),
      },
    });

    // ---- read a day ----
    if (req.method === 'GET' && url.pathname === '/pivotes') {
      const date = url.searchParams.get('date') || '';
      if (!DATE.test(date)) return json({ error: 'fecha inválida' }, 400);
      const r = await gh(`pivotes/${date}.json?ref=main`);
      if (r.status === 404) return json({ date, on: [] });
      if (!r.ok) return json({ error: `github ${r.status}` }, 502);
      const file = await r.json();
      try { return json(JSON.parse(atob(file.content.replace(/\n/g, '')))); }
      catch { return json({ date, on: [] }); }
    }

    // ---- camera photo: exactly one kept, each upload overwrites the previous one ----
    if (url.pathname === '/foto.jpg' && req.method === 'GET') {
      if (!env.CAM) return new Response('sin configurar', { status: 500 });
      const { value, metadata } = await env.CAM.getWithMetadata('latest', { type: 'arrayBuffer' });
      if (!value) return new Response('todavía no hay foto', { status: 404, headers: { 'Cache-Control': 'no-store' } });
      return new Response(value, { headers: {
        'Content-Type': 'image/jpeg', 'Cache-Control': 'no-store',
        'X-Foto-Hora': (metadata && metadata.hora) || '', 'Access-Control-Allow-Origin': '*' } });
    }
    if (url.pathname === '/foto' && req.method === 'GET') {
      if (!env.CAM) return json({ error: 'sin configurar' }, 500);
      const { metadata } = await env.CAM.getWithMetadata('latest', { type: 'stream' });
      return json({ hora: (metadata && metadata.hora) || null });
    }
    if (url.pathname === '/foto' && req.method === 'PUT') {
      if (!env.CAM || !env.CAM_KEY) return json({ error: 'sin configurar' }, 500);
      if (!(await sameText(req.headers.get('X-Cam-Key') || '', env.CAM_KEY))) return json({ error: 'clave incorrecta' }, 401);
      const img = await req.arrayBuffer();
      const head = new Uint8Array(img.slice(0, 2));
      if (img.byteLength < 1000 || img.byteLength > 5e6 || head[0] !== 0xff || head[1] !== 0xd8)
        return json({ error: 'foto inválida' }, 400);
      const hora = (req.headers.get('X-Foto-Hora') || new Date().toISOString()).slice(0, 25);
      await env.CAM.put('latest', img, { metadata: { hora } });
      return json({ ok: true, hora });
    }

    if (req.method !== 'POST' || !['/check', '/pivotes'].includes(url.pathname)) return json({ error: 'no encontrado' }, 404);

    let body;
    try { body = await req.json(); } catch { return json({ error: 'pedido inválido' }, 400); }

    // ---- password ----
    if (!env.EDIT_PASSWORD || !env.GH_TOKEN) return json({ error: 'servicio sin configurar' }, 500);
    if (!(await sameText(String(body.password || ''), env.EDIT_PASSWORD))) {
      await new Promise(r => setTimeout(r, 800));            // slow down guessing
      return json({ error: 'contraseña incorrecta' }, 401);
    }
    if (url.pathname === '/check') return json({ ok: true });

    // ---- save a day ----
    const { date, on } = body;
    if (!DATE.test(date || '')) return json({ error: 'fecha inválida' }, 400);
    if (!Array.isArray(on) || on.length > 20 || !on.every(s => typeof s === 'string' && SLOTS.test(s)))
      return json({ error: 'horarios inválidos' }, 400);

    const path = `pivotes/${date}.json`;
    const content = btoa(JSON.stringify({ date, on, guardado: new Date().toISOString() }) + '\n');
    for (let attempt = 0; attempt < 3; attempt++) {
      const cur = await gh(`${path}?ref=main`);
      const sha = cur.ok ? (await cur.json()).sha : undefined;
      const put = await gh(path, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: `Pivotes ${date}`, content, branch: 'main', ...(sha ? { sha } : {}) }),
      });
      if (put.ok) return json({ ok: true });
      if (put.status !== 409 && put.status !== 422) return json({ error: `github ${put.status}` }, 502);
    }
    return json({ error: 'conflicto al guardar, intente de nuevo' }, 409);
  },
};

// constant-time comparison of two strings (compares their SHA-256 digests)
async function sameText(a, b) {
  const enc = new TextEncoder();
  const [x, y] = await Promise.all([a, b].map(s => crypto.subtle.digest('SHA-256', enc.encode(s))));
  const u = new Uint8Array(x), v = new Uint8Array(y);
  let diff = 0;
  for (let i = 0; i < u.length; i++) diff |= u[i] ^ v[i];
  return diff === 0;
}
