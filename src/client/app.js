(function () {
  'use strict';
  var POLL_MS = 5000;
  var TIMEOUT_MS = 4000;
  var REDIS_DOWN_MSG = 'Visit recording unavailable while Redis is down.';
  var $ = function (id) { return document.getElementById(id); };
  var el = {
    overall: $('overall'), error: $('error'), service: $('service'), version: $('version'),
    uptime: $('uptime'), redis: $('redis'), visits: $('visits'), btn: $('visit-btn'),
    msg: $('visit-msg'), updated: $('updated')
  };

  var pollInflight = false;
  var visitInflight = false;
  var redisOk = false;
  var visitRetryTimer = null;

  function setText(node, text, cls) {
    node.textContent = text;
    if (cls !== undefined) node.className = cls;
  }

  function str(v, fallback) {
    return (typeof v === 'string' && v !== '') ? v : fallback;
  }

  function formatUptime(s) {
    if (typeof s !== 'number' || !isFinite(s) || s < 0) return 'unknown';
    s = Math.floor(s);
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
        m = Math.floor(s % 3600 / 60), sec = s % 60, parts = [];
    if (d) parts.push(d + 'd');
    if (d || h) parts.push(h + 'h');
    if (d || h || m) parts.push(m + 'm');
    parts.push(sec + 's');
    return parts.join(' ');
  }

  function formatVisits(v) {
    return (typeof v === 'number' && isFinite(v)) ? String(v) : 'unavailable';
  }

  function showError(msg) {
    if (msg) { el.error.textContent = msg; el.error.hidden = false; }
    else { el.error.textContent = ''; el.error.hidden = true; }
  }

  function setOverall(kind, text) {
    setText(el.overall, text, 'badge badge-' + kind);
  }

  function syncVisitControls() {
    if (visitInflight || visitRetryTimer) return;
    if (redisOk) {
      el.btn.disabled = false;
      if (el.msg.textContent === REDIS_DOWN_MSG) setText(el.msg, '');
    } else {
      el.btn.disabled = true;
    }
  }

  function parseRetryAfter(raw) {
    if (!raw) return null;
    var n = parseInt(raw, 10);
    if (!isFinite(n) || n < 0 || String(n) !== String(raw).trim()) return null;
    return n;
  }

  function formatWait(n) {
    if (typeof n !== 'number' || !isFinite(n) || n <= 0) return 'shortly';
    if (n >= 60) return 'in ' + Math.ceil(n / 60) + ' min';
    return 'in ' + n + ' s';
  }

  function fetchJson(url, opts) {
    var ctrl = new AbortController();
    var t = setTimeout(function () { ctrl.abort(); }, TIMEOUT_MS);
    opts = opts || {};
    opts.signal = ctrl.signal;
    opts.cache = 'no-store';
    return fetch(url, opts).then(function (r) {
      clearTimeout(t);
      var retryAfter = parseRetryAfter(r.headers.get('Retry-After'));
      return r.json().catch(function () { return null; }).then(function (body) {
        return { ok: r.ok, status: r.status, body: body, retryAfter: retryAfter };
      });
    }, function (e) { clearTimeout(t); throw e; });
  }

  function scheduleVisitRetry(seconds) {
    if (typeof seconds !== 'number' || !isFinite(seconds) || seconds <= 0 || !redisOk) return;
    if (visitRetryTimer) clearTimeout(visitRetryTimer);
    el.btn.disabled = true;
    var ms = Math.min(seconds, 60) * 1000;
    visitRetryTimer = setTimeout(function () {
      visitRetryTimer = null;
      if (!visitInflight) syncVisitControls();
    }, ms);
  }

  function renderStatus(d) {
    if (!d || typeof d !== 'object') throw new Error('bad payload');
    setText(el.service, str(d.service, 'unknown'));
    setText(el.version, str(d.version, 'unknown'));
    setText(el.uptime, formatUptime(d.uptime_seconds));
    var connected = !!(d.redis && d.redis.connected === true);
    redisOk = connected;
    setText(el.redis, connected ? 'ok' : 'down', connected ? 'ok' : 'bad');
    setText(el.visits, formatVisits(d.visits), connected ? '' : 'warn');
    if (connected) { setOverall('ok', 'Healthy'); showError(null); }
    else {
      setOverall('warn', 'Degraded');
      showError('Redis is down. Visit counting is unavailable.');
      if (!visitInflight) setText(el.msg, REDIS_DOWN_MSG);
    }
    syncVisitControls();
    setText(el.updated, new Date().toLocaleTimeString());
  }

  function renderUnreachable(msg) {
    redisOk = false;
    setOverall('bad', 'Unreachable');
    showError(msg);
    setText(el.redis, 'unknown', 'warn');
    syncVisitControls();
  }

  function fetchStatus(opts) {
    opts = opts || {};
    return fetchJson('/api/status').then(function (r) {
      if (!r.ok) throw new Error('Status request failed (HTTP ' + r.status + ').');
      renderStatus(r.body);
    }).catch(function (e) {
      // After a successful visit, do not paint Unreachable or wipe visit state.
      if (opts.soft) return;
      var m = (e && e.message && /^Status request/.test(e.message)) ? e.message
        : 'Cannot reach the service. Retrying every 5 seconds.';
      renderUnreachable(m);
    });
  }

  function poll() {
    if (pollInflight) return;
    pollInflight = true;
    fetchStatus().then(function () { pollInflight = false; });
  }

  el.btn.addEventListener('click', function () {
    if (!redisOk || visitInflight) return;
    visitInflight = true;
    if (visitRetryTimer) { clearTimeout(visitRetryTimer); visitRetryTimer = null; }
    el.btn.disabled = true;
    setText(el.msg, 'Recording…');
    fetchJson('/api/visits', { method: 'POST' }).then(function (r) {
      if (r.ok && r.body && typeof r.body.visits === 'number') {
        setText(el.visits, String(r.body.visits), '');
        setText(el.msg, 'Visit recorded.');
        return fetchStatus({ soft: true }).then(function () { return null; });
      } else if (r.status === 429) {
        setText(el.msg, 'Too many visits — try again ' + formatWait(r.retryAfter) + '.');
        return r.retryAfter;
      } else if (r.status === 503) {
        var err = (r.body && typeof r.body.error === 'string') ? r.body.error : '';
        if (err === 'redis unavailable') {
          redisOk = false;
          setText(el.msg, 'Could not record visit: Redis is unavailable.');
          return null;
        }
        if (err === 'rate limit unavailable') {
          setText(el.msg, 'Visit rate limiting is temporarily unavailable. Try again shortly.');
        } else {
          setText(el.msg, 'Could not record visit (HTTP 503).');
        }
        return r.retryAfter;
      } else {
        setText(el.msg, 'Could not record visit (HTTP ' + r.status + ').');
        return r.retryAfter;
      }
    }).catch(function () {
      setText(el.msg, 'Could not record visit: network error.');
      return null;
    }).then(function (retryAfter) {
      visitInflight = false;
      syncVisitControls();
      scheduleVisitRetry(retryAfter);
    });
  });

  syncVisitControls();
  poll();
  setInterval(poll, POLL_MS);
})();
