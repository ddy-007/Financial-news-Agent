const assert = require('node:assert/strict')
const { test } = require('node:test')
const { displayReport, reportDateNotice } = require('../miniprogram/utils/report')

function loadPage(name) {
  const path = require.resolve(`../miniprogram/pages/${name}/index`)
  let page
  global.Page = (definition) => {
    page = {
      ...definition,
      data: { ...definition.data },
      setData(update) { Object.assign(this.data, update) }
    }
  }
  try {
    delete require.cache[path]
    require(path)
  } finally {
    delete global.Page
  }
  return page
}

function mockRequests(t) {
  const requests = []
  global.wx = { request: (options) => requests.push(options) }
  t.after(() => { delete global.wx })
  return requests
}

test('日报格式化保留业务日期与零分，兼容旧摘要并避免渲染异常值', () => {
  const report = displayReport({
    report_day: '2026-11-12', date: '2026-11-13T18:30:00', score: 0,
    title: { unexpected: true }, sentiment: [], confidence: 'medium',
    content: { market_summary: ' ', raw: '原始摘要' }
  })
  assert.equal(report.date, '2026-11-12')
  assert.equal(report.score, '0.00')
  assert.equal(report.title, '未命名日报')
  assert.equal(report.sentiment, '—')
  assert.equal(report.confidence, '中')
  assert.equal(report.summary, '原始摘要')
  assert.equal(displayReport({ report_day: '2026-02-30', date: '2026-03-01T00:00:00' }).date, '2026-03-01')
  assert.equal(displayReport({ content: [] }).summary, '暂无摘要。')
  assert.equal(displayReport(null), null)
})

test('北京时间跨日后旧日报显示最近一期', () => {
  const before = Date.parse('2026-11-12T15:59:59Z')
  const after = Date.parse('2026-11-12T16:00:00Z')
  assert.equal(reportDateNotice('2026-11-12', before), '')
  assert.match(reportDateNotice('2026-11-12', after), /最近一期/)
  assert.equal(reportDateNotice('2026-11-13', after), '')
})

test('审计徽标只在两个字段一致通过时标绿', () => {
  for (const [status, flag, label, tone] of [
    ['passed', true, '审计通过', 'passed'],
    ['rejected', false, '审计未通过', 'rejected'],
    ['error', false, '审计调用失败', 'pending'],
    ['incomplete_sources', false, '原文证据不完整', 'pending'],
    ['skipped', false, '未执行审计', 'pending'],
    ['not_reviewed', false, '尚未复审', 'pending'],
    ['passed', false, '审计状态异常', 'pending'],
    ['rejected', true, '审计状态异常', 'pending'],
    ['unknown', true, '尚未完成核验', 'pending'],
    [undefined, true, '尚未完成核验', 'pending']
  ]) {
    const report = displayReport({ content: { critic_status: status, is_passed: flag } })
    assert.equal(report.auditLabel, label, String(status))
    assert.equal(report.auditTone, tone, String(status))
    assert.ok(report.auditNote)
  }
})

test('日报页处理 404、畸形响应和刷新失败，保留上次数据', async (t) => {
  const requests = mockRequests(t)
  const page = loadPage('report')
  let now = Date.parse('2026-11-01T15:59:59Z')
  t.mock.method(Date, 'now', () => now)
  let pending = page.loadReport()
  await page.loadReport()
  assert.equal(requests.length, 1)
  assert.match(requests[0].url, /\/api\/v1\/reports\/today$/)
  assert.equal(requests[0].method, 'GET')
  assert.equal(requests[0].timeout, 12000)
  requests[0].success({ statusCode: 404 })
  await pending
  assert.equal(page.data.empty, true)
  pending = page.loadReport()
  requests[1].success({ statusCode: 200, data: [] })
  await pending
  assert.match(page.data.error, /格式异常/)
  pending = page.loadReport()
  requests[2].success({ statusCode: 200, data: { title: '测试日报', report_day: '2026-11-01' } })
  await pending
  assert.equal(page.data.empty, false)
  assert.equal(page.data.dateNotice, '')
  now += 1000
  pending = page.loadReport()
  requests[3].fail({ errMsg: 'request:fail timeout' })
  await pending
  assert.match(page.data.error, /仍显示上次加载的日报。读取日报超时/)
  assert.match(page.data.dateNotice, /最近一期/)
  assert.equal(page.data.report.title, '测试日报')
  assert.equal(page.data.loading, false)
})

test('问答页阻止空白、超长及重复发送，显示等待并支持复制', async (t) => {
  const requests = mockRequests(t)
  const page = loadPage('chat')
  const intervals = new Set()
  let tick
  const time = t.mock.method(Date, 'now', () => 1000)
  t.mock.method(global, 'setInterval', (callback) => {
    tick = callback
    intervals.add(0)
    return 0
  })
  t.mock.method(global, 'clearInterval', (id) => intervals.delete(id))
  const input = (value) => page.onInput({ detail: { value } })
  input('   ')
  await page.sendQuestion()
  input('问'.repeat(1001))
  await page.sendQuestion()
  assert.equal(requests.length, 0)
  assert.match(page.data.error, /1000/)
  input('  有哪些进展？  ')
  const pending = page.sendQuestion()
  await page.sendQuestion()
  assert.equal(requests.length, 1)
  assert.equal(page.data.sending, true)
  assert.match(requests[0].url, /\/api\/v1\/agent\/chat$/)
  assert.equal(requests[0].method, 'POST')
  assert.equal(requests[0].timeout, 180000)
  assert.deepEqual(requests[0].data, { query: '有哪些进展？', chat_history: [] })
  time.mock.mockImplementation(() => 32000)
  tick()
  assert.equal(page.data.elapsedSeconds, 31)
  requests[0].success({ statusCode: 200, data: { answer: ' 测试回答\n第二行 ' } })
  await pending
  assert.equal(page.data.sending, false)
  assert.equal(intervals.size, 0)
  let copied
  wx.setClipboardData = ({ data }) => { copied = data }
  page.copyAnswer()
  assert.equal(copied, '测试回答\n第二行')
})

test('问答失败后保留输入，恢复发送且不自动重试', async (t) => {
  const requests = mockRequests(t)
  const page = loadPage('chat')
  page.onInput({ detail: { value: '原问题' } })
  const cases = [
    [req => req.fail({ errMsg: 'request:fail timeout' }), /服务端可能仍在处理/],
    [req => req.fail({ errMsg: 'request:fail connection refused' }), /无法连接后端/],
    [req => req.success({ statusCode: 500, data: { detail: 'internal details' } }), /服务暂时未完成/],
    [req => req.success({ statusCode: 429 }), /过于频繁/],
    [req => req.success({ statusCode: 422 }), /格式有误/],
    [req => req.success({ statusCode: 200, data: { answer: ' ' } }), /未收到有效回答/]
  ]
  for (const [index, [complete, expected]] of cases.entries()) {
    const pending = page.sendQuestion()
    complete(requests[index])
    await pending
    assert.match(page.data.error, expected)
    assert.equal(page.data.query, '原问题')
    assert.equal(page.data.sending, false)
    assert.equal(page._waitTimer, null)
    assert.equal(requests.length, index + 1)
  }
})

test('问答页卸载即清理计时，忽略晚到的请求结果', async (t) => {
  const requests = mockRequests(t)
  const page = loadPage('chat')
  page.onInput({ detail: { value: '尚未返回' } })
  const pending = page.sendQuestion()
  page.onUnload()
  assert.equal(page._waitTimer, null)
  page.setData = () => assert.fail('页面卸载后不应调用 setData')
  requests[0].success({ statusCode: 200, data: { answer: '晚到的回答' } })
  await pending
})
