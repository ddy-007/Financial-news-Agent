const { getDailyReports, requestErrorMessage } = require('../../utils/api')
const { historyReports, compareReports } = require('../../utils/report')

Page({
  data: {
    reports: [], datedReports: [], dateOptions: [], loading: false, loaded: false, error: '',
    selected: null, showCompare: false, firstIndex: -1, secondIndex: -1, comparison: null, comparisonHint: ''
  },

  onLoad(options) {
    this._compareDay = options && options.compareDay
    return this.loadReports()
  },

  onUnload() {
    this._unloaded = true
  },

  async loadReports() {
    if (this.data.loading || this._unloaded) return
    this.setData({ loading: true, error: '' })
    try {
      const records = await getDailyReports()
      if (this._unloaded) return
      if (!Array.isArray(records)) {
        this.setData({ error: '日报列表格式异常，请重试。' })
        return
      }
      const reports = historyReports(records)
      const datedReports = reports.filter(report => report.date !== '—')
      const compareDay = this._compareDay
      const previousDay = this.data.datedReports[this.data.secondIndex]?.date
      const firstDay = this.data.datedReports[this.data.firstIndex]?.date || compareDay
      const firstIndex = firstDay ? datedReports.findIndex(report => report.date === firstDay) : datedReports.length ? 0 : -1
      const secondIndex = previousDay
        ? datedReports.findIndex(report => report.date === previousDay)
        : firstIndex >= 0 && firstIndex + 1 < datedReports.length ? firstIndex + 1 : -1
      const selectedDay = this.data.selected?.date
      const selected = (selectedDay && selectedDay !== '—' && reports.find(report => report.date === selectedDay)) || null
      const comparison = compareReports(datedReports[firstIndex], datedReports[secondIndex])
      this.setData({
        reports, datedReports, dateOptions: ['请选择', ...datedReports.map(report => report.date)],
        selected, loaded: true, firstIndex, secondIndex,
        showCompare: this.data.loaded ? this.data.showCompare : Boolean(compareDay),
        comparison,
        comparisonHint: !comparison && (compareDay || datedReports.length < 2)
          ? '当前列表暂无可对照的上期日报。'
          : '请选择两个不同的业务日。'
      })
    } catch (error) {
      if (!this._unloaded) this.setData({ error: requestErrorMessage(error, 'report') })
    } finally {
      if (!this._unloaded) this.setData({ loading: false })
    }
  },

  selectReport(event) {
    const selected = this.data.reports[Number(event.currentTarget.dataset.index)]
    if (!selected) return
    this.setData({ selected, showCompare: false })
    wx.pageScrollTo({ scrollTop: 0, duration: 0 })
  },

  openCompare() {
    this.setData({ showCompare: true })
    wx.pageScrollTo({ scrollTop: 0, duration: 0 })
  },

  selectFirst(event) {
    this.setComparison(Number(event.detail.value) - 1, this.data.secondIndex)
  },

  selectSecond(event) {
    this.setComparison(this.data.firstIndex, Number(event.detail.value) - 1)
  },

  setComparison(firstIndex, secondIndex) {
    const { datedReports } = this.data
    this.setData({
      firstIndex, secondIndex,
      comparison: compareReports(datedReports[firstIndex], datedReports[secondIndex]),
      comparisonHint: '请选择两个不同的业务日。'
    })
  }
})
