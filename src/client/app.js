(function () {
  'use strict';
  var POLL_MS = 5000;
  var TIMEOUT_MS = 4000;
  var $ = function (id) { return document.getElementById(id); };
  var el = {
    overall: $('overall'), error: $('error'), service: $('service'), version: $('version'),
    uptime: $('uptime'), redis: $('redis'), visits: $('visits'), btn: $('visit-btn'),
    msg: $('visit-msg'), updated: $('updated')
  };

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

  function fetchJson(url, opts) {
    var ctrl = new AbortController();
    var t = setTimeout(function () { ctrl.abort(); }, TIMEOUT_MS);
    opts = opts || {};
    opts.signal = ctrl.signal;
    opts.cache = 'no-store';
    return fetch(url, opts).then(function (r) {
      clearTimeout(t);
      return r.json().catch(function () { return null; }).then(function (body) {
        return { ok: r.ok, status: r.status, body: body };
      });
    }, function (e) { clearTimeout(t); throw e; });
  }

  function renderStatus(d) {
    if (!d || typeof d !== 'object') throw new Error('bad payload');
    setText(el.service, str(d.service, 'unknown'));
    setText(el.version, str(d.version, 'unknown'));
    setText(el.uptime, formatUptime(d.uptime_seconds));
    var connected = !!(d.redis && d.redis.connected === true);
    setText(el.redis, connected ? 'ok' : 'down', connected ? 'ok' : 'bad');
    setText(el.visits, formatVisits(d.visits), connected ? '' : 'warn');
    if (connected) { setOverall('ok', 'Healthy'); showError(null); }
    else {
      setOverall('warn', 'Degraded');
      showError('Redis is down. Visit counting is unavailable.');
    }
    setText(el.updated, new Date().toLocaleTimeString());
  }

  function renderUnreachable(msg) {
    setOverall('bad', 'Unreachable');
    showError(msg);
    setText(el.redis, 'unknown', 'warn');
  }

  var inflight = false;
  function poll() {
    if (inflight) return;
    inflight = true;
    fetchJson('/api/status').then(function (r) {
      if (!r.ok) throw new Error('Status request failed (HTTP ' + r.status + ').');
      renderStatus(r.body);
    }).catch(function (e) {
      var m = (e && e.message && /^Status request/.test(e.message)) ? e.message
        : 'Cannot reach the service. Retrying every 5 seconds.';
      renderUnreachable(m);
    }).then(function () { inflight = false; });
  }

  el.btn.addEventListener('click', function () {
    el.btn.disabled = true;
    setText(el.msg, 'Recording…');
    fetchJson('/api/visits', { method: 'POST' }).then(function (r) {
      if (r.ok && r.body && typeof r.body.visits === 'number') {
        setText(el.visits, String(r.body.visits), '');
        setText(el.msg, 'Visit recorded.');
      } else if (r.status === 503) {
        setText(el.msg, 'Could not record visit: Redis is unavailable.');
      } else {
        setText(el.msg, 'Could not record visit (HTTP ' + r.status + ').');
      }
    }).catch(function () {
      setText(el.msg, 'Could not record visit: network error.');
    }).then(function () { el.btn.disabled = false; });
  });

  poll();
  setInterval(poll, POLL_MS);
})();
