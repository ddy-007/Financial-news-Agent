const { getLatestReport } = require('../../utils/api')

const CONFIDENCE_LABELS = new Map([['high', '高'], ['medium', '中'], ['low', '低']])
const AUDIT_LABELS = new Map([
  ['rejected', ['审计未通过', 'rejected']],
  ['error', ['审计调用失败', 'pending']],
  ['incomplete_sources', ['原文证据不完整', 'pending']],
  ['skipped', ['未执行审计', 'pending']],
  ['not_reviewed', ['尚未复审', 'pending']]
])

function displayReport(report) {
  if (!report || typeof report !== 'object' || Array.isArray(report)) {
    throw new Error('日报数据格式异常。')
  }
  const content = report.content || {}
  const [auditLabel, auditTone] = content.is_passed === true
    ? ['审计通过', 'passed']
    : AUDIT_LABELS.get(content.critic_status) || ['尚未完成核验', 'pending']
  const date = typeof report.date === 'string' ? report.date.slice(0, 10) : ''
  const summary = content.market_summary || content.raw

  return {
    title: report.title || '未命名日报',
    date: report.report_day || date || '—',
    sentiment: report.sentiment || '—',
    score: Number.isFinite(report.score) ? report.score.toFixed(2) : '—',
    confidence: CONFIDENCE_LABELS.get(report.confidence) || report.confidence || '—',
    summary: typeof summary === 'string' && summary.trim() ? summary : '暂无摘要。',
    auditLabel,
    auditTone
  }
}

Page({
  data: { report: null, loading: false, error: '' },

  onLoad() {
    this.loadReport()
  },

  async loadReport() {
    if (this.data.loading) return
    this.setData({ loading: true, error: '' })
    try {
      this.setData({ report: displayReport(await getLatestReport()) })
    } catch (error) {
      this.setData({ error: error.message || '加载日报失败。' })
    } finally {
      this.setData({ loading: false })
    }
  }
})
