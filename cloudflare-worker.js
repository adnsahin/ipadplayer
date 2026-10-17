// IPTV liste proxy'si — Cloudflare Workers (ücretsiz plan yeterli).
// Yalnızca M3U/Xtream LİSTESİNİ indirir; video yayınlarını taşımaz.
// Kurulum: dash.cloudflare.com → Workers & Pages → Create → Worker → bu kodu yapıştır → Deploy.

const ALLOWED_ORIGIN = 'https://adnsahin.github.io';

export default {
  async fetch(request) {
    const cors = {
      'Access-Control-Allow-Origin': ALLOWED_ORIGIN,
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Vary': 'Origin'
    };
    if (request.method === 'OPTIONS') return new Response(null, { headers: cors });
    if (request.method !== 'GET') return new Response('Yalnızca GET', { status: 405, headers: cors });

    // Başka sitelerin bu proxy'yi kullanmasını engelle
    const origin = request.headers.get('Origin');
    if (origin && origin !== ALLOWED_ORIGIN) return new Response('Yasak', { status: 403 });

    const target = new URL(request.url).searchParams.get('url');
    if (!target || !/^https?:\/\//i.test(target)) {
      return new Response('Kullanım: ?url=<liste adresi>', { status: 400, headers: cors });
    }

    let upstream;
    try {
      upstream = await fetch(target, {
        headers: { 'User-Agent': 'VLC/3.0.20 LibVLC/3.0.20' },
        redirect: 'follow'
      });
    } catch (e) {
      return new Response('Liste sunucusuna ulaşılamadı: ' + e.message, { status: 502, headers: cors });
    }

    const headers = new Headers(cors);
    headers.set('Content-Type', 'text/plain; charset=utf-8');
    headers.set('Cache-Control', 'no-store');
    return new Response(upstream.body, { status: upstream.status, headers });
  }
};
