const { getWatchedNews, requestErrorMessage } = require('../../utils/api')

const STORAGE_KEY = 'welcomeScreen.watchlist.v1'
const MAX_KEYWORDS = 12
const MAX_LENGTH = 30
const MAX_PREVIEW_LENGTH = 3000

function readKeywords(saved) {
  if (!Array.isArray(saved)) return []
  const keywords = []
  const seen = new Set()
  for (const item of saved) {
    if (typeof item !== 'string') continue
    const keyword = item.trim()
    const key = keyword.toLowerCase()
    if (!keyword || keyword.length > MAX_LENGTH || seen.has(key)) continue
    keywords.push(keyword)
    seen.add(key)
    if (keywords.length === MAX_KEYWORDS) break
  }
  return keywords
}

function displayNews(records) {
  const news = []
  const seen = new Set()
  for (const record of records) {
    if (!record || typeof record.id !== 'string' || !record.id || seen.has(record.id) ||
        typeof record.title !== 'string' || !record.title.trim()) continue
    seen.add(record.id)
    news.push({
      id: record.id,
      title: record.title,
      source: typeof record.source === 'string' ? record.source : '',
      date: typeof record.publish_time === 'string' ? record.publish_time.replace('T', ' ').slice(0, 16) : '',
      content: typeof record.content === 'string' ? record.content.trim() : '',
      url: typeof record.url === 'string' && /^https?:\/\/[^\s\\]+$/i.test(record.url) ? record.url : ''
    })
    if (news.length === 20) break
  }
  return news
}

Page({
  data: {
    keywords: [], active: '', input: '', maxLength: MAX_LENGTH, storageError: '',
    news: [], expandedId: '', expandedText: '', expandedLink: false,
    loading: false, loaded: false, newsError: '', storageUnavailable: false
  },

  onLoad() {
    let keywords
    try {
      keywords = readKeywords(wx.getStorageSync(STORAGE_KEY))
    } catch (error) {
      this.setData({ storageError: '暂时无法读取本机关注。请重新打开此页后再编辑。', storageUnavailable: true })
      return
    }
    this.setData({ keywords })
    if (keywords.length) return this.loadNews(keywords[0])
  },

  onUnload() {
    this._unloaded = true
    this._news = []
  },

  onInput(event) {
    this.setData({ input: event.detail.value })
  },

  saveKeywords(keywords) {
    if (this.data.storageUnavailable) return false
    try {
      wx.setStorageSync(STORAGE_KEY, keywords)
    } catch (error) {
      this.setData({ storageError: '保存失败，请稍后重试。' })
      return false
    }
    this.setData({ keywords, storageError: '' })
    return true
  },

  addKeyword() {
    if (this.data.storageUnavailable) return
    const keyword = this.data.input.trim()
    if (!keyword) return
    if (keyword.length > MAX_LENGTH) {
      this.setData({ storageError: `关键词最多 ${MAX_LENGTH} 个字符。` })
      return
    }
    const existing = this.data.keywords.find(item => item.toLowerCase() === keyword.toLowerCase())
    if (existing) {
      this.setData({ input: '', storageError: '' })
      return this.loadNews(existing)
    }
    if (this.data.keywords.length >= MAX_KEYWORDS) {
      this.setData({ storageError: `最多关注 ${MAX_KEYWORDS} 个关键词。` })
      return
    }
    if (!this.saveKeywords([...this.data.keywords, keyword])) return
    this.setData({ input: '' })
    return this.loadNews(keyword)
  },

  removeKeyword(event) {
    const keyword = event.currentTarget.dataset.keyword
    const index = this.data.keywords.indexOf(keyword)
    if (index < 0) return
    const keywords = this.data.keywords.filter(item => item !== keyword)
    if (!this.saveKeywords(keywords) || this.data.active !== keyword) return
    return this.loadNews(keywords[Math.min(index, keywords.length - 1)] || '')
  },

  selectKeyword(event) {
    const keyword = event.currentTarget.dataset.keyword
    if (this.data.keywords.includes(keyword)) return this.loadNews(keyword)
  },

  refreshNews() {
    return this.loadNews(this.data.active)
  },

  async loadNews(keyword) {
    if (this._unloaded || (this.data.loading && keyword === this.data.active)) return
    const request = {}
    this._request = request
    this._news = []
    this.setData({
      active: keyword, news: [], expandedId: '', expandedText: '', expandedLink: false,
      loading: Boolean(keyword), loaded: false, newsError: ''
    })
    if (!keyword) return

    try {
      const records = await getWatchedNews(keyword)
      if (this._unloaded || request !== this._request) return
      if (!Array.isArray(records)) {
        this.setData({ newsError: '新闻列表格式异常，请重试。' })
      } else {
        this._news = displayNews(records)
        const news = this._news.map(({ id, title, source, date }) => ({ id, title, source, date }))
        this.setData({ news, loaded: true })
      }
    } catch (error) {
      if (!this._unloaded && request === this._request) this.setData({ newsError: requestErrorMessage(error, 'news') })
    } finally {
      if (!this._unloaded && request === this._request) this.setData({ loading: false })
    }
  },

  toggleNews(event) {
    const id = event.currentTarget.dataset.id
    const item = (this._news || []).find(item => item.id === id)
    if (!item || this.data.expandedId === id) {
      this.setData({ expandedId: '', expandedText: '', expandedLink: false })
      return
    }
    const preview = item.content.slice(0, MAX_PREVIEW_LENGTH).replace(/[\uD800-\uDBFF]$/, '')
    this.setData({
      expandedId: id, expandedLink: Boolean(item.url),
      expandedText: item.content.length > MAX_PREVIEW_LENGTH ? `${preview}\n…（正文预览）` : item.content
    })
  },

  copyLink(event) {
    const item = (this._news || []).find(item => item.id === event.currentTarget.dataset.id)
    if (item && item.url) wx.setClipboardData({
      data: item.url,
      fail: () => {
        if (!this._unloaded) wx.showToast({ title: '链接复制失败', icon: 'none' })
      }
    })
  },

  openTimeline(event) {
    const id = event.currentTarget.dataset.id
    if (this.data.news.some(item => item.id === id)) {
      wx.navigateTo({ url: `/pages/timeline/index?newsId=${encodeURIComponent(id)}` })
    }
  }
})
