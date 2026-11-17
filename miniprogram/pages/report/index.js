const { getLatestReport, requestErrorMessage } = require('../../utils/api')
const { displayReport, reportDateNotice } = require('../../utils/report')

Page({
  data: { report: null, dateNotice: '', loading: false, empty: false, error: '' },

  onLoad() {
    this.loadReport()
  },

  onShow() {
    if (this.data.report) this.setData({ dateNotice: reportDateNotice(this.data.report.date) })
  },

  onUnload() {
    this._unloaded = true
  },

  openHistory() {
    wx.navigateTo({ url: '/pages/history/index' })
  },

  openWatchlist() {
    wx.navigateTo({ url: '/pages/watchlist/index' })
  },

  comparePrevious() {
    const day = this.data.report && this.data.report.date
    if (day && day !== '—') wx.navigateTo({ url: `/pages/history/index?compareDay=${encodeURIComponent(day)}` })
  },

  async loadReport() {
    if (this.data.loading || this._unloaded) return
    this.setData({ loading: true, empty: false, error: '' })
    try {
      const payload = await getLatestReport()
      if (this._unloaded) return
      const isDailyReport = payload?.report_type === 'daily' &&
        typeof payload.id === 'string' && payload.id.trim() !== ''
      const report = isDailyReport ? displayReport(payload) : null
      if (report) {
        this.setData({ report, dateNotice: reportDateNotice(report.date) })
      } else {
        this.showLoadError('日报数据格式异常。')
      }
    } catch (error) {
      if (this._unloaded) return
      if (error.kind === 'no_daily_report' && !this.data.report) {
        this.setData({ empty: true })
      } else {
        this.showLoadError(requestErrorMessage(error, 'report'))
      }
    } finally {
      if (!this._unloaded) this.setData({ loading: false })
    }
  },

  showLoadError(message) {
    const { report } = this.data
    if (report) {
      this.setData({
        error: `刷新失败，仍显示上次加载的日报。${message}`,
        dateNotice: reportDateNotice(report.date)
      })
    } else {
      this.setData({ error: message })
    }
  }
})
