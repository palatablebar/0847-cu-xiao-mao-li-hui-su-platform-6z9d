(function () {
  var state = { payload: null, kind: 'bars', run: null, approved: false, profile: 'dirty' };
  var $ = function (id) { return document.getElementById(id); };
  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch];
    });
  }
  function yuan(cents) {
    return (Number(cents || 0) / 100).toFixed(2);
  }
  async function request(url, options) {
    var response = await fetch(url, Object.assign({headers: {'Content-Type': 'application/json'}}, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  function setNotice(text) { $('notice').textContent = text || ''; }
  function renderIssues(data) {
    $('issue-count').textContent = data.issues.length + ' 条';
    $('issue-list').innerHTML = data.issues.length ? data.issues.map(function (item) {
      return '<li><span>' + esc(item.line || '-') + '</span><span>' + esc(item.key || '无主键') + '</span><code>' + esc(item.code) + '</code></li>';
    }).join('') : '<li class="empty">暂无异常</li>';
  }
  function renderBreakdown(items) {
    if (!items || !items.length) return '';
    return '<details><summary>' + items.length + ' 项</summary><ul>' + items.map(function (item) {
      return '<li>' + esc(item) + '</li>';
    }).join('') + '</ul></details>';
  }
  function renderRows(data) {
    var labels = Object.assign({label: '记录'}, state.resultColumns);
    var source = data.row_kind === 'source';
    var hasBreakdown = data.rows.some(function (row) { return row.breakdown && row.breakdown.length; });
    var keys = source ? state.inputColumns : Object.keys(labels).filter(function (key) {
      return data.rows.some(function (row) { return Object.hasOwn(row, key); });
    });
    $('row-count').textContent = source ? data.rows.length + ' 行预览' :
      data.summary.PASS + ' 通过 / ' + data.summary.REVIEW + ' 复核 / ' + data.summary.REJECT + ' 阻断';
    var tail = source ? '' : '<th>结论</th>' + (hasBreakdown ? '<th>计算分项</th>' : '');
    $('row-head').innerHTML = '<tr>' + keys.map(function (key) { return '<th>' + esc(labels[key] || key) + '</th>'; }).join('') + tail + '</tr>';
    $('row-body').innerHTML = data.rows.length ? data.rows.map(function (row) {
      var values = keys.map(function (key) { return '<td class="' + (typeof row[key] === 'number' ? 'num' : '') + '">' + esc(row[key]) + '</td>'; }).join('');
      var verdict = source ? '' : '<td><span class="verdict" data-v="' + esc(row.verdict) + '">' + esc(row.verdict || '') + '</span><br><small>' + esc(row.reason || '') + '</small></td>';
      var breakdown = hasBreakdown ? '<td>' + renderBreakdown(row.breakdown) + '</td>' : '';
      return '<tr>' + values + verdict + breakdown + '</tr>';
    }).join('') : '<tr><td class="empty" colspan="' + (keys.length + (source ? 0 : 1)) + '">没有可展示条目</td></tr>';
  }
  function renderLedger(data) {
    if (!data.ledger) { $('ledger-list').innerHTML = '<p class="empty">运行对账后显示</p>'; return; }
    var values = data.ledger;
    $('ledger-list').innerHTML = [
      ['来源行', values.counts.input], ['采信行', values.counts.accepted], ['隔离行', values.counts.isolated],
      ['精确重复', values.counts.duplicates], ['忽略行', values.counts.ignored], ['计算条目', values.counts.computed]
    ].map(function (row) { return '<div class="ledger-row"><span>' + row[0] + '</span><strong>' + row[1] + '</strong></div>'; }).join('') +
      '<div class="ledger-row"><span>' + esc(values.metric_field) + '</span><strong>' + esc(values.metric_total) + '</strong></div>' +
      values.checks.map(function (check) { return '<div class="ledger-row"><span>' + esc(check.label) + '</span><strong class="' + (check.ok ? 'check-ok' : 'check-bad') + '">' + (check.ok ? '通过' : '异常') + '</strong></div>'; }).join('');
  }

  // ---- 退货跨日记账 ----
  function returnsColumns() {
    return [['sale_id', '销售行'], ['line_qty', '原售数量'], ['refunded_qty', '已退款'], ['accepted_qty', '已验收'],
      ['matched_qty', '可冲回数量'], ['revenue_reversal_cents', '收入冲减分'], ['cost_reversal_cents', '成本冲回分'],
      ['net_margin_after_cents', '退货后毛利分']];
  }
  function renderReturns(result) {
    state.returns = result;
    var cols = returnsColumns();
    $('returns-head').innerHTML = '<tr>' + cols.map(function (pair) { return '<th>' + esc(pair[1]) + '</th>'; }).join('') + '</tr>';
    var rows = result.days || [];
    $('returns-body').innerHTML = rows.length ? rows.map(function (row) {
      return '<tr><td>' + esc(row.date) + '</td>' +
        '<td class="num">' + yuan(row.base_margin_cents) + '</td>' +
        '<td class="num">' + yuan(row.revenue_reversal_cents) + '</td>' +
        '<td class="num">' + yuan(row.cost_reversal_cents) + '</td>' +
        '<td class="num"><strong>' + yuan(row.net_margin_cents) + '</strong></td></tr>';
    }).join('') : '<tr><td class="empty" colspan="5">没有退货记录</td></tr>';
    $('returns-hint').textContent = (result.counts ? result.counts.documents + ' 张凭据' : '按日');
    var day = result.day || result.selected_day;
    if (day) {
      $('returns-day-value').textContent = yuan(day.net_margin_cents);
      $('returns-day-note').textContent = '收入冲减 ' + yuan(day.revenue_reversal_cents) + ' · 成本冲回 ' + yuan(day.cost_reversal_cents);
    }
  }
  function renderReturnsMain(result) {
    var summary = {PASS: 0, REVIEW: 0, REJECT: 0};
    (result.checks || []).forEach(function (check) { if (!check.ok) summary.REJECT += 1; });
    if (!summary.REJECT && (result.issues || []).length) summary.REVIEW = 1;
    if (!summary.REJECT && !(result.issues || []).length) summary.PASS = Math.max(1, (result.sales || []).length);
    result.summary = summary;
    result.rows = (result.sales || []).map(function (line) {
      return {
        label: line.sale_id, margin: line.net_margin_after_cents,
        verdict: line.unmatched_refund_qty ? 'REVIEW' : 'PASS',
        reason: line.unmatched_refund_qty ? 'AWAITING_ACCEPTANCE' : 'MATCHED',
        breakdown: ['退款 ' + line.refunded_qty + ' / 验收 ' + line.accepted_qty]
      };
    });
    result.row_kind = 'result';
    result.counts = Object.assign({input: (result.sales || []).length, accepted: (result.sales || []).length,
      isolated: result.counts ? result.counts.isolated : 0, duplicates: 0, computed: (result.sales || []).length}, result.counts || {});
    result.ledger = {
      counts: {input: result.counts.documents, accepted: (result.sales || []).length,
        isolated: result.counts.isolated, duplicates: result.counts.replayed, ignored: 0, computed: (result.sales || []).length},
      metric_field: '退货后净毛利元', metric_total: yuan(result.totals.net_margin_cents),
      checks: result.checks
    };
    result.chart = (result.days || []).slice(-12).map(function (row) {
      return {label: row.date, value: row.net_margin_cents / 100, limit: row.base_margin_cents / 100};
    });
    return result;
  }
  function render(data) {
    state.payload = data;
    $('chart-label').textContent = Object.values(state.resultColumns)[0];
    $('m-input').textContent = data.counts.input;
    $('m-accepted').textContent = data.counts.accepted;
    $('m-isolated').textContent = data.counts.isolated + (data.counts.duplicates || 0);
    $('m-rejected').textContent = data.summary.REJECT;
    renderRows(data);
    renderIssues(data);
    renderLedger(data);
    window.renderChart($('chart'), data.chart && data.chart.length ? (state.kind || 'bars') : 'bars', data.chart || []);
  }
  async function refreshReturns(date) {
    var data = await request('/api/returns/ledger', {method: 'POST',
      body: JSON.stringify({stored: true, date: date || $('returns-date').value})});
    renderReturns(data);
    return data;
  }
  async function importReturns() {
    await request('/api/returns/ingest', {method: 'POST',
      body: JSON.stringify({profile: 'returns'})});
    setNotice('样例退货凭据已导入');
  }
  function stageButtons(stages) {
    $('stage-nav').innerHTML = stages.map(function (stage, index) {
      return '<button type="button" data-stage="' + stage.id + '" aria-pressed="' + (index === 0 ? 'true' : 'false') + '">' + esc(stage.name) + '</button>';
    }).join('');
    Array.from(document.querySelectorAll('[data-stage]')).forEach(function (button) {
      button.addEventListener('click', function () {
        document.querySelectorAll('[data-stage]').forEach(function (other) { other.setAttribute('aria-pressed', String(other === button)); });
        if ($('profile').value === 'returns') { loadReturns(); return; }
        loadStage(button.getAttribute('data-stage'));
      });
    });
  }
  async function loadStage(stage) {
    try {
      setNotice('');
      document.body.setAttribute('aria-busy', 'true');
      var payload = await request('/api/stages/' + stage, {method: 'POST', body: JSON.stringify({profile: $('profile').value})});
      render(payload);
    } catch (error) { setNotice(error.message); }
    finally { document.body.removeAttribute('aria-busy'); }
  }
  async function loadReturns() {
    try {
      setNotice('');
      state.profile = $('profile').value;
      var data = await refreshReturns($('returns-date').value);
      if (!data.days || !data.days.length) {
        await importReturns();
        data = await refreshReturns($('returns-date').value);
      }
      var main = renderReturnsMain(data);
      render(main);
      $('run-status').textContent = '退货账 · ' + (data.days || []).length + ' 天';
      $('revision').textContent = 'rev 0';
      $('sign').disabled = true;
      $('hold').disabled = true;
      $('download').hidden = true;
    } catch (error) { setNotice(error.message); }
  }
  async function runBatch() {
    try {
      setNotice('');
      $('run').disabled = true;
      if ($('profile').value === 'returns') {
        var probe = await refreshReturns($('returns-date').value);
        if (!probe.days || !probe.days.length) {
          await importReturns();
        }
        var run = await request('/api/returns/runs', {method: 'POST',
          body: JSON.stringify({stored: true, date: $('returns-date').value})});
        state.run = run;
        state.approved = run.status === 'APPROVED';
        renderReturns(run.returns);
        render(renderReturnsMain(run.returns));
        $('run-status').textContent = run.status + ' · rev ' + run.revision;
        $('revision').textContent = 'rev ' + run.revision;
        $('sign').disabled = run.summary.REJECT > 0 || state.approved;
        $('hold').disabled = state.approved;
        $('download').hidden = !state.approved;
        if (state.approved) $('download').href = '/api/runs/' + run.run_id + '/evidence';
        return;
      }
      var batch = await request('/api/runs', {method: 'POST', body: JSON.stringify({profile: $('profile').value})});
      state.run = batch;
      state.approved = batch.status === 'APPROVED';
      render(batch);
      $('run-status').textContent = batch.status + ' · rev ' + batch.revision;
      $('revision').textContent = 'rev ' + batch.revision;
      $('sign').disabled = batch.summary.REJECT > 0 || state.approved;
      $('hold').disabled = state.approved;
      $('download').hidden = !state.approved;
      if (state.approved) $('download').href = '/api/runs/' + batch.run_id + '/evidence';
    } catch (error) { setNotice(error.message); }
    finally { $('run').disabled = false; }
  }
  async function review(decision) {
    if (!state.run) return;
    try {
      var run = await request('/api/runs/' + state.run.run_id + '/review', {
        method: 'POST',
        body: JSON.stringify({decision: decision, comment: $('comment').value, expected_revision: state.run.revision})
      });
      state.run = run;
      state.approved = run.status === 'APPROVED';
      $('run-status').textContent = run.status + ' · rev ' + run.revision;
      $('revision').textContent = 'rev ' + run.revision;
      $('sign').disabled = true;
      $('hold').disabled = true;
      $('download').hidden = !state.approved;
      if (state.approved) $('download').href = '/api/runs/' + run.run_id + '/evidence';
    } catch (error) { setNotice(error.message); }
  }
  async function boot() {
    try {
      var catalog = await request('/api/catalog');
      state.kind = catalog.chart;
      state.resultColumns = catalog.result_columns;
      state.inputColumns = catalog.columns;
      stageButtons(catalog.stages.slice(0, 5));
      document.querySelector('.rail-note').textContent = catalog.code + ' · ' + catalog.stages[6].name;
      await loadStage('intake');
      $('returns-date').value = $('returns-date').value || (catalog.returns && catalog.returns.date) || '';
    } catch (error) { setNotice(error.message); }
    $('run').addEventListener('click', runBatch);
    $('sign').addEventListener('click', function () { review('APPROVED'); });
    $('hold').addEventListener('click', function () { review('HELD'); });
    $('returns-query').addEventListener('click', function () {
      refreshReturns($('returns-date').value).catch(function (error) { setNotice(error.message); });
    });
    $('profile').addEventListener('change', function () {
      state.run = null; state.approved = false;
      $('sign').disabled = true; $('hold').disabled = true; $('download').hidden = true;
      $('run-status').textContent = '未执行批次'; $('revision').textContent = 'rev 0';
      if ($('profile').value === 'returns') { loadReturns(); return; }
      loadStage('intake');
    });
  }
  document.addEventListener('DOMContentLoaded', boot);
})();
