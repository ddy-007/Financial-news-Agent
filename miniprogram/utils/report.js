const CONFIDENCE_LABELS = new Map([['high', '高'], ['medium', '中'], ['low', '低']])
const AUDIT_LABELS = new Map([
  ['passed', ['审计通过', 'passed', '本期报告已完成系统核验，关键结论仍可结合原文阅读。']],
  ['rejected', ['审计未通过', 'rejected', '系统发现需修正的内容，请谨慎参考。']],
  ['error', ['审计调用失败', 'pending', '系统未能给出有效审计结论。']],
  ['incomplete_sources', ['原文证据不完整', 'pending', '部分证据缺失，系统无法完成核验。']],
  ['skipped', ['未执行审计', 'pending', '本期报告尚无审计结论。']],
  ['not_reviewed', ['尚未复审', 'pending', '本期报告尚无审计结论。']],
  ['pending', ['尚未完成核验', 'pending', '本期报告尚无审计结论。']]
])

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function text(value) {
  return typeof value === 'string' ? value.trim() : ''
}

function validDay(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return ''
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value ? value : ''
}

function reportDateNotice(day, now = Date.now()) {
  if (day === '—') return '报告日期未提供。'
  // 业务日使用北京时间，避免真机时区改变“今日”的判断。
  const today = new Date(now + 8 * 60 * 60 * 1000).toISOString().slice(0, 10)
  return day === today ? '' : '当前展示最近一期日报，日期见上方。'
}

function auditDisplay(content) {
  const status = content.critic_status
  const labels = AUDIT_LABELS.get(status)
  if (!labels) return ['尚未完成核验', 'pending', '本期报告尚无可确认的审计结论。']
  if ((status === 'passed') !== (content.is_passed === true)) {
    return ['审计状态异常', 'pending', '审计字段不一致，暂无法确认核验结果。']
  }
  return labels
}

function displayReport(report) {
  if (!isObject(report)) return null

  const content = isObject(report.content) ? report.content : {}
  const [auditLabel, auditTone, auditNote] = auditDisplay(content)
  const day = validDay(text(report.report_day)) || validDay(text(report.date).slice(0, 10))
  const confidence = text(report.confidence)

  return {
    title: text(report.title) || '未命名日报',
    date: day || '—',
    sentiment: text(report.sentiment) || '—',
    score: Number.isFinite(report.score) ? report.score.toFixed(2) : '—',
    confidence: CONFIDENCE_LABELS.get(confidence) || confidence || '—',
    summary: text(content.market_summary) || text(content.raw) || '暂无摘要。',
    auditLabel,
    auditTone,
    auditNote
  }
}

module.exports = { displayReport, reportDateNotice }
