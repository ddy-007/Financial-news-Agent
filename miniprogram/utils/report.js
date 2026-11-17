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

function coverageNotice(freshness) {
  if (freshness && (freshness.coverage_incomplete === true ||
      (Number.isFinite(freshness.evidence_excluded_count) && freshness.evidence_excluded_count > 0))) {
    return '新闻覆盖受限，部分材料未纳入研判。'
  }
  if (freshness && freshness.readiness_scope === 'snapshot' &&
      freshness.coverage_incomplete === false && freshness.evidence_excluded_count === 0) return ''
  return '新闻覆盖范围未记录。'
}

function displayReport(report) {
  if (!isObject(report)) return null

  const content = isObject(report.content) ? report.content : {}
  const businessDay = validDay(text(report.report_day))
  const day = businessDay || validDay(text(report.date).slice(0, 10))
  const title = text(report.title)
  const summary = text(content.market_summary) || text(content.raw)
  if (!day && !title && !summary) return null

  const [auditLabel, auditTone, auditNote] = auditDisplay(content)
  const confidence = text(report.confidence)

  return {
    title: title || '未命名日报',
    date: day || '—',
    dateSource: businessDay ? '' : day ? '按生成日期显示' : '报告日期未提供',
    sentiment: text(report.sentiment) || '—',
    score: Number.isFinite(report.score) ? report.score.toFixed(2) : '—',
    confidence: CONFIDENCE_LABELS.get(confidence) || confidence || '—',
    summary: summary || '暂无摘要。',
    coverage: coverageNotice(content.data_freshness),
    auditLabel,
    auditTone,
    auditNote
  }
}

function displayDailyReport(record) {
  if (record?.report_type !== 'daily' || !text(record.id)) return null
  return displayReport(record)
}

function historyReports(records) {
  if (!Array.isArray(records)) return null
  const dated = new Map()
  const undated = []
  for (const [index, record] of records.entries()) {
    const display = displayDailyReport(record)
    if (!display) return null
    const entry = {
      ...display, key: index, id: text(record.id),
      scoreValue: Number.isFinite(record.score) ? record.score : null
    }
    if (entry.date === '—') {
      undated.push(entry)
    } else if (!dated.has(entry.date)) {
      // The API orders by generation time descending, so the first record wins.
      dated.set(entry.date, entry)
    }
  }
  return [...dated.keys()].sort().reverse().map(day => dated.get(day)).concat(undated)
}

function compareReports(first, second) {
  if (!first || !second || first.date === '—' || second.date === '—' || first.date === second.date) return null
  const [newer, older] = first.date > second.date ? [first, second] : [second, first]
  const delta = newer.scoreValue !== null && older.scoreValue !== null ? newer.scoreValue - older.scoreValue : NaN
  let scoreChange = ''
  if (Number.isFinite(delta)) {
    const rounded = delta.toFixed(2)
    scoreChange = Number(rounded) === 0 ? '0.00' : delta > 0 ? `+${rounded}` : rounded
  }
  return { newer, older, scoreChange }
}

module.exports = { displayReport, displayDailyReport, reportDateNotice, historyReports, compareReports }
