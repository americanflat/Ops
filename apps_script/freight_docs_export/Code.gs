/**
 * Freight docs export: Administrator@ Gmail -> gs://americanflat-freight-docs/raw/
 *
 * Runs hourly (installHourlyTrigger). For every thread matching one of
 * SEARCHES and not yet labelled ops/exported it writes:
 *
 *   raw/<doc_type>/<YYYY>/<MM>/<sha256>.meta.json   provenance, written first
 *   raw/<doc_type>/<YYYY>/<MM>/<sha256>.pdf         each PDF attachment
 *   raw/email/<doc_type>/<YYYY>/<MM>/<message_id>.json   body, for body-only searches
 *
 * Every object is created with ifGenerationMatch=0: GCS answers 412 when the
 * name already exists, which this script counts as done. So a PDF sent twice
 * is stored once (its name is its hash), and a re-run after a crash rewrites
 * nothing. The thread is labelled ops/exported only after every object for it
 * is in GCS; a thread with any failed write stays unlabelled and is retried
 * on the next run.
 *
 * Nothing here reads PDF contents or guesses at layouts. doc_type is routing
 * only (search + headers + filename + subject); the extract service decides the rest.
 *
 * Setup and IAM: docs/freight-docs-pdf-ingestion.md in americanflat/Ops.
 */

var CONFIG = {
  BUCKET: 'americanflat-freight-docs',
  LABEL: 'ops/exported',
  THREADS_PER_SEARCH: 50,          // per run; the rest are picked up next hour
  TIME_BUDGET_MS: 4.5 * 60 * 1000, // stop starting threads well before the 6 min limit
};

// Gmail searches. -label:<LABEL> is appended to each. savePdfs writes PDF
// attachments; saveBody writes the message body as JSON (for the emails that
// carry their data in the text, not in a PDF). docType(filename, subject,
// headers) names the doc_type, or returns null to skip that message: a thread
// matches as a whole, but not every message in it is a freight document.
var SEARCHES = [
  { name: 'wwl', query: '(from:wwllmail.com OR cc:wwllmail.com OR to:wwllmail.com) has:attachment',
    savePdfs: true, saveBody: false, docType: routeWwl },
  { name: 'billcom_cbp', query: 'from:hq.bill.com "Customs and Border Protection"',
    savePdfs: true, saveBody: true, docType: function () { return 'billcom_cbp_reminder'; } },
  // WWL titles these both "CUSTOMS RELEASE" and "CUSTOM RELEASE".
  { name: 'customs_release', query: 'subject:(customs OR custom) subject:(release OR clearance)',
    savePdfs: true, saveBody: true, docType: function () { return 'customs_release'; } },
];

// Worldwide Logistics (wwllmail.com) mail reaches Administrator@ three ways:
//   * from WWL directly;
//   * from WWL through the nyc_ops@ group, which rewrites From to
//     NYC_Ops@americanflat.com and leaves wwllmail.com on Cc (Reply-To and
//     X-Original-Sender keep the real sender);
//   * to WWL, from sourcing: the factory's commercial invoice and packing list.
// A message that involves WWL in none of these ways is skipped.
function routeWwl(filename, subject, h) {
  var wwl = /wwllmail\.com/i;
  var fromWwl = wwl.test(h.from) || wwl.test(h.replyTo) || wwl.test(h.originalSender);
  if (!fromWwl && wwl.test(h.to)) return 'factory_shipping_docs';
  if (!fromWwl && !wwl.test(h.cc)) return null;
  return classifyWwl(filename, subject);
}

// WWL sends several document kinds from one address. Route on the attachment
// filename and the subject only; the 7501 is also re-detected from its text
// by the extract service.
function classifyWwl(filename, subject) {
  var s = (filename || '') + ' ' + (subject || '');
  if (/7501|entry\s*summary/i.test(s)) return 'customs_7501';
  if (/per\s*diem/i.test(s)) return 'wwl_per_diem';
  if (/invoice|\binv\b/i.test(s)) return 'wwl_invoice';
  return 'wwl_other';
}

// ------------------------------------------------------------------ entry points

/** Hourly trigger target. */
function exportFreightDocs() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) {
    console.log('another run holds the lock; skipping');
    return;
  }
  try {
    run_();
  } finally {
    lock.releaseLock();
  }
}

/** Run once by hand after pasting the script. Safe to run again. */
function installHourlyTrigger() {
  var existing = ScriptApp.getProjectTriggers().filter(function (t) {
    return t.getHandlerFunction() === 'exportFreightDocs';
  });
  if (existing.length) {
    console.log('hourly trigger already installed (' + existing.length + ')');
    return;
  }
  ScriptApp.newTrigger('exportFreightDocs').timeBased().everyHours(1).create();
  console.log('installed hourly trigger for exportFreightDocs');
}

// ------------------------------------------------------------------ main loop

function run_() {
  var started = Date.now();
  var bucket = PropertiesService.getScriptProperties().getProperty('FREIGHT_DOCS_BUCKET') || CONFIG.BUCKET;
  var labelId = labelId_(CONFIG.LABEL);

  // A thread can match more than one search (a WWL email titled "customs
  // release"); collect them all first so it is exported for each before the
  // label hides it from the next search.
  var byThread = {};
  var order = [];
  SEARCHES.forEach(function (search) {
    var q = search.query + ' -label:' + CONFIG.LABEL;
    var resp = Gmail.Users.Threads.list('me', { q: q, maxResults: CONFIG.THREADS_PER_SEARCH });
    (resp.threads || []).forEach(function (t) {
      if (!byThread[t.id]) {
        byThread[t.id] = [];
        order.push(t.id);
      }
      byThread[t.id].push(search);
    });
  });

  var stats = { threads: 0, created: 0, existed: 0, failedThreads: 0, deferred: 0 };
  for (var i = 0; i < order.length; i++) {
    if (Date.now() - started > CONFIG.TIME_BUDGET_MS) {
      stats.deferred = order.length - i;
      break;
    }
    var threadId = order[i];
    try {
      var r = exportThread_(bucket, threadId, byThread[threadId]);
      stats.created += r.created;
      stats.existed += r.existed;
      Gmail.Users.Threads.modify({ addLabelIds: [labelId] }, 'me', threadId);
      stats.threads++;
    } catch (e) {
      // Leave the thread unlabelled: the next run retries it, and the objects
      // already written are no-ops (412) the second time.
      stats.failedThreads++;
      console.error('thread ' + threadId + ' not exported: ' + (e && e.stack || e));
    }
  }
  console.log(JSON.stringify(stats));
}

function exportThread_(bucket, threadId, searches) {
  var thread = Gmail.Users.Threads.get('me', threadId, { format: 'full' });
  var out = { created: 0, existed: 0 };
  var tally = function (created) { if (created) out.created++; else out.existed++; };

  (thread.messages || []).forEach(function (msg) {
    var h = headers_(msg);
    var date = new Date(Number(msg.internalDate));
    var ym = Utilities.formatDate(date, 'UTC', 'yyyy/MM');

    searches.forEach(function (search) {
      if (search.savePdfs) {
        pdfParts_(msg.payload).forEach(function (part) {
          var docType = search.docType(part.filename, h.subject, h);
          if (!docType) return;
          var bytes = partBytes_(msg.id, part);
          var sha = sha256Hex_(bytes);
          var base = 'raw/' + docType + '/' + ym + '/' + sha;
          var meta = {
            sha256: sha,
            doc_type: docType,
            gmail_message_id: msg.id,
            gmail_thread_id: threadId,
            date: date.toISOString(),
            sender: h.from,
            to: h.to,
            cc: h.cc,
            subject: h.subject,
            attachment_filename: part.filename,
            size_bytes: bytes.length,
            search_name: search.name,
            search_query: search.query,
            exported_at: new Date().toISOString(),
            exporter: 'apps_script/freight_docs_export',
          };
          // Meta first: the PDF's arrival is what triggers extraction, and the
          // extractor reads the meta for provenance.
          tally(gcsCreate_(bucket, base + '.meta.json', jsonBytes_(meta), 'application/json'));
          tally(gcsCreate_(bucket, base + '.pdf', bytes, 'application/pdf'));
        });
      }
      var docTypeBody = search.saveBody && search.docType('', h.subject, h);
      if (docTypeBody) {
        var body = {
          gmail_message_id: msg.id,
          gmail_thread_id: threadId,
          date: date.toISOString(),
          sender: h.from,
          to: h.to,
          cc: h.cc,
          subject: h.subject,
          doc_type: docTypeBody,
          search_name: search.name,
          search_query: search.query,
          body_text: bodyText_(msg.payload),
          attachment_filenames: allParts_(msg.payload).filter(function (p) { return p.filename; })
            .map(function (p) { return p.filename; }),
          exporter: 'apps_script/freight_docs_export',
        };
        var name = 'raw/email/' + docTypeBody + '/' + ym + '/' + msg.id + '.json';
        tally(gcsCreate_(bucket, name, jsonBytes_(body), 'application/json'));
      }
    });
  });
  return out;
}

// ------------------------------------------------------------------ GCS

/**
 * Create gs://bucket/name unless it exists. true = created, false = already
 * there (HTTP 412 on ifGenerationMatch=0). Anything else throws.
 */
function gcsCreate_(bucket, name, bytes, contentType) {
  var url = 'https://storage.googleapis.com/upload/storage/v1/b/' + encodeURIComponent(bucket) +
    '/o?uploadType=media&ifGenerationMatch=0&name=' + encodeURIComponent(name);
  var resp = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: contentType,
    payload: bytes,
    headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
    muteHttpExceptions: true,
  });
  var code = resp.getResponseCode();
  if (code === 200) return true;
  if (code === 412) return false;
  throw new Error('GCS upload ' + name + ' failed: HTTP ' + code + ' ' + resp.getContentText().slice(0, 500));
}

// ------------------------------------------------------------------ Gmail helpers

function labelId_(name) {
  var labels = Gmail.Users.Labels.list('me').labels || [];
  for (var i = 0; i < labels.length; i++) {
    if (labels[i].name === name) return labels[i].id;
  }
  return Gmail.Users.Labels.create({
    name: name, labelListVisibility: 'labelShow', messageListVisibility: 'show',
  }, 'me').id;
}

function headers_(msg) {
  var names = { 'from': 'from', 'to': 'to', 'cc': 'cc', 'subject': 'subject', 'reply-to': 'replyTo',
                'x-original-sender': 'originalSender' };
  var out = { from: '', to: '', cc: '', subject: '', replyTo: '', originalSender: '' };
  ((msg.payload && msg.payload.headers) || []).forEach(function (h) {
    var key = names[h.name.toLowerCase()];
    if (key) out[key] = h.value;
  });
  return out;
}

function allParts_(part) {
  var out = [];
  (function walk(p) {
    if (!p) return;
    out.push(p);
    (p.parts || []).forEach(walk);
  })(part);
  return out;
}

function pdfParts_(payload) {
  return allParts_(payload).filter(function (p) {
    return p.filename && (p.mimeType === 'application/pdf' || /\.pdf$/i.test(p.filename));
  });
}

function partBytes_(messageId, part) {
  var data = part.body.data;
  if (!data && part.body.attachmentId) {
    data = Gmail.Users.Messages.Attachments.get('me', messageId, part.body.attachmentId).data;
  }
  return Utilities.base64DecodeWebSafe(data || '');
}

function bodyText_(payload) {
  var parts = allParts_(payload).filter(function (p) { return !p.filename && p.body && p.body.data; });
  var plain = parts.filter(function (p) { return p.mimeType === 'text/plain'; })[0];
  if (plain) return decode_(plain.body.data);
  var html = parts.filter(function (p) { return p.mimeType === 'text/html'; })[0];
  if (!html) return '';
  return decode_(html.body.data)
    .replace(/<(script|style)[\s\S]*?<\/\1>/gi, ' ')
    .replace(/<br\s*\/?>|<\/(p|div|tr|li|h\d)>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim();
}

function decode_(b64url) {
  return Utilities.newBlob(Utilities.base64DecodeWebSafe(b64url)).getDataAsString('UTF-8');
}

// ------------------------------------------------------------------ bytes

function sha256Hex_(bytes) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes).map(function (b) {
    return ('0' + (b & 0xff).toString(16)).slice(-2);
  }).join('');
}

function jsonBytes_(obj) {
  return Utilities.newBlob(JSON.stringify(obj, null, 2)).getBytes();
}
