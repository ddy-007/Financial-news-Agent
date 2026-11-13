const { getEventTimeline, requestErrorMessage } = require('../../utils/api')

const LABELS = { report: '报道', response: '回应', correction: '更正' }

function displayDate(value) {
  return typeof value === 'string' ? value.replace('T', ' ').slice(0, 16) : ''
}

Page({
  data: {
    asOf: '', today: '', items: [], loading: false, loaded: false, error: '',
    truncated: false, skipped: 0, expandedId: ''
  },

  onLoad(options) {
    const newsId = options && options.newsId
    if (typeof newsId !== 'string' || !/^[A-Za-z0-9-]{1,64}$/.test(newsId)) {
      this.setData({ error: '缺少有效的新闻记录，请从关注页重新打开。' })
      return
    }
    this._newsId = newsId
    const today = new Date(Date.now() + 8 * 60 * 60 * 1000).toISOString().slice(0, 10)
    this.setData({ today })
    return this.loadTimeline('')
  },

  onUnload() {
    this._unloaded = true
  },

  selectDate(event) {
    return this.loadTimeline(event.detail.value)
  },

  showLatest() {
    return this.loadTimeline('')
  },

  retry() {
    return this.loadTimeline(this.data.asOf)
  },

  async loadTimeline(asOf) {
    if (this._unloaded || !this._newsId || (this.data.loading && this.data.asOf === asOf)) return
    const request = {}
    this._request = request
    this.setData({ asOf, items: [], loading: true, loaded: false, error: '', truncated: false, skipped: 0, expandedId: '' })
    try {
      const result = await getEventTimeline(this._newsId, asOf)
      if (this._unloaded || request !== this._request) return
      if (!result || !Array.isArray(result.items) || result.items.some(item =>
        !item || typeof item.id !== 'string' || typeof item.title !== 'string' ||
        !displayDate(item.published_at) || !displayDate(item.archived_at))) {
        this.setData({ error: '时间线数据格式异常，请重试。' })
        return
      }
      const items = result.items.map((item, index) => ({
        id: item.id, title: item.title, source: item.source,
        date: displayDate(item.published_at), archivedAt: displayDate(item.archived_at),
        label: index === 0 && item.kind === 'report' && !result.skipped ? '库内最早报道' : LABELS[item.kind] || '报道',
        preview: item.preview, previewTruncated: item.preview_truncated,
        url: typeof item.url === 'string' && /^https?:\/\/[^\s\\]+$/i.test(item.url) ? item.url : ''
      }))
      this.setData({ items, truncated: result.truncated === true, skipped: result.skipped || 0, loaded: true })
    } catch (error) {
      if (!this._unloaded && request === this._request) this.setData({ error: requestErrorMessage(error, 'news') })
    } finally {
      if (!this._unloaded && request === this._request) this.setData({ loading: false })
    }
  },

  toggleArticle(event) {
    const id = event.currentTarget.dataset.id
    this.setData({ expandedId: this.data.expandedId === id ? '' : id })
  },

  copyLink(event) {
    const item = this.data.items.find(item => item.id === event.currentTarget.dataset.id)
    if (item && item.url) wx.setClipboardData({
      data: item.url,
      fail: () => {
        if (!this._unloaded) wx.showToast({ title: '链接复制失败', icon: 'none' })
      }
    })
  }
})
