const assert = require('node:assert/strict')
const { test } = require('node:test')
const { displayReport, reportDateNotice, historyReports, compareReports } = require('../miniprogram/utils/report')
const { formatAnswer } = require('../miniprogram/utils/answer')

test('长回答的标题、加粗、时效提示、证据表和编号均保留为可读区块', () => {
  const sample = [
    '## 最新一份日报（2026-11-06）', '',
    '**报告**：《美联储鹰派叠加美股暴跌 全球风险资产承压》',
    '**情绪：偏空，综合分 -0.537**', '',
    '⚠️ **时效说明**：这份报告生成于 **2026 年 11 月 6 日**。', '',
    '## 一条主要风险', '',
    '| 环节 | 依据（含日期） |', '|---|---|',
    '| 触发源 | 纳指100 跌 6.24%（2026-11-06） |',
    '| A 股传导 | **成交额 1.67 万亿** |', '',
    '## 几处需要打折扣的地方', '',
    '1. **行情时间差**：`get_market_overview` 的快照滞后。',
    '2. **周末事件**：影响尚无数据验证。'
  ].join('\n')
  const blocks = formatAnswer(sample)
  assert.deepEqual(blocks.map(block => block.kind), ['heading', 'paragraph', 'notice', 'heading', 'table', 'heading', 'item', 'item'])
  assert.equal(blocks[0].spans[0].text, '最新一份日报（2026-11-06）')
  assert.ok(blocks[1].spans.some(span => span.style === 'strong' && span.text.includes('-0.537')))
  assert.ok(blocks[2].spans.some(span => span.style === 'strong' && span.text === '时效说明'))
  const rows = blocks[4].rows
  assert.equal(rows.length, 2)
  assert.equal(rows[0].cells[0].spans[0].text, '触发源')
  assert.equal(rows[1].cells[1].label[0].text, '依据（含日期）')
  assert.equal(rows[1].cells[1].spans[0].text, '成交额 1.67 万亿')
  assert.equal(blocks[7].marker, '2.')
  assert.ok(blocks[6].spans.some(span => span.style === 'code' && span.text === 'get_market_overview'))
})

test('不完整表格行保留原文，标签与代码只作为文本传给页面', () => {
  const blocks = formatAnswer([
    '| 名称 | 依据 |', '|---|---|', '| x \\| y | `a|b` |',
    '正文 | 保留原样', '| 不完整 |', '', '<img src="https://example.com/a.png">',
    '```text', '## 代码中的标题保留原样', '```'
  ].join('\n'))
  assert.deepEqual(blocks.map(block => block.kind), ['table', 'paragraph', 'paragraph', 'code'])
  assert.equal(blocks[0].rows[0].cells[0].spans.map(span => span.text).join(''), 'x | y')
  assert.equal(blocks[0].rows[0].cells[1].spans[0].text, 'a|b')
  assert.equal(blocks[1].spans[0].text, '正文 | 保留原样\n| 不完整 |')
  assert.equal(blocks[2].spans[0].text, '<img src="https://example.com/a.png">')
  assert.equal(blocks[3].text, '## 代码中的标题保留原样')
})

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

test('关注保存在本机，重新打开时只查询一个标题关键词', async (t) => {
  const requests = mockRequests(t)
  const storage = new Map()
  wx.getStorageSync = key => storage.get(key)
  wx.setStorageSync = (key, value) => storage.set(key, value)
  wx.navigateTo = ({ url }) => assert.equal(url, '/pages/watchlist/index')
  loadPage('report').openWatchlist()
  const page = loadPage('watchlist')
  await page.onLoad()
  assert.equal(requests.length, 0)
  page.onInput({ detail: { value: '  AI_%  ' } })
  let pending = page.addKeyword()
  assert.match(requests[0].url, /\/api\/v1\/news$/)
  assert.equal(requests[0].method, 'GET')
  assert.deepEqual(requests[0].data, { keyword: 'AI_%', limit: 20 })
  requests[0].success({ statusCode: 200, data: [{ id: '1', title: 'AI_% 报道', content: '正文'.repeat(3000), url: 'https://example.com/1' }] })
  await pending
  assert.equal(page.data.news[0].title, 'AI_% 报道')
  assert.equal(page.data.news[0].content, undefined)
  page.toggleNews({ currentTarget: { dataset: { id: '1' } } })
  assert.equal(page.data.expandedId, '1')
  assert.equal(page.data.expandedText, `${'正文'.repeat(1500)}\n…（正文预览）`)
  wx.setClipboardData = ({ data }) => assert.equal(data, 'https://example.com/1')
  page.copyLink({ currentTarget: { dataset: { id: '1' } } })
  wx.navigateTo = ({ url }) => assert.equal(url, '/pages/timeline/index?newsId=1')
  page.openTimeline({ currentTarget: { dataset: { id: '1' } } })
  const reopened = loadPage('watchlist')
  pending = reopened.onLoad()
  assert.deepEqual(reopened.data.keywords, ['AI_%'])
  requests[1].success({ statusCode: 200, data: [] })
  await pending
  reopened.onInput({ detail: { value: 'ai_%' } })
  pending = reopened.addKeyword()
  requests[2].success({ statusCode: 200, data: [] })
  await pending
  assert.deepEqual(reopened.data.keywords, ['AI_%'])
  await reopened.removeKeyword({ currentTarget: { dataset: { keyword: 'AI_%' } } })
  assert.equal(reopened.data.active, '')
  assert.deepEqual([...storage.values()], [[]])
})

test('事件时间线按日期回看，丢弃旧请求并保留原文来源', async (t) => {
  const requests = mockRequests(t)
  const page = loadPage('timeline')
  const latest = page.onLoad({ newsId: 'news-1' })
  assert.match(requests[0].url, /\/api\/v1\/news\/news-1\/timeline$/)
  const history = page.selectDate({ detail: { value: '2026-11-05' } })
  assert.deepEqual(requests[1].data, { as_of: '2026-11-05' })
  const item = { id: 'raw-1', title: '原始标题', source: '媒体', kind: 'report',
    published_at: '2026-11-05T12:01:02', archived_at: '2026-11-05T12:02:00',
    preview: '原文内容', url: 'https://example.com/1' }
  requests[1].success({ statusCode: 200, data: { items: [item] } })
  await history
  requests[0].success({ statusCode: 200, data: { items: [] } })
  await latest
  assert.equal(page.data.asOf, '2026-11-05')
  assert.equal(page.data.items[0].title, '原始标题')
  assert.equal(page.data.items[0].label, '库内最早报道')
  assert.equal(page.data.items[0].date, '2026-11-05 12:01')
  page.toggleArticle({ currentTarget: { dataset: { id: 'raw-1' } } })
  assert.equal(page.data.expandedId, 'raw-1')
  let copied
  wx.setClipboardData = ({ data }) => { copied = data }
  page.copyLink({ currentTarget: { dataset: { id: 'raw-1' } } })
  assert.equal(copied, item.url)
  const refresh = page.showLatest()
  requests[2].success({ statusCode: 200, data: { items: [null] } })
  await refresh
  assert.match(page.data.error, /格式异常/)
})

test('关注切换丢弃过期响应，网络错误可重试，存储失败不伪装成成功', async (t) => {
  const requests = mockRequests(t)
  wx.getStorageSync = () => ['半导体', '黄金']
  wx.setStorageSync = () => { throw Error('Storage unavailable') }
  const page = loadPage('watchlist')
  const old = page.onLoad()
  let current = page.selectKeyword({ currentTarget: { dataset: { keyword: '黄金' } } })
  requests[1].fail({ errMsg: 'request:fail timeout' })
  await current
  requests[0].success({ statusCode: 200, data: [{ id: 'old', title: '过期新闻' }] })
  await old
  assert.equal(page.data.active, '黄金')
  assert.match(page.data.newsError, /读取新闻超时/)
  assert.deepEqual(page.data.news, [])
  current = page.refreshNews()
  requests[2].success({ statusCode: 200, data: [] })
  await current
  assert.equal(page.data.loaded, true)
  page.onInput({ detail: { value: '政策' } })
  await page.addKeyword()
  assert.deepEqual(page.data.keywords, ['半导体', '黄金'])
  assert.equal(page.data.input, '政策')
  assert.match(page.data.storageError, /保存失败/)
})

test('历史日报按业务日排序去重，保留旧日期来源和无日期详情', () => {
  const reports = historyReports([
    { report_day: '2026-11-06', date: '2026-11-10T10:00:00', title: '回补的周五', score: 0 },
    { report_day: '2026-11-09', date: '2026-11-09T18:00:00', title: '周一', score: -0.54,
      content: { is_passed: false, critic_status: 'rejected', data_freshness: { coverage_incomplete: true } } },
    { report_day: '2026-11-06', date: '2026-11-06T18:00:00', title: '较早的周五', score: 0.44 },
    { date: '2026-11-05T18:00:00', score: null,
      content: { data_freshness: { readiness_scope: 'historical_unknown', coverage_incomplete: false, evidence_excluded_count: 0 } } },
    { title: '无日期但可查看摘要', content: { market_summary: '摘要' } }, null
  ])
  assert.deepEqual(reports.map(report => report.date), ['2026-11-09', '2026-11-06', '2026-11-05', '—'])
  assert.equal(reports[1].title, '回补的周五')
  assert.equal(reports[2].dateSource, '按生成日期显示')
  assert.match(reports[2].coverage, /未记录/)
  assert.equal(reports[3].summary, '摘要')
  const pair = compareReports(reports[1], reports[0])
  assert.equal(pair.newer.auditLabel, '审计未通过')
  assert.match(pair.newer.coverage, /覆盖受限/)
  assert.equal(pair.older.score, '0.00')
  assert.equal(pair.scoreChange, '-0.54')
  assert.equal(compareReports(reports[2], reports[0]).scoreChange, '')
  assert.equal(compareReports(reports[3], reports[0]), null)
  assert.equal(compareReports(reports[0], reports[0]), null)
})

test('首页入口与历史页用真实接口形状联通，可查看详情和切换对照日期', async (t) => {
  const requests = mockRequests(t)
  const home = loadPage('report')
  const history = loadPage('history')
  let url
  wx.navigateTo = (options) => { url = options.url }
  wx.pageScrollTo = () => {}
  home.openHistory()
  assert.equal(url, '/pages/history/index')
  home.setData({ report: { date: '2026-11-09' } })
  home.comparePrevious()
  assert.equal(url, '/pages/history/index?compareDay=2026-11-09')

  const pending = history.onLoad({ compareDay: '2026-11-09' })
  await history.loadReports()
  assert.equal(requests.length, 1)
  assert.match(requests[0].url, /\/api\/v1\/reports\?report_type=daily&limit=10$/)
  assert.equal(requests[0].method, 'GET')
  requests[0].success({ statusCode: 200, data: [
    { report_day: '2026-11-10', score: 0.3 },
    { report_day: '2026-11-09', score: -0.54 },
    { report_day: '2026-11-06', score: 0.44, content: { market_summary: '周五摘要' } }
  ] })
  await pending
  assert.equal(history.data.showCompare, true)
  assert.equal(history.data.comparison.older.date, '2026-11-06')
  assert.equal(history.data.comparison.scoreChange, '-0.98')
  history.selectFirst({ detail: { value: '3' } })
  assert.equal(history.data.comparison, null)
  assert.match(history.data.comparisonHint, /请选择/)
  history.selectFirst({ detail: { value: '1' } })
  assert.equal(history.data.comparison.newer.date, '2026-11-10')
  history.selectReport({ currentTarget: { dataset: { index: 2 } } })
  assert.equal(history.data.showCompare, false)
  assert.equal(history.data.selected.summary, '周五摘要')
})

test('历史请求可从网络错误恢复，区分空列表和没有上期', async (t) => {
  const requests = mockRequests(t)
  const history = loadPage('history')
  let pending = history.onLoad({ compareDay: '2026-11-09' })
  requests[0].fail({ errMsg: 'request:fail timeout' })
  await pending
  assert.equal(history.data.loaded, false)
  assert.match(history.data.error, /超时/)
  pending = history.loadReports()
  requests[1].success({ statusCode: 200, data: [] })
  await pending
  assert.equal(history.data.loaded, true)
  assert.deepEqual(history.data.reports, [])
  pending = history.loadReports()
  requests[2].success({ statusCode: 200, data: [{ report_day: '2026-11-10', score: 1 }] })
  await pending
  assert.equal(history.data.showCompare, true)
  assert.equal(history.data.comparison, null)
  assert.match(history.data.comparisonHint, /暂无/)
  assert.equal(history.data.error, '')
})

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
  requests[0].success({ statusCode: 200, data: { answer: ' ## 测试回答\n**第二行** ' } })
  await pending
  assert.equal(page.data.sending, false)
  assert.equal(intervals.size, 0)
  assert.deepEqual(page.data.answerBlocks.map(block => block.kind), ['heading', 'paragraph'])
  let copied
  wx.setClipboardData = ({ data }) => { copied = data }
  page.copyAnswer()
  assert.equal(copied, '## 测试回答\n**第二行**')
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

test('追问只携带成功问答，失败保留上一轮，新对话从空上下文开始', async (t) => {
  const requests = mockRequests(t)
  const page = loadPage('chat')
  const input = value => page.onInput({ detail: { value } })
  input('市场情绪如何？')
  let pending = page.sendQuestion()
  assert.deepEqual(requests[0].data.chat_history, [])
  requests[0].success({ statusCode: 200, data: { answer: '**偏空**' } })
  await pending
  assert.equal(page.data.query, '')
  input('为什么？')
  pending = page.sendQuestion()
  page.newConversation()
  assert.deepEqual(requests[1].data, { query: '为什么？', chat_history: [['市场情绪如何？', '**偏空**']] })
  requests[1].success({ statusCode: 500 })
  await pending
  assert.equal(page.data.query, '为什么？')
  assert.equal(page.data.answer, '**偏空**')
  assert.equal(page.data.recentQuestions.length, 1)
  pending = page.sendQuestion()
  assert.deepEqual(requests[2].data, requests[1].data)
  requests[2].success({ statusCode: 200, data: { answer: '科技板块承压。' } })
  await pending
  assert.equal(page.data.answer, '科技板块承压。')
  page.selectTurn({ currentTarget: { dataset: { index: 0 } } })
  assert.equal(page.data.answer, '**偏空**')
  page.newConversation()
  assert.equal(page.data.answer, '')
  assert.deepEqual(page.data.recentQuestions, [])
  input('新话题')
  pending = page.sendQuestion()
  assert.deepEqual(requests[3].data, { query: '新话题', chat_history: [] })
  requests[3].success({ statusCode: 200, data: { answer: '新答案' } })
  await pending
})

test('问答上下文最多三轮且不超过字符预算，较长原答案仍完整显示', async (t) => {
  const requests = mockRequests(t)
  const page = loadPage('chat')
  const ask = question => {
    page.onInput({ detail: { value: question } })
    return page.sendQuestion()
  }
  for (let index = 0; index < 4; index++) {
    const pending = ask(`问题${index}`)
    requests[index].success({ statusCode: 200, data: { answer: `回答${index}` } })
    await pending
  }
  let pending = ask('长答案')
  assert.deepEqual(requests[4].data.chat_history, [['问题1', '回答1'], ['问题2', '回答2'], ['问题3', '回答3']])
  const answer = '内容'.repeat(5000)
  requests[4].success({ statusCode: 200, data: { answer } })
  await pending
  assert.equal(page.data.answer, answer)
  pending = ask('继续解释')
  const history = requests[5].data.chat_history
  assert.equal(history.length, 1)
  assert.equal(history[0][0], '长答案')
  assert.match(history[0][1], /较长回答已截断/)
  assert.ok(history[0][0].length + history[0][1].length <= 8000)
  requests[5].success({ statusCode: 200, data: { answer: '说明' } })
  await pending
})

test('问答来源跟随所选轮次，只复制合法链接，兼容旧接口', async (t) => {
  const requests = mockRequests(t)
  const page = loadPage('chat')
  let copied = ''
  wx.setClipboardData = ({ data }) => { copied = data }
  page.onInput({ detail: { value: '第一问' } })
  let pending = page.sendQuestion()
  requests[0].success({ statusCode: 200, data: { answer: '第一答', sources: [
    { id: 'a', title: '新闻 A', published_at: '2026-11-05T12:00:00', url: 'https://example.com/a' },
    { id: 'a', title: '重复' },
    { id: 'b', title: '新闻 B', url: 'javascript:alert(1)' },
    { id: 'bad', title: null }
  ] } })
  await pending
  assert.deepEqual(page.data.sources.map(source => source.title), ['新闻 A', '新闻 B'])
  assert.equal(page.data.sources[0].publishedAt, '2026-11-05 12:00:00')
  page.copySource({ currentTarget: { dataset: { index: 1 } } })
  assert.equal(copied, '')
  page.copySource({ currentTarget: { dataset: { index: 0 } } })
  assert.equal(copied, 'https://example.com/a')
  page.onInput({ detail: { value: '第二问' } })
  pending = page.sendQuestion()
  assert.deepEqual(requests[1].data.chat_history, [['第一问', '第一答']])
  requests[1].success({ statusCode: 200, data: { answer: '旧接口只返回文本' } })
  await pending
  assert.deepEqual(page.data.sources, [])
  page.selectTurn({ currentTarget: { dataset: { index: 0 } } })
  assert.equal(page.data.sources[0].id, 'a')
  page.newConversation()
  assert.deepEqual(page.data.sources, [])
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
